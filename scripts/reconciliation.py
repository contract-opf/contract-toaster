#!/usr/bin/env python3
"""
Deterministic reconciliation (issue #82): merges the primary pass's output,
the adversarial critic pass's output, and any deterministic detector fires
into the final review result -- by CODE, not by a third model call, so the
outcome is reproducible and auditable.

Implements ARCHITECTURE.md -> "Two-pass review" -> "Deterministic
reconciliation":

  - **Hard rejections are monotonic.** Any hard rejection raised by either
    pass (or a deterministic detector fire) forces the overall decision to
    REQUEST_CHANGE. The critic cannot downgrade a hard rejection the
    primary (or a detector) found, and vice-versa.
  - **The critic adds, it does not silently rewrite.** The critic may add
    issues (provenance="critic-added", appended to the final `issues` list
    per docs/output-contract.md -> "Critic-delta presentation" ->
    "Critic-added issue attribution") and may flag the primary's
    `proposed_replacement_text` as drifting, but it may NOT silently
    overwrite the primary's replacement text. A contested replacement is
    recorded under `critic_delta.contested_replacements` -- the primary
    issue's `proposed_replacement_text` is never mutated.
  - **Deltas are preserved.** The final result retains both the primary
    output (as the base `issues` list) and the critic's deltas (added
    issues, contested replacements, rationale objections) under the
    top-level `critic_delta` key, in the shape
    docs/output-contract.md -> "Critic-delta presentation" and
    playbooks/output-schema-v1.json's `CriticDelta` definition require for
    the result-view UI (issue #36).
  - **Critic disagreement degrades the confidence band.** A contested
    replacement or a critic-added issue moves `confidence_state` (and its
    mirrored `confidence_band`) one level toward
    `ERROR_MANUAL_REVIEW_REQUIRED`, per docs/output-contract.md ->
    "Confidence band" -> "Critic-delta confidence merge rule" (issue #265).
    The merge is monotonic: the critic can only degrade the band, never
    raise it back toward `OK`. A rationale objection alone does not trigger
    this degradation.

`reconcile()` is a pure function: no I/O, no model calls, deterministic
given its inputs -- so it is unit-testable as a table
(tests/test_critic_reconciliation_82.py) and reproducible/auditable in
production.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "output-schema-v1"

# CriticDelta sub-keys, per playbooks/output-schema-v1.json definitions.CriticDelta.
_CRITIC_DELTA_KEYS = ("added_issues", "contested_replacements", "rationale_objections")

# confidence_state levels, least to most degraded. Per docs/output-contract.md
# -> "Confidence band" -> "Critic-delta confidence merge rule" (issue #265):
# the critic can move confidence_state down this list (never up).
_CONFIDENCE_LEVELS = (
    "OK",
    "LOW_CONFIDENCE",
    "MANUAL_REVIEW_REQUIRED",
    "ERROR_MANUAL_REVIEW_REQUIRED",
)


def _degrade_confidence_state(confidence_state: str) -> str:
    """Move `confidence_state` one level down `_CONFIDENCE_LEVELS` (toward
    ERROR_MANUAL_REVIEW_REQUIRED), capped at the worst level. An unrecognized
    input is treated as the best level (OK) before degrading, so the result
    is always a valid, more-degraded state."""
    try:
        index = _CONFIDENCE_LEVELS.index(confidence_state)
    except ValueError:
        index = 0
    index = min(index + 1, len(_CONFIDENCE_LEVELS) - 1)
    return _CONFIDENCE_LEVELS[index]


def _issue_key(issue: dict[str, Any]) -> tuple[Any, Any]:
    """Dedupe key for an issue: (playbook_topic_id, section_ref). Used so a
    detector fire that a model pass ALSO happened to report is not
    double-appended to the final issues list."""
    return (issue.get("playbook_topic_id"), issue.get("section_ref"))


def reconcile(
    *,
    primary_result: dict[str, Any],
    critic_result: dict[str, Any] | None = None,
    detector_fires: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministically merge the primary pass output, the critic pass
    output, and deterministic detector fires into the final review result.

    `primary_result` / `critic_result` are the schema-valid, parsed
    `output-schema-v1` response bodies returned by
    `primary_review_pass.run_primary_pass` / `critic_review_pass.run_critic_pass`
    (the `"response"` key of a `status="OK"` result) -- NOT the raw
    orchestration-status wrapper. `critic_result` is `None` when no critic
    pass ran (never call this with a failed/`ERROR_MANUAL_REVIEW_REQUIRED`
    critic pass -- ARCHITECTURE.md's "never a silent single-pass DONE" rule
    means the caller must not reconcile in that case at all).

    `detector_fires` are deterministic hard-rejection issues produced by
    the lexical hard-rejection detector layer (data-flow step 13) --
    `Issue`-shaped dicts with `provenance="detector:<rule_id>"`. They are
    monotonic: appended to the final issues list and force
    `decision="REQUEST_CHANGE"` regardless of what either model pass
    concluded, and regardless of ordering (both-models-silent is the
    common case -- detectors are deterministic pre-model-call checks the
    models are not guaranteed to also restate).

    Returns a merged `output-schema-v1`-shaped dict.
    """
    detector_fires = detector_fires or []

    final_issues: list[dict[str, Any]] = [dict(issue) for issue in primary_result.get("issues", [])]
    seen_keys = {_issue_key(issue) for issue in final_issues}

    critic_delta_record: dict[str, list[Any]] = {key: [] for key in _CRITIC_DELTA_KEYS}
    critic_decision: str | None = None

    if critic_result is not None:
        critic_decision = critic_result.get("decision")
        raw_delta = critic_result.get("critic_delta") or {}

        # The critic adds, it does not silently rewrite: added issues are
        # appended to the final issues list with attribution enforced by
        # this pipeline code (never trusted verbatim from model output),
        # and also preserved under critic_delta for the #36 UI/audit shape.
        for issue in raw_delta.get("added_issues", []):
            attributed = dict(issue)
            attributed["provenance"] = "critic-added"
            key = _issue_key(attributed)
            if key not in seen_keys:
                final_issues.append(attributed)
                seen_keys.add(key)
            critic_delta_record["added_issues"].append(attributed)

        # Contested replacements are recorded ONLY here -- the matching
        # primary issue's proposed_replacement_text is never mutated.
        critic_delta_record["contested_replacements"] = [
            dict(item) for item in raw_delta.get("contested_replacements", [])
        ]
        critic_delta_record["rationale_objections"] = [
            dict(item) for item in raw_delta.get("rationale_objections", [])
        ]

    # Deterministic detector fires: monotonic, appended if not already
    # present, regardless of what either model pass said (or didn't say).
    for fire in detector_fires:
        key = _issue_key(fire)
        if key not in seen_keys:
            final_issues.append(dict(fire))
            seen_keys.add(key)

    has_critic_delta = any(critic_delta_record[key] for key in _CRITIC_DELTA_KEYS)

    # Hard rejections are monotonic: any REQUEST_CHANGE signal from either
    # pass, or any issue surviving into the final list (primary, critic-
    # added, or detector fire), forces REQUEST_CHANGE. Nothing downgrades
    # it -- a critic ACCEPT (or a primary ACCEPT) can never win against a
    # detector fire or the other pass's REQUEST_CHANGE.
    decision = "REQUEST_CHANGE" if (
        primary_result.get("decision") == "REQUEST_CHANGE"
        or critic_decision == "REQUEST_CHANGE"
        or final_issues
    ) else "ACCEPT"

    # Critic-delta confidence merge (issue #265): a contested replacement or
    # a critic-added issue means the critic disagreed with the primary pass,
    # so the confidence band shown pre-download (docs/output-contract.md ->
    # "Confidence band") must not misrepresent the review as fully
    # confident. Degrade confidence_state (and its mirrored confidence_band)
    # one level below the primary's own confidence_state. A rationale
    # objection alone does not contest a replacement or add an issue, so it
    # does not trigger this degradation. The rule is monotonic -- the critic
    # can only move confidence_state toward ERROR_MANUAL_REVIEW_REQUIRED,
    # never back toward OK.
    critic_contests_output = bool(critic_delta_record["added_issues"]) or bool(
        critic_delta_record["contested_replacements"]
    )
    primary_confidence_state = primary_result.get("confidence_state", "OK")
    confidence_state = (
        _degrade_confidence_state(primary_confidence_state)
        if critic_contests_output
        else primary_confidence_state
    )
    confidence_band = None if confidence_state == "OK" else confidence_state

    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "confidence_state": confidence_state,
        "confidence_band": confidence_band,
        "issues": final_issues,
        "critic_delta": critic_delta_record if has_critic_delta else None,
        "verdict_summary": primary_result.get("verdict_summary"),
    }


