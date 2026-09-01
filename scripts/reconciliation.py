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
  - **There is no size-based degrade.** Issue #625 (owner decision
    2026-08-25) deleted the heading-digest review mode: a document either
    fits `primary_review_pass.MAX_INPUT_TOKENS` and is reviewed in full, or
    the review fails loudly as `document_too_large` and never reaches this
    function at all. The confidence degrade that mode used to trigger here,
    and the fixed notice it appended to `verdict_summary`, are gone with it
    -- there is no longer a reduced review quality for them to warn about.

`reconcile()` is a pure function: no I/O, no model calls, deterministic
given its inputs -- so it is unit-testable as a table
(tests/test_critic_reconciliation_82.py) and reproducible/auditable in
production.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import primary_review_pass as _pp  # noqa: E402

# The envelope literal the merged result stamps itself with. Issue #627: read
# off the ACTIVE output-contract artifact (`primary_review_pass
# .OUTPUT_SCHEMA_VERSION` -> `playbooks/output-schema-v3.json`'s own
# `schema_version` const), NOT restated as a literal here.
#
# A hardcoded `"output-schema-v1"` was correct for as long as v1 and v2 shared
# a const, and became a silent contradiction the moment the active artifact
# bumped one: this module stamps an envelope claiming a contract, and the
# result it stamps is then read by surfaces that validate against the ACTIVE
# artifact. Two spellings of "which contract is this" is exactly the drift the
# cutover exists to remove, so there is now one.
SCHEMA_VERSION = _pp.OUTPUT_SCHEMA_VERSION

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


#: Format of a response-local issue handle, per
#: `playbooks/output-schema-v3.json`'s `IssueKey` (`^I[0-9]{1,4}$`).
_ISSUE_KEY_FORMAT = "I%d"


def _with_response_issue_key(
    issue: dict[str, Any], existing: list[dict[str, Any]]
) -> dict[str, Any]:
    """Give `issue` an `issue_key` that is unique against `existing` -- the
    issues already merged into this response (issue #627). Returns the same
    dict, mutated. A key that is already free stays untouched; a missing key
    and a COLLIDING key are both replaced with the lowest unused index.

    WHY THIS EXISTS. Under v3 every Issue REQUIRES an `issue_key`, mutually
    unique across the whole response. `primary_review_pass
    ._duplicate_issue_key_error` enforces that uniqueness WITHIN one model
    response -- it never sees a second one -- but `reconcile` below merges
    THREE producers into a single `issues` list, and downstream code keys
    that merged list (`redline_generate.generate_redline_from_blocks` builds
    `issues_by_key` and attributes every proven edit through it, last key
    wins). Uniqueness therefore has to be re-established against the merged
    result, and this is the only place that can see it.

    Both non-primary producers need it, for different reasons:

      - A deterministic fire (`floor_judge.floor_fires`, and the lexical-
        detector shape it mirrors) is built by CODE, from a verdict, with no
        model in the loop to name it. Appending one unkeyed would make the
        merged result stop conforming to the very contract this module
        stamps it with two lines later.
      - A CRITIC-added issue is model-authored and so arrives keyed -- but
        keyed in the critic's OWN response, where the primary's keys were
        not in scope. Both passes are told by the same
        `primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK` to number issues
        "I1", "I2", "I3", ..., so a critic that adds its first issue emits
        "I1" -- the key the primary's first issue almost always has.
        Un-rekeyed, that collision silently re-attributes the primary's
        proven block edits (and the counterparty-facing footnote they carry)
        to the critic's unrelated issue.

    Only the LATER producer is ever re-keyed: the primary's keys are the
    ones `block_patches`/`block_ops` segments name, so moving one would
    orphan a proven edit. Lowest unused index, so the assignment is
    deterministic and reproducible for the same inputs -- the same property
    every other merge decision in this module has.
    """
    taken = {
        other.get("issue_key") for other in existing if other is not issue
    }
    current = issue.get("issue_key")
    if current and current not in taken:
        return issue
    index = 1
    while _ISSUE_KEY_FORMAT % index in taken:
        index += 1
    issue["issue_key"] = _ISSUE_KEY_FORMAT % index
    return issue


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

    Returns a merged `output-schema-v1`-shaped dict, plus (issue #626) the
    primary pass's own top-level `block_patches`/`block_ops` when it carries
    them -- see the comment above the return statement for why they are
    forwarded rather than merged, and why only the primary's.
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
            # Re-key against the MERGED result, not against the critic's own
            # response -- see `_with_response_issue_key`. The critic numbers
            # its issues from "I1" in a response where the primary's keys
            # were not in scope, so its first added issue routinely collides
            # with the primary's first, and `issues_by_key` downstream is
            # last-wins. `attributed` is the SAME object that goes into
            # `critic_delta_record` below, so mutating it here fixes the
            # audit record too. Issues already merged AND ones only recorded
            # under `critic_delta` both count as taken: v3 requires the keys
            # to be mutually unique across the whole response.
            _with_response_issue_key(
                attributed, final_issues + critic_delta_record["added_issues"]
            )
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
            final_issues.append(_with_response_issue_key(dict(fire), final_issues))
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

    verdict_summary = primary_result.get("verdict_summary")

    confidence_band = None if confidence_state == "OK" else confidence_state

    # Issue #626: carry the v3 block transcript through to the redline
    # stage. `block_patches` / `block_ops` are top-level, response-scoped
    # edit carriers (`playbooks/output-schema-v3.json`), not per-issue
    # fields, so nothing above merges them -- they are forwarded verbatim
    # from the PRIMARY pass, and only when that pass actually carries them.
    # A v1/v2 response has neither key, so this dict is byte-identical to
    # what it was before issue #626 for a caller still on that contract; a
    # v3 response (the active contract since issue #627) carries them.
    #
    # The CRITIC's own transcript is deliberately not merged here. Two
    # transcripts naming one `block_id` cannot both be proven against that
    # block without merging them into a single ordered segment list, which
    # is a repair -- exactly what `block_transcript.validate_block_patches`
    # refuses (`duplicate_block_id`). A critic-added issue with no edits of
    # its own therefore reaches the redline stage as an ordinary flag-only
    # issue; giving the critic a way to author block edits belongs to the
    # flip ticket, together with the prompt that would ask it for them.
    block_carriers = {
        key: primary_result[key]
        for key in ("block_patches", "block_ops")
        if primary_result.get(key)
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "confidence_state": confidence_state,
        "confidence_band": confidence_band,
        "issues": final_issues,
        "critic_delta": critic_delta_record if has_critic_delta else None,
        "verdict_summary": verdict_summary,
        **block_carriers,
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
