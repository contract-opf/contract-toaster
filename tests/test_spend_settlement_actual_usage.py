#!/usr/bin/env python3
"""
TDD slice test for issue #415: "Settle daily spend from actual OpenRouter
usage".

## Root problem this proves fixed

`pipeline_runner._settle_reservation` (backend/src/pipeline_runner.py)
always settled at `actual=0` -- correct for `run_mock_pipeline` (nothing was
ever spent), wrong for `run_real_pipeline`, which called the same function
at its verify-gate, cancelled, success, and fail-closed exception call
sites. So the $20/day guardrail (`reviews.py::reserve_spend`) reserved the
worst case up front but then ALWAYS released the full reservation back,
regardless of what a review actually cost -- the cap counted zero real
spend, forever.

`reviews.compute_actual_usd_cents_from_usage` already implemented the
pricing math from real provider usage but had no production caller.

This issue wires the two together:
  1. `OpenRouterModelClient` (backend/src/model_client.py) gains
     `cumulative_usage` -- a running `{"input_tokens", "output_tokens"}`
     total across every successful `invoke()` on the instance (primary +
     critic + every retry -- issue #270's "one client instance spans the
     whole review").
  2. `run_real_pipeline` reads it at EVERY settle call site via the new
     `_actual_cents_from_client` helper and passes it through
     `_settle_reservation`'s new `actual_usd_cents` parameter (default 0,
     so the mock pipeline's call sites are untouched).

## What this test asserts (mirrors the issue's acceptance criteria)

  (a) `OpenRouterModelClient.cumulative_usage` sums prompt/completion
      tokens across ALL successful invokes, including a retry -- not just
      the last one (`last_usage`'s existing, unchanged, overwrite
      behavior).
  (b) `pipeline_runner._settle_reservation`'s new `actual_usd_cents`
      parameter reaches `reviews.settle_spend` and therefore
      `daily_spend.settled_usd_cents` -- proven directly, then again
      end to end.
  (c) A REAL review driven through `run_real_pipeline` (primary retry,
      then success, then a successful critic pass) settles at the
      documented pricing formula applied to the SUMMED tokens across all
      three successful invokes -- not the critic's alone (the last
      invoke), not 0, and not the worst-case reservation.
  (d) A real review that fails AFTER a successful primary pass (the critic
      call raises) settles at the primary-pass cost alone -- not 0, and
      not the reservation.
  (e) `run_mock_pipeline` still settles at 0 (default `actual_usd_cents`
      untouched).

MUST FAIL on the pre-fix tree:
  - `OpenRouterModelClient` has no `cumulative_usage` attribute
    (AttributeError).
  - `_settle_reservation` has no `actual_usd_cents` parameter (TypeError).
  - Every real-pipeline settle call site hardcodes `0`, so (c) and (d)
    would observe `settled_usd_cents == 0` instead of the real cost.

Run standalone: `python3 tests/test_spend_settlement_actual_usage.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import model_client  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import reviews  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_model_invocation_ledger.py): reuse #259's real-pipeline docx
# fixture builder, canned responses, and DynamoDB/S3 fakes rather than
# duplicating them.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _clear_model_provider() -> None:
    # This test's expected-cents math uses the Bedrock rate constants
    # (reviews.PRIMARY_INPUT_RATE_USD_PER_MILLION etc.), same as every
    # sibling pipeline_runner test that never sets MODEL_PROVIDER --
    # defensively cleared so a leaked env var from another test module
    # cannot silently switch the pricing branch out from under this file.
    os.environ.pop("MODEL_PROVIDER", None)


# ---------------------------------------------------------------------------
# (a) OpenRouterModelClient.cumulative_usage
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    """Deterministic offline stand-in for the injected `http_client` --
    same shape as tests/test_openrouter_spend_branch.py's fake, duplicated
    locally per this repo's self-contained-test-script convention."""

    def __init__(self, responses: list[dict]):
        self._queue = list(responses)

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        payload = self._queue.pop(0)
        return _FakeHttpResponse(200, payload)


