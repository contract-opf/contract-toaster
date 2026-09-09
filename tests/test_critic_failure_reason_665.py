#!/usr/bin/env python3
"""
Issue #665: a critic-pass failure must record a reason TOKEN, not a stage name.

## What was actually broken

`scripts/review_spine.py` composed the two passes and then, on the critic
branch, wrote `two_pass["stage"]` -- the literal string `"critic"` -- into
the result's `reason` field:

    return _terminal(status=two_pass["status"], reason=two_pass.get("stage"), ...)

`frontend/src/ReviewSubmission.tsx::explainFailure` looks `reason` up in
`REASON_EXPLANATIONS`, whose keys are issue-#442 reason tokens
(`model_output_truncated`, `leakage_detected`, ...). `"critic"` is not one,
so the lookup missed and the vague `STAGE_EXPLANATIONS.run_review` fallback
rendered instead -- "The exact cause was not identified." Every critic-branch
failure landed in that bucket permanently.

A real paid production review (2026-09-02, 278s wall clock) failed exactly
that way. `GET /api/admin/diagnostics/recent-failures` returned
`{"failing_stage": "run_review", "reason": "critic", ...}` and the tab built
to answer "why did this fail?" could not.

## What this file proves

  1. A critic pass that exhausted its bounded retry stores a token, never
     the stage name -- and CLASSIFIED from its own `last_error`, so a critic
     whose JSON the contract rejected (`critic_schema_invalid`, the class
     issue #673 measured in live traffic) is told apart from one whose body
     was not readable as JSON at all (`critic_invalid_json`). Only the
     fixed-vocabulary token half of `last_error` is read
     (`primary_review_pass._error_token`), never the ": detail" remainder.
  2. A critic pass that refused an oversized prompt keeps its OWN token
     (`document_too_large`), which the composition used to discard. So the
     reachable critic terminals are DISTINGUISHABLE, which is the property
     the issue asks for.
  3. Truncation -- the critic failure class that never reaches this branch
     at all -- is distinguishable too, and was already: `run_critic_pass`
     RE-RAISES
     `ModelOutputTruncatedError` once the truncation allowance is spent, so
     it never reaches the critic branch at all and
     `pipeline_runner.classify_failure_reason` records it as
     `model_output_truncated`. Asserted here so nobody "fixes" the gap by
     minting a `critic_output_truncated` token no production path can write.
  4. End to end through `review_spine.run_review`: the stored `reason` is
     the token and the critic's spent RETRY BUDGET rides along as
     `critic_attempts` -- computed by the pass, persisted onto the reviews
     row by `pipeline_runner._write_real_terminal`, and served by
     `reviews._RECENT_FAILURE_FIELDS`. `run_critic_pass` reports
     `attempts_allowed`, the whole budget rather than the attempt it stopped
     on, so the two values a row can carry are the baseline
     (`1 + MAX_RETRIES_PER_PASS`) and a baseline widened by a truncation
     grant. Both are pinned here, because three comments in the shipped code
     now tell an operator to read the count that way.
  5. THE CLASS OF BUG, not just this instance: every reason token the review
     path can put on a result has reader-facing copy, and none of them is a
     pipeline stage name. The token vocabulary is collected from the real
     module sources by AST walk (every `{"status": ..., "reason": ...}`
     terminal dict and every `_terminal(reason=...)` call), so the next token
     someone adds is checked without anyone remembering to list it here.

## Fixture fidelity

Every critic result fed into `reconciliation.run_two_pass_review` here is
produced by the REAL `critic_review_pass.run_critic_pass` -- driven with the
shipped `FakeBedrockClient` over the real on-disk playbook and the real
output schema -- never a hand-written dict of the shape production is assumed
to write. That matters for this issue specifically: the terminal shape the
composition reads (`attempts` / `last_error` / `reason`) is exactly what a
hand-built dict would have gotten subtly wrong, and `document_too_large`
would never have been noticed as a second reachable branch at all.

The schema-invalid critic body is the shipped valid fixture with
`confidence_state` set to `"medium"` -- the natural-language word a real
model actually returned (see tests/test_primary_pass_retry_recovery.py) --
so the rejection comes from the real validator against the real schema.

Run standalone: `python3 tests/test_critic_failure_reason_665.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import ast
import json
import os
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

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import critic_review_pass as cp  # noqa: E402
import model_client  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import reconciliation  # noqa: E402
import redline_block_apply  # noqa: E402
import review_spine  # noqa: E402
import synthetic_form_paragraphs as sfp_module  # noqa: E402

try:  # reviews.py prefers the `src.` package form when it is importable
    from src import reviews as reviews_module  # type: ignore
except ImportError:  # pragma: no cover - the sys.path form the other tests use
    import reviews as reviews_module  # type: ignore

from test_review_spine import (  # noqa: E402
    _build_draft_docx,
    _load_bundle,
    _primary_accept_response,
)

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
FRONTEND_REVIEW_SUBMISSION = REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx"

REVIEW_ID = "00000000-0000-4000-a000-000000000665"

# The stage name that used to be stored in `reason`. Every test that uses it
# first asserts `run_two_pass_review` really did label the branch with it, so
# a rename in the producer fails the setup guard here rather than leaving the
# assertions quietly comparing against a string nothing emits any more.
_CRITIC_STAGE = "critic"

# The modules on the review path that can put a `reason` on a result
# `review_spine.run_review` returns.
_REASON_PRODUCING_MODULES = (
    "review_spine.py",
    "primary_review_pass.py",
    "critic_review_pass.py",
    "redline_generate.py",
)


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _critic_model_id() -> str:
    return _playbook()["playbook"]["metadata"]["critic_model_id"]


def _valid_critic_body() -> dict[str, Any]:
    return json.loads(
        (MODEL_RESPONSES_DIR / "critic_no_delta_accept_valid.json").read_text(encoding="utf-8")
    )


def _schema_invalid_critic_body() -> str:
    """The shipped valid critic response with the ONE field a real model got
    wrong in production: `confidence_state` as a natural-language word rather
    than the enum. Rejected by the real validator against the real schema."""
    body = _valid_critic_body()
    body["confidence_state"] = "medium"
    return json.dumps(body)


def _unreadable_critic_body() -> str:
    """Not JSON at all -- the prose class `_extract_json_object` exists to
    unwrap and cannot, so the real validator rejects it as `invalid_json`."""
    return "I am not able to review this document."


class TruncatingClient:
    """Every attempt truncates -- the shape OpenRouter reports as
    `finish_reason='length'` on an HTTP 200 (see
    `model_client.ModelOutputTruncatedError` and
    tests/test_output_budget_sizing_658.py's own double)."""

    def __init__(self) -> None:
        self.calls = 0
        # Capabilities are delegated to the shipped double rather than
        # hand-rolled, so this client cannot accidentally grant (or deny in a
        # different SHAPE than) what the real clients report -- the pass reads
        # them to decide whether to ask the provider to enforce the schema.
        self._capabilities = model_client.FakeBedrockClient({})

    def capabilities(self, model_id: str) -> dict[str, bool]:
        return self._capabilities.capabilities(model_id)

    def invoke(self, **_kwargs: Any) -> str:
        self.calls += 1
        raise model_client.ModelOutputTruncatedError(
            "OpenRouter truncated the response before it finished "
            "(finish_reason='length', HTTP 200).",
            status_code=200,
        )


class TruncatesOnceThenSchemaInvalidClient:
    """Truncates the FIRST attempt, then answers schema-invalid forever.

    This is the only shape that raises `attempts_allowed` above its baseline:
    `run_critic_pass` spends a `MAX_TRUNCATION_RETRIES_PER_PASS` grant, adds
    one attempt to the budget, and re-asks with a widened output budget --
    then exhausts the widened budget on schema failures instead of raising.
    It is what makes a `critic_attempts` ABOVE baseline a real, producible
    row value rather than a hypothetical one.
    """

    def __init__(self) -> None:
        self.calls = 0
        self._delegate = model_client.FakeBedrockClient({})

    def capabilities(self, model_id: str) -> dict[str, bool]:
        return self._delegate.capabilities(model_id)

    def invoke(self, **_kwargs: Any) -> str:
        self.calls += 1
        if self.calls == 1:
            raise model_client.ModelOutputTruncatedError(
                "OpenRouter truncated the response before it finished "
                "(finish_reason='length', HTTP 200).",
                status_code=200,
            )
        return _schema_invalid_critic_body()


def _run_real_critic(**overrides: Any) -> dict[str, Any]:
    """The REAL critic pass, over the real playbook and the real schema."""
    critic_id = _critic_model_id()
    kwargs: dict[str, Any] = {
        "review_id": REVIEW_ID,
        "primary_output": json.loads(_primary_accept_response()),
        "playbook": _playbook(),
        "model_id": critic_id,
        "ledger_write": [].append,
        "doc_text": "## Term\nThis agreement runs for two years.",
    }
    kwargs.setdefault(
        "model_client",
        model_client.FakeBedrockClient({critic_id: [_schema_invalid_critic_body()] * 8}),
    )
    kwargs.update(overrides)
    return cp.run_critic_pass(**kwargs)


class FakeReviewsTable:
    """The same generic SET interpreter tests/test_review_failure_reason_472.py
    uses, so the assertion is about what `_write_real_terminal` actually
    emits rather than about a bespoke recorder."""

    def __init__(self, status: str = "RUNNING") -> None:
        self.item: dict[str, Any] = {"review_id": REVIEW_ID, "status": status}

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


# ---------------------------------------------------------------------------
# 1. The two reachable critic terminals get two different tokens, and neither
#    is the stage name.
# ---------------------------------------------------------------------------


def test_a_retry_exhausted_critic_stores_a_token_not_the_stage_name(
    failures: list[str],
) -> None:
    """The critic here answers with JSON the real schema rejects, so the
    classified token is the `schema_invalid` one -- the class issue #673
    measured against live OpenRouter traffic (`backend/src/config.py::
    structured_output_enabled`)."""
    critic_result = _run_real_critic()
    if critic_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[1a] setup: the real critic pass should exhaust its retries on a "
            f"schema-invalid body; got {critic_result.get('status')!r}"
        )
        return
    two_pass = reconciliation.run_two_pass_review(
        primary_pass_result={"status": "OK", "response": _valid_critic_body()},
        critic_pass_result=critic_result,
    )
    if two_pass.get("stage") != _CRITIC_STAGE:
        failures.append(
            f"[1b] setup: expected the critic branch; got stage={two_pass.get('stage')!r}"
        )
        return
    reason = review_spine.critic_failure_reason(two_pass)
    if reason == _CRITIC_STAGE:
        failures.append(
            "[1c] the STAGE NAME is being stored as the reason -- this is the bug: "
            "REASON_EXPLANATIONS has no such key, so the reader gets "
            "'the exact cause was not identified'"
        )
    if reason != review_spine.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            f"[1d] a critic whose JSON the output contract rejected must record "
            f"{review_spine.REASON_CRITIC_SCHEMA_INVALID!r}; got {reason!r}"
        )
    if not str(critic_result.get("last_error", "")).startswith("schema_invalid:"):
        failures.append(
            f"[1e] setup: the classification is only meaningful if the real pass "
            f"really did record a `schema_invalid` last_error; got "
            f"{critic_result.get('last_error')!r}"
        )


