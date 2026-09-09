#!/usr/bin/env python3
"""
TDD slice test for issue #288: registry-derived bundle `profile()` +
profile-conditional CI gates (knowledge playbooks go green with no anchor
map).

RED (before this slice): `scripts/playbook_registry.py` has no `profile()`
function -- every check below that calls it fails.

GREEN (after): `profile()` classifies a resolved registry entry as
"precision" iff BOTH `anchor_map_path` and `section_config_path` are set,
else "knowledge"; the profile-conditional gates named in the issue print an explicit
"SKIP (knowledge profile): ..." line for a knowledge entry instead of
hard-failing on a null anchor map, and continue to fully enforce a
precision entry.

Issue #631 deleted two of the four gates the issue named
(tests/anchor/test_form_coverage.py and tests/anchor/test_heading_hash_
drift.py) together with the anchor-map / standard-form-diff subsystem they
gated. The two that survive -- tests/lint-acceptable-variations.py and
scripts/eval_harness.py's detector D-gate -- are exercised below on both
halves of the contract: they must SKIP a knowledge entry and still ENFORCE
a precision one.

Uses the synthetic-registry pattern documented in
scripts/playbook_registry.py's module docstring (see also
tests/test_playbook_id_contract.py's `_build_synthetic_registry`): a
self-contained temp dir laid out exactly like the real repo (playbooks/,
standard-forms/, tests/gold-fixtures*/), with `playbook_registry.
REGISTRY_PATH` monkeypatched to point at it -- this test never reads or
writes the real playbooks/registry.json.

Run with: python3 tests/test_registry_profiles.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402

# Imported HERE, at module load time, against the REAL playbooks/registry.json
# (before any test below monkeypatches playbook_registry.REGISTRY_PATH).
# scripts/canonicalize.py resolves the "eiaa" playbook_id at IMPORT time
# (module-level `PLAYBOOK_PATH = playbook_registry.resolve_playbook(...)`),
# and scripts/eval_harness.py transitively imports canonicalize -- importing
# eval_harness for the first time while REGISTRY_PATH pointed at a synthetic
# registry lacking "eiaa" would blow up at import, not at the call we're
# actually testing. Each gate's main()/entry point below does its own
# playbook_id resolution at CALL time (late-bound, per playbook_registry.
# resolve_playbook's docstring), so importing early and calling later under a
# patched REGISTRY_PATH is safe and exercises exactly what we want to test.
import eval_harness  # noqa: E402


def _load_module_from_path(name: str, path: Path):
    """Import a hyphenated-filename script (e.g. tests/lint-acceptable-
    variations.py) as a module. Not importable via a normal `import`
    statement (hyphens aren't valid identifiers)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Same import-order rationale as above: load this hyphenated-filename module
# now, against the real registry, before any test patches REGISTRY_PATH.
_LINT_ACCEPTABLE_VARIATIONS = _load_module_from_path(
    "lint_acceptable_variations_288",
    REPO_ROOT / "tests" / "lint-acceptable-variations.py",
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _run(fn, *args, **kwargs) -> tuple[int, str]:
    """Run a gate's main()-like callable, capturing stdout and normalizing
    a SystemExit (or a returned int) to an exit code."""
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, int):
                code = result
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# Synthetic registry builder
# ---------------------------------------------------------------------------
# One precision entry ("precision-fixture", a stand-in for eiaa's shape:
# both anchor_map_path and section_config_path set) and one knowledge entry
# ("knowledge-fixture", a stand-in for a knowledge-profile registry entry:
# both null -- issue #288 committed the original "synthetic-knowledge" example
# of this shape; issue #343 later renamed/reshaped it to a precision-profile
# "sample-agreement" entry, so no committed knowledge-profile entry remains,
# but the shape itself is still exercised here via this synthetic fixture).
# Callers toggle which artifacts exist via the keyword flags below to
# exercise the RED/GREEN paths of each gate.

# Kept as the precision playbook's realistic shape: a precision entry
# carries `anchor_migrations` covering its anchors. The gate that consumed
# them (the heading-hash-drift gate) was deleted by issue #631; the field
# stays on the fixture so the fixture still resembles a real precision
# playbook rather than a stripped-down one.
_DRIFT_COVERING_MIGRATIONS = [
    {"anchor": "sec-8", "note": "synthetic fixture migration"},
    {"anchor": "sec-9", "note": "synthetic fixture migration"},
]


def _build_registry(
    root: Path,
    *,
    include_precision: bool = True,
    include_knowledge: bool = True,
    precision_anchor_migrations=_DRIFT_COVERING_MIGRATIONS,
    omit_precision_anchor_map_file: bool = False,
) -> Path:
    registry: dict = {"playbooks": {}}

    if include_precision:
        playbook_path = "playbooks/precision-fixture-v1.0.0.json"
        section_config_path = "playbooks/precision-fixture-v1.0.0.sections.json"
        anchor_map_path = "standard-forms/precision-fixture-v1.0.0.anchor-map.json"
        fixtures_dir = "tests/gold-fixtures-precision-fixture"

        _write_json(root / playbook_path, {
            "playbook": {"id": "precision-fixture", "version": "1.0.0"},
            "topics": [
                {"id": "t1", "section_anchors": ["sec-1", "sec-2"]},
            ],
            "hard_rejections": [],
            "anchor_migrations": precision_anchor_migrations or [],
        })
        _write_json(root / section_config_path, {"sections": []})
        if not omit_precision_anchor_map_file:
            _write_json(root / anchor_map_path, {
                "schema_version": "1",
                "anchors": {
                    "sec-1": _sha256_text("Section One"),
                    "sec-2": _sha256_text("Section Two"),
                },
                "coverage_exempt_anchors": [],
                "coverage_exempt_rationales": {},
            })
        (root / fixtures_dir).mkdir(parents=True, exist_ok=True)

        registry["playbooks"]["precision-fixture"] = {
            "playbook_id": "precision-fixture",
            "playbook_path": playbook_path,
            "anchor_map_path": anchor_map_path,
            "section_config_path": section_config_path,
            "fixtures_dir": fixtures_dir,
            "standard_form_docx": None,
        }

    if include_knowledge:
        playbook_path = "playbooks/knowledge-fixture-v1.0.0.json"
        fixtures_dir = "tests/gold-fixtures-knowledge-fixture"
        _write_json(root / playbook_path, {
            "playbook": {"id": "knowledge-fixture", "version": "1.0.0"},
            "topics": [],
        })
        (root / fixtures_dir).mkdir(parents=True, exist_ok=True)

        registry["playbooks"]["knowledge-fixture"] = {
            "playbook_id": "knowledge-fixture",
            "playbook_path": playbook_path,
            "anchor_map_path": None,
            "section_config_path": None,
            "fixtures_dir": fixtures_dir,
            "standard_form_docx": None,
        }

    registry_path = root / "playbooks" / "registry.json"
    _write_json(registry_path, registry)
    return registry_path


class _RegistryPatch:
    """Context manager: point playbook_registry.REGISTRY_PATH at a synthetic
    registry for the duration of the block, then restore it. resolve_playbook
    late-binds this global (see its docstring), so every consumer under test
    picks it up with zero code changes of their own."""

    def __init__(self, registry_path: Path):
        self._new = registry_path
        self._orig = None

    def __enter__(self):
        self._orig = playbook_registry.REGISTRY_PATH
        playbook_registry.REGISTRY_PATH = self._new
        return self

    def __exit__(self, *exc):
        playbook_registry.REGISTRY_PATH = self._orig


# ---------------------------------------------------------------------------
# Check 1 — profile() unit-tested for both shapes
# ---------------------------------------------------------------------------

def check_profile_both_shapes() -> list[str]:
    failures = []

    if not hasattr(playbook_registry, "profile"):
        return ["  scripts/playbook_registry.py has no profile() function."]

    PE = playbook_registry.PlaybookEntry

    precision_entry = PE(
        playbook_id="x",
        playbook_path=Path("p.json"),
        anchor_map_path=Path("a.json"),
        section_config_path=Path("s.json"),
        fixtures_dir=Path("fixtures"),
    )
    if playbook_registry.profile(precision_entry) != "precision":
        failures.append(
            "  profile() did not return 'precision' for an entry with both "
            "anchor_map_path and section_config_path set."
        )

    knowledge_entry = PE(
        playbook_id="x",
        playbook_path=Path("p.json"),
        anchor_map_path=None,
        section_config_path=None,
        fixtures_dir=Path("fixtures"),
    )
    if playbook_registry.profile(knowledge_entry) != "knowledge":
        failures.append(
            "  profile() did not return 'knowledge' for an entry with both "
            "anchor_map_path and section_config_path null."
        )

    # Asymmetric shapes must also classify as knowledge -- profile requires
    # BOTH fields, not either.
    only_anchor_map = PE(
        playbook_id="x", playbook_path=Path("p.json"),
        anchor_map_path=Path("a.json"), section_config_path=None,
        fixtures_dir=Path("fixtures"),
    )
    if playbook_registry.profile(only_anchor_map) != "knowledge":
        failures.append(
            "  profile() returned 'precision' for an entry with only "
            "anchor_map_path set (section_config_path null) -- both fields "
            "are required for 'precision'."
        )

    only_section_config = PE(
        playbook_id="x", playbook_path=Path("p.json"),
        anchor_map_path=None, section_config_path=Path("s.json"),
        fixtures_dir=Path("fixtures"),
    )
    if playbook_registry.profile(only_section_config) != "knowledge":
        failures.append(
            "  profile() returned 'precision' for an entry with only "
            "section_config_path set (anchor_map_path null) -- both fields "
            "are required for 'precision'."
        )

    return failures


# ---------------------------------------------------------------------------
# Check 2 — guard rail: resolve_playbook() on a knowledge-profile entry
# works, and NO modified gate hard-fails (raises) on a null anchor map.
# ---------------------------------------------------------------------------

def check_guard_rail_no_hard_fail_on_null_anchor_map() -> list[str]:
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_registry(root)  # both precision-fixture and knowledge-fixture

        with _RegistryPatch(root / "playbooks" / "registry.json"):
            # resolve_playbook on a knowledge entry must not raise, and its
            # anchor/section paths must resolve to None (already Optional).
            try:
                entry = playbook_registry.resolve_playbook("knowledge-fixture")
            except Exception as exc:  # noqa: BLE001
                return [f"  resolve_playbook('knowledge-fixture') raised: {exc!r}"]
            if entry.anchor_map_path is not None or entry.section_config_path is not None:
                failures.append(
                    "  resolve_playbook('knowledge-fixture') did not resolve "
                    "anchor_map_path/section_config_path to None for a null "
                    "registry entry."
                )
            if playbook_registry.profile(entry) != "knowledge":
                failures.append(
                    "  profile(resolve_playbook('knowledge-fixture')) != 'knowledge'."
                )

            for label, fn in (
                ("tests/lint-acceptable-variations.py", _LINT_ACCEPTABLE_VARIATIONS.main),
                ("scripts/eval_harness.py", eval_harness.main),
            ):
                try:
                    code, output = _run(fn)
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        f"  {label} main() HARD-FAILED (raised) with a knowledge "
                        f"entry present in the registry: {exc!r}"
                    )
                    continue
                if "SKIP (knowledge profile)" not in output:
                    failures.append(
                        f"  {label} main() did not print an explicit "
                        f"'SKIP (knowledge profile)' line for 'knowledge-fixture'. "
                        f"Output:\n{output}"
                    )
                if code != 0:
                    failures.append(
                        f"  {label} main() exited {code} (expected 0 -- the "
                        f"precision-fixture entry in this registry is fully "
                        f"valid and the knowledge entry must be SKIPped, not "
                        f"failed). Output:\n{output}"
                    )

    return failures


