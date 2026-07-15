#!/usr/bin/env python3
"""
Red gate for issue #45: playbook_id multi-playbook contract.

Asserts the following invariants across the living docs:

A. Vector metadata model (ARCHITECTURE.md § Metadata model) includes `playbook_id`.
   The old model only had `playbook_topic_id`; `playbook_id` is required so cross-
   playbook topic-id collisions (e.g. "confidentiality" in both EIAA and NDA) cannot
   contaminate retrieval results.

B. Reviews row (ARCHITECTURE.md DynamoDB table) includes `playbook_id`.
   Every review must record which playbook it ran against so rollback/quarantine
   queries and future multi-playbook routing work correctly.

C. POST /api/reviews route description references a playbook selector.
   The review creation contract must accept (and default) a `playbook_id`.

D. Data flow step 3 (ARCHITECTURE.md § Data flow) references playbook-scoped bundle
   resolution (not just "the" singular active bundle).

E. Canonical field dictionary (docs/data-handling.md) lists `playbook_id` in the
   `reviews` field table.

F. Evaluation namespacing (docs/evaluation.md) mentions playbook_id namespacing for
   eval suites (or spend ledger dimensions).

G. Phase-0 issue #12 (docs/phase-0-issues.md § Bedrock Knowledge Base) ACs reference
   `playbook_id` in the metadata model.

H-J (issue #209): playbook_id is a first-class RUNTIME parameter of the engine
itself, not just the docs. A new contract type must be addable by authoring
data (a playbook.json + anchor-map.json + section-config.json + fixtures dir +
a registry entry) with NO code edit to scripts/canonicalize.py,
scripts/diff_standard_form.py, scripts/build_anchor_map.py, or
scripts/eval_harness.py.

H. scripts/canonicalize.py, scripts/diff_standard_form.py,
   scripts/build_anchor_map.py, and scripts/eval_harness.py resolve their
   playbook/anchor-map/standard-form/fixtures paths from a playbook REGISTRY
   keyed on playbook_id (scripts/playbook_registry.py), not the literal
   playbooks/eiaa-v1.0.0.json. Registering a SECOND, synthetic playbook_id in
   the registry must resolve to that playbook's own artifacts, with no code
   edit -- verified against a temporary registry (monkeypatching
   scripts/playbook_registry.py's REGISTRY_PATH), not the real one.

I. build_anchor_map.py's SECTION_CONFIG / COVERAGE_EXEMPT_RATIONALES are read
   from a per-playbook section-config DATA FILE (via
   build_anchor_map.load_section_config(playbook_id)), not Python literals
   shared across every playbook. A second playbook_id's section config must
   resolve to genuinely different content than the first.

J. diff_standard_form._load_active_anchor_map() selects the anchor map by
   playbook_id via the registry, not by picking the lexically-last
   *.anchor-map.json file in the shared standard-forms/ directory. Two
   anchor-map files coexisting in the same directory, where the WRONG one
   (for the requested playbook_id) sorts lexically last, must still resolve
   to the CORRECT one.

Exit code: 0 = all pass, 1 = one or more failed.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DATA_HANDLING = REPO_ROOT / "docs" / "data-handling.md"
EVALUATION = REPO_ROOT / "docs" / "evaluation.md"
PHASE0 = REPO_ROOT / "docs" / "phase-0-issues.md"

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── Check A — playbook_id in vector metadata model ────────────────────────────

def check_a() -> list[str]:
    """ARCHITECTURE.md metadata-model bullet list must include playbook_id."""
    text = read(ARCHITECTURE)

    # Find the metadata-model section
    # Looking for the bullet list under "Metadata model (fits the S3 Vectors limits)"
    # The list contains items like `clause_id`, `corpus_snapshot_version`, etc.
    meta_section = re.search(
        r"#### Metadata model.*?(?=\n#+|\Z)",
        text,
        re.DOTALL,
    )
    if not meta_section:
        return ["  Could not locate '#### Metadata model' section in ARCHITECTURE.md"]

    section_text = meta_section.group(0)

    # playbook_id must appear as a metadata field bullet
    if "`playbook_id`" not in section_text:
        return [
            "  ARCHITECTURE.md Metadata model section is missing `playbook_id` field.\n"
            "  The model only has `playbook_topic_id`; `playbook_id` is required to\n"
            "  prevent cross-playbook topic-id collisions in retrieval filters."
        ]
    return []


# ── Check B — playbook_id in reviews row ──────────────────────────────────────

def check_b() -> list[str]:
    """ARCHITECTURE.md DynamoDB reviews table row must include playbook_id."""
    text = read(ARCHITECTURE)

    # Find the `reviews` table row in the DynamoDB tables section
    m = re.search(r"\| *`reviews` *\|.*", text)
    if not m:
        return ["  Could not find the `reviews` table row in ARCHITECTURE.md"]

    reviews_row = m.group(0)
    if "playbook_id" not in reviews_row:
        return [
            "  ARCHITECTURE.md `reviews` DynamoDB row does not include `playbook_id`.\n"
            "  Every review must record which playbook it ran against so rollback/\n"
            "  quarantine queries and multi-playbook routing work correctly."
        ]
    return []


# ── Check C — POST /api/reviews accepts a playbook selector ──────────────────

def check_c() -> list[str]:
    """POST /api/reviews route must reference a playbook selector / playbook_id."""
    text = read(ARCHITECTURE)

    # Find the POST /api/reviews row in the Routes table
    # The table format is: | POST   | `/api/reviews`  | allowlisted | Purpose |
    m = re.search(r"\|\s*POST\s*\|\s*`/api/reviews`\s*\|.*", text)
    if not m:
        return ["  Could not find 'POST | /api/reviews' row in ARCHITECTURE.md Routes table"]

    route_row = m.group(0)
    if "playbook" not in route_row.lower():
        return [
            "  POST /api/reviews route description does not mention a playbook selector.\n"
            "  The review creation contract must accept (and default) a `playbook_id`\n"
            "  so multi-playbook routing can work (issue #45 AC)."
        ]
    return []


# ── Check D — Data flow step 3 scopes by playbook ────────────────────────────

def check_d() -> list[str]:
    """Data flow step 3 must reference playbook-scoped bundle resolution."""
    text = read(ARCHITECTURE)

    # Find step 3 in the data flow section
    # Step 3 currently says "Resolve the active release bundle ... singular"
    # We need it to scope by playbook_id
    m = re.search(
        r"(?m)^\s*3\.\s+Resolve.*?(?=\n\s*4\.|\Z)",
        text,
        re.DOTALL,
    )
    if not m:
        return ["  Could not locate data flow step 3 in ARCHITECTURE.md"]

    step_text = m.group(0)
    if "playbook_id" not in step_text and "playbook" not in step_text.lower():
        return [
            "  Data flow step 3 does not reference playbook-scoped bundle resolution.\n"
            "  It currently resolves 'the' singular active bundle with no playbook scope;\n"
            "  it must include the playbook_id from the request to scope the bundle."
        ]
    return []


# ── Check E — playbook_id in canonical field dictionary ──────────────────────

def check_e() -> list[str]:
    """docs/data-handling.md canonical reviews field table must list playbook_id."""
    text = read(DATA_HANDLING)

    # The canonical field table lists rows like:
    # | `review_id`, `created_at`, ... | No | Internal | Indefinite (audit) |
    # We need playbook_id somewhere in that table.
    if "playbook_id" not in text:
        return [
            "  docs/data-handling.md canonical reviews field dictionary does not\n"
            "  include `playbook_id`. It must be added so the authoritative field\n"
            "  list covers the new multi-playbook routing field."
        ]
    return []


# ── Check F — evaluation namespacing by playbook_id ──────────────────────────

def check_f() -> list[str]:
    """docs/evaluation.md must mention playbook_id namespacing of eval suites."""
    text = read(EVALUATION)

    # The issue requires: "Eval namespacing documented" — gold sets and detector
    # gates are namespaced by playbook id.
    if "playbook_id" not in text and "playbook id" not in text.lower():
        return [
            "  docs/evaluation.md does not document playbook_id namespacing for\n"
            "  evaluation suites / gold sets. Issue #45 requires eval suites and\n"
            "  spend-ledger dimensions to be namespaced by playbook id."
        ]
    return []


# ── Check G — phase-0 issue #12 ACs reference playbook_id ────────────────────

def check_g() -> list[str]:
    """docs/phase-0-issues.md issue #12 ACs must reference playbook_id in metadata."""
    text = read(PHASE0)

    # Issue #12 is "## 12. Bedrock Knowledge Base + S3 Vectors (retrieval store)"
    # The body lives between the '---begin---' and '---end---' markers that follow
    # the issue-12 heading. Find the heading first, then grab the begin/end block.
    issue12_start = text.find("## 12. Bedrock Knowledge Base")
    if issue12_start < 0:
        return ["  Could not locate '## 12. Bedrock Knowledge Base' heading in docs/phase-0-issues.md"]

    # Find the begin/end block that belongs to issue 12
    begin_idx = text.find("---begin---", issue12_start)
    end_idx = text.find("---end---", begin_idx) if begin_idx >= 0 else -1

    if begin_idx < 0 or end_idx < 0:
        return [
            "  Could not locate '---begin---/---end---' block for issue #12 "
            "in docs/phase-0-issues.md"
        ]

    section_text = text[begin_idx:end_idx + len("---end---")]
    if "playbook_id" not in section_text:
        return [
            "  docs/phase-0-issues.md issue #12 ACs do not reference `playbook_id`\n"
            "  in the metadata model. The issue must be updated to include `playbook_id`\n"
            "  so infra is built with the correct metadata from the start (issue #45 AC)."
        ]
    return []


