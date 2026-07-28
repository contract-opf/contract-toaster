#!/usr/bin/env python3
"""
CI gate (issue #349): public-cut de-brand lint for the CODE+DATA surface
GRIND SPEC items 1, 2, and 5 cover — the `exos_standard` -> `our_standard`
field rename, the playbook `$schema`/`$id` URL repoint, and the infra
functional-literal parameterization.

## Problem this guards against

Issue #341's overnight grind surfaced that de-branding reached beyond
#274's user-facing-strings pass into functional code and the playbook DATA
MODEL: `exos_standard` was a schema field key read by seven modules, all
four `playbooks/*.json` schema documents hard-coded `teamexos.com` in their
`$schema`/`$id` URLs, and `infra/lib/nested/auth-stack.ts` /
`observability-stack.ts` baked the real tenant domain into Cognito/SNS
functional defaults. Nothing enforced that a future edit couldn't
reintroduce any of these. This lint fails loudly on regressions.

## What counts as a violation

1. The literal identifier `exos_standard` anywhere under `playbooks/`,
   `scripts/`, `backend/src/`, and `tests/` (the data model, the 7 renamed
   consumer modules, and the tests that exercise them) — issue #349 GRIND
   SPEC item 1. `docs/planning/` is explicitly OUT of scope: it holds frozen
   historical planning artifacts that predate the `playbooks/` reorg
   (issue #184) and are not read by any runtime code.
2. The literal substring `teamexos.com` (case-insensitive) anywhere in
   `playbooks/*.json` — GRIND SPEC item 2 (schema/$id URLs must point at
   `contract-opf.github.io`, not the internal tenant domain).
3. The literal substring `teamexos.com` (case-insensitive) anywhere in
   `infra/lib/nested/auth-stack.ts` or `infra/lib/nested/observability-stack.ts`
   — GRIND SPEC item 3 (the Google IdP hosted-domain / ALLOWED_DOMAIN /
   LEGAL_ADMIN_GROUP / alarms-email literals must be context-driven, no
   internal teamexos default).

## Self-test

Before trusting the real scan, this proves the scanner actually catches
each violation class against disposable temp files, never real source.

Run: python3 tests/lint-issue-349-debrand.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories scanned for the exos_standard identifier. docs/planning/ is
# deliberately excluded (see module docstring, item 1) as are the usual
# non-source trees.
EXOS_STANDARD_SCAN_DIRS = ("playbooks", "scripts", "backend/src", "tests")
EXOS_STANDARD_SCAN_EXTENSIONS = {".py", ".json"}
_EXOS_STANDARD_RE = re.compile(r"exos_standard")

# This lint script's own docstring/self-test and the companion positive test
# (tests/test_our_standard_field_rename_349.py) legitimately name the OLD
# `exos_standard` identifier in prose/assertions to document and verify the
# rename itself -- exclude them from the scan rather than the scan flagging
# its own documentation of the thing it guards against.
_SELF_REFERENTIAL_EXCLUDED_FILES = {
    "tests/lint-issue-349-debrand.py",
    "tests/test_our_standard_field_rename_349.py",
}

# playbooks/*.json must never carry the internal tenant domain in a
# $schema/$id URL (or anywhere else).
PLAYBOOKS_DIR = REPO_ROOT / "playbooks"

# The two infra functional files GRIND SPEC item 3 parameterizes.
INFRA_FUNCTIONAL_FILES = (
    "infra/lib/nested/auth-stack.ts",
    "infra/lib/nested/observability-stack.ts",
)

_TEAMEXOS_RE = re.compile(r"teamexos\.com", re.IGNORECASE)

_EXCLUDED_DIR_NAMES = {"node_modules", "cdk.out", ".git"}


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _iter_scan_files(root: Path, extensions: set[str]):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if any(part in _EXCLUDED_DIR_NAMES or part.startswith(".claude") for part in path.parts):
            continue
        if path.is_file() and path.suffix in extensions:
            yield path


def scan_for_exos_standard(repo_root: Path) -> list[str]:
    hits: list[str] = []
    for rel_dir in EXOS_STANDARD_SCAN_DIRS:
        root = repo_root / rel_dir
        for path in _iter_scan_files(root, EXOS_STANDARD_SCAN_EXTENSIONS):
            if str(path.relative_to(repo_root)) in _SELF_REFERENTIAL_EXCLUDED_FILES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _EXOS_STANDARD_RE.search(line):
                    hits.append(f"{path.relative_to(repo_root)}:{lineno}: {line.strip()!r}")
    return hits


def scan_for_teamexos(path: Path, repo_root: Path) -> list[str]:
    if not path.is_file():
        return [f"{path.relative_to(repo_root)}: MISSING"]
    hits: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _TEAMEXOS_RE.search(line):
            hits.append(f"{path.relative_to(repo_root)}:{lineno}: {line.strip()!r}")
    return hits


# ---------------------------------------------------------------------------
# Self-test: prove the scanners actually catch each violation class against
# disposable temp files, never real source.
# ---------------------------------------------------------------------------


def _self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        dirty_py = tmp_path / "dirty.py"
        dirty_py.write_text('topic.get("exos_standard")\n', encoding="utf-8")
        hits = []
        for lineno, line in enumerate(dirty_py.read_text().splitlines(), start=1):
            if _EXOS_STANDARD_RE.search(line):
                hits.append(line)
        if not hits:
            raise AssertionError("self-test failed: did not flag 'exos_standard'")

        clean_py = tmp_path / "clean.py"
        clean_py.write_text('topic.get("our_standard")\n', encoding="utf-8")
        clean_hits = [
            line for line in clean_py.read_text().splitlines() if _EXOS_STANDARD_RE.search(line)
        ]
        if clean_hits:
            raise AssertionError("self-test failed: false-positived on 'our_standard'")

        dirty_json = tmp_path / "dirty.json"
        dirty_json.write_text('{"$schema": "https://teamexos.com/playbooks/schema/v1.json"}\n', encoding="utf-8")
        teamexos_hits = scan_for_teamexos(dirty_json, tmp_path)
        if not teamexos_hits:
            raise AssertionError("self-test failed: did not flag 'teamexos.com'")

        clean_json = tmp_path / "clean.json"
        clean_json.write_text(
            '{"$schema": "https://contract-opf.github.io/playbooks/schema/v1.json"}\n', encoding="utf-8"
        )
        clean_teamexos_hits = scan_for_teamexos(clean_json, tmp_path)
        if clean_teamexos_hits:
            raise AssertionError("self-test failed: false-positived on 'contract-opf.github.io'")


# ---------------------------------------------------------------------------
# The real gate.
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        _self_test()
    except AssertionError as exc:
        print(f"FAIL (lint self-test): {exc}", file=sys.stderr)
        return 1
    print("Self-test OK: scanner catches exos_standard / teamexos.com violations.")

    failures: list[str] = []

    print(
        f"\nCheck 1: zero 'exos_standard' under {', '.join(EXOS_STANDARD_SCAN_DIRS)} "
        "(docs/planning/ excluded — archival, unread by any runtime code) …"
    )
    exos_standard_hits = scan_for_exos_standard(REPO_ROOT)
    failures += _assert(
        not exos_standard_hits,
        "no 'exos_standard' identifier remains in scope (issue #349 GRIND SPEC item 1)",
        "\n         ".join(exos_standard_hits) if exos_standard_hits else "",
    )

    print("\nCheck 2: zero 'teamexos.com' in playbooks/*.json …")
    playbook_hits: list[str] = []
    for pb_path in sorted(PLAYBOOKS_DIR.glob("*.json")):
        playbook_hits += scan_for_teamexos(pb_path, REPO_ROOT)
    failures += _assert(
        not playbook_hits,
        "no 'teamexos.com' literal remains in any playbooks/*.json ($schema/$id repointed to "
        "contract-opf.github.io per issue #349 GRIND SPEC item 2)",
        "\n         ".join(playbook_hits) if playbook_hits else "",
    )

    print("\nCheck 3: zero 'teamexos.com' in the infra auth/observability functional files …")
    infra_hits: list[str] = []
    for rel in INFRA_FUNCTIONAL_FILES:
        infra_hits += scan_for_teamexos(REPO_ROOT / rel, REPO_ROOT)
    failures += _assert(
        not infra_hits,
        "no 'teamexos.com' literal remains in auth-stack.ts / observability-stack.ts "
        "(hosted domain / ALLOWED_DOMAIN / LEGAL_ADMIN_GROUP / alarms email are context-driven "
        "per issue #349 GRIND SPEC item 3)",
        "\n         ".join(infra_hits) if infra_hits else "",
    )

    if failures:
        print(f"\nFAIL: {len(failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: no issue #349 public-cut de-brand violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
