#!/usr/bin/env python3
"""
Issue #658: the model OUTPUT budget must be sized from the document, and a
truncation must get a retry allowance of its own.

## What was actually broken

`scripts/primary_review_pass.py::MAX_OUTPUT_TOKENS` was a flat `8_000` from
the very first primary-pass commit (18a7434), and nothing re-derived it when
the v3 block-transcript contract landed (1aef16e) and changed the output
economics completely. Under v3 `playbooks/output-schema-v3.json`'s
`block_patches` require the keep+delete half to reproduce every edited
block's real text end to end, plus insert segments carrying the new
language, plus per-issue rationale and evidence prose -- so the response is
roughly PROPORTIONAL to the reviewed text. A five-page educational
affiliation agreement (2,445 words / 57 blocks / ~4,100 tokens of body text)
died at `run_review` with `model_output_truncated` on 2026-09-01: the flat
budget, then the single retry that doubled it, then the review was gone.

Two mechanisms are proven here, both of which were absent:

  1. SIZING. The budget is `clamp(FLOOR + 2.5 * document_tokens, FLOOR, the
     selected model's OWN declared output cap)` -- so a short agreement and
     an 80-page agreement no longer ask for the same thing, the ceiling
     comes from `model-policy/openrouter.json` rather than a constant, and a
     model that declares no cap fails closed to a conservative default.

  2. TRUNCATION'S OWN RETRY ALLOWANCE. `MAX_RETRIES_PER_PASS` was shared
     across every failure class, so a schema (or `source_mismatch`)
     rejection on attempt 1 left attempt 2 running at the un-widened budget,
     where a truncation was immediately terminal. A response that did not
     fit must ALWAYS get at least one more attempt with more room -- it is
     the one failure where the model did nothing wrong.

## Fixture fidelity

The document text every sizing assertion is made against is produced by the
PRODUCTION path, not typed in: a synthetic `.docx` (tests/test_block_mode_e2e
.py::_make_docx) -> `extraction_normalization_stage.extract_and_normalize` ->
`review_spine.document_text_for_review` -- the exact three steps
`review_spine.run_review` takes before it hands `doc_text` to the passes. The
model-policy figures are read from the real on-disk
`model-policy/openrouter.json`, never from a hand-built policy dict, so a
policy edit that drops the new field fails this file rather than passing
against a mirror.

`BudgetRecordingClient` REJECTS what the real provider rejects: an
OpenRouter request whose `max_tokens` exceeds the model's declared output
cap is a provider-side 400, so the double raises rather than quietly
recording an impossible number -- otherwise every "the budget never exceeds
the cap" assertion behind it would be a rubber stamp.

Run standalone: `python3 tests/test_output_budget_sizing_658.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

try:  # reviews.py prefers the `src.` package form when it is importable
    from src import reviews as reviews_module  # type: ignore
except ImportError:  # pragma: no cover - the sys.path form the other tests use
    import reviews as reviews_module  # type: ignore

from test_block_mode_e2e import SECTIONS, _make_docx  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
OPENROUTER_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"

# Real ids from the shipped policy artifact. `_DECLARING_MODEL_ID` declares a
# `max_output_tokens`; `_SILENT_MODEL_ID` deliberately declares none, which is
# what exercises the fail-closed branch.
_DECLARING_MODEL_ID = "anthropic/claude-opus-5"
_SILENT_MODEL_ID = "moonshotai/kimi-k3"
_CRITIC_MODEL_ID = "anthropic/claude-sonnet-4.6"


def _policy() -> dict[str, Any]:
    with open(OPENROUTER_POLICY_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _doc_text(repeats: int) -> str:
    """A real reviewed-document string, via the production path.

    `SECTIONS` repeated `repeats` times keeps the synthetic prose the repo
    already ships (never counterparty text) while letting the document grow,
    which is the only variable the sizing formula reads.
    """
    sections = []
    for i in range(repeats):
        for heading, bodies in SECTIONS:
            sections.append((f"{heading} ({i + 1})", list(bodies)))
    normalized = ens.extract_and_normalize(_make_docx(sections))
    assert normalized["status"] == "normalized", normalized
    return review_spine.document_text_for_review(normalized["paragraphs"])


class BudgetRecordingClient:
    """Records the budget each attempt asks for and replays a scripted list
    of responses. A scripted entry that is an exception CLASS is raised.

    Rejects an over-cap request the way OpenRouter does (HTTP 400), so a
    budget bug cannot hide behind a permissive double.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.budgets: list[int] = []

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
        **_kwargs: Any,
    ) -> str:
        declared = model_client.openrouter_model_max_output_tokens(model_id)
        if max_output_tokens > declared:
            raise AssertionError(
                f"the provider would refuse max_tokens={max_output_tokens} for "
                f"{model_id!r}, whose declared output cap is {declared}"
            )
        self.budgets.append(max_output_tokens)
        nxt = self._script.pop(0)
        if isinstance(nxt, type) and issubclass(nxt, Exception):
            raise nxt(
                "OpenRouter truncated the response before it finished "
                "(finish_reason='length', HTTP 200).",
                status_code=200,
            )
        return nxt