# ── Runtime registry fixture (issue #209, checks H-J) ────────────────────────
#
# A self-contained SYNTHETIC two-playbook "mini repo" under a tempdir, laid
# out exactly like the real repo (playbooks/, standard-forms/,
# tests/gold-fixtures/) so playbook_registry.resolve_playbook()'s "root is
# registry_path.parent.parent" convention resolves it identically to
# production. Includes a decoy anchor-map file that sorts lexically AFTER
# the correct "eiaa" map, to prove selection is playbook_id-driven, not
# filename-order-driven (the exact bug being fixed).

_SYNTHETIC_SECTIONS = {
    "eiaa": [["sec-1", "Sec One", False, None]],
    "synthetic-widget": [
        ["sec-w1", "Widget Warranty", False, None],
        ["sec-w2", "Widget Returns", False, None],
    ],
}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _build_synthetic_registry(root: Path) -> Path:
    """
    Build a synthetic registry.json plus every artifact it points at, rooted
    at `root`. Returns the registry.json path. Registers TWO playbook_ids
    ("eiaa" and the synthetic "synthetic-widget") to exercise coexistence.
    """
    registry = {"playbooks": {}}

    for playbook_id, sections in _SYNTHETIC_SECTIONS.items():
        playbook_path = f"playbooks/{playbook_id}-v1.0.0.json"
        section_config_path = f"playbooks/{playbook_id}-v1.0.0.sections.json"
        anchor_map_path = f"standard-forms/{playbook_id}-v1.0.0.anchor-map.json"
        fixtures_dir = f"tests/gold-fixtures/{playbook_id}"

        _write_json(root / playbook_path, {
            "playbook": {"id": playbook_id, "version": "1.0.0", "topics": [], "hard_rejections": []}
        })
        _write_json(root / section_config_path, {
            "sections": sections,
            "coverage_exempt_rationales": {},
            "absent_from_form_anchors": [],
            "structural_anchors": [],
        })
        anchors = {
            anchor: {
                "heading": heading,
                "heading_hash": "sha256:0" * 8,
                "sub_clause_split": False,
                "absent_from_form": False,
                "structural": False,
            }
            for anchor, heading, _sub_split, _parent in sections
        }
        _write_json(root / anchor_map_path, {
            "schema_version": "1",
            "playbook_version": "1.0.0",
            "coverage_exempt_anchors": [],
            "coverage_exempt_rationales": {},
            "anchors": anchors,
        })
        (root / fixtures_dir).mkdir(parents=True, exist_ok=True)

        registry["playbooks"][playbook_id] = {
            "playbook_id": playbook_id,
            "playbook_path": playbook_path,
            "anchor_map_path": anchor_map_path,
            "section_config_path": section_config_path,
            "fixtures_dir": fixtures_dir,
            "standard_form_docx": None,
        }

    # Decoy anchor-map: sorts lexically AFTER "eiaa-v1.0.0.anchor-map.json"
    # (old code: `sorted(STANDARD_FORMS_DIR.glob("*.anchor-map.json"))[-1]`
    # would pick THIS file for every playbook_id, since "zzz..." > "eiaa...").
    # It carries an anchor no real playbook registers, so a resolver that
    # picked it by filename order (instead of playbook_id) is caught red-handed.
    _write_json(root / "standard-forms" / "zzz-decoy.anchor-map.json", {
        "schema_version": "1",
        "playbook_version": "999.0.0",
        "coverage_exempt_anchors": [],
        "coverage_exempt_rationales": {},
        "anchors": {"WRONG-ANCHOR-FROM-DECOY": {"heading": "Decoy", "heading_hash": "x"}},
    })

    registry_path = root / "playbooks" / "registry.json"
    _write_json(registry_path, registry)
    return registry_path


