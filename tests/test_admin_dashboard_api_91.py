#!/usr/bin/env python3
"""
Executable tests for issue #252: the admin dashboard READ-API — the backend
slice #91's UI binds to.

  GET /api/admin/spend          spend ledger + reconcile view
  GET /api/admin/health         pipeline health summary
  GET /api/admin/manual-review  manual-review queue, filterable (#37)
  GET /api/admin/releases       release activity + cost-outlier flag

Drives the REAL, shipped application object (`src.main.app`) through a
FastAPI `TestClient`, with DynamoDB provided by **moto** (`mock_aws`) rather
than an in-memory fake — same convention as
tests/test_playbook_create_485.py. `get_dynamodb_resource` and
`get_current_user` are dependency-overridden; no network, no AWS.

## What is asserted here

  1. ADMIN-ONLY, AND THE ROLE COMES FROM THE `users` ROW. A non-admin caller
     gets 403 from all four routes and the refusal body carries none of the
     data the route would have served. The SAME caller, with a JWT-shaped
     claim set that asserts `is_admin`/an admin group, is still refused —
     and is then admitted the instant their DynamoDB `users` row flips.
     That pair is what proves the gate reads the row and not the token
     (ARCHITECTURE.md -> "Group-naming misnomer").
  2. THE FOUR SHAPES. Each route returns the documented shape for an admin,
     computed from seeded rows rather than asserted structurally.
  3. NOTHING LEAKS. A manual-review row is seeded with every
     sensitive-looking field a real reviews row can carry (document
     substance, the submitter's own guidance, S3 keys, an execution ARN, key
     material) and NONE of it appears anywhere in any of the four raw
     response bodies. Asserted against the RAW response text, and the queue
     row shape is asserted to be EXACTLY the documented key set.
  4. BOUNDED, AND `?days=` IS A CALENDAR WINDOW. `?days=`, `?limit=` and
     `?stale_after_seconds=` are all clamped; a hostile `limit=100000` cannot
     turn a tile into a full-table dump. Each limit fixture seeds MORE rows
     than the cap it is testing, because `len(rows) <= MAX` over a fixture
     smaller than MAX is true of an implementation with no clamp at all.
     The cost-outlier block's ledger read is bounded too — a row cap, an
     append-only ledger being exactly the table a dashboard must not scan
     in full — and it reports the cap and whether the sample was cut. A
     daily-spend row exists only for a day that HAD spend, so `days=30` is
     asserted to return the last 30 CALENDAR days and not the 30 most
     recent rows — an idle deployment must not answer a 30-day question
     with a quarter's worth of spend.
  5. FILTERS ARE VALIDATED, NOT IGNORED. An unrecognised `status_filter` /
     `triage` value is a 400 — a silently-ignored filter would return the
     whole queue to an operator who believes they filtered it. `counts`
     stays computed over the UNFILTERED queue so the tile figure does not
     move when the list below it is filtered.
  6. THE PRODUCTION DECIMAL HAZARD (issue #440 / commit df60971): boto3's
     resource API hands back `decimal.Decimal` for every stored number,
     which `JSONResponse` cannot encode — that 500'd GET /api/users on the
     live deployment while the suite stayed green, because in-memory fakes
     store plain ints. moto stores real Decimals, and the spend counters and
     ledger token counts here are seeded as numbers deliberately.
  7. THE REAL RELEASE-AUDIT ROW SHAPE. The activation/rollback rows are
     built by calling the WRITERS (`playbook_versions.activate_playbook_
     version` / `rollback_playbook_version`), never by hand-copying their
     believed output, and a real non-release audit row (written by
     `users.update_user`) must NOT appear in the release feed.
  8. THE OPTIONAL LEDGER DEGRADES, NEVER 500s. With no
     MODEL_INVOCATIONS_TABLE (every mock-pipeline deployment) the
     cost-outlier block reports `available: false`; with a seeded ledger a
     50x review is flagged and an unmeasured one is not silently priced at
     zero. EVERY ledgered `pass_name` is priced, not just primary+critic:
     a review whose runaway tokens sit on the `requote`/`floor`/`cover_note`
     passes is still flagged, and the critic pass is still billed at critic
     rates. Pricing the sample resolves the provider rates ONCE for the
     whole request (asserted with a call-count spy on
     `reviews._active_provider_rates`, which re-reads the model-policy file
     and the admin model-selection row on every call).
  9. DE-BRANDED. No 'Exos'/'EXOS' appears in any of the four payloads.

MUST FAIL on the pre-implementation tree: none of the four routes are
registered (404) and `src.admin_dashboard` does not exist.

Run standalone: `python3 tests/test_admin_dashboard_api_91.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import decimal
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-dash252-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-dash252-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-dash252-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-dash252-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-dash252-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-dash252-test"
)
os.environ.setdefault("DAILY_SPEND_CAP_USD_CENTS", "2000")
os.environ.setdefault("PIPELINE_MAX_CONCURRENCY", "5")
# The model-invocation ledger (#414) is OPTIONAL and is deliberately left
# UNSET here: that is the mock-pipeline deployment shape, and the
# cost-outlier block must degrade rather than 500. The one test that needs a
# ledger sets it for its own duration.
os.environ.pop("MODEL_INVOCATIONS_TABLE", None)
LEDGER_TABLE = "contract-toaster-model-invocations-dash252-test"

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.admin_dashboard as admin_dashboard  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.playbook_versions as playbook_versions  # noqa: E402
from src.users import update_user  # noqa: E402

SPEND_ROUTE = "/api/admin/spend"
HEALTH_ROUTE = "/api/admin/health"
MANUAL_REVIEW_ROUTE = "/api/admin/manual-review"
RELEASES_ROUTE = "/api/admin/releases"
ALL_ROUTES = (SPEND_ROUTE, HEALTH_ROUTE, MANUAL_REVIEW_ROUTE, RELEASES_ROUTE)

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"
TARGET_SUB = "reviewer-2"

PLAYBOOK_ID = "synthetic-nda-sample"

HOUR = 3600
DAY = 24 * HOUR


# ---------------------------------------------------------------------------
# Sensitive material planted on a manual-review row.
#
# Every one of these is a field a REAL reviews row can carry (see
# reviews._create_review_row, the persist stage's writes, and
# get_review_detail's projection) plus the two things a log viewer would have
# exposed and a dashboard must not: an exception message and a stack trace.
# Distinctive marker strings, so presence anywhere in a response body is
# unambiguous. Synthetic throughout -- no real party names, per the repo's
# fixture rule.
# ---------------------------------------------------------------------------
SENSITIVE_FIELDS: dict[str, Any] = {
    "summary": "SENTINEL-SUMMARY-indemnity is uncapped in clause 9",
    "findings": [{"rationale_text": "SENTINEL-RATIONALE-the cap was struck"}],
    "toaster_guidance": "SENTINEL-GUIDANCE-be lenient on the payment terms",
    "output_s3_key": "outputs/sub-owner/SENTINEL-S3-KEY/out.docx",
    "upload_s3_key": "uploads/sub-owner/SENTINEL-UPLOAD-KEY/in.docx",
    "owner_sub": "SENTINEL-OWNER-SUB",
    "exception_message": "SENTINEL-EXC-provider returned HTTP 402",
    "stack_trace": 'SENTINEL-TRACE-File "/app/backend/src/pipeline_runner.py", line 1',
    "model_api_key": "SENTINEL-KEY-MATERIAL",
    "execution_arn": "arn:aws:states:us-east-1:000000000000:execution:SENTINEL-EXEC",
    "cover_note_draft": "SENTINEL-COVER-NOTE-please find our comments attached",
}

# The EXACT key set one manual-review queue entry may carry. Nine projected
# row fields plus the three derived ones. A new key appearing here is a
# deliberate disclosure decision, and this assertion is what forces that
# decision to be made on purpose.
EXPECTED_QUEUE_KEYS = {
    "review_id",
    "created_at",
    "updated_at",
    "failed_at",
    "failing_stage",
    "status",
    "playbook_id",
    "legal_triage_status",
    "attorney_disposition",
    "reason",
    "waiting_seconds",
    "sla_breached",
}


def _put_user(table: Any, sub: str, is_admin: bool) -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": "active",
            "is_admin": is_admin,
        }
    )


class AdminDashboardTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["USERS_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["DAILY_SPEND_TABLE"],
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
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
            TableName=os.environ["PLAYBOOK_VERSIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "playbook_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "playbook_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.spend_table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        self.audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])

        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)
        _put_user(self.users_table, TARGET_SUB, is_admin=False)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        self._authenticate_as(ADMIN_SUB)

    def tearDown(self) -> None:
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()
        os.environ.pop("MODEL_INVOCATIONS_TABLE", None)

    # -- helpers ------------------------------------------------------------

    def _authenticate_as(self, sub: str, extra_claims: dict[str, Any] | None = None) -> None:
        claims = {"sub": sub, "email": f"{sub}@example.com", "token_use": "access"}
        claims.update(extra_claims or {})
        backend_main.app.dependency_overrides[backend_main.get_current_user] = lambda: claims

    def _put_review(self, review_id: str, **fields: Any) -> None:
        row: dict[str, Any] = {"review_id": review_id, "playbook_id": PLAYBOOK_ID}
        row.update(fields)
        self.reviews_table.put_item(Item={k: v for k, v in row.items() if v is not None})

    def _seed_ledger_table(self) -> Any:
        """Create and select the OPTIONAL model-invocation ledger (#414)."""
        self.ddb.create_table(
            TableName=LEDGER_TABLE,
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
        os.environ["MODEL_INVOCATIONS_TABLE"] = LEDGER_TABLE
        return self.ddb.Table(LEDGER_TABLE)


# ---------------------------------------------------------------------------
# 1. Admin gate — and where the role comes from
# ---------------------------------------------------------------------------


class AdminGateTests(AdminDashboardTestBase):
    def test_non_admin_is_refused_by_every_route(self) -> None:
        self._authenticate_as(NON_ADMIN_SUB)
        for route in ALL_ROUTES:
            with self.subTest(route=route):
                resp = self.client.get(route)
                self.assertEqual(resp.status_code, 403, resp.text)

    def test_refusal_body_serves_none_of_the_data(self) -> None:
        """A 403 must not be a filtered 200 in disguise: none of the payload
        keys the route would have served may appear in the refusal."""
        self.spend_table.put_item(
            Item={"spend_date": time.strftime("%Y-%m-%d", time.gmtime()), "reserved_usd_cents": 777}
        )
        self._put_review("rev-manual", status="MANUAL_REVIEW_REQUIRED", created_at="1000")
        self._authenticate_as(NON_ADMIN_SUB)
        for route, forbidden_key in (
            (SPEND_ROUTE, "reserved_usd_cents"),
            (HEALTH_ROUTE, "status_counts"),
            (MANUAL_REVIEW_ROUTE, "counts"),
            (RELEASES_ROUTE, "releases"),
        ):
            with self.subTest(route=route):
                resp = self.client.get(route)
                self.assertEqual(resp.status_code, 403)
                self.assertNotIn(forbidden_key, resp.text)
                self.assertNotIn("777", resp.text)
                self.assertNotIn("rev-manual", resp.text)

    def test_role_is_the_users_row_not_a_token_claim(self) -> None:
        """The same caller: refused while their ROW says non-admin even with
        an admin-asserting token, admitted the moment the ROW flips."""
        self._authenticate_as(
            NON_ADMIN_SUB,
            extra_claims={
                "is_admin": True,
                "cognito:groups": ["admins"],
                "custom:is_admin": "true",
            },
        )
        for route in ALL_ROUTES:
            with self.subTest(route=route, phase="token-claims-only"):
                self.assertEqual(self.client.get(route).status_code, 403)

        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=True)
        for route in ALL_ROUTES:
            with self.subTest(route=route, phase="row-flipped"):
                self.assertEqual(self.client.get(route).status_code, 200)


# ---------------------------------------------------------------------------
# 2. GET /api/admin/spend
# ---------------------------------------------------------------------------


class SpendLedgerTests(AdminDashboardTestBase):
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def test_today_reserved_settled_cap_and_reconcile_remainder(self) -> None:
        # Seeded as real numbers -> moto hands them back as Decimals, the
        # exact shape that 500'd GET /api/users in production (issue #440).
        self.spend_table.put_item(
            Item={
                "spend_date": self._today(),
                "reserved_usd_cents": 1500,
                "settled_usd_cents": 400,
                "daily_cap_usd_cents": 2000,
            }
        )
        resp = self.client.get(SPEND_ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        today = resp.json()["today"]
        self.assertEqual(today["spend_date"], self._today())
        self.assertEqual(today["reserved_usd_cents"], 1500)
        self.assertEqual(today["settled_usd_cents"], 400)
        self.assertEqual(today["daily_cap_usd_cents"], 2000)
        # The reconcile view: 1100c is reserved and not yet settled.
        self.assertEqual(today["outstanding_reservation_usd_cents"], 1100)
        # Never Decimal-typed in the wire payload.
        for value in today.values():
            self.assertNotIsInstance(value, decimal.Decimal)

    def test_a_day_with_no_row_still_renders_with_the_configured_cap(self) -> None:
        resp = self.client.get(SPEND_ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        today = resp.json()["today"]
        self.assertEqual(today["reserved_usd_cents"], 0)
        self.assertEqual(today["settled_usd_cents"], 0)
        self.assertEqual(today["outstanding_reservation_usd_cents"], 0)
        self.assertEqual(
            today["daily_cap_usd_cents"], int(os.environ["DAILY_SPEND_CAP_USD_CENTS"])
        )

    def test_preflight_only_day_never_reports_negative_outstanding(self) -> None:
        """Preflight (#491) / cover-note (#499) spend settles with no
        reservation behind it, so reserved-minus-settled goes negative. The
        raw counters stay honest; the derived figure floors at zero."""
        self.spend_table.put_item(
            Item={"spend_date": self._today(), "reserved_usd_cents": 0, "settled_usd_cents": 90}
        )
        today = self.client.get(SPEND_ROUTE).json()["today"]
        self.assertEqual(today["settled_usd_cents"], 90)
        self.assertEqual(today["outstanding_reservation_usd_cents"], 0)

    def _day(self, days_ago: int) -> str:
        """The UTC `spend_date` key `days_ago` days before today."""
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days_ago * 86400))

    def test_window_is_bounded_and_newest_first(self) -> None:
        for days_ago in range(0, 39):
            self.spend_table.put_item(
                Item={"spend_date": self._day(days_ago), "settled_usd_cents": days_ago}
            )
        body = self.client.get(SPEND_ROUTE, params={"days": 5}).json()
        self.assertEqual(body["window_days"], 5)
        self.assertEqual(len(body["days"]), 5)
        self.assertEqual(
            [d["spend_date"] for d in body["days"]],
            [self._day(0), self._day(1), self._day(2), self._day(3), self._day(4)],
        )

    def test_window_is_calendar_days_not_the_n_most_recent_rows(self) -> None:
        """A row exists only for a day that HAD spend, so "the 30 most recent
        rows" and "the last 30 days" are different windows on any deployment
        that has ever been idle. `days=30` must answer with the calendar
        window it reports in `window_days` — an idle stretch contributes no
        rows rather than dragging quarter-old days into a 30-day tile."""
        # 20 busy days inside the window...
        for days_ago in range(0, 20):
            self.spend_table.put_item(
                Item={"spend_date": self._day(days_ago), "settled_usd_cents": 10}
            )
        # ...then a two-month idle gap, and 10 busy days before it.
        for days_ago in range(80, 90):
            self.spend_table.put_item(
                Item={"spend_date": self._day(days_ago), "settled_usd_cents": 999}
            )

        body = self.client.get(SPEND_ROUTE, params={"days": 30}).json()
        self.assertEqual(body["window_days"], 30)
        returned = [d["spend_date"] for d in body["days"]]
        # Row-count truncation would have served 30 rows spanning ~90 days.
        self.assertEqual(len(returned), 20)
        cutoff = self._day(29)
        self.assertTrue(all(day >= cutoff for day in returned), returned)
        for days_ago in range(80, 90):
            self.assertNotIn(self._day(days_ago), returned)
        # ...and a "last 30 days" tile sums only the in-window spend.
        self.assertEqual(sum(d["settled_usd_cents"] for d in body["days"]), 200)

    def test_a_day_outside_the_window_is_excluded_even_when_rows_are_few(self) -> None:
        """The bound is the DATE, not a shortfall of rows: two rows, one of
        them older than the window, still yields one day."""
        self.spend_table.put_item(Item={"spend_date": self._day(1), "settled_usd_cents": 5})
        self.spend_table.put_item(Item={"spend_date": self._day(9), "settled_usd_cents": 5})
        body = self.client.get(SPEND_ROUTE, params={"days": 7}).json()
        self.assertEqual([d["spend_date"] for d in body["days"]], [self._day(1)])

    def test_a_hostile_window_is_clamped(self) -> None:
        for days_ago in range(0, 39):
            self.spend_table.put_item(Item={"spend_date": self._day(days_ago)})
        body = self.client.get(SPEND_ROUTE, params={"days": 100000}).json()
        self.assertEqual(body["window_days"], admin_dashboard.SPEND_LEDGER_MAX_DAYS)
        self.assertLessEqual(len(body["days"]), admin_dashboard.SPEND_LEDGER_MAX_DAYS)

    def test_worst_case_reservation_matches_the_reservation_path(self) -> None:
        from src import reviews as reviews_module

        body = self.client.get(SPEND_ROUTE).json()
        self.assertEqual(
            body["worst_case_reservation_usd_cents"],
            reviews_module.compute_worst_case_reservation_usd_cents(self.ddb),
        )


# ---------------------------------------------------------------------------
# 3. GET /api/admin/health
# ---------------------------------------------------------------------------


class PipelineHealthTests(AdminDashboardTestBase):
    def test_status_counts_cover_every_status_including_zeroes(self) -> None:
        self._put_review("r-done", status="DONE", created_at="1000")
        self._put_review("r-done-2", status="DONE", created_at="1001")
        self._put_review("r-err", status="ERROR", created_at="1002")
        body = self.client.get(HEALTH_ROUTE).json()
        counts = body["status_counts"]
        self.assertEqual(counts["DONE"], 2)
        self.assertEqual(counts["ERROR"], 1)
        # Present-with-zero so the tile's rows do not appear and vanish.
        self.assertEqual(counts["QUARANTINED"], 0)
        self.assertEqual(counts["CANCELLED"], 0)
        self.assertIn("PENDING", counts)
        self.assertIn("RUNNING", counts)

    def test_stale_in_flight_reviews_are_counted_sampled_and_aged(self) -> None:
        now = int(time.time())
        self._put_review(
            "r-fresh", status="RUNNING", created_at=str(now - 60), updated_at=str(now - 60)
        )
        self._put_review(
            "r-stuck", status="PENDING", created_at=str(now - 5 * HOUR), updated_at=str(now - 5 * HOUR)
        )
        self._put_review(
            "r-stuck-2", status="RUNNING", created_at=str(now - 3 * HOUR), updated_at=str(now - 3 * HOUR)
        )
        body = self.client.get(HEALTH_ROUTE).json()
        self.assertEqual(body["stale"]["threshold_seconds"], admin_dashboard.STALE_IN_FLIGHT_SECONDS_DEFAULT)
        self.assertEqual(body["stale"]["total"], 2)
        self.assertEqual(body["stale"]["pending"], 1)
        self.assertEqual(body["stale"]["running"], 1)
        # Oldest first, so "what has been stuck longest" is the first row.
        self.assertEqual(
            [r["review_id"] for r in body["stale"]["reviews"]], ["r-stuck", "r-stuck-2"]
        )
        self.assertGreaterEqual(body["stale"]["reviews"][0]["age_seconds"], 5 * HOUR)
        self.assertEqual(body["in_flight"], {"total": 3, "pending": 1, "running": 2})

    def test_a_review_still_making_progress_is_not_stale(self) -> None:
        """Age is measured from the LAST movement, not from submission: a
        long review whose `updated_at` keeps advancing is not stuck."""
        now = int(time.time())
        self._put_review(
            "r-long", status="RUNNING", created_at=str(now - 10 * HOUR), updated_at=str(now - 30)
        )
        body = self.client.get(HEALTH_ROUTE).json()
        self.assertEqual(body["stale"]["total"], 0)

    def test_stale_threshold_is_caller_settable_and_clamped(self) -> None:
        now = int(time.time())
        self._put_review(
            "r-recent", status="RUNNING", created_at=str(now - 300), updated_at=str(now - 300)
        )
        tightened = self.client.get(HEALTH_ROUTE, params={"stale_after_seconds": 120}).json()
        self.assertEqual(tightened["stale"]["threshold_seconds"], 120)
        self.assertEqual(tightened["stale"]["total"], 1)

        clamped = self.client.get(HEALTH_ROUTE, params={"stale_after_seconds": 0}).json()
        self.assertEqual(
            clamped["stale"]["threshold_seconds"], admin_dashboard.STALE_IN_FLIGHT_SECONDS_MIN
        )

    def test_concurrency_occupancy_is_running_over_the_configured_cap(self) -> None:
        now = int(time.time())
        for i in range(2):
            self._put_review(
                f"r-run-{i}", status="RUNNING", created_at=str(now), updated_at=str(now)
            )
        body = self.client.get(HEALTH_ROUTE).json()
        self.assertEqual(body["concurrency"]["running"], 2)
        self.assertEqual(body["concurrency"]["limit"], int(os.environ["PIPELINE_MAX_CONCURRENCY"]))
        self.assertAlmostEqual(body["concurrency"]["occupancy"], 0.4, places=4)

    def test_health_never_carries_review_substance(self) -> None:
        now = int(time.time())
        self._put_review(
            "r-stuck",
            status="RUNNING",
            created_at=str(now - 5 * HOUR),
            updated_at=str(now - 5 * HOUR),
            **SENSITIVE_FIELDS,
        )
        resp = self.client.get(HEALTH_ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        for marker in _sentinel_markers():
            self.assertNotIn(marker, resp.text)


# ---------------------------------------------------------------------------
# 4. GET /api/admin/manual-review
# ---------------------------------------------------------------------------


def _sentinel_markers() -> list[str]:
    """Every distinctive marker planted in SENSITIVE_FIELDS, however deeply
    nested -- so the leak assertion below is derived from the fixture rather
    than from a hand-maintained second list that could fall behind it."""
    return sorted(set(re.findall(r"SENTINEL-[A-Z0-9-]+", json.dumps(SENSITIVE_FIELDS))))


class ManualReviewQueueTests(AdminDashboardTestBase):
    def _seed_queue(self) -> None:
        now = int(time.time())
        self._put_review(
            "r-manual",
            status="MANUAL_REVIEW_REQUIRED",
            created_at=str(now - 2 * DAY),
            updated_at=str(now - 2 * DAY),
            failed_at=str(now - 2 * DAY),
            failing_stage="preflight",
            reason="document_too_large",
            legal_triage_status="PENDING_TRIAGE",
            attorney_disposition="EDITED",
            **SENSITIVE_FIELDS,
        )
        self._put_review(
            "r-error-manual",
            status="ERROR_MANUAL_REVIEW_REQUIRED",
            created_at=str(now - HOUR),
            updated_at=str(now - HOUR),
            failed_at=str(now - HOUR),
            failing_stage="primary_review",
            reason="structured_output_retry_exhausted",
        )
        self._put_review(
            "r-triaged",
            status="MANUAL_REVIEW_REQUIRED",
            created_at=str(now - 3 * HOUR),
            updated_at=str(now - 3 * HOUR),
            failed_at=str(now - 3 * HOUR),
            reason="model_context_length_exceeded",
            legal_triage_status="TRIAGED",
            attorney_disposition="REJECTED",
        )
        # Negative controls: neither is a manual-review state.
        self._put_review("r-done", status="DONE", created_at=str(now))
        self._put_review("r-error", status="ERROR", created_at=str(now), reason="model_timeout")

    def test_only_the_two_manual_review_states_are_queued(self) -> None:
        self._seed_queue()
        body = self.client.get(MANUAL_REVIEW_ROUTE).json()
        ids = [r["review_id"] for r in body["reviews"]]
        self.assertEqual(ids, ["r-error-manual", "r-triaged", "r-manual"])  # newest first
        self.assertNotIn("r-done", ids)
        self.assertNotIn("r-error", ids)

    def test_entry_shape_is_exactly_the_documented_key_set(self) -> None:
        self._seed_queue()
        entry = next(
            r
            for r in self.client.get(MANUAL_REVIEW_ROUTE).json()["reviews"]
            if r["review_id"] == "r-manual"
        )
        self.assertEqual(set(entry), EXPECTED_QUEUE_KEYS)
        self.assertEqual(entry["reason"], "document_too_large")
        self.assertEqual(entry["failing_stage"], "preflight")
        self.assertEqual(entry["legal_triage_status"], "PENDING_TRIAGE")

    def test_quarantine_reason_is_coalesced_like_every_other_surface(self) -> None:
        """A row whose cause is stored under `quarantine_reason` must still
        name its cause -- the drift `reviews._resolve_failure_reason` exists
        to prevent."""
        now = int(time.time())
        self._put_review(
            "r-quarantined-manual",
            status="MANUAL_REVIEW_REQUIRED",
            created_at=str(now),
            quarantine_reason="submission_time_bundle_retired",
        )
        entry = self.client.get(MANUAL_REVIEW_ROUTE).json()["reviews"][0]
        self.assertEqual(entry["reason"], "submission_time_bundle_retired")

    def test_sla_breach_follows_the_runbook_24_hour_window(self) -> None:
        self._seed_queue()
        body = self.client.get(MANUAL_REVIEW_ROUTE).json()
        self.assertEqual(body["sla_seconds"], 24 * HOUR)
        by_id = {r["review_id"]: r for r in body["reviews"]}
        self.assertTrue(by_id["r-manual"]["sla_breached"])  # waiting 2 days
        self.assertFalse(by_id["r-error-manual"]["sla_breached"])  # waiting 1 hour
        self.assertGreaterEqual(by_id["r-manual"]["waiting_seconds"], 2 * DAY)

    def test_counts_are_the_tile_figure_and_ignore_the_filter(self) -> None:
        self._seed_queue()
        unfiltered = self.client.get(MANUAL_REVIEW_ROUTE).json()["counts"]
        self.assertEqual(unfiltered["total"], 3)
        self.assertEqual(unfiltered["MANUAL_REVIEW_REQUIRED"], 2)
        self.assertEqual(unfiltered["ERROR_MANUAL_REVIEW_REQUIRED"], 1)
        self.assertEqual(unfiltered["pending_triage"], 1)
        self.assertEqual(unfiltered["triaged"], 1)
        self.assertEqual(unfiltered["sla_breached"], 1)

        filtered = self.client.get(
            MANUAL_REVIEW_ROUTE, params={"status_filter": "ERROR_MANUAL_REVIEW_REQUIRED"}
        ).json()
        self.assertEqual([r["review_id"] for r in filtered["reviews"]], ["r-error-manual"])
        self.assertEqual(filtered["counts"], unfiltered)

    def test_triage_filter_selects_the_owner_workflow_subsets(self) -> None:
        self._seed_queue()

        def ids(**params: Any) -> list[str]:
            return [
                r["review_id"]
                for r in self.client.get(MANUAL_REVIEW_ROUTE, params=params).json()["reviews"]
            ]

        self.assertEqual(ids(triage="pending"), ["r-manual"])
        self.assertEqual(ids(triage="triaged"), ["r-triaged"])
        self.assertEqual(ids(triage="none"), ["r-error-manual"])
        self.assertEqual(len(ids(triage="all")), 3)

    def test_an_unrecognised_filter_is_refused_not_ignored(self) -> None:
        self._seed_queue()
        for params in ({"status_filter": "DONE"}, {"triage": "maybe"}, {"status_filter": "'; --"}):
            with self.subTest(params=params):
                resp = self.client.get(MANUAL_REVIEW_ROUTE, params=params)
                self.assertEqual(resp.status_code, 400, resp.text)
                # A refused filter must not hand back the unfiltered queue.
                self.assertNotIn("r-manual", resp.text)

    def test_limit_is_bounded(self) -> None:
        """Seeded ABOVE the cap deliberately: with a fixture smaller than
        MANUAL_REVIEW_MAX_LIMIT, `len(reviews) <= MAX_LIMIT` holds for any
        implementation — including one with no clamp at all — so the
        assertion would prove nothing. Here an unclamped `limit=100000`
        returns every seeded row and this fails."""
        now = int(time.time())
        seeded = admin_dashboard.MANUAL_REVIEW_MAX_LIMIT + 5
        for i in range(seeded):
            self._put_review(
                f"r-q-{i:03d}", status="MANUAL_REVIEW_REQUIRED", created_at=str(now - i)
            )
        self.assertEqual(len(self.client.get(MANUAL_REVIEW_ROUTE, params={"limit": 3}).json()["reviews"]), 3)
        hostile = self.client.get(MANUAL_REVIEW_ROUTE, params={"limit": 100000}).json()
        self.assertEqual(len(hostile["reviews"]), admin_dashboard.MANUAL_REVIEW_MAX_LIMIT)
        # The count is still exact even when the list is truncated.
        self.assertEqual(hostile["counts"]["total"], seeded)

    def test_the_queue_never_carries_review_substance(self) -> None:
        self._seed_queue()
        resp = self.client.get(MANUAL_REVIEW_ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        markers = _sentinel_markers()
        self.assertTrue(markers, "sentinel extraction produced nothing to assert on")
        for marker in markers:
            self.assertNotIn(marker, resp.text)


# ---------------------------------------------------------------------------
# 5. GET /api/admin/releases
# ---------------------------------------------------------------------------


class ReleaseActivityTests(AdminDashboardTestBase):
    def _seed_release_history(self) -> None:
        """Build the audit rows by calling the real WRITERS, so this fixture
        cannot drift from the shape production actually stores."""
        for version, content_hash in (("1.0.0", "hash-one"), ("2.0.0", "hash-two")):
            self.versions_table.put_item(
                Item={
                    "playbook_id": PLAYBOOK_ID,
                    "version": version,
                    "status": "uploaded",
                    "content_hash": content_hash,
                }
            )
        playbook_versions.activate_playbook_version(PLAYBOOK_ID, "1.0.0", ADMIN_SUB, self.ddb)
        playbook_versions.activate_playbook_version(PLAYBOOK_ID, "2.0.0", ADMIN_SUB, self.ddb)
        playbook_versions.rollback_playbook_version(PLAYBOOK_ID, "1.0.0", ADMIN_SUB, self.ddb)

    def test_activations_and_rollbacks_render_with_their_bundle_hashes(self) -> None:
        self._seed_release_history()
        body = self.client.get(RELEASES_ROUTE).json()
        actions = [r["action"] for r in body["releases"]]
        self.assertEqual(
            sorted(actions),
            ["release_bundle_activate", "release_bundle_activate", "release_bundle_rollback"],
        )
        rollback = next(r for r in body["releases"] if r["action"] == "release_bundle_rollback")
        self.assertEqual(rollback["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(rollback["version"], "1.0.0")
        self.assertEqual(rollback["content_hash"], "hash-one")
        self.assertEqual(rollback["actor"], ADMIN_SUB)
        self.assertEqual(rollback["prior_active_version"], "2.0.0")
        self.assertIs(rollback["gate7_reevaluated"], False)
        self.assertIsInstance(rollback["recorded_at"], int)

    def test_a_non_release_audit_row_never_joins_the_feed(self) -> None:
        self._seed_release_history()
        # A REAL audit row from a different writer, in the same partition.
        # (`is_admin` rather than `status`: users.update_user's
        # UpdateExpression spells the attribute name literally, and `status`
        # is a DynamoDB reserved keyword -- a pre-existing defect in that
        # function, unrelated to this ticket and not worked around here
        # beyond choosing the field that does exercise the audit write.)
        update_user(
            TARGET_SUB,
            {"is_admin": True},
            self.users_table.get_item(Key={"cognito_sub": ADMIN_SUB})["Item"],
            self.ddb,
        )
        body = self.client.get(RELEASES_ROUTE).json()
        self.assertEqual(len(body["releases"]), 3)
        self.assertNotIn("user_lifecycle_update", json.dumps(body))
        self.assertNotIn(TARGET_SUB, json.dumps(body))

    def test_release_limit_is_bounded(self) -> None:
        """Seeded ABOVE the cap: with only the three real history rows,
        `len(releases) <= RELEASE_ACTIVITY_MAX_LIMIT` holds however the
        limit is (mis)handled. The extra rows are CLONES of a row the real
        writer produced — copied, not hand-built — so the fixture cannot
        drift from the shape production stores, and an unclamped
        `limit=100000` returns all of them and fails this."""
        self._seed_release_history()
        template = next(
            item
            for item in self.audit_table.scan()["Items"]
            if item.get("action") == "release_bundle_activate"
        )
        epoch = int(time.time())
        for i in range(admin_dashboard.RELEASE_ACTIVITY_MAX_LIMIT + 5):
            clone = dict(template)
            clone["event_id"] = f"clone-{i:03d}"
            clone["timestamp"] = f"{epoch - i}#clone-{i:03d}"
            self.audit_table.put_item(Item=clone)

        self.assertEqual(len(self.client.get(RELEASES_ROUTE, params={"limit": 1}).json()["releases"]), 1)
        hostile = self.client.get(RELEASES_ROUTE, params={"limit": 100000}).json()
        self.assertEqual(len(hostile["releases"]), admin_dashboard.RELEASE_ACTIVITY_MAX_LIMIT)

    def test_cost_outliers_degrade_when_the_ledger_is_absent(self) -> None:
        """No MODEL_INVOCATIONS_TABLE is the mock-pipeline shape: report
        unavailable, never 500."""
        self.assertIsNone(os.environ.get("MODEL_INVOCATIONS_TABLE"))
        resp = self.client.get(RELEASES_ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        outliers = resp.json()["cost_outliers"]
        self.assertFalse(outliers["available"])
        self.assertEqual(outliers["reviews"], [])

    def test_a_runaway_review_is_flagged_against_the_median(self) -> None:
        ledger = self._seed_ledger_table()

        def _ledger_row(review_id: str, input_tokens: Any, output_tokens: Any) -> None:
            ledger.put_item(
                Item={
                    "review_id": review_id,
                    "record_id": f"primary#01#{review_id}",
                    "pass_name": "primary",
                    "model_id": "synthetic-primary",
                    "attempt_number": 1,
                    "outcome": "success",
                    "actual_input_tokens": input_tokens,
                    "actual_output_tokens": output_tokens,
                }
            )

        for i in range(3):
            _ledger_row(f"r-normal-{i}", 10_000, 1_000)
        _ledger_row("r-runaway", 1_000_000, 100_000)
        # Never measured (#414 stores None, not 0) -> priced at nothing at
        # all rather than silently counted as a free review.
        _ledger_row("r-unmeasured", None, None)

        outliers = self.client.get(RELEASES_ROUTE).json()["cost_outliers"]
        self.assertTrue(outliers["available"])
        self.assertEqual(outliers["multiple"], admin_dashboard.COST_OUTLIER_MULTIPLE)
        self.assertEqual(outliers["sample_size"], 4)
        flagged = [o["review_id"] for o in outliers["reviews"]]
        self.assertEqual(flagged, ["r-runaway"])
        self.assertGreaterEqual(
            outliers["reviews"][0]["multiple_of_median"], admin_dashboard.COST_OUTLIER_MULTIPLE
        )
        self.assertNotIn("r-unmeasured", json.dumps(outliers))

    def _ledger_pass(
        self,
        ledger: Any,
        review_id: str,
        pass_name: str,
        input_tokens: Any,
        output_tokens: Any,
    ) -> None:
        ledger.put_item(
            Item={
                "review_id": review_id,
                "record_id": f"{pass_name}#01#{review_id}",
                "pass_name": pass_name,
                "model_id": f"synthetic-{pass_name}",
                "attempt_number": 1,
                "outcome": "success",
                "actual_input_tokens": input_tokens,
                "actual_output_tokens": output_tokens,
            }
        )

    def test_every_ledgered_pass_is_priced_not_just_primary_and_critic(self) -> None:
        """This codebase ledgers FIVE `pass_name` values — primary, critic,
        floor, requote (#569) and cover_note. Pricing only primary+critic
        credits the other three at $0, which understates exactly the review
        that ran extra passes and biases the runaway/injection flag toward
        false negatives. Here the expensive tokens are ALL on non-primary,
        non-critic passes."""
        from src import reviews as reviews_module

        ledger = self._seed_ledger_table()
        for i in range(3):
            self._ledger_pass(ledger, f"r-normal-{i}", "primary", 10_000, 1_000)
        # A review whose primary pass is unremarkable but whose repair and
        # Floor passes ran away.
        self._ledger_pass(ledger, "r-repair", "primary", 10_000, 1_000)
        self._ledger_pass(ledger, "r-repair", "requote", 900_000, 90_000)
        self._ledger_pass(ledger, "r-repair", "floor", 100_000, 10_000)
        self._ledger_pass(ledger, "r-repair", "cover_note", 5_000, 500)

        outliers = self.client.get(RELEASES_ROUTE).json()["cost_outliers"]
        self.assertTrue(outliers["available"])
        self.assertEqual([o["review_id"] for o in outliers["reviews"]], ["r-repair"])

        # The priced figure is the WHOLE review: every non-critic pass
        # summed at primary rates.
        expected = reviews_module.compute_actual_usd_cents_from_usage(
            {"input_tokens": 1_015_000, "output_tokens": 101_500}, None, self.ddb
        )
        self.assertEqual(outliers["reviews"][0]["actual_usd_cents"], expected)

    def test_the_critic_pass_is_priced_at_critic_rates(self) -> None:
        """The one pass that is NOT folded into the primary slot: the split
        `_CRITIC_RATE_PASSES` documents has to be the split that is used."""
        from src import reviews as reviews_module

        ledger = self._seed_ledger_table()
        self._ledger_pass(ledger, "r-split", "primary", 40_000, 4_000)
        self._ledger_pass(ledger, "r-split", "critic", 20_000, 2_000)

        outliers = self.client.get(RELEASES_ROUTE).json()["cost_outliers"]
        self.assertEqual(outliers["sample_size"], 1)
        self.assertEqual(
            outliers["median_usd_cents"],
            reviews_module.compute_actual_usd_cents_from_usage(
                {"input_tokens": 40_000, "output_tokens": 4_000},
                {"input_tokens": 20_000, "output_tokens": 2_000},
                self.ddb,
            ),
        )
        # ...and that is NOT the same as billing the whole review at primary
        # rates, so the split is doing real work.
        self.assertNotEqual(
            outliers["median_usd_cents"],
            reviews_module.compute_actual_usd_cents_from_usage(
                {"input_tokens": 60_000, "output_tokens": 6_000}, None, self.ddb
            ),
        )

    def test_the_provider_rates_are_resolved_once_for_the_whole_request(self) -> None:
        """`reviews._active_provider_rates` re-resolves on EVERY call — on
        the OpenRouter path it re-reads `model-policy/openrouter.json` and
        the admin model-selection DynamoDB row — so pricing each review
        through the pricing function's own resolution made one
        `GET /api/admin/releases` an N+1 over an append-only ledger: 8
        reviews, 8 resolutions, and it grows for the life of the
        deployment. ONE resolution per request, whatever the sample size."""
        from src import reviews as reviews_module

        ledger = self._seed_ledger_table()
        for i in range(8):
            self._ledger_pass(ledger, f"r-priced-{i:02d}", "primary", 10_000, 1_000)

        with mock.patch.object(
            reviews_module,
            "_active_provider_rates",
            wraps=reviews_module._active_provider_rates,
        ) as rate_spy:
            body = self.client.get(RELEASES_ROUTE).json()

        # Every review was still priced — this is not "resolved once because
        # nothing was priced".
        self.assertEqual(body["cost_outliers"]["sample_size"], 8)
        self.assertEqual(rate_spy.call_count, 1)

    def test_the_ledger_read_is_bounded_and_says_when_it_was_cut(self) -> None:
        """The baseline used to be "the median over every review ever run",
        an uncapped scan of an append-only ledger on every dashboard poll.
        The read is capped, the cap is clamped like every other bound here,
        and a cut sample is REPORTED rather than passed off as the
        deployment's median."""
        ledger = self._seed_ledger_table()
        for i in range(10):
            self._ledger_pass(ledger, f"r-capped-{i:02d}", "primary", 10_000, 1_000)
        admin_row = self.users_table.get_item(Key={"cognito_sub": ADMIN_SUB})["Item"]

        capped = admin_dashboard.get_release_activity(
            admin_row, self.ddb, outlier_sample_rows=4
        )["cost_outliers"]
        self.assertTrue(capped["available"])
        self.assertEqual(capped["sample_row_cap"], 4)
        self.assertIs(capped["truncated"], True)
        # Fewer reviews priced than the ledger holds: the cap bounded the
        # READ, not just the output.
        self.assertLessEqual(capped["sample_size"], 4)
        self.assertLess(capped["sample_size"], 10)

        # Same ledger under the default cap: nothing is cut, everything priced.
        whole = self.client.get(RELEASES_ROUTE).json()["cost_outliers"]
        self.assertEqual(
            whole["sample_row_cap"], admin_dashboard.COST_OUTLIER_LEDGER_ROWS_DEFAULT
        )
        self.assertIs(whole["truncated"], False)
        self.assertEqual(whole["sample_size"], 10)

        # A hostile sample size is clamped, exactly like `?days=` and `?limit=`.
        hostile = admin_dashboard.get_release_activity(
            admin_row, self.ddb, outlier_sample_rows=10_000_000
        )["cost_outliers"]
        self.assertEqual(
            hostile["sample_row_cap"], admin_dashboard.COST_OUTLIER_LEDGER_ROWS_MAX
        )

    def test_nothing_is_flagged_below_the_minimum_sample(self) -> None:
        """With one review the median IS that review; a lone expensive
        review must not be an 'outlier' against itself."""
        ledger = self._seed_ledger_table()
        ledger.put_item(
            Item={
                "review_id": "r-only",
                "record_id": "primary#01#1",
                "pass_name": "primary",
                "actual_input_tokens": 1_000_000,
                "actual_output_tokens": 100_000,
            }
        )
        outliers = self.client.get(RELEASES_ROUTE).json()["cost_outliers"]
        self.assertTrue(outliers["available"])
        self.assertEqual(outliers["sample_size"], 1)
        self.assertEqual(outliers["reviews"], [])


# ---------------------------------------------------------------------------
# 6. De-brand
# ---------------------------------------------------------------------------


class DebrandTests(AdminDashboardTestBase):
    def test_no_tenant_brand_in_any_payload(self) -> None:
        now = int(time.time())
        self._put_review(
            "r-manual",
            status="MANUAL_REVIEW_REQUIRED",
            created_at=str(now),
            reason="document_too_large",
        )
        self.spend_table.put_item(
            Item={"spend_date": time.strftime("%Y-%m-%d", time.gmtime()), "settled_usd_cents": 5}
        )
        for route in ALL_ROUTES:
            with self.subTest(route=route):
                resp = self.client.get(route)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertNotIn("Exos", resp.text)
                self.assertNotIn("EXOS", resp.text)
                self.assertNotIn("exos", resp.text)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(case)
        for case in (
            AdminGateTests,
            SpendLedgerTests,
            PipelineHealthTests,
            ManualReviewQueueTests,
            ReleaseActivityTests,
            DebrandTests,
        )
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