def test_an_unreadable_critic_is_a_different_token_from_a_schema_invalid_one(
    failures: list[str],
) -> None:
    """The two bounded-retry classes are materially different operator
    diagnoses -- `schema_invalid` is the #673 structured-output lead,
    `invalid_json` is the prose/fence one -- so they must not collapse into a
    single "it kept returning something unreadable"."""
    critic_id = _critic_model_id()
    critic_result = _run_real_critic(
        model_client=model_client.FakeBedrockClient(
            {critic_id: [_unreadable_critic_body()] * 8}
        )
    )
    if not str(critic_result.get("last_error", "")).startswith("invalid_json:"):
        failures.append(
            f"[1f] setup: the real validator should reject a non-JSON body as "
            f"`invalid_json`; got {critic_result.get('last_error')!r}"
        )
        return
    two_pass = reconciliation.run_two_pass_review(
        primary_pass_result={"status": "OK", "response": _valid_critic_body()},
        critic_pass_result=critic_result,
    )
    reason = review_spine.critic_failure_reason(two_pass)
    if reason != review_spine.REASON_CRITIC_INVALID_JSON:
        failures.append(
            f"[1g] an unreadable critic body must record "
            f"{review_spine.REASON_CRITIC_INVALID_JSON!r}; got {reason!r}"
        )
    if reason == review_spine.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            "[1h] the two bounded-retry classes collapsed into one token -- the "
            "next occurrence is no more diagnosable than 'it kept returning "
            "something unreadable'"
        )
    # The ": detail" remainder never crosses: only the token half is read.
    detail = str(critic_result.get("last_error", "")).split(":", 1)[1]
    if detail.strip() and detail.strip() in reason:
        failures.append(
            f"[1i] the reason token must be fixed vocabulary, never the validator's "
            f"own message; {reason!r} carries it"
        )

    # The RESIDUAL. `run_two_pass_review` documents a `critic_pass_result is
    # None` branch, which carries neither a reason nor a `last_error`; a
    # `last_error` the map does not name lands the same way. The point of the
    # fallback is that `reason` is never empty on this branch -- an empty one
    # would miss `REASON_EXPLANATIONS` exactly as "critic" did.
    residual = review_spine.critic_failure_reason(
        reconciliation.run_two_pass_review(
            primary_pass_result={"status": "OK", "response": _valid_critic_body()},
            critic_pass_result=None,
        )
    )
    if residual != review_spine.REASON_CRITIC_RETRY_EXHAUSTED:
        failures.append(
            f"[1j] an unclassifiable critic terminal must fall back to "
            f"{review_spine.REASON_CRITIC_RETRY_EXHAUSTED!r}, never to an empty "
            f"reason; got {residual!r}"
        )


