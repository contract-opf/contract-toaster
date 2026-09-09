#!/usr/bin/env python3
"""
Slice test for issue #682: "the Floor judge's 1024-token output budget cannot
survive a reasoning-class model -- every real document truncated".

## Root problem this proves fixed

`scripts/floor_judge.py::judge_floor_invariants` shipped with
`max_output_tokens: int = 1024`, a number sized from nothing: it fit the
synthetic fixtures and the first REAL document broke it. OpenRouter's
`max_tokens` is a COMBINED ceiling over a reasoning-class model's thinking
AND its content (`model-policy/openrouter.json`'s own REASONING BUDGET note,
and `backend/src/model_client.py::OpenRouterModelClient.invoke`, which sends
`max_tokens = max_output_tokens + reasoning_allowance`). Measured on the
private corpus for #677 (landed as 41e0b4c), one real counterparty-paper
agreement truncated 5 runs out of 5 at that ceiling with
`finish_reason='length'` and completed 2 of 2 once ~4000 tokens of thinking
room existed.

#677's policy pin masks it for the two models it touched. It does not fix
it: `model-policy/openrouter.json` still ships `selectable` entries whose
`reasoning_max_tokens` is `0` (anthropic/claude-sonnet-5,
openai/gpt-5.6-sol, deepseek/deepseek-v4-pro), and an operator who selects
one of those puts the judge straight back in the broken shape. The budget
must survive on its own.

The second defect this file pins is the one the first exposed. The module
docstring and `judge_floor_invariants`' own docstring both promise the
function "never raises on a judge failure" and that an unjudgeable invariant
lands in `FloorJudgment.unjudged` (fail closed). That promise did not cover
truncation: `model_client.ModelOutputTruncatedError` propagated straight out
of the loop, past `fail_closed`, past
`review_spine.REASON_FLOOR_INVARIANT_UNJUDGED`, and killed the review as an
unhandled exception. Fail-closed-to-unjudged is the property that must
survive any budget change, so it is asserted here directly.

## What this file asserts

  1. [1a-1c] The default budget is sized from the response contract this
     module actually enforces plus the measured reasoning need -- not a
     round number -- and is big enough that a reasoning-class model with NO
     declared allowance still answers.
  2. [2a-2c] RED-FIRST: a reasoning-class model given the OLD 1024 budget
     truncates, and the SAME model at the shipped default completes. Same
     double, same document, nothing varied but the ceiling.
  3. [3a-3d] A judge that truncates anyway (a model whose thinking exceeds
     even the new ceiling) fails CLOSED: the invariant lands in `unjudged`,
     `fail_closed` is True, it is NEVER counted as satisfied, and it never
     produces a `floor_fires` entry.
  4. [4a-4b] A truncated invariant is CLASSIFIED -- `FloorJudgment
     .truncated` names it, so "the judge ran out of room" is told apart from
     "the judge answered and the answer was unreadable".
  5. [5a-5b] The bounded retry is intact: a truncation spends exactly the
     retry budget this module has always had (1 initial + 1 re-invoke), and
     an invariant that truncates once and answers on the retry is judged.
  6. [6a] The caller can still override the budget.
  7. [7a-7f] The classification REACHES AN OPERATOR. A whole `review_spine
     .run_review` driven with a truncating judge carries `truncated` onto
     the result's `floor_judgment`, fails closed as
     `MANUAL_REVIEW_REQUIRED`, and records the DISTINCT reason token
     `floor_invariant_truncated`, which `pipeline_runner
     ._write_real_terminal` writes to the reviews row and
     `frontend/src/ReviewSubmission.tsx` explains in its own words -- "a
     retry will not help", not `floor_invariant_unjudged`'s "worth
     submitting again", which for a deterministic budget failure is the
     wrong instruction. The unreadable-judge run in the same test still
     records `floor_invariant_unjudged`, so the token is split, not flipped.

     This matters because the fix in (3)/(4) is a swallow: before it, the
     truncation escaped as `ModelOutputTruncatedError`, and
     `pipeline_runner.classify_failure_reason` recorded the accurate
     `model_output_truncated` on the row. Catching it must not cost the
     operator that diagnosis. `floor_judgment["truncated"]` alone cannot
     repay it -- it rides only the analysis artifact, which nothing in the
     product opens (see `pipeline_runner`'s own note at its `critic_attempts`
     write: "detail that lives only in the artifact is detail no operator can
     reach from inside the app").

Run with: python3 tests/test_floor_judge_budget_682.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Same table/bucket environment every other test of this layer sets before
# importing the backend modules (see
# tests/test_terminal_reason_completeness_670.py).
os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import model_client  # noqa: E402
import floor_judge  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

OPENROUTER_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"
FRONTEND_REVIEW_SUBMISSION = REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx"

# The gold OPF 0.3 fixture tests/test_review_opf_digest_mode_479.py drives its
# own Floor-coverage cases with: posture plus exactly ONE floor invariant.
OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
_FIXTURE_INVARIANT_ID = "no-uncapped-liability"

# Bedrock-form ids, the form an OPF bundle's `playbook.metadata` carries --
# same two ids that file uses.
_BUNDLE_PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
_BUNDLE_CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"

# Real ids from the shipped policy artifact. `_PINNED_MODEL_ID` is
# models.primary and carries a non-zero `reasoning_max_tokens`;
# `_NO_ALLOWANCE_MODEL_ID` is a `selectable` entry that declares `0`, which
# is the operator-selectable shape the judge's own budget has to survive.
_PINNED_MODEL_ID = "anthropic/claude-opus-5"
_NO_ALLOWANCE_MODEL_ID = "anthropic/claude-sonnet-5"

# The measured thinking need from #677/41e0b4c: the allowance that turned
# 0-of-5 completions into 2-of-2 on a real document.
_MEASURED_REASONING_NEED = 4000


def _invariant() -> dict[str, Any]:
    # Same {id, statement, rationale} shape as
    # tests/fixtures/opf/synthetic-eiaa.opf.json's floor.invariants.
    # Deliberately party-NEUTRAL so the #679 fail-closed gate (which sends a
    # party-relative statement straight to `unjudged` with no model call) is
    # not what this file is measuring.
    return {
        "id": "floor-no-uncapped-liability",
        "statement": (
            "Our maximum liability under this agreement is a fixed dollar "
            "cap stated in the agreement; it is never unlimited."
        ),
        "rationale": "Synthetic placeholder rationale; not legal advice.",
    }


def _verdict(invariant_id: str, violated: bool, evidence_quote: str = "") -> str:
    return json.dumps(
        {
            "invariant_id": invariant_id,
            "violated": violated,
            "evidence_quote": evidence_quote,
        }
    )


class ReasoningModelClient:
    """A reasoning-class model behind `OpenRouterModelClient`, honest about
    the budget arithmetic that actually killed real reviews.

    The real client sends `max_tokens = max_output_tokens +
    openrouter_reasoning_max_tokens(model_id)` and raises
    `ModelOutputTruncatedError` when the provider comes back
    `finish_reason='length'` (`backend/src/model_client.py::
    OpenRouterModelClient.invoke`, the raise site at the `finish_reason ==
    "length"` check). This double reproduces exactly that: the model spends
    `thinking_tokens` of reasoning FIRST and then writes the verdict, and if
    the two together do not fit the combined ceiling the caller bought, it
    truncates -- the same failure, from the same cause, in the same
    exception class.

    Rejects an over-cap request the way OpenRouter does, so a budget bug
    cannot hide behind a permissive double (same discipline as
    tests/test_output_budget_sizing_658.py::BudgetRecordingClient).
    """

    def __init__(self, responses: list[str], *, thinking_tokens: int) -> None:
        self._responses = list(responses)
        self._thinking_tokens = thinking_tokens
        self.budgets: list[int] = []
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
        **_kwargs: Any,
    ) -> str:
        declared_cap = model_client.openrouter_model_max_output_tokens(model_id)
        if max_output_tokens > declared_cap:
            raise AssertionError(
                f"the provider would refuse max_tokens={max_output_tokens} for "
                f"{model_id!r}, whose declared output cap is {declared_cap}"
            )
        self.budgets.append(max_output_tokens)
        self.calls.append(
            {"model_id": model_id, "system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        if not self._responses:
            raise AssertionError(
                "ReasoningModelClient ran out of seeded responses -- the judge "
                "made more attempts than this test scripted."
            )
        response_text = self._responses.pop(0)
        # The combined ceiling the real client would have sent.
        combined_ceiling = max_output_tokens + model_client.openrouter_reasoning_max_tokens(
            model_id
        )
        spent = self._thinking_tokens + pp.estimate_tokens(response_text)
        if spent > combined_ceiling:
            raise model_client.ModelOutputTruncatedError(
                "OpenRouter truncated the response before it finished "
                "(finish_reason='length', HTTP 200).",
                status_code=200,
            )
        return response_text


# ---------------------------------------------------------------------------
# 1. The default budget is derived, not guessed.
# ---------------------------------------------------------------------------


def test_default_budget_is_sized_from_the_response_contract(failures: list[str]) -> None:
    default = floor_judge.DEFAULT_MAX_OUTPUT_TOKENS

    # [1a] The largest verdict this module's OWN validator will accept has to
    # fit, with room to spare -- that is the content half of the budget.
    largest_verdict = _verdict(
        "i" * 120, True, "q" * floor_judge._EVIDENCE_QUOTE_MAX_CHARS
    )
    is_valid, _parsed = floor_judge._validate_judge_response(
        largest_verdict, expected_invariant_id="i" * 120
    )
    if not is_valid:
        failures.append(
            "[1a] setup: the maximal verdict this test sizes against is not "
            "one _validate_judge_response accepts -- the response contract "
            "moved and this sizing is measuring nothing"
        )
    content_need = pp.estimate_tokens(largest_verdict)
    if default <= content_need:
        failures.append(
            f"[1a] the default budget {default} does not even cover the "
            f"largest acceptable verdict ({content_need} est. tokens)"
        )

    # [1b] ...and the measured reasoning need has to fit ALONGSIDE it, because
    # `max_tokens` is a combined ceiling. This is the whole defect: 1024 was
    # under the measured thinking need on its own.
    if default < content_need + _MEASURED_REASONING_NEED:
        failures.append(
            f"[1b] the default budget {default} is below the measured "
            f"reasoning need ({_MEASURED_REASONING_NEED}) plus the content "
            f"need ({content_need}) -- a model with no declared allowance "
            f"truncates again"
        )

    # [1c] The old value is gone. Named explicitly so a revert is loud.
    if default == 1024:
        failures.append("[1c] the default budget is still the unsized 1024")


# ---------------------------------------------------------------------------
# 2. RED-FIRST: same model, same document, only the ceiling varies.
# ---------------------------------------------------------------------------


def test_old_budget_truncates_and_the_shipped_default_completes(failures: list[str]) -> None:
    invariants = [_invariant()]
    context = "Liability under this agreement shall be unlimited."

    # A model that declares NO reasoning allowance, so `max_output_tokens` is
    # the entire ceiling -- the operator-selectable shape the policy pin does
    # not cover.
    allowance = model_client.openrouter_reasoning_max_tokens(_NO_ALLOWANCE_MODEL_ID)
    if allowance != 0:
        failures.append(
            f"[2a] setup: {_NO_ALLOWANCE_MODEL_ID!r} now declares a reasoning "
            f"allowance of {allowance}, so it no longer exercises the "
            f"no-allowance path this test exists for -- pick another "
            f"`selectable` entry that declares 0"
        )

    thinking = _MEASURED_REASONING_NEED
    response = _verdict("floor-no-uncapped-liability", True, "liability shall be unlimited")

    # [2b] At the OLD ceiling the same model truncates.
    old_client = ReasoningModelClient([response, response], thinking_tokens=thinking)
    old_judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context=context,
        model_client=old_client,
        model_id=_NO_ALLOWANCE_MODEL_ID,
        max_output_tokens=1024,
    )
    if old_judgment.verdicts:
        failures.append(
            "[2b] the 1024 ceiling produced a verdict from a model that "
            "spends the measured reasoning need -- the double is not "
            "reproducing the measured truncation"
        )

    # [2c] At the SHIPPED default the same model answers. Nothing else varied.
    new_client = ReasoningModelClient([response], thinking_tokens=thinking)
    new_judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context=context,
        model_client=new_client,
        model_id=_NO_ALLOWANCE_MODEL_ID,
    )
    if new_judgment.unjudged:
        failures.append(
            f"[2c] the shipped default budget still truncates a "
            f"reasoning-class model with no declared allowance: "
            f"unjudged={new_judgment.unjudged}"
        )
    if [v["invariant_id"] for v in new_judgment.verdicts] != ["floor-no-uncapped-liability"]:
        failures.append(f"[2c] expected one verdict, got {new_judgment.verdicts!r}")


# ---------------------------------------------------------------------------
# 3. Fail closed: truncation must land in `unjudged`, never in "satisfied".
# ---------------------------------------------------------------------------


def test_a_truncated_judge_fails_closed_into_unjudged(failures: list[str]) -> None:
    invariants = [_invariant()]
    response = _verdict("floor-no-uncapped-liability", False)

    # A model whose thinking exceeds even the new ceiling -- the budget can
    # always be outrun, which is exactly why the property below cannot depend
    # on the budget.
    runaway = floor_judge.DEFAULT_MAX_OUTPUT_TOKENS + _MEASURED_REASONING_NEED + 10_000
    client = ReasoningModelClient([response, response], thinking_tokens=runaway)

    try:
        judgment = floor_judge.judge_floor_invariants(
            invariants=invariants,
            review_context="Liability under this agreement shall be unlimited.",
            model_client=client,
            model_id=_PINNED_MODEL_ID,
        )
    except model_client.ModelOutputTruncatedError:
        failures.append(
            "[3a] a truncated judge RAISED out of judge_floor_invariants -- "
            "the documented contract is that an unjudgeable invariant lands "
            "in `unjudged` (fail closed), never that it kills the caller"
        )
        return

    # [3a] The invariant is unjudged.
    if judgment.unjudged != ["floor-no-uncapped-liability"]:
        failures.append(f"[3a] expected the invariant in `unjudged`, got {judgment.unjudged!r}")
    # [3b] ...and the coverage gate is closed.
    if not judgment.fail_closed:
        failures.append("[3b] a truncated invariant did not close the coverage gate")
    # [3c] ...and it is NOT counted as satisfied (no `violated: false` verdict
    # invented for it).
    if judgment.verdicts:
        failures.append(
            f"[3c] a truncated invariant produced a verdict -- it was counted "
            f"as answered: {judgment.verdicts!r}"
        )
    # [3d] ...and it fires nothing either: silence is not a violation.
    if floor_judge.floor_fires(judgment):
        failures.append("[3d] a truncated invariant produced a detector fire")


# ---------------------------------------------------------------------------
# 4. The truncation is classified, not just swallowed.
# ---------------------------------------------------------------------------


def test_truncation_is_named_on_the_judgment_and_on_the_result(failures: list[str]) -> None:
    invariants = [_invariant()]
    runaway = floor_judge.DEFAULT_MAX_OUTPUT_TOKENS + _MEASURED_REASONING_NEED + 10_000

    truncating = ReasoningModelClient(
        [_verdict("floor-no-uncapped-liability", False)] * 2, thinking_tokens=runaway
    )
    truncated_judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability under this agreement shall be unlimited.",
        model_client=truncating,
        model_id=_PINNED_MODEL_ID,
    )
    # [4a] "ran out of room" is a different diagnosis from "answered
    # unreadably", and both land in `unjudged` -- so `unjudged` alone cannot
    # tell them apart.
    if getattr(truncated_judgment, "truncated", None) != ["floor-no-uncapped-liability"]:
        failures.append(
            f"[4a] the truncated invariant is not named on the judgment: "
            f"truncated={getattr(truncated_judgment, 'truncated', '<missing>')!r}"
        )

    # [4b] The OTHER unjudged class -- a model that answers, twice, with
    # something the validator rejects -- must NOT be labelled truncated.
    unreadable = model_client.FakeBedrockClient({_PINNED_MODEL_ID: ["not json", "still not json"]})
    unreadable_judgment = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability under this agreement shall be unlimited.",
        model_client=unreadable,
        model_id=_PINNED_MODEL_ID,
    )
    if unreadable_judgment.unjudged != ["floor-no-uncapped-liability"]:
        failures.append(
            f"[4b] setup: the unreadable-response path no longer lands in "
            f"`unjudged`: {unreadable_judgment.unjudged!r}"
        )
    if getattr(unreadable_judgment, "truncated", None):
        failures.append(
            f"[4b] an unreadable (not truncated) response was labelled a "
            f"truncation: {unreadable_judgment.truncated!r}"
        )


# ---------------------------------------------------------------------------
# 5. The bounded retry is intact.
# ---------------------------------------------------------------------------


def test_truncation_spends_the_bounded_retry_and_no_more(failures: list[str]) -> None:
    invariants = [_invariant()]
    runaway = floor_judge.DEFAULT_MAX_OUTPUT_TOKENS + _MEASURED_REASONING_NEED + 10_000

    # [5a] 1 initial + exactly 1 re-invoke, then it stops. Seeded with more
    # responses than that: the double raises AssertionError if the judge asks
    # for a third, and counts what it actually served.
    client = ReasoningModelClient(
        [_verdict("floor-no-uncapped-liability", False)] * 5, thinking_tokens=runaway
    )
    floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability under this agreement shall be unlimited.",
        model_client=client,
        model_id=_PINNED_MODEL_ID,
    )
    expected_attempts = 1 + floor_judge._MAX_RETRIES_PER_INVARIANT
    if len(client.calls) != expected_attempts:
        failures.append(
            f"[5a] a truncated invariant made {len(client.calls)} attempts, "
            f"expected the module's own bounded budget of {expected_attempts}"
        )

    # [5b] A truncation is not terminal for the invariant: the retry can still
    # produce a verdict. Modelled with a client whose thinking drops on the
    # second call -- the same nondeterminism a real model has.
    class RecoveringClient(ReasoningModelClient):
        def invoke(self, **kwargs: Any) -> str:
            try:
                return super().invoke(**kwargs)
            finally:
                self._thinking_tokens = 10

    recovering = RecoveringClient(
        [_verdict("floor-no-uncapped-liability", True, "unlimited")] * 2,
        thinking_tokens=runaway,
    )
    recovered = floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability under this agreement shall be unlimited.",
        model_client=recovering,
        model_id=_PINNED_MODEL_ID,
    )
    if recovered.unjudged or len(recovered.verdicts) != 1:
        failures.append(
            f"[5b] a truncation on the first attempt was terminal for the "
            f"invariant even though the retry answered: "
            f"unjudged={recovered.unjudged!r} verdicts={recovered.verdicts!r}"
        )
    if getattr(recovered, "truncated", None):
        failures.append(
            f"[5b] an invariant that recovered on the retry was still "
            f"labelled truncated: {recovered.truncated!r}"
        )


# ---------------------------------------------------------------------------
# 6. The caller can still override the budget.
# ---------------------------------------------------------------------------


def test_the_caller_can_still_override_the_budget(failures: list[str]) -> None:
    invariants = [_invariant()]
    client = ReasoningModelClient(
        [_verdict("floor-no-uncapped-liability", False)], thinking_tokens=10
    )
    floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="Liability is capped at a fixed dollar amount.",
        model_client=client,
        model_id=_PINNED_MODEL_ID,
        max_output_tokens=7777,
    )
    if client.budgets != [7777]:
        failures.append(
            f"[6a] the caller's explicit budget did not reach the client: "
            f"{client.budgets!r}"
        )


# ---------------------------------------------------------------------------
# 7 harness: a real OPF review, driven end to end.
#
# Same shapes tests/test_review_opf_digest_mode_479.py's own Floor-coverage
# cases use -- the gold OPF fixture as an `opf_bundle_v2`, a minimal synthetic
# .docx, and the shipped `FakeBedrockClient` for the passes that are not the
# subject here.
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)
_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _build_docx_bytes() -> bytes:
    """A synthetic two-paragraph agreement extract. No real counterparty
    text: invented wording, no party names."""
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r>'
        "<w:t>Indemnification</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Each party shall indemnify the other without "
        "limitation as to amount.</w:t></w:r></w:p>"
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _opf_bundle() -> dict[str, Any]:
    with open(OPF_FIXTURE_PATH, encoding="utf-8") as handle:
        doc = json.load(handle)
    return {
        "opf_bundle_v2": {"opf": doc, "overrides": None},
        "playbook": {
            "metadata": {
                "primary_model_id": _BUNDLE_PRIMARY_MODEL_ID,
                "critic_model_id": _BUNDLE_CRITIC_MODEL_ID,
            }
        },
    }


def _accept_response(summary: str | None) -> str:
    return json.dumps(
        {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": summary,
        }
    )


class _FloorJudgeRoutingClient:
    """Routes by `system_prompt`: the FLOOR JUDGE's calls (which always carry
    `floor_judge._SYSTEM_PROMPT`) go to `_judge_response`, every other pass to
    a seeded `FakeBedrockClient`. Routing on the system prompt rather than on
    a call index means a change in pass ordering cannot silently point these
    tests at the wrong call."""

    def __init__(self) -> None:
        self._passes = model_client.FakeBedrockClient(
            {
                _BUNDLE_PRIMARY_MODEL_ID: [_accept_response("No changes identified.")],
                _BUNDLE_CRITIC_MODEL_ID: [_accept_response(None)],
            }
        )
        self.judge_calls: list[dict[str, Any]] = []

    def capabilities(self, model_id: str) -> dict[str, bool]:
        return self._passes.capabilities(model_id)

    def invoke(self, **kwargs: Any) -> str:
        if kwargs.get("system_prompt") == floor_judge._SYSTEM_PROMPT:
            self.judge_calls.append(dict(kwargs))
            return self._judge_response(**kwargs)
        return self._passes.invoke(**kwargs)

    def _judge_response(self, **kwargs: Any) -> str:
        raise NotImplementedError


class _TruncatingFloorJudgeClient(_FloorJudgeRoutingClient):
    """The judge spends more thinking than the ceiling it was bought, on
    every attempt. The truncation is produced by `ReasoningModelClient`'s own
    combined-ceiling arithmetic -- the same double the rest of this file
    measures the budget with -- not by a bare `raise`."""

    def __init__(self) -> None:
        super().__init__()
        self._judge = ReasoningModelClient(
            [_verdict(_FIXTURE_INVARIANT_ID, False)] * 4,
            thinking_tokens=(
                floor_judge.DEFAULT_MAX_OUTPUT_TOKENS + _MEASURED_REASONING_NEED + 10_000
            ),
        )

    def _judge_response(self, **kwargs: Any) -> str:
        return self._judge.invoke(**kwargs)


class _UnreadableFloorJudgeClient(_FloorJudgeRoutingClient):
    """The other way an invariant goes unjudged: the judge ANSWERS, within
    budget, and the answer is not a verdict this module will accept."""

    def _judge_response(self, **kwargs: Any) -> str:
        return "not json"


class FakeReviewsTable:
    """The same generic SET interpreter
    tests/test_terminal_reason_completeness_670.py and
    tests/test_critic_failure_reason_665.py use, so the assertion is about
    what `_write_real_terminal` actually emits rather than about a bespoke
    recorder."""

    def __init__(self, review_id: str) -> None:
        self.item: dict[str, Any] = {"review_id": review_id, "status": "RUNNING"}

    def update_item(
        self,
        Key,  # noqa: N803 - boto3 signature
        UpdateExpression,  # noqa: N803
        ConditionExpression=None,  # noqa: N803
        ExpressionAttributeNames=None,  # noqa: N803
        ExpressionAttributeValues=None,  # noqa: N803
    ):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            self.item[field] = values[val_token.strip()]


class FakeDDB:
    def __init__(self, reviews_table: FakeReviewsTable) -> None:
        self._reviews = reviews_table

    def Table(self, name):  # noqa: N802, ARG002 - boto3 signature
        return self._reviews


def _row_for(result: dict[str, Any]) -> dict[str, Any]:
    """The reviews row `pipeline_runner._write_real_terminal` writes for
    *result* -- the ONLY failure surface the product reads back
    (`backend/src/reviews.py::list_recent_failures` never opens the analysis
    artifact)."""
    table = FakeReviewsTable("floor-682-row")
    pr._write_real_terminal("floor-682-row", result, None, FakeDDB(table))
    return table.item


def _reason_explanations() -> dict[str, tuple[str, str]]:
    """`REASON_EXPLANATIONS` key -> its (cause, fix) strings, read out of the
    real UI source -- the same cross-language source read
    tests/test_terminal_reason_completeness_670.py does."""
    source = FRONTEND_REVIEW_SUBMISSION.read_text(encoding="utf-8")
    parts = source.split("REASON_EXPLANATIONS: Record", 1)
    if len(parts) != 2:
        raise AssertionError(f"REASON_EXPLANATIONS not found in {FRONTEND_REVIEW_SUBMISSION}")
    block = parts[1].split("const STAGE_EXPLANATIONS", 1)[0]
    entries: dict[str, tuple[str, str]] = {}
    current: str | None = None
    cause: str | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        if stripped.endswith(": {"):
            current = stripped[: -len(": {")]
            cause = None
            continue
        if current is None:
            continue
        if stripped.startswith("cause: '"):
            cause = stripped[len("cause: '") :].rstrip(",").rstrip("'")
        elif stripped.startswith("fix: '") and cause is not None:
            entries[current] = (cause, stripped[len("fix: '") :].rstrip(",").rstrip("'"))
            current = None
            cause = None
    return entries


# ---------------------------------------------------------------------------
# 7. The classification reaches an operator: result -> reviews row -> copy.
# ---------------------------------------------------------------------------


def test_a_truncated_floor_judge_reaches_the_operator(failures: list[str]) -> None:
    """A whole `run_review` against the gold OPF fixture, with the primary
    and critic passes answering normally and only the FLOOR JUDGE running out
    of room.

    Catching the truncation (section 3) took away the accurate
    `model_output_truncated` the row used to get from
    `pipeline_runner.classify_failure_reason`. This is where that debt is
    repaid: the same fail-closed terminal, a DISTINCT reason token on the
    reviews row, and its own reader-facing copy.
    """
    truncating = _TruncatingFloorJudgeClient()
    result = review_spine.run_review(
        _build_docx_bytes(), _opf_bundle(), truncating, review_id="floor-682-truncated"
    )

    # [7a] Setup guard: the judge was actually invoked. Without it this whole
    # test would pass on the #679 party-relative gate, which sends this
    # fixture's invariant to `unjudged` with no model call at all -- unjudged,
    # but never truncated.
    if not truncating.judge_calls:
        failures.append(
            "[7a] setup: the floor judge was never invoked, so nothing in "
            "this run truncated -- the invariant reached `unjudged` some "
            "other way and the assertions below prove nothing"
        )
        return

    # [7b] The classification survives the spine, onto the result key
    # `pipeline_runner._ANALYSIS_FIELDS` persists.
    floor_judgment = result.get("floor_judgment") or {}
    if floor_judgment.get("truncated") != [_FIXTURE_INVARIANT_ID]:
        failures.append(
            f"[7b] run_review's floor_judgment does not name the truncated "
            f"invariant: {floor_judgment.get('truncated')!r}"
        )
    # [7c] ...and it is still unjudged: a diagnosis, never a downgrade of the
    # fail-closed coverage gate.
    if floor_judgment.get("unjudged") != [_FIXTURE_INVARIANT_ID]:
        failures.append(
            f"[7c] the truncated invariant left `unjudged`: "
            f"{floor_judgment.get('unjudged')!r}"
        )
    if result.get("status") != "MANUAL_REVIEW_REQUIRED" or result.get("decision") is not None:
        failures.append(
            f"[7c] the run did not fail closed: status={result.get('status')!r} "
            f"decision={result.get('decision')!r}"
        )

    # [7d] The row -- the only surface an operator reads (`reviews
    # .list_recent_failures` never opens the analysis artifact) -- carries the
    # truncation's OWN token.
    row = _row_for(result)
    if row.get("reason") != review_spine.REASON_FLOOR_INVARIANT_TRUNCATED:
        failures.append(
            f"[7d] the reviews row does not name the truncation: "
            f"reason={row.get('reason')!r}, expected "
            f"{review_spine.REASON_FLOOR_INVARIANT_TRUNCATED!r}"
        )

    # [7e] The OTHER unjudged class is NOT relabelled: a judge that answers
    # unreadably twice still records `floor_invariant_unjudged`, whose copy
    # (correctly, for that failure) invites a resubmit.
    unreadable = _UnreadableFloorJudgeClient()
    unreadable_result = review_spine.run_review(
        _build_docx_bytes(), _opf_bundle(), unreadable, review_id="floor-682-unreadable"
    )
    unreadable_row = _row_for(unreadable_result)
    if unreadable_row.get("reason") != review_spine.REASON_FLOOR_INVARIANT_UNJUDGED:
        failures.append(
            f"[7e] an unreadable (not truncated) judge no longer records "
            f"{review_spine.REASON_FLOOR_INVARIANT_UNJUDGED!r}: "
            f"{unreadable_row.get('reason')!r} -- the token was flipped for "
            f"every fail-closed floor terminal instead of split by cause"
        )
    if (unreadable_result.get("floor_judgment") or {}).get("truncated"):
        failures.append(
            f"[7e] the unreadable run was labelled truncated: "
            f"{unreadable_result['floor_judgment']['truncated']!r}"
        )

    # [7f] Both tokens have their own reader-facing copy, and it is DIFFERENT
    # copy -- a token with no `REASON_EXPLANATIONS` entry renders the vague
    # stage fallback, and a token sharing the other's entry would put the
    # "worth submitting again" advice back in front of a deterministic budget
    # failure. Same cross-language source read
    # tests/test_terminal_reason_completeness_670.py uses; that the screen
    # RENDERS the entry is asserted in the frontend suite.
    explanations = _reason_explanations()
    for token in (
        review_spine.REASON_FLOOR_INVARIANT_TRUNCATED,
        review_spine.REASON_FLOOR_INVARIANT_UNJUDGED,
    ):
        if token not in explanations:
            failures.append(
                f"[7f] {token!r} has no REASON_EXPLANATIONS entry in "
                f"{FRONTEND_REVIEW_SUBMISSION.name}, so the reader is shown "
                f"the vague stage copy instead of a cause and a fix"
            )
    truncated_copy = explanations.get(review_spine.REASON_FLOOR_INVARIANT_TRUNCATED)
    unjudged_copy = explanations.get(review_spine.REASON_FLOOR_INVARIANT_UNJUDGED)
    if truncated_copy is not None and unjudged_copy is not None and (
        truncated_copy[1] == unjudged_copy[1]
    ):
        failures.append(
            "[7f] both floor terminals render the SAME fix text, so the "
            "truncation still tells the reader to submit the same document "
            "again -- which is exactly the advice this split exists to stop"
        )


def main() -> int:
    failures: list[str] = []
    for test in (
        test_default_budget_is_sized_from_the_response_contract,
        test_old_budget_truncates_and_the_shipped_default_completes,
        test_a_truncated_judge_fails_closed_into_unjudged,
        test_truncation_is_named_on_the_judgment_and_on_the_result,
        test_truncation_spends_the_bounded_retry_and_no_more,
        test_the_caller_can_still_override_the_budget,
        test_a_truncated_floor_judge_reaches_the_operator,
    ):
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001 - report, do not abort the file
            failures.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")

    if failures:
        print("FAIL (issue #682):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: floor judge output budget is sized from evidence and fails closed (#682)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
