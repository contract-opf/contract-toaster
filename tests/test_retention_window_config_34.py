#!/usr/bin/env python3
"""
Slice test for issue #34 (v1 scope, maintainer-confirmed 2026-07-10):
configurable retention window + a "forever / indefinite preservation"
option, exposed as a setting.

Exercises the real `retention_settings` global-row config surface against
`moto`-mocked DynamoDB/S3 (no live AWS, no network) -- same convention as
tests/test_playbook_version_audit_9.py:

  1. `backend/src/reviews.py::_current_retention_window_days` reads the
     ADMIN-CONFIGURED window from the `retention_settings` row (not a
     hard-coded 90), and the purge worker (`backend/src/retention.py`'s
     `run_purge_sweep_now`, mirroring the production
     `infra/lambda/purge_worker/handler.py::run_purge_sweep`) honors each
     review's own snapshot of that configured value.
  2. Setting the window to the `"forever"` sentinel disables purge for
     records snapshotted at it -- they are never marked purge-eligible, at
     any age -- in BOTH the backend/src/retention.py mirror and the actual
     production `infra/lambda/purge_worker/handler.py` entry point.
  3. The retention-settings surface's rendered option labels contain no
     "Exos"/"EXOS" branding (release de-branding directive -- "your"
     voicing only).

This test MUST FAIL on the pre-fix tree (no `forever` sentinel support --
`request_retention_change` rejects it as out-of-range, `_is_past_retention`
treats any window as a numeric day-count and TypeErrors on a string, and
`get_retention_settings` returns no `window_options`) and PASS after the
fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
PURGE_WORKER_DIR = REPO_ROOT / "infra" / "lambda" / "purge_worker"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(PURGE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(PURGE_WORKER_DIR))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.retention as retention_module  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

# The production purge-worker Lambda -- a separate deployable, so it reads
# the same env vars set above but is imported independently of backend/src
# (see infra/lambda/purge_worker/handler.py's own module docstring).
import handler as purge_handler_module  # noqa: E402

DAY = 86400
FOREVER = retention_module.RETENTION_WINDOW_FOREVER

ADMIN = {"cognito_sub": "admin-1", "email": "admin-1@example.com", "is_admin": True}


class RetentionWindowConfigTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["RETENTION_SETTINGS_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # PK/SK match exactly what retention.py::_write_audit_entry writes
        # (`partition`, `timestamp`), independent of the production
        # CDK-defined audit table's own schema -- this test only needs the
        # write path to succeed, not to validate the CDK schema.
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
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.s3.create_bucket(Bucket=os.environ["OUTPUTS_BUCKET"])

        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self):
        self._mock_aws.stop()

    def _seed_review(self, review_id: str, window_days, age_days: float,
                      status_: str = "DONE") -> None:
        now = retention_module.now_epoch()
        self.reviews_table.put_item(
            Item={
                "review_id": review_id,
                "status": status_,
                "created_at": str(now - age_days * DAY),
                "retention_window_at_creation": window_days,
                "legal_hold": False,
                "verdict_summary": "some summary",
                "issue_rationale_text": "some rationale",
            }
        )


# ---------------------------------------------------------------------------
# (1) The configured window (not a hard-coded 90) flows from the admin
#     setting through review-creation snapshotting to the purge worker.
# ---------------------------------------------------------------------------

class TestPurgeWorkerHonorsConfiguredWindow(RetentionWindowConfigTestBase):
    def test_new_review_snapshots_the_admin_configured_window(self):
        # Raise the default 90-day window to 200 days -- a forward-looking
        # change, applies single-admin, immediately (#13/#61 dual control
        # only gates reductions).
        result = retention_module.request_retention_change(200, ADMIN, self.ddb)
        self.assertEqual(result["status"], "APPLIED")

        # A review created "now" must snapshot the CONFIGURED 200-day
        # window, not the hard-coded 90-day constant.
        snapshotted = reviews_module._current_retention_window_days(self.ddb)
        self.assertEqual(snapshotted, 200)

    def test_worker_purges_by_configured_window_not_hard_coded_90(self):
        retention_module.request_retention_change(200, ADMIN, self.ddb)
        configured_window = reviews_module._current_retention_window_days(self.ddb)
        self.assertEqual(configured_window, 200)

        # 150 days old: past the OLD hard-coded 90-day default, but NOT
        # past the newly configured 200-day window. If the worker were
        # still hard-coded to 90, this review would be wrongly purged.
        self._seed_review("r-not-yet-eligible", configured_window, age_days=150)
        # 250 days old: past the configured 200-day window either way.
        self._seed_review("r-eligible", configured_window, age_days=250)

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)
        self.assertIn("r-eligible", summary["deleted_reviews"])
        self.assertNotIn("r-not-yet-eligible", summary["deleted_reviews"])
        self.assertIn("r-not-yet-eligible", summary["skipped_not_yet_eligible"])

    def test_production_purge_worker_lambda_honors_configured_window(self):
        """Same assertion against the actual production entry point,
        infra/lambda/purge_worker/handler.py::run_purge_sweep -- not just
        the backend/src/retention.py mirror."""
        retention_module.request_retention_change(200, ADMIN, self.ddb)
        configured_window = reviews_module._current_retention_window_days(self.ddb)

        self._seed_review("r-not-yet-eligible", configured_window, age_days=150)
        self._seed_review("r-eligible", configured_window, age_days=250)

        summary = purge_handler_module.run_purge_sweep()
        self.assertIn("r-eligible", summary["deleted_reviews"])
        self.assertNotIn("r-not-yet-eligible", summary["deleted_reviews"])
        self.assertIn("r-not-yet-eligible", summary["skipped_not_yet_eligible"])


# ---------------------------------------------------------------------------
# (2) "forever" disables purge, at any age.
# ---------------------------------------------------------------------------

class TestForeverDisablesPurge(RetentionWindowConfigTestBase):
    def test_forever_is_a_valid_forward_looking_setting(self):
        result = retention_module.request_retention_change(FOREVER, ADMIN, self.ddb)
        self.assertEqual(result["status"], "APPLIED")
        self.assertTrue(result["applied_immediately"])

        settings = retention_module.get_retention_settings(ADMIN, self.ddb)
        self.assertEqual(settings["retention_window_days"], FOREVER)

        snapshotted = reviews_module._current_retention_window_days(self.ddb)
        self.assertEqual(snapshotted, FOREVER)

    def test_forever_snapshotted_review_never_purge_eligible_even_when_ancient(self):
        retention_module.request_retention_change(FOREVER, ADMIN, self.ddb)
        configured_window = reviews_module._current_retention_window_days(self.ddb)
        self.assertEqual(configured_window, FOREVER)

        # Absurdly old (~27 years) -- would be purge-eligible under any
        # bounded window, including the 1095-day (3-year) maximum.
        self._seed_review("r-ancient-forever", configured_window, age_days=10_000)

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)
        self.assertNotIn("r-ancient-forever", summary["deleted_reviews"])
        self.assertIn("r-ancient-forever", summary["skipped_not_yet_eligible"])

    def test_production_purge_worker_lambda_never_purges_forever(self):
        retention_module.request_retention_change(FOREVER, ADMIN, self.ddb)
        configured_window = reviews_module._current_retention_window_days(self.ddb)

        self._seed_review("r-ancient-forever", configured_window, age_days=10_000)

        summary = purge_handler_module.run_purge_sweep()
        self.assertNotIn("r-ancient-forever", summary["deleted_reviews"])
        self.assertIn("r-ancient-forever", summary["skipped_not_yet_eligible"])

    def test_reverting_forever_to_a_finite_window_is_a_retroactive_reduction(self):
        """Going from `forever` back to any finite window is the maximum
        possible reduction and must be dual-controlled, same as any other
        retroactive reduction (#13/#61) -- `forever` must not be a loophole
        around dual control. Uses the second-admin-confirmed path (rather
        than the unconfirmed/pending path) as the observable signal: a
        forward-looking change always applies with
        `applied_immediately=True` regardless of any confirmation supplied,
        while a retroactive reduction applies (once confirmed) with
        `applied_immediately=False` -- so `False` here proves forever ->
        1095 was classified as a reduction, not silently treated as
        forward-looking."""
        retention_module.request_retention_change(FOREVER, ADMIN, self.ddb)
        result = retention_module.request_retention_change(
            1095, ADMIN, self.ddb, second_admin_confirmation={"actor": "admin-2"},
        )
        self.assertEqual(result["status"], "APPLIED")
        self.assertFalse(result["applied_immediately"])
        settings = retention_module.get_retention_settings(ADMIN, self.ddb)
        self.assertEqual(settings["retention_window_days"], 1095)

    def test_out_of_range_still_rejected_forever_is_not_a_bypass(self):
        with self.assertRaises(Exception) as ctx:
            retention_module.request_retention_change(5000, ADMIN, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)
        with self.assertRaises(Exception) as ctx:
            retention_module.request_retention_change("literally-forever", ADMIN, self.ddb)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)


# ---------------------------------------------------------------------------
# (3) De-brand: rendered option labels contain no "Exos"/"EXOS".
# ---------------------------------------------------------------------------

class TestRetentionSettingsSurfaceDeBrand(RetentionWindowConfigTestBase):
    def test_no_row_yet_default_settings_include_forever_option_no_branding(self):
        settings = retention_module.get_retention_settings(ADMIN, self.ddb)
        self.assertIn("window_options", settings)
        options = settings["window_options"]
        self.assertTrue(len(options) >= 1)

        values = [opt["value"] for opt in options]
        self.assertIn(FOREVER, values, "forever/indefinite MUST be one of the choices")

        for opt in options:
            label = opt["label"]
            self.assertNotIn("exos", label.lower(), f"branded label: {label!r}")

    def test_settings_after_a_real_change_still_carry_unbranded_options(self):
        retention_module.request_retention_change(FOREVER, ADMIN, self.ddb)
        settings = retention_module.get_retention_settings(ADMIN, self.ddb)

        self.assertEqual(settings["default_retention_window_days"], 90)
        for opt in settings["window_options"]:
            self.assertNotIn("exos", opt["label"].lower())
            self.assertNotIn("EXOS", opt["label"])


# ---------------------------------------------------------------------------
# (4) The real HTTP surface -- POST/GET /api/admin/retention -- accepts
#     "forever" end-to-end, through backend/src/main.py's request parsing.
#     Regression guard: `int(body["retention_window_days"])` on a
#     `"forever"` string body raises ValueError/400 before the request ever
#     reaches request_retention_change's own (correct) validation.
# ---------------------------------------------------------------------------

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"


class TestRetentionEndpointAcceptsForever(RetentionWindowConfigTestBase):
    def setUp(self):
        super().setUp()
        self.ddb.create_table(
            TableName=os.environ["USERS_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        users_table.put_item(Item={
            "cognito_sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com",
            "status": "active", "is_admin": True,
        })

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "token_use": "access"}
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def test_post_retention_accepts_forever_string_body(self):
        response = self.client.post(
            "/api/admin/retention",
            json={"retention_window_days": "forever", "second_admin_confirmation": None},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "APPLIED")
        self.assertTrue(body["applied_immediately"])

    def test_get_retention_reflects_forever_and_unbranded_options(self):
        self.client.post(
            "/api/admin/retention",
            json={"retention_window_days": "forever", "second_admin_confirmation": None},
        )
        response = self.client.get("/api/admin/retention")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["retention_window_days"], "forever")
        values = [opt["value"] for opt in body["window_options"]]
        self.assertIn("forever", values)
        for opt in body["window_options"]:
            self.assertNotIn("exos", opt["label"].lower())

    def test_post_retention_still_accepts_ordinary_int_body(self):
        """Regression guard for the int() parsing change itself -- ordinary
        numeric bodies (the pre-#34 behavior) must keep working."""
        response = self.client.post(
            "/api/admin/retention",
            json={"retention_window_days": 200, "second_admin_confirmation": None},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "APPLIED")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestPurgeWorkerHonorsConfiguredWindow))
    suite.addTests(loader.loadTestsFromTestCase(TestForeverDisablesPurge))
    suite.addTests(loader.loadTestsFromTestCase(TestRetentionSettingsSurfaceDeBrand))
    suite.addTests(loader.loadTestsFromTestCase(TestRetentionEndpointAcceptsForever))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