def _openrouter_response(content: str, prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class TestOpenRouterClientTracksCumulativeUsage(unittest.TestCase):
    def test_starts_at_zero_not_none(self) -> None:
        """Unlike last_usage (None until the first call), cumulative_usage
        starts as a real zero-valued dict -- a caller can always sum it
        without a null check."""
        client = model_client.OpenRouterModelClient(
            api_key="sk-test", http_client=_FakeHttpClient([])
        )
        self.assertEqual(client.cumulative_usage, {"input_tokens": 0, "output_tokens": 0})
        self.assertIsNone(client.last_usage)

    def test_cumulative_usage_sums_across_all_successful_invokes_including_a_retry(self) -> None:
        """Three successful invokes on ONE instance -- primary attempt 1
        (would be a schema retry at the pass level, but the client itself
        just sees a successful HTTP 200 either way), primary attempt 2, and
        the critic pass -- must all land in cumulative_usage. last_usage
        must still show only the LAST call's numbers (unchanged, pre-#415
        behavior)."""
        http = _FakeHttpClient([
            _openrouter_response("primary attempt 1", 100, 20),
            _openrouter_response("primary attempt 2", 150, 40),
            _openrouter_response("critic", 80, 30),
        ])
        client = model_client.OpenRouterModelClient(api_key="sk-test", http_client=http)

        for _ in range(3):
            client.invoke(
                model_id=model_client.openrouter_primary_model_id(),
                system_prompt="s", user_prompt="u", max_output_tokens=100,
            )

        self.assertEqual(client.last_usage, {"input_tokens": 80, "output_tokens": 30})
        self.assertEqual(
            client.cumulative_usage,
            {"input_tokens": 100 + 150 + 80, "output_tokens": 20 + 40 + 30},
            "cumulative_usage must be the SUM of every successful invoke, not just the last.",
        )

    def test_readable_after_close(self) -> None:
        """Issue #415 Notes: 'tolerate reading attrs from a closed client
        (attrs survive close; only the httpx transport is closed)' --
        cumulative_usage is a plain instance attribute, unaffected by
        close()."""
        http = _FakeHttpClient([_openrouter_response("x", 10, 5)])
        client = model_client.OpenRouterModelClient(api_key="sk-test", http_client=http)
        client.invoke(
            model_id=model_client.openrouter_primary_model_id(),
            system_prompt="s", user_prompt="u", max_output_tokens=100,
        )
        client.close()
        self.assertEqual(client.cumulative_usage, {"input_tokens": 10, "output_tokens": 5})


# ---------------------------------------------------------------------------
# (b) _settle_reservation's new actual_usd_cents parameter
# ---------------------------------------------------------------------------


class FakeSubmissionsTable:
    """Purpose-built REVIEW_SUBMISSIONS_TABLE stand-in: the scan+filter
    fallback path `_find_submission_by_review_id` uses when the table has
    no `.query()` (same lightweight-double convention as
    tests/test_spend_reservation_settlement.py's FakeTable), plus the one
    update_item shape `_settle_reservation` issues (the
    `reservation_released` flip)."""

    def __init__(self, submission: dict[str, Any]) -> None:
        self.items: dict[str, dict[str, Any]] = {submission["idempotency_key"]: dict(submission)}

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None):
        vals = ExpressionAttributeValues or {}
        if FilterExpression != "review_id = :rid":
            raise AssertionError(f"unhandled FilterExpression {FilterExpression!r}")
        matches = [v for v in self.items.values() if v.get("review_id") == vals.get(":rid")]
        return {"Items": [dict(v) for v in matches]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **_kw):
        vals = ExpressionAttributeValues or {}
        item = self.items[Key["idempotency_key"]]
        if "reservation_released = :t" in UpdateExpression:
            item["reservation_released"] = vals[":t"]
            return
        raise AssertionError(f"unhandled UpdateExpression {UpdateExpression!r}")


