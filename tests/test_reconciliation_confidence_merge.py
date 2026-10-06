#!/usr/bin/env python3
"""
`reconcile()`'s confidence band and its schema conformance -- issue #265's
merge rule, RETIRED by issue #138 (ADR 0001).

## What changed

Issue #265 made a critic that disagreed degrade the primary's
`confidence_state` one level (a contested replacement or an added issue moved
`OK` to `LOW_CONFIDENCE`, and so on), because the critic could only flag: the
primary's words shipped, and a band that hid the critic's objection would
have misrepresented a contested review as a confident one.

Under ADR 0001 the critic no longer contests -- it decides. Its result is the
final review, so its own `confidence_state` is the final one
(docs/output-contract.md -> "Confidence band" -> "Critic-delta confidence
merge rule", rewritten by #138). This file pins the retirement:

  - the final `confidence_state` is the CRITIC's, whatever the primary's;
  - so a critic CAN report a better band than the primary (the old rule's
    monotonic "never raise" no longer holds -- the primary's band is not the
    baseline any more);
  - a critic override, an added issue or a rationale objection moves nothing
    by itself;
  - `confidence_band` mirrors the final `confidence_state`: null when OK.

And it keeps the schema-conformance guard (`[7]`, issue #627 lineage): the
result `reconcile()` stamps with `SCHEMA_VERSION` must validate against that
schema, EXCEPT for the pipeline-owned extensions #138 documents, which are
stripped first and nothing else: a `primary-retained` provenance, the
`source` tag on a critic override, and the computed override entries.

Run with: python3 tests/test_reconciliation_confidence_merge.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import primary_review_pass as pp  # noqa: E402
import reconciliation as recon  # noqa: E402

_LEVELS = ("OK", "LOW_CONFIDENCE", "MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED")


def _issue(key: str, topic: str = "limitation-of-liability", section: str = "8") -> dict[str, Any]:
    return {
        "issue_key": key,
        "section_ref": section,
        "section_title": "Limitation of Liability",
        "counterparty_change_summary": "Cap lowered to $75,000.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "Cap must remain at $150,000.",
        "playbook_topic_id": topic,
        "internal_precedent_citation": None,
        "provenance": "model",
    }


def _primary(confidence_state: str = "OK", *, with_issue: bool = False) -> dict[str, Any]:
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE" if with_issue else "ACCEPT",
        "confidence_state": confidence_state,
        "confidence_band": None if confidence_state == "OK" else confidence_state,
        "issues": [_issue("I1")] if with_issue else [],
        "critic_delta": None,
        "verdict_summary": "Nothing material changed.",
    }


def _critic(
    confidence_state: str = "OK",
    *,
    keeps_primary_issue: bool = False,
    adds_issue: bool = False,
    objects: bool = False,
    overrides: bool = False,
) -> dict[str, Any]:
    """A critic FINAL result (issue #138) with the knobs the old merge rule
    keyed on. Every shape here is what `run_critic_pass` accepts: a
    disposition for each primary issue, keys unique across the response."""
    issues = []
    dispositions = []
    if keeps_primary_issue:
        issues.append(_issue("I1"))
        dispositions.append(
            {
                "issue_id": "I1",
                "disposition": "REVISE" if overrides else "KEEP",
                "reason": "Same issue.",
            }
        )
    added: list[dict[str, Any]] = []
    if adds_issue:
        issues.append(_issue("I2", topic="non-exclusive-arrangement", section="9"))
        added.append(_issue("I7", topic="non-exclusive-arrangement", section="9"))
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE" if issues else "ACCEPT",
        "confidence_state": confidence_state,
        "confidence_band": None if confidence_state == "OK" else confidence_state,
        "issues": issues,
        "critic_delta": {
            "dispositions": dispositions,
            "overrides": [
                {
                    "issue_id": "I1",
                    "field": "rationale",
                    "primary_value": "Cap must remain at $150,000.",
                    "critic_value": "The cap must stay at $150,000 with the carve-out.",
                    "reason": "The rationale omitted the carve-out.",
                }
            ]
            if overrides
            else [],
            "added_issues": added,
            "contested_replacements": [],
            "rationale_objections": [
                # `objection`, NOT `critic_objection` -- the schema's
                # CriticDelta.rationale_objections items are
                # {section_ref, objection}.
                {"section_ref": "8", "objection": "The rationale undersells the risk."}
            ]
            if objects
            else [],
        },
        "verdict_summary": "The critic's own summary.",
    }


# ---------------------------------------------------------------------------
# 1. The critic's band is final -- every (primary, critic) pair.
# ---------------------------------------------------------------------------


def test_the_critics_band_is_final_for_every_pair(failures: list[str]) -> None:
    for primary_state in _LEVELS:
        for critic_state in _LEVELS:
            result = recon.reconcile(
                primary_result=_primary(primary_state),
                critic_result=_critic(critic_state),
            )
            if result["confidence_state"] != critic_state:
                failures.append(
                    f"[1a] primary={primary_state} critic={critic_state}: expected the "
                    f"critic's own state; got {result['confidence_state']!r}"
                )
            expected_band = None if critic_state == "OK" else critic_state
            if result["confidence_band"] != expected_band:
                failures.append(
                    f"[1b] primary={primary_state} critic={critic_state}: confidence_band "
                    f"must mirror the final state ({expected_band!r}); got "
                    f"{result['confidence_band']!r}"
                )


# ---------------------------------------------------------------------------
# 2. The #265 triggers move nothing by themselves any more.
# ---------------------------------------------------------------------------


def test_disagreement_alone_does_not_degrade(failures: list[str]) -> None:
    cases = {
        "critic added issue": (_primary(), _critic(adds_issue=True)),
        "critic override": (
            _primary(with_issue=True),
            _critic(keeps_primary_issue=True, overrides=True),
        ),
        "rationale objection": (
            _primary(with_issue=True),
            _critic(keeps_primary_issue=True, objects=True),
        ),
    }
    for label, (primary, critic) in cases.items():
        result = recon.reconcile(primary_result=primary, critic_result=critic)
        if result["confidence_state"] != "OK" or result["confidence_band"] is not None:
            failures.append(
                f"[2a] {label}: a confident critic's disagreement must not degrade its own "
                f"band (the #265 rule is retired); got {result['confidence_state']!r} / "
                f"{result['confidence_band']!r}"
            )


def test_a_critic_may_report_a_better_band_than_the_primary(failures: list[str]) -> None:
    """The old rule was monotonic against the PRIMARY's band. Under ADR 0001
    the primary's band is no baseline: the critic, which has strictly more
    information, reports the final one."""
    result = recon.reconcile(
        primary_result=_primary("MANUAL_REVIEW_REQUIRED"),
        critic_result=_critic("OK"),
    )
    if result["confidence_state"] != "OK" or result["confidence_band"] is not None:
        failures.append(
            f"[3a] the critic's OK must stand over the primary's MANUAL_REVIEW_REQUIRED; got "
            f"{result['confidence_state']!r} / {result['confidence_band']!r}"
        )


# ---------------------------------------------------------------------------
# 7. Schema conformance of what `reconcile()` stamps.
# ---------------------------------------------------------------------------


def _detector_fire() -> dict[str, Any]:
    """A deterministic detector fire, appended verbatim into `issues`."""
    return {
        "section_ref": "12",
        "section_title": "Assignment",
        "counterparty_change_summary": "Assignment clause made freely assignable.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "Assignment requires prior written consent.",
        "proposed_replacement_text": "Neither party may assign without prior written consent.",
        "playbook_topic_id": "assignment-consent",
        "internal_precedent_citation": None,
        # Must match the schema's provenance pattern
        # ^(model|critic-added|detector:[a-z0-9][a-z0-9-]*)$
        "provenance": "detector:assignment-consent",
    }


def _without_pipeline_extensions(result: dict[str, Any]) -> dict[str, Any]:
    """`result` minus EXACTLY the pipeline-owned extensions issue #138 adds
    (docs/output-contract.md -> "The critic's final result"), so the rest of
    the object is still held to the schema it is stamped with:

      * an issue's `primary-retained` provenance (not a value any model may
        emit, so the model-facing pattern does not admit it);
      * the `source` tag on a critic-authored override;
      * the computed override entries (`source="computed"`), whose shape
        carries both versions of a whole issue.
    """
    stripped = copy.deepcopy(result)
    for issue in stripped.get("issues") or []:
        if issue.get("provenance") == recon.PROVENANCE_PRIMARY_RETAINED:
            issue["provenance"] = "model"
    delta = stripped.get("critic_delta")
    if isinstance(delta, dict):
        kept = []
        for entry in delta.get("overrides") or []:
            if entry.get("source") == recon.OVERRIDE_SOURCE_COMPUTED:
                continue
            entry.pop("source", None)
            kept.append(entry)
        delta["overrides"] = kept
    return stripped


def test_reconcile_output_validates_against_the_schema_it_stamps(failures: list[str]) -> None:
    schema = pp.load_output_schema()

    summary_at_max = _critic()
    summary_at_max["verdict_summary"] = "x" * 2000
    null_summary = _critic()
    null_summary["verdict_summary"] = None

    cases: list[tuple[str, dict[str, Any]]] = [
        ("critic accepts", {"primary_result": _primary(), "critic_result": _critic()}),
        (
            "critic added issue",
            {"primary_result": _primary(), "critic_result": _critic(adds_issue=True)},
        ),
        (
            "critic override",
            {
                "primary_result": _primary(with_issue=True),
                "critic_result": _critic(keeps_primary_issue=True, overrides=True),
            },
        ),
        (
            "critic rationale objection",
            {
                "primary_result": _primary(with_issue=True),
                "critic_result": _critic(keeps_primary_issue=True, objects=True),
            },
        ),
        (
            "detector fire",
            {
                "primary_result": _primary(),
                "critic_result": _critic(),
                "detector_fires": [_detector_fire()],
            },
        ),
        (
            "critic drops a hard rejection",
            {
                "primary_result": _primary(with_issue=True),
                "critic_result": {
                    **_critic(),
                    "critic_delta": {
                        "dispositions": [
                            {"issue_id": "I1", "disposition": "DROP", "reason": "Not needed."}
                        ],
                        "overrides": [],
                    },
                },
                "hard_rejection_rule_ids": {"limitation-of-liability"},
            },
        ),
        ("null verdict_summary", {"primary_result": _primary(), "critic_result": null_summary}),
        (
            "verdict_summary at the 2000-char maximum",
            {"primary_result": _primary(), "critic_result": summary_at_max},
        ),
    ]

    for label, kwargs in cases:
        result = recon.reconcile(**kwargs)
        if result.get("schema_version") != recon.SCHEMA_VERSION:
            failures.append(
                f"[7a] {label}: reconcile() stamped schema_version="
                f"{result.get('schema_version')!r}, expected {recon.SCHEMA_VERSION!r}"
            )
        try:
            pp.jsonschema.validate(instance=_without_pipeline_extensions(result), schema=schema)
        except pp.jsonschema.ValidationError as exc:
            stray = sorted(set(result) - set(schema.get("properties", {})))
            hint = f" (top-level keys not in the schema: {stray})" if stray else ""
            failures.append(
                f"[7b] {label}: reconcile()'s output, less the documented pipeline "
                f"extensions, must validate against the schema it stamps itself with "
                f"({pp.OUTPUT_SCHEMA_PATH.name}), got: {exc.message}{hint}"
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_the_critics_band_is_final_for_every_pair,
    test_disagreement_alone_does_not_degrade,
    test_a_critic_may_report_a_better_band_than_the_primary,
    test_reconcile_output_validates_against_the_schema_it_stamps,
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
    print("PASS: all reconciliation confidence (issue #265 retired by #138) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
