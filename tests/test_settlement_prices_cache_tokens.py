#!/usr/bin/env python3
"""
Issue #154: settlement must price prompt-cache writes and reads at their own
rates, not at the base input rate.

## What was broken

Since issue #143, `anthropic/claude-opus-5` requests prompt caching on the
DTS/OpenRouter target and the ledger records `cache_creation_input_tokens` /
`cache_read_input_tokens` per attempt. Settlement
(`reviews.compute_actual_usd_cents_from_usage`, reached from the in-process
path through `pipeline_runner._actual_cents_from_client` and from the
restart/cancel path through `reviews._actual_cents_from_ledger`) still priced
every input token at the base rate. The provider bills a cache write at about
1.25x and a cache read at about 0.1x the input rate, so the daily ledger
under-recorded a review whose call wrote the cache and over-recorded one
that read it.

## What this test proves

  1. The ticket's two figures, through the REAL ledger reader: one ledger
     row of 68,460 input tokens of which 35,000 were WRITTEN to the cache
     settles at base*(68460-35000) + 1.25*base*35000; the same row with the
     35,000 READ from the cache settles at base*(68460-35000) +
     0.1*base*35000. Both fail on the untouched tree, which prices all
     68,460 at base.
  2. A request that did not use caching -- cache columns `None` (provider
     reported nothing) or `0` -- settles exactly as before.
  3. The in-process path: a real `OpenRouterModelClient` whose two calls
     reported a cache write then a cache read accumulates both counts on
     `cumulative_usage`, and `_actual_cents_from_client` prices them.
  4. The multipliers come from model-policy/openrouter.json, per model:
     every `prompt_caching: true` entry declares both; a selected model that
     declares none (Sonnet 4.6) prices at base; the Bedrock path is
     untouched whatever cache counts it is handed.
  5. Per ROLE: with an admin-selected mixed pair (a reviewer that declares no
     multipliers, an Opus 5 critic that declares 1.25/0.1), each ledger row
     is priced at its OWN slot's model -- a critic priced with the
     primary's multipliers, or resolved against the primary's model, fails.
  6. `scripts/live_smoke_eval.py`, whose `cost_usd_cents` claims to be the
     figure settlement charges, carries the records' cache counts through
     `pass_usage_totals` and prices them the same way: its whole-cent figure
     equals the ledger settlement of the same records, and its
     full-precision `cost_usd` rounds to it.

FIXTURE FIDELITY. Ledger rows are written by the production writer
(`invocation_ledger.make_ledger_write` + `model_client.ModelInvocationRecord`,
the objects `scripts/primary_review_pass.py::run_primary_pass` builds from
the client's `last_usage` in its own `finally` block). The client's usage is
parsed by the production `parse_openrouter_usage` from OpenRouter's own
`prompt_tokens_details.cache_write_tokens` / `.cached_tokens` shape. Rates
and multipliers are read from the real policy file, never restated here
except as the ticket's own 1.25 / 0.1, which the policy is asserted to carry.

Run standalone: `python3 tests/test_settlement_prices_cache_tokens.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "backend" / "src", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

MODEL_INVOCATIONS_TABLE = "contract-toaster-model-invocations-cache154-test"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
import invocation_ledger  # noqa: E402
import live_smoke_eval  # noqa: E402
import model_client  # noqa: E402
import pipeline_runner  # noqa: E402
import reviews  # noqa: E402
from moto import mock_aws  # noqa: E402
from openrouter_sse_double import sse_stream_adapter  # noqa: E402

# The ticket's own numbers.
ACTUAL_INPUT_TOKENS = 68_460
CACHED_TOKENS = 35_000
TICKET_WRITE_MULTIPLIER = 1.25
TICKET_READ_MULTIPLIER = 0.1

OPUS_5 = "anthropic/claude-opus-5"
SONNET_4_6 = "anthropic/claude-sonnet-4.6"
GPT_5_6_SOL = "openai/gpt-5.6-sol"


def _cents(usd: float) -> int:
    return int(round(usd * 100))  # noqa: RUF046


def _primary_base_rate() -> float:
    """The primary pin's input rate, USD per token, out of the real policy."""
    policy = model_client.load_openrouter_policy()
    return float(policy["models"]["primary"]["cost_per_million_input_usd"]) / 1_000_000


