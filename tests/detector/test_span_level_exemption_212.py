#!/usr/bin/env python3
"""
Slice test (TDD) for issue #212: span-level exempt_terms exemption.

playbooks/schema.json's hard_rejections[].exempt_terms description (~line
521-527) is explicit: "A trigger hit that falls INSIDE an exempt phrase
does not fire." Before this fix, all three reference implementations
(scripts/eval_harness.py, tests/lint-gold-fixtures.py,
tests/lint-acceptable-variations.py) checked exemption HUNK-WIDE: if any
exempt phrase matched anywhere in the hunk, EVERY trigger fire in that hunk
was suppressed -- including a fully separate, non-exempt trigger match
located elsewhere in the same hunk. That let a counterparty co-locate one
mutual-sounding sentence with a one-way rights grab in a single hunk and
silently defeat the two "deterministic, near-certain" hard-rejection rules
this test targets: no-exos-indemnity and no-exclusivity.

This test:
  1. Imports scripts/detector_common (the shared module this fix adds) and
     directly reproduces the two concrete bypasses quoted verbatim in the
     issue, in both sentence orders, asserting each still fires after the
     fix (and would NOT fire under the old hunk-wide semantics -- see
     _hunk_wide_is_exempted below, which intentionally re-implements the
     PRE-FIX buggy behavior so the regression is pinned, not just the fix).
  2. Asserts scripts/eval_harness.py, tests/lint-gold-fixtures.py, and
     tests/lint-acceptable-variations.py all import their on_insert
     exemption logic from detector_common (issue #212's "pick one
     implementation ... make the lints import it" requirement), so the
     three call sites cannot silently re-diverge into hunk-wide copies.
  3. Runs the four adversarial gold fixtures added alongside this test
     (tests/gold-fixtures/reject-combined-hunk-*-212.json's siblings)
     through scripts/eval_harness.score_all() end-to-end and asserts they
     PASS.

On the pre-fix tree this test fails at step 1's import (scripts/
detector_common.py does not exist yet) -- reproducing the Concern per the
issue's "Required verification" section. After the fix it passes.

Run with: python3 tests/detector/test_span_level_exemption_212.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import detector_common  # noqa: E402
import eval_harness  # noqa: E402

FAILURES: list[str] = []


def _check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def load_playbook() -> dict:
    with open(PLAYBOOK_PATH) as f:
        return json.load(f)


def _hunk_wide_is_exempted(text: str, exempt_terms: list[str], match_type: str) -> bool:
    """Intentional re-implementation of the PRE-#212 buggy hunk-wide
    exemption check (scripts/eval_harness.py's old _is_exempted): True if
    ANY exempt phrase matches anywhere in `text`, regardless of where the
    trigger match is. Used only to prove these fixtures actually exercise
    the bug this fix closes (i.e. that the old logic would have wrongly
    suppressed the fire)."""
    for exempt in exempt_terms:
        if detector_common.phrase_matches(text, exempt, match_type):
            return True
    return False


# ---------------------------------------------------------------------------
# 1. Direct repro of the two adversarial bypasses quoted in issue #212
# ---------------------------------------------------------------------------

COMBINED_HUNKS = {
    "no-exos-indemnity": [
        (
            "exempt_first",
            "Each party shall hold harmless the other party from ordinary "
            "third-party claims arising in the normal course of business. "
            "In addition, Exos shall indemnify Institution for any and all "
            "claims, damages, and losses, without limit.",
        ),
        (
            "violation_first",
            "Exos shall indemnify Institution for any and all claims, "
            "damages, and losses, without limit. In addition, each party "
            "shall hold harmless the other party from ordinary third-party "
            "claims arising in the normal course of business.",
        ),
    ],
    "no-exclusivity": [
        (
            "exempt_first",
            "This Agreement is non-exclusive as to placements in Arizona. "
            "Institution shall be the exclusive provider of interns to Exos "
            "nationwide.",
        ),
        (
            "violation_first",
            "Institution shall be the exclusive provider of interns to Exos "
            "nationwide. This Agreement is non-exclusive as to placements in "
            "Arizona.",
        ),
    ],
}


def test_combined_hunks_fire_under_span_level_exemption() -> None:
    playbook = load_playbook()
    rules = {r["id"]: r for r in playbook.get("hard_rejections", [])}
    topic_id_by_rule = {
        "no-exos-indemnity": "indemnification",
        "no-exclusivity": "exclusivity",
    }

    for rule_id, variants in COMBINED_HUNKS.items():
        rule = rules[rule_id]
        topic_id = topic_id_by_rule[rule_id]
        match_type = rule.get("match", "word_boundary")
        exempt_terms = rule.get("exempt_terms", [])

        for order_label, hunk in variants:
            # (a) The old hunk-wide check WOULD have suppressed this hunk --
            # pin that the fixture actually exercises the reported bug.
            _check(
                _hunk_wide_is_exempted(hunk, exempt_terms, match_type),
                f"{rule_id}/{order_label}: fixture hunk does not contain an "
                f"exempt phrase match, so it does not reproduce the reported "
                f"hunk-wide bypass: {hunk!r}",
            )

            # (b) The fixed, span-level implementation must still fire.
            fires = detector_common.check_on_insert_rule_fires(rule, hunk, topic_id)
            fired_rule_ids = {f["rule_id"] for f in fires}
            _check(
                rule_id in fired_rule_ids,
                f"{rule_id}/{order_label}: expected a fire under span-level "
                f"exemption but got none. hunk={hunk!r} fires={fires!r}",
            )


# ---------------------------------------------------------------------------
# 2. Structural check: the three call sites import the shared module
# ---------------------------------------------------------------------------

CALL_SITES = {
    "scripts/eval_harness.py": SCRIPTS_DIR / "eval_harness.py",
    "tests/lint-gold-fixtures.py": TESTS_DIR / "lint-gold-fixtures.py",
    "tests/lint-acceptable-variations.py": TESTS_DIR / "lint-acceptable-variations.py",
}

IMPORT_PATTERN = re.compile(r"^\s*(import detector_common|from detector_common import)", re.MULTILINE)


def test_call_sites_import_shared_module() -> None:
    for label, path in CALL_SITES.items():
        _check(path.exists(), f"{label}: file not found at {path}")
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        _check(
            bool(IMPORT_PATTERN.search(source)),
            f"{label}: does not import scripts/detector_common -- on_insert "
            f"exemption logic must be delegated to the shared module, not a "
            f"local copy (issue #212).",
        )


# ---------------------------------------------------------------------------
# 3. End-to-end: the adversarial gold fixtures score PASS via eval_harness
# ---------------------------------------------------------------------------

ADVERSARIAL_FIXTURE_CASE_IDS = {
    "reject-combined-hunk-no-exos-indemnity-exempt-first",
    "reject-combined-hunk-no-exos-indemnity-violation-first",
    "reject-combined-hunk-no-exclusivity-exempt-first",
    "reject-combined-hunk-no-exclusivity-violation-first",
}


def test_adversarial_fixtures_score_pass() -> None:
    # Explicit eiaa fixtures_dir/playbook_path (issue #343 repointed
    # eval_harness's module-level default FIXTURES_PATH/PLAYBOOK_PATH to the
    # public "sample-agreement" sample playbook) -- these adversarial
    # fixtures live in tests/gold-fixtures/, eiaa's directory.
    results = {
        r.case_id: r
        for r in eval_harness.score_all(fixtures_dir=REPO_ROOT / "tests" / "gold-fixtures", playbook_path=PLAYBOOK_PATH)
    }
    missing = ADVERSARIAL_FIXTURE_CASE_IDS - set(results)
    _check(not missing, f"adversarial gold fixtures not found by eval_harness: {sorted(missing)}")

    for case_id in sorted(ADVERSARIAL_FIXTURE_CASE_IDS & set(results)):
        result = results[case_id]
        _check(
            result.passed,
            f"{case_id}: eval_harness scored FAIL: {result.reasons!r}",
        )


def main() -> int:
    test_combined_hunks_fire_under_span_level_exemption()
    test_call_sites_import_shared_module()
    test_adversarial_fixtures_score_pass()

    if FAILURES:
        print("FAIL: span-level exemption slice test (issue #212):\n")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"\n{len(FAILURES)} failure(s).")
        return 1

    print("PASS: span-level exempt_terms exemption verified (issue #212).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
