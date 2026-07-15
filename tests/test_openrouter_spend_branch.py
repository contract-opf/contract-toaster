#!/usr/bin/env python3
"""
TDD slice test for issue #268: "Spend model: OpenRouter pricing branch +
settle from actual provider token usage."

## Root problem this proves fixed

`backend/src/reviews.py`'s spend model (`compute_worst_case_reservation_usd_cents`,
`reserve_spend`, `settle_spend`) always priced with the Bedrock model-policy
matrix (hardcoded `PRIMARY_INPUT_RATE_USD_PER_MILLION` etc.), even though
`model-policy/openrouter.json` already carries the DTS/OpenRouter target's
real per-token rates. With `MODEL_PROVIDER=openrouter`, neither the daily
reservation nor the settled amount reflected the provider actually being
billed -- and settlement had no path from a real OpenRouter API response's
`usage.prompt_tokens`/`usage.completion_tokens` fields to a settled dollar
amount at all (`backend/src/pipeline_runner.py::_settle_reservation` always
settles at a fixed constant, never real usage).

This test proves:
  1. `compute_worst_case_reservation_usd_cents()` branches on
     `config.model_provider()`: `MODEL_PROVIDER=openrouter` prices the
     reservation from `model-policy/openrouter.json`'s
     `cost_per_million_{input,output}_usd` rates; the Bedrock path (default /
     any other value) is byte-for-byte unchanged (still $2.11 / 211 cents,
     issue #189's documented worst case).
  2. `OpenRouterModelClient.invoke()` captures the REAL token usage
     (`usage.prompt_tokens` / `usage.completion_tokens`) an OpenAI-compatible
     OpenRouter response carries, exposed as `.last_usage` after each call --
     fully offline via an injected fake HTTP client (standing rule 4).
  3. A new `compute_actual_usd_cents_from_usage()` prices a review's actual
     settled cost from real primary+critic usage using the SAME
     provider-aware rate table the reservation used (`model-policy/
     openrouter.json` for `MODEL_PROVIDER=openrouter`) -- and that this
     actual figure is a genuinely different number from the worst-case
     reservation estimate, then `settle_spend()` records exactly that real
     amount (not the reservation, not a hardcoded $0).
  4. Daily-cap breach (`reserve_spend` raising `HTTPException(429)`) behaves
     identically whether `MODEL_PROVIDER` is unset (Bedrock) or `openrouter`.

MUST FAIL on the pre-fix tree:
  - `compute_worst_case_reservation_usd_cents()` ignores `MODEL_PROVIDER`
    entirely, so the openrouter-rates assertion fails (it returns the
    Bedrock-priced 211 cents regardless).
  - `OpenRouterModelClient` has no `.last_usage` attribute (AttributeError).
  - `reviews.compute_actual_usd_cents_from_usage` does not exist
    (AttributeError).

Run standalone: `python3 tests/test_openrouter_spend_branch.py`
Exit codes: 0 = pass, 1 = fail

## Convention note

Per the sibling precedent already flagged in
tests/test_dts_pipeline_runner_real_review.py's and
tests/test_bundle_runtime_validation.py's own docstrings: the ticket's
"Required verification" names `backend/tests/test_openrouter_spend_branch.py`,
but `backend/tests/` does not exist anywhere in this repo -- every test in
this repo (and `scripts/check.sh`'s own discovery loop,
`scripts/collect_test_failures.sh`'s `tests/test_*.py` glob) lives at
`tests/test_*.py` at the repo root. This file lives here, consistent with
every sibling ticket.
"""

from __future__ import annotations

import json
import sys
import time
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "backend" / "src"
OPENROUTER_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"
BEDROCK_POLICY_PATH = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


# ---------------------------------------------------------------------------
# Third-party stubs (no live boto3/fastapi dependency required in CI) --
# same convention as tests/test_spend_reservation_settlement.py.
# ---------------------------------------------------------------------------

