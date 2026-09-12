#!/usr/bin/env python3
"""
Red gate for issue #69 (G10 / B10 in
docs/reports/2026-09-05-stability-maintainability-qol-diagnostic.md).

Two things were left open after the docs scaffolding landed:

  1. The historical review packets still sat at the top of `docs/`, beside
     the living design docs they are explicitly not part of.
  2. `scripts/docs-lint.py` hand-listed which files count as "living", so the
     `docs/INDEX.md` projection and the lint gate were two lists that could
     drift apart. The point of a projection is that there is one.

This test pins both, plus the mechanism that makes (2) possible: `kind:` is
part of the INDEX line grammar in `tools/docs_sync.py`, so the tag survives a
`docs_sync.py project --write` re-projection instead of being dropped.

Check 4 is the one that proves the derivation is real rather than a
coincidence that happens to match the old hard-coded list: it builds a
throw-away INDEX with a DIFFERENT living set — emitted by `docs_sync.emit_index`,
the same function `project --write` writes the real file with, so the shape
under test is the shape production produces — and asserts docs-lint follows it.
It also seeds the failure variants (no tag at all, a tag on a path that does
not exist) and asserts docs-lint refuses rather than scanning an empty set,
because an empty living set would make Checks A, B and F pass vacuously.

Run with: python3 tests/test_docs_lint_living_from_index_69.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import importlib.util
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_LINT = REPO_ROOT / "scripts" / "docs-lint.py"
DOCS_SYNC = REPO_ROOT / "tools" / "docs_sync.py"
INDEX = REPO_ROOT / "docs" / "INDEX.md"
DOCS_DIR = REPO_ROOT / "docs"

# The living design docs as of this change. Nine files: the eight the gate has
# always scanned plus ARCHITECTURE.md at the repo root.
EXPECTED_LIVING = {
    "ARCHITECTURE.md",
    "docs/data-handling.md",
    "docs/evaluation.md",
    "docs/output-contract.md",
    "docs/playbook-governance.md",
    "docs/design-notes.md",
    "docs/threat-model.md",
    "docs/audit-queries.md",
    "docs/phase-0-issues.md",
}

# Historical packets: these must not sit at the top level of docs/ any more.
HISTORICAL_GLOBS = ("architecture-review-*", "architecture-issue-spotting-*", "handoff_*")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__] and raises if the module is not there yet.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def rel(p: Path) -> str:
    return p.resolve().relative_to(REPO_ROOT).as_posix()


# ── Check 1 — no hard-coded literal living list in docs-lint.py ──────────────

def check_no_hardcoded_living_list():
    failures = []
    text = DOCS_LINT.read_text(encoding="utf-8")
    if re.search(r"^LIVING_DOCS\s*=\s*\[", text, re.MULTILINE):
        failures.append(
            "  scripts/docs-lint.py still assigns LIVING_DOCS from a literal list; "
            "it must be derived from docs/INDEX.md."
        )
    # The derivation must actually read the index, not merely avoid the literal.
    if "INDEX" not in text or "kind: living" not in text:
        failures.append(
            "  scripts/docs-lint.py does not reference docs/INDEX.md and the "
            "`kind: living` tag it derives the set from."
        )
    return failures


# ── Check 2 — the derived set is exactly today's nine living docs ────────────

def check_derived_set_matches():
    failures = []
    docs_lint = load_module("docs_lint_69", DOCS_LINT)
    derived = {rel(p) for p in docs_lint.LIVING_DOCS}
    for missing in sorted(EXPECTED_LIVING - derived):
        failures.append(f"  living doc not derived from docs/INDEX.md: {missing}")
    for extra in sorted(derived - EXPECTED_LIVING):
        failures.append(f"  unexpected doc derived as living: {extra}")
    # Every derived path must be tagged in the index itself, and exist.
    index_text = INDEX.read_text(encoding="utf-8")
    tagged = {
        m.group(1)
        for m in re.finditer(r"^- `([^`]+)` — .* kind: living(?: |$)", index_text, re.MULTILINE)
    }
    for missing in sorted(EXPECTED_LIVING - tagged):
        failures.append(f"  docs/INDEX.md line for {missing} is not tagged `kind: living`")
    for p in docs_lint.LIVING_DOCS:
        if not p.is_file():
            failures.append(f"  derived living doc does not exist on disk: {rel(p)}")
    return failures


# ── Check 3 — historical packets have left the top level of docs/ ────────────

def check_historical_packets_moved():
    failures = []
    reports = DOCS_DIR / "reports"
    if not reports.is_dir():
        return ["  docs/reports/ does not exist"]
    for pattern in HISTORICAL_GLOBS:
        stray = sorted(p.name for p in DOCS_DIR.glob(pattern) if p.is_file())
        if stray:
            failures.append(
                f"  still at the top level of docs/ (belongs in docs/reports/): {', '.join(stray)}"
            )
        if not sorted(p for p in reports.glob(pattern) if p.is_file()):
            failures.append(f"  no file matching {pattern} found under docs/reports/")
    # A moved file must not be indexed under its old path.
    index_text = INDEX.read_text(encoding="utf-8")
    for pattern in HISTORICAL_GLOBS:
        prefix = pattern.rstrip("*")
        if re.search(r"^- `docs/" + re.escape(prefix), index_text, re.MULTILINE):
            failures.append(f"  docs/INDEX.md still indexes a `docs/{prefix}…` path")
    return failures


# ── Check 4 — the derivation follows the index, and refuses an empty set ─────

def _emit_index(docs_sync, entries):
    """An INDEX.md built by docs_sync's own emitter — the same code path
    `docs_sync.py project --write` uses to write the real file."""
    config = docs_sync.DocsConfig()
    return docs_sync.emit_index(config, docs_sync.DEFAULT_PREAMBLE, {e.path: e for e in entries})


def check_derivation_follows_index():
    failures = []
    docs_lint = load_module("docs_lint_69_b", DOCS_LINT)
    docs_sync = load_module("docs_sync_69", DOCS_SYNC)

    entry = docs_sync.DocEntry

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # (a) A different living set: only two docs tagged, and one of the
        #     nine real ones deliberately left untagged. Both paths are real
        #     files (the derivation resolves against the repo root).
        variant = _emit_index(
            docs_sync,
            [
                entry(path="ARCHITECTURE.md", scope="Root design doc.", kind="living"),
                entry(path="docs/threat-model.md", scope="Security home.", kind="living",
                      anchors=["identity-and-authorization"]),
                entry(path="docs/evaluation.md", scope="Quality bar — deliberately NOT living here."),
            ],
        )
        p = tmp_path / "variant.md"
        p.write_text(variant, encoding="utf-8")
        got = {rel(x) for x in docs_lint.living_docs_from_index(p)}
        want = {"ARCHITECTURE.md", "docs/threat-model.md"}
        if got != want:
            failures.append(
                f"  derivation did not follow the index: expected {sorted(want)}, got {sorted(got)}"
            )

        # (b) The tag round-trips through the grammar: re-parsing the emitted
        #     line must give the kind back, or `project --write` would drop it.
        reparsed = docs_sync.parse_index(p)
        if reparsed.parse_errors:
            failures.append(f"  docs_sync cannot parse its own emitted index: {reparsed.parse_errors}")
        for path_, kind_ in (("ARCHITECTURE.md", "living"), ("docs/threat-model.md", "living"),
                             ("docs/evaluation.md", None)):
            actual = reparsed.entries[path_].kind if path_ in reparsed.entries else "<absent>"
            if actual != kind_:
                failures.append(f"  kind for {path_} did not round-trip: expected {kind_!r}, got {actual!r}")

        # (c) No tag anywhere must be refused, not silently scanned as empty.
        none_tagged = _emit_index(
            docs_sync, [entry(path="ARCHITECTURE.md", scope="Root design doc.")]
        )
        p2 = tmp_path / "none.md"
        p2.write_text(none_tagged, encoding="utf-8")
        try:
            docs_lint.living_docs_from_index(p2)
            failures.append("  an index with no `kind: living` entry was accepted (empty scan set)")
        except SystemExit:
            pass

        # (d) A tag on a path that does not exist must be refused, not skipped.
        ghost = _emit_index(
            docs_sync,
            [entry(path="docs/does-not-exist.md", scope="Ghost.", kind="living")],
        )
        p3 = tmp_path / "ghost.md"
        p3.write_text(ghost, encoding="utf-8")
        try:
            docs_lint.living_docs_from_index(p3)
            failures.append("  a `kind: living` tag on a missing file was accepted")
        except SystemExit:
            pass

        # (e) A missing index must be refused too.
        try:
            docs_lint.living_docs_from_index(tmp_path / "absent.md")
            failures.append("  a missing INDEX.md was accepted")
        except SystemExit:
            pass

    return failures


def main() -> int:
    checks = [
        ("1", "scripts/docs-lint.py has no hard-coded LIVING_DOCS literal",
         check_no_hardcoded_living_list),
        ("2", "The derived living set is exactly the nine `kind: living` docs",
         check_derived_set_matches),
        ("3", "Historical packets live under docs/reports/, not the top of docs/",
         check_historical_packets_moved),
        ("4", "The derivation follows docs/INDEX.md and refuses an empty set",
         check_derivation_follows_index),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All docs-lint living-from-index checks passed.")
        return 0
    print("One or more docs-lint living-from-index checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
