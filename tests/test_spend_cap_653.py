#!/usr/bin/env python3
"""
Tests for the spend surface (issue #653, epic #649) -- the daily cap, what
today has spent, what is left, and what the next review will cost, plus the
cap becoming an admin-settable stored setting instead of a line in a prod
compose file.

What is actually at stake here, and therefore what these tests drive:

  1. **The reported totals are the ledger's, not a story about it.** Every
     figure on the screen is asserted after driving the REAL producers --
     `reviews.reserve_spend`, `reviews.settle_spend`,
     `reviews.record_preflight_spend` -- against moto, never by hand-writing
     a `daily_spend` row. Nothing outside those functions writes that row in
     production, so a fixture that seeded one directly would be asserting
     over a shape the system reaches only in the test file. Each total is
     also asserted to MOVE when another review reserves, so a constant
     could not pass.
  2. **The cap an admin sets is the cap the reservation path enforces.**
     Not "the cap the screen displays" -- the number that decides whether a
     submission is admitted. Lowering it below what today has already
     reserved must refuse the next review with the existing 429, driven
     through the real `reserve_spend`, because a cap that only changes a
     label is worse than no cap: it reads as protection that is not there.
  3. **The estimate tracks the model selection.** `next review` is priced
     from the models an admin actually chose (#445), so changing the choice
     has to change the number. Asserted in BOTH directions (dearer and
     cheaper) against rates read out of the on-disk policy artifact, so a
     repricing cannot leave the test asserting stale money. BOTH figures are
     driven -- the worst case a submission reserves and the cost a document of
     ordinary size is expected to incur -- because the gap between them is
     what the issue was filed on ($0.2742 measured against a $1.10 worst case
     on 2026-09-01) and reporting only one of them misinforms in a direction.
  3a. **The day's first reservation is not exempt from the cap.**
     `reserve_spend`'s condition admits unconditionally while
     `reserved_usd_cents` does not exist, so a cap smaller than one review
     used to let exactly one review through on a fresh day. Unreachable while
     the cap was a deploy-time constant; reachable the moment it is a form
     field, which is what this issue adds. Driven through the real
     reservation path, in both directions, so the refusal is the cap biting
     rather than the path being broken.
  4. **Precedence, matching the API key's and the selection's exactly:**
     admin row > DAILY_SPEND_CAP_USD_CENTS > the built-in default -- with a
     stored value outside the accepted bounds reading as "no admin cap",
     never as a silently clamped one.
  5. **Degraded reads, asserted in the direction that hurts.** A DynamoDB
     blip reading the cap degrades to the deployment's configured ceiling
     rather than failing every submission -- which means it FAILS OPEN
     whenever the admin's stored cap was LOWER than the deployment's. That
     case is seeded and asserted as it actually behaves (a 5x rise, pinned
     as a known deliberate degradation bounded by
     DAILY_SPEND_CAP_USD_CENTS), not named away by a test that only
     exercises the no-stored-cap path.
  6. **The write is admin-only, bounded, and audited**, and the route that
     now also serves the cap still refuses a non-admin and still never echoes
     the API key.

moto-mocked DynamoDB only -- no live AWS, no network (standing rule 4).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import logging
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUTH_SETTINGS_TABLE", "contract-toaster-auth-settings-test")
os.environ.setdefault("MODEL_SETTINGS_TABLE", "contract-toaster-model-settings-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")

import boto3  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.admin_dashboard as admin_dashboard  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.model_settings as model_settings  # noqa: E402
import src.reviews as reviews  # noqa: E402

POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"

ADMIN_SUB = "admin-1"
ADMIN = {"cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "is_admin": True}
NON_ADMIN = {"cognito_sub": "reviewer-1", "email": "reviewer-1@example.com", "is_admin": False}

# Non-hex body on purpose: a real-length hex body matches secret scanners'
# OpenRouter key pattern. Only the length is ever validated.
FAKE_KEY = "sk-or-v1-TEST-FIXTURE-NOT-A-REAL-KEY-0000-beef"

# The two ends of the `selectable` price range, looked up by id out of the
# on-disk policy rather than carrying hardcoded rates.
DEAREST_ID = "openai/gpt-5.6-sol"
CHEAPEST_ID = "deepseek/deepseek-v4-pro"


def _policy() -> dict:
    with open(POLICY_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _no_env_overrides(**extra: str):
    """Neutralise the break-glass model-id env vars (and, by default, the
    deployment cap) WITHOUT `clear=True`.

    `patch.dict(os.environ, {}, clear=True)` also unsets MODEL_SETTINGS_TABLE,
    which silently turns the settings store off -- a resolution test written
    that way passes for the wrong reason (no store, so nothing to find).
    """
    env = {"OPENROUTER_PRIMARY_MODEL_ID": "", "OPENROUTER_CRITIC_MODEL_ID": ""}
    env.update(extra)
    return patch.dict(os.environ, env)


class ExplodingResource:
    """A DynamoDB resource that fails the way a transient blip does: the very
    first `.Table()` raises."""

    def Table(self, _name):  # noqa: N802 - boto3 resource API shape
        raise RuntimeError("DynamoDB is having a moment")


class SpendTestBase(unittest.TestCase):
    """moto tables + the OpenRouter provider, which is the only target where
    the admin model selection (and therefore #653's "the estimate tracks the
    selection") means anything."""

    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
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
        self.ddb.create_table(
            TableName=os.environ["DAILY_SPEND_TABLE"],
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self._prior_provider = os.environ.get("MODEL_PROVIDER")
        os.environ["MODEL_PROVIDER"] = "openrouter"

    def tearDown(self):
        if self._prior_provider is None:
            os.environ.pop("MODEL_PROVIDER", None)
        else:
            os.environ["MODEL_PROVIDER"] = self._prior_provider
        self._mock_aws.stop()

    # -- helpers ------------------------------------------------------------

    def _audit_rows(self):
        return self.ddb.Table(os.environ["AUDIT_TABLE"]).scan().get("Items", [])

    def _spend_row(self) -> dict:
        table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        key = {"spend_date": time.strftime("%Y-%m-%d", time.gmtime())}
        return table.get_item(Key=key).get("Item") or {}

    def _reserved_cents(self) -> int:
        return int(self._spend_row().get("reserved_usd_cents", 0))

    @staticmethod
    def _rates(model_id: str) -> dict:
        policy = _policy()
        for entry in policy.get("selectable", []):
            if entry["model_id"] == model_id:
                return entry
        for role in ("primary", "critic"):
            if policy["models"][role]["model_id"] == model_id:
                return policy["models"][role]
        raise AssertionError(f"{model_id!r} is neither selectable nor a pin")

    def _expected_reservation_cents(self, primary_id: str, critic_id: str) -> int:
        """The reservation formula recomputed from the artifact, independently
        of backend/src/reviews.py, so this cannot pass against a mirror."""
        attempts = 1 + reviews.MAX_RETRIES_PER_PASS + reviews.MAX_TRUNCATION_RETRIES_PER_PASS
        total = 0.0
        for model_id in (primary_id, critic_id):
            rates = self._rates(model_id)
            total += (
                reviews.MAX_INPUT_TOKENS * rates["cost_per_million_input_usd"] / 1_000_000
                + reviews.MAX_OUTPUT_TOKENS * rates["cost_per_million_output_usd"] / 1_000_000
            )
        return int(round(attempts * total * 100))

    def _ledger(self, **env: str) -> dict:
        with _no_env_overrides(**env):
            return admin_dashboard.get_spend_ledger(ADMIN, self.ddb)


# ---------------------------------------------------------------------------
# (1) The cap is a stored setting now: precedence, bounds, clearing, audit.
# ---------------------------------------------------------------------------


class TestTheCapResolution(SpendTestBase):
    def test_nothing_stored_uses_the_deployment_cap(self):
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="3500"):
            self.assertEqual(
                model_settings.resolve_daily_spend_cap_cents(self.ddb), 3500
            )

    def test_nothing_stored_and_no_env_uses_the_built_in_default(self):
        env = dict(os.environ)
        env.pop("DAILY_SPEND_CAP_USD_CENTS", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                model_settings.resolve_daily_spend_cap_cents(self.ddb),
                model_settings.DAILY_SPEND_CAP_USD_CENTS_DEFAULT,
            )

    def test_the_stored_cap_wins_over_the_deployment(self):
        model_settings.set_daily_spend_cap_cents(777, ADMIN, self.ddb)
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="3500"):
            self.assertEqual(model_settings.resolve_daily_spend_cap_cents(self.ddb), 777)

    def test_clearing_falls_back_to_the_deployment_rather_than_freezing_its_value(self):
        """"Use the deployment's cap" has to keep TRACKING the deployment. A
        clear that stored the deployment's current number would stop tracking
        it the moment the deployment changed."""
        model_settings.set_daily_spend_cap_cents(777, ADMIN, self.ddb)
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="3500"):
            model_settings.set_daily_spend_cap_cents(None, ADMIN, self.ddb)
            self.assertEqual(model_settings.resolve_daily_spend_cap_cents(self.ddb), 3500)
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="4100"):
            self.assertEqual(model_settings.resolve_daily_spend_cap_cents(self.ddb), 4100)

    def test_an_out_of_bounds_stored_value_reads_as_no_admin_cap(self):
        """A row that somehow carries a value outside the accepted bounds must
        fall through to the deployment's cap, not be clamped into a ceiling
        nobody chose."""
        self.ddb.Table(os.environ["MODEL_SETTINGS_TABLE"]).put_item(
            Item={
                "setting_id": model_settings.SPEND_SETTING_ID,
                "daily_cap_usd_cents": model_settings.MAX_DAILY_SPEND_CAP_USD_CENTS + 1,
            }
        )
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="3500"):
            self.assertEqual(model_settings.resolve_daily_spend_cap_cents(self.ddb), 3500)
            self.assertEqual(
                model_settings.get_spend_cap_settings(ADMIN, self.ddb)["daily_cap_source"],
                "env",
                "a stored value that is not in force must not claim to be",
            )

    def test_an_unparseable_deployment_cap_reads_as_unset(self):
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="twenty dollars"):
            self.assertEqual(
                model_settings.resolve_daily_spend_cap_cents(self.ddb),
                model_settings.DAILY_SPEND_CAP_USD_CENTS_DEFAULT,
            )

    def test_a_ddb_blip_degrades_to_the_deployment_cap(self):
        """The no-stored-cap case: a blip must not fail every submission."""
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="500"):
            with self.assertLogs("src.model_settings", level="WARNING"):
                cents = model_settings.resolve_daily_spend_cap_cents(ExplodingResource())
        self.assertEqual(cents, 500)

    def test_a_blip_over_a_lower_admin_cap_fails_OPEN_to_the_deployment_cap(self):
        """The direction that hurts, asserted as it actually behaves.

        `_spend_row_or_default` degrades to `{}` on a read failure, and `{}`
        means "no admin cap in force" -- so while the settings table is
        unreadable the ceiling is whatever the DEPLOYMENT configured, even
        when the admin has set a LOWER one. With env=500 and an admin cap of
        100, a blip raises the effective ceiling 5x.

        That is a deliberate fail-open, documented on
        `model_settings._spend_row_or_default`: this read sits on the
        reservation path of every submission, and the alternative -- refusing
        every review while DynamoDB is unhappy -- fails a whole deployment
        closed over a transient. It is bounded on both ends (the env cap is
        still enforced, and `MAX_DAILY_SPEND_CAP_USD_CENTS` still bounds what
        an admin could have stored), and it lasts only as long as the blip.
        Pinned here so it is a KNOWN degradation with a test naming it,
        rather than a surprise found in an incident -- and so a future change
        that made the fallback stricter has to come past this assertion.

        The property the code DOES hold is asserted alongside it: the blip
        can never widen the ceiling beyond `DAILY_SPEND_CAP_USD_CENTS`.
        """
        model_settings.set_daily_spend_cap_cents(100, ADMIN, self.ddb)
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="500"):
            healthy = model_settings.resolve_daily_spend_cap_cents(self.ddb)
            with self.assertLogs("src.model_settings", level="WARNING"):
                degraded = model_settings.resolve_daily_spend_cap_cents(ExplodingResource())
        self.assertEqual(healthy, 100, "the admin cap is the ceiling while the read works")
        self.assertEqual(
            degraded,
            500,
            "a blip falls back to the deployment cap -- ABOVE the admin's own, "
            "by 5x here. Deliberate: see this test's docstring.",
        )
        self.assertLessEqual(
            degraded,
            500,
            "whatever else a blip does, it may never exceed DAILY_SPEND_CAP_USD_CENTS",
        )

    def test_a_blip_with_no_deployment_cap_lands_on_the_built_in_default(self):
        """The fail-open's real upper bound with the env var unset: the
        built-in default, not "no cap"."""
        env = dict(os.environ)
        env.pop("DAILY_SPEND_CAP_USD_CENTS", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("src.model_settings", level="WARNING"):
                cents = model_settings.resolve_daily_spend_cap_cents(ExplodingResource())
        self.assertEqual(cents, model_settings.DAILY_SPEND_CAP_USD_CENTS_DEFAULT)

    def test_no_resource_keeps_the_pre_feature_env_behavior(self):
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="3500"):
            self.assertEqual(model_settings.resolve_daily_spend_cap_cents(), 3500)

    def test_reviews_default_is_the_same_object_as_the_settings_default(self):
        """The alias left in reviews.py must not become a second number."""
        self.assertEqual(
            reviews.DAILY_SPEND_CAP_USD_CENTS_DEFAULT,
            model_settings.DAILY_SPEND_CAP_USD_CENTS_DEFAULT,
        )