def _selectable_entry(model_id: str) -> dict[str, Any]:
    """One `selectable` entry out of the real policy."""
    for entry in model_client.load_openrouter_policy()["selectable"]:
        if entry["model_id"] == model_id:
            return entry
    raise AssertionError(f"{model_id} is not a selectable model in the policy")


def _expected_write_cents() -> int:
    base = _primary_base_rate()
    return _cents(
        base * (ACTUAL_INPUT_TOKENS - CACHED_TOKENS)
        + TICKET_WRITE_MULTIPLIER * base * CACHED_TOKENS
    )


def _expected_read_cents() -> int:
    base = _primary_base_rate()
    return _cents(
        base * (ACTUAL_INPUT_TOKENS - CACHED_TOKENS)
        + TICKET_READ_MULTIPLIER * base * CACHED_TOKENS
    )


def _expected_base_cents() -> int:
    return _cents(_primary_base_rate() * ACTUAL_INPUT_TOKENS)


class _OpenRouterEnv(unittest.TestCase):
    """MODEL_PROVIDER=openrouter with no break-glass model override, so the
    effective models are the policy pins (Opus 5 on both passes)."""

    def setUp(self) -> None:
        env = patch.dict(
            os.environ,
            {"MODEL_PROVIDER": "openrouter", "MODEL_INVOCATIONS_TABLE": MODEL_INVOCATIONS_TABLE},
        )
        env.start()
        self.addCleanup(env.stop)
        for key in ("OPENROUTER_PRIMARY_MODEL_ID", "OPENROUTER_CRITIC_MODEL_ID"):
            if key in os.environ:
                popped = os.environ.pop(key)
                self.addCleanup(os.environ.__setitem__, key, popped)


class TestTheFiguresAreDistinct(unittest.TestCase):
    def test_write_read_and_base_settle_to_different_cents(self) -> None:
        """Pins that the cents equality below discriminates: on the real
        policy rate the write, read and all-at-base figures differ."""
        figures = {_expected_write_cents(), _expected_read_cents(), _expected_base_cents()}
        self.assertEqual(len(figures), 3, figures)


