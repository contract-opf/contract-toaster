#!/usr/bin/env python3
"""
FloorJudge (issue #285): runtime enforcement of the OPF v0.2 Floor.

The OPF v0.2 Floor (`opf.floor.invariants`, each `{id, statement,
rationale}` -- see `tests/fixtures/opf/synthetic-eiaa.opf.json`, #283) is
**judged NL invariants**: there is no lexical detector grammar for it in
the shipped engine schema (unlike `hard_rejections` rules, which
`scripts/review_spine.py::run_detectors_on_hunks` matches deterministically
by pattern). A Floor invariant can only be evaluated by asking a model
whether its `statement` is violated by the material under review --
exactly the "FloorJudge" pattern from the engine's own
`opf-engine`#151/#158.

This module gives that judgment a deterministic, offline, fail-closed
runtime seam:

  - `judge_floor_invariants()` invokes the injected `model_client
    .BedrockModelClient` (ordinarily `FakeBedrockClient`,
    `backend/src/model_client.py`) once per invariant, with a FIXED system
    prompt (identical across every invariant and every call -- only the
    user prompt varies) asking the model to decide, and ONLY decide,
    whether the invariant is violated. The response must be strict JSON;
    parsing/validation mirrors `primary_review_pass.validate_model_response`'s
    strictness (reject non-JSON, wrong `invariant_id`, missing/non-bool
    `violated`). Exactly one bounded re-invoke is allowed per invariant on
    an invalid response, then that invariant fails closed.
  - `FloorJudgment.fail_closed` is the deterministic coverage gate: True
    whenever ANY invariant has no valid verdict after its retry
    (`unjudged` non-empty). This maps to the `MANUAL_REVIEW_REQUIRED`
    SYSTEM STATUS (docs/output-contract.md -> "The decision is binary;
    uncertainty is a system status") -- never a silent pass, and never
    itself a legal decision.
  - `floor_fires()` converts each violated verdict into the exact
    detector-fire shape `reconciliation.reconcile()` already consumes
    today (`scripts/review_spine.py::_issue_from_detector_fire` is the
    lexical-detector analogue), with `provenance="floor:<invariant_id>"`
    mirroring the existing `detector:<rule_id>` convention
    (`reconciliation.py` docstring, `scripts/third_party_output_integration.py`).
    `reconcile()` treats every `detector_fires` entry as monotonic --
    unconditionally appended and forcing `decision="REQUEST_CHANGE"` --
    so a Floor fire has exactly the same "cannot be downgraded by either
    model pass" guarantee a lexical detector fire has. This module makes
    NO change to `reconcile()` itself: the shape is drop-in.

NO document substance or invariant text in logs/exceptions: only
`invariant_id` (a rule-id-shaped string, e.g. "floor-no-uncapped-
liability") ever appears in a log line or an exception message here --
never `statement`, `rationale`, `review_context`, or a raw model response
body, all of which may carry confidential contract or playbook substance
(same discipline as `model_client.ModelInvocationError` and
`scripts/review_spine.py::_issue_from_detector_fire`'s rule-id-free
human-surfaced fields).

MOCKED-MODEL, offline, deterministic (issue #81's owner-approved scope,
extended to this module): driven entirely by an injected
`model_client.BedrockModelClient`. No live Bedrock, no network.

Out of scope for this slice (see issue #285 "Out of scope"): wiring this
module into the pipeline/spine or `run_two_pass_review` (lands with the
v2-bundle spine work), prompt-manifest/ledger integration, any lexical
detector change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Fixed across every invariant and every call -- the ONLY thing that varies
# per invoke() is the user prompt (invariant id/statement + review_context).
# The judge's task is deliberately narrow: decide violated/not-violated for
# ONE invariant, nothing else -- no redline proposal, no rationale beyond a
# short evidence quote.
_SYSTEM_PROMPT = """You are a Floor-invariant judge for a contract review system.

Your ONLY task is to decide whether the invariant given to you in the user
message (its "statement") is violated by the material in the
REVIEW_CONTEXT block of the user message. You do not review anything else
about the document, and you do not propose replacement language.

Respond with STRICT JSON ONLY -- no prose, no markdown fencing -- in
exactly this shape:

{"invariant_id": "<the invariant_id you were given>", "violated": true|false, "evidence_quote": "<short quote from REVIEW_CONTEXT, <=200 chars, empty string when not violated>"}
"""

# One bounded re-invoke per invariant on an invalid response (ticket AC),
# then fail closed for that invariant. Mirrors
# primary_review_pass.MAX_RETRIES_PER_PASS's "1 initial + 1 retry" shape.
_MAX_RETRIES_PER_INVARIANT = 1

_EVIDENCE_QUOTE_MAX_CHARS = 200


def _build_user_prompt(*, invariant_id: str, statement: str, review_context: str) -> str:
    return (
        f"invariant_id: {invariant_id}\n"
        f"statement: {statement}\n"
        "\n"
        "<REVIEW_CONTEXT>\n"
        f"{review_context}\n"
        "</REVIEW_CONTEXT>\n"
    )


def _validate_judge_response(raw_text: str, *, expected_invariant_id: str) -> tuple[bool, dict[str, Any] | None]:
    """Parse + strictly validate one judge response for one invariant.

    Mirrors primary_review_pass.validate_model_response's strictness:
    reject non-JSON, wrong invariant_id, missing/non-bool violated. Also
    rejects a non-string or over-length evidence_quote (the ">200 chars"
    bound the system prompt asks for) -- never best-effort-patched.

    Returns (True, {"invariant_id", "violated", "evidence_quote"}) on
    success, (False, None) on any validation failure.
    """
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return False, None
    if not isinstance(parsed, dict):
        return False, None
    if parsed.get("invariant_id") != expected_invariant_id:
        return False, None
    violated = parsed.get("violated")
    if not isinstance(violated, bool):
        return False, None
    evidence_quote = parsed.get("evidence_quote", "")
    if not isinstance(evidence_quote, str) or len(evidence_quote) > _EVIDENCE_QUOTE_MAX_CHARS:
        return False, None
    return True, {
        "invariant_id": expected_invariant_id,
        "violated": violated,
        "evidence_quote": evidence_quote,
    }


@dataclass
class FloorJudgment:
    """Result of judging a set of Floor invariants against a review
    context.

    `verdicts`: one entry per invariant that produced a VALID verdict
    (within its retry budget), each `{invariant_id, violated,
    evidence_quote}`.
    `unjudged`: invariant ids that had no valid verdict after the one
    bounded retry -- the deterministic coverage gate.
    """

    verdicts: list[dict[str, Any]] = field(default_factory=list)
    unjudged: list[str] = field(default_factory=list)

    @property
    def fail_closed(self) -> bool:
        """True whenever ANY invariant could not be judged. This is the
        deterministic coverage gate (issue #285 AC): the caller must treat
        the review as fail-closed (maps to the MANUAL_REVIEW_REQUIRED
        system status, never silently passing an unjudged invariant)."""
        return bool(self.unjudged)


def judge_floor_invariants(
    *,
    invariants: list[dict[str, Any]],
    review_context: str,
    model_client: Any,
    model_id: str,
    max_output_tokens: int = 1024,
) -> FloorJudgment:
    """Judge every Floor invariant against `review_context`, one
    `model_client.invoke()` call per invariant (plus one bounded retry for
    an invariant whose response fails validation).

    `invariants` is `opf.floor.invariants` (or a subset) -- each
    `{id, statement, rationale}` per the OPF v0.2 Floor shape. `rationale`
    is accepted but never sent to the model or echoed anywhere: the judge's
    task is scoped to `statement` only.

    Returns a `FloorJudgment`. Never raises on a judge failure -- an
    invalid-after-retry invariant lands in `unjudged` (fail-closed),
    exactly like `primary_review_pass`'s bounded-retry-then-terminal
    pattern, just scoped per-invariant instead of per-pass.
    """
    verdicts: list[dict[str, Any]] = []
    unjudged: list[str] = []

    for invariant in invariants:
        invariant_id = invariant["id"]
        statement = invariant["statement"]
        user_prompt = _build_user_prompt(
            invariant_id=invariant_id, statement=statement, review_context=review_context
        )

        verdict: dict[str, Any] | None = None
        attempts_allowed = 1 + _MAX_RETRIES_PER_INVARIANT
        for _attempt in range(1, attempts_allowed + 1):
            raw_response = model_client.invoke(
                model_id=model_id,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_output_tokens=max_output_tokens,
            )
            is_valid, parsed = _validate_judge_response(
                raw_response, expected_invariant_id=invariant_id
            )
            if is_valid:
                verdict = parsed
                break

        if verdict is None:
            unjudged.append(invariant_id)
        else:
            verdicts.append(verdict)

    return FloorJudgment(verdicts=verdicts, unjudged=unjudged)


def floor_fires(judgment: FloorJudgment) -> list[dict[str, Any]]:
    """Convert each VIOLATED verdict in `judgment` into the exact
    detector-fire shape `reconciliation.reconcile()`'s `detector_fires`
    parameter consumes today (output-schema-v1 Issue shape), with
    `provenance="floor:<invariant_id>"` mirroring the `detector:<rule_id>`
    convention (`scripts/review_spine.py::_issue_from_detector_fire`).

    An unjudged invariant (in `judgment.unjudged`) never fires here --
    `judgment.fail_closed` is how the caller learns about it instead; a
    fire is only ever produced from a VALID, VIOLATED verdict, never from
    silence or ambiguity.

    Human-surfaced fields are deliberately generic (no invariant statement/
    rationale text), matching the lexical-detector fire's own discipline
    that rule text is confidential internal reasoning and must never be
    echoed into an external-facing field -- `provenance` alone carries the
    invariant id as system metadata. `evidence_quote` (already bounded to
    <=200 chars and drawn from the counterparty's own review_context, not
    from internal reasoning) is used for `counterparty_change_summary` when
    present.
    """
    fires: list[dict[str, Any]] = []
    for verdict in judgment.verdicts:
        if not verdict.get("violated"):
            continue
        invariant_id = verdict["invariant_id"]
        evidence_quote = verdict.get("evidence_quote") or ""
        fires.append(
            {
                "section_ref": invariant_id,
                "section_title": "Floor invariant",
                "counterparty_change_summary": (
                    evidence_quote
                    if evidence_quote
                    else (
                        "A Floor invariant governing this agreement was "
                        "judged violated by the counterparty draft."
                    )
                ),
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": (
                    "This agreement must satisfy the Floor invariant "
                    "governing this position."
                ),
                "proposed_replacement_text": "",
                "playbook_topic_id": invariant_id,
                "internal_precedent_citation": None,
                "provenance": f"floor:{invariant_id}",
            }
        )
    return fires