class TestTheCapWrite(SpendTestBase):
    def test_a_non_admin_cannot_read_or_write_the_cap(self):
        with self.assertRaises(HTTPException) as read_ctx:
            model_settings.get_spend_cap_settings(NON_ADMIN, self.ddb)
        self.assertEqual(read_ctx.exception.status_code, 403)
        with self.assertRaises(HTTPException) as write_ctx:
            model_settings.set_daily_spend_cap_cents(500, NON_ADMIN, self.ddb)
        self.assertEqual(write_ctx.exception.status_code, 403)
        self.assertIsNone(model_settings.stored_daily_spend_cap_cents(self.ddb))

    def test_the_bounds_are_enforced_on_both_ends(self):
        for bad in (
            model_settings.MIN_DAILY_SPEND_CAP_USD_CENTS - 1,
            model_settings.MAX_DAILY_SPEND_CAP_USD_CENTS + 1,
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as ctx:
                    model_settings.set_daily_spend_cap_cents(bad, ADMIN, self.ddb)
                self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(model_settings.stored_daily_spend_cap_cents(self.ddb))

    def test_a_non_numeric_cap_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_daily_spend_cap_cents("lots", ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_a_boolean_is_not_a_number_of_cents(self):
        """`True` is an int in Python. It is not a spend ceiling."""
        with self.assertRaises(HTTPException) as ctx:
            model_settings.set_daily_spend_cap_cents(True, ADMIN, self.ddb)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_every_change_writes_an_audit_row_naming_the_actor(self):
        model_settings.set_daily_spend_cap_cents(1500, ADMIN, self.ddb)
        rows = [r for r in self._audit_rows() if r.get("action") == "spend_cap_change"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["actor"], ADMIN_SUB)
        self.assertEqual(int(rows[0]["after_daily_cap_usd_cents"]), 1500)
        self.assertEqual(rows[0]["after_daily_cap_source"], "admin")

    def test_the_cap_row_survives_rotating_the_key_back_to_the_environment(self):
        """`clear_model_key` DELETES its row. A shared row would take the
        instance's spend ceiling with it."""
        model_settings.set_model_key(FAKE_KEY, ADMIN, self.ddb)
        model_settings.set_daily_spend_cap_cents(1500, ADMIN, self.ddb)
        model_settings.clear_model_key(ADMIN, self.ddb)
        self.assertEqual(model_settings.stored_daily_spend_cap_cents(self.ddb), 1500)

    def test_no_store_refuses_the_write_and_says_where_to_set_it(self):
        env = dict(os.environ)
        env.pop("MODEL_SETTINGS_TABLE", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                model_settings.set_daily_spend_cap_cents(1500, ADMIN, self.ddb)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("DAILY_SPEND_CAP_USD_CENTS", ctx.exception.detail)
            settings = model_settings.get_spend_cap_settings(ADMIN, self.ddb)
        self.assertFalse(settings["cap_store_available"])


# ---------------------------------------------------------------------------
# (2) The ledger surface: the four figures, derived from the REAL producers.
#
# Nothing here hand-writes a `daily_spend` row. The only things that write
# that row in production are reviews.reserve_spend / settle_spend /
# record_preflight_spend, so those are what run; a seeded row would be a
# shape the system reaches only inside this file.
# ---------------------------------------------------------------------------


class TestTheLedgerReportsWhatWasActuallySpent(SpendTestBase):
    CAP = "10000"  # $100/day, room for several reservations

    def test_the_totals_track_each_real_reservation(self):
        ledger = self._ledger(DAILY_SPEND_CAP_USD_CENTS=self.CAP)
        self.assertEqual(ledger["spent_today_usd_cents"], 0)
        self.assertEqual(ledger["daily_cap_usd_cents"], 10000)
        self.assertEqual(ledger["remaining_usd_cents"], 10000)

        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS=self.CAP):
            reviews.reserve_spend("review-1", self.ddb)
        one = self._ledger(DAILY_SPEND_CAP_USD_CENTS=self.CAP)
        per_review = one["worst_case_reservation_usd_cents"]
        self.assertGreater(per_review, 0, "fixture check: a review must cost something")
        self.assertEqual(one["spent_today_usd_cents"], per_review)
        self.assertEqual(one["remaining_usd_cents"], 10000 - per_review)

        # MUTATE the ledger through the real producer: a second reservation
        # has to move both totals. A constant, or a figure read off anything
        # but this day's row, cannot satisfy both of these.
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS=self.CAP):
            reviews.reserve_spend("review-2", self.ddb)
        two = self._ledger(DAILY_SPEND_CAP_USD_CENTS=self.CAP)
        self.assertEqual(two["spent_today_usd_cents"], 2 * per_review)
        self.assertEqual(two["remaining_usd_cents"], 10000 - 2 * per_review)

    def test_settling_cheaper_than_reserved_gives_the_headroom_back(self):
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS=self.CAP):
            reservation_id = reviews.reserve_spend("review-settle", self.ddb)
            reserved = self._reserved_cents()
            self.assertGreater(reserved, 5, "fixture check: needs room to settle under")
            reviews.settle_spend("review-settle", reservation_id, 5, self.ddb)
        after = self._ledger(DAILY_SPEND_CAP_USD_CENTS=self.CAP)
        self.assertEqual(
            after["spent_today_usd_cents"],
            5,
            "once settled, the day counts what the review actually cost",
        )
        self.assertEqual(after["remaining_usd_cents"], 10000 - 5)

    def test_preflight_spend_is_reported_but_is_not_inside_the_cap(self):
        """#491 preflight settles without reserving. The screen must show it
        as billed and must NOT pretend the cap held it back -- claiming a
        ceiling covers a path it does not cover is the failure mode here."""
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS=self.CAP):
            reviews.record_preflight_spend(42, self.ddb)
        ledger = self._ledger(DAILY_SPEND_CAP_USD_CENTS=self.CAP)
        self.assertEqual(ledger["today"]["settled_usd_cents"], 42)
        self.assertEqual(ledger["spent_today_usd_cents"], 0)

    def test_a_settled_review_is_not_counted_twice(self):
        """reserved+settled would double-count every settled review. The
        reported figure must be the counter the cap is checked against."""
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS=self.CAP):
            reservation_id = reviews.reserve_spend("review-dbl", self.ddb)
            reviews.settle_spend("review-dbl", reservation_id, 60, self.ddb)
        ledger = self._ledger(DAILY_SPEND_CAP_USD_CENTS=self.CAP)
        self.assertEqual(ledger["today"]["settled_usd_cents"], 60)
        self.assertEqual(ledger["spent_today_usd_cents"], 60)

    def test_the_reported_cap_is_the_enforced_one_not_the_days_seeded_one(self):
        """The day row's own `daily_cap_usd_cents` records the cap each
        reservation was CHECKED AGAINST, so it is history: after a reservation
        it lags any change made since. An admin who lowers the cap afterwards
        must see the cap that is now in force, or the screen advertises
        headroom the reservation path will refuse."""
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS=self.CAP):
            reviews.reserve_spend("review-cap", self.ddb)
            model_settings.set_daily_spend_cap_cents(3000, ADMIN, self.ddb)
            ledger = admin_dashboard.get_spend_ledger(ADMIN, self.ddb)
        self.assertEqual(ledger["today"]["daily_cap_usd_cents"], 10000)
        self.assertEqual(ledger["daily_cap_usd_cents"], 3000)
        self.assertEqual(ledger["daily_cap_source"], "admin")

    def test_the_admission_verdict_agrees_with_the_reservation_path(self):
        """`next_review_admissible` is the whole question the screen answers.
        It is asserted against what `reserve_spend` ACTUALLY does, in both
        directions -- a verdict that can say "yes" to a refused submission is
        worse than no verdict."""
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS=self.CAP):
            roomy = admin_dashboard.get_spend_ledger(ADMIN, self.ddb)
            self.assertTrue(roomy["next_review_admissible"])
            reviews.reserve_spend("review-ok", self.ddb)  # must not raise

            per_review = roomy["worst_case_reservation_usd_cents"]
            model_settings.set_daily_spend_cap_cents(per_review, ADMIN, self.ddb)
            tight = admin_dashboard.get_spend_ledger(ADMIN, self.ddb)
            self.assertFalse(tight["next_review_admissible"])
            with self.assertRaises(HTTPException) as ctx:
                reviews.reserve_spend("review-refused", self.ddb)
        self.assertEqual(ctx.exception.status_code, 429)