def run_two_pass_review(
    *,
    primary_pass_result: dict[str, Any],
    critic_pass_result: dict[str, Any] | None,
    detector_fires: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compose the primary-pass and critic-pass orchestration results (the
    `{"status": ..., "response": ...}` dicts returned by
    `primary_review_pass.run_primary_pass` / `critic_review_pass.run_critic_pass`)
    into a single terminal outcome, enforcing ARCHITECTURE.md's
    "Critic-pass failure is terminal -- never a silent single-pass DONE"
    rule.

    Returns one of:
      {"status": "MANUAL_REVIEW_REQUIRED" | "ERROR_MANUAL_REVIEW_REQUIRED", ...}
        -- the primary pass failed (propagated verbatim; the critic is
        never invoked in this slice's contract, mirroring
        run_primary_pass's own oversized-doc short-circuit).
      {"status": "ERROR_MANUAL_REVIEW_REQUIRED", "stage": "critic", ...}
        -- the primary pass succeeded but the critic pass did not (after
        its own bounded retry). The primary's schema-valid output is
        DELIBERATELY NOT reconciled/returned as a result here -- surfacing
        it would be exactly the silent single-pass DONE this rule forbids.
      {"status": "OK", "result": {...}}
        -- both passes succeeded; `result` is `reconcile()`'s merged
        output-schema-v1-shaped dict.
    """
    if primary_pass_result.get("status") != "OK":
        return dict(primary_pass_result)

    if critic_pass_result is None or critic_pass_result.get("status") != "OK":
        return {
            "status": "ERROR_MANUAL_REVIEW_REQUIRED",
            "stage": "critic",
            "attempts": (critic_pass_result or {}).get("attempts"),
            "last_error": (critic_pass_result or {}).get("last_error"),
        }

    reconciled = reconcile(
        primary_result=primary_pass_result["response"],
        critic_result=critic_pass_result["response"],
        detector_fires=detector_fires,
    )
    return {"status": "OK", "result": reconciled}