def test_an_oversized_critic_prompt_keeps_its_own_token(failures: list[str]) -> None:
    """`run_critic_pass`'s oversized-prompt gate already names its cause. The
    composition used to throw it away, so the one critic failure that HAD a
    diagnosis arrived indistinguishable from the ones that did not -- and the
    critic prompt is the larger of the two, so it is the gate more likely to
    fire."""
    critic_result = _run_real_critic(max_input_tokens=1)
    if critic_result.get("reason") != "document_too_large":
        failures.append(
            f"[2a] setup: the real oversize gate should refuse before any model call; "
            f"got {critic_result!r}"
        )
        return
    two_pass = reconciliation.run_two_pass_review(
        primary_pass_result={"status": "OK", "response": _valid_critic_body()},
        critic_pass_result=critic_result,
    )
    reason = review_spine.critic_failure_reason(two_pass)
    if reason == _CRITIC_STAGE:
        failures.append("[2b] the stage name is still being stored as the reason")
    if reason != "document_too_large":
        failures.append(
            f"[2c] the critic pass's own token must survive the composition; got {reason!r}"
        )

    other = review_spine.critic_failure_reason(
        reconciliation.run_two_pass_review(
            primary_pass_result={"status": "OK", "response": _valid_critic_body()},
            critic_pass_result=_run_real_critic(),
        )
    )
    if reason == other:
        failures.append(
            f"[2d] an oversized critic and a retry-exhausted critic must be "
            f"DISTINGUISHABLE; both stored {reason!r}"
        )


