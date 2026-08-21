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
  - **Outline-only input degrades the confidence band too.** When the
    primary pass reviewed a section outline rather than the full document
    text (`input_mode="section_outline"`, issue #419 -- the document was
    over `primary_review_pass.DEFAULT_FULL_DOC_TOKEN_THRESHOLD`),
    `confidence_state` is degraded one FURTHER level (stacking with any
    critic-disagreement degrade above) and a fixed, substance-free sentence
    is appended to `verdict_summary` saying so -- see
    `OUTLINE_MODE_SUMMARY_NOTICE` below. A model reviewing a table of
    contents and returning a confident-looking decision is exactly the
    silent-degrade this issue exists to end.

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

# ---------------------------------------------------------------------------
# Outline-only input degrade (issue #419). `input_mode` is
# `primary_review_pass.run_primary_pass`'s own observability field -- see
# that module's `resolve_input_mode` / `INPUT_MODE_SECTION_OUTLINE`. The
# literal below is a DUPLICATE of that constant, not an import: this module
# is deliberately dependency-free (no I/O, no model calls, no cross-module
# imports -- see the module docstring), the same "each module owns its own
# copy of small shared sentinels" convention primary_review_pass.py's own
# MAX_INPUT_TOKENS comment documents. tests/test_full_doc_threshold.py
# cross-checks the two literals so they cannot silently drift.
INPUT_MODE_SECTION_OUTLINE = "section_outline"

# The fixed, substance-free sentence appended to `verdict_summary` whenever
# `input_mode == INPUT_MODE_SECTION_OUTLINE` (issue #419 AC: "a fixed
# sentence... contains no document content"). Static string only -- never
# interpolated with anything document-derived, so it needs no leakage-scan
# consideration beyond what verdict_summary already gets.
# Release voicing: "your document" phrasing, no internal-org name, no
# jargon (project de-brand rule; see docs -> "Release voicing").
OUTLINE_MODE_SUMMARY_NOTICE = (
    "Your document was too large for a full-text review, so this result is "
    "based on a section outline rather than the full document text -- "
    "treat it with extra caution."
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
    input_mode: str = "full_document",
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

    `input_mode` (issue #419, default `"full_document"`): the ORCHESTRATION
    wrapper's own field (`primary_pass_result["input_mode"]` -- NOT part of
    `primary_result`/the schema-valid response dict itself, since it is
    pipeline-derived metadata the model never emits). `run_two_pass_review`
    below reads it off `primary_pass_result` and passes it straight through.
    When it equals `INPUT_MODE_SECTION_OUTLINE`, `confidence_state` (and its
    mirrored `confidence_band`) is degraded one FURTHER level beyond
    whatever the critic-delta merge above already produced, and
    `OUTLINE_MODE_SUMMARY_NOTICE` is appended to `verdict_summary` -- see
    the module docstring's "Outline-only input degrades the confidence band
    too" bullet. The default reproduces pre-#419 behavior exactly.

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

    # Outline-only input degrade (issue #419): a SEPARATE, independent
    # degrade from the critic-delta one above -- the two stack (a review
    # that is both outline-only AND critic-contested is worse than either
    # alone) -- applied last so it always reflects the critic-merged state,
    # never gets silently overwritten by it.
    verdict_summary = primary_result.get("verdict_summary")
    is_outline_only = input_mode == INPUT_MODE_SECTION_OUTLINE
    if is_outline_only:
        confidence_state = _degrade_confidence_state(confidence_state)
        if verdict_summary:
            # Bound the merged string to the schema's 2000-char maximum
            # (playbooks/output-schema-v1.json / -v2.json's
            # `verdict_summary.oneOf[1].maxLength`) -- a schema-valid
            # <=2000-char model summary must not become a >2000-char merged
            # one just because this notice got appended. OUTLINE_MODE_
            # SUMMARY_NOTICE is the load-bearing user signal here, so it is
            # NEVER truncated; the model's own summary is elided instead to
            # make room for the separator + fixed notice.
            _separator = "\n\n"
            _max_model_summary_len = 2000 - len(_separator) - len(OUTLINE_MODE_SUMMARY_NOTICE)
            if len(verdict_summary) > _max_model_summary_len:
                _ellipsis = "..."
                verdict_summary = verdict_summary[: _max_model_summary_len - len(_ellipsis)] + _ellipsis
            verdict_summary = f"{verdict_summary}{_separator}{OUTLINE_MODE_SUMMARY_NOTICE}"
        else:
            verdict_summary = OUTLINE_MODE_SUMMARY_NOTICE

    confidence_band = None if confidence_state == "OK" else confidence_state

    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "confidence_state": confidence_state,
        "confidence_band": confidence_band,
        "issues": final_issues,
        "critic_delta": critic_delta_record if has_critic_delta else None,
        "verdict_summary": verdict_summary,
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

    `primary_pass_result["input_mode"]` (issue #419, absent on a pre-#419
    caller/fixture) is read here and passed to `reconcile()` -- see that
    function's own `input_mode` docstring. Absent defaults to
    `"full_document"`, reproducing pre-#419 behavior exactly.
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
        input_mode=primary_pass_result.get("input_mode", "full_document"),
    )
    return {"status": "OK", "result": reconciled}