def _run_primary(client: Any, doc_text: str, model_id: str = _DECLARING_MODEL_ID,
                 **overrides: Any) -> dict[str, Any]:
    return pp.run_primary_pass(
        review_id="budget-658",
        retrieved_precedent=[],
        playbook=_playbook(),
        model_client=client,
        model_id=model_id,
        ledger_write=[].append,
        doc_text=doc_text,
        **overrides,
    )


# ---------------------------------------------------------------------------
# 1. The budget is sized from the document, not from a constant.
# ---------------------------------------------------------------------------


def test_a_short_and_a_long_document_get_materially_different_budgets(
    failures: list[str],
) -> None:
    short_text = _doc_text(1)
    long_text = _doc_text(40)

    short_client = BudgetRecordingClient([_fixture("primary_request_change_valid.json")])
    long_client = BudgetRecordingClient([_fixture("primary_request_change_valid.json")])
    _run_primary(short_client, short_text)
    _run_primary(long_client, long_text)

    if not short_client.budgets or not long_client.budgets:
        failures.append("[1a] setup: the pass made no model call")
        return
    short_budget, long_budget = short_client.budgets[0], long_client.budgets[0]

    if pp.estimate_tokens(long_text) <= pp.estimate_tokens(short_text):
        failures.append("[1b] setup: the long document is not actually longer")
    if long_budget <= short_budget:
        failures.append(
            f"[1c] the budget must scale with the document -- a "
            f"{pp.estimate_tokens(short_text)}-token document asked for {short_budget} "
            f"and a {pp.estimate_tokens(long_text)}-token one asked for {long_budget}"
        )
    floor = model_client.OUTPUT_BUDGET_FLOOR_TOKENS
    if short_budget < floor:
        failures.append(f"[1d] every budget is at least the floor {floor}; got {short_budget}")
    cap = model_client.openrouter_model_max_output_tokens(_DECLARING_MODEL_ID)
    if long_budget > cap:
        failures.append(
            f"[1e] no budget may exceed the model's declared cap {cap}; got {long_budget}"
        )
    # The flat 8_000 this replaced is the specific number that killed a real
    # five-page agreement -- no sized budget may land back on it.
    if short_budget <= 8_000:
        failures.append(
            f"[1f] the sized budget collapsed back to the pre-#658 flat 8_000 "
            f"neighbourhood ({short_budget})"
        )


def test_the_budget_matches_the_documented_formula(failures: list[str]) -> None:
    """The formula, recomputed here from the on-disk policy rather than read
    off the module, so this cannot pass against a mirror of itself."""
    doc_text = _doc_text(3)
    client = BudgetRecordingClient([_fixture("primary_request_change_valid.json")])
    _run_primary(client, doc_text)

    declared = None
    policy = _policy()
    for entry in list((policy.get("models") or {}).values()) + list(policy.get("selectable") or []):
        if entry.get("model_id") == _DECLARING_MODEL_ID and entry.get("max_output_tokens"):
            declared = int(entry["max_output_tokens"])
            break
    if declared is None:
        failures.append(
            f"[2a] model-policy/openrouter.json declares no max_output_tokens for "
            f"{_DECLARING_MODEL_ID!r} -- the ceiling must come from the policy, not a constant"
        )
        return

    document_tokens = pp.estimate_tokens(doc_text)
    expected = min(
        max(
            model_client.OUTPUT_BUDGET_FLOOR_TOKENS
            + int(model_client.OUTPUT_BUDGET_TOKENS_PER_DOCUMENT_TOKEN * document_tokens),
            model_client.OUTPUT_BUDGET_FLOOR_TOKENS,
        ),
        declared,
    )
    if client.budgets[:1] != [expected]:
        failures.append(
            f"[2b] expected clamp(FLOOR + K*{document_tokens}, FLOOR, {declared}) = {expected}; "
            f"the pass asked for {client.budgets[:1]}"
        )


