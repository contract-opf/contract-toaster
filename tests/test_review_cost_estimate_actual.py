#!/usr/bin/env python3
"""
Tests for the input-aware cost estimate and the persisted actual cost.

Before this change the estimate a reviewer saw before pressing the button was
ONE constant (the policy artifact's `approx_tokens_per_review_*` basis priced at
the active models' rates), identical for a two-page NDA and a sixty-page MSA and
for every playbook, and the only record of what a review really cost was the
day's total on the admin ledger. This file pins the replacement:

  1. THE ESTIMATE (`reviews.estimate_review_cost` / `review_cost_estimate`).
     Deterministic for fixed inputs; grows with the document; falls back to the
     policy constant when an input is missing; reports None where the provider
     carries no basis (Bedrock); takes its OUTPUT tokens from the median of this
     playbook's finished reviews only from `REVIEW_COST_MIN_SAMPLE` of them; and
     turns the p25-p75 of (actual / predicted) into the low/high range under the
     same threshold.
  2. THE PLAYBOOK'S PROMPT BASIS (`pipeline_runner.review_prompt_basis`), derived
     from the stored playbook by the same assembly a review uses.
  3. PERSISTED ACTUAL COST. `run_real_pipeline` writes `actual_usd_cents` (and
     `actual_cost_failed`) with the terminal status for a DONE review AND for a
     failed one; a failed review that made no model call records 0; the other
     settle paths (`record_actual_cost`, the cancel settle) stamp it first-write-
     wins; a legacy row simply has none.
  4. THE ALLOWLISTS. The exact tuple of fields on the estimate route and on the
     owner's detail / History rows, owner-only visibility of those rows, and
     the regression that the non-admin estimate route still returns no cap,
     spend, remainder or model id.

Every sampled row is produced by the writers production uses
(`reviews._create_review_row`, `pipeline_runner._write_real_terminal`) against a
REAL (moto) reviews table, as `tests/test_review_duration_estimate_71.py` does.
No network, no live AWS.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"

for _path in (str(BACKEND_ROOT), str(SCRIPTS_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-test")
os.environ.setdefault("MODEL_SETTINGS_TABLE", "contract-toaster-model-settings-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("ENV_NAME", "dev")
os.environ.pop("RETENTION_SETTINGS_TABLE", None)
os.environ.pop("MODEL_INVOCATIONS_TABLE", None)

import boto3  # noqa: E402, I001
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import primary_review_pass  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.model_settings as model_settings  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.review_routes as review_routes  # noqa: E402
import src.reviews as reviews  # noqa: E402

from ddb_fixtures import create_reviews_table, create_submissions_table  # noqa: E402

POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"
OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.6.opf.json"

OWNER = {"cognito_sub": "owner-1", "email": "owner-1@example.com", "is_admin": False}
OTHER = {"cognito_sub": "other-1", "email": "other-1@example.com", "is_admin": False}
ADMIN = {"cognito_sub": "admin-1", "email": "admin-1@example.com", "is_admin": True}

PLAYBOOK = "synthetic-nda-sample"
OTHER_PLAYBOOK = "synthetic-msa-sample"
BUNDLE_HASH = "a" * 64
BASE_EPOCH = 1_700_000_000

BASIS = reviews.PromptBasis(prefix_tokens=30_000, floor_calls=4)
# A long agreement, so a cents figure is big enough that rounding is noise.
DOC_WORDS = 60_000


def _policy() -> dict[str, Any]:
    with open(POLICY_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class OpenRouterBase(unittest.TestCase):
    """moto reviews + submissions + spend tables, the OpenRouter provider (the
    only one with a per-review token basis) and the policy-pinned models."""

    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.table = create_reviews_table(self.ddb)
        create_submissions_table(self.ddb)
        self.ddb.create_table(
            TableName=os.environ["DAILY_SPEND_TABLE"],
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.Table(os.environ["DAILY_SPEND_TABLE"]).put_item(
            Item={"spend_date": time.strftime("%Y-%m-%d", time.gmtime()), "reserved_usd_cents": 5_000}
        )
        self.ddb.create_table(
            TableName=os.environ["MODEL_SETTINGS_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["AUDIT_TABLE"],
            KeySchema=[
                {"AttributeName": "partition", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "partition", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self._prior_provider = os.environ.get("MODEL_PROVIDER")
        os.environ["MODEL_PROVIDER"] = "openrouter"
        self.effective = model_settings.resolve_openrouter_model_ids(None)
        self.next_id = 0

    def tearDown(self) -> None:
        if self._prior_provider is None:
            os.environ.pop("MODEL_PROVIDER", None)
        else:
            os.environ["MODEL_PROVIDER"] = self._prior_provider
        self.mock.stop()

    # -- the real producers, driven ----------------------------------------

    def _finished_row(
        self,
        *,
        playbook_id: str = PLAYBOOK,
        words: int | None = DOC_WORDS,
        actual_cents: int | None = None,
        output_tokens: int | None = None,
        failed: bool = False,
        models: dict[str, str] | None = None,
    ) -> str:
        """One DONE row, written by `_create_review_row` and
        `_write_real_terminal`, optionally carrying the settled cost the way
        `run_real_pipeline` writes it."""
        self.next_id += 1
        review_id = f"00000000-0000-4000-a000-{self.next_id:012d}"
        created = BASE_EPOCH + self.next_id * 1_000
        with patch("time.time", return_value=float(created)):
            reviews._create_review_row(
                review_id, OWNER["cognito_sub"], playbook_id, BUNDLE_HASH, self.ddb,
                word_count=words,
            )
        cost: dict[str, Any] | None = None
        if actual_cents is not None:
            cost = reviews.actual_cost_fields(
                actual_cents, failed=failed, output_tokens=output_tokens
            )
        pair = models or {
            "primary_model_id": self.effective["primary"],
            "critic_model_id": self.effective["critic"],
        }
        with patch("time.time", return_value=float(created + 60)):
            pipeline_runner._mark_running(review_id, self.ddb)
            pipeline_runner._write_real_terminal(
                review_id,
                {"status": "OK", "decision": "REQUEST_CHANGE", "summary": "s"},
                None,
                self.ddb,
                model_ids=pair,
                actual_cost=cost,
            )
        return review_id

    def _row(self, review_id: str) -> dict[str, Any]:
        return self.table.get_item(Key={"review_id": review_id})["Item"]

    def _estimate(self, words: int | None = DOC_WORDS, playbook_id: str = PLAYBOOK,
                  basis: reviews.PromptBasis | None = BASIS) -> dict[str, Any]:
        return reviews.estimate_review_cost(playbook_id, words, basis, self.ddb)

    def _independent_formula_cents(
        self, words: int, basis: reviews.PromptBasis = BASIS,
        out_primary: int | None = None, out_critic: int | None = None,
    ) -> float:
        """The documented formula, priced from the policy artifact with plain
        arithmetic -- not by calling the implementation's helpers."""
        policy = _policy()
        primary, critic = policy["models"]["primary"], policy["models"]["critic"]
        # Pins are one model on both passes, so the critic reads the cache the
        # primary wrote.
        self.assertEqual(self.effective["primary"], self.effective["critic"])
        p_in, p_out = primary["cost_per_million_input_usd"], primary["cost_per_million_output_usd"]
        c_in, c_out = critic["cost_per_million_input_usd"], critic["cost_per_million_output_usd"]
        write, read = primary["cache_write_input_rate_multiplier"], primary["cache_read_input_rate_multiplier"]
        out_primary = primary["approx_tokens_per_review_output"] if out_primary is None else out_primary
        out_critic = critic["approx_tokens_per_review_output"] if out_critic is None else out_critic
        doc = max(1, -(-int(words * 6.0) // 4))  # ceil(words * 6 / 4)
        cacheable = basis.prefix_tokens + doc
        usd = (cacheable * write + 1_500) * p_in / 1e6 + out_primary * p_out / 1e6
        usd += (cacheable * read + 1_500 + out_primary) * c_in / 1e6 + out_critic * c_out / 1e6
        usd += basis.floor_calls * (doc + 1_500) * c_in / 1e6
        return usd * 100


# ---------------------------------------------------------------------------
# 1. The estimate
# ---------------------------------------------------------------------------


class TestEstimateFormula(OpenRouterBase):
    def test_it_matches_the_documented_formula_priced_independently(self):
        body = self._estimate(words=8_000)
        self.assertEqual(body["estimate_basis"], "document")
        expected = self._independent_formula_cents(8_000)
        self.assertAlmostEqual(body["estimated_usd_cents"], expected, delta=1.0)

    def test_it_is_deterministic_for_fixed_inputs(self):
        first = self._estimate(words=8_000)
        second = self._estimate(words=8_000)
        self.assertEqual(first, second)

    def test_it_scales_with_the_document(self):
        small = self._estimate(words=1_000)["estimated_usd_cents"]
        medium = self._estimate(words=10_000)["estimated_usd_cents"]
        large = self._estimate(words=50_000)["estimated_usd_cents"]
        self.assertLess(small, medium)
        self.assertLess(medium, large)
        # And the growth is the document's, not a rounding artefact: a 5x
        # document adds the independently-priced difference.
        self.assertAlmostEqual(
            large - medium,
            self._independent_formula_cents(50_000) - self._independent_formula_cents(10_000),
            delta=1.5,
        )

    def test_it_scales_with_the_playbook_prefix_and_floor(self):
        thin = reviews.PromptBasis(prefix_tokens=5_000, floor_calls=0)
        heavy = reviews.PromptBasis(prefix_tokens=60_000, floor_calls=8)
        self.assertLess(
            self._estimate(basis=thin)["estimated_usd_cents"],
            self._estimate(basis=heavy)["estimated_usd_cents"],
        )

    def test_a_split_model_selection_loses_the_cache_reuse(self):
        """When the critic is not the model that wrote the cache it WRITES its
        own copy (1.25x) instead of reading it (0.1x), so the same inputs cost
        more. Tested at the formula, where the two cases differ only by that."""
        rates = reviews._active_provider_rates(self.ddb)
        cache = reviews._active_cache_multipliers(self.ddb)
        kwargs = {
            "document_tokens": 12_000, "basis": BASIS, "output_tokens": (8_000, 5_000),
            "rates": rates, "cache": cache,
        }
        shared = reviews._expected_review_usd(shared_cache=True, **kwargs)
        split = reviews._expected_review_usd(shared_cache=False, **kwargs)
        self.assertGreater(split, shared)

    def test_the_range_brackets_the_figure_on_the_documented_band(self):
        body = self._estimate(words=8_000)
        low, mid, high = (
            body["estimated_low_usd_cents"],
            body["estimated_usd_cents"],
            body["estimated_high_usd_cents"],
        )
        self.assertLessEqual(low, mid)
        self.assertLessEqual(mid, high)
        self.assertAlmostEqual(low, mid * reviews.DOCUMENT_ESTIMATE_BAND[0], delta=1.0)
        self.assertAlmostEqual(high, mid * reviews.DOCUMENT_ESTIMATE_BAND[1], delta=1.0)
        self.assertEqual(body["sample_size"], 0)

    def test_the_tokens_per_char_constants_match_the_pipelines_own(self):
        """The document ratio is the pipeline's prose ratio; if that estimator
        is recalibrated this estimate must follow, not silently diverge."""
        self.assertEqual(
            reviews.DOCUMENT_CHARS_PER_TOKEN_ESTIMATE,
            primary_review_pass.CHARS_PER_TOKEN_ESTIMATE,
        )
        self.assertEqual(reviews.document_tokens_for_words(1_000), 1_500)


class TestEstimateFallbacks(OpenRouterBase):
    def _assert_policy_default(self, body: dict[str, Any]) -> None:
        constant = reviews.estimate_review_usd_cents(self.ddb)
        self.assertEqual(body["estimated_usd_cents"], constant)
        self.assertEqual(body["estimate_basis"], "policy_default")
        self.assertEqual(body["sample_size"], 0)
        self.assertEqual(body["estimated_low_usd_cents"], int(round(constant * 0.5)))  # noqa: RUF046
        self.assertEqual(body["estimated_high_usd_cents"], int(round(constant * 2.0)))  # noqa: RUF046

    def test_no_word_count_gives_the_constant(self):
        self._assert_policy_default(self._estimate(words=None))
        self._assert_policy_default(self._estimate(words=0))

    def test_no_playbook_gives_the_constant(self):
        self._assert_policy_default(self._estimate(playbook_id=""))

    def test_an_underivable_playbook_basis_gives_the_constant(self):
        self._assert_policy_default(self._estimate(basis=None))

    def test_the_route_function_with_no_inputs_is_exactly_the_old_constant(self):
        body = reviews.review_cost_estimate(self.ddb)
        self.assertEqual(body["estimated_usd_cents"], reviews.estimate_review_usd_cents(self.ddb))
        self.assertEqual(body["estimate_basis"], "policy_default")

    def test_a_provider_with_no_per_review_basis_reports_nothing(self):
        os.environ["MODEL_PROVIDER"] = "bedrock"
        body = self._estimate(words=8_000)
        self.assertIsNone(body["estimated_usd_cents"])
        self.assertIsNone(body["estimated_low_usd_cents"])
        self.assertIsNone(body["estimated_high_usd_cents"])
        self.assertIsNone(body["estimate_basis"])
        self.assertGreater(body["worst_case_reservation_usd_cents"], 0)


class TestMeasuredOutputAndRange(OpenRouterBase):
    MIN = reviews.REVIEW_COST_MIN_SAMPLE

    def test_the_min_sample_is_five(self):
        self.assertEqual(self.MIN, 5)

    def test_output_stays_on_the_policy_basis_below_the_minimum_sample(self):
        baseline = self._estimate()["estimated_usd_cents"]
        for _ in range(self.MIN - 1):
            self._finished_row(words=None, actual_cents=100, output_tokens=40_000)
        self.assertEqual(self._estimate()["estimated_usd_cents"], baseline)

    def test_output_switches_to_the_measured_median_at_the_minimum_sample(self):
        baseline = self._estimate()["estimated_usd_cents"]
        # Rows carry no word count, so only the OUTPUT path is exercised (the
        # range needs a word count per row and stays on the fixed band).
        for _ in range(self.MIN):
            self._finished_row(words=None, actual_cents=100, output_tokens=40_000)
        body = self._estimate()
        self.assertEqual(body["estimate_basis"], "document")
        self.assertGreater(body["estimated_usd_cents"], baseline)
        # 40,000 measured total, split by the policy's primary:critic output ratio.
        policy = _policy()
        primary_out = policy["models"]["primary"]["approx_tokens_per_review_output"]
        critic_out = policy["models"]["critic"]["approx_tokens_per_review_output"]
        out_primary = round(40_000 * primary_out / (primary_out + critic_out))
        expected = self._independent_formula_cents(
            DOC_WORDS, out_primary=out_primary, out_critic=40_000 - out_primary
        )
        self.assertAlmostEqual(body["estimated_usd_cents"], expected, delta=1.0)

    def test_the_measured_median_is_the_nearest_rank_median(self):
        for tokens in (1_000, 2_000, 30_000, 31_000, 90_000):
            self._finished_row(words=None, actual_cents=100, output_tokens=tokens)
        # Median of the five is 30,000.
        policy = _policy()
        primary_out = policy["models"]["primary"]["approx_tokens_per_review_output"]
        critic_out = policy["models"]["critic"]["approx_tokens_per_review_output"]
        out_primary = round(30_000 * primary_out / (primary_out + critic_out))
        expected = self._independent_formula_cents(
            DOC_WORDS, out_primary=out_primary, out_critic=30_000 - out_primary
        )
        self.assertAlmostEqual(self._estimate()["estimated_usd_cents"], expected, delta=1.0)

    def _seed_ratio_rows(self, factors: list[float], words: int = DOC_WORDS) -> None:
        predicted = self._estimate(words=words)["estimated_usd_cents"]
        for factor in factors:
            self._finished_row(words=words, actual_cents=int(round(predicted * factor)))  # noqa: RUF046

    def test_a_thin_sample_keeps_the_fixed_band(self):
        self._seed_ratio_rows([0.5, 0.6, 0.7, 0.8])
        body = self._estimate()
        self.assertEqual(body["estimate_basis"], "document")
        self.assertEqual(body["sample_size"], 0)

    def test_the_range_is_the_measured_p25_to_p75_at_the_minimum_sample(self):
        predicted = self._estimate()["estimated_usd_cents"]
        self.assertGreater(predicted, 200, "pick a figure big enough that cents rounding is noise")
        self._seed_ratio_rows([0.6, 0.8, 1.0, 1.2, 1.4])
        body = self._estimate()
        self.assertEqual(body["estimate_basis"], "measured")
        self.assertEqual(body["sample_size"], 5)
        # Nearest rank over five: p25 = 2nd (0.8), p50 = 3rd (1.0), p75 = 4th (1.2).
        self.assertAlmostEqual(body["estimated_low_usd_cents"], predicted * 0.8, delta=2)
        self.assertAlmostEqual(body["estimated_usd_cents"], predicted * 1.0, delta=2)
        self.assertAlmostEqual(body["estimated_high_usd_cents"], predicted * 1.2, delta=2)

    def test_a_systematically_cheaper_history_moves_the_figure_down(self):
        predicted = self._estimate()["estimated_usd_cents"]
        self._seed_ratio_rows([0.4, 0.4, 0.4, 0.4, 0.4])
        body = self._estimate()
        self.assertEqual(body["estimate_basis"], "measured")
        self.assertLess(body["estimated_usd_cents"], predicted)
        self.assertAlmostEqual(body["estimated_usd_cents"], predicted * 0.4, delta=2)

    def test_one_wild_row_is_clamped_not_allowed_to_drag_the_range(self):
        predicted = self._estimate()["estimated_usd_cents"]
        self._seed_ratio_rows([1.0, 1.0, 1.0, 1.0, 400.0])
        body = self._estimate()
        self.assertLessEqual(
            body["estimated_high_usd_cents"], predicted * reviews.COST_RATIO_CLAMP[1] + 1
        )

    def test_the_range_follows_the_document_not_just_the_history(self):
        self._seed_ratio_rows([0.6, 0.8, 1.0, 1.2, 1.4])
        small = self._estimate(words=1_000)
        large = self._estimate(words=30_000)
        self.assertEqual(small["estimate_basis"], "measured")
        self.assertLess(small["estimated_usd_cents"], large["estimated_usd_cents"])
        self.assertLess(small["estimated_high_usd_cents"], large["estimated_low_usd_cents"])

    def test_rows_that_are_not_measurements_of_the_next_review_are_ignored(self):
        predicted = self._estimate()["estimated_usd_cents"]
        cost = int(round(predicted * 0.3))  # noqa: RUF046
        # Another playbook, a failed review, an old row with no cost, a row
        # that ran on a different model pair: none counts, even at five.
        for _ in range(self.MIN):
            self._finished_row(playbook_id=OTHER_PLAYBOOK, actual_cents=cost)
        for _ in range(self.MIN):
            self._finished_row(actual_cents=cost, failed=True)
        for _ in range(self.MIN):
            self._finished_row(actual_cents=None)
        for _ in range(self.MIN):
            self._finished_row(
                actual_cents=cost,
                models={"primary_model_id": "other/model", "critic_model_id": "other/model"},
            )
        body = self._estimate()
        self.assertEqual(body["estimate_basis"], "document")
        self.assertEqual(body["sample_size"], 0)
        self.assertEqual(body["estimated_usd_cents"], predicted)

    def test_an_unreadable_history_degrades_to_the_formula(self):
        baseline = self._estimate()
        with patch.object(reviews, "_sample_finished_reviews", side_effect=RuntimeError("ddb")):
            self.assertEqual(self._estimate(), baseline)


class TestPromptBasis(unittest.TestCase):
    """`pipeline_runner.review_prompt_basis` -- the playbook half."""

    def test_a_stored_v1_playbook_has_a_deterministic_nonzero_prefix(self):
        first = pipeline_runner.review_prompt_basis(PLAYBOOK, None, None)
        second = pipeline_runner.review_prompt_basis(PLAYBOOK, None, None)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first, second)
        # The prefix is the system blocks AND the forced-tool schema.
        self.assertGreater(first.prefix_tokens, pipeline_runner._tool_schema_tokens())
        self.assertEqual(first.floor_calls, 0)

    def test_an_unknown_playbook_has_no_basis_rather_than_an_error(self):
        self.assertIsNone(pipeline_runner.review_prompt_basis("no-such-playbook", None, None))

    def test_an_opf_playbook_is_sized_from_the_blocks_a_review_would_send(self):
        with open(OPF_FIXTURE_PATH, encoding="utf-8") as fh:
            opf_doc = json.load(fh)
        bundle = {
            "opf_bundle_v2": {
                "opf": opf_doc,
                "overrides": None,
                "knowledge_memo": None,
                "accepted_stub_basis": True,
            },
            "playbook": {"metadata": {}},
        }
        with patch.object(pipeline_runner, "_load_playbook_bundle", return_value=bundle):
            basis = pipeline_runner.review_prompt_basis("opf-playbook", None, None)
        self.assertIsNotNone(basis)
        assert basis is not None
        self.assertGreater(basis.prefix_tokens, pipeline_runner._tool_schema_tokens())
        # The fixture's Floor has invariants, and each is one judge call.
        self.assertGreaterEqual(basis.floor_calls, 1)

    def test_a_bigger_playbook_has_a_bigger_prefix(self):
        with open(OPF_FIXTURE_PATH, encoding="utf-8") as fh:
            opf_doc = json.load(fh)
        wrapper = {"opf": opf_doc, "overrides": None, "knowledge_memo": None,
                   "accepted_stub_basis": True}
        with patch.object(
            pipeline_runner, "_load_playbook_bundle",
            return_value={"opf_bundle_v2": wrapper, "playbook": {"metadata": {}}},
        ):
            opf_basis = pipeline_runner.review_prompt_basis("opf-playbook", None, None)
        v1_basis = pipeline_runner.review_prompt_basis(PLAYBOOK, None, None)
        assert opf_basis is not None and v1_basis is not None
        self.assertNotEqual(opf_basis.prefix_tokens, v1_basis.prefix_tokens)


# ---------------------------------------------------------------------------
# 2. Persisted actual cost
# ---------------------------------------------------------------------------

REVIEW_ID = "00000000-0000-4000-a000-0000000000c1"


class _Usage:
    """A client double with the one attribute settlement reads."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.cumulative_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}


class TestActualCostIsPersisted(OpenRouterBase):
    """`run_real_pipeline`, driven for real with only the model, the document
    fetch and the object-store writes replaced. Provider is Bedrock here so the
    price is the module constants (5.50 in / 27.50 out per million): 100,000 in
    and 20,000 out is 55 + 55 = 110 cents, exactly."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["MODEL_PROVIDER"] = "bedrock"

    def _payload(self) -> dict[str, Any]:
        return {
            "review_id": REVIEW_ID,
            "owner_sub": OWNER["cognito_sub"],
            "playbook_id": PLAYBOOK,
            "upload_s3_key": f"uploads/{OWNER['cognito_sub']}/{REVIEW_ID}/in.docx",
            "release_bundle_hash": BUNDLE_HASH,
        }

    def _drive(self, client: Any, spine: Any) -> dict[str, Any]:
        reviews._create_review_row(
            REVIEW_ID, OWNER["cognito_sub"], PLAYBOOK, BUNDLE_HASH, self.ddb, word_count=900
        )
        patches = (
            patch.object(reviews, "verify_submission_time_bundle", return_value={"verified": True}),
            patch.object(pipeline_runner, "_load_playbook_bundle", return_value={}),
            patch.object(
                pipeline_runner, "_bundle_with_openrouter_model_ids",
                side_effect=lambda bundle, dynamodb_resource=None: bundle,
            ),
            patch.object(pipeline_runner, "_fetch_upload_bytes", return_value=b"docx"),
            patch.object(pipeline_runner, "_write_real_output", return_value=None),
            patch.object(pipeline_runner, "_write_real_analysis", return_value=None),
            patch.object(pipeline_runner.review_spine, "run_review", side_effect=spine),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        pipeline_runner.run_real_pipeline(
            REVIEW_ID, self._payload(), dynamodb_resource=self.ddb, s3_client=object(),
            model_client=client,
        )
        return self._row(REVIEW_ID)

    def test_a_done_review_records_what_it_settled_at(self):
        row = self._drive(
            _Usage(100_000, 20_000),
            lambda *a, **k: {"status": "OK", "decision": "ACCEPT", "summary": "fine"},
        )
        self.assertEqual(row["status"], "DONE")
        self.assertEqual(int(row["actual_usd_cents"]), 110)
        self.assertIs(row["actual_cost_failed"], False)
        # The measured token totals ride along for the estimator's sample.
        self.assertEqual(int(row["actual_input_tokens"]), 100_000)
        self.assertEqual(int(row["actual_output_tokens"]), 20_000)

    def test_the_row_and_the_daily_ledger_carry_the_same_figure(self):
        """One number: the row's cost is what `_actual_cents_from_client`
        settles the day's counter at, not a second computation."""
        client = _Usage(100_000, 20_000)
        row = self._drive(
            client, lambda *a, **k: {"status": "OK", "decision": "ACCEPT", "summary": "fine"}
        )
        self.assertEqual(
            int(row["actual_usd_cents"]),
            pipeline_runner._actual_cents_from_client(client, self.ddb, {}),
        )

    def test_the_cost_is_on_the_row_with_the_terminal_status_not_after_it(self):
        """The first poll that sees DONE must see its cost: both land in the one
        update, so there is no window in which the row is DONE and costless."""
        seen: list[dict[str, Any]] = []
        real = pipeline_runner._write_real_terminal

        def spy(review_id, result, key, ddb, **kwargs):
            seen.append(kwargs.get("actual_cost") or {})
            return real(review_id, result, key, ddb, **kwargs)

        with patch.object(pipeline_runner, "_write_real_terminal", side_effect=spy):
            self._drive(
                _Usage(100_000, 20_000),
                lambda *a, **k: {"status": "OK", "decision": "ACCEPT", "summary": "x"},
            )
        self.assertEqual(seen[0]["actual_usd_cents"], 110)

    def test_a_failed_review_that_made_model_calls_records_its_cost(self):
        def spine(*args: Any, on_progress=None, **kwargs: Any) -> Any:
            raise RuntimeError("model fell over mid-review")

        row = self._drive(_Usage(100_000, 20_000), spine)
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(int(row["actual_usd_cents"]), 110)
        self.assertIs(row["actual_cost_failed"], True)

    def test_a_failed_review_that_made_no_model_call_records_zero(self):
        def spine(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("failed before any call")

        row = self._drive(object(), spine)  # no `cumulative_usage` at all
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(int(row["actual_usd_cents"]), 0)
        self.assertIs(row["actual_cost_failed"], True)
        self.assertNotIn("actual_input_tokens", row)

    def test_a_computed_failure_result_is_marked_failed_and_still_costed(self):
        row = self._drive(
            _Usage(100_000, 20_000),
            lambda *a, **k: {"status": "ERROR", "reason": "leakage_detected", "decision": None},
        )
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(int(row["actual_usd_cents"]), 110)
        self.assertIs(row["actual_cost_failed"], True)

    def test_a_pricing_fault_leaves_the_row_unknown_and_does_not_fail_the_review(self):
        with patch.object(
            pipeline_runner, "_actual_cents_from_client", side_effect=RuntimeError("policy gone")
        ), patch.object(pipeline_runner, "_settled_cents", return_value=0):
            row = self._drive(
                _Usage(1, 1),
                lambda *a, **k: {"status": "OK", "decision": "ACCEPT", "summary": "fine"},
            )
        self.assertEqual(row["status"], "DONE")
        self.assertNotIn("actual_usd_cents", row)


class TestRecordActualCost(OpenRouterBase):
    def _pending(self) -> str:
        reviews._create_review_row(
            REVIEW_ID, OWNER["cognito_sub"], PLAYBOOK, BUNDLE_HASH, self.ddb
        )
        return REVIEW_ID

    def test_it_stamps_the_row_and_the_first_write_wins(self):
        review_id = self._pending()
        reviews.record_actual_cost(review_id, self.ddb, 42, failed=False, output_tokens=7)
        reviews.record_actual_cost(review_id, self.ddb, 999, failed=True, output_tokens=99)
        row = self._row(review_id)
        self.assertEqual(int(row["actual_usd_cents"]), 42)
        self.assertIs(row["actual_cost_failed"], False)
        self.assertEqual(int(row["actual_output_tokens"]), 7)

    def test_it_never_creates_a_row_for_an_id_that_is_gone(self):
        reviews.record_actual_cost("00000000-0000-4000-a000-0000000000ff", self.ddb, 5)
        self.assertEqual(self.table.scan()["Items"], [])

    def test_it_never_raises_into_a_review(self):
        class Exploding:
            def Table(self, _name):  # noqa: N802
                raise RuntimeError("DynamoDB is having a moment")

        reviews.record_actual_cost(REVIEW_ID, Exploding(), 5)

    def test_a_negative_figure_is_clamped_not_written_negative(self):
        review_id = self._pending()
        reviews.record_actual_cost(review_id, self.ddb, -50)
        self.assertEqual(int(self._row(review_id)["actual_usd_cents"]), 0)

    def test_the_cancel_settle_records_the_ledger_priced_cost(self):
        review_id = self._pending()
        submissions = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions.put_item(
            Item={
                "idempotency_key": "idem-1", "review_id": review_id,
                "spend_reservation_id": "res-1",
            }
        )
        reviews.settle_reservation_for_cancel(review_id, self.ddb)
        row = self._row(review_id)
        # No model-invocation ledger configured: nothing to price, settled at 0.
        self.assertEqual(int(row["actual_usd_cents"]), 0)
        self.assertIs(row["actual_cost_failed"], False)

    def test_the_recovery_settle_marks_the_stranded_review_failed(self):
        review_id = self._pending()
        submissions = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        submissions.put_item(
            Item={
                "idempotency_key": "idem-2", "review_id": review_id,
                "spend_reservation_id": "res-2",
            }
        )
        reviews.settle_reservation_for_cancel(review_id, self.ddb, failed=True)
        self.assertIs(self._row(review_id)["actual_cost_failed"], True)

    def test_a_legacy_row_has_no_cost_fields_and_reads_as_unknown(self):
        review_id = self._pending()
        row = self._row(review_id)
        for field in reviews.REVIEW_COST_ROW_FIELDS:
            self.assertNotIn(field, row)
        detail = reviews.get_review_detail(review_id, OWNER, self.ddb)
        for field in reviews.REVIEW_COST_ROW_FIELDS:
            self.assertIsNone(detail[field])


class TestSubmitTimeEstimate(OpenRouterBase):
    def test_the_estimate_shown_at_submit_is_recorded_on_the_row(self):
        reviews._create_review_row(
            REVIEW_ID, OWNER["cognito_sub"], PLAYBOOK, BUNDLE_HASH, self.ddb,
            word_count=5_000, estimated_usd_cents=81,
        )
        self.assertEqual(int(self._row(REVIEW_ID)["estimated_usd_cents"]), 81)

    def test_no_estimate_means_no_attribute_never_a_null(self):
        reviews._create_review_row(
            REVIEW_ID, OWNER["cognito_sub"], PLAYBOOK, BUNDLE_HASH, self.ddb
        )
        self.assertNotIn("estimated_usd_cents", self._row(REVIEW_ID))

    def test_a_zero_or_bool_estimate_is_not_recorded(self):
        for bad in (0, -3, True):
            with self.subTest(bad=bad):
                review_id = f"00000000-0000-4000-a000-00000000bb{int(bad) + 5:02d}"
                reviews._create_review_row(
                    review_id, OWNER["cognito_sub"], PLAYBOOK, BUNDLE_HASH, self.ddb,
                    estimated_usd_cents=bad,  # type: ignore[arg-type]
                )
                self.assertNotIn("estimated_usd_cents", self._row(review_id))

    def test_the_route_computes_it_server_side_from_the_counted_words(self):
        with patch.object(pipeline_runner, "review_prompt_basis", return_value=BASIS):
            cents = review_routes._submitted_cost_estimate_cents(PLAYBOOK, 8_000, self.ddb, object())
        self.assertEqual(
            cents,
            reviews.review_cost_estimate(
                self.ddb, playbook_id=PLAYBOOK, words=8_000, basis=BASIS
            )["estimated_usd_cents"],
        )

    def test_it_is_fail_soft_without_a_word_count_or_a_basis_or_on_error(self):
        with patch.object(pipeline_runner, "review_prompt_basis", return_value=BASIS):
            self.assertIsNone(
                review_routes._submitted_cost_estimate_cents(PLAYBOOK, None, self.ddb, object())
            )
        with patch.object(pipeline_runner, "review_prompt_basis", return_value=None):
            self.assertIsNone(
                review_routes._submitted_cost_estimate_cents(PLAYBOOK, 8_000, self.ddb, object())
            )
        with patch.object(pipeline_runner, "review_prompt_basis", side_effect=RuntimeError("x")):
            self.assertIsNone(
                review_routes._submitted_cost_estimate_cents(PLAYBOOK, 8_000, self.ddb, object())
            )


# ---------------------------------------------------------------------------
# 3. Allowlists and visibility
# ---------------------------------------------------------------------------


class TestAllowlists(OpenRouterBase):
    def test_the_estimate_route_field_tuple_is_exact(self):
        self.assertEqual(
            reviews.REVIEW_COST_ESTIMATE_FIELDS,
            (
                "estimated_usd_cents",
                "estimated_low_usd_cents",
                "estimated_high_usd_cents",
                "estimate_basis",
                "sample_size",
                "worst_case_reservation_usd_cents",
            ),
        )
        body = reviews.review_cost_estimate(
            self.ddb, playbook_id=PLAYBOOK, words=8_000, basis=BASIS
        )
        self.assertEqual(tuple(body), reviews.REVIEW_COST_ESTIMATE_FIELDS)

    def test_the_per_review_cost_field_tuple_is_exact(self):
        self.assertEqual(
            reviews.REVIEW_COST_ROW_FIELDS,
            ("estimated_usd_cents", "actual_usd_cents", "actual_cost_failed"),
        )

    def _finished(self, **kwargs: Any) -> str:
        return self._finished_row(actual_cents=77, **kwargs)

    def test_the_owners_detail_carries_exactly_the_cost_fields_and_no_token_counts(self):
        review_id = self._finished(output_tokens=1_234)
        detail = reviews.get_review_detail(review_id, OWNER, self.ddb)
        self.assertEqual(detail["actual_usd_cents"], 77)
        self.assertIs(detail["actual_cost_failed"], False)
        for field in reviews.REVIEW_COST_ROW_FIELDS:
            self.assertIn(field, detail)
        # The estimator's own inputs are not a reader's business.
        for internal in ("actual_input_tokens", "actual_output_tokens"):
            self.assertNotIn(internal, detail)

    def test_the_history_row_carries_the_cost_fields_and_no_token_counts(self):
        review_id = self._finished(output_tokens=1_234)
        page = reviews.list_reviews(OWNER, self.ddb, owner_scoped=True)
        row = next(item for item in page["items"] if item["review_id"] == review_id)
        self.assertEqual(row["actual_usd_cents"], 77)
        self.assertIn("estimated_usd_cents", row)
        self.assertIn("actual_cost_failed", row)
        for internal in ("actual_input_tokens", "actual_output_tokens"):
            self.assertNotIn(internal, row)
        self.assertTrue(set(reviews.REVIEW_COST_ROW_FIELDS) <= set(reviews._REVIEW_LIST_ITEM_FIELDS))

    def test_a_non_owner_cannot_read_another_reviewers_cost(self):
        review_id = self._finished()
        with self.assertRaises(HTTPException) as caught:
            reviews.get_review_detail(review_id, OTHER, self.ddb)
        self.assertEqual(caught.exception.status_code, 404)
        # And the listing never includes it either, even asked for broadly.
        for owner_scoped in (True, False):
            page = reviews.list_reviews(OTHER, self.ddb, owner_scoped=owner_scoped)
            self.assertEqual([i["review_id"] for i in page["items"]], [])

    def test_an_admin_sees_every_reviewers_cost(self):
        review_id = self._finished()
        self.assertEqual(
            reviews.get_review_detail(review_id, ADMIN, self.ddb)["actual_usd_cents"], 77
        )
        page = reviews.list_reviews(ADMIN, self.ddb)
        self.assertIn(review_id, [i["review_id"] for i in page["items"]])

    def test_the_cost_fields_carry_no_budget_or_model_id(self):
        review_id = self._finished()
        detail = reviews.get_review_detail(review_id, OWNER, self.ddb)
        for field in reviews.REVIEW_COST_ROW_FIELDS:
            rendered = json.dumps(detail[field])
            for model_id in self.effective.values():
                self.assertNotIn(model_id, rendered)
        for budget in ("daily_cap_usd_cents", "spent_today_usd_cents", "remaining_usd_cents"):
            self.assertNotIn(budget, reviews.REVIEW_COST_ROW_FIELDS)
            self.assertNotIn(budget, reviews.REVIEW_COST_ESTIMATE_FIELDS)


class TestTheNonAdminRouteStillLeaksNoOperatorFacts(OpenRouterBase):
    def setUp(self) -> None:
        super().setUp()
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        backend_main.app.dependency_overrides[backend_main.get_s3_client] = lambda: object()
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = lambda: OWNER
        self.client = TestClient(backend_main.app)

    def tearDown(self) -> None:
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def test_with_inputs_it_returns_exactly_the_allowlist_and_nothing_operational(self):
        model_settings.set_daily_spend_cap_cents(4321, ADMIN, self.ddb)
        with patch.object(pipeline_runner, "review_prompt_basis", return_value=BASIS):
            response = self.client.get(
                "/api/review-cost-estimate", params={"playbook_id": PLAYBOOK, "words": 8000}
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), set(reviews.REVIEW_COST_ESTIMATE_FIELDS))
        self.assertEqual(body["estimate_basis"], "document")
        for budget_field in (
            "daily_cap_usd_cents", "spent_today_usd_cents", "remaining_usd_cents",
            "cap_setting", "today", "days", "primary_model_id", "critic_model_id",
        ):
            self.assertNotIn(budget_field, body)
        for secret in ("4321", *self.effective.values()):
            self.assertNotIn(secret, response.text)

    def test_without_inputs_it_is_the_old_constant_and_still_the_same_key_set(self):
        body = self.client.get("/api/review-cost-estimate").json()
        self.assertEqual(set(body), set(reviews.REVIEW_COST_ESTIMATE_FIELDS))
        self.assertEqual(body["estimated_usd_cents"], reviews.estimate_review_usd_cents(self.ddb))
        self.assertEqual(body["estimate_basis"], "policy_default")

    def test_an_unreadable_playbook_still_answers_with_the_constant(self):
        with patch.object(pipeline_runner, "review_prompt_basis", return_value=None):
            body = self.client.get(
                "/api/review-cost-estimate", params={"playbook_id": "gone", "words": 100}
            ).json()
        self.assertEqual(body["estimate_basis"], "policy_default")

    def test_the_document_aware_figure_moves_with_the_words_parameter(self):
        with patch.object(pipeline_runner, "review_prompt_basis", return_value=BASIS):
            small = self.client.get(
                "/api/review-cost-estimate", params={"playbook_id": PLAYBOOK, "words": 500}
            ).json()
            large = self.client.get(
                "/api/review-cost-estimate", params={"playbook_id": PLAYBOOK, "words": 40000}
            ).json()
        self.assertLess(small["estimated_usd_cents"], large["estimated_usd_cents"])

    def test_a_malformed_words_parameter_is_a_422_not_a_guess(self):
        for bad in ("-1", "many"):
            response = self.client.get(
                "/api/review-cost-estimate", params={"playbook_id": PLAYBOOK, "words": bad}
            )
            self.assertEqual(response.status_code, 422, bad)

    def test_the_bedrock_path_still_reports_no_expected_figure(self):
        os.environ["MODEL_PROVIDER"] = "bedrock"
        body = self.client.get(
            "/api/review-cost-estimate", params={"playbook_id": PLAYBOOK, "words": 100}
        ).json()
        self.assertIsNone(body["estimated_usd_cents"])
        self.assertIsNone(body["estimated_low_usd_cents"])
        self.assertGreater(body["worst_case_reservation_usd_cents"], 0)


if __name__ == "__main__":
    unittest.main()