# ---------------------------------------------------------------------------
# (3) Lowering the cap has to REFUSE the next review, not relabel a screen.
# ---------------------------------------------------------------------------


class TestLoweringTheCapRefusesTheNextReview(SpendTestBase):
    def test_a_cap_lowered_below_todays_spend_stops_the_next_submission(self):
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="10000"):
            reviews.reserve_spend("review-before", self.ddb)
            spent = self._reserved_cents()
            self.assertGreater(spent, 1, "fixture check: needs a spend to undercut")

            # Below what today has already committed.
            model_settings.set_daily_spend_cap_cents(spent - 1, ADMIN, self.ddb)

            with self.assertRaises(HTTPException) as ctx:
                reviews.reserve_spend("review-after", self.ddb)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(
            self._reserved_cents(),
            spent,
            "a refused submission must not have reserved anything",
        )

    def test_raising_the_cap_again_lets_the_next_submission_through(self):
        """The other direction, so the refusal above cannot be a cap that
        simply broke."""
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="10000"):
            reviews.reserve_spend("review-before", self.ddb)
            spent = self._reserved_cents()
            model_settings.set_daily_spend_cap_cents(spent - 1, ADMIN, self.ddb)
            with self.assertRaises(HTTPException):
                reviews.reserve_spend("review-refused", self.ddb)

            model_settings.set_daily_spend_cap_cents(
                model_settings.MAX_DAILY_SPEND_CAP_USD_CENTS, ADMIN, self.ddb
            )
            reviews.reserve_spend("review-after", self.ddb)  # must not raise
        self.assertEqual(self._reserved_cents(), 2 * spent)

    def test_a_lowered_cap_also_stops_a_cover_note(self):
        """#499's cover-note path reads the cap too. It must read the SAME
        resolved cap, or an admin who lowers the ceiling closes one spend
        path and leaves another wide open."""
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="10000"):
            reviews.record_preflight_spend(900, self.ddb)
            self.assertFalse(reviews.cover_note_daily_cap_reached(self.ddb))
            model_settings.set_daily_spend_cap_cents(900, ADMIN, self.ddb)
            self.assertTrue(reviews.cover_note_daily_cap_reached(self.ddb))


