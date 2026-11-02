#!/usr/bin/env python3
"""
QA remediation W8 (2026-10-31 QA report, finding #16 and area 2): pipeline
spend accounting.

## What was wrong

  1. A truncated, empty-content or cancelled-mid-stream attempt was billed by
     the provider and priced at $0: `OpenRouterModelClient.invoke` counted
     usage only on a successful return, and the passes ledgered usage only
     when `invoke()` returned.
  2. Every token was settled at the REVIEWER's rate. The client's grand total
     was priced in the primary slot, and the ledger path priced the Floor
     judge (a critic-model call) in the primary slot too.
  3. The worst-case reservation did not include the Floor judge -- one or two
     whole-document calls per judged invariant.
  4. `max_tokens = content budget + reasoning allowance` could reach 132,000
     against Opus 5's declared 128,000 cap on any document over ~44,800
     estimated tokens -- a provider 400 on every long document.
  5. A run that failed to claim its row (PENDING -> RUNNING) ran anyway, paid
     for the whole review, and could overwrite a DONE row's result.
  6. The Floor judge ran after a FAILED critic pass: wasted spend, and its own
     fail-closed reason masked the critic's.
  7. A truncation retry re-sent a stale correction; the Binding block
     promised a "closing self-check" no live review runs; a Floor fire was
     not deduped against a critic issue on the same invariant.

Item 8 (rollout-overlap relabel in runner_recovery) is closed without code:
Coolify does not perform rolling updates for Docker Compose deployments, so
old and new backend containers do not overlap (see the W8 report).

Every test here drives the real code with offline doubles. No network.

Run: python3 tests/test_spend_accounting_w8.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

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
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")
os.environ.setdefault("MODEL_INVOCATIONS_TABLE", "model-invocations-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import admin_dashboard  # noqa: E402
import critic_review_pass as cp  # noqa: E402
import floor_judge  # noqa: E402
import model_client as mc  # noqa: E402
import opf_prompt  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import reconciliation  # noqa: E402
import review_spine  # noqa: E402
import reviews  # noqa: E402

# Cross-test-file imports (established convention -- see
# tests/test_spend_settlement_actual_usage.py): the hand-written SSE wire
# fixtures, the real-pipeline docx/DDB/S3 fakes, the moto submissions mixin,
# and the OPF Floor fixture with its routing client.
import test_dts_pipeline_runner_real_review as dts  # noqa: E402
import test_floor_judge_budget_682 as fb  # noqa: E402
import test_openrouter_streaming as ors  # noqa: E402
import test_spend_settlement_actual_usage as ssa  # noqa: E402
from openrouter_sse_double import SseStreamContext, SseStreamResponse  # noqa: E402

MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "samples" / "synthetic-nda-sample-v1.0.0.json"

OPUS_5 = "anthropic/claude-opus-5"  # declares max_output_tokens 128000, reasoning 4000


def _fixture(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _usage(prompt: int, completion: int) -> dict[str, int]:
    return {"prompt_tokens": prompt, "completion_tokens": completion}


class SequencedStreamHttpClient:
    """Serves one hand-written SSE line list per `.stream()` call, in order,
    through the shared `SseStreamResponse` (so the client is still held to a
    real streamed response's ordering rules). Records every request."""

    def __init__(self, streams: list[list[str]]) -> None:
        self._streams = list(streams)
        self.calls: list[dict[str, Any]] = []

    def stream(self, method, url, json=None, headers=None):  # noqa: A002 - httpx sig
        assert method == "POST", method
        self.calls.append({"url": url, "json": json})
        if not self._streams:
            raise AssertionError("more model calls than the test scripted")
        return SseStreamContext(SseStreamResponse(200, self._streams.pop(0)))

    def close(self) -> None:
        pass


def _client(http: Any, **kwargs: Any) -> mc.OpenRouterModelClient:
    kwargs.setdefault("max_retries", 0)
    kwargs.setdefault("sleep_fn", lambda _seconds: None)
    return mc.OpenRouterModelClient(api_key="sk-test", http_client=http, **kwargs)


def _invoke(client: mc.OpenRouterModelClient, **kwargs: Any) -> str:
    kwargs.setdefault("model_id", OPUS_5)
    kwargs.setdefault("system_prompt", "SYS")
    kwargs.setdefault("user_prompt", "USER")
    kwargs.setdefault("max_output_tokens", 8000)
    with patch.dict("os.environ", {}, clear=True):
        return client.invoke(**kwargs)


# ---------------------------------------------------------------------------
# 1. Every billed attempt is counted -- at the client.
# ---------------------------------------------------------------------------


class TestBilledFailuresAreCounted(unittest.TestCase):
    def test_a_truncated_200_is_counted_and_its_usage_rides_on_the_exception(self) -> None:
        http = ors.WireStreamHttpClient(
            ors.content_stream('{"partial', finish_reason="length", usage=_usage(30_000, 20_000))
        )
        client = _client(http)
        with self.assertRaises(mc.ModelOutputTruncatedError) as ctx:
            _invoke(client)
        self.assertEqual(ctx.exception.usage["input_tokens"], 30_000)
        self.assertEqual(ctx.exception.usage["output_tokens"], 20_000)
        self.assertEqual(client.cumulative_usage["input_tokens"], 30_000)
        self.assertEqual(client.cumulative_usage["output_tokens"], 20_000)
        self.assertEqual(client.usage_by_model[OPUS_5]["output_tokens"], 20_000)
        self.assertEqual(
            mc.reported_usage_from_failure(ctx.exception)["input_tokens"], 30_000
        )

    def test_an_empty_200_is_counted_and_its_usage_rides_on_the_exception(self) -> None:
        http = ors.WireStreamHttpClient(
            [ors.finish("stop"), "", ors.usage_chunk(_usage(12_000, 900)), "", ors.DONE, ""]
        )
        client = _client(http)
        with self.assertRaises(mc.ModelEmptyContentError) as ctx:
            _invoke(client)
        self.assertEqual(ctx.exception.usage["input_tokens"], 12_000)
        self.assertEqual(client.cumulative_usage["output_tokens"], 900)

    def test_a_cancelled_stream_is_counted_as_a_flagged_estimate(self) -> None:
        http = ors.WireStreamHttpClient(ors.content_stream("A" * 400, fragment_size=1))
        calls = {"n": 0}

        def checkpoint() -> None:
            calls["n"] += 1
            if calls["n"] > 50:
                raise ors.Stopped("reviewer pressed stop")

        client = _client(http, cancel_checkpoint=checkpoint)
        long_prompt = "clause text " * 1000
        with self.assertRaises(ors.Stopped) as ctx:
            _invoke(client, user_prompt=long_prompt)
        # The prompt was billed in full: at least its length at the
        # calibrated ratio, never the zero it used to be.
        self.assertGreaterEqual(
            client.cumulative_usage["input_tokens"],
            int(len(long_prompt) / mc.CANCELLED_STREAM_CHARS_PER_TOKEN_ESTIMATE),
        )
        self.assertGreater(client.cumulative_usage["output_tokens"], 0)
        self.assertTrue(getattr(ctx.exception, "usage_estimated", False))
        # An estimate is counted against the cap but never ledgered as
        # provider-reported usage.
        self.assertIsNone(mc.reported_usage_from_failure(ctx.exception))

    def test_truncate_then_succeed_counts_both_attempts(self) -> None:
        http = SequencedStreamHttpClient(
            [
                ors.content_stream('{"cut', finish_reason="length", usage=_usage(30_000, 20_000)),
                ors.content_stream(ors.REVIEW_JSON, usage=_usage(31_000, 3_000)),
            ]
        )
        client = _client(http)
        with self.assertRaises(mc.ModelOutputTruncatedError):
            _invoke(client)
        self.assertEqual(_invoke(client), ors.REVIEW_JSON)
        self.assertEqual(client.cumulative_usage["input_tokens"], 61_000)
        self.assertEqual(client.cumulative_usage["output_tokens"], 23_000)
        # `last_usage` is still the last RETURNED call's own figure.
        self.assertEqual(client.last_usage["output_tokens"], 3_000)


# ---------------------------------------------------------------------------
# 1 (end to end). Truncate-then-succeed settles both attempts: live
# settlement, the ledger, ledger settlement and the admin dashboard.
# ---------------------------------------------------------------------------


class CaptureLedgerTable:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item):  # noqa: N803 - boto3 signature
        self.items.append(dict(Item))

    def query(self, **_kwargs):
        return {"Items": list(self.items)}