def test_an_eighty_page_document_reaches_the_declared_cap(failures: list[str]) -> None:
    """The owner's stated bar: an ~80-page agreement (~40,000 words, ~53,000
    estimated tokens) must be reviewable end to end, i.e. its budget rides
    the model's declared cap rather than a 32,000-token stand-in."""
    declared = model_client.openrouter_model_max_output_tokens(_DECLARING_MODEL_ID)
    budget = model_client.output_budget_for_document(53_000, declared)
    if budget != declared:
        failures.append(
            f"[3a] an ~80-page document must be budgeted at the model's declared cap "
            f"{declared}; got {budget}"
        )
    if declared <= model_client.DEFAULT_MAX_OUTPUT_TOKENS:
        failures.append(
            f"[3b] setup: {_DECLARING_MODEL_ID!r} must declare a cap ABOVE the fail-closed "
            f"default for this assertion to mean anything (declared={declared})"
        )


def test_a_model_that_declares_no_cap_fails_closed(failures: list[str]) -> None:
    policy = _policy()
    for entry in list((policy.get("models") or {}).values()) + list(policy.get("selectable") or []):
        if entry.get("model_id") == _SILENT_MODEL_ID and entry.get("max_output_tokens"):
            failures.append(
                f"[4a] setup: {_SILENT_MODEL_ID!r} now declares max_output_tokens, so it no "
                f"longer exercises the fail-closed branch -- pick another silent model"
            )
            return

    resolved = model_client.openrouter_model_max_output_tokens(_SILENT_MODEL_ID)
    if resolved != model_client.DEFAULT_MAX_OUTPUT_TOKENS:
        failures.append(
            f"[4b] a model declaring no cap must fail closed to "
            f"{model_client.DEFAULT_MAX_OUTPUT_TOKENS}; got {resolved}"
        )
    if model_client.openrouter_model_max_output_tokens("") != model_client.DEFAULT_MAX_OUTPUT_TOKENS:
        failures.append("[4c] a falsy model_id must fail closed too, never match by accident")
    if (
        model_client.openrouter_model_max_output_tokens("anthropic.claude-opus-4-8")
        != model_client.DEFAULT_MAX_OUTPUT_TOKENS
    ):
        failures.append("[4d] a Bedrock model id is absent from this artifact -> fail closed")

    # And the budget for a huge document on that model is capped there, not
    # at the declaring model's much larger figure.
    budget = model_client.output_budget_for_document(53_000, resolved)
    if budget != model_client.DEFAULT_MAX_OUTPUT_TOKENS:
        failures.append(
            f"[4e] the fail-closed cap must bound the budget too; got {budget}"
        )


def test_the_critic_pass_sizes_from_the_document_too(failures: list[str]) -> None:
    doc_text = _doc_text(6)
    client = BudgetRecordingClient([_fixture("critic_no_delta_accept_valid.json")])
    cp.run_critic_pass(
        review_id="budget-658-critic",
        primary_output=json.loads(_fixture("primary_request_change_valid.json")),
        playbook=_playbook(),
        model_client=client,
        model_id=_CRITIC_MODEL_ID,
        ledger_write=[].append,
        doc_text=doc_text,
    )
    if not client.budgets:
        failures.append("[5a] setup: the critic pass made no model call")
        return
    expected = model_client.output_budget_for_document(
        pp.estimate_tokens(doc_text),
        model_client.openrouter_model_max_output_tokens(_CRITIC_MODEL_ID),
    )
    if client.budgets[0] != expected:
        failures.append(
            f"[5b] the critic budget must follow the same sizing ({expected}); "
            f"got {client.budgets[0]}"
        )
    if client.budgets[0] <= 8_000:
        failures.append(
            f"[5c] the critic is the MORE truncation-prone pass and must not be left on "
            f"the pre-#658 flat budget; got {client.budgets[0]}"
        )


