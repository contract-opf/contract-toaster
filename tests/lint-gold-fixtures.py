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

Issue #400 fix-round-1: this file is now the sole home for the deterministic
detector-correctness invariants that used to live in the retired
scripts/eval_harness.py::run_detectors_on_case/score_case (that module now
drives the LLM-native pipeline instead -- see its own docstring). Two
invariants restored here:

  - Cross-rule false positives (D2 "and only it" / D1 zero-fire floor),
    within the fixture's own topic: `run_fixture_tests` below now runs
    every hard_rejection rule against a fixture's hunk, not just the
    fixture's own named rule -- but each rule's `applies_to_topics` guard
    (scripts/detector_common.py) still limits which rules can ever fire on
    a given hunk to those admitting the fixture's `topic_id`. Within that
    scope, a no_fire fixture fails if any admitted rule fires beyond
    `fp_tolerance`, and a fire fixture fails if any OTHER admitted rule
    also fires beyond `fp_tolerance`. A rule scoped to a different topic
    cannot be caught by this sweep even if its trigger terms appear in the
    hunk's text (issue #400 fix-round-2: this is the sweep's documented
    limit, not a claim of true cross-topic coverage).
  - Per-rule topic coverage (D2 "at least one gold case per
    hard_rejections[].id"): `check_topic_coverage` below delegates to
    scripts/generate_gold_fixtures.py::missing_rule_coverage (the module
    that already owns this bookkeeping for the mechanical fixture
    generator) and fails the gate when it reports any uncovered rule.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
FIXTURES_PATH = Path(__file__).parent / "gold-fixtures"

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from detector_common import check_on_insert_rule_fires  # noqa: E402
from detector_common import check_on_remove_or_alter_rule_fires as _check_on_remove_or_alter_rule_fires  # noqa: E402
import generate_gold_fixtures  # noqa: E402
import playbook_registry  # noqa: E402

# Resolved through the registry (issue #209), not a hard-coded literal --
# tests/gold-fixtures/ is the "synthetic-generic" playbook_id's registered fixtures_dir,
# so its rule_ids are checked against whatever playbook is currently
# registered for "synthetic-generic" (a brand-neutral synthetic fixture; see #403).
PLAYBOOK_PATH = playbook_registry.resolve_playbook("synthetic-generic").playbook_path


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
        inserted_hunk = planted_variation.get("inserted_hunk")
        if inserted_hunk is None:
            return []
        return check_on_insert_rule_fires(rule, inserted_hunk, topic_id)
    if rule.get("kind") == "on_remove_or_alter":
        altered_hunk = planted_variation.get("altered_hunk")
        if altered_hunk is None:
            return []
        return check_on_remove_or_alter_rule_fires(rule, altered_hunk, topic_id)
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
        fp_tolerance = fixture.get("fp_tolerance", 0)

        # Evaluate every hard_rejection rule against this fixture's hunk --
        # not just the fixture's own named rule -- so this gate also
        # enforces D1's zero-fire floor and D2's "and only it" for every
        # rule whose applies_to_topics admits this fixture's topic_id (a
        # rule scoped to a different topic cannot fire here regardless of
        # its trigger terms -- see detector_common.check_on_insert_rule_
        # fires's applies_to_topics guard). See module docstring "Issue
        # #400 fix-round-1/2".
        all_fires = []
        for rule in hard_rejections:
            all_fires.extend(check_rule_fires(rule, planted_variation, topic_id))
        fired_rule_ids = sorted({f["rule_id"] for f in all_fires})
        other_rule_ids = [r for r in fired_rule_ids if r != rule_id]

        if expected_result == "no_fire":
            # Assert zero (within fp_tolerance) fires across every rule admitted
            # by this fixture's topic_id (see applies_to_topics guard above).
            if fired_rule_ids and len(fired_rule_ids) > fp_tolerance:
                failures.append({
                    "case_id": case_id,
                    "rule_id": rule_id,
                    "expected": "no_fire",
                    "actual": f"FIRED on {fired_rule_ids}",
                    "hunk": hunk_for_report[:120],
                })
            else:
                print(f"  PASS (no_fire): {case_id} / rule={rule_id}")
        elif expected_result == "fire":
            # Assert the named rule fires, and no other rule fires beyond fp_tolerance.
            if rule_id not in fired_rule_ids:
                failures.append({
                    "case_id": case_id,
                    "rule_id": rule_id,
                    "expected": "fire",
                    "actual": "did not fire",
                    "hunk": hunk_for_report[:120],
                })
            elif len(other_rule_ids) > fp_tolerance:
                failures.append({
                    "case_id": case_id,
                    "rule_id": rule_id,
                    "expected": f"fire (and only it, within fp_tolerance={fp_tolerance})",
                    "actual": f"also FIRED on {other_rule_ids}",
                    "hunk": hunk_for_report[:120],
                })
            else:
                print(f"  PASS (fire): {case_id} / rule={rule_id}")
        else:
            # Guards against a fixture's expected_result silently drifting
            # to a typo or unrecognized value (e.g. "fires") -- without
            # this branch such a fixture matches neither the "no_fire" nor
            # "fire" arm above and is silently never checked, while the
            # gate still exits 0. See tests/detector/test_match_mode_and_
            # semantics_220.py's equivalent guard for the same failure mode.
            failures.append({
                "case_id": case_id,
                "rule_id": rule_id,
                "expected": "'no_fire' or 'fire'",
                "actual": f"unrecognized detector_expectation.expected_result: {expected_result!r}",
                "hunk": hunk_for_report[:120],
            })

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


def check_topic_coverage() -> list[str]:
    """Every hard_rejection rule id must have at least one gold-case
    detector fixture in FIXTURES_PATH (docs/evaluation.md 'Regression
    gates' #2 / D2 "at least one gold case per hard_rejections[].id").
    Delegates to scripts/generate_gold_fixtures.py::missing_rule_coverage
    -- the module that already owns this bookkeeping for the mechanical
    fixture generator -- so the coverage definition cannot drift between
    the generator and this gate."""
    return generate_gold_fixtures.missing_rule_coverage(FIXTURES_PATH, PLAYBOOK_PATH)


def main() -> int:
    playbook = load_playbook(PLAYBOOK_PATH)
    print("Running gold fixture detector tests...")
    rc = run_fixture_tests(playbook)

    missing = check_topic_coverage()
    if missing:
        print(
            f"\nFAIL: {len(missing)} hard_rejection rule(s) have no gold-case detector "
            f"fixture: {missing}. Every rule must have at least one planted-violation "
            f"gold case (docs/evaluation.md topic-coverage gate)."
        )
        rc = 1

    if rc == 0:
        print("\nPASS: all gold fixture detector expectations met.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