class TestLedgerRowSettlement(_OpenRouterEnv):
    """The ticket's required verification, through `_actual_cents_from_ledger`
    -- the settlement that prices a review from its ledger rows when no live
    client is left (runner restart, cancel)."""

    def setUp(self) -> None:
        super().setUp()
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=MODEL_INVOCATIONS_TABLE,
            KeySchema=[
                {"AttributeName": "review_id", "KeyType": "HASH"},
                {"AttributeName": "record_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "review_id", "AttributeType": "S"},
                {"AttributeName": "record_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    def _ledger_one_attempt(
        self,
        review_id: str,
        *,
        cache_creation: int | None,
        cache_read: int | None,
        pass_name: str = "primary",  # noqa: S107 - a ledger pass name, not a password
        model_id: str = OPUS_5,
    ) -> model_client.ModelInvocationRecord:
        write = invocation_ledger.make_ledger_write(review_id, self.ddb)
        record = model_client.ModelInvocationRecord(
            review_id=review_id,
            pass_name=pass_name,
            model_id=model_id,
            attempt_number=1,
            outcome="success",
            input_tokens_est=60_000,
            output_tokens_est=0,
            actual_input_tokens=ACTUAL_INPUT_TOKENS,
            actual_output_tokens=0,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        )
        write(record)
        return record

    def test_cache_write_settles_at_the_write_rate(self) -> None:
        self._ledger_one_attempt("rev-154-write", cache_creation=CACHED_TOKENS, cache_read=0)
        self.assertEqual(
            reviews._actual_cents_from_ledger("rev-154-write", self.ddb),
            _expected_write_cents(),
        )

    def test_cache_read_settles_at_the_read_rate(self) -> None:
        self._ledger_one_attempt("rev-154-read", cache_creation=0, cache_read=CACHED_TOKENS)
        self.assertEqual(
            reviews._actual_cents_from_ledger("rev-154-read", self.ddb),
            _expected_read_cents(),
        )

    def test_critic_row_prices_its_cache_at_the_critic_slot(self) -> None:
        """`_actual_cents_from_ledger` branches on pass_name; the cache
        columns must ride into the critic bucket too, not only primary."""
        self._ledger_one_attempt(
            "rev-154-critic", cache_creation=0, cache_read=CACHED_TOKENS, pass_name="critic"
        )
        self.assertEqual(
            reviews._actual_cents_from_ledger("rev-154-critic", self.ddb),
            _expected_read_cents(),
        )

    def test_a_mixed_pair_prices_each_slot_at_its_own_models_cache_rates(self) -> None:
        """An admin-selected mixed pair -- gpt-5.6-sol reviewer (no cache
        multipliers) with an Opus 5 critic (1.25/0.1), the pre-#148 shape --
        each with a cache-bearing ledger row: the primary's cache WRITE is
        priced at gpt-5.6-sol's base rate, the critic's cache READ at Opus
        5's base rate times its read multiplier. The default policy pins
        Opus 5 on both roles, which would hide any primary/critic swap."""
        primary_entry = _selectable_entry(GPT_5_6_SOL)
        critic_entry = _selectable_entry(OPUS_5)
        # Fixture preconditions: the two slots really do differ.
        self.assertNotIn(reviews.CACHE_WRITE_MULTIPLIER_KEY, primary_entry)
        self.assertNotIn(reviews.CACHE_READ_MULTIPLIER_KEY, primary_entry)
        self.assertEqual(critic_entry[reviews.CACHE_WRITE_MULTIPLIER_KEY], TICKET_WRITE_MULTIPLIER)
        self.assertEqual(critic_entry[reviews.CACHE_READ_MULTIPLIER_KEY], TICKET_READ_MULTIPLIER)
        primary_base = float(primary_entry["cost_per_million_input_usd"]) / 1_000_000
        critic_base = float(critic_entry["cost_per_million_input_usd"]) / 1_000_000

        review_id = "rev-154-mixed"
        with patch.object(
            reviews.model_settings,
            "resolve_openrouter_model_ids",
            return_value={"primary": GPT_5_6_SOL, "critic": OPUS_5},
        ):
            self._ledger_one_attempt(
                review_id, cache_creation=CACHED_TOKENS, cache_read=0, model_id=GPT_5_6_SOL
            )
            self._ledger_one_attempt(
                review_id, cache_creation=0, cache_read=CACHED_TOKENS, pass_name="critic"
            )
            actual = reviews._actual_cents_from_ledger(review_id, self.ddb)

        uncached = ACTUAL_INPUT_TOKENS - CACHED_TOKENS
        primary_at_base = primary_base * ACTUAL_INPUT_TOKENS
        critic_at_its_read_rate = (
            critic_base * uncached + TICKET_READ_MULTIPLIER * critic_base * CACHED_TOKENS
        )
        expected = _cents(primary_at_base + critic_at_its_read_rate)
        # The figures a swapped slot would produce all differ from it.
        primary_at_opus_write_rate = (
            primary_base * uncached + TICKET_WRITE_MULTIPLIER * primary_base * CACHED_TOKENS
        )
        critic_at_base = critic_base * ACTUAL_INPUT_TOKENS
        self.assertNotEqual(expected, _cents(primary_at_base + critic_at_base))
        self.assertNotEqual(expected, _cents(primary_at_opus_write_rate + critic_at_its_read_rate))
        self.assertEqual(actual, expected)

    def test_smoke_eval_prices_cached_records_as_settlement_does(self) -> None:
        """`scripts/live_smoke_eval.py` documents `cost_usd_cents` as the
        figure settlement charges and `cost_usd` as the same figure unrounded.
        Fed the SAME records the ledger holds -- a primary cache write and a
        critic cache read -- both must match the ledger settlement, not the
        all-at-base figure."""
        review_id = "rev-154-smoke"
        records = [
            self._ledger_one_attempt(review_id, cache_creation=CACHED_TOKENS, cache_read=None),
            self._ledger_one_attempt(
                review_id, cache_creation=None, cache_read=CACHED_TOKENS, pass_name="critic"
            ),
        ]
        primary_usage, critic_usage = live_smoke_eval.pass_usage_totals(records)
        self.assertEqual(
            primary_usage,
            {
                "input_tokens": ACTUAL_INPUT_TOKENS,
                "output_tokens": 0,
                "cache_creation_input_tokens": CACHED_TOKENS,
            },
        )
        self.assertEqual(
            critic_usage,
            {
                "input_tokens": ACTUAL_INPUT_TOKENS,
                "output_tokens": 0,
                "cache_read_input_tokens": CACHED_TOKENS,
            },
        )
        settled = reviews._actual_cents_from_ledger(review_id, self.ddb)
        base = _primary_base_rate()
        uncached = ACTUAL_INPUT_TOKENS - CACHED_TOKENS
        expected_usd = (
            base * uncached
            + TICKET_WRITE_MULTIPLIER * base * CACHED_TOKENS
            + base * uncached
            + TICKET_READ_MULTIPLIER * base * CACHED_TOKENS
        )
        self.assertNotEqual(_cents(expected_usd), _cents(2 * base * ACTUAL_INPUT_TOKENS))
        self.assertEqual(settled, _cents(expected_usd))
        self.assertEqual(
            reviews.compute_actual_usd_cents_from_usage(primary_usage, critic_usage, None), settled
        )
        cost_usd = live_smoke_eval.compute_actual_usd_from_usage(primary_usage, critic_usage)
        self.assertAlmostEqual(cost_usd, expected_usd, places=12)
        self.assertEqual(_cents(cost_usd), settled)

    def test_smoke_eval_keeps_the_two_key_shape_for_uncached_records(self) -> None:
        records = [
            self._ledger_one_attempt("rev-154-smoke-none", cache_creation=None, cache_read=None)
        ]
        primary_usage, critic_usage = live_smoke_eval.pass_usage_totals(records)
        self.assertEqual(primary_usage, {"input_tokens": ACTUAL_INPUT_TOKENS, "output_tokens": 0})
        self.assertEqual(critic_usage, {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(
            _cents(live_smoke_eval.compute_actual_usd_from_usage(primary_usage, critic_usage)),
            _expected_base_cents(),
        )

    def test_a_request_that_did_not_cache_settles_unchanged(self) -> None:
        """`None` (provider reported no caching) and `0` (reported, none
        cached) both price every input token at base, as before #154."""
        for review_id, creation, read in (
            ("rev-154-none", None, None),
            ("rev-154-zero", 0, 0),
        ):
            with self.subTest(review_id=review_id):
                self._ledger_one_attempt(review_id, cache_creation=creation, cache_read=read)
                self.assertEqual(
                    reviews._actual_cents_from_ledger(review_id, self.ddb),
                    _expected_base_cents(),
                )


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _QueuedHttpClient:
    """Canned OpenRouter bodies, one per call, rendered as SSE by the shared
    double -- the usage chunk carries OpenRouter's own cache-detail shape."""

    stream = sse_stream_adapter

    def __init__(self, usages: list[dict[str, Any]]) -> None:
        self._responses = [
            _FakeHttpResponse(
                {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": usage,
                }
            )
            for usage in usages
        ]

    def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeHttpResponse:  # noqa: A002
        return self._responses.pop(0)

    def close(self) -> None:
        pass


class TestClientCumulativeSettlement(_OpenRouterEnv):
    """The in-process settlement: `run_real_pipeline` prices the client's
    `cumulative_usage` through `_actual_cents_from_client`."""

    def _client_after(self, usages: list[dict[str, Any]]) -> model_client.OpenRouterModelClient:
        client = model_client.OpenRouterModelClient(
            api_key="sk-test",
            http_client=_QueuedHttpClient(usages),
            max_retries=0,
            sleep_fn=lambda _s: None,
        )
        for _ in usages:
            client.invoke(
                model_id=OPUS_5, system_prompt="SYS", user_prompt="USER", max_output_tokens=10
            )
        return client

    def test_a_write_then_a_read_both_accumulate_and_are_priced(self) -> None:
        client = self._client_after(
            [
                {
                    "prompt_tokens": ACTUAL_INPUT_TOKENS,
                    "completion_tokens": 0,
                    "prompt_tokens_details": {"cache_write_tokens": CACHED_TOKENS, "cached_tokens": 0},
                },
                {
                    "prompt_tokens": ACTUAL_INPUT_TOKENS,
                    "completion_tokens": 0,
                    "prompt_tokens_details": {"cache_write_tokens": 0, "cached_tokens": CACHED_TOKENS},
                },
            ]
        )
        self.assertEqual(
            client.cumulative_usage,
            {
                "input_tokens": 2 * ACTUAL_INPUT_TOKENS,
                "output_tokens": 0,
                "cache_creation_input_tokens": CACHED_TOKENS,
                "cache_read_input_tokens": CACHED_TOKENS,
            },
        )
        base = _primary_base_rate()
        expected = _cents(
            base * 2 * (ACTUAL_INPUT_TOKENS - CACHED_TOKENS)
            + TICKET_WRITE_MULTIPLIER * base * CACHED_TOKENS
            + TICKET_READ_MULTIPLIER * base * CACHED_TOKENS
        )
        self.assertNotEqual(expected, _cents(base * 2 * ACTUAL_INPUT_TOKENS))
        self.assertEqual(pipeline_runner._actual_cents_from_client(client, None), expected)

    def test_a_client_that_never_cached_keeps_the_two_key_shape(self) -> None:
        client = self._client_after(
            [{"prompt_tokens": ACTUAL_INPUT_TOKENS, "completion_tokens": 0}]
        )
        self.assertEqual(
            client.cumulative_usage, {"input_tokens": ACTUAL_INPUT_TOKENS, "output_tokens": 0}
        )
        self.assertEqual(
            pipeline_runner._actual_cents_from_client(client, None), _expected_base_cents()
        )


class TestMultipliersLiveInThePolicy(_OpenRouterEnv):
    def test_every_caching_entry_declares_both_multipliers(self) -> None:
        policy = model_client.load_openrouter_policy()
        entries = [
            (f"models.{role}", block) for role, block in policy["models"].items()
        ] + [(f"selectable[{e['model_id']}]", e) for e in policy["selectable"]]
        caching = [(where, e) for where, e in entries if e.get("prompt_caching") is True]
        self.assertTrue(caching, "no entry declares prompt_caching")
        for where, entry in caching:
            with self.subTest(entry=where):
                self.assertIn(reviews.CACHE_WRITE_MULTIPLIER_KEY, entry)
                self.assertIn(reviews.CACHE_READ_MULTIPLIER_KEY, entry)

    def test_opus_5_carries_the_ticket_multipliers(self) -> None:
        self.assertEqual(
            reviews._active_cache_multipliers(None),
            (
                (TICKET_WRITE_MULTIPLIER, TICKET_READ_MULTIPLIER),
                (TICKET_WRITE_MULTIPLIER, TICKET_READ_MULTIPLIER),
            ),
        )

    def test_a_selected_model_without_multipliers_prices_cache_at_base(self) -> None:
        """Sonnet 4.6 declares no caching and no multipliers: an admin who
        selects it gets the base rate on any cache count, never Opus's."""
        with patch.object(
            reviews.model_settings,
            "resolve_openrouter_model_ids",
            return_value={"primary": SONNET_4_6, "critic": SONNET_4_6},
        ):
            self.assertEqual(reviews._active_cache_multipliers(None), ((1.0, 1.0), (1.0, 1.0)))
            sonnet_input_rate, _o, _ci, _co = reviews._active_provider_rates(None)
            cents = reviews.compute_actual_usd_cents_from_usage(
                {
                    "input_tokens": ACTUAL_INPUT_TOKENS,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": CACHED_TOKENS,
                },
                None,
            )
        self.assertEqual(cents, _cents(sonnet_input_rate / 1_000_000 * ACTUAL_INPUT_TOKENS))

    def test_bedrock_path_is_unchanged_by_cache_counts(self) -> None:
        with patch.dict(os.environ, {"MODEL_PROVIDER": "bedrock"}):
            usage = {"input_tokens": ACTUAL_INPUT_TOKENS, "output_tokens": 0}
            plain = reviews.compute_actual_usd_cents_from_usage(usage, None)
            cached = reviews.compute_actual_usd_cents_from_usage(
                {**usage, "cache_creation_input_tokens": CACHED_TOKENS}, None
            )
        self.assertEqual(plain, cached)


if __name__ == "__main__":
    unittest.main(verbosity=2)