# ── Check H — registry-driven path resolution, not the literal eiaa path ─────

def check_h() -> list[str]:
    """canonicalize.py / diff_standard_form.py / build_anchor_map.py /
    eval_harness.py resolve paths via a playbook registry keyed on
    playbook_id, not the literal playbooks/eiaa-v1.0.0.json -- verified
    against a SYNTHETIC registry (not the real one) so this proves the
    resolution mechanism itself, not just that the real eiaa entry exists."""
    failures: list[str] = []

    try:
        import playbook_registry
        import build_anchor_map
        import canonicalize
        import diff_standard_form
        import eval_harness
    except ImportError as exc:
        return [f"  Could not import scripts/playbook_registry.py or its consumers: {exc}"]

    if not hasattr(playbook_registry, "resolve_playbook"):
        return ["  scripts/playbook_registry.py has no resolve_playbook() -- no registry exists yet."]

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _build_synthetic_registry(root)

        orig_registry_path = playbook_registry.REGISTRY_PATH
        try:
            playbook_registry.REGISTRY_PATH = root / "playbooks" / "registry.json"

            # canonicalize.py must resolve "synthetic-widget" to the SYNTHETIC
            # playbook file, never falling back to the real eiaa literal.
            if hasattr(canonicalize, "resolve_playbook_path"):
                resolved = canonicalize.resolve_playbook_path("synthetic-widget")
                if "eiaa" in str(resolved).lower():
                    failures.append(
                        "  [H] canonicalize.py resolved playbook_id='synthetic-widget' to a "
                        f"path containing 'eiaa' ({resolved}) -- still hard-coded to the "
                        "literal EIAA path instead of resolving via the registry."
                    )
                elif not resolved.exists() or "synthetic-widget" not in resolved.read_text():
                    failures.append(
                        f"  [H] canonicalize.py resolve_playbook_path('synthetic-widget') "
                        f"did not resolve to the synthetic playbook file ({resolved})."
                    )
            else:
                failures.append(
                    "  [H] scripts/canonicalize.py has no playbook_id-driven path resolver "
                    "(e.g. resolve_playbook_path()) -- PLAYBOOK_PATH is still a fixed literal."
                )

            # diff_standard_form.py must resolve the SYNTHETIC playbook/anchor-map
            # for "synthetic-widget", not the eiaa ones.
            synthetic_playbook = diff_standard_form._load_playbook("synthetic-widget")
            if synthetic_playbook.get("playbook", {}).get("id") != "synthetic-widget":
                failures.append(
                    "  [H] diff_standard_form._load_playbook('synthetic-widget') did not "
                    f"resolve the synthetic playbook (got: {synthetic_playbook})."
                )

            # build_anchor_map.py must resolve the SYNTHETIC section-config file
            # for "synthetic-widget", not build_anchor_map.SECTION_CONFIG (eiaa).
            synthetic_cfg = build_anchor_map.load_section_config("synthetic-widget")
            synthetic_anchors = {row[0] for row in synthetic_cfg["sections"]}
            if synthetic_anchors != {"sec-w1", "sec-w2"}:
                failures.append(
                    "  [H] build_anchor_map.load_section_config('synthetic-widget') did not "
                    f"resolve the synthetic section config (got anchors: {synthetic_anchors})."
                )

            # eval_harness.py must resolve the SYNTHETIC fixtures_dir for
            # "synthetic-widget", namespaced separately from eiaa's.
            entry = playbook_registry.resolve_playbook("synthetic-widget")
            if entry.fixtures_dir == eval_harness.FIXTURES_PATH:
                failures.append(
                    "  [H] eval_harness fixtures resolution for 'synthetic-widget' is not "
                    "namespaced -- resolved to the same directory as the default eiaa fixtures."
                )
        finally:
            playbook_registry.REGISTRY_PATH = orig_registry_path

    return failures