# ---------------------------------------------------------------------------
# Check 3 — precision entries lose no enforcement: the knowledge SKIP must be
# SELECTIVE. A gate that skipped unconditionally would satisfy check 2 while
# silently dropping every precision playbook's enforcement.
# ---------------------------------------------------------------------------
#
# Issue #631 note: this check used to break the precision entry's own
# artifacts (delete its anchor-map file; strip its anchor_migrations) and
# assert the two anchor gates went RED. Those gates were deleted with the
# anchor-map subsystem, and neither surviving gate reads an anchor map at
# all -- so breaking one is no longer a RED path for anything, and asserting
# it went red would assert over a state that cannot occur. What IS still
# assertable, and is the property check 2 cannot see on its own, is that the
# skip names the knowledge entry and ONLY the knowledge entry.

def check_precision_entry_still_enforced() -> list[str]:
    failures = []

    gates = (
        ("tests/lint-acceptable-variations.py", _LINT_ACCEPTABLE_VARIATIONS.main),
        ("scripts/eval_harness.py", eval_harness.main),
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_registry(root)  # both entries, both fully valid
        with _RegistryPatch(root / "playbooks" / "registry.json"):
            for label, fn in gates:
                code, output = _run(fn)
                skip_lines = [
                    line for line in output.splitlines()
                    if "SKIP (knowledge profile)" in line
                ]
                if not any("knowledge-fixture" in line for line in skip_lines):
                    failures.append(
                        f"  {label} did not SKIP the knowledge entry at all "
                        f"(no 'SKIP (knowledge profile)' line naming "
                        f"'knowledge-fixture'). Output:\n{output}"
                    )
                for line in skip_lines:
                    if "precision-fixture" in line:
                        failures.append(
                            f"  {label} SKIPped the PRECISION entry as though it "
                            f"were a knowledge one -- the profile check is not "
                            f"selective, so every precision playbook's "
                            f"enforcement is silently gone: {line!r}"
                        )
                if code != 0:
                    failures.append(
                        f"  {label} exited {code} on a registry where both "
                        f"entries are valid. Output:\n{output}"
                    )

            # eval_harness reports per-playbook: the precision entry must be
            # SCORED (enforced), and the summary must count exactly one of
            # each -- a skip that swallowed the precision entry would show 0
            # scored while still exiting 0.
            code, output = _run(eval_harness.main)
            if "[precision-fixture]" not in output:
                failures.append(
                    "  scripts/eval_harness.py never names the precision entry in "
                    f"its per-playbook output -- it was not scored.\n{output}"
                )
            if "1 playbook(s) scored, 1 skipped" not in output:
                failures.append(
                    "  scripts/eval_harness.py's summary does not report exactly "
                    "1 playbook scored and 1 skipped for a registry holding one "
                    f"precision and one knowledge entry.\n{output}"
                )

    return failures


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        ("1", "profile() classifies both shapes correctly", check_profile_both_shapes),
        ("2", "guard rail: no gate hard-fails on a null anchor map", check_guard_rail_no_hard_fail_on_null_anchor_map),
        ("3", "precision entries lose no enforcement", check_precision_entry_still_enforced),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All registry-profile checks passed.")
        return 0
    else:
        print("One or more registry-profile checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