class LedgerAndSettlementFakeDDB(ssa.SettlementAwareFakeDDB):
    def __init__(self, *args: Any, ledger: CaptureLedgerTable, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ledger = ledger

    def Table(self, name):  # noqa: N802 - boto3 signature
        if name == os.environ["MODEL_INVOCATIONS_TABLE"]:
            return self.ledger
        return super().Table(name)


class TestTruncateThenSucceedSettlesBothAttempts(ssa.MotoSubmissionsMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        ssa._clear_model_provider()
        self.spend_date = _today()
        self.reservation_cents = reviews.compute_worst_case_reservation_usd_cents()

    def test_every_settlement_path_counts_the_truncated_attempt(self) -> None:
        truncated = {"input_tokens": 30_000, "output_tokens": 20_000}
        primary_ok = {"input_tokens": 31_000, "output_tokens": 3_000}
        critic_ok = {"input_tokens": 40_000, "output_tokens": 2_500}
        http = SequencedStreamHttpClient(
            [
                ors.content_stream(
                    '{"decision":"REQ',
                    finish_reason="length",
                    usage=_usage(truncated["input_tokens"], truncated["output_tokens"]),
                ),
                ors.content_stream(
                    dts._primary_request_change_response(),
                    usage=_usage(primary_ok["input_tokens"], primary_ok["output_tokens"]),
                ),
                ors.content_stream(
                    dts._critic_no_delta_response(),
                    usage=_usage(critic_ok["input_tokens"], critic_ok["output_tokens"]),
                ),
            ]
        )
        client = _client(http)
        submissions = self.seed_submission(
            {
                "idempotency_key": "idem-w8-1",
                "review_id": dts.REVIEW_ID,
                "spend_reservation_id": "res-w8-1",
            }
        )
        daily = ssa.FakeDailySpendTable(self.spend_date, self.reservation_cents)
        ledger = CaptureLedgerTable()
        reviews_table = dts.FakeReviewsTable()
        ddb = LedgerAndSettlementFakeDDB(reviews_table, submissions, daily, ledger=ledger)
        s3 = dts.FakeS3(
            {
                f"uploads/user-1/{dts.REVIEW_ID}/in.docx": dts._build_draft_docx(
                    {"sec-8": dts._SEC8_DRAFT_TEXT}
                )
            }
        )

        with patch.dict("os.environ", {"OPENROUTER_PRIMARY_MODEL_ID": "", "OPENROUTER_CRITIC_MODEL_ID": ""}):
            pr.run_real_pipeline(
                dts.REVIEW_ID, dts._payload(), dynamodb_resource=ddb, s3_client=s3, model_client=client
            )

        self.assertEqual(len(http.calls), 3, "setup: truncated primary, primary retry, critic")
        self.assertEqual(reviews_table.item["status"], "DONE")

        total = {
            "input_tokens": sum(u["input_tokens"] for u in (truncated, primary_ok, critic_ok)),
            "output_tokens": sum(u["output_tokens"] for u in (truncated, primary_ok, critic_ok)),
        }
        expected = reviews.compute_actual_usd_cents_from_usage(total, None)
        without_truncated = reviews.compute_actual_usd_cents_from_usage(
            {
                "input_tokens": primary_ok["input_tokens"] + critic_ok["input_tokens"],
                "output_tokens": primary_ok["output_tokens"] + critic_ok["output_tokens"],
            },
            None,
        )
        self.assertNotEqual(expected, without_truncated, "setup: the truncated attempt costs cents")

        # Live settlement.
        self.assertEqual(daily.items[self.spend_date]["settled_usd_cents"], expected)

        # The ledger carries the truncated attempt's REAL usage.
        primary_rows = sorted(
            (row for row in ledger.items if row["pass_name"] == "primary"),
            key=lambda row: row["attempt_number"],
        )
        self.assertEqual(len(primary_rows), 2)
        self.assertEqual(primary_rows[0]["actual_input_tokens"], truncated["input_tokens"])
        self.assertEqual(primary_rows[0]["actual_output_tokens"], truncated["output_tokens"])
        self.assertEqual(primary_rows[1]["actual_output_tokens"], primary_ok["output_tokens"])

        # Ledger settlement (restart recovery / cancel) agrees.
        self.assertEqual(reviews._actual_cents_from_ledger(dts.REVIEW_ID, ddb), expected)

        # And the admin dashboard's per-review cost.
        passes: dict[str, dict[str, int]] = {}
        for row in ledger.items:
            bucket = passes.setdefault(row["pass_name"], {"input_tokens": 0, "output_tokens": 0})
            bucket["input_tokens"] += int(row["actual_input_tokens"] or 0)
            bucket["output_tokens"] += int(row["actual_output_tokens"] or 0)
        self.assertEqual(
            admin_dashboard._priced_review_usd_cents(passes, reviews._active_provider_rates(None)),
            expected,
        )


# ---------------------------------------------------------------------------
# 2. Each model's tokens are priced at that model's rate.
# ---------------------------------------------------------------------------

# A cheap reviewer and an expensive critic, USD per million tokens:
# (primary_input, primary_output, critic_input, critic_output).
_SPLIT_RATES = (1.0, 2.0, 10.0, 50.0)


def _cents(usage_rates: list[tuple[dict[str, int], float, float]]) -> int:
    usd = 0.0
    for usage, in_rate, out_rate in usage_rates:
        usd += usage["input_tokens"] * in_rate / 1_000_000
        usd += usage["output_tokens"] * out_rate / 1_000_000
    return int(round(usd * 100))  # noqa: RUF046


class _PerModelClient:
    def __init__(self, usage_by_model: dict[str, dict[str, int]]) -> None:
        self.usage_by_model = copy.deepcopy(usage_by_model)
        self.cumulative_usage = {"input_tokens": 0, "output_tokens": 0}
        for usage in usage_by_model.values():
            self.cumulative_usage["input_tokens"] += usage["input_tokens"]
            self.cumulative_usage["output_tokens"] += usage["output_tokens"]


class TestPricedPerRole(unittest.TestCase):
    PRIMARY: ClassVar[dict[str, int]] = {"input_tokens": 400_000, "output_tokens": 30_000}
    CRITIC_AND_FLOOR: ClassVar[dict[str, int]] = {"input_tokens": 500_000, "output_tokens": 40_000}

    def setUp(self) -> None:
        patcher = patch.object(reviews, "_active_provider_rates", return_value=_SPLIT_RATES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_live_settlement_prices_the_critic_models_tokens_at_the_critic_rate(self) -> None:
        client = _PerModelClient({"cheap/reviewer": self.PRIMARY, "dear/critic": self.CRITIC_AND_FLOOR})
        model_ids = {"primary_model_id": "cheap/reviewer", "critic_model_id": "dear/critic"}
        expected = _cents(
            [(self.PRIMARY, 1.0, 2.0), (self.CRITIC_AND_FLOOR, 10.0, 50.0)]
        )
        all_at_reviewer_rate = _cents(
            [(self.PRIMARY, 1.0, 2.0), (self.CRITIC_AND_FLOOR, 1.0, 2.0)]
        )
        self.assertNotEqual(expected, all_at_reviewer_rate)
        self.assertEqual(pr._actual_cents_from_client(client, None, model_ids), expected)

    def test_an_unknown_model_is_priced_in_the_dearer_slot(self) -> None:
        client = _PerModelClient({"cheap/reviewer": self.PRIMARY, "off/policy": self.CRITIC_AND_FLOOR})
        model_ids = {"primary_model_id": "cheap/reviewer", "critic_model_id": "dear/critic"}
        expected = _cents(
            [(self.PRIMARY, 1.0, 2.0), (self.CRITIC_AND_FLOOR, 10.0, 50.0)]
        )
        self.assertEqual(pr._actual_cents_from_client(client, None, model_ids), expected)

    def test_a_client_without_a_per_model_split_keeps_the_grand_total(self) -> None:
        client = ssa.UsageAccumulatingClient({}, [])
        client.cumulative_usage = dict(self.PRIMARY)
        self.assertEqual(
            pr._actual_cents_from_client(client, None, {"critic_model_id": "x"}),
            _cents([(self.PRIMARY, 1.0, 2.0)]),
        )

    def test_ledger_settlement_prices_the_floor_judge_at_the_critic_rate(self) -> None:
        floor = {"input_tokens": 200_000, "output_tokens": 8_000}
        critic = {"input_tokens": 300_000, "output_tokens": 32_000}
        ledger = CaptureLedgerTable()
        for name, usage in (("primary", self.PRIMARY), ("critic", critic), ("floor", floor)):
            ledger.items.append(
                {
                    "pass_name": name,
                    "actual_input_tokens": usage["input_tokens"],
                    "actual_output_tokens": usage["output_tokens"],
                }
            )

        class _DDB:
            def Table(self, name):  # noqa: N802
                assert name == os.environ["MODEL_INVOCATIONS_TABLE"], name
                return ledger

        expected = _cents(
            [
                (self.PRIMARY, 1.0, 2.0),
                (
                    {
                        "input_tokens": critic["input_tokens"] + floor["input_tokens"],
                        "output_tokens": critic["output_tokens"] + floor["output_tokens"],
                    },
                    10.0,
                    50.0,
                ),
            ]
        )
        self.assertEqual(reviews._actual_cents_from_ledger("r-w8", _DDB()), expected)
        self.assertEqual(
            admin_dashboard._priced_review_usd_cents(
                {"primary": self.PRIMARY, "critic": critic, "floor": floor}, _SPLIT_RATES
            ),
            expected,
        )

    def test_the_two_ledger_pricing_sets_agree(self) -> None:
        self.assertEqual(
            set(admin_dashboard._CRITIC_RATE_PASSES), set(reviews._CRITIC_RATE_PASS_NAMES)
        )
        self.assertIn("floor", reviews._CRITIC_RATE_PASS_NAMES)


# ---------------------------------------------------------------------------
# 3. The Floor judge is reserved before any model call.
# ---------------------------------------------------------------------------


class TestFloorJudgeIsReserved(ssa.MotoSubmissionsMixin, unittest.TestCase):
    def test_worst_case_is_two_attempts_per_judged_invariant_over_the_whole_document(self) -> None:
        document = "The supplier shall indemnify without limit. " * 2_000
        invariants = [
            {"id": "inv-a", "statement": "Liability is capped."},
            {"id": "inv-b", "statement": "Governing law is stated."},
        ]
        usage = floor_judge.worst_case_judge_usage(
            invariants=invariants, review_context=document, model_id=OPUS_5
        )
        self.assertEqual(usage["calls"], 4)
        # Every call re-sends the whole document.
        self.assertGreaterEqual(usage["input_tokens"], 4 * pp.estimate_input_tokens(document))
        per_call = min(
            floor_judge.DEFAULT_MAX_OUTPUT_TOKENS + mc.openrouter_reasoning_max_tokens(OPUS_5),
            mc.openrouter_model_max_output_tokens(OPUS_5),
        )
        self.assertEqual(usage["output_tokens"], 4 * per_call)

    def test_an_invariant_the_judge_skips_without_a_call_costs_nothing(self) -> None:
        usage = floor_judge.worst_case_judge_usage(
            invariants=[{"id": "dir", "statement": "The counterparty may not assign."}],
            review_context="text",
            model_id=OPUS_5,
        )
        self.assertEqual(usage, {"calls": 0, "input_tokens": 0, "output_tokens": 0})

    def test_the_reservation_is_priced_at_the_critic_rate_and_rounded_up(self) -> None:
        with patch.object(reviews, "_active_provider_rates", return_value=_SPLIT_RATES):
            # 100,001 input at $10/M + 0 output = 100.001 cents -> 101.
            self.assertEqual(reviews.compute_floor_reservation_usd_cents(100_001, 0), 101)
            self.assertEqual(reviews.compute_floor_reservation_usd_cents(0, 20_000), 100)

    def test_the_top_up_is_added_recorded_and_reversed_at_settlement(self) -> None:
        submissions = self.seed_submission(
            {"idempotency_key": "idem-floor", "review_id": "r-floor", "spend_reservation_id": "res-f"}
        )
        base = reviews.compute_worst_case_reservation_usd_cents()
        daily = _TopUpDailySpendTable(_today(), base)
        ddb = ssa._SettlementOnlyDDB(submissions, daily)

        added = reviews.reserve_floor_spend("r-floor", 400_000, 40_000, ddb)
        self.assertGreater(added, 0)
        self.assertEqual(daily.items[_today()]["reserved_usd_cents"], base + added)
        self.assertEqual(self.submission_row("idem-floor")["floor_reserved_usd_cents"], added)

        pr._settle_reservation("r-floor", ddb, 321)
        # The base reservation AND the Floor top-up are both reversed; only
        # the actual cost stays on the day's counter.
        self.assertEqual(daily.items[_today()]["reserved_usd_cents"], 321)
        self.assertEqual(daily.items[_today()]["settled_usd_cents"], 321)

    def test_cancel_and_recovery_settlement_reverse_the_top_up_too(self) -> None:
        submissions = self.seed_submission(
            {"idempotency_key": "idem-floor-c", "review_id": "r-floor-c", "spend_reservation_id": "r"}
        )
        base = reviews.compute_worst_case_reservation_usd_cents()
        daily = _TopUpDailySpendTable(_today(), base)
        ddb = ssa._SettlementOnlyDDB(submissions, daily)
        reviews.reserve_floor_spend("r-floor-c", 400_000, 40_000, ddb)
        with patch.dict("os.environ", {"MODEL_INVOCATIONS_TABLE": ""}):
            reviews.settle_reservation_for_cancel("r-floor-c", ddb)
        self.assertEqual(daily.items[_today()]["reserved_usd_cents"], 0)

    def test_no_reservation_means_nothing_to_top_up(self) -> None:
        submissions = self.seed_submission({"idempotency_key": "idem-none", "review_id": "r-none"})
        daily = _TopUpDailySpendTable(_today(), 0)
        ddb = ssa._SettlementOnlyDDB(submissions, daily)
        self.assertEqual(reviews.reserve_floor_spend("r-none", 400_000, 40_000, ddb), 0)
        self.assertEqual(daily.items[_today()]["reserved_usd_cents"], 0)

    def test_the_spine_reserves_before_its_first_model_call(self) -> None:
        events: list[Any] = []

        class _Client(fb._FloorJudgeRoutingClient):
            def invoke(self, **kwargs: Any) -> str:
                events.append("invoke")
                return super().invoke(**kwargs)

            def _judge_response(self, **kwargs: Any) -> str:
                invariant_id = kwargs["user_prompt"].split("invariant_id: ", 1)[1].split("\n", 1)[0]
                return fb._verdict(invariant_id, False)

        client = _Client()
        review_spine.run_review(
            fb._build_docx_bytes(),
            fb._opf_bundle(),
            client,
            review_id="w8-floor-reserve",
            reserve_floor_spend=lambda usage: events.append(("reserve", dict(usage))),
        )
        self.assertTrue(client.judge_calls, "setup: the Floor judge ran")
        self.assertIsInstance(events[0], tuple, "the reservation must precede every model call")
        usage = events[0][1]
        judged = {
            call["user_prompt"].split("invariant_id: ", 1)[1].split("\n", 1)[0]
            for call in client.judge_calls
        }
        self.assertEqual(usage["calls"], 2 * len(judged))
        self.assertEqual(sum(1 for e in events if isinstance(e, tuple)), 1)


class _TopUpDailySpendTable(ssa.FakeDailySpendTable):
    """`ssa.FakeDailySpendTable` plus the Floor top-up's update shape."""

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **kw):  # noqa: N803
        vals = ExpressionAttributeValues or {}
        if UpdateExpression.startswith(
            "SET reserved_usd_cents = if_not_exists(reserved_usd_cents, :zero) + :amount"
        ) and "settled" not in UpdateExpression:
            item = self.items.setdefault(Key["spend_date"], dict(Key))
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":amount"]
            return
        return super().update_item(Key, UpdateExpression, ExpressionAttributeValues, **kw)


# ---------------------------------------------------------------------------
# 4. max_tokens never exceeds the model's declared cap.
# ---------------------------------------------------------------------------


class TestOutputCapIsNeverExceeded(unittest.TestCase):
    CAP = 128_000
    REASONING = 4_000

    def setUp(self) -> None:
        # Setup guard: the policy values this test is written against.
        self.assertEqual(mc.openrouter_model_max_output_tokens(OPUS_5), self.CAP)
        self.assertEqual(mc.openrouter_reasoning_max_tokens(OPUS_5), self.REASONING)

    def test_the_content_ceiling_is_the_cap_minus_the_reasoning_allowance(self) -> None:
        self.assertEqual(mc.openrouter_content_budget_ceiling(OPUS_5), self.CAP - self.REASONING)

    def test_a_44800_token_document_asks_for_exactly_the_cap(self) -> None:
        # 16,000 + 2.5 x 44,800 = 128,000: the boundary where the old sizing
        # put max_tokens at 132,000.
        old_budget = mc.output_budget_for_document(44_800, self.CAP)
        self.assertEqual(old_budget + self.REASONING, 132_000, "setup: the defect's arithmetic")

        budget = mc.output_budget_for_document(44_800, mc.openrouter_content_budget_ceiling(OPUS_5))
        self.assertEqual(budget, self.CAP - self.REASONING)
        http = ors.WireStreamHttpClient(ors.content_stream(ors.REVIEW_JSON))
        _invoke(_client(http), max_output_tokens=budget)
        payload = http.calls[0]["json"]
        self.assertEqual(payload["max_tokens"], self.CAP)
        self.assertEqual(payload["reasoning"], {"max_tokens": self.REASONING})

    def test_just_under_the_clamp_point_is_unchanged(self) -> None:
        # 16,000 + 2.5 x 43,000 = 123,500 < 124,000: not clamped.
        budget = mc.output_budget_for_document(43_000, mc.openrouter_content_budget_ceiling(OPUS_5))
        self.assertEqual(budget, 123_500)
        http = ors.WireStreamHttpClient(ors.content_stream(ors.REVIEW_JSON))
        _invoke(_client(http), max_output_tokens=budget)
        self.assertEqual(http.calls[0]["json"]["max_tokens"], 127_500)

    def test_invoke_clamps_a_caller_that_asks_past_the_cap(self) -> None:
        http = ors.WireStreamHttpClient(ors.content_stream(ors.REVIEW_JSON))
        _invoke(_client(http), max_output_tokens=self.CAP)
        self.assertEqual(http.calls[0]["json"]["max_tokens"], self.CAP)

    def test_both_passes_size_against_the_content_ceiling(self) -> None:
        for module in (pp, cp):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIn(
                "output_budget_ceiling = _model_client.openrouter_content_budget_ceiling(model_id)",
                source,
                f"{module.__name__} must widen against cap - reasoning, not the bare cap",
            )


# ---------------------------------------------------------------------------
# 5. A run that does not own its row spends nothing and writes nothing.
# ---------------------------------------------------------------------------


class _RecordingClient:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, **_kwargs: Any) -> str:
        self.calls += 1
        raise AssertionError("no model call may be made for a row this run does not own")