def _stub_third_party() -> None:
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_202_ACCEPTED = 202
            HTTP_404_NOT_FOUND = 404
            HTTP_409_CONFLICT = 409
            HTTP_429_TOO_MANY_REQUESTS = 429
            HTTP_503_SERVICE_UNAVAILABLE = 503

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status
        sys.modules["fastapi"] = fastapi_mod

    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")
        exceptions_mod = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            def __init__(self, error_response=None, operation_name=""):
                self.response = error_response or {}
                super().__init__(str(error_response))

        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.exceptions"] = exceptions_mod

    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")

        def _unset(*_a, **_kw):
            raise AssertionError(
                "boto3.resource()/client() called without being patched by "
                "the test -- monkeypatch <module>.boto3 first."
            )

        boto3_mod.resource = _unset
        boto3_mod.client = _unset
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("SEMAPHORE_TABLE", "contract-toaster-semaphore-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)

import reviews as _reviews_module  # noqa: E402
import model_client as _model_client_module  # noqa: E402

HTTPException = sys.modules["fastapi"].HTTPException


# ---------------------------------------------------------------------------
# In-memory daily_spend table fake -- only the slice reserve_spend/
# settle_spend exercise (same interpreter shape as
# tests/test_spend_reservation_settlement.py's FakeTable, trimmed to this
# file's needs).
# ---------------------------------------------------------------------------

class FakeSpendTable:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ConditionExpression=None, ExpressionAttributeNames=None):
        key = Key["spend_date"]
        vals = ExpressionAttributeValues or {}
        item = self.items.setdefault(key, dict(Key))

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                ClientError = sys.modules["botocore.exceptions"].ClientError
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "reserved_usd_cents = reserved_usd_cents + :delta" in UpdateExpression:
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":delta"]
            item["settled_usd_cents"] = item.get("settled_usd_cents", 0) + vals[":actual"]
            return


class FakeDynamoDBResource:
    def __init__(self):
        self._table = FakeSpendTable()

    def Table(self, _name: str) -> FakeSpendTable:
        return self._table


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _clear_model_provider() -> None:
    os.environ.pop("MODEL_PROVIDER", None)


# ---------------------------------------------------------------------------
# Fake OpenRouter HTTP transport -- same shape as
# tests/test_openrouter_model_client.py's FakeHttpClient/FakeResponse,
# duplicated locally per this repo's self-contained-test-script convention.
# ---------------------------------------------------------------------------

class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    """Deterministic offline stand-in for the injected `http_client`:
    returns one canned (content, usage) response per `.post()` call, in
    order -- the fake client supplying deterministic usage for this test,
    per issue #268's Grind notes."""

    def __init__(self, responses: list[dict]):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        self.calls.append({"url": url, "json": json, "headers": headers})
        payload = self._queue.pop(0)
        return _FakeHttpResponse(200, payload)