class FakeDailySpendTable:
    """Purpose-built DAILY_SPEND_TABLE stand-in -- only the settle_spend
    update shape (same interpreter fragment as
    tests/test_spend_reservation_settlement.py's FakeTable)."""

    def __init__(self, spend_date: str, reserved_usd_cents: int) -> None:
        self.items: dict[str, dict[str, Any]] = {
            spend_date: {
                "spend_date": spend_date,
                "reserved_usd_cents": reserved_usd_cents,
                "daily_cap_usd_cents": 2000,
            }
        }

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **_kw):
        vals = ExpressionAttributeValues or {}
        key = Key["spend_date"]
        item = self.items.setdefault(key, dict(Key))
        if "reserved_usd_cents = reserved_usd_cents + :delta" in UpdateExpression:
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":delta"]
            item["settled_usd_cents"] = item.get("settled_usd_cents", 0) + vals[":actual"]
            return
        raise AssertionError(f"unhandled UpdateExpression {UpdateExpression!r}")


class _SettlementOnlyDDB:
    def __init__(self, submissions: FakeSubmissionsTable, daily_spend: FakeDailySpendTable):
        self._submissions = submissions
        self._daily_spend = daily_spend

    def Table(self, name):
        if name == os.environ["REVIEW_SUBMISSIONS_TABLE"]:
            return self._submissions
        if name == os.environ["DAILY_SPEND_TABLE"]:
            return self._daily_spend
        raise AssertionError(f"unexpected Table({name!r})")


class TestSettleReservationActualUsdCents(unittest.TestCase):
    def setUp(self) -> None:
        _clear_model_provider()
        self.spend_date = _today()
        self.reservation_cents = reviews.compute_worst_case_reservation_usd_cents()

    def _ddb(self, review_id: str, idempotency_key: str) -> _SettlementOnlyDDB:
        submissions = FakeSubmissionsTable({
            "idempotency_key": idempotency_key,
            "review_id": review_id,
            "spend_reservation_id": f"res-{review_id}",
        })
        daily_spend = FakeDailySpendTable(self.spend_date, self.reservation_cents)
        return _SettlementOnlyDDB(submissions, daily_spend)

    def test_default_actual_cents_is_still_zero(self) -> None:
        """Acceptance criterion: 'Mock pipeline still settles at 0.' The
        mock pipeline's call sites never pass a third argument -- proving
        the default alone keeps that contract."""
        ddb = self._ddb("review-default", "idem-default")
        pr._settle_reservation("review-default", ddb)
        self.assertEqual(ddb._daily_spend.items[self.spend_date]["settled_usd_cents"], 0)
        self.assertEqual(ddb._daily_spend.items[self.spend_date]["reserved_usd_cents"], 0)

    def test_explicit_actual_cents_reaches_settle_spend(self) -> None:
        ddb = self._ddb("review-explicit", "idem-explicit")
        pr._settle_reservation("review-explicit", ddb, 137)
        self.assertEqual(ddb._daily_spend.items[self.spend_date]["settled_usd_cents"], 137)
        self.assertEqual(ddb._daily_spend.items[self.spend_date]["reserved_usd_cents"], 137)

    def test_settle_reservation_safely_threads_actual_cents_through(self) -> None:
        ddb = self._ddb("review-safely", "idem-safely")
        pr._settle_reservation_safely("review-safely", ddb, 250)
        self.assertEqual(ddb._daily_spend.items[self.spend_date]["settled_usd_cents"], 250)


# ---------------------------------------------------------------------------
# (c) / (d) / (e): end to end through run_real_pipeline / run_mock_pipeline
# ---------------------------------------------------------------------------


class SettlementAwareFakeDDB(dts.FakeDDB):
    """Extends dts.FakeDDB (REVIEWS_TABLE + PLAYBOOKS_TABLE) with real
    REVIEW_SUBMISSIONS_TABLE + DAILY_SPEND_TABLE routes -- same
    cross-file-extension convention as
    tests/test_model_invocation_ledger.py's LedgerAwareFakeDDB."""

    def __init__(
        self,
        reviews_table,
        submissions_table: FakeSubmissionsTable,
        daily_spend_table: FakeDailySpendTable,
        playbooks_table=None,
    ):
        super().__init__(reviews_table, playbooks_table)
        self._submissions_name = os.environ["REVIEW_SUBMISSIONS_TABLE"]
        self._daily_spend_name = os.environ["DAILY_SPEND_TABLE"]
        self._submissions = submissions_table
        self._daily_spend = daily_spend_table

    def Table(self, name):
        if name == self._submissions_name:
            return self._submissions
        if name == self._daily_spend_name:
            return self._daily_spend
        return super().Table(name)