# ---------------------------------------------------------------------------
# 2. Truncation's own retry allowance.
# ---------------------------------------------------------------------------


def _schema_invalid_body() -> str:
    """A complete review whose `confidence_state` is the natural-language
    word a real model actually returned (see
    tests/test_primary_pass_retry_recovery.py) -- schema-invalid, and
    nothing else about it is wrong."""
    body = json.loads(_fixture("primary_request_change_valid.json"))
    body["confidence_state"] = "medium"
    return json.dumps(body)


def test_a_schema_rejection_does_not_consume_the_truncation_retry(
    failures: list[str],
) -> None:
    """THE regression this issue exists for. Attempt 1 is rejected by the
    schema (spending the general retry), attempt 2 truncates. Before #658
    that was terminal at the un-widened budget; now the truncation spends its
    OWN allowance and attempt 3 runs with strictly more room."""
    doc_text = _doc_text(2)
    client = BudgetRecordingClient(
        [
            _schema_invalid_body(),
            model_client.ModelOutputTruncatedError,
            _fixture("primary_request_change_valid.json"),
        ]
    )
    try:
        result: Any = _run_primary(client, doc_text)
    except model_client.ModelOutputTruncatedError:
        # The pre-#658 shape: the general retry was already spent by the
        # schema rejection, so the truncation had nowhere to go and killed
        # the whole review.
        result = {"status": "raised ModelOutputTruncatedError"}

    if result.get("status") != "OK":
        failures.append(
            f"[6a] a truncation AFTER a schema rejection must still buy one more "
            f"attempt with more room; got {result!r}"
        )
    if len(client.budgets) != 3:
        failures.append(f"[6b] expected 3 attempts (1 general + 1 truncation); got {client.budgets}")
        return
    if client.budgets[0] != client.budgets[1]:
        failures.append(
            f"[6c] the schema retry must NOT change the budget -- only a truncation does; "
            f"got {client.budgets[0]} then {client.budgets[1]}"
        )
    if client.budgets[2] <= client.budgets[1]:
        failures.append(
            f"[6d] the attempt after the truncation must ask for strictly more room; "
            f"got {client.budgets[1]} then {client.budgets[2]}"
        )


def test_the_truncation_allowance_is_bounded_not_a_free_loop(failures: list[str]) -> None:
    """One truncation allowance, not an unbounded one: a pass whose every
    attempt truncates still terminates, and still terminates AS a truncation
    (the token Diagnostics keys its copy off)."""
    doc_text = _doc_text(2)
    client = BudgetRecordingClient([model_client.ModelOutputTruncatedError] * 8)
    raised: BaseException | None = None
    try:
        _run_primary(client, doc_text)
    except model_client.ModelOutputTruncatedError as exc:
        raised = exc

    if raised is None:
        failures.append("[7a] an unrecoverable truncation must still reach the caller")
    expected_max = (
        1 + pp.MAX_RETRIES_PER_PASS + pp.MAX_TRUNCATION_RETRIES_PER_PASS
    )
    if len(client.budgets) > expected_max:
        failures.append(
            f"[7b] at most {expected_max} attempts per pass; got {len(client.budgets)}"
        )
    if len(client.budgets) < 2:
        failures.append(f"[7c] a truncation must buy at least one retry; got {client.budgets}")


def test_the_widening_step_is_a_meaningful_jump_and_respects_the_cap(
    failures: list[str],
) -> None:
    cap = model_client.openrouter_model_max_output_tokens(_DECLARING_MODEL_ID)
    widened = model_client.widen_output_budget(20_000, cap)
    if widened - 20_000 < 4_000:
        failures.append(
            f"[8a] the widening must be a meaningful jump, not a token or two; "
            f"20000 -> {widened}"
        )
    if widened >= 40_000:
        failures.append(
            f"[8b] with a document-sized starting budget the first widening must not "
            f"double the spend and latency commitment; 20000 -> {widened}"
        )
    if model_client.widen_output_budget(cap, cap) != cap:
        failures.append("[8c] widening may never exceed the model's declared cap")
    if model_client.widen_output_budget(cap - 1, cap) != cap:
        failures.append("[8d] widening must clamp to the cap rather than overshoot it")