def _openrouter_response(content: str, prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


# ---------------------------------------------------------------------------
# (1) Reservation pricing branch: openrouter.json rates vs Bedrock unchanged
# ---------------------------------------------------------------------------

class TestOpenRouterReservationPricingBranch(unittest.TestCase):
    def tearDown(self):
        _clear_model_provider()

    def test_bedrock_path_unchanged_by_default(self):
        """No MODEL_PROVIDER (or any value other than 'openrouter') must
        still reserve the documented Bedrock worst case (issue #189:
        $2.11 / 211 cents) -- this branch must not disturb the AWS target."""
        _clear_model_provider()
        self.assertEqual(_reviews_module.compute_worst_case_reservation_usd_cents(), 211)

    def test_openrouter_reservation_uses_openrouter_json_rates(self):
        """MODEL_PROVIDER=openrouter must price the reservation from
        model-policy/openrouter.json's cost_per_million_{input,output}_usd
        rates -- computed independently here from the on-disk policy file so
        this test cannot tautologically pass against a hardcoded mirror."""
        os.environ["MODEL_PROVIDER"] = "openrouter"
        with open(OPENROUTER_POLICY_PATH, encoding="utf-8") as f:
            policy = json.load(f)
        primary = policy["models"]["primary"]
        critic = policy["models"]["critic"]

        attempts_per_pass = 1 + _reviews_module.MAX_RETRIES_PER_PASS
        primary_usd = (
            _reviews_module.MAX_INPUT_TOKENS * primary["cost_per_million_input_usd"] / 1_000_000
            + _reviews_module.MAX_OUTPUT_TOKENS * primary["cost_per_million_output_usd"] / 1_000_000
        )
        critic_usd = (
            _reviews_module.MAX_INPUT_TOKENS * critic["cost_per_million_input_usd"] / 1_000_000
            + _reviews_module.MAX_OUTPUT_TOKENS * critic["cost_per_million_output_usd"] / 1_000_000
        )
        expected_cents = int(round(attempts_per_pass * (primary_usd + critic_usd) * 100))

        actual_cents = _reviews_module.compute_worst_case_reservation_usd_cents()
        self.assertEqual(actual_cents, expected_cents)
        self.assertNotEqual(
            actual_cents, 211,
            "Must diverge from the Bedrock worst case, not silently reuse it.",
        )


# ---------------------------------------------------------------------------
# (2) OpenRouterModelClient captures real usage from the API response
# ---------------------------------------------------------------------------

class TestOpenRouterModelClientCapturesUsage(unittest.TestCase):
    def test_invoke_exposes_last_usage_from_response(self):
        http = _FakeHttpClient([_openrouter_response("primary text", 12345, 678)])
        client = _model_client_module.OpenRouterModelClient(api_key="sk-test", http_client=http)

        # Policy-pinned id (issue #270 rebase note below), not a placeholder:
        # OpenRouterModelClient.invoke() now enforces
        # enforce_openrouter_policy_model_id() (issue #269) on every call.
        text = client.invoke(
            model_id=_model_client_module.openrouter_primary_model_id(),
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=8000,
        )

        self.assertEqual(text, "primary text")
        self.assertEqual(client.last_usage, {"input_tokens": 12345, "output_tokens": 678})

    def test_last_usage_updates_per_call(self):
        http = _FakeHttpClient([
            _openrouter_response("primary text", 12345, 678),
            _openrouter_response("critic text", 23456, 901),
        ])
        client = _model_client_module.OpenRouterModelClient(api_key="sk-test", http_client=http)

        # Policy-pinned ids (see note above): a placeholder id like "p"/"c"
        # now trips enforce_openrouter_policy_model_id() (issue #269).
        client.invoke(
            model_id=_model_client_module.openrouter_primary_model_id(),
            system_prompt="s", user_prompt="u", max_output_tokens=100,
        )
        primary_usage = client.last_usage
        client.invoke(
            model_id=_model_client_module.openrouter_critic_model_id(),
            system_prompt="s", user_prompt="u", max_output_tokens=100,
        )
        critic_usage = client.last_usage

        self.assertEqual(primary_usage, {"input_tokens": 12345, "output_tokens": 678})
        self.assertEqual(critic_usage, {"input_tokens": 23456, "output_tokens": 901})


# ---------------------------------------------------------------------------
# (3) Settle from actual usage, not the worst-case estimate
# ---------------------------------------------------------------------------

class TestSettleFromActualProviderUsage(unittest.TestCase):
    def tearDown(self):
        _clear_model_provider()

    def test_settle_records_actual_usage_based_cost_not_the_reservation(self):
        os.environ["MODEL_PROVIDER"] = "openrouter"
        http = _FakeHttpClient([
            _openrouter_response('{"decision":"ACCEPT"}', 12345, 678),
            _openrouter_response('{"decision":"ACCEPT"}', 23456, 901),
        ])
        client = _model_client_module.OpenRouterModelClient(api_key="sk-test", http_client=http)

        # Policy-pinned ids (see note in TestOpenRouterModelClientCapturesUsage
        # above): a placeholder id like "p"/"c" now trips
        # enforce_openrouter_policy_model_id() (issue #269).
        client.invoke(
            model_id=_model_client_module.openrouter_primary_model_id(),
            system_prompt="s", user_prompt="u", max_output_tokens=100,
        )
        primary_usage = client.last_usage
        client.invoke(
            model_id=_model_client_module.openrouter_critic_model_id(),
            system_prompt="s", user_prompt="u", max_output_tokens=100,
        )
        critic_usage = client.last_usage

        with open(OPENROUTER_POLICY_PATH, encoding="utf-8") as f:
            policy = json.load(f)
        primary_rates = policy["models"]["primary"]
        critic_rates = policy["models"]["critic"]
        expected_usd = (
            primary_usage["input_tokens"] * primary_rates["cost_per_million_input_usd"] / 1_000_000
            + primary_usage["output_tokens"] * primary_rates["cost_per_million_output_usd"] / 1_000_000
            + critic_usage["input_tokens"] * critic_rates["cost_per_million_input_usd"] / 1_000_000
            + critic_usage["output_tokens"] * critic_rates["cost_per_million_output_usd"] / 1_000_000
        )
        expected_cents = int(round(expected_usd * 100))

        actual_cents = _reviews_module.compute_actual_usd_cents_from_usage(
            primary_usage, critic_usage
        )
        self.assertEqual(actual_cents, expected_cents)
        reservation_cents = _reviews_module.compute_worst_case_reservation_usd_cents()
        self.assertNotEqual(
            actual_cents, reservation_cents,
            "The settled amount must reflect real usage, not equal the "
            "worst-case reservation estimate.",
        )

        ddb = FakeDynamoDBResource()
        spend_date = _today()
        table = ddb.Table("daily-spend")
        table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": reservation_cents,
            "daily_cap_usd_cents": 2000,
        }

        _reviews_module.settle_spend("review-or-1", "res-or-1", actual_cents, ddb)

        self.assertEqual(table.items[spend_date]["reserved_usd_cents"], actual_cents)
        self.assertEqual(table.items[spend_date]["settled_usd_cents"], actual_cents)

    def test_missing_critic_usage_contributes_zero(self):
        """A primary-only pass (e.g. the critic pass never ran because the
        primary pass failed closed) must settle at the primary-only cost,
        not raise or silently price a phantom critic call."""
        os.environ["MODEL_PROVIDER"] = "openrouter"
        primary_usage = {"input_tokens": 1000, "output_tokens": 200}
        cents_with_critic_none = _reviews_module.compute_actual_usd_cents_from_usage(
            primary_usage, None
        )
        cents_with_critic_zero = _reviews_module.compute_actual_usd_cents_from_usage(
            primary_usage, {"input_tokens": 0, "output_tokens": 0}
        )
        self.assertEqual(cents_with_critic_none, cents_with_critic_zero)
        self.assertGreater(cents_with_critic_none, 0)


# ---------------------------------------------------------------------------
# (4) Daily-cap breach behaves identically on both targets
# ---------------------------------------------------------------------------

class TestCapBreachIdenticalOnBothTargets(unittest.TestCase):
    def tearDown(self):
        _clear_model_provider()

    def _assert_cap_breach(self, model_provider: str | None) -> None:
        if model_provider:
            os.environ["MODEL_PROVIDER"] = model_provider
        else:
            _clear_model_provider()

        ddb = FakeDynamoDBResource()
        spend_date = _today()
        table = ddb.Table("daily-spend")
        cap = 2000
        table.items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": cap,
            "daily_cap_usd_cents": cap,
        }

        with self.assertRaises(HTTPException) as ctx:
            _reviews_module.reserve_spend("review-cap-1", ddb)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_bedrock_cap_breach(self):
        self._assert_cap_breach(None)

    def test_openrouter_cap_breach(self):
        self._assert_cap_breach("openrouter")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestOpenRouterReservationPricingBranch,
        TestOpenRouterModelClientCapturesUsage,
        TestSettleFromActualProviderUsage,
        TestCapBreachIdenticalOnBothTargets,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
