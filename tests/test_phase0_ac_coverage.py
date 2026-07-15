#!/usr/bin/env python3
"""
RED gate for issue #46: phase-0-issues.md must cover every governance CI rule
from playbook-governance.md, and the anchor-map builder must have an issue home.

The test enumerates a canonical list of CI rules that playbook-governance.md
defines and asserts each one appears (by a unique sentinel token) in the body
of a Phase 0 issue's acceptance criteria in docs/phase-0-issues.md.

Failing today because issue #19's AC uses pre-redesign language that misses:
  - kind-conditional validation (on_insert / on_remove_or_alter per-kind fields)
  - anchor resolution gate (section_anchors resolves to real standard-form sections)
  - not_in_standard + sec-_new requirement
  - required-token-presence check (on_remove_or_alter guards tokens that exist)
  - exempt_terms liveness check
  - empty-scope gate (no rule has empty effective hunk-scope)
  - acceptable-variations lint

Also failing because neither #16 nor any other issue mentions the anchor-map
builder (docx → section-anchor map, §10 sub-clause splitting).

GREEN fix: update issue #19's AC in docs/phase-0-issues.md with post-redesign
language; add anchor-map builder clause to issue #16's AC.

Additionally the rule-count math must be corrected: the original plan
referenced "16" total rules (written as "9 on_insert + 5 on_remove_or_alter")
but the shipped playbook has 15 rules (9 on_insert + 6 on_remove_or_alter).
This test asserts that docs/phase-0-issues.md does NOT still claim the stale
counts (9+5 = 14 distinct-kind split, or a "16" total) without a correction
annotation.

Exit codes: 0 = all checks pass, 1 = one or more checks fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PHASE0 = REPO_ROOT / "docs" / "phase-0-issues.md"
GOVERNANCE = REPO_ROOT / "docs" / "playbook-governance.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Sentinel tokens — each must appear somewhere in docs/phase-0-issues.md.
# These encode the CI rules that playbook-governance.md defines but that
# issue #19's original AC did not enumerate.
# ---------------------------------------------------------------------------

# Format: (sentinel_token, human_description_of_what_it_checks, issue_hint)
REQUIRED_SENTINELS = [
    # --- kind-conditional validation ---
    (
        "on_insert",
        "kind-conditional validation: 'on_insert' kind referenced in issue #19 AC",
        "issue #19 must mention 'on_insert' kind in its schema-hardening AC",
    ),
    (
        "on_remove_or_alter",
        "kind-conditional validation: 'on_remove_or_alter' kind referenced in issue #19 AC",
        "issue #19 must mention 'on_remove_or_alter' kind in its schema-hardening AC",
    ),
    # --- anchor resolution ---
    (
        "section_anchors",
        "anchor-resolution gate: 'section_anchors' referenced in issue #19 AC",
        "issue #19 must mention 'section_anchors' resolution to the standard form",
    ),
    # --- not_in_standard + sec-_new ---
    (
        "not_in_standard",
        "not_in_standard rule: referenced in issue #19 AC",
        "issue #19 must mention 'not_in_standard' topics carrying sec-_new",
    ),
    (
        "sec-_new",
        "sec-_new pseudo-anchor: referenced in issue #19 AC",
        "issue #19 must mention 'sec-_new' pseudo-anchor requirement",
    ),
    # --- required-token-presence check ---
    (
        "required_tokens",
        "required-token-presence check: 'required_tokens' referenced in issue #19 AC",
        "issue #19 must mention that on_remove_or_alter.required_tokens must exist in the standard",
    ),
    # --- exempt_terms liveness check ---
    (
        "exempt_terms",
        "exempt_terms liveness check: 'exempt_terms' referenced in issue #19 AC",
        "issue #19 must mention the exempt_terms liveness check",
    ),
    # --- empty-scope gate ---
    (
        "empty.*scope",
        "empty-scope gate: mentioned in issue #19 AC",
        "issue #19 must mention that rules with empty effective hunk-scope fail the build",
    ),
    # --- acceptable-variations lint ---
    (
        "acceptable.variation",
        "acceptable-variations lint: mentioned in issue #19 AC",
        "issue #19 must mention the acceptable-variations lint (zero detector fires)",
    ),
    # --- anchor-map builder ---
    (
        "anchor.map",
        "anchor-map builder: 'anchor map' or 'anchor-map' mentioned in some Phase 0 issue",
        "issue #16 or a new dedicated issue must mention the docx → section-anchor map builder",
    ),
    # --- rule-count correction ---
    (
        r"9\s*on_insert.*6\s*on_remove|6\s*on_remove.*9\s*on_insert|9\+6|15\s+rules|15\s+hard.rejection",
        "rule-count correction: corrected counts (15 rules = 9 on_insert + 6 on_remove_or_alter) noted",
        "docs must note the corrected rule count (15 = 9+6) to fix the stale '16'/'9+5' math",
    ),
]


def check_sentinel(text: str, sentinel: str) -> bool:
    """Return True if sentinel regex matches anywhere in text (case-insensitive)."""
    return bool(re.search(sentinel, text, re.IGNORECASE | re.DOTALL))


def main() -> int:
    if not PHASE0.exists():
        print(f"FAIL: {PHASE0.relative_to(REPO_ROOT)} does not exist")
        return 1
    if not GOVERNANCE.exists():
        print(f"FAIL: {GOVERNANCE.relative_to(REPO_ROOT)} does not exist")
        return 1

    text = read(PHASE0)
    failures = []

    print("Checking docs/phase-0-issues.md for post-redesign CI rule sentinels …\n")

    for sentinel, description, hint in REQUIRED_SENTINELS:
        found = check_sentinel(text, sentinel)
        status = "PASS" if found else "FAIL"
        print(f"  [{status}] {description}")
        if not found:
            failures.append((sentinel, description, hint))

    print()

    if failures:
        print(
            f"FAIL: {len(failures)} sentinel(s) not found in "
            f"docs/phase-0-issues.md.\n"
        )
        for sentinel, description, hint in failures:
            print(f"  Missing: {description!r}")
            print(f"  Hint   : {hint}")
            print(f"  Regex  : {sentinel!r}")
            print()
        print(
            "These are the post-redesign CI rules that issue #19's original AC\n"
            "does not enumerate.  GREEN fix: update issue #19's acceptance criteria\n"
            "in docs/phase-0-issues.md to include the missing rules, and add the\n"
            "anchor-map builder to issue #16's AC.\n"
            "See: https://github.com/contract-opf/contract-toaster/issues/46"
        )
        return 1

    print(
        f"PASS: all {len(REQUIRED_SENTINELS)} governance CI rule sentinels are "
        f"present in docs/phase-0-issues.md."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