# ---------------------------------------------------------------------------
# 3. The spend reservation reads the SAME sizing function.
# ---------------------------------------------------------------------------


def test_the_reservation_is_computed_from_the_sizing_function(failures: list[str]) -> None:
    expected = model_client.output_budget_for_document(
        reviews_module.MAX_INPUT_TOKENS, model_client.DEFAULT_MAX_OUTPUT_TOKENS
    )
    if reviews_module.MAX_OUTPUT_TOKENS != expected:
        failures.append(
            f"[9a] reviews.MAX_OUTPUT_TOKENS must be the sizing function's worst case "
            f"({expected}); got {reviews_module.MAX_OUTPUT_TOKENS}"
        )

    source = (REPO_ROOT / "backend" / "src" / "reviews.py").read_text(encoding="utf-8")
    for literal in ("8_000", "8000"):
        if f"MAX_OUTPUT_TOKENS = {literal}" in source:
            failures.append(
                f"[9b] backend/src/reviews.py still re-declares the output budget as a "
                f"{literal} literal instead of importing the sizing function"
            )

    # Retry-inclusive, and inclusive of the truncation attempt the pass can
    # now make -- pricing one fewer attempt than the pass can spend is the
    # under-reservation issue #658 called out.
    attempts = (
        1
        + reviews_module.MAX_RETRIES_PER_PASS
        + reviews_module.MAX_TRUNCATION_RETRIES_PER_PASS
    )
    expected_cents = int(round(
        attempts
        * (
            (
                reviews_module.MAX_INPUT_TOKENS
                * reviews_module.PRIMARY_INPUT_RATE_USD_PER_MILLION
                + reviews_module.MAX_OUTPUT_TOKENS
                * reviews_module.PRIMARY_OUTPUT_RATE_USD_PER_MILLION
            )
            + (
                reviews_module.MAX_INPUT_TOKENS
                * reviews_module.CRITIC_INPUT_RATE_USD_PER_MILLION
                + reviews_module.MAX_OUTPUT_TOKENS
                * reviews_module.CRITIC_OUTPUT_RATE_USD_PER_MILLION
            )
        )
        / 1_000_000
        * 100
    ))
    actual_cents = reviews_module.compute_worst_case_reservation_usd_cents()
    if actual_cents != expected_cents:
        failures.append(
            f"[9c] the reservation must price every attempt the pass can make: "
            f"expected {expected_cents} cents, got {actual_cents}"
        )


def test_no_caller_can_ask_a_model_for_more_than_it_declares(failures: list[str]) -> None:
    """Issue #658 fix round 1. The first cut of this issue let a caller pass
    `max_output_tokens` and RAISED the widening ceiling to it
    (`output_budget_ceiling = max(output_budget_ceiling, max_output_tokens)`),
    so an explicit 200,000 against a model declaring 128,000 came back as
    200,000 -- the single path that could breach the acceptance criterion
    "the budget never exceeds the selected model's declared
    `max_output_tokens`". Nothing in the repo passed one, so the branch was
    green forever without ever being run. Both passes now size the budget
    themselves and the parameter is gone, which makes the cap structural
    rather than a convention every caller has to keep.
    """
    for fn, name in (
        (pp.run_primary_pass, "primary_review_pass.run_primary_pass"),
        (cp.run_critic_pass, "critic_review_pass.run_critic_pass"),
    ):
        if "max_output_tokens" in inspect.signature(fn).parameters:
            failures.append(
                f"[10a] {name} takes a caller-supplied max_output_tokens again -- an "
                f"explicit budget can then exceed the model's declared cap, and no "
                f"caller in this repo exercises it"
            )

    # And the widening itself clamps, whatever budget it is handed.
    cap = model_client.openrouter_model_max_output_tokens(_DECLARING_MODEL_ID)
    if model_client.widen_output_budget(cap * 2, cap) > cap:
        failures.append(
            f"[10b] widen_output_budget must clamp to the declared cap even when handed "
            f"a larger current budget; {cap * 2} -> "
            f"{model_client.widen_output_budget(cap * 2, cap)}"
        )


