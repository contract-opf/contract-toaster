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
    (`unjudged` non-empty). This maps to the `ERROR`
    SYSTEM STATUS (docs/output-contract.md -> "The decision is binary;
    a run that does not complete is a failure") -- never a silent pass, and never
    itself a legal decision.
  - `floor_fires()` converts each violated verdict into the exact
    detector-fire shape `reconciliation.reconcile()` already consumes
    today (`scripts/review_spine.py::_issue_from_detector_fire` is the
    lexical-detector analogue), with `provenance="floor:<invariant_id>"`
    mirroring the existing `detector:<rule_id>` convention
    (`reconciliation.py` docstring).
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

WIRED (issue #479). This module is no longer standalone: for an OPF-governed
review, `scripts/review_spine.py::run_review` calls `judge_floor_invariants`
once per review as stage 3.5, between the critic pass and reconciliation, so
every `opf.floor.invariants` entry is judged exactly once. Every judge attempt
is ledgered through the `ledger_write` seam with `pass_name="floor"`, alongside
the primary and critic records. A `judgment.fail_closed` result terminates the
review ERROR / `floor_invariant_unjudged` rather than letting
an unevaluated invariant pass silently, and a `violation` verdict becomes a
monotonic `detector_fires` entry that `reconcile()` cannot downgrade.

TOLD WHO IT ACTS FOR (issue #679). A Floor invariant is frequently
DIRECTIONAL -- phrased in terms of what may or may not run in "the
counterparty's" favour -- and until #679 this function was given no party
identity at all, so it resolved that pronoun by guess. Measured live against
the real playbook (#679's own runs), it guessed BACKWARDS every time, firing
a signed monotonic Floor rule against our own principal.
`judge_floor_invariants(perspective_note=...)` now carries the resolved
identity into each per-invariant prompt, and a party-relative invariant with
NO resolvable identity fails closed into `unjudged` rather than being
guessed at.

GIVEN ROOM TO ANSWER (issue #682). The default `max_output_tokens` was
`1024`, sized from nothing and big enough only for the synthetic fixtures:
OpenRouter's `max_tokens` is a COMBINED reasoning+content ceiling, so on a
reasoning-class model that budget was spent thinking and every real document
died here with `finish_reason='length'`. `DEFAULT_MAX_OUTPUT_TOKENS` below is
derived instead -- the largest verdict `_validate_judge_response` accepts,
plus the reasoning allowance #677 measured -- so the judge survives even a
model whose policy entry declares no reasoning allowance at all. The same
issue closed the hole that failure exposed: a truncated judge now fails
CLOSED into `unjudged` (and is named in `FloorJudgment.truncated`) instead of
raising out of this module and killing the review.

Still out of scope (the remainder of issue #285's list): prompt-manifest
integration and any lexical detector change.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional  # noqa: UP035

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client as _model_client  # noqa: E402
import primary_review_pass as _primary_review_pass  # noqa: E402

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

{"invariant_id": "<the invariant_id you were given>", "violated": true|false, "evidence_quote": "<verbatim quote from REVIEW_CONTEXT, empty string when not violated>"}

Keep evidence_quote to about 200 characters -- the shortest excerpt that
shows the violation. Go longer only when the violation cannot be shown in
less, and never past 500 characters: a longer quote is rejected.
"""

# One bounded re-invoke per invariant on an invalid response (ticket AC),
# then fail closed for that invariant. Mirrors
# primary_review_pass.MAX_RETRIES_PER_PASS's "1 initial + 1 retry" shape.
_MAX_RETRIES_PER_INVARIANT = 1

# Owner decision 2026-10-16 (issue #160), after a live Claude Haiku 4.5 judge
# quoted 264 characters on every violated call against a hard 200: 200 stays
# the TARGET the system prompt asks for (`_EVIDENCE_QUOTE_TARGET_CHARS`), and
# 500 is the hard bound `_check_judge_response` enforces -- room to go longer
# when the violation needs it, about 2x the observed overshoot. Above 500 the
# verdict is still refused, never truncated -- and
# the retry now SAYS so, with the limit and the overshoot (`_correction_block`),
# because a model re-sent the identical prompt overshoots identically. The
# output budget below derives from this number, so it grows with it.
_EVIDENCE_QUOTE_MAX_CHARS = 500
_EVIDENCE_QUOTE_TARGET_CHARS = 200

# ---------------------------------------------------------------------------
# Output budget (issue #682)
# ---------------------------------------------------------------------------
# This function's default budget used to be a bare `1024` -- a number sized
# from nothing. It fit the synthetic fixtures, and the first REAL document
# broke it. OpenRouter's `max_tokens` is a COMBINED ceiling over a
# reasoning-class model's THINKING and its CONTENT (model-policy/
# openrouter.json's own "REASONING BUDGET" note; `model_client
# .OpenRouterModelClient.invoke` sends `max_tokens = max_output_tokens +
# reasoning_allowance`), so at 1024 the thinking ate the verdict: measured on
# the private corpus for issue #677 (landed as 41e0b4c), one real
# counterparty-paper agreement truncated 5 runs out of 5 here with
# `finish_reason='length'`, and completed 2 of 2 once ~4000 tokens of
# thinking room existed.
#
# #677's policy pin does not make this budget safe -- it only widens the
# ceiling for the models it pinned. `model-policy/openrouter.json` still
# ships `selectable` entries declaring `reasoning_max_tokens: 0`, and an
# operator who selects one of those gets `max_tokens == max_output_tokens`
# and the broken shape back. So the default is built here from the two
# quantities that are actually knowable:
#
#   CONTENT -- the judge emits exactly ONE small JSON verdict, and
#   `_validate_judge_response` below bounds it: an echoed `invariant_id`, a
#   bool, and an `evidence_quote` of at most `_EVIDENCE_QUOTE_MAX_CHARS`.
#   `_LARGEST_ACCEPTABLE_VERDICT` is the biggest response that validator will
#   accept; the content half is its measured size with 4x headroom.
#
#   REASONING -- `_MEASURED_REASONING_TOKENS`, the allowance #677 measured as
#   sufficient on a real document. It is folded into `max_output_tokens`
#   deliberately, so the judge survives a model that declares NO allowance.
#   A policy pin, where one exists, is ADDED on top by the client -- extra
#   headroom, never a substitute for this.
#
# Raising a CEILING is not spend. A provider bills the tokens it actually
# generates, and the verdict asked for here is the same few hundred
# characters it always was.

#: Generous upper bound on an OPF Floor invariant id (a rule-id-shaped
#: slug, e.g. "floor-no-uncapped-liability"), used only to size the largest
#: verdict the validator can accept. Not a limit imposed on callers.
_MAX_INVARIANT_ID_CHARS = 120

_LARGEST_ACCEPTABLE_VERDICT = json.dumps(
    {
        "invariant_id": "i" * _MAX_INVARIANT_ID_CHARS,
        "violated": True,
        "evidence_quote": "q" * _EVIDENCE_QUOTE_MAX_CHARS,
    }
)

#: The content half: the largest acceptable verdict, times four.
_VERDICT_CONTENT_TOKENS = 4 * _primary_review_pass.estimate_tokens(_LARGEST_ACCEPTABLE_VERDICT)

#: The reasoning half: the allowance issue #677 measured as sufficient for a
#: real document (0 of 5 completions without it, 2 of 2 with it).
_MEASURED_REASONING_TOKENS = 4000

#: Default `max_output_tokens` for `judge_floor_invariants`. Callers may
#: override it; nothing in the pipeline does today.
DEFAULT_MAX_OUTPUT_TOKENS = _VERDICT_CONTENT_TOKENS + _MEASURED_REASONING_TOKENS

# Issue #679: an invariant phrased relative to one side of the deal -- in
# terms of "the counterparty" -- cannot be judged by a judge that has not
# been told which party is which. Deliberately ONE term, matched as a
# case-insensitive substring of `statement`: "an invariant phrased in terms
# of 'the counterparty' is unjudgeable" is the ticket's own scoping, and
# widening it (e.g. to "our"/"we") would push party-neutral-in-practice
# invariants into the fail-closed path and change behavior for callers this
# issue is not about.
_PARTY_RELATIVE_TERM = "counterparty"


def _is_party_relative(statement: str) -> bool:
    """True when `statement` is phrased relative to a party, so it is
    unjudgeable without a resolved binding (issue #679)."""
    return _PARTY_RELATIVE_TERM in statement.lower()


def _build_user_prompt(
    *, invariant_id: str, statement: str, review_context: str, perspective_note: str = ""
) -> str:
    # Issue #679: the note goes FIRST, ahead of the invariant -- its closing
    # line disambiguates "the counterparty" for "the rule below", so it only
    # reads correctly when the rule follows it.
    prefix = f"{perspective_note}\n\n" if perspective_note else ""
    return (
        f"{prefix}"
        f"invariant_id: {invariant_id}\n"
        f"statement: {statement}\n"
        "\n"
        "<REVIEW_CONTEXT>\n"
        f"{review_context}\n"
        "</REVIEW_CONTEXT>\n"
    )


def _check_judge_response(raw_text: str, *, expected_invariant_id: str) -> tuple[dict[str, Any] | None, str]:
    """Parse + strictly validate one judge response for one invariant.

    Mirrors primary_review_pass.validate_model_response's strictness:
    reject non-JSON, wrong invariant_id, missing/non-bool violated. Also
    rejects a non-string or over-length evidence_quote (the
    `_EVIDENCE_QUOTE_MAX_CHARS` bound the system prompt states) -- never
    best-effort-patched.

    Returns `(verdict, "")` on success, `(None, error)` on any validation
    failure, where `error` follows the review passes' "TOKEN: detail"
    convention. The TOKEN is a closed vocabulary (`invalid_json`,
    `wrong_invariant_id`, `invalid_violated`, `invalid_evidence_quote`,
    `evidence_quote_too_long`) and is all the ledger ever records
    (`primary_review_pass._error_token`); the detail feeds only the retry's
    correction block, and carries numbers, never model or document text.

    A ```json fence or a prose preamble around the object is unwrapped first
    by the same `_extract_json_object` the review passes use -- unwrapping,
    not repair, so `json.loads` stays the sole judge of validity. Without it
    a model that fences its answer (Claude Haiku 4.5 did, on every call of a
    2026-10-06 live run) had every verdict refused and failed every review
    closed as `floor_invariant_unjudged`.

    Nothing but whitespace or the closing fence may FOLLOW the object. The
    unwrap keeps only the first balanced object, so a judge that answers
    `violated: false` and then takes it back ("Correction: {... true ...}")
    would otherwise pass as not violated -- failing OPEN on a gate nothing
    downstream can re-fire. Such a response stays refused, and unjudged.
    """
    if not isinstance(raw_text, str):
        return None, "invalid_json: the response was not text"
    body = _primary_review_pass._extract_json_object(raw_text)
    trailing = raw_text[raw_text.find(body) + len(body) :]
    if trailing.strip().strip("`").strip():
        return None, "invalid_json: text followed the JSON object"
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None, "invalid_json: the response was not a JSON object"
    if not isinstance(parsed, dict):
        return None, "invalid_json: the response was not a JSON object"
    if parsed.get("invariant_id") != expected_invariant_id:
        return None, "wrong_invariant_id: invariant_id must echo the one you were given"
    violated = parsed.get("violated")
    if not isinstance(violated, bool):
        return None, "invalid_violated: violated must be true or false"
    evidence_quote = parsed.get("evidence_quote", "")
    if not isinstance(evidence_quote, str):
        return None, "invalid_evidence_quote: evidence_quote must be a string"
    if len(evidence_quote) > _EVIDENCE_QUOTE_MAX_CHARS:
        over = len(evidence_quote) - _EVIDENCE_QUOTE_MAX_CHARS
        return None, (
            f"evidence_quote_too_long: evidence_quote is {len(evidence_quote)} characters; "
            f"the maximum is {_EVIDENCE_QUOTE_MAX_CHARS}, so it is {over} over"
        )
    return {
        "invariant_id": expected_invariant_id,
        "violated": violated,
        "evidence_quote": evidence_quote,
    }, ""


def _validate_judge_response(raw_text: str, *, expected_invariant_id: str) -> tuple[bool, dict[str, Any] | None]:
    """`(True, verdict)` / `(False, None)` -- `_check_judge_response` without
    the reason, for callers that only need the yes/no."""
    verdict, _error = _check_judge_response(raw_text, expected_invariant_id=expected_invariant_id)
    return verdict is not None, verdict


#: Heading of the correction appended to a retried judge prompt -- the review
#: passes' own (`primary_review_pass.RETRY_CORRECTION_HEADING`), so a reader of
#: any pass's retry sees the same marker.
_RETRY_CORRECTION_HEADING = _primary_review_pass.RETRY_CORRECTION_HEADING


def _correction_block(error: str) -> str:
    """The correction appended to the retry's user prompt (issue #160).

    "" for no error, so an attempt with nothing to correct is byte-identical
    to attempt 1. Appended to the USER prompt only, as the review passes do:
    the system prompt stays fixed.

    An over-length quote names the limit and the overshoot and asks for a
    shorter verbatim excerpt -- never a paraphrase, and never a different
    verdict: the fault is the quote's length, not the judgment. Every other
    fault restates the response shape.
    """
    if not error:
        return ""
    if error.startswith("evidence_quote_too_long"):
        fault = "Your verdict was rejected because evidence_quote is too long:"
        remedy = (
            f"Resend the SAME verdict -- same invariant_id, same violated -- with "
            f"evidence_quote cut to at most {_EVIDENCE_QUOTE_MAX_CHARS} characters "
            f"(aim for about {_EVIDENCE_QUOTE_TARGET_CHARS}), counting characters. "
            "Quote a shorter excerpt copied verbatim from "
            "REVIEW_CONTEXT: do not paraphrase, summarize or add ellipses, and do "
            "not change your judgment to make the quote fit."
        )
    else:
        fault = "Your previous response did not match the required response shape:"
        remedy = (
            "Resend your verdict in exactly the shape stated above. Only the "
            "shape was wrong -- do not change your judgment to accommodate it."
        )
    return (
        f"\n\n{_RETRY_CORRECTION_HEADING}\n"
        f"{fault}\n\n"
        f"  {error}\n\n"
        f"{remedy} Respond with a single raw JSON object and nothing else."
    )


@dataclass
class FloorJudgment:
    """Result of judging a set of Floor invariants against a review
    context.

    `verdicts`: one entry per invariant that produced a VALID verdict
    (within its retry budget), each `{invariant_id, violated,
    evidence_quote}`.
    `unjudged`: invariant ids that had no valid verdict after the one
    bounded retry -- the deterministic coverage gate.
    `truncated` (issue #682): the SUBSET of `unjudged` that ran out of
    output room (`model_client.ModelOutputTruncatedError`) rather than
    answering unreadably. Both fail closed identically -- this field changes
    no decision, it only tells the two diagnoses apart, because they lead an
    operator to different fixes: a budget/`reasoning_max_tokens` problem
    versus a model that answered and got the shape wrong. Not decoration:
    `review_spine.run_review` reads it to choose between its
    `REASON_FLOOR_INVARIANT_TRUNCATED` and `REASON_FLOOR_INVARIANT_UNJUDGED`
    terminals, which is what puts the right cause/fix copy in front of the
    reader -- "a retry will not help" for a deterministic budget failure
    instead of "it is worth submitting again".
    """

    verdicts: list[dict[str, Any]] = field(default_factory=list)
    unjudged: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)

    @property
    def fail_closed(self) -> bool:
        """True whenever ANY invariant could not be judged. This is the
        deterministic coverage gate (issue #285 AC): the caller must treat
        the review as fail-closed (maps to the ERROR
        system status, never silently passing an unjudged invariant)."""
        return bool(self.unjudged)


def judge_floor_invariants(
    *,
    invariants: list[dict[str, Any]],
    review_context: str,
    model_client: Any,
    model_id: str,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    review_id: str = "",
    ledger_write: Optional[Callable[["_model_client.ModelInvocationRecord"], None]] = None,  # noqa: UP037, UP045
    perspective_note: str = "",
) -> FloorJudgment:
    """Judge every Floor invariant against `review_context`, one
    `model_client.invoke()` call per invariant (plus one bounded retry for
    an invariant whose response fails validation).

    `invariants` is `opf.floor.invariants` (or a subset) -- each
    `{id, statement, rationale}` per the OPF v0.2 Floor shape. `rationale`
    is accepted but never sent to the model or echoed anywhere: the judge's
    task is scoped to `statement` only.

    `ledger_write` (issue #479, default `None` -> a no-op): every judge
    attempt -- success, retry, or terminal failure alike -- is ledgered via
    this seam, exactly like the primary/critic passes' `finally`-path
    ledgering (`primary_review_pass.run_primary_pass`,
    `critic_review_pass.run_critic_pass`). `pass_name="floor"` distinguishes
    these rows from `"primary"`/`"critic"`. `review_id` is required to
    build a `ModelInvocationRecord` when `ledger_write` is given; the
    default `""` keeps every existing direct caller (offline/unit tests
    that pass no `ledger_write`) unaffected.

    `perspective_note` (issue #679, default `""`): who this review acts
    for, resolved by the caller from the OPF document's `perspective` block
    (`scripts/review_spine.py::_floor_perspective_note` is the one producer
    in the pipeline). Prepended VERBATIM to every per-invariant user prompt,
    ahead of the invariant. A directional Floor invariant is phrased
    relative to a principal this function was never given -- it asks about
    "the counterparty" -- and measured live those invariants resolved
    that pronoun BACKWARDS -- firing a signed, monotonic, un-downgradable
    Floor rule against our own client. The judge resolves the pronoun itself
    once it is told who the parties are; there is no rule table here.

    FAIL CLOSED when it cannot be resolved: with NO `perspective_note`, an
    invariant whose `statement` is party-relative (`_is_party_relative`) is
    genuinely unjudgeable and goes straight to `unjudged` -- no model call,
    no guess. A wrong Floor is worse than an unjudged one, and the caller
    already knows how to stop (`fail_closed` ->
    `review_spine.REASON_FLOOR_INVARIANT_UNJUDGED`). A party-NEUTRAL
    invariant is judged exactly as before, so the default is a no-op for
    every caller whose invariants do not depend on a binding.

    `max_output_tokens` (issue #682, default `DEFAULT_MAX_OUTPUT_TOKENS`):
    see that constant's derivation above -- it is sized from the verdict
    contract this module enforces plus the reasoning need #677 measured on a
    real document, NOT from the round `1024` that shipped first and
    truncated every real review here. Still caller-overridable; nothing in
    the pipeline overrides it today.

    Returns a `FloorJudgment`. Never raises on a judge failure -- an
    invalid-after-retry invariant lands in `unjudged` (fail-closed),
    exactly like `primary_review_pass`'s bounded-retry-then-terminal
    pattern, just scoped per-invariant instead of per-pass. Issue #682 made
    that true of a TRUNCATED judge too (`model_client
    .ModelOutputTruncatedError`, which used to propagate out of here); it
    lands in `unjudged` and is additionally named in `truncated` so the
    "ran out of room" diagnosis is not lost. Every other
    `ModelInvocationError` still propagates -- a broken model seam is not a
    per-invariant judgment.
    """
    ledger_write = ledger_write or (lambda record: None)
    verdicts: list[dict[str, Any]] = []
    unjudged: list[str] = []
    truncated: list[str] = []

    for invariant in invariants:
        invariant_id = invariant["id"]
        statement = invariant["statement"]
        # Issue #679 fail-closed gate, BEFORE any spend: no resolved
        # binding + a party-relative statement == unjudgeable. Not judged
        # satisfied, not judged violated, and not one model call spent
        # guessing which side is which.
        if not perspective_note and _is_party_relative(statement):
            unjudged.append(invariant_id)
            continue
        user_prompt = _build_user_prompt(
            invariant_id=invariant_id,
            statement=statement,
            review_context=review_context,
            perspective_note=perspective_note,
        )

        verdict: dict[str, Any] | None = None
        # Issue #160: the previous attempt's validation error, fed back as a
        # correction on the retry and ledgered as a token on every attempt.
        last_error = ""
        # Issue #682: whether ANY attempt for THIS invariant ran out of
        # output room. Reset per invariant, and only consulted once the
        # attempt budget is spent -- an invariant that truncates and then
        # answers on its retry is judged, not labelled.
        ran_out_of_room = False
        attempts_allowed = 1 + _MAX_RETRIES_PER_INVARIANT
        for attempt in range(1, attempts_allowed + 1):
            raw_response = None
            outcome = "failure"
            # Issue #160: a retry carries the correction for the PREVIOUS
            # attempt's fault; attempt 1 (and a retry after a truncation,
            # which has no shape to correct) sends the base prompt unchanged.
            attempt_prompt = user_prompt + _correction_block(last_error)
            last_error = ""
            attempt_truncated = False
            # Issue #414: same timing/usage seam as run_primary_pass /
            # run_critic_pass (see those functions' identical comments) --
            # `attempt_duration_ms` is captured the instant invoke()
            # returns, before response validation, so it reflects the
            # provider round-trip rather than this loop's own bookkeeping;
            # `actual_usage` is only read when THIS attempt's invoke()
            # genuinely returned, never borrowed from a prior attempt.
            attempt_started_monotonic = time.monotonic()
            attempt_duration_ms: int | None = None
            try:
                raw_response = model_client.invoke(
                    model_id=model_id,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=attempt_prompt,
                    max_output_tokens=max_output_tokens,
                )
                attempt_duration_ms = int((time.monotonic() - attempt_started_monotonic) * 1000)
                parsed, last_error = _check_judge_response(
                    raw_response, expected_invariant_id=invariant_id
                )
                if parsed is not None:
                    outcome = "success"
                    verdict = parsed
                else:
                    outcome = "retry" if attempt < attempts_allowed else "failure"
            except _model_client.ModelOutputTruncatedError:
                # Issue #682: the provider stopped at `finish_reason='length'`
                # -- the judge was CUT OFF before it could write its verdict.
                # This used to propagate straight out of this function, past
                # `fail_closed` and past the caller's
                # `REASON_FLOOR_INVARIANT_UNJUDGED` terminal, killing the
                # review as an unhandled exception -- contradicting both this
                # module's and this function's documented "never raises on a
                # judge failure / an unjudgeable invariant lands in
                # `unjudged`" contract. It is now what it always claimed to
                # be: a judge failure, which fails CLOSED.
                #
                # Deliberately NARROW -- only this subclass. Every other
                # `ModelInvocationError` (throttling, an empty body, a
                # transport fault) still propagates, because those are not
                # "this invariant could not be judged", they are "the model
                # seam is broken" and the caller must see them as such.
                #
                # It spends its place in the SAME bounded retry budget as an
                # unreadable response -- no extra allowance, no widening.
                # Model output length is not deterministic, so the re-invoke
                # is a real chance rather than a replay, and the fix for a
                # judge that reliably outruns its ceiling is the sized
                # `DEFAULT_MAX_OUTPUT_TOKENS` above, not an unbounded loop.
                ran_out_of_room = True
                attempt_truncated = True
                outcome = "retry" if attempt < attempts_allowed else "failure"
            finally:
                # The token this attempt failed on, for the ledger only. A
                # truncation is ledgered but never fed back as a correction.
                attempt_error = last_error or (
                    "model_output_truncated" if attempt_truncated else ""
                )
                actual_usage = (
                    getattr(model_client, "last_usage", None) if raw_response is not None else None
                )
                # LEDGER every attempt -- success, retry, or terminal failure
                # alike -- mirroring the primary/critic passes' own
                # finally-path ledgering.
                ledger_write(
                    _model_client.ModelInvocationRecord(
                        review_id=review_id,
                        pass_name="floor",  # noqa: S106
                        model_id=model_id,
                        attempt_number=attempt,
                        outcome=outcome,
                        # Issue #144: the calibrated input estimate the two
                        # review passes use (no schema rides on this call).
                        input_tokens_est=_primary_review_pass.request_input_tokens_est(
                            _SYSTEM_PROMPT, attempt_prompt
                        ),
                        output_tokens_est=_primary_review_pass.estimate_tokens(raw_response or ""),
                        actual_input_tokens=(actual_usage or {}).get("input_tokens"),
                        actual_output_tokens=(actual_usage or {}).get("output_tokens"),
                        duration_ms=(
                            attempt_duration_ms
                            if attempt_duration_ms is not None
                            else int((time.monotonic() - attempt_started_monotonic) * 1000)
                        ),
                        # Issue #568 fix round 1: same seam as the primary/
                        # critic passes -- prompt-cache usage the provider
                        # reported for THIS attempt, if any, read off the
                        # SAME `actual_usage` dict already in hand above.
                        cache_read_input_tokens=(actual_usage or {}).get("cache_read_input_tokens"),
                        cache_creation_input_tokens=(actual_usage or {}).get(
                            "cache_creation_input_tokens"
                        ),
                        # Issue #661: same seam as the primary/critic passes
                        # -- the reasoning tokens the provider reported for
                        # THIS attempt, None (not 0) when it reported none.
                        reasoning_tokens=(actual_usage or {}).get("reasoning_tokens"),
                        # Issue #160: the closed-vocabulary TOKEN this attempt
                        # failed on (never its detail), and the same token as
                        # `retry_reason` on a retried attempt -- the #157
                        # convention the review passes already ledger.
                        error_token=(
                            "" if outcome == "success" else _primary_review_pass._error_token(attempt_error)
                        ),
                        retry_reason=_primary_review_pass.attempt_retry_reason(outcome, attempt_error),
                    )
                )
            if verdict is not None:
                break

        if verdict is None:
            unjudged.append(invariant_id)
            # Issue #682: `truncated` is a strict subset of `unjudged` --
            # recorded only for an invariant that ended up unjudged AND ran
            # out of room on the way, never for one that recovered.
            if ran_out_of_room:
                truncated.append(invariant_id)
        else:
            verdicts.append(verdict)

    return FloorJudgment(verdicts=verdicts, unjudged=unjudged, truncated=truncated)


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
    `_EVIDENCE_QUOTE_MAX_CHARS`, inside the output schema's 2000-char cap on
    that field, and drawn from the counterparty's own review_context, not
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