class _WriteRecordingDDB(dts.FakeDDB):
    """Every table except REVIEWS records its writes; REVIEWS is the shared
    conditional fake."""

    def __init__(self, reviews_table: dts.FakeReviewsTable) -> None:
        super().__init__(reviews_table)
        self.writes: list[tuple[str, str]] = []

    def Table(self, name):  # noqa: N802
        if name in (os.environ["REVIEWS_TABLE"], os.environ["PLAYBOOKS_TABLE"]):
            return super().Table(name)
        outer = self

        class _Recorder:
            def __getattr__(self, attr):
                def _call(*_a, **_k):
                    if attr in ("put_item", "update_item", "delete_item"):
                        outer.writes.append((name, attr))
                    return {}

                return _call

        return _Recorder()


class TestUnownedRowIsNeverRun(unittest.TestCase):
    def _run_against(self, status: str) -> None:
        table = dts.FakeReviewsTable(status=status)
        table.item.update({"decision": "ACCEPT", "summary": "the paid-for result"})
        before = copy.deepcopy(table.item)
        ddb = _WriteRecordingDDB(table)
        client = _RecordingClient()
        with patch.object(pr.reviews, "record_stage_failure") as record_failure, patch.object(
            pr, "_settle_reservation_safely"
        ) as settle:
            pr.run_real_pipeline(
                dts.REVIEW_ID, dts._payload(), dynamodb_resource=ddb, s3_client=dts.FakeS3({}),
                model_client=client,
            )
        self.assertEqual(client.calls, 0)
        self.assertEqual(table.item, before, f"a {status} row must be left exactly as it was")
        self.assertEqual(ddb.writes, [])
        record_failure.assert_not_called()
        settle.assert_not_called()

    def test_a_done_row_makes_zero_model_calls_and_writes_nothing(self) -> None:
        self._run_against("DONE")

    def test_an_error_row_makes_zero_model_calls_and_writes_nothing(self) -> None:
        self._run_against("ERROR")

    def test_a_row_another_run_holds_running_is_left_alone(self) -> None:
        self._run_against("RUNNING")

    def test_the_terminal_write_lands_only_on_a_running_row(self) -> None:
        for status in ("DONE", "CANCELLED", "PENDING"):
            table = dts.FakeReviewsTable(status=status)
            pr._write_real_terminal(
                dts.REVIEW_ID, {"status": "ERROR", "reason": "x"}, None, dts.FakeDDB(table)
            )
            self.assertEqual(table.item["status"], status)
        table = dts.FakeReviewsTable(status="RUNNING")
        pr._write_real_terminal(
            dts.REVIEW_ID, {"status": "OK", "decision": "ACCEPT"}, None, dts.FakeDDB(table)
        )
        self.assertEqual(table.item["status"], "DONE")