def test_a_truncated_critic_is_distinguishable_without_a_token_of_its_own(
    failures: list[str],
) -> None:
    """The third critic failure class. It never reaches the critic branch:
    `run_critic_pass` re-raises once the truncation allowance is spent, so
    the exception leaves the spine and `classify_failure_reason` records
    `model_output_truncated`. Pinned here so the gap is not "closed" with a
    `critic_output_truncated` token nothing can ever write."""
    client = TruncatingClient()
    raised: BaseException | None = None
    result: Any = None
    try:
        result = _run_real_critic(model_client=client)
    except model_client.ModelOutputTruncatedError as exc:
        raised = exc

    if raised is None:
        failures.append(
            f"[3a] an unrecoverable critic truncation must still reach the caller as an "
            f"exception (tests/test_output_budget_sizing_658.py pins the same contract "
            f"for the primary pass); got {result!r}"
        )
        return
    classified = pr.classify_failure_reason(raised)
    if classified != "model_output_truncated":
        failures.append(
            f"[3b] a truncated critic must classify as 'model_output_truncated'; "
            f"got {classified!r}"
        )
    critic_branch_tokens = {
        review_spine.REASON_CRITIC_RETRY_EXHAUSTED,
        *review_spine._CRITIC_LAST_ERROR_REASONS.values(),
    }
    if classified in critic_branch_tokens:
        failures.append(
            "[3c] a truncated critic and a bounded-retry critic must not collapse "
            "into one token"
        )


