#!/usr/bin/env python3
"""
Coverage slice for issue #183: "Author the legal-judgment gold test set
(near-miss/ACCEPT/must_not_flag) for the eval harness."

#62 landed the model-free harness skeleton plus a mechanically-derived
gold-case subset (one synthetic planted-violation case per existing
hard_rejection rule). What was still missing is the set of judgment-
shaped case types a mechanical generator cannot produce: near-miss,
ACCEPT, must_not_flag, and acceptable_variations cases.

Per the issue's 2026-07-10 owner decision, this ticket's AFK scope is a
fully SYNTHETIC scaffold: every new fixture is marked "synthetic": true
and "gc_signoff": "pending" (not yet production-qualified -- Legal/GC
still signs off before these gate anything for real), and no fixture
prose may reference "Exos"/"EXOS" (release de-brand voicing). The
underlying playbook/rule identifiers (e.g. "preserve-exos-precedence",
"no-exos-indemnity") are pre-existing data this ticket does not touch --
same treatment "EIAA" already gets -- so the de-brand scan below is
scoped to free-text PROSE fields only (description, planted_variation
hunks/notes, must_not_flag reasons, detector_expectation rationale), not
to case_id/rule_id/topic_id identifiers.

Four checks, each executed against tests/gold-fixtures/:

  1. Case-type coverage: near-miss, ACCEPT, must_not_flag, and
     acceptable_variations cases are ALL present somewhere in the gold
     set (existing + new).
  2. Every issue-183 fixture (NEW_FIXTURES_183 below) exists on disk.
  3. Every issue-183 fixture carries "synthetic": true and
     "gc_signoff": "pending".
  4. No issue-183 fixture's prose fields contain "exos" (case-insensitive).

Deterministic, offline, no network/AWS/Bedrock. Mirrors the repo
tests/test_*.py convention: a __main__ runner that executes every check
and exits non-zero on any failure (SKIP_INFRA-safe).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "gold-fixtures"

# The 16 synthetic fixtures this ticket adds. Listed explicitly so checks
# 2-4 assert against exactly this ticket's slice, not the whole (older,
# Exos-branded) gold set.
NEW_FIXTURES_183 = [
    "near-miss-non-exclusive-restated",
    "near-miss-students-not-employees-restated",
    "near-miss-neither-party-business-associate",
    "near-miss-including-without-limitation",
    "near-miss-ferpa-designated-official",
    "near-miss-preserved-liability-cap",
    "near-miss-preserved-consequential-damages-waiver",
    "near-miss-order-of-precedence-reworded-still-prevails",
    "accept-governing-law-home-state-litigation-venue",
    "accept-insurance-certificate-of-insurance",
    "accept-term-length-shortened-initial-term",
    "accept-notices-deemed-receipt-email",
    "accept-confidentiality-destruction-timeline",
    "accept-non-discrimination-additional-protected-classes",
    "accept-inspections-accompanied-and-notice",
    "accept-survival-narrow-compliance-survival",
]

# Prose fields scanned for "exos"/"EXOS" -- free text a fixture author
# writes, not playbook-defined identifiers.
_PROSE_FIELDS_TOP = ("description",)
_PLANTED_VARIATION_PROSE_FIELDS = ("inserted_hunk", "altered_hunk", "notes")
_MUST_NOT_FLAG_PROSE_FIELDS = ("reason",)
_DETECTOR_EXPECTATION_PROSE_FIELDS = ("rationale",)


def _load_all_fixtures() -> dict[str, dict[str, Any]]:
    fixtures: dict[str, dict[str, Any]] = {}
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        case_id = raw.get("case_id", path.stem)
        fixtures[case_id] = raw
    return fixtures


def _prose_strings(raw: dict[str, Any]) -> list[str]:
    strings: list[str] = []
    for field in _PROSE_FIELDS_TOP:
        val = raw.get(field)
        if isinstance(val, str):
            strings.append(val)

    pv = raw.get("planted_variation") or {}
    for field in _PLANTED_VARIATION_PROSE_FIELDS:
        val = pv.get(field)
        if isinstance(val, str):
            strings.append(val)

    for entry in raw.get("must_not_flag") or []:
        if not isinstance(entry, dict):
            continue
        for field in _MUST_NOT_FLAG_PROSE_FIELDS:
            val = entry.get(field)
            if isinstance(val, str):
                strings.append(val)

    de = raw.get("detector_expectation") or {}
    for field in _DETECTOR_EXPECTATION_PROSE_FIELDS:
        val = de.get(field)
        if isinstance(val, str):
            strings.append(val)

    return strings


# ── Check 1: case-type coverage across the whole gold set ──────────────────

def check_case_type_coverage(fixtures: dict[str, dict[str, Any]]) -> list[str]:
    failures: list[str] = []

    has_near_miss = any(
        "near-miss" in case_id or "near_miss" in case_id or raw.get("case_type") == "near_miss"
        for case_id, raw in fixtures.items()
    )
    has_accept = any(raw.get("expected_decision") == "ACCEPT" for raw in fixtures.values())
    has_must_not_flag = any(raw.get("must_not_flag") for raw in fixtures.values())
    has_acceptable_variation = any(
        "acceptable_variation" in (raw.get("description") or "").lower()
        or any(
            "acceptable_variation" in (entry.get("reason") or "").lower()
            for entry in (raw.get("must_not_flag") or [])
            if isinstance(entry, dict)
        )
        for raw in fixtures.values()
    )

    if not has_near_miss:
        failures.append("  no gold fixture is tagged as a near-miss case (case_id containing 'near-miss'/'near_miss', or case_type=='near_miss')")
    if not has_accept:
        failures.append("  no gold fixture has expected_decision == 'ACCEPT'")
    if not has_must_not_flag:
        failures.append("  no gold fixture has a non-empty must_not_flag[] list")
    if not has_acceptable_variation:
        failures.append("  no gold fixture's description/must_not_flag[].reason mentions 'acceptable_variation'")

    return failures


# ── Check 2: every issue-183 fixture exists ─────────────────────────────────

def check_new_fixtures_exist(fixtures: dict[str, dict[str, Any]]) -> list[str]:
    failures = []
    for case_id in NEW_FIXTURES_183:
        if case_id not in fixtures:
            failures.append(f"  missing tests/gold-fixtures/{case_id}.json")
    return failures


# ── Check 3: every issue-183 fixture is marked synthetic + pending GC sign-off ──

def check_new_fixtures_marked_synthetic_pending(fixtures: dict[str, dict[str, Any]]) -> list[str]:
    failures = []
    for case_id in NEW_FIXTURES_183:
        raw = fixtures.get(case_id)
        if raw is None:
            continue  # already reported by check 2
        if raw.get("synthetic") is not True:
            failures.append(f"  {case_id}: \"synthetic\" is not True (got {raw.get('synthetic')!r})")
        if raw.get("gc_signoff") != "pending":
            failures.append(f"  {case_id}: \"gc_signoff\" is not 'pending' (got {raw.get('gc_signoff')!r})")
    return failures


# ── Check 4: no issue-183 fixture prose contains "exos" ─────────────────────

def check_new_fixtures_no_exos_in_prose(fixtures: dict[str, dict[str, Any]]) -> list[str]:
    failures = []
    for case_id in NEW_FIXTURES_183:
        raw = fixtures.get(case_id)
        if raw is None:
            continue  # already reported by check 2
        for text in _prose_strings(raw):
            if "exos" in text.lower():
                failures.append(f"  {case_id}: prose field contains 'exos' (release de-brand rule): {text!r}")
    return failures


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    fixtures = _load_all_fixtures()

    checks = [
        ("1", "Case-type coverage (near-miss, ACCEPT, must_not_flag, acceptable_variations) present in gold set",
         lambda: check_case_type_coverage(fixtures)),
        ("2", "All issue-183 synthetic fixtures exist",
         lambda: check_new_fixtures_exist(fixtures)),
        ("3", "All issue-183 fixtures marked synthetic:true and gc_signoff:pending",
         lambda: check_new_fixtures_marked_synthetic_pending(fixtures)),
        ("4", "No issue-183 fixture prose contains 'Exos'/'EXOS'",
         lambda: check_new_fixtures_no_exos_in_prose(fixtures)),
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
        print("All gold-set coverage checks (issue #183) passed.")
        return 0
    else:
        print("One or more gold-set coverage checks (issue #183) FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
