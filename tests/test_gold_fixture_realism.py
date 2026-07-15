#!/usr/bin/env python3
"""
Red gate for issue #218: gold fixtures are synthetic, detector-level only,
and their decision-level fields gate nothing.

This file exercises scripts/generate_gold_fixtures.py and the trigger
vocabulary of the no-uncapped-liability hard_rejection rule in
playbooks/eiaa-v1.0.0.json. Two assertions, both FAILED before the #218
fix:

  1. section_ref realism: a mechanically-generated on_insert gold fixture
     (e.g. reject-no-uncapped-liability.json, reject-no-ferpa-school-
     official.json) must carry a plausible REAL section label in
     expected_issues[].section_ref -- the topic's own section_ref field
     from the playbook (e.g. '8 Limitation on Liability', '5 Student
     Records', or '[absent] X' for a not_in_standard topic). Before the
     fix, scripts/generate_gold_fixtures.py._derive_on_insert_case() put
     the RULE's description sentence there instead (e.g. 'Counterparty
     introduces uncapped/unlimited liability.') -- see
     tests/gold-fixtures/reject-no-uncapped-liability.json:9 and
     reject-no-ferpa-school-official.json:9 before this fix.

  2. Trigger-vocabulary coverage: a real drafter proposing uncapped
     liability writes phrasings like 'no cap on liability', 'shall not
     be limited', or 'without limit' -- none of which fired
     no-uncapped-liability before this fix (only the bare words
     'uncapped' / 'unlimited liability' did). This asserts those three
     realistic hunks now fire no-uncapped-liability via the same detector
     scripts/eval_harness.py uses (scripts/detector_common.
     check_on_insert_rule_fires), scoped to the rule's
     'limitation-of-liability' topic.

Deterministic, offline, no network/AWS/Bedrock. Mirrors the repo
tests/test_*.py convention: a __main__ runner that executes every check
and exits non-zero on any failure (SKIP_INFRA-safe; this file never
shells out to cdk).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import detector_common  # noqa: E402
import generate_gold_fixtures  # noqa: E402


def _load_playbook() -> dict[str, Any]:
    return generate_gold_fixtures.load_playbook(generate_gold_fixtures.PLAYBOOK_PATH)


def _rules_by_id(playbook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in playbook.get("hard_rejections", [])}


def _topics_by_id(playbook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in playbook.get("topics", [])}


# ── Check 1: generated fixture section_ref is a plausible real section label ──

def check_generated_section_ref_is_real_section_label() -> list[str]:
    failures: list[str] = []
    playbook = _load_playbook()
    rules = _rules_by_id(playbook)
    topics = _topics_by_id(playbook)

    # Both currently reach the generator's on_insert path (mechanically
    # derived -- no hand-authored fixture exists for either rule).
    for rule_id in ("no-uncapped-liability", "no-ferpa-school-official"):
        rule = rules.get(rule_id)
        if rule is None:
            failures.append(f"  playbook is missing expected hard_rejection rule {rule_id!r}")
            continue

        fixture = generate_gold_fixtures.derive_fixture(rule, all_rules=None, topics_by_id=topics)
        issues = fixture.get("expected_issues", [])
        if not issues:
            failures.append(f"  {rule_id}: generated fixture has no expected_issues")
            continue

        section_ref = issues[0].get("section_ref")
        rule_description = rule.get("description", "")
        topic_id = issues[0].get("playbook_topic_id")
        topic = topics.get(topic_id, {})
        expected_section_ref = topic.get("section_ref", "")

        if section_ref == rule_description:
            failures.append(
                f"  {rule_id}: generated section_ref equals the RULE DESCRIPTION "
                f"({section_ref!r}) instead of a real section label -- "
                "_derive_on_insert_case() must look up topics_by_id[topic_id]"
                "['section_ref'], not rule['description']."
            )
        if section_ref != expected_section_ref:
            failures.append(
                f"  {rule_id}: generated section_ref {section_ref!r} does not match "
                f"the playbook topic's own section_ref {expected_section_ref!r} for "
                f"topic {topic_id!r}."
            )

    return failures


# ── Check 2: widened trigger vocabulary fires no-uncapped-liability ──

# One hunk per realistic phrase, isolated, so a failure names exactly which
# phrase the detector still misses.
_PHRASE_TO_HUNK = {
    "no cap on liability": (
        "Counterparty's aggregate liability under this Agreement shall be uncapped and "
        "there shall be no cap on liability for any claim arising hereunder."
    ),
    "shall not be limited": (
        "Notwithstanding Section 8, each party's liability under this Agreement shall not "
        "be limited by any cap, ceiling, or other restriction on damages."
    ),
    "without limit": (
        "In no event shall either party's liability be capped; damages recoverable "
        "hereunder shall be available without limit."
    ),
}


def check_widened_vocabulary_fires_no_uncapped_liability() -> list[str]:
    failures: list[str] = []
    playbook = _load_playbook()
    rule = _rules_by_id(playbook).get("no-uncapped-liability")
    if rule is None:
        return ["  playbook is missing expected hard_rejection rule 'no-uncapped-liability'"]

    for phrase, hunk in _PHRASE_TO_HUNK.items():
        fires = detector_common.check_on_insert_rule_fires(
            rule, hunk, topic_id="limitation-of-liability"
        )
        fired_rule_ids = {f["rule_id"] for f in fires}
        if "no-uncapped-liability" not in fired_rule_ids:
            failures.append(
                f"  realistic drafter phrase {phrase!r} does not fire "
                "no-uncapped-liability (hunk: "
                f"{hunk!r}). trigger_terms for no-uncapped-liability must be widened "
                "to cover this phrasing."
            )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("1", "Generated fixture section_ref is a real section label, not the rule description",
         check_generated_section_ref_is_real_section_label),
        ("2", "Widened trigger vocabulary fires no-uncapped-liability on realistic drafter phrasing",
         check_widened_vocabulary_fires_no_uncapped_liability),
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
        print("All gold-fixture realism checks passed.")
        return 0
    else:
        print("One or more gold-fixture realism checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