# ---------------------------------------------------------------------------
# 2. End to end: the stored reason, and the attempt count that survives with
#    it all the way to the Diagnostics projection.
# ---------------------------------------------------------------------------


def _run_review_with_a_failing_critic() -> dict[str, Any]:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    client = model_client.FakeBedrockClient(
        {
            primary_id: [_primary_accept_response()],
            # Enough for every attempt the bounded retry can make; the fake
            # raises FakeBedrockClientExhausted rather than fabricating one,
            # so an undercount would fail loudly instead of passing.
            critic_id: [_schema_invalid_critic_body()] * 8,
        }
    )
    return review_spine.run_review(
        _build_draft_docx(sfp_module, {}), bundle, client, review_id=REVIEW_ID
    )


def test_run_review_stores_the_token_and_the_critic_attempt_count(
    failures: list[str],
) -> None:
    result = _run_review_with_a_failing_critic()
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[4a] setup: a failing critic is terminal (never a silent single-pass "
            f"DONE); got {result.get('status')!r}"
        )
        return
    if result.get("reason") == _CRITIC_STAGE:
        failures.append(
            "[4b] run_review still stores the stage name in `reason` -- this is the "
            "field the reader-facing copy is keyed by"
        )
    if result.get("reason") != review_spine.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            f"[4c] expected {review_spine.REASON_CRITIC_SCHEMA_INVALID!r}; "
            f"got {result.get('reason')!r}"
        )
    attempts = result.get("critic_attempts")
    if not isinstance(attempts, int) or attempts < 1:
        failures.append(
            f"[4d] the critic's spent retry budget must survive onto the result -- "
            f"it is what tells an output-truncation run apart from a plainly "
            f"unreadable one; got {attempts!r}"
        )
    elif attempts != 1 + cp.MAX_RETRIES_PER_PASS:
        failures.append(
            f"[4e] `run_critic_pass` reports `attempts_allowed`, the WHOLE budget, "
            f"so a run with no truncation grant reads exactly "
            f"{1 + cp.MAX_RETRIES_PER_PASS} -- the BASELINE the shipped comments in "
            f"review_spine.py, reviews.py and AdminDiagnostics.tsx tell an operator "
            f"to read it against; got {attempts}"
        )