class UsageAccumulatingClient:
    """Same wiring as tests/test_model_invocation_ledger.py's
    UsageReportingClient, extended with `cumulative_usage` (issue #415) --
    the shape the real OpenRouterModelClient now reports after this issue's
    model_client.py change. Driven through the REAL run_real_pipeline so
    this proves the end-to-end wiring, not just the client class in
    isolation (that's TestOpenRouterClientTracksCumulativeUsage above)."""

    def __init__(
        self, responses: dict[str, list[str]], usage_sequence: list[dict[str, int]]
    ) -> None:
        self._inner = model_client.FakeBedrockClient(responses)
        self._usage_sequence = list(usage_sequence)
        self.last_usage: dict[str, int] | None = None
        self.cumulative_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    def invoke(self, **kwargs: Any) -> str:
        text = self._inner.invoke(**kwargs)
        usage = self._usage_sequence.pop(0)
        self.last_usage = usage
        self.cumulative_usage["input_tokens"] += usage["input_tokens"]
        self.cumulative_usage["output_tokens"] += usage["output_tokens"]
        return text


class TestRealPipelineSettlesActualUsage(unittest.TestCase):
    def setUp(self) -> None:
        _clear_model_provider()
        self.spend_date = _today()
        self.reservation_cents = reviews.compute_worst_case_reservation_usd_cents()

    def _ddb(self, reviews_table) -> tuple[SettlementAwareFakeDDB, FakeDailySpendTable]:
        submissions = FakeSubmissionsTable({
            "idempotency_key": "idem-real-1",
            "review_id": dts.REVIEW_ID,
            "spend_reservation_id": "res-real-1",
        })
        daily_spend = FakeDailySpendTable(self.spend_date, self.reservation_cents)
        return SettlementAwareFakeDDB(reviews_table, submissions, daily_spend), daily_spend

    def test_successful_review_settles_at_summed_tokens_not_the_last_invoke(self) -> None:
        """(c): primary retries once (schema-invalid then valid), critic
        succeeds on the first attempt -- three successful client invokes.
        Settlement must equal the documented formula applied to the SUM of
        all three, not the critic's alone."""
        primary_id = model_client.openrouter_primary_model_id()
        critic_id = model_client.openrouter_critic_model_id()
        client = UsageAccumulatingClient(
            {
                primary_id: [
                    _fixture("schema_invalid_missing_issues.json"),
                    dts._primary_request_change_response(),
                ],
                critic_id: [dts._critic_no_delta_response()],
            },
            usage_sequence=[
                # Realistic (not toy) token magnitudes -- these round-trip
                # through the per-million-token rate table and `int(round(
                # usd * 100))` to nonzero cents; a few hundred tokens like a
                # unit-test placeholder would round to 0 cents and this test
                # would pass by accident.
                {"input_tokens": 12_000, "output_tokens": 800},  # primary attempt 1 (retry)
                {"input_tokens": 15_000, "output_tokens": 1_200},  # primary attempt 2 (success)
                {"input_tokens": 9_000, "output_tokens": 700},  # critic attempt 1 (success)
            ],
        )
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        reviews_table = dts.FakeReviewsTable()
        ddb, daily_spend = self._ddb(reviews_table)
        s3 = dts.FakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        pr.run_real_pipeline(
            dts.REVIEW_ID, dts._payload(),
            dynamodb_resource=ddb, s3_client=s3, model_client=client,
        )

        self.assertEqual(reviews_table.item["status"], "DONE")

        summed_usage = {
            "input_tokens": 12_000 + 15_000 + 9_000,
            "output_tokens": 800 + 1_200 + 700,
        }
        expected_cents = reviews.compute_actual_usd_cents_from_usage(summed_usage, None)
        last_invoke_only_cents = reviews.compute_actual_usd_cents_from_usage(
            {"input_tokens": 9_000, "output_tokens": 700}, None
        )

        settled = daily_spend.items[self.spend_date]["settled_usd_cents"]
        self.assertEqual(
            settled, expected_cents,
            "settlement must equal the documented formula applied to the SUMMED tokens",
        )
        self.assertNotEqual(settled, 0)
        self.assertNotEqual(settled, self.reservation_cents)
        self.assertNotEqual(
            settled, last_invoke_only_cents,
            "settlement must not be priced from only the LAST invoke's usage",
        )
        self.assertTrue(ddb._submissions.items["idem-real-1"]["reservation_released"])

    def test_review_failing_after_primary_pass_settles_at_primary_pass_cost(self) -> None:
        """(d): the primary pass succeeds and accumulates real usage, then
        the critic call raises (no responses seeded for it) before it can
        ever report usage of its own. The review must fail closed AND
        settle at the primary-pass cost -- not 0, not the reservation."""
        primary_id = model_client.openrouter_primary_model_id()
        critic_id = model_client.openrouter_critic_model_id()
        client = UsageAccumulatingClient(
            {primary_id: [dts._primary_request_change_response()], critic_id: []},
            usage_sequence=[{"input_tokens": 20_000, "output_tokens": 1_500}],
        )
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        reviews_table = dts.FakeReviewsTable()
        ddb, daily_spend = self._ddb(reviews_table)
        s3 = dts.FakeS3({f"uploads/user-1/{dts.REVIEW_ID}/in.docx": docx_bytes})

        pr.run_real_pipeline(
            dts.REVIEW_ID, dts._payload(),
            dynamodb_resource=ddb, s3_client=s3, model_client=client,
        )

        self.assertNotIn(reviews_table.item["status"], ("PENDING", "RUNNING", "DONE"))

        primary_only_usage = {"input_tokens": 20_000, "output_tokens": 1_500}
        expected_cents = reviews.compute_actual_usd_cents_from_usage(primary_only_usage, None)

        settled = daily_spend.items[self.spend_date]["settled_usd_cents"]
        self.assertEqual(
            settled, expected_cents,
            "a review that fails after the primary pass must settle at the "
            "primary-pass cost, not 0 and not the reservation",
        )
        self.assertNotEqual(settled, 0)
        self.assertNotEqual(settled, self.reservation_cents)


