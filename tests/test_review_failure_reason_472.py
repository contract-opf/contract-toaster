#!/usr/bin/env python3
"""
Executable tests for issue #472: classify the pre-call no-key case and
timeouts, and stamp `failed_at` on every ERROR row.

## The bug this locks down

Issue #442 gave `run_real_pipeline` a controlled reason-token vocabulary for
PROVIDER-side failures (a 401/402/403/404/429/503 the provider returned), but
a brand-new adopter's single most likely first-run mistake -- uploading a
contract before an OpenRouter key is saved -- never reaches the provider at
all. `_build_openrouter_client` raised a bare `ValueError`, which
`classify_failure_reason` cannot recognise, so the row recorded the
least-informative `unhandled_exception` for the single most common cause
(live cold-start evidence, 2026-08-03, issue #472's comment). The same was
true for a request that timed out after exhausting retries: a transport
failure with no `.status_code` also fell into `unhandled_exception`.

Separately, `record_stage_failure` (issue #258) and the mock pipeline's own
`_fail_review` never stamped a failure timestamp at all -- the Diagnostics
"Failed at" column quietly read `created_at` (submission time) instead, so
two rows on a live sweep showed a blank cell where a genuine failure moment
should have been.

## What is asserted here

  1. `_build_openrouter_client` raises `model_client.ModelKeyMissingError`
     -- not a bare `ValueError`, not a live HTTP call -- when no key is
     configured, and `classify_failure_reason` maps it to `model_key_missing`,
     distinct from `model_key_rejected` (issue #442, a provider 401/403).
  2. A provider request that times out (real `OpenRouterModelClient.invoke`,
     driven through an injected fake transport that raises `httpx.
     ReadTimeout`) raises `model_client.ModelTimeoutError`, classified as
     `model_timeout` -- distinct from a generic transport failure (still
     `unhandled_exception`, unchanged from #442).
  3. Both new tokens have a DELIBERATE terminal status in
     `reviews.STAGE_FAILURE_REASON_STATUS` (`ERROR` -- an admin fix, never
     attorney work, same reasoning as every other #442 operator token) and
     user-facing prose in `frontend/src/ReviewSubmission.tsx`'s
     `REASON_EXPLANATIONS`.
  4. End to end: `run_real_pipeline` with no key configured (real
     `_build_openrouter_client`, no injected `model_client`) lands
     `reason="model_key_missing"`, `failing_stage="build_model_client"`,
     `status="ERROR"` on the reviews row -- and never makes an HTTP call.
  5. `reviews.record_stage_failure` and `pipeline_runner._fail_review` both
     stamp `failed_at` (an epoch-second string) alongside the existing
     `status`/`failing_stage`/`reason` write, and never before the guard
     that refuses to clobber a `DONE` row (a review that didn't fail gets no
     `failed_at`).
  6. `backend/src/reviews.py::_RECENT_FAILURE_FIELDS` -- the Diagnostics
     route's disclosure allowlist -- carries `failed_at`.
  7. The #425 rule holds: neither new token contains a digit, an endpoint, or
     any other material the backend keeps out of user-facing strings.

This test MUST FAIL on the pre-fix tree (`model_client.ModelKeyMissingError`
/ `ModelTimeoutError` do not exist; `record_stage_failure` never writes
`failed_at`).

Fully offline -- fake transport, fake DynamoDB, no network.
Run standalone: `python3 tests/test_review_failure_reason_472.py`
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

REVIEW_ID = "00000000-0000-4000-a000-000000000472"

PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"

FRONTEND_REVIEW_SUBMISSION = REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx"

NEW_TOKENS = ("model_key_missing", "model_timeout")


# ---------------------------------------------------------------------------
# Offline fakes (same shape as tests/test_review_failure_reason_442.py)
# ---------------------------------------------------------------------------

class FakeReviewsTable:
    """Generic-enough SET interpreter to apply whatever UpdateExpression
    record_stage_failure / _fail_review emits."""

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
    """Deliberately has NO model-settings table -- `MODEL_SETTINGS_TABLE`
    stays unset in this test's environment, so `resolve_openrouter_api_key`
    never tries to look one up (see backend/src/model_settings.py)."""

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


class NoCallHttpClient:
    """Fails the test if `.post` is ever reached -- proves the missing-key
    case is caught BEFORE any provider call, not merely turned into a
    provider 401 after the fact."""

    def post(self, url, json=None, headers=None):  # noqa: A002
        raise AssertionError("no HTTP call should be made when no key is configured")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 1. The pre-call no-key case
# ---------------------------------------------------------------------------

class TestMissingKeyClassifiedPreCall(unittest.TestCase):
    def test_build_client_raises_model_key_missing_not_a_bare_value_error(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            os.environ["REVIEWS_TABLE"] = "reviews-test"
            with self.assertRaises(mc.ModelKeyMissingError):
                pr._build_openrouter_client(dynamodb_resource=None)

    def test_model_key_missing_classifies_to_its_own_token(self) -> None:
        exc = mc.ModelKeyMissingError("no key")
        self.assertEqual(pr.classify_failure_reason(exc), "model_key_missing")

    def test_distinct_from_provider_rejected_key(self) -> None:
        """model_key_missing (no key sent) and model_key_rejected (provider
        401/403 on a key that WAS sent) must never collapse into the same
        token -- they are different admin fixes."""
        missing = pr.classify_failure_reason(mc.ModelKeyMissingError("no key"))
        rejected = pr.classify_failure_reason(
            mc.ModelInvocationError("opaque", status_code=401)
        )
        self.assertNotEqual(missing, rejected)
        self.assertEqual(missing, "model_key_missing")
        self.assertEqual(rejected, "model_key_rejected")

    def test_end_to_end_no_http_call_is_ever_made(self) -> None:
        """The real `_build_openrouter_client` path, driven through
        run_real_pipeline with NO injected model_client -- proves the
        classification holds for the actual production call site, and that
        catching it pre-call means no request reaches the network."""
        reviews_table = FakeReviewsTable()
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": b"PK-not-a-real-docx"})
        with patch.dict("os.environ", {}, clear=True):
            os.environ["REVIEWS_TABLE"] = "reviews-test"
            os.environ["UPLOADS_BUCKET"] = "uploads-test"
            os.environ["OUTPUTS_BUCKET"] = "outputs-test"
            os.environ["PLAYBOOKS_TABLE"] = "playbooks-test"
            with patch.object(pr, "_settle_reservation"), \
                 patch.object(pr, "_load_playbook_bundle", return_value={}), \
                 patch.object(
                     pr,
                     "_bundle_with_openrouter_model_ids",
                     side_effect=lambda bundle, dynamodb_resource=None: bundle,
                 ), \
                 patch.object(pr, "_fetch_upload_bytes", return_value=b"docx"):
                pr.run_real_pipeline(
                    REVIEW_ID, _payload(),
                    dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
                    # model_client deliberately NOT injected: exercises the
                    # real _build_openrouter_client call site.
                )
        self.assertEqual(reviews_table.item["reason"], "model_key_missing")
        self.assertEqual(reviews_table.item["failing_stage"], "build_model_client")
        self.assertEqual(reviews_table.item["status"], "ERROR")
        self.assertIn("failed_at", reviews_table.item)


# ---------------------------------------------------------------------------
# 2. Timeouts
# ---------------------------------------------------------------------------

class TestTimeoutClassified(unittest.TestCase):
    def test_invoke_raises_model_timeout_error_on_a_real_timeout(self) -> None:
        import httpx

        class TimingOutHttpClient:
            def post(self, url, json=None, headers=None):  # noqa: A002
                raise httpx.ReadTimeout("timed out", request=None)

            def close(self):
                pass

        client = mc.OpenRouterModelClient(
            api_key="sk-test",
            http_client=TimingOutHttpClient(),
            max_retries=0,
            sleep_fn=lambda _seconds: None,
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelTimeoutError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID,
                    system_prompt="SYS",
                    user_prompt="CONFIDENTIAL clause text",
                    max_output_tokens=8000,
                )
        self.assertIsInstance(ctx.exception, mc.ModelInvocationError)
        self.assertNotIn("CONFIDENTIAL", str(ctx.exception))

    def test_non_timeout_transport_failure_is_unaffected(self) -> None:
        """A plain OSError (connection reset, DNS failure, ...) must still
        raise the generic ModelInvocationError, never ModelTimeoutError --
        exactly today's #442 behavior for a transport failure that is not a
        timeout."""

        class BoomClient:
            def post(self, url, json=None, headers=None):  # noqa: A002
                raise OSError("connection reset")

            def close(self):
                pass

        client = mc.OpenRouterModelClient(
            api_key="sk-test", http_client=BoomClient(), max_retries=0,
            sleep_fn=lambda _seconds: None,
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.ModelInvocationError) as ctx:
                client.invoke(
                    model_id=PRIMARY_MODEL_ID, system_prompt="s", user_prompt="u",
                    max_output_tokens=10,
                )
        self.assertNotIsInstance(ctx.exception, mc.ModelTimeoutError)

    def test_model_timeout_classifies_to_its_own_token(self) -> None:
        exc = mc.ModelTimeoutError("timed out")
        self.assertEqual(pr.classify_failure_reason(exc), "model_timeout")


# ---------------------------------------------------------------------------
# 3. Taxonomy + copy for both new tokens
# ---------------------------------------------------------------------------

class TestNewTokenTaxonomyAndCopy(unittest.TestCase):
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
        source = FRONTEND_REVIEW_SUBMISSION.read_text(encoding="utf-8")
        explanations = source.split("const REASON_EXPLANATIONS", 1)
        self.assertEqual(len(explanations), 2, "REASON_EXPLANATIONS not found in the UI")
        block = explanations[1].split("const STAGE_EXPLANATIONS", 1)[0]
        for token in NEW_TOKENS:
            with self.subTest(token=token):
                self.assertIn(f"{token}: {{", block)


# ---------------------------------------------------------------------------
# 4. failed_at
# ---------------------------------------------------------------------------

class TestFailedAtStamped(unittest.TestCase):
    def setUp(self) -> None:
        self.ddb = FakeDDB(FakeReviewsTable(status="RUNNING"))

    def test_record_stage_failure_stamps_failed_at(self) -> None:
        reviews.record_stage_failure(
            REVIEW_ID, "run_review", "model_timeout", self.ddb
        )
        item = self.ddb._reviews.item
        self.assertIn("failed_at", item)
        self.assertTrue(str(item["failed_at"]).isdigit())
        self.assertEqual(item["failed_at"], item["updated_at"])

    def test_mock_pipeline_fail_review_stamps_failed_at(self) -> None:
        table = FakeReviewsTable(status="RUNNING")
        ddb = FakeDDB(table)
        pr._fail_review(REVIEW_ID, ddb)
        self.assertIn("failed_at", table.item)
        self.assertTrue(str(table.item["failed_at"]).isdigit())
        self.assertEqual(table.item["status"], "ERROR")

    def test_recent_failure_fields_allowlist_includes_failed_at(self) -> None:
        self.assertIn("failed_at", reviews._RECENT_FAILURE_FIELDS)

    def test_write_real_terminal_stamps_failed_at_on_a_fail_closed_result(self) -> None:
        """The spine's own fail-closed result (MANUAL_REVIEW_REQUIRED /
        ERROR_MANUAL_REVIEW_REQUIRED) is persisted by `_write_real_terminal`,
        a THIRD terminal-write path distinct from `record_stage_failure` and
        the mock pipeline's `_fail_review` -- it must stamp `failed_at` too,
        or a Diagnostics row for this path renders the submission time under
        a "Failed at" header."""
        table = FakeReviewsTable(status="RUNNING")
        ddb = FakeDDB(table)
        pr._write_real_terminal(
            REVIEW_ID,
            {"status": "MANUAL_REVIEW_REQUIRED", "decision": "REQUEST_CHANGE"},
            output_s3_key=None,
            dynamodb_resource=ddb,
        )
        self.assertIn("failed_at", table.item)
        self.assertTrue(str(table.item["failed_at"]).isdigit())
        self.assertEqual(table.item["failed_at"], table.item["updated_at"])
        self.assertEqual(table.item["status"], "MANUAL_REVIEW_REQUIRED")

    def test_write_real_terminal_stamps_no_failed_at_on_a_success(self) -> None:
        """A genuinely successful review must not be told it failed."""
        table = FakeReviewsTable(status="RUNNING")
        ddb = FakeDDB(table)
        pr._write_real_terminal(
            REVIEW_ID, {"status": "OK", "decision": "ACCEPT"}, output_s3_key=None,
            dynamodb_resource=ddb,
        )
        self.assertNotIn("failed_at", table.item)
        self.assertEqual(table.item["status"], "DONE")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestMissingKeyClassifiedPreCall,
        TestTimeoutClassified,
        TestNewTokenTaxonomyAndCopy,
        TestFailedAtStamped,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