def test_the_documented_under_reservation_matches_the_arithmetic(
    failures: list[str],
) -> None:
    """Issue #658 fix round 1: the reservation prices the FAIL-CLOSED output
    ceiling because no document exists at submission time, while the sizing
    function asks a declaring model for its full cap. So the reservation is
    deliberately NOT an upper bound on settle -- and a residual that is
    documented with the wrong magnitude is the same as one that is hidden.

    The arithmetic is asserted first and the prose second: the phrases below
    are only forbidden while the SHIPPED configuration actually falsifies
    them, so raising the fail-closed default until it covers every declared
    cap would make this test go quiet rather than fail.
    """
    policy = _policy()
    entries = list((policy.get("models") or {}).values()) + list(policy.get("selectable") or [])
    declared = [
        int(entry["max_output_tokens"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("max_output_tokens")
    ]
    if not declared:
        failures.append("[11a] setup: no model in the shipped policy declares max_output_tokens")
        return
    largest = max(declared)
    reserved_tokens = reviews_module.MAX_OUTPUT_TOKENS

    arch = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    if largest <= reserved_tokens:
        return  # the reservation really does bound settle; nothing to police

    attempts = (
        1
        + reviews_module.MAX_RETRIES_PER_PASS
        + reviews_module.MAX_TRUNCATION_RETRIES_PER_PASS
    )

    def _usd(output_tokens: int) -> float:
        primary = (
            reviews_module.MAX_INPUT_TOKENS * reviews_module.PRIMARY_INPUT_RATE_USD_PER_MILLION
            + output_tokens * reviews_module.PRIMARY_OUTPUT_RATE_USD_PER_MILLION
        ) / 1_000_000
        critic = (
            reviews_module.MAX_INPUT_TOKENS * reviews_module.CRITIC_INPUT_RATE_USD_PER_MILLION
            + output_tokens * reviews_module.CRITIC_OUTPUT_RATE_USD_PER_MILLION
        ) / 1_000_000
        return attempts * (primary + critic)

    for label, figure in (
        ("reserved", f"${_usd(reserved_tokens):.2f}"),
        ("worst-case actual", f"${_usd(largest):.2f}"),
    ):
        if figure not in arch:
            failures.append(
                f"[11b] ARCHITECTURE.md must state the measured {label} cost {figure} per "
                f"review: the reservation prices {reserved_tokens} output tokens while the "
                f"shipped policy lets an attempt ask for {largest}"
            )

    for phrase in (
        "provably ≥ worst-case settle",
        "cannot overshoot it",
        "worst-case-upper-bound",
    ):
        if phrase in arch:
            failures.append(
                f"[11c] ARCHITECTURE.md still claims the reservation bounds settle "
                f"({phrase!r}), which this configuration falsifies by "
                f"{_usd(largest) / _usd(reserved_tokens):.2f}x"
            )


TESTS = [
    test_a_short_and_a_long_document_get_materially_different_budgets,
    test_the_budget_matches_the_documented_formula,
    test_an_eighty_page_document_reaches_the_declared_cap,
    test_a_model_that_declares_no_cap_fails_closed,
    test_the_critic_pass_sizes_from_the_document_too,
    test_a_schema_rejection_does_not_consume_the_truncation_retry,
    test_the_truncation_allowance_is_bounded_not_a_free_loop,
    test_the_widening_step_is_a_meaningful_jump_and_respects_the_cap,
    test_the_reservation_is_computed_from_the_sizing_function,
    test_no_caller_can_ask_a_model_for_more_than_it_declares,
    test_the_documented_under_reservation_matches_the_arithmetic,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        test(failures)
        status = "PASS" if len(failures) == before else "FAIL"
        print(f"{status}: {test.__name__}")
    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print(f"PASS: all {len(TESTS)} output-budget-sizing checks passed (issue #658).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
