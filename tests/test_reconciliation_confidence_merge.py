#!/usr/bin/env python3
"""
Slice test (TDD) for issue #265: "Reconciliation: critic disagreement never
reaches the confidence band shown at the download gate".

## Root problem this proves fixed

Before this slice, `reconcile()` (scripts/reconciliation.py) always copied
`confidence_state` / `confidence_band` straight from `primary_result`, even
when the critic contested a replacement or added an issue. The critic can
force `decision=REQUEST_CHANGE`, but had no way to move the confidence band
-- so the band shown above the download button (the #85/#255 trust gate)
could misrepresent a contested review as a confident one (`confidence_band
= null`) even though the critic disagreed with the primary reviewer.

## Merge rule under test (documented in docs/output-contract.md -> "Confidence
band" -> "Critic-delta confidence merge rule")

  - Confidence levels are ordered OK < LOW_CONFIDENCE < MANUAL_REVIEW_REQUIRED
    < ERROR_MANUAL_REVIEW_REQUIRED (least to most degraded).
  - If the critic contests a replacement OR adds an issue, the final
    confidence_state is degraded (moved) at least one level below the
    primary's confidence_state, capped at ERROR_MANUAL_REVIEW_REQUIRED.
  - A critic rationale objection alone (no contested replacement, no added
    issue) does NOT degrade the band.
  - No critic delta at all -> the primary's confidence_state/confidence_band
    pass through unchanged.
  - The rule is monotonic: the critic can only degrade (never improve/raise)
    the band relative to the primary's own confidence_state.
  - confidence_band mirrors confidence_state: null when confidence_state is
    OK, else the confidence_state string itself (per docs/output-contract.md
    -> "Confidence band").

Run with: python3 tests/test_reconciliation_confidence_merge.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import reconciliation as recon  # noqa: E402


def _primary(confidence_state: str = "OK", decision: str = "ACCEPT") -> dict[str, Any]:
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": decision,
        "confidence_state": confidence_state,
        "confidence_band": None if confidence_state == "OK" else confidence_state,
        "issues": [],
        "critic_delta": None,
        "verdict_summary": "Nothing material changed.",
    }


def _critic_added_issue() -> dict[str, Any]:
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": {
            "added_issues": [
                {
                    "section_ref": "9",
                    "section_title": "Non-Exclusivity",
                    "counterparty_change_summary": "Counterparty added a non-exclusive carve-out.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Non-exclusivity must be flagged.",
                    "proposed_replacement_text": "This arrangement is exclusive.",
                    "playbook_topic_id": "non-exclusive-arrangement",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                }
            ],
            "contested_replacements": [],
            "rationale_objections": [],
        },
        "verdict_summary": None,
    }


def _critic_contested_replacement() -> dict[str, Any]:
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": {
            "added_issues": [],
            "contested_replacements": [
                {
                    "section_ref": "8",
                    "primary_replacement_text": "Liability shall not exceed $150,000.",
                    "critic_objection": "This drifts from the playbook's gross-negligence carve-out.",
                    "critic_suggested_replacement": "Liability shall not exceed $150,000, except in cases of gross negligence.",
                }
            ],
            "rationale_objections": [],
        },
        "verdict_summary": None,
    }


def _critic_rationale_objection_only() -> dict[str, Any]:
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": {
            "added_issues": [],
            "contested_replacements": [],
            "rationale_objections": [
                {
                    "section_ref": "8",
                    "critic_objection": "The rationale footnote undersells the risk.",
                }
            ],
        },
        "verdict_summary": None,
    }


def _primary_request_change_issue() -> dict[str, Any]:
    primary = _primary(confidence_state="OK", decision="REQUEST_CHANGE")
    primary["issues"] = [
        {
            "section_ref": "8",
            "section_title": "Limitation of Liability",
            "counterparty_change_summary": "Cap lowered to $75,000.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "Cap must remain at $150,000.",
            "proposed_replacement_text": "Liability shall not exceed $150,000.",
            "playbook_topic_id": "limitation-of-liability",
            "internal_precedent_citation": None,
            "provenance": "model",
        }
    ]
    return primary


# ---------------------------------------------------------------------------
# 1. Critic-added issue degrades an OK band to LOW_CONFIDENCE.
# ---------------------------------------------------------------------------


def test_critic_added_issue_degrades_ok_band(failures: list[str]) -> None:
    primary = _primary(confidence_state="OK")
    critic = _critic_added_issue()

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])

    if result["confidence_state"] != "LOW_CONFIDENCE":
        failures.append(
            f"[1a] Critic-added issue must degrade confidence_state one level from OK; "
            f"got {result['confidence_state']!r}"
        )
    if result["confidence_band"] != "LOW_CONFIDENCE":
        failures.append(
            f"[1b] confidence_band must mirror the degraded confidence_state; got {result['confidence_band']!r}"
        )


# ---------------------------------------------------------------------------
# 2. Contested replacement degrades an OK band to LOW_CONFIDENCE.
# ---------------------------------------------------------------------------


def test_contested_replacement_degrades_ok_band(failures: list[str]) -> None:
    primary = _primary_request_change_issue()
    critic = _critic_contested_replacement()

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])

    if result["confidence_state"] != "LOW_CONFIDENCE":
        failures.append(
            f"[2a] Contested replacement must degrade confidence_state one level from OK; "
            f"got {result['confidence_state']!r}"
        )
    if result["confidence_band"] != "LOW_CONFIDENCE":
        failures.append(f"[2b] confidence_band must mirror LOW_CONFIDENCE; got {result['confidence_band']!r}")
    # Primary text must still stand -- this reconciliation slice does not
    # change that #82 rule.
    if result["issues"][0]["proposed_replacement_text"] != "Liability shall not exceed $150,000.":
        failures.append("[2c] Primary replacement text must remain unmodified by the confidence merge.")


# ---------------------------------------------------------------------------
# 3. Degradation is one level at a time, capped at ERROR_MANUAL_REVIEW_REQUIRED.
# ---------------------------------------------------------------------------


def test_degradation_steps_one_level_at_a_time(failures: list[str]) -> None:
    cases = [
        ("OK", "LOW_CONFIDENCE"),
        ("LOW_CONFIDENCE", "MANUAL_REVIEW_REQUIRED"),
        ("MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED"),
        ("ERROR_MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED"),  # capped
    ]
    for start, expected in cases:
        primary = _primary(confidence_state=start)
        critic = _critic_added_issue()
        result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])
        if result["confidence_state"] != expected:
            failures.append(
                f"[3] Degrading from {start!r} must yield {expected!r}; got {result['confidence_state']!r}"
            )


# ---------------------------------------------------------------------------
# 4. A rationale objection alone does NOT degrade the band.
# ---------------------------------------------------------------------------


def test_rationale_objection_alone_does_not_degrade(failures: list[str]) -> None:
    primary = _primary(confidence_state="OK")
    critic = _critic_rationale_objection_only()

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])

    if result["confidence_state"] != "OK":
        failures.append(
            f"[4a] A rationale objection alone must not degrade confidence_state; got {result['confidence_state']!r}"
        )
    if result["confidence_band"] is not None:
        failures.append(f"[4b] confidence_band must remain null when confidence_state is OK; got {result['confidence_band']!r}")


# ---------------------------------------------------------------------------
# 5. No critic delta at all -> primary band passes through unchanged.
# ---------------------------------------------------------------------------


def test_no_delta_keeps_primary_band(failures: list[str]) -> None:
    primary = _primary(confidence_state="MANUAL_REVIEW_REQUIRED")

    result = recon.reconcile(primary_result=primary, critic_result=None, detector_fires=[])

    if result["confidence_state"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[5a] No critic run -> confidence_state must pass through unchanged; got {result['confidence_state']!r}"
        )
    if result["confidence_band"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[5b] confidence_band must pass through unchanged; got {result['confidence_band']!r}")


def test_no_delta_with_critic_result_keeps_primary_band(failures: list[str]) -> None:
    # Critic ran, replied ACCEPT with no delta at all -- primary band unaffected.
    primary = _primary(confidence_state="LOW_CONFIDENCE")
    critic = {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": None,
        "verdict_summary": None,
    }

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])

    if result["confidence_state"] != "LOW_CONFIDENCE":
        failures.append(
            f"[5c] Critic with no delta must not change confidence_state; got {result['confidence_state']!r}"
        )
    if result["confidence_band"] != "LOW_CONFIDENCE":
        failures.append(f"[5d] confidence_band must remain LOW_CONFIDENCE; got {result['confidence_band']!r}")


# ---------------------------------------------------------------------------
# 6. Monotonic: the critic can never raise (improve) the band, only degrade.
# ---------------------------------------------------------------------------


def test_critic_can_never_raise_the_band(failures: list[str]) -> None:
    # Primary is already at its worst state; critic has no delta at all --
    # the band must not somehow "improve" back toward OK.
    primary = _primary(confidence_state="ERROR_MANUAL_REVIEW_REQUIRED")
    critic = {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": None,
        "verdict_summary": None,
    }

    result = recon.reconcile(primary_result=primary, critic_result=critic, detector_fires=[])
    if result["confidence_state"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[6a] A critic ACCEPT with no delta must never raise/improve the primary's confidence_state; "
            f"got {result['confidence_state']!r}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_critic_added_issue_degrades_ok_band,
    test_contested_replacement_degrades_ok_band,
    test_degradation_steps_one_level_at_a_time,
    test_rationale_objection_alone_does_not_degrade,
    test_no_delta_keeps_primary_band,
    test_no_delta_with_critic_result_keeps_primary_band,
    test_critic_can_never_raise_the_band,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all reconciliation confidence-merge (issue #265) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
