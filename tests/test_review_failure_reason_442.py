#!/usr/bin/env python3
"""
Executable tests for issue #442: record WHY a review failed, not just that it
failed.

## The bug these lock down

`backend/src/pipeline_runner.py::run_real_pipeline` wraps the whole pipeline
in one deliberate catch-all (`# noqa: BLE001` -- failing closed so a review is
never wedged in PENDING/RUNNING) and used to record the CONSTANT string
`"unhandled_exception"` for every failure. The real cause reached
`logger.exception` and was then discarded.

On production 2026-08-01 a review died on `OpenRouter returned HTTP 402` --
the model account was out of credits, a two-minute admin fix -- and the UI
could only offer three guesses, none of them that one. Diagnosing it needed
shell access to a production container.

## What is asserted here

  1. `ModelInvocationError` carries the provider status as a STRUCTURED
     attribute (`.status_code`), populated by the REAL OpenRouter code path
     (driven through `invoke()` with an injected fake transport), so
     classification never depends on regex-matching a message string that
     would rot the next time the copy changes.
  2. `pipeline_runner.classify_failure_reason` maps each status to its reason
     TOKEN: 402 -> out of credits, 401/403 -> key rejected, 429 -> rate
     limited, 404/503 -> unavailable, and a provider-side context-length
     rejection to its own token.
  3. NO REGRESSION: anything unrecognised -- an unmapped status, a model
     error with no status at all, an ordinary exception from any other stage
     -- still yields `unhandled_exception`, exactly today's behavior.
  4. End to end: a 402 raised inside the `run_review` stage lands
     `reason="model_account_out_of_credits"` (and the real failing stage) on
     the reviews row via the SHARED `reviews.record_stage_failure`, with the
     terminal status the taxonomy assigns.
  5. Every token has a DELIBERATE terminal status in
     `reviews.STAGE_FAILURE_REASON_STATUS`, and every token the classifier
     can emit has user-facing prose in `frontend/src/ReviewSubmission.tsx`
     -- a token with no copy would surface to the reader as a bare
     identifier, which is the very failure this issue is about.
  6. The #425 rule holds at the boundary: no reason token contains a status
     code, an endpoint, or any other material the backend deliberately keeps
     out of user-facing strings. The number stops in the backend; only the
     token crosses.

This test MUST FAIL on the pre-fix tree (`classify_failure_reason` does not
exist; every path records `unhandled_exception`).

Fully offline -- fake transport, fake DynamoDB, no network.
Run standalone: `python3 tests/test_review_failure_reason_442.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

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

import model_client as mc  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import reviews  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000442"

# The policy-pinned primary id (model-policy/openrouter.json). Used only so
# invoke()'s runtime policy-pin assertion (issue #269) does not fire ahead of
# the transport behavior under test here.
PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"

FRONTEND_REVIEW_SUBMISSION = REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx"


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

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        return self.response

    def close(self):
        pass


class FakeReviewsTable:
    """Generic-enough SET interpreter to apply whatever UpdateExpression
    record_stage_failure emits (same shape as the fake in
    tests/test_stage_failure_taxonomy.py)."""

    def __init__(self, status: str = "PENDING"):
        self.item: dict[str, Any] = {"review_id": REVIEW_ID, "status": status}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            self.item[field] = values[val_token.strip()]


class FakePlaybooksTable:
    def __init__(self, active_hashes: dict[str, str]):
        self._active_hashes = dict(active_hashes)

    def get_item(self, Key):
        playbook_id = Key["playbook_id"]
        active_hash = self._active_hashes.get(playbook_id)
        if active_hash is None:
            return {}
        return {"Item": {"playbook_id": playbook_id, "active_release_bundle_hash": active_hash}}


class FakeDDB:
    def __init__(self, reviews_table: FakeReviewsTable):
        self._reviews = reviews_table
        self._playbooks = FakePlaybooksTable({"synthetic-generic": "hash-1"})

    def Table(self, name):
        if name == os.environ["PLAYBOOKS_TABLE"]:
            return self._playbooks
        return self._reviews


class FakeS3:
    def __init__(self, uploads: dict[str, bytes]):
        self._uploads = uploads

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self._uploads[Key])}

    def put_object(self, Bucket, Key, Body):
        pass


def _payload() -> dict[str, Any]:
    return {
        "review_id": REVIEW_ID,
        "owner_sub": "user-1",
        "playbook_id": "synthetic-generic",
        "upload_s3_key": f"uploads/user-1/{REVIEW_ID}/in.docx",
        "release_bundle_hash": "hash-1",
    }


# ---------------------------------------------------------------------------
# 1. The status reaches the exception STRUCTURALLY, from the real code path
# ---------------------------------------------------------------------------

class TestModelInvocationErrorCarriesStatusCode(unittest.TestCase):
    def _invoke_expecting_error(self, status_code: int) -> mc.ModelInvocationError:
        client = mc.OpenRouterModelClient(
            api_key="sk-test",
            base_url="https://openrouter.ai/api/v1",
            http_client=FakeHttpClient(FakeResponse(status_code)),
            max_retries=0,
            sleep_fn=lambda _seconds: None,
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="SYS",
                    user_prompt="CONFIDENTIAL clause text",
                    max_output_tokens=8000,
                )
        return ctx.exception

    def test_non_200_carries_the_status_structurally(self) -> None:
        """The whole point: classification reads `.status_code`, not the
        message. A message-parse would rot silently the next time the copy
        changes."""
        for status_code in (401, 402, 403, 404, 429, 503):
            with self.subTest(status_code=status_code):
                exc = self._invoke_expecting_error(status_code)
                self.assertEqual(exc.status_code, status_code)

    def test_context_length_rejection_carries_its_status_too(self) -> None:
        exc = self._invoke_expecting_error(413)
        self.assertIsInstance(exc, mc.ModelContextLengthExceededError)
        self.assertEqual(exc.status_code, 413)

    def test_transport_failure_carries_no_status(self) -> None:
        """No status exists for a transport-level failure -- it must be None,
        not a guess, so the classifier falls back rather than mislabelling."""

        class BoomClient:
            def post(self, url, json=None, headers=None):  # noqa: A002
                raise OSError("connection reset")

            def close(self):
                pass

        client = mc.OpenRouterModelClient(
            api_key="sk-test",
            http_client=BoomClient(),
            max_retries=0,
            sleep_fn=lambda _seconds: None,
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="SYS",
                    user_prompt="CONFIDENTIAL clause text",
                    max_output_tokens=8000,
                )
        self.assertIsNone(ctx.exception.status_code)

    def test_error_still_carries_no_prompt_or_response_substance(self) -> None:
        """model_client.py's deliberate body omission stands -- adding a
        structured status must not have widened what the message carries."""
        exc = self._invoke_expecting_error(402)
        self.assertNotIn("CONFIDENTIAL", str(exc))
        self.assertNotIn("sk-test", str(exc))


# ---------------------------------------------------------------------------
# 2 + 3. Classification, and the untouched fallback
# ---------------------------------------------------------------------------

class TestClassifyFailureReason(unittest.TestCase):
    def test_maps_each_provider_status_to_its_token(self) -> None:
        cases = {
            402: "model_account_out_of_credits",
            401: "model_key_rejected",
            403: "model_key_rejected",
            429: "model_rate_limited",
            404: "model_unavailable",
            503: "model_unavailable",
        }
        for status_code, expected in cases.items():
            with self.subTest(status_code=status_code):
                exc = mc.ModelInvocationError("opaque", status_code=status_code)
                self.assertEqual(pr.classify_failure_reason(exc), expected)

    def test_context_length_rejection_gets_its_own_token(self) -> None:
        exc = mc.ModelContextLengthExceededError("opaque", status_code=413)
        self.assertEqual(
            pr.classify_failure_reason(exc), "model_context_length_exceeded"
        )

    def test_unmapped_status_falls_back(self) -> None:
        exc = mc.ModelInvocationError("opaque", status_code=500)
        self.assertEqual(pr.classify_failure_reason(exc), "unhandled_exception")

    def test_model_error_without_a_status_falls_back(self) -> None:
        exc = mc.ModelInvocationError("opaque")
        self.assertEqual(pr.classify_failure_reason(exc), "unhandled_exception")

    def test_any_other_exception_falls_back(self) -> None:
        """A failure from a non-model stage (S3, DynamoDB, an unregistered
        playbook) must behave EXACTLY as it does today."""
        for exc in (KeyError("uploads/..."), ValueError("bad"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(pr.classify_failure_reason(exc), "unhandled_exception")


# ---------------------------------------------------------------------------
# 4. End to end through run_real_pipeline's catch-all
# ---------------------------------------------------------------------------

class TestRunRealPipelineRecordsTheClassifiedReason(unittest.TestCase):
    def _run_with_spine_raising(self, exc: BaseException) -> FakeReviewsTable:
        reviews_table = FakeReviewsTable()
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": b"PK-not-a-real-docx"})
        with patch.object(pr, "_settle_reservation"), \
             patch.object(pr, "_load_playbook_bundle", return_value={}), \
             patch.object(pr, "_bundle_with_openrouter_model_ids", side_effect=lambda b: b), \
             patch.object(pr, "_fetch_upload_bytes", return_value=b"docx"), \
             patch.object(pr.review_spine, "run_review", side_effect=exc):
            pr.run_real_pipeline(
                REVIEW_ID, _payload(),
                dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
                model_client=object(),
            )
        return reviews_table

    def test_402_records_out_of_credits_against_the_real_stage(self) -> None:
        """The production case, end to end: the row now says the account is
        out of credits, at the stage that actually failed."""
        table = self._run_with_spine_raising(
            mc.ModelInvocationError("OpenRouter returned HTTP 402.", status_code=402)
        )
        self.assertEqual(table.item["reason"], "model_account_out_of_credits")
        self.assertEqual(table.item["failing_stage"], "run_review")
        self.assertEqual(table.item["status"], "ERROR")
        self.assertNotIn(table.item["status"], ("PENDING", "RUNNING"))

    def test_context_length_rejection_lands_the_documented_oversize_status(self) -> None:
        table = self._run_with_spine_raising(
            mc.ModelContextLengthExceededError("opaque", status_code=413)
        )
        self.assertEqual(table.item["reason"], "model_context_length_exceeded")
        self.assertEqual(table.item["status"], "MANUAL_REVIEW_REQUIRED")

    def test_unclassifiable_failure_is_unchanged_from_today(self) -> None:
        table = self._run_with_spine_raising(RuntimeError("something else broke"))
        self.assertEqual(table.item["reason"], "unhandled_exception")
        self.assertEqual(table.item["failing_stage"], "run_review")
        self.assertEqual(table.item["status"], "ERROR")


# ---------------------------------------------------------------------------
# 5 + 6. The taxonomy is deliberate, the copy exists, and nothing leaks
# ---------------------------------------------------------------------------

def _classifier_tokens() -> set[str]:
    tokens = set(pr._MODEL_STATUS_FAILURE_REASONS.values())
    tokens.add("model_context_length_exceeded")
    return tokens


class TestTokenTaxonomyAndCopy(unittest.TestCase):
    def test_every_token_has_a_deliberate_terminal_status(self) -> None:
        """An entry present in the table is a decision on the record; an
        absent one is a silent default. Every #442 token is listed."""
        for token in _classifier_tokens():
            with self.subTest(token=token):
                self.assertIn(token, reviews.STAGE_FAILURE_REASON_STATUS)
                self.assertIn(
                    reviews.STAGE_FAILURE_REASON_STATUS[token],
                    reviews.REVIEW_STATUSES_TERMINAL,
                )

    def test_operator_problems_are_not_filed_as_manual_review(self) -> None:
        """Out of credits / bad key / rate limit / model gone are an ADMIN
        fix, not attorney work to queue -- they must not be reclassified as a
        manual-review outcome."""
        for token in (
            "model_account_out_of_credits",
            "model_key_rejected",
            "model_rate_limited",
            "model_unavailable",
        ):
            with self.subTest(token=token):
                self.assertEqual(reviews.STAGE_FAILURE_REASON_STATUS[token], "ERROR")

    def test_no_token_carries_a_status_code_or_endpoint(self) -> None:
        """Issue #425: the token is the ONLY thing that crosses into the UI,
        so it must contain no digits, no scheme, and no provider host."""
        for token in _classifier_tokens():
            with self.subTest(token=token):
                self.assertIsNone(re.search(r"\d", token))
                self.assertNotIn("http", token)
                self.assertNotIn("openrouter", token)
                self.assertRegex(token, r"^[a-z_]+$")

    def test_every_token_has_user_facing_prose_in_the_ui(self) -> None:
        """A token with no entry in REASON_EXPLANATIONS reaches the reader as
        a bare identifier -- which is the failure this issue exists to fix."""
        source = FRONTEND_REVIEW_SUBMISSION.read_text(encoding="utf-8")
        explanations = source.split("const REASON_EXPLANATIONS", 1)
        self.assertEqual(len(explanations), 2, "REASON_EXPLANATIONS not found in the UI")
        block = explanations[1].split("const STAGE_EXPLANATIONS", 1)[0]
        for token in _classifier_tokens():
            with self.subTest(token=token):
                self.assertIn(f"{token}: {{", block)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestModelInvocationErrorCarriesStatusCode,
        TestClassifyFailureReason,
        TestRunRealPipelineRecordsTheClassifiedReason,
        TestTokenTaxonomyAndCopy,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
