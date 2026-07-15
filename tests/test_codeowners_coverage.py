#!/usr/bin/env python3
"""
CI structural gate: CODEOWNERS must gate all legal-behavior files/paths under
@exos-legal/gc (possibly co-reviewed with @exos-legal/engineering).

Issue #10: the following paths were NOT covered by a GC rule and would fall
through to the default engineering-only rule:

  docs/playbook-governance.md   — defines when a playbook may activate
  docs/output-contract.md       — external framing and citation rules
  docs/audit-queries.md         — audit semantics
  tests/gold-fixtures/**        — gold fixtures (expected_decision / expected_issues)

An engineering-only approved PR could weaken the GC's own gates or change the
expected legal answers the harness enforces.

This test asserts that every required path-set is matched by at least one
CODEOWNERS rule that includes @exos-legal/gc as an owner.

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEOWNERS_PATH = REPO_ROOT / ".github" / "CODEOWNERS"

GC_TEAM = "@exos-legal/gc"

# Each entry is (human_label, regex_that_must_match_some_codeowners_pattern).
# The regex matches against the path-pattern field (first token) of each
# CODEOWNERS line that also lists GC_TEAM.
REQUIRED_GC_PATHS = [
    (
        "docs/playbook-governance.md",
        r"^/?docs/playbook-governance\.md$",
    ),
    (
        "docs/output-contract.md",
        r"^/?docs/output-contract\.md$",
    ),
    (
        "docs/audit-queries.md",
        r"^/?docs/audit-queries\.md$",
    ),
    (
        "tests/gold-fixtures/** (gold fixtures)",
        # Matches /tests/gold-fixtures/, /tests/gold-fixtures/**, etc. — any
        # pattern that unambiguously covers the gold-fixtures tree.
        r"^/?tests/gold-fixtures(/(\*\*)?)?$",
    ),
]


def parse_gc_patterns(codeowners_text: str) -> list[str]:
    """
    Return the list of path-patterns from CODEOWNERS lines that include GC_TEAM.
    Skips blank lines and comment lines.
    """
    gc_patterns = []
    for raw_line in codeowners_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owners = parts[1:]
        if GC_TEAM in owners:
            gc_patterns.append(pattern)
    return gc_patterns


def main() -> int:
    if not CODEOWNERS_PATH.exists():
        print(f"FAIL: {CODEOWNERS_PATH} does not exist.")
        return 1

    codeowners_text = CODEOWNERS_PATH.read_text()
    gc_patterns = parse_gc_patterns(codeowners_text)

    failures = []

    for label, required_regex in REQUIRED_GC_PATHS:
        rx = re.compile(required_regex)
        # Also accept a blanket /docs/ rule that covers all docs files
        # and a blanket rule that explicitly lists the path.
        # We also accept a wildcard blanket docs rule like /docs/ or docs/**
        docs_blanket = any(
            re.match(r"^/?docs(/\*\*?)?$", p) for p in gc_patterns
        )

        matched = any(rx.match(p) for p in gc_patterns)

        # For docs/* paths we also accept a /docs/ blanket rule
        if label.startswith("docs/") and not matched:
            matched = docs_blanket

        if not matched:
            failures.append(
                f"  MISSING: '{label}' — no CODEOWNERS rule with {GC_TEAM} "
                f"covers this path.\n"
                f"    GC-gated patterns found: {gc_patterns}"
            )

    if failures:
        print(
            "FAIL: the following legal-behavior paths are NOT gated by "
            f"{GC_TEAM} in CODEOWNERS.\n"
        )
        for msg in failures:
            print(msg)
        print(
            f"\n{len(failures)} of {len(REQUIRED_GC_PATHS)} required path-sets "
            "are unprotected.\n"
            "An engineering-only PR could weaken GC gates or change expected "
            "legal answers.\n"
            "Fix: add the missing paths to the @exos-legal/gc block in "
            ".github/CODEOWNERS."
        )
        return 1
    else:
        print(
            f"PASS: all {len(REQUIRED_GC_PATHS)} required legal-behavior "
            f"path-sets are gated by {GC_TEAM} in CODEOWNERS."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
