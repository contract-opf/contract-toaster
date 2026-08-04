#!/usr/bin/env python3
"""
Executable tests for issue #527: reasoning-class OpenRouter models (Kimi K3,
Gemini 3.1 Pro) fail EVERY review because `max_tokens` is a COMBINED budget
across `reasoning` and `content` tokens, and `OpenRouterModelClient.invoke`
used to hand back a null/truncated `content` to its caller instead of
failing closed.

## What is asserted here

  1. `openrouter_reasoning_max_tokens` reads the per-model allowance from
     model-policy/openrouter.json and defaults to 0 for a model the policy
     names none for.
  2. `OpenRouterModelClient.invoke` sends `max_tokens` = the caller's
     `max_output_tokens` PLUS that model's reasoning allowance -- added on
     top, never carved out -- and every model with a 0 allowance (every
     currently-known non-reasoning model, and an unlisted override id)
     keeps sending the EXACT `max_tokens` value it sent before this issue
     (the prompt-fixture guarantee).
  3. A 200 response with `content: null`/`""` raises
     `ModelEmptyContentError` -- never returns `None` to the caller.
  4. A 200 response with `finish_reason: "length"` raises
     `ModelOutputTruncatedError`, DISTINCT from `ModelEmptyContentError` --
     including when content also happens to be empty, truncation wins.
  5. Both new exceptions classify to their own `classify_failure_reason`
     tokens (`model_empty_content`, `model_output_truncated`), each with a
     deliberate `ERROR` terminal status and user-facing prose in
     `frontend/src/ReviewSubmission.tsx`'s `REASON_EXPLANATIONS`.
  6. `reviews.record_stage_failure` stamps `primary_model_id` /
     `critic_model_id` onto an ERROR row when passed `model_ids`, and stays
     byte-identical (writes neither field) when it is not.
  7. `scripts/primary_review_pass.py::_extract_json_object` raises the named
     `ModelResponseContractViolation` for a non-string input instead of a
     bare `AttributeError`, and `validate_model_response` turns that into an
     `invalid_response_contract` failure message rather than crashing the
     caller.

This test MUST FAIL on the pre-fix tree (`ModelEmptyContentError` /
`ModelOutputTruncatedError` / `ModelResponseContractViolation` do not exist;
`invoke()` returns `None` verbatim on empty content; `record_stage_failure`
never writes model ids).

Fully offline -- fake transport, fake DynamoDB, no network.
Run standalone: `python3 tests/test_reasoning_budget_527.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

from unittest.mock import patch  # noqa: E402

import model_client as mc  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import reviews  # noqa: E402

PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"  # reasoning_max_tokens: 0
KIMI_MODEL_ID = "moonshotai/kimi-k3"  # reasoning_max_tokens: > 0
GEMINI_MODEL_ID = "google/gemini-3.1-pro-preview"  # reasoning_max_tokens: > 0

FRONTEND_REVIEW_SUBMISSION = REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx"

NEW_TOKENS = ("model_empty_content", "model_output_truncated")


# ---------------------------------------------------------------------------
# Offline fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response

    def close(self):
        pass


def _client(http: FakeHttpClient) -> mc.OpenRouterModelClient:
    return mc.OpenRouterModelClient(
        api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None,
    )


def _choice_response(status: int = 200, *, content: Any = "{}", finish_reason: str | None = "stop") -> FakeResponse:
    message: dict[str, Any] = {"content": content}
    choice: dict[str, Any] = {"message": message}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return FakeResponse(status, {"choices": [choice], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


# ---------------------------------------------------------------------------
# 1 + 2. The reasoning-budget lookup and its effect on the request
# ---------------------------------------------------------------------------


class TestReasoningAllowanceLookup(unittest.TestCase):
    def test_kimi_and_gemini_have_a_nonzero_allowance(self) -> None:
        self.assertGreater(mc.openrouter_reasoning_max_tokens(KIMI_MODEL_ID), 0)
        self.assertGreater(mc.openrouter_reasoning_max_tokens(GEMINI_MODEL_ID), 0)

    def test_every_other_known_model_defaults_to_zero(self) -> None:
        policy = mc.load_openrouter_policy()
        non_reasoning_ids = [PRIMARY_MODEL_ID, policy["models"]["critic"]["model_id"]] + [
            e["model_id"]
            for e in policy["selectable"]
            if e["model_id"] not in (KIMI_MODEL_ID, GEMINI_MODEL_ID)
        ]
        for model_id in non_reasoning_ids:
            with self.subTest(model_id=model_id):
                self.assertEqual(mc.openrouter_reasoning_max_tokens(model_id), 0)

    def test_an_unlisted_override_id_defaults_to_zero(self) -> None:
        self.assertEqual(mc.openrouter_reasoning_max_tokens("some/unlisted-model"), 0)


class TestReasoningAllowanceAppliedToRequest(unittest.TestCase):
    def test_reasoning_model_gets_allowance_added_on_top(self) -> None:
        http = FakeHttpClient(_choice_response())
        allowance = mc.openrouter_reasoning_max_tokens(KIMI_MODEL_ID)
        with patch.dict("os.environ", {}, clear=True):
            _client(http).invoke(
                model_id=KIMI_MODEL_ID, system_prompt="s", user_prompt="u",
                max_output_tokens=8000,
            )
        self.assertEqual(http.calls[0]["json"]["max_tokens"], 8000 + allowance)
        self.assertGreater(allowance, 0)

    def test_non_reasoning_model_request_is_byte_identical(self) -> None:
        """The prompt-fixture guarantee (amended AC3): a model with a 0
        allowance sends the EXACT max_tokens value it sent before this
        issue -- nothing added, nothing carved out."""
        http = FakeHttpClient(_choice_response())
        with patch.dict("os.environ", {}, clear=True):
            _client(http).invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u",
                max_output_tokens=8000,
            )
        self.assertEqual(http.calls[0]["json"]["max_tokens"], 8000)


# ---------------------------------------------------------------------------
# 3 + 4. Fail closed on empty content / truncation, never a bare None
# ---------------------------------------------------------------------------


class TestFailClosedOnEmptyOrTruncatedContent(unittest.TestCase):
    def test_null_content_raises_model_empty_content_error(self) -> None:
        http = FakeHttpClient(_choice_response(content=None, finish_reason="stop"))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelEmptyContentError) as ctx:
                _client(http).invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u",
                    max_output_tokens=64,
                )
        self.assertIsInstance(ctx.exception, mc.ModelInvocationError)

    def test_empty_string_content_also_raises_model_empty_content_error(self) -> None:
        http = FakeHttpClient(_choice_response(content="", finish_reason="stop"))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelEmptyContentError):
                _client(http).invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u",
                    max_output_tokens=64,
                )

    def test_finish_reason_length_raises_model_output_truncated_error(self) -> None:
        http = FakeHttpClient(_choice_response(content='{"ok', finish_reason="length"))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelOutputTruncatedError) as ctx:
                _client(http).invoke(
                    model_id=KIMI_MODEL_ID, system_prompt="s", user_prompt="u",
                    max_output_tokens=64,
                )
        self.assertIsInstance(ctx.exception, mc.ModelInvocationError)

    def test_truncation_wins_over_empty_content_when_both_are_true(self) -> None:
        """The live-probe reproduction: Kimi K3 at a tight budget returns
        finish_reason=length AND an empty content -- this must classify as
        truncation (the more specific, actionable cause), never the generic
        empty-content error."""
        http = FakeHttpClient(_choice_response(content=None, finish_reason="length"))
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelOutputTruncatedError):
                _client(http).invoke(
                    model_id=KIMI_MODEL_ID, system_prompt="s", user_prompt="u",
                    max_output_tokens=64,
                )

    def test_normal_stop_with_content_is_unaffected(self) -> None:
        http = FakeHttpClient(_choice_response(content='{"decision":"ACCEPT"}', finish_reason="stop"))
        with patch.dict("os.environ", {}, clear=True):
            out = _client(http).invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u",
                max_output_tokens=64,
            )
        self.assertEqual(out, '{"decision":"ACCEPT"}')

    def test_missing_finish_reason_key_is_not_misclassified_as_truncated(self) -> None:
        """An older/canned fixture that omits finish_reason entirely must
        not be treated as `length` -- absence is not truncation."""
        http = FakeHttpClient(FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}))
        with patch.dict("os.environ", {}, clear=True):
            out = _client(http).invoke(
                model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u",
                max_output_tokens=64,
            )
        self.assertEqual(out, "{}")


# ---------------------------------------------------------------------------
# 5. Classification + taxonomy + copy
# ---------------------------------------------------------------------------


class TestNewTokenTaxonomyAndCopy(unittest.TestCase):
    def test_empty_content_classifies_to_its_own_token(self) -> None:
        exc = mc.ModelEmptyContentError("opaque")
        self.assertEqual(pr.classify_failure_reason(exc), "model_empty_content")

    def test_truncated_classifies_to_its_own_token(self) -> None:
        exc = mc.ModelOutputTruncatedError("opaque")
        self.assertEqual(pr.classify_failure_reason(exc), "model_output_truncated")

    def test_both_tokens_are_deliberate_operator_errors(self) -> None:
        for token in NEW_TOKENS:
            with self.subTest(token=token):
                self.assertIn(token, reviews.STAGE_FAILURE_REASON_STATUS)
                self.assertEqual(reviews.STAGE_FAILURE_REASON_STATUS[token], "ERROR")

    def test_no_token_carries_a_status_code_or_endpoint(self) -> None:
        for token in NEW_TOKENS:
            with self.subTest(token=token):
                self.assertIsNone(re.search(r"\d", token))
                self.assertNotIn("http", token)
                self.assertNotIn("openrouter", token)
                self.assertRegex(token, r"^[a-z_]+$")

    def test_both_tokens_have_user_facing_prose_in_the_ui(self) -> None:
        # Cheap cross-language canary only: a source grep proves the object
        # literal key exists, not that Diagnostics renders it. The real
        # assertion -- that a run_review row carrying either token shows this
        # prose and never "the exact cause was not identified" -- lives in
        # frontend/src/__tests__/admin-diagnostics.test.tsx.
        source = FRONTEND_REVIEW_SUBMISSION.read_text(encoding="utf-8")
        explanations = source.split("const REASON_EXPLANATIONS", 1)
        self.assertEqual(len(explanations), 2, "REASON_EXPLANATIONS not found in the UI")
        block = explanations[1].split("const STAGE_EXPLANATIONS", 1)[0]
        for token in NEW_TOKENS:
            with self.subTest(token=token):
                self.assertIn(f"{token}: {{", block)


# ---------------------------------------------------------------------------
# 6. record_stage_failure stamps model ids
# ---------------------------------------------------------------------------


class FakeReviewsTable:
    def __init__(self, status: str = "RUNNING"):
        self.item: dict[str, Any] = {"review_id": "rid", "status": status}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            self.item[field] = values[val_token.strip()]


class FakeDDB:
    def __init__(self, table: FakeReviewsTable):
        self._table = table

    def Table(self, _name):
        return self._table


class TestRecordStageFailureStampsModelIds(unittest.TestCase):
    def test_model_ids_written_when_provided(self) -> None:
        table = FakeReviewsTable()
        with patch.dict("os.environ", {"REVIEWS_TABLE": "reviews-test"}, clear=False):
            reviews.record_stage_failure(
                "rid", "run_review", "model_empty_content", FakeDDB(table),
                model_ids={"primary_model_id": "anthropic/claude-opus-4.8",
                           "critic_model_id": "moonshotai/kimi-k3"},
            )
        self.assertEqual(table.item["primary_model_id"], "anthropic/claude-opus-4.8")
        self.assertEqual(table.item["critic_model_id"], "moonshotai/kimi-k3")
        self.assertEqual(table.item["reason"], "model_empty_content")

    def test_omitted_when_not_provided_byte_identical_to_before(self) -> None:
        table = FakeReviewsTable()
        with patch.dict("os.environ", {"REVIEWS_TABLE": "reviews-test"}, clear=False):
            reviews.record_stage_failure("rid", "run_review", "unhandled_exception", FakeDDB(table))
        self.assertNotIn("primary_model_id", table.item)
        self.assertNotIn("critic_model_id", table.item)

    def test_partial_model_ids_writes_only_what_is_known(self) -> None:
        """A failure before load_playbook has resolved the critic id (e.g.
        during verify_active_bundle) writes only the keys it has."""
        table = FakeReviewsTable()
        with patch.dict("os.environ", {"REVIEWS_TABLE": "reviews-test"}, clear=False):
            reviews.record_stage_failure(
                "rid", "verify_active_bundle", "unhandled_exception", FakeDDB(table),
                model_ids={},
            )
        self.assertNotIn("primary_model_id", table.item)
        self.assertNotIn("critic_model_id", table.item)


class TestRunRealPipelineCapturesModelIdsOnFailure(unittest.TestCase):
    def test_run_review_failure_after_load_playbook_still_stamps_both_ids(self) -> None:
        """End to end through run_real_pipeline's own stage sequence and
        exception handling: `review_spine.run_review` itself raises (as it
        would for a real `ModelEmptyContentError` from deep inside a pass)
        -- the ERROR row must still carry both model ids `load_playbook`
        had already resolved, not just the reason token. `run_review` is
        stubbed (rather than driven through real docx extraction) so this
        test exercises pipeline_runner's own model-id-capture wiring in
        isolation, not the whole review spine (covered elsewhere, e.g.
        tests/test_dts_pipeline_runner_real_review.py)."""
        review_id = "00000000-0000-4000-a000-000000000527"
        reviews_table = FakeReviewsTable(status="PENDING")

        class FakePlaybooksTable:
            def get_item(self, Key):
                return {"Item": {"playbook_id": Key["playbook_id"], "active_release_bundle_hash": "hash-1"}}

        class FakeDDBFull:
            def __init__(self):
                self._reviews = reviews_table
                self._playbooks = FakePlaybooksTable()

            def Table(self, name):
                if name == os.environ["PLAYBOOKS_TABLE"]:
                    return self._playbooks
                return self._reviews

        class FakeS3:
            def get_object(self, Bucket, Key):
                import io
                return {"Body": io.BytesIO(b"docx-bytes")}

            def put_object(self, Bucket, Key, Body):
                pass

        class InertModelClient:
            def close(self):
                pass

        payload = {
            "review_id": review_id,
            "owner_sub": "user-1",
            "playbook_id": "synthetic-generic",
            "upload_s3_key": f"uploads/user-1/{review_id}/in.docx",
            "release_bundle_hash": "hash-1",
        }

        with patch.dict("os.environ", {}, clear=True):
            os.environ["REVIEWS_TABLE"] = "reviews-test"
            os.environ["UPLOADS_BUCKET"] = "uploads-test"
            os.environ["OUTPUTS_BUCKET"] = "outputs-test"
            os.environ["PLAYBOOKS_TABLE"] = "playbooks-test"
            with patch.object(pr, "_settle_reservation"), \
                 patch.object(pr, "_load_playbook_bundle", return_value={"playbook": {"metadata": {}}}), \
                 patch.object(
                     pr, "_bundle_with_openrouter_model_ids",
                     side_effect=lambda bundle, dynamodb_resource=None: {
                         "playbook": {"metadata": {
                             "primary_model_id": PRIMARY_MODEL_ID,
                             "critic_model_id": KIMI_MODEL_ID,
                         }}
                     },
                 ), \
                 patch.object(pr.review_spine, "run_review", side_effect=mc.ModelEmptyContentError("empty")):
                pr.run_real_pipeline(
                    review_id, payload,
                    dynamodb_resource=FakeDDBFull(), s3_client=FakeS3(),
                    model_client=InertModelClient(),
                )

        self.assertEqual(reviews_table.item["reason"], "model_empty_content")
        self.assertEqual(reviews_table.item["status"], "ERROR")
        self.assertEqual(reviews_table.item["primary_model_id"], PRIMARY_MODEL_ID)
        self.assertEqual(reviews_table.item["critic_model_id"], KIMI_MODEL_ID)


# ---------------------------------------------------------------------------
# 7. _extract_json_object / validate_model_response contract violation
# ---------------------------------------------------------------------------


class TestModelResponseContractViolation(unittest.TestCase):
    def test_extract_json_object_raises_named_error_for_non_string(self) -> None:
        with self.assertRaises(pp.ModelResponseContractViolation):
            pp._extract_json_object(None)  # type: ignore[arg-type]

    def test_extract_json_object_never_raises_a_bare_attribute_error(self) -> None:
        try:
            pp._extract_json_object(None)  # type: ignore[arg-type]
        except pp.ModelResponseContractViolation:
            pass
        except AttributeError:
            self.fail("a bare AttributeError leaked instead of the named contract violation")

    def test_validate_model_response_reports_contract_violation_not_a_crash(self) -> None:
        is_valid, error = pp.validate_model_response(None)  # type: ignore[arg-type]
        self.assertFalse(is_valid)
        self.assertIn("invalid_response_contract", error)

    def test_validate_model_response_normal_string_input_is_unaffected(self) -> None:
        is_valid, _parsed_or_error = pp.validate_model_response(
            '{"decision":"ACCEPT","confidence_state":"OK","issues":[]}'
        )
        self.assertTrue(is_valid)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestReasoningAllowanceLookup,
        TestReasoningAllowanceAppliedToRequest,
        TestFailClosedOnEmptyOrTruncatedContent,
        TestNewTokenTaxonomyAndCopy,
        TestRecordStageFailureStampsModelIds,
        TestRunRealPipelineCapturesModelIdsOnFailure,
        TestModelResponseContractViolation,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