# ── Check I — SECTION_CONFIG / COVERAGE_EXEMPT_RATIONALES are DATA ───────────

def check_i() -> list[str]:
    """build_anchor_map's section config must be read from a per-playbook data
    file (via load_section_config(playbook_id)), not a Python literal shared
    identically across every playbook_id."""
    failures: list[str] = []

    try:
        import build_anchor_map
    except ImportError as exc:
        return [f"  Could not import scripts/build_anchor_map.py: {exc}"]

    if not hasattr(build_anchor_map, "load_section_config"):
        return [
            "  [I] scripts/build_anchor_map.py has no load_section_config(playbook_id) -- "
            "SECTION_CONFIG / COVERAGE_EXEMPT_RATIONALES are still Python literals, not "
            "per-playbook data."
        ]

    import playbook_registry
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _build_synthetic_registry(root)

        orig_registry_path = playbook_registry.REGISTRY_PATH
        try:
            playbook_registry.REGISTRY_PATH = root / "playbooks" / "registry.json"

            eiaa_cfg = build_anchor_map.load_section_config("eiaa")
            synthetic_cfg = build_anchor_map.load_section_config("synthetic-widget")

            eiaa_anchors = {row[0] for row in eiaa_cfg["sections"]}
            synthetic_anchors = {row[0] for row in synthetic_cfg["sections"]}

            if eiaa_anchors == synthetic_anchors:
                failures.append(
                    "  [I] load_section_config() returned the SAME anchor set for 'eiaa' and "
                    "'synthetic-widget' -- section config is not actually per-playbook data."
                )
            if synthetic_anchors != {"sec-w1", "sec-w2"}:
                failures.append(
                    f"  [I] load_section_config('synthetic-widget') did not read its own "
                    f"section-config data file (got: {synthetic_anchors})."
                )
        finally:
            playbook_registry.REGISTRY_PATH = orig_registry_path

    return failures