def test_a_truncation_grant_is_what_raises_the_recorded_budget(
    failures: list[str],
) -> None:
    """The only producible value ABOVE the baseline, and therefore the only
    thing the persisted count actually distinguishes. Three shipped comments
    say so; this is what makes that claim true rather than plausible."""
    client = TruncatesOnceThenSchemaInvalidClient()
    critic_result = _run_real_critic(model_client=client)
    if critic_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[8a] setup: a truncation grant must be spent on a RETRY, not on "
            f"turning the terminal into something else; got {critic_result!r}"
        )
        return
    baseline = 1 + cp.MAX_RETRIES_PER_PASS
    expected = baseline + cp.MAX_TRUNCATION_RETRIES_PER_PASS
    if critic_result.get("attempts") != expected:
        failures.append(
            f"[8b] one truncation grant must raise the reported budget from "
            f"{baseline} to {expected}; got {critic_result.get('attempts')!r}"
        )
    if critic_result.get("attempts") == baseline:
        failures.append(
            "[8c] the count reads the same with and without a truncation grant, so "
            "it distinguishes nothing -- three shipped comments claim it does"
        )


def test_the_attempt_count_reaches_the_row_and_the_diagnostics_projection(
    failures: list[str],
) -> None:
    result = _run_review_with_a_failing_critic()
    table = FakeReviewsTable()
    pr._write_real_terminal(REVIEW_ID, result, None, FakeDDB(table))

    if table.item.get("reason") != review_spine.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            f"[5a] the row must carry the token; got {table.item.get('reason')!r}"
        )
    if table.item.get("critic_attempts") != result.get("critic_attempts"):
        failures.append(
            f"[5b] the attempt count must land on the reviews ROW -- "
            f"`reviews.list_recent_failures` never opens analysis.json, so detail "
            f"that lives only in the artifact is detail no operator can reach; "
            f"row has {table.item.get('critic_attempts')!r}"
        )
    if "critic_attempts" not in reviews_module._RECENT_FAILURE_FIELDS:
        failures.append(
            "[5c] the Diagnostics projection is an allowlist -- a field missing from "
            "_RECENT_FAILURE_FIELDS never leaves the backend"
        )
    if "critic_attempts" not in pr._ANALYSIS_FIELDS:
        failures.append("[5d] the analysis artifact must keep the count too")

    # A review that did NOT fail on the critic must not grow the field.
    clean = FakeReviewsTable()
    pr._write_real_terminal(
        REVIEW_ID, {"status": "OK", "decision": "ACCEPT"}, None, FakeDDB(clean)
    )
    if "critic_attempts" in clean.item:
        failures.append(
            "[5e] absent, never a null placeholder, on every other review -- same "
            "convention as normalization_notes and the leakage fields"
        )


# ---------------------------------------------------------------------------
# 3. The CLASS of bug: no stage name in `reason`, and no token without copy.
# ---------------------------------------------------------------------------


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return constants


def _literal_reason(value: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name) and value.id in constants:
        return constants[value.id]
    return None