# ---------------------------------------------------------------------------
# (4) The estimate tracks the model selection (#445's pricing, #653's screen).
# ---------------------------------------------------------------------------


class TestTheEstimateTracksTheModelSelection(SpendTestBase):
    def test_choosing_a_dearer_pair_raises_the_estimate_and_a_cheaper_one_lowers_it(self):
        cheap_expected = self._expected_reservation_cents(CHEAPEST_ID, CHEAPEST_ID)
        dear_expected = self._expected_reservation_cents(DEAREST_ID, DEAREST_ID)
        self.assertLess(
            cheap_expected,
            dear_expected,
            "fixture check: the two ends of the catalogue must actually differ",
        )

        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        cheap = self._ledger(DAILY_SPEND_CAP_USD_CENTS="100000")
        self.assertEqual(cheap["worst_case_reservation_usd_cents"], cheap_expected)

        model_settings.set_model_selection(DEAREST_ID, DEAREST_ID, ADMIN, self.ddb)
        dear = self._ledger(DAILY_SPEND_CAP_USD_CENTS="100000")
        self.assertEqual(dear["worst_case_reservation_usd_cents"], dear_expected)

        self.assertGreater(
            dear["worst_case_reservation_usd_cents"],
            cheap["worst_case_reservation_usd_cents"],
            "the estimate must MOVE with the selection, not merely be present",
        )

    def test_the_two_roles_are_priced_independently(self):
        model_settings.set_model_selection(DEAREST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        mixed = self._ledger(DAILY_SPEND_CAP_USD_CENTS="100000")
        self.assertEqual(
            mixed["worst_case_reservation_usd_cents"],
            self._expected_reservation_cents(DEAREST_ID, CHEAPEST_ID),
        )

    def test_a_dearer_selection_can_turn_an_admissible_day_inadmissible(self):
        """The estimate is not decorative: it feeds the admission verdict and
        the reservation the cap is checked against.

        The day is opened with one CHEAP review first, on purpose: that puts
        the ledger in the state the screen is actually read in (something has
        already run today), and it proves the verdict flips on the SELECTION
        rather than on the day being untouched. The empty-day branch is pinned
        separately by `test_the_days_first_reservation_is_not_exempt_from_the_
        cap` below.
        """
        cheap_cents = self._expected_reservation_cents(CHEAPEST_ID, CHEAPEST_ID)
        cap = 2 * cheap_cents + 1
        self.assertLess(
            cap,
            self._expected_reservation_cents(DEAREST_ID, DEAREST_ID),
            "fixture check: the cap must sit between the two pairs",
        )
        with _no_env_overrides():
            model_settings.set_daily_spend_cap_cents(cap, ADMIN, self.ddb)
            model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
            reviews.reserve_spend("review-opens-the-day", self.ddb)
            self.assertEqual(self._reserved_cents(), cheap_cents)

            self.assertTrue(
                admin_dashboard.get_spend_ledger(ADMIN, self.ddb)["next_review_admissible"]
            )

            model_settings.set_model_selection(DEAREST_ID, DEAREST_ID, ADMIN, self.ddb)
            self.assertFalse(
                admin_dashboard.get_spend_ledger(ADMIN, self.ddb)["next_review_admissible"]
            )
            with self.assertRaises(HTTPException) as ctx:
                reviews.reserve_spend("review-too-dear", self.ddb)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_the_days_first_reservation_is_not_exempt_from_the_cap(self):
        """The hole this issue makes reachable, and closes.

        `reserve_spend`'s ConditionExpression is
        `attribute_not_exists(reserved_usd_cents) OR reserved_usd_cents <=
        budget`, so on a fresh day the FIRST reservation used to be admitted
        unconditionally -- the arithmetic budget (`cap - amount`) goes negative
        and nothing is compared against it. That was unreachable while the cap
        was a deploy-time constant chosen to be many reviews wide. It becomes
        reachable the moment an admin can type a cap smaller than one review
        into a form, which is exactly what #653 adds, so `reserve_spend` now
        refuses a reservation larger than the whole cap outright.

        Asserted through the REAL reservation path, not through the verdict:
        the verdict is only worth anything if it agrees with what the server
        does, so the server is what is driven and the verdict is checked
        against it.
        """
        dear_cents = self._expected_reservation_cents(DEAREST_ID, DEAREST_ID)
        with _no_env_overrides():
            model_settings.set_model_selection(DEAREST_ID, DEAREST_ID, ADMIN, self.ddb)
            model_settings.set_daily_spend_cap_cents(dear_cents - 1, ADMIN, self.ddb)

            ledger = admin_dashboard.get_spend_ledger(ADMIN, self.ddb)
            self.assertEqual(ledger["remaining_usd_cents"], dear_cents - 1)
            self.assertFalse(
                ledger["next_review_admissible"],
                "a cap below one review's reservation admits nothing, not even the first",
            )
            with self.assertRaises(HTTPException) as ctx:
                reviews.reserve_spend("review-first-of-day", self.ddb)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(
            self._reserved_cents(), 0, "a refused submission must not have reserved anything"
        )

        # And one cent more of headroom lets exactly that same review through,
        # so the refusal above is the CAP biting rather than the path being
        # broken.
        with _no_env_overrides():
            model_settings.set_daily_spend_cap_cents(dear_cents, ADMIN, self.ddb)
            self.assertTrue(
                admin_dashboard.get_spend_ledger(ADMIN, self.ddb)["next_review_admissible"]
            )
            reviews.reserve_spend("review-now-affordable", self.ddb)  # must not raise
        self.assertEqual(self._reserved_cents(), dear_cents)

    def test_a_settled_only_day_still_has_its_whole_cap_to_spend(self):
        """Preflight/cover-note spend (#491/#499) writes `settled_usd_cents`
        and never `reserved_usd_cents`, so it is billed but sits OUTSIDE the
        counter `reserve_spend`'s condition compares against.

        The screen must report that honestly rather than folding the two
        counters together: the day has spent 50c, and the reservation gate
        still has the whole cap available to it. Both halves are asserted,
        because a view that quietly summed them would show a smaller
        remainder than the gate actually enforces -- and then refuse nothing
        it had promised to refuse.
        """
        with _no_env_overrides(DAILY_SPEND_CAP_USD_CENTS="10000"):
            reviews.record_preflight_spend(50, self.ddb)
            ledger = admin_dashboard.get_spend_ledger(ADMIN, self.ddb)
            self.assertEqual(ledger["today"]["settled_usd_cents"], 50)
            self.assertEqual(ledger["spent_today_usd_cents"], 0)
            self.assertEqual(ledger["remaining_usd_cents"], 10000)
            self.assertTrue(ledger["next_review_admissible"])
            reviews.reserve_spend("review-after-preflight", self.ddb)  # must not raise


# ---------------------------------------------------------------------------
# (5) The HTTP surface.
# ---------------------------------------------------------------------------


class TestHttpSurface(SpendTestBase):
    def setUp(self):
        super().setUp()
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, user_row):
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = lambda: user_row

    def test_a_non_admin_cannot_set_the_cap(self):
        self._as(NON_ADMIN)
        response = self.client.post(
            "/api/admin/spend-cap", json={"daily_cap_usd_cents": 100}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(model_settings.stored_daily_spend_cap_cents(self.ddb))

    def test_a_non_admin_cannot_read_the_spend_ledger(self):
        self._as(NON_ADMIN)
        self.assertEqual(self.client.get("/api/admin/spend").status_code, 403)

    def test_setting_the_cap_over_http_changes_what_the_ledger_reports(self):
        self._as(ADMIN)
        post = self.client.post("/api/admin/spend-cap", json={"daily_cap_usd_cents": 4321})
        self.assertEqual(post.status_code, 200, post.text)
        self.assertEqual(post.json()["daily_cap_usd_cents"], 4321)

        ledger = self.client.get("/api/admin/spend")
        self.assertEqual(ledger.status_code, 200, ledger.text)
        body = ledger.json()
        self.assertEqual(body["daily_cap_usd_cents"], 4321)
        self.assertEqual(body["daily_cap_source"], "admin")
        self.assertEqual(body["remaining_usd_cents"], 4321)
        self.assertTrue(body["cap_setting"]["cap_store_available"])

    def test_an_out_of_range_cap_400s_over_http(self):
        self._as(ADMIN)
        response = self.client.post(
            "/api/admin/spend-cap",
            json={"daily_cap_usd_cents": model_settings.MAX_DAILY_SPEND_CAP_USD_CENTS + 1},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_a_null_cap_clears_the_override_over_http(self):
        self._as(ADMIN)
        self.client.post("/api/admin/spend-cap", json={"daily_cap_usd_cents": 4321})
        response = self.client.post("/api/admin/spend-cap", json={"daily_cap_usd_cents": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["stored_daily_cap_usd_cents"])
        self.assertNotEqual(response.json()["daily_cap_source"], "admin")

    def test_the_spend_routes_never_echo_the_api_key(self):
        """The spend surface reads the settings TABLE now. It must still be
        impossible to read the key back through it."""
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": FAKE_KEY})
        ledger = self.client.get("/api/admin/spend")
        post = self.client.post("/api/admin/spend-cap", json={"daily_cap_usd_cents": 4321})
        self.assertNotIn(FAKE_KEY, ledger.text)
        self.assertNotIn(FAKE_KEY, post.text)


# ---------------------------------------------------------------------------
# (6) Scope item 2: the estimate reaches the person about to submit a review,
#     and nothing else does.
# ---------------------------------------------------------------------------


class TestTheReviewerFacingEstimate(SpendTestBase):
    """`GET /api/review-cost-estimate` -- #653 Scope item 2, "shown where a
    reviewer can see it before submitting".

    The Settings tab that carries the rest of #653 is admin-gated, and
    `AdminSettings.tsx` renders nothing at all on the 403 a non-admin gets
    from `GET /api/admin/spend`. So the two claims worth driving here are:
    a NON-ADMIN can read the estimate at all, and reading it hands over the
    estimate ONLY -- never the cap, the day's spend, or the remainder, which
    are this deployment's operating budget.
    """

    def setUp(self):
        super().setUp()
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def _as(self, user_row):
        backend_main.app.dependency_overrides[backend_main.get_active_user_row] = lambda: user_row

    def test_a_non_admin_can_read_the_estimate(self):
        self._as(NON_ADMIN)
        response = self.client.get("/api/review-cost-estimate")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["worst_case_reservation_usd_cents"],
            reviews.compute_worst_case_reservation_usd_cents(self.ddb),
        )

    def test_it_carries_the_estimate_and_nothing_else(self):
        """The budget must not ride along. Asserted as an EXACT key set, not
        as "the cap is absent": a figure added to the ledger tomorrow has to
        fail this test rather than quietly reach a reviewer."""
        self._as(ADMIN)
        self.client.post("/api/admin/spend-cap", json={"daily_cap_usd_cents": 4321})
        self._as(NON_ADMIN)
        body = self.client.get("/api/review-cost-estimate").json()
        self.assertEqual(set(body), set(reviews.REVIEW_COST_ESTIMATE_FIELDS))
        for budget_field in (
            "daily_cap_usd_cents",
            "spent_today_usd_cents",
            "remaining_usd_cents",
            "cap_setting",
            "today",
            "days",
        ):
            self.assertNotIn(budget_field, body)
        self.assertNotIn("4321", self.client.get("/api/review-cost-estimate").text)

    def test_the_reviewer_sees_the_same_number_the_admin_ledger_reports(self):
        """One source. A second formula behind the reviewer's copy would be
        free to drift from the one that decides admission."""
        model_settings.set_model_selection(DEAREST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        self._as(ADMIN)
        ledger = self.client.get("/api/admin/spend").json()
        self._as(NON_ADMIN)
        estimate = self.client.get("/api/review-cost-estimate").json()
        self.assertEqual(
            estimate["worst_case_reservation_usd_cents"],
            ledger["worst_case_reservation_usd_cents"],
        )
        self.assertEqual(
            estimate["worst_case_reservation_usd_cents"],
            self._expected_reservation_cents(DEAREST_ID, CHEAPEST_ID),
        )

    def test_the_reviewers_estimate_tracks_the_model_selection(self):
        """#653's own verification line -- "change the models and the number
        must change" -- asserted on the surface a reviewer actually reads,
        not only on the admin one."""
        self._as(ADMIN)
        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        self._as(NON_ADMIN)
        cheap = self.client.get("/api/review-cost-estimate").json()

        self._as(ADMIN)
        model_settings.set_model_selection(DEAREST_ID, DEAREST_ID, ADMIN, self.ddb)
        self._as(NON_ADMIN)
        dear = self.client.get("/api/review-cost-estimate").json()

        self.assertEqual(
            cheap["worst_case_reservation_usd_cents"],
            self._expected_reservation_cents(CHEAPEST_ID, CHEAPEST_ID),
        )
        self.assertEqual(
            dear["worst_case_reservation_usd_cents"],
            self._expected_reservation_cents(DEAREST_ID, DEAREST_ID),
        )
        self.assertGreater(
            dear["worst_case_reservation_usd_cents"],
            cheap["worst_case_reservation_usd_cents"],
            "the reviewer's estimate must MOVE with the selection",
        )

    def test_the_expected_cost_is_reported_beside_the_reservation_and_is_lower(self):
        """#653's Why is the GAP between the two figures: a $0.2742 review
        against a $1.10 worst case, measured 2026-09-01, neither discoverable
        from the UI. Reporting only the reservation would tell a reviewer a
        review costs several times what it does.

        Priced independently of `backend/src/reviews.py` -- from the policy
        artifact's own per-review token basis and the selected models' rates --
        so this cannot pass against a mirror of the implementation.
        """
        self._as(ADMIN)
        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        self._as(NON_ADMIN)
        body = self.client.get("/api/review-cost-estimate").json()

        policy = _policy()
        rates = self._rates(CHEAPEST_ID)
        expected = 0.0
        for role in ("primary", "critic"):
            block = policy["models"][role]
            expected += (
                block["approx_tokens_per_review_input"]
                * rates["cost_per_million_input_usd"]
                / 1_000_000
            )
            expected += (
                block["approx_tokens_per_review_output"]
                * rates["cost_per_million_output_usd"]
                / 1_000_000
            )
        self.assertEqual(body["estimated_usd_cents"], int(round(expected * 100)))
        self.assertLess(
            body["estimated_usd_cents"],
            body["worst_case_reservation_usd_cents"],
            "the expected cost must be under the worst case, or one of them is wrong",
        )

    def test_the_expected_cost_tracks_the_model_selection_too(self):
        """Both figures move with the selection, not just the reservation."""
        self._as(ADMIN)
        model_settings.set_model_selection(CHEAPEST_ID, CHEAPEST_ID, ADMIN, self.ddb)
        self._as(NON_ADMIN)
        cheap = self.client.get("/api/review-cost-estimate").json()["estimated_usd_cents"]

        self._as(ADMIN)
        model_settings.set_model_selection(DEAREST_ID, DEAREST_ID, ADMIN, self.ddb)
        self._as(NON_ADMIN)
        dear = self.client.get("/api/review-cost-estimate").json()["estimated_usd_cents"]

        self.assertGreater(dear, cheap, "the expected cost must MOVE with the selection")

    def test_a_provider_with_no_per_review_basis_reports_no_expected_cost(self):
        """The Bedrock path prices from the module constants and carries no
        measured per-review basis, so there is no honest expected figure. It
        must report None and leave the reservation standing, rather than
        inventing a number the UI would then render as fact."""
        prior = os.environ.get("MODEL_PROVIDER")
        os.environ["MODEL_PROVIDER"] = "bedrock"
        try:
            self._as(NON_ADMIN)
            body = self.client.get("/api/review-cost-estimate").json()
        finally:
            if prior is None:
                os.environ.pop("MODEL_PROVIDER", None)
            else:
                os.environ["MODEL_PROVIDER"] = prior
        self.assertIsNone(body["estimated_usd_cents"])
        self.assertGreater(body["worst_case_reservation_usd_cents"], 0)

    def test_it_never_echoes_the_api_key(self):
        self._as(ADMIN)
        self.client.post("/api/admin/model-key", json={"api_key": FAKE_KEY})
        self._as(NON_ADMIN)
        self.assertNotIn(FAKE_KEY, self.client.get("/api/review-cost-estimate").text)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestTheCapResolution,
        TestTheCapWrite,
        TestTheLedgerReportsWhatWasActuallySpent,
        TestLoweringTheCapRefusesTheNextReview,
        TestTheEstimateTracksTheModelSelection,
        TestHttpSurface,
        TestTheReviewerFacingEstimate,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