# ── Check J — anchor-map selection by playbook_id, not lexically-last file ───

def check_j() -> list[str]:
    """diff_standard_form._load_active_anchor_map() must select the anchor
    map by playbook_id via the registry, never by picking the lexically-last
    *.anchor-map.json file in standard-forms/ -- a decoy file that sorts
    after the correct one must NOT be selected."""
    failures: list[str] = []

    try:
        import diff_standard_form
        import playbook_registry
    except ImportError as exc:
        return [f"  Could not import scripts/diff_standard_form.py: {exc}"]

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _build_synthetic_registry(root)  # includes the zzz-decoy.anchor-map.json trap

        orig_registry_path = playbook_registry.REGISTRY_PATH
        try:
            playbook_registry.REGISTRY_PATH = root / "playbooks" / "registry.json"

            resolved = diff_standard_form._load_active_anchor_map("eiaa")
            anchors = set(resolved.get("anchors", {}).keys())

            if "WRONG-ANCHOR-FROM-DECOY" in anchors:
                failures.append(
                    "  [J] _load_active_anchor_map('eiaa') selected the decoy anchor-map file "
                    "(lexically-last by filename) instead of the one registered for 'eiaa' -- "
                    "still resolving by sorted(*.anchor-map.json)[-1], not by playbook_id."
                )
            if anchors != {"sec-1"}:
                failures.append(
                    f"  [J] _load_active_anchor_map('eiaa') did not resolve the anchor map "
                    f"registered for 'eiaa' (got anchors: {anchors})."
                )

            resolved_synth = diff_standard_form._load_active_anchor_map("synthetic-widget")
            synth_anchors = set(resolved_synth.get("anchors", {}).keys())
            if synth_anchors != {"sec-w1", "sec-w2"}:
                failures.append(
                    "  [J] _load_active_anchor_map('synthetic-widget') did not resolve the "
                    f"anchor map registered for 'synthetic-widget' (got anchors: {synth_anchors})."
                )
        finally:
            playbook_registry.REGISTRY_PATH = orig_registry_path

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("A", "playbook_id in vector metadata model (ARCHITECTURE.md)", check_a),
        ("B", "playbook_id in reviews DynamoDB row (ARCHITECTURE.md)", check_b),
        ("C", "POST /api/reviews accepts playbook selector (ARCHITECTURE.md routes)", check_c),
        ("D", "Data flow step 3 scopes bundle resolution by playbook_id", check_d),
        ("E", "playbook_id in canonical field dictionary (data-handling.md)", check_e),
        ("F", "Eval namespacing by playbook_id documented (evaluation.md)", check_f),
        ("G", "Phase-0 issue #12 ACs reference playbook_id (phase-0-issues.md)", check_g),
        ("H", "Scripts resolve paths via a playbook registry, not the eiaa literal", check_h),
        ("I", "SECTION_CONFIG / COVERAGE_EXEMPT_RATIONALES are per-playbook data", check_i),
        ("J", "Anchor map selected by playbook_id, not lexically-last filename", check_j),
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
        print("All playbook_id contract checks passed.")
        return 0
    else:
        print("One or more playbook_id contract checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