def _result_reason_tokens() -> set[str]:
    """Every reason token the review path can put on a RESULT, collected from
    the real module sources rather than hand-listed here.

    Two shapes carry one: a terminal dict literal with both a `"status"` and
    a `"reason"` key (`primary_review_pass`, `critic_review_pass`,
    `redline_generate`), and a `_terminal(reason=...)` call
    (`review_spine`). Values that are not literals are pass-through points --
    `two_pass.get('stage')` was one of them, and was this issue's bug -- so
    the two that remain are added explicitly below with their producer named.
    """
    tokens: set[str] = set()
    for module_name in _REASON_PRODUCING_MODULES:
        tree = ast.parse((SCRIPTS_DIR / module_name).read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                if "status" in keys and "reason" in keys:
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == "reason":
                            literal = _literal_reason(value, constants)
                            if literal is not None:
                                tokens.add(literal)
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_terminal":
                for keyword in node.keywords:
                    if keyword.arg == "reason":
                        literal = _literal_reason(keyword.value, constants)
                        if literal is not None:
                            tokens.add(literal)

    # The two pass-through points, resolved to what actually flows through
    # them. `review_spine.critic_failure_reason` classifies the critic branch;
    # `redline_generate`'s `failure["reason"]` forwards a BATCH-level
    # compile failure, and the only compile failure built without an
    # `issue_key` (which is what makes it batch-level) is the projection gate.
    tokens.add(review_spine.REASON_CRITIC_RETRY_EXHAUSTED)
    # Read from the classifier map itself, so a class split out of the
    # bounded-retry terminal later is checked for copy without anyone
    # remembering to add it here.
    tokens.update(review_spine._CRITIC_LAST_ERROR_REASONS.values())
    tokens.add(redline_block_apply.REASON_PROJECTION_VERIFICATION_FAILED)
    return tokens


def _explanation_keys(block_marker: str, end_marker: str) -> set[str]:
    source = FRONTEND_REVIEW_SUBMISSION.read_text(encoding="utf-8")
    start = source.split(block_marker, 1)
    if len(start) != 2:
        raise AssertionError(f"{block_marker!r} not found in {FRONTEND_REVIEW_SUBMISSION}")
    block = start[1].split(end_marker, 1)[0]
    return {
        line.strip().rstrip(": {")
        for line in block.splitlines()
        if line.strip().endswith(": {") and not line.strip().startswith("//")
    }


def test_every_reason_token_the_review_path_emits_has_reader_facing_copy(
    failures: list[str],
) -> None:
    """The guard for the CLASS, not the instance: a token with no
    REASON_EXPLANATIONS entry falls through to the stage copy, which is how
    'the exact cause was not identified' reached a production operator."""
    copy_keys = _explanation_keys("REASON_EXPLANATIONS: Record", "const STAGE_EXPLANATIONS")
    for token in sorted(_result_reason_tokens()):
        if token not in copy_keys:
            failures.append(
                f"[6a] reason token {token!r} can reach a result but has no "
                f"REASON_EXPLANATIONS entry -- the reader is shown the vague stage "
                f"fallback instead of a cause"
            )


def test_no_reason_token_is_a_pipeline_stage_name(failures: list[str]) -> None:
    """The exact leak this issue fixes, stated as an invariant: `reason` and
    `failing_stage` are different vocabularies, and a value from one must
    never be stored in the other."""
    stage_names = _explanation_keys("STAGE_EXPLANATIONS: Record", "\n};")
    if "run_review" not in stage_names:
        failures.append("[7a] setup: STAGE_EXPLANATIONS keys were not parsed")
        return
    stage_names.add(_CRITIC_STAGE)
    for token in sorted(_result_reason_tokens()):
        if token in stage_names:
            failures.append(
                f"[7b] {token!r} is a pipeline STAGE name being stored as a reason "
                f"token -- exactly the confusion that made every critic failure "
                f"undiagnosable"
            )


TESTS = [
    test_a_retry_exhausted_critic_stores_a_token_not_the_stage_name,
    test_an_unreadable_critic_is_a_different_token_from_a_schema_invalid_one,
    test_an_oversized_critic_prompt_keeps_its_own_token,
    test_a_truncated_critic_is_distinguishable_without_a_token_of_its_own,
    test_run_review_stores_the_token_and_the_critic_attempt_count,
    test_a_truncation_grant_is_what_raises_the_recorded_budget,
    test_the_attempt_count_reaches_the_row_and_the_diagnostics_projection,
    test_every_reason_token_the_review_path_emits_has_reader_facing_copy,
    test_no_reason_token_is_a_pipeline_stage_name,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        # Same fault-tolerant runner as tests/test_block_ir.py and
        # tests/test_accept_all_materializer.py. It matters for THIS file's
        # own red-first check: run against a pre-fix tree the spine has no
        # `critic_failure_reason` at all, and without this an AttributeError
        # in the first test would abort the run before the behavioural
        # assertions further down (notably [4b], "run_review still stores the
        # stage name in `reason`") ever got the chance to fire. A reviewer
        # reproducing the red must see the real failures, not one traceback.
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        status = "PASS" if len(failures) == before else "FAIL"
        print(f"{status}: {test.__name__}")
    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print(f"PASS: all {len(TESTS)} critic-failure-reason checks passed (issue #665).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
