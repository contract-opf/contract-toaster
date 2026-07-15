#!/usr/bin/env python3
"""
CI gate: validate that gold fixtures for ACCEPT cases with detector_expectation=no_fire
actually produce no hard-rejection detector fires on their planted_variation.inserted_hunk.

This is also part of the RED test -- it confirms the detector fires today on the
planted acceptable-variation hunks, and must pass after the GREEN fix.

Issue #62 extends this gate to also check on_remove_or_alter rules (protects.
required_tokens over planted_variation.altered_hunk), since the mechanically-
derived gold fixtures added for #62 cover all 15 hard_rejection rules,
including the 6 on_remove_or_alter rules that this file previously skipped
silently (no detector_expectation branch matched them, so they passed only
because scored fixtures with no matching rule produce zero failures -- see
scripts/eval_harness.py for the authoritative model-free harness that now
also runs this check as part of the runner/scorer skeleton).

Issue #212: on_insert rule matching (check_on_insert_rule_fires) is now
imported from scripts/detector_common, the single shared implementation of
SPAN-level exempt_terms semantics, instead of a local hunk-wide copy.

Issue #213: on_remove_or_alter rule matching (check_on_remove_or_alter_rule_
fires) is now ALSO imported from scripts/detector_common instead of a local
copy, for the same reason -- three divergent implementations of "does this
hunk retain the protected required_tokens" is exactly the bug class #212
fixed for on_insert, recurring one layer down.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
FIXTURES_PATH = Path(__file__).parent / "gold-fixtures"

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from detector_common import check_on_insert_rule_fires  # noqa: E402
from detector_common import check_on_remove_or_alter_rule_fires as _check_on_remove_or_alter_rule_fires  # noqa: E402


def load_playbook(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def check_on_remove_or_alter_rule_fires(rule: dict, altered_hunk: str, topic_id: str) -> list:
    """Delegates to scripts/detector_common.check_on_remove_or_alter_rule_
    fires (issue #213), translating its {"rule_id", "missing_tokens"} fire
    shape into this file's established {"rule_id", "trigger_term"} shape
    (trigger_term = comma-joined missing tokens) so check_rule_fires()
    below and the failure-report formatting stay unchanged."""
    return [
        {"rule_id": fire["rule_id"], "trigger_term": ",".join(fire["missing_tokens"])}
        for fire in _check_on_remove_or_alter_rule_fires(rule, altered_hunk, topic_id)
    ]


def check_rule_fires(rule: dict, planted_variation: dict, topic_id: str) -> list:
    if rule.get("kind") == "on_insert":
        return check_on_insert_rule_fires(rule, planted_variation.get("inserted_hunk", ""), topic_id)
    if rule.get("kind") == "on_remove_or_alter":
        return check_on_remove_or_alter_rule_fires(rule, planted_variation.get("altered_hunk", ""), topic_id)
    return []


def run_fixture_tests(playbook: dict) -> int:
    hard_rejections = playbook.get("hard_rejections", [])
    failures = []
    fixture_files = sorted(FIXTURES_PATH.glob("*.json"))

    if not fixture_files:
        print("WARN: no gold fixture files found in tests/gold-fixtures/")
        return 0

    for fixture_path in fixture_files:
        with open(fixture_path) as f:
            fixture = json.load(f)

        case_id = fixture.get("case_id", fixture_path.name)
        detector_expectation = fixture.get("detector_expectation")
        planted_variation = fixture.get("planted_variation")

        if not detector_expectation or not planted_variation:
            continue  # No detector assertion for this fixture

        expected_result = detector_expectation.get("expected_result")
        rule_id = detector_expectation.get("rule_id")
        topic_id = planted_variation.get("topic_id", "")
        hunk_for_report = planted_variation.get("inserted_hunk") or planted_variation.get("altered_hunk", "")

        if expected_result == "no_fire":
            # Assert zero fires for this rule on this hunk
            for rule in hard_rejections:
                if rule.get("id") != rule_id:
                    continue
                fires = check_rule_fires(rule, planted_variation, topic_id)
                if fires:
                    failures.append({
                        "case_id": case_id,
                        "rule_id": rule_id,
                        "expected": "no_fire",
                        "actual": f"FIRED on {[f['trigger_term'] for f in fires]}",
                        "hunk": hunk_for_report[:120],
                    })
                else:
                    print(f"  PASS (no_fire): {case_id} / rule={rule_id}")
        elif expected_result == "fire":
            # Assert the rule does fire
            for rule in hard_rejections:
                if rule.get("id") != rule_id:
                    continue
                fires = check_rule_fires(rule, planted_variation, topic_id)
                if not fires:
                    failures.append({
                        "case_id": case_id,
                        "rule_id": rule_id,
                        "expected": "fire",
                        "actual": "did not fire",
                        "hunk": hunk_for_report[:120],
                    })
                else:
                    print(f"  PASS (fire): {case_id} / rule={rule_id}")

    if failures:
        print("\nFAIL: gold fixture detector expectations not met:\n")
        for f in failures:
            print(f"  case_id={f['case_id']!r}")
            print(f"  rule_id={f['rule_id']!r}")
            print(f"  expected={f['expected']!r}  actual={f['actual']!r}")
            print(f"  hunk: {f['hunk']!r}")
            print()
        return 1
    return 0


def main() -> int:
    playbook = load_playbook(PLAYBOOK_PATH)
    print("Running gold fixture detector tests...")
    rc = run_fixture_tests(playbook)
    if rc == 0:
        print("\nPASS: all gold fixture detector expectations met.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