# ---------------------------------------------------------------------------
# 6. No Floor judge after a failed critic.
# ---------------------------------------------------------------------------


class TestFloorSkippedAfterFailedCritic(unittest.TestCase):
    def test_a_failed_critic_is_not_followed_by_the_judge_and_keeps_its_own_reason(self) -> None:
        class _Client(fb._FloorJudgeRoutingClient):
            def __init__(self) -> None:
                super().__init__()
                self._passes = mc.FakeBedrockClient(
                    {
                        fb._BUNDLE_PRIMARY_MODEL_ID: [fb._accept_response("No changes identified.")],
                        fb._BUNDLE_CRITIC_MODEL_ID: ["not json"] * 6,
                    }
                )

            def _judge_response(self, **kwargs: Any) -> str:
                return "not json"  # had it run, it would fail closed first

        client = _Client()
        result = review_spine.run_review(
            fb._build_docx_bytes(), fb._opf_bundle(), client, review_id="w8-critic-failed"
        )
        self.assertEqual(client.judge_calls, [], "the judge must not run after a failed critic")
        self.assertNotEqual(result["status"], review_spine.STATUS_OK)
        self.assertNotIn(
            result.get("reason"),
            (
                review_spine.REASON_FLOOR_INVARIANT_UNJUDGED,
                review_spine.REASON_FLOOR_INVARIANT_TRUNCATED,
            ),
        )
        self.assertIn(
            result.get("reason"),
            (
                review_spine.REASON_CRITIC_INVALID_JSON,
                review_spine.REASON_CRITIC_RETRY_EXHAUSTED,
                review_spine.REASON_CRITIC_SCHEMA_INVALID,
            ),
        )