class _MockPipelineFakeS3:
    """`run_mock_pipeline` copies its canned fixture via `copy_object`,
    unlike the real pipeline's `get_object`/`put_object` -- mirrors
    tests/test_pipeline_runner_inprocess.py's own FakeS3."""

    def __init__(self) -> None:
        self.copies: list[dict[str, Any]] = []

    def copy_object(self, Bucket, Key, CopySource) -> None:
        self.copies.append({"Bucket": Bucket, "Key": Key, "CopySource": CopySource})


class TestMockPipelineStillSettlesAtZero(unittest.TestCase):
    def test_mock_pipeline_end_to_end_settles_at_zero(self) -> None:
        """(e): run_mock_pipeline's call sites never pass actual_usd_cents
        -- driven end to end (not just the direct-call check in (b) above)
        to prove the mock pipeline's own behavior is genuinely unchanged."""
        _clear_model_provider()
        spend_date = _today()
        reservation_cents = reviews.compute_worst_case_reservation_usd_cents()
        reviews_table = dts.FakeReviewsTable()
        submissions = FakeSubmissionsTable({
            "idempotency_key": "idem-mock-1",
            "review_id": dts.REVIEW_ID,
            "spend_reservation_id": "res-mock-1",
        })
        daily_spend = FakeDailySpendTable(spend_date, reservation_cents)
        ddb = SettlementAwareFakeDDB(reviews_table, submissions, daily_spend)
        s3 = _MockPipelineFakeS3()

        pr.run_mock_pipeline(
            dts.REVIEW_ID,
            {"review_id": dts.REVIEW_ID, "playbook_id": "synthetic-generic"},
            dynamodb_resource=ddb, s3_client=s3,
        )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(daily_spend.items[spend_date]["settled_usd_cents"], 0)
        self.assertEqual(daily_spend.items[spend_date]["reserved_usd_cents"], 0)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestOpenRouterClientTracksCumulativeUsage,
        TestSettleReservationActualUsdCents,
        TestRealPipelineSettlesActualUsage,
        TestMockPipelineStillSettlesAtZero,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