# ---------------------------------------------------------------------------
# 7. Smaller items.
# ---------------------------------------------------------------------------


class _ScriptedPromptClient:
    """Replays a script; an exception CLASS entry is raised as a truncation.
    Records every user prompt it was sent."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.prompts: list[str] = []

    def invoke(self, *, user_prompt: Any, **_kwargs: Any) -> str:
        self.prompts.append(json.dumps(user_prompt) if not isinstance(user_prompt, str) else user_prompt)
        nxt = self._script.pop(0)
        if isinstance(nxt, type) and issubclass(nxt, Exception):
            raise nxt("truncated (finish_reason='length', HTTP 200).", status_code=200)
        return nxt


class TestTruncationRetryResetsTheCorrection(unittest.TestCase):
    def test_the_primary_retry_after_a_truncation_sends_no_stale_correction(self) -> None:
        client = _ScriptedPromptClient(
            [
                _fixture("schema_invalid_missing_issues.json"),
                mc.ModelOutputTruncatedError,
                _fixture("primary_request_change_valid.json"),
            ]
        )
        result = pp.run_primary_pass(
            review_id="w8-primary",
            retrieved_precedent=[],
            playbook=_playbook(),
            model_client=client,
            model_id=OPUS_5,
            ledger_write=[].append,
            doc_text="Section 1. The parties agree.",
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(client.prompts), 3)
        self.assertNotEqual(client.prompts[1], client.prompts[0], "setup: attempt 2 carried a correction")
        self.assertEqual(client.prompts[2], client.prompts[0])

    def test_the_critic_retry_after_a_truncation_sends_no_stale_correction(self) -> None:
        client = _ScriptedPromptClient(
            ["not json", mc.ModelOutputTruncatedError, _fixture("critic_drop_i1_accept_valid.json")]
        )
        result = cp.run_critic_pass(
            review_id="w8-critic",
            primary_output=json.loads(_fixture("primary_request_change_valid.json")),
            playbook=_playbook(),
            model_client=client,
            model_id=OPUS_5,
            ledger_write=[].append,
            doc_text="Section 1. The parties agree.",
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(client.prompts), 3)
        self.assertNotEqual(client.prompts[1], client.prompts[0], "setup: attempt 2 carried a correction")
        self.assertEqual(client.prompts[2], client.prompts[0])


class TestTruncatedAttemptIsLedgeredWithItsUsage(unittest.TestCase):
    def test_the_floor_judge_ledgers_a_truncated_attempts_reported_usage(self) -> None:
        class _Truncating:
            def invoke(self, **_kwargs: Any) -> str:
                raise mc.ModelOutputTruncatedError(
                    "truncated", status_code=200, usage={"input_tokens": 9_000, "output_tokens": 5_000}
                )

        records: list[Any] = []
        floor_judge.judge_floor_invariants(
            invariants=[{"id": "inv-a", "statement": "Liability is capped."}],
            review_context="text",
            model_client=_Truncating(),
            model_id=OPUS_5,
            review_id="w8-floor",
            ledger_write=records.append,
        )
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record.actual_input_tokens, 9_000)
            self.assertEqual(record.actual_output_tokens, 5_000)


class TestBindingIntroPromisesNoSelfCheck(unittest.TestCase):
    def test_the_binding_block_no_longer_promises_an_unwired_self_check(self) -> None:
        self.assertNotIn("self-check", opf_prompt.BINDING_INTRO)
        self.assertNotIn("re-read against your finished redline", opf_prompt.BINDING_INTRO)


class TestFloorFireDedupesAgainstCriticIssue(unittest.TestCase):
    def _critic(self, topic: str) -> dict[str, Any]:
        critic = json.loads(_fixture("critic_added_issue_valid.json"))
        critic["issues"][0]["playbook_topic_id"] = topic
        return critic

    def _fire(self, invariant_id: str) -> dict[str, Any]:
        judgment = floor_judge.FloorJudgment(
            verdicts=[{"invariant_id": invariant_id, "violated": True, "evidence_quote": "q"}],
            unjudged=[],
            truncated=[],
        )
        return floor_judge.floor_fires(judgment)[0]

    def _reconcile(self, critic: dict[str, Any], fire: dict[str, Any]) -> dict[str, Any]:
        primary = json.loads(_fixture("primary_request_change_valid.json"))
        return reconciliation.reconcile(
            primary_result=primary, critic_result=critic, detector_fires=[fire]
        )

    def test_a_floor_fire_on_an_invariant_the_critic_raised_is_not_duplicated(self) -> None:
        merged = self._reconcile(self._critic("inv-cap"), self._fire("inv-cap"))
        topics = [issue.get("playbook_topic_id") for issue in merged["issues"]]
        self.assertEqual(topics.count("inv-cap"), 1)
        self.assertEqual(merged["decision"], "REQUEST_CHANGE")

    def test_a_floor_fire_on_another_invariant_is_still_appended(self) -> None:
        merged = self._reconcile(self._critic("some-other-topic"), self._fire("inv-cap"))
        provenances = [issue.get("provenance") for issue in merged["issues"]]
        self.assertIn("floor:inv-cap", provenances)
        self.assertEqual(merged["decision"], "REQUEST_CHANGE")


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestBilledFailuresAreCounted,
        TestTruncateThenSucceedSettlesBothAttempts,
        TestPricedPerRole,
        TestFloorJudgeIsReserved,
        TestOutputCapIsNeverExceeded,
        TestUnownedRowIsNeverRun,
        TestFloorSkippedAfterFailedCritic,
        TestTruncationRetryResetsTheCorrection,
        TestTruncatedAttemptIsLedgeredWithItsUsage,
        TestBindingIntroPromisesNoSelfCheck,
        TestFloorFireDedupesAgainstCriticIssue,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
