#!/usr/bin/env python3
"""
Executable tests for issue #443: GET /api/admin/diagnostics/recent-failures --
the admin Diagnostics route that surfaces WHY recent reviews failed without
shell access to a production container.

Drives the REAL, shipped application object (`src.main.app`) via a FastAPI
`TestClient`, same convention as tests/test_pen_rules_validate_route_432.py and
tests/test_playbook_version_routes_430.py. DynamoDB is an in-memory fake (the
users table for the admin gate, the reviews table for the scan); both
`get_dynamodb_resource` and `get_current_user` are dependency-overridden. No
network, no AWS.

## What is asserted here

  1. ADMIN-ONLY. A non-admin caller gets HTTP 403 -- this is an
     instance-wide view of every user's failures, so there is no
     "your own row" case.
  2. ONLY FAILURES. Successful (`DONE`), still-running (`PENDING`/`RUNNING`)
     and administratively-superseded (`SUPERSEDED`) reviews are not failures
     and never appear; `ERROR`, `ERROR_MANUAL_REVIEW_REQUIRED`,
     `MANUAL_REVIEW_REQUIRED` and `QUARANTINED` do.
  3. NOTHING LEAKS -- the negative assertion this ticket exists for. A failed
     review row is seeded with every sensitive-looking field a real row can
     carry (document substance, the reviewer's own free-text guidance, an
     S3 key, a stack trace, an exception message, an API key) and NONE of it
     appears anywhere in the response body. The route is a controlled
     projection, not a log viewer -- so the assertion is made against the RAW
     response text, not against a parsed subset, and the row shape is
     asserted to be EXACTLY the five documented fields.
  4. BOUNDED. `?limit=` is clamped into [1, RECENT_FAILURES_MAX_LIMIT]; the
     default is RECENT_FAILURES_DEFAULT_LIMIT; a hostile `limit=100000`
     cannot turn the route into a full-table dump.
  5. NEWEST FIRST, so "what just broke" is the first row.
  6. THE PRODUCTION DECIMAL HAZARD (issue #440 / commit df60971): boto3's
     resource API hands back `decimal.Decimal` for every stored number, which
     `JSONResponse` cannot encode -- that 500'd GET /api/users on the live
     deployment while the suite stayed green, because the in-memory fakes
     store plain ints. This file seeds Decimals deliberately.
  7. THE REAL QUARANTINED ROW SHAPE. The one QUARANTINED writer in the
     backend (`verify_submission_time_bundle`) stores its cause under
     `quarantine_reason`, never `reason`, and writes no `failing_stage` -- so
     that row is built here by calling that WRITER, not by hand-copying its
     believed output, and the route must still name the cause.
  8. NO DRIFT WITH THE READER-FACING COPY. `frontend/src/AdminDiagnostics.tsx`
     must CONSUME `ReviewSubmission.tsx`'s `REASON_EXPLANATIONS` /
     `explainFailure` rather than defining a second token->prose table, which
     would drift the moment one surface gained a token the other lacked.

MUST FAIL on the pre-implementation tree: the route is not registered (404)
and `reviews.list_recent_failures` does not exist.

Run standalone: `python3 tests/test_admin_diagnostics_443.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import decimal
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-status-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")

from fastapi.testclient import TestClient  # noqa: E402

import src.main as backend_main  # noqa: E402
from src import reviews as reviews_module  # noqa: E402

ROUTE = "/api/admin/diagnostics/recent-failures"

FRONTEND_DIR = REPO_ROOT / "frontend" / "src"
ADMIN_DIAGNOSTICS_TSX = FRONTEND_DIR / "AdminDiagnostics.tsx"
REVIEW_SUBMISSION_TSX = FRONTEND_DIR / "ReviewSubmission.tsx"


# ---------------------------------------------------------------------------
# Sensitive material planted on a failed review row.
#
# Every one of these is a field a REAL reviews row can carry (see
# reviews._create_review_row, the persist stage's writes, and
# get_review_detail's projection) -- plus the two things a log viewer would
# have exposed and this route must not: an exception message and a stack
# trace. Each value is a distinctive marker string so its presence anywhere in
# the response body is unambiguous.
# ---------------------------------------------------------------------------
SENSITIVE_FIELDS: dict[str, Any] = {
    "verdict_summary": "SENTINEL-VERDICT-indemnity is uncapped in clause 9",
    "issues": [{"rationale_text": "SENTINEL-RATIONALE-counterparty redlined the cap"}],
    "issue_rationale_text": "SENTINEL-RATIONALE-TEXT",
    "toaster_guidance": "SENTINEL-GUIDANCE-be lenient on the payment terms",
    "output_s3_key": "outputs/sub-owner/SENTINEL-S3-KEY/out.docx",
    "upload_s3_key": "uploads/sub-owner/SENTINEL-UPLOAD-KEY/in.docx",
    "owner_sub": "SENTINEL-OWNER-SUB",
    "exception_message": "SENTINEL-EXC-OpenRouter returned HTTP 402 for key sk-or-v1-SENTINEL",
    "stack_trace": 'SENTINEL-TRACE-File "/app/backend/src/pipeline_runner.py", line 1, in run',
    "model_api_key": "sk-or-v1-SENTINEL-KEY-MATERIAL",
    "execution_arn": "arn:aws:states:us-east-1:000000000000:execution:SENTINEL-EXEC",
}


def _review_row(
    review_id: str,
    *,
    status_value: str,
    created_at: Any,
    reason: str | None = None,
    failing_stage: str | None = None,
    sensitive: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "review_id": review_id,
        "status": status_value,
        "created_at": created_at,
        "updated_at": created_at,
        "playbook_id": "synthetic-nda-sample",
    }
    if reason is not None:
        row["reason"] = reason
    if failing_stage is not None:
        row["failing_stage"] = failing_stage
    if sensitive:
        row.update(SENSITIVE_FIELDS)
    return row


# ---------------------------------------------------------------------------
# In-memory DynamoDB fake -- users (get_item) + reviews (scan).
# ---------------------------------------------------------------------------


class FakeUsersTable:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def get_item(self, Key):  # noqa: N803 - boto3 kwarg name
        item = self.items.get(Key["cognito_sub"])
        return {"Item": dict(item)} if item else {}


class FakeReviewsTable:
    """Scan-first stand-in. Deliberately has NO `query`, and returns rows in
    an arbitrary (insertion) order -- ordering is the route's job.

    Also supports `put_item`/`get_item`/`update_item` so a test can let the
    REAL production writer shape a row instead of hand-copying what that
    writer is believed to store. `update_item` applies a plain `SET a = :x,
    #b = :y` expression GENERICALLY -- it does not pattern-match any
    particular caller's expression string, so it cannot silently agree with a
    writer that changed.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])

    def scan(self, **_kwargs):
        return {"Items": [dict(r) for r in self.rows]}

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg name
        self.rows = [r for r in self.rows if r["review_id"] != Item["review_id"]]
        self.rows.append(dict(Item))

    def get_item(self, Key):  # noqa: N803 - boto3 kwarg name
        for row in self.rows:
            if row["review_id"] == Key["review_id"]:
                return {"Item": dict(row)}
        return {}

    def update_item(  # noqa: N803 - boto3 kwarg names
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        **_kwargs,
    ):
        expression = UpdateExpression.strip()
        if not expression.upper().startswith("SET "):
            raise AssertionError(f"fake only understands SET: {UpdateExpression!r}")
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}

        target = None
        for row in self.rows:
            if row["review_id"] == Key["review_id"]:
                target = row
                break
        if target is None:
            target = dict(Key)
            self.rows.append(target)

        for assignment in expression[4:].split(","):
            attribute, _, placeholder = assignment.partition("=")
            attribute = attribute.strip()
            target[names.get(attribute, attribute)] = values[placeholder.strip()]


class FakePlaybooksTable:
    """Only what `_read_active_release_bundle_hash` touches. An EMPTY table is
    the documented "no bundle is active" state, which is one of the two ways a
    review gets quarantined at the verify step."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def get_item(self, Key):  # noqa: N803 - boto3 kwarg name
        item = self.items.get(Key["playbook_id"])
        return {"Item": dict(item)} if item else {}


class FakeDynamoDBResource:
    def __init__(
        self,
        users: FakeUsersTable,
        reviews: FakeReviewsTable,
        playbooks: "FakePlaybooksTable | None" = None,
    ) -> None:
        self._tables = {
            os.environ["USERS_TABLE"]: users,
            os.environ["REVIEWS_TABLE"]: reviews,
            os.environ["PLAYBOOKS_TABLE"]: playbooks or FakePlaybooksTable(),
        }

    def Table(self, name: str):  # noqa: N802 - boto3 method name
        return self._tables[name]


def _seed_user(table: FakeUsersTable, sub: str, *, is_admin: bool) -> None:
    table.items[sub] = {
        "cognito_sub": sub,
        "email": f"{sub}@example.com",
        "status": "active",
        "is_admin": is_admin,
        "last_auth_at": 1000,
        "created_at": 900,
        "admission": "jit",
    }


class DiagnosticsRouteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.users = FakeUsersTable()
        self.reviews = FakeReviewsTable()
        self.playbooks = FakePlaybooksTable()
        self.ddb = FakeDynamoDBResource(self.users, self.reviews, self.playbooks)
        self.app = backend_main.app
        self.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def _authenticate_as(self, sub: str) -> None:
        claims = {"sub": sub, "email": f"{sub}@example.com", "token_use": "id"}
        self.app.dependency_overrides[backend_main.get_current_user] = lambda: claims

    def _as_admin(self) -> None:
        _seed_user(self.users, "sub-admin", is_admin=True)
        self._authenticate_as("sub-admin")

    def _as_reviewer(self) -> None:
        _seed_user(self.users, "sub-reviewer", is_admin=False)
        self._authenticate_as("sub-reviewer")

    def _get(self, query: str = ""):
        return self.client.get(f"{ROUTE}{query}")


# ---------------------------------------------------------------------------
# 1. Admin-only
# ---------------------------------------------------------------------------


class TestAdminGate(DiagnosticsRouteTestBase):
    def test_non_admin_is_forbidden(self) -> None:
        self._as_reviewer()
        self.reviews.rows = [
            _review_row("r-1", status_value="ERROR", created_at="1000", reason="model_key_rejected")
        ]
        resp = self._get()
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_response_carries_no_review_data(self) -> None:
        """A 403 that still echoed the rows would be worse than no route."""
        self._as_reviewer()
        self.reviews.rows = [
            _review_row(
                "r-secret",
                status_value="ERROR",
                created_at="1000",
                reason="model_key_rejected",
                failing_stage="run_review",
                sensitive=True,
            )
        ]
        body = self._get().text
        self.assertNotIn("r-secret", body)
        for value in SENSITIVE_FIELDS.values():
            self.assertNotIn(str(value), body)

    def test_admin_is_allowed(self) -> None:
        self._as_admin()
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"failures": []})


# ---------------------------------------------------------------------------
# 2. Only failures, and every failure
# ---------------------------------------------------------------------------


class TestWhichRowsAppear(DiagnosticsRouteTestBase):
    def _ids(self, query: str = "") -> list[str]:
        resp = self._get(query)
        self.assertEqual(resp.status_code, 200)
        return [row["review_id"] for row in resp.json()["failures"]]

    def test_succeeded_running_and_superseded_reviews_never_appear(self) -> None:
        self._as_admin()
        self.reviews.rows = [
            _review_row("r-done", status_value="DONE", created_at="1005"),
            _review_row("r-pending", status_value="PENDING", created_at="1004"),
            _review_row("r-running", status_value="RUNNING", created_at="1003"),
            _review_row("r-superseded", status_value="SUPERSEDED", created_at="1002"),
            _review_row(
                "r-error",
                status_value="ERROR",
                created_at="1001",
                reason="model_account_out_of_credits",
                failing_stage="run_review",
            ),
        ]
        self.assertEqual(self._ids(), ["r-error"])

    def test_every_failure_terminal_appears(self) -> None:
        self._as_admin()
        failure_statuses = [
            "ERROR",
            "ERROR_MANUAL_REVIEW_REQUIRED",
            "MANUAL_REVIEW_REQUIRED",
            "QUARANTINED",
        ]
        self.reviews.rows = [
            _review_row(
                f"r-{i}",
                status_value=s,
                created_at=f"{1000 + i}",
                reason="unhandled_exception",
                failing_stage="run_review",
            )
            for i, s in enumerate(failure_statuses)
        ]
        self.assertEqual(sorted(self._ids()), sorted(f"r-{i}" for i in range(len(failure_statuses))))

    def test_a_quarantined_row_as_production_actually_writes_it_carries_its_cause(self) -> None:
        """The row shape the fixtures could not produce (review of #443).

        `_review_row` above seeds `reason=...`, and no production writer ever
        stores a quarantine cause there. The ONLY QUARANTINED writer in the
        backend, `reviews.verify_submission_time_bundle`, writes
        `quarantine_reason` and never `reason` or `failing_stage` -- so a
        projection that read the bare `reason` attribute returned
        `{"reason": null, "failing_stage": null}` for every quarantined
        review, rendering as Stage "--" and "no cause was recorded" while the
        submitter's own Review tab (which coalesces) showed the true cause.

        Same class of blind spot as issue #440, where the fakes stored plain
        ints and production stored `Decimal`. So this test does NOT hand-copy
        the believed row shape: it drives the REAL writer against the fake
        table and then reads the row back out through the REAL route. If that
        writer ever moves the token to a different attribute, this fails.
        """
        self._as_admin()
        # A running review whose submission-time bundle is about to be found
        # retired: the playbooks table is empty, the documented "no bundle is
        # active" state.
        self.reviews.put_item(
            Item={
                "review_id": "r-quarantined",
                "status": "RUNNING",
                "created_at": "1700000000",
                "playbook_id": "synthetic-nda-sample",
                "playbook_hash": "sha256:retired-bundle",
            }
        )

        verdict = reviews_module.verify_submission_time_bundle(
            review_id="r-quarantined",
            playbook_id="synthetic-nda-sample",
            submission_time_bundle_hash="sha256:retired-bundle",
            dynamodb_resource=self.ddb,
        )
        self.assertFalse(verdict["verified"], "precondition: the review must be quarantined")

        persisted = self.reviews.get_item(Key={"review_id": "r-quarantined"})["Item"]
        self.assertEqual(persisted["status"], "QUARANTINED")
        self.assertEqual(
            persisted["quarantine_reason"],
            reviews_module.QUARANTINE_REASON_SUBMISSION_TIME_BUNDLE_RETIRED,
        )
        self.assertNotIn("reason", persisted, "precondition: production writes no `reason` here")
        self.assertNotIn("failing_stage", persisted)

        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["failures"]
        self.assertEqual([r["review_id"] for r in rows], ["r-quarantined"])
        self.assertEqual(
            rows[0]["reason"],
            reviews_module.QUARANTINE_REASON_SUBMISSION_TIME_BUNDLE_RETIRED,
            "an operator must be able to read WHY a review was quarantined",
        )
        # Coalesced INTO `reason`, not served as a sixth field: the allowlist
        # is the security boundary and its shape is unchanged.
        self.assertEqual(
            sorted(rows[0]),
            sorted(reviews_module._RECENT_FAILURE_FIELDS),
        )
        self.assertNotIn("sha256:retired-bundle", resp.text, "the stale hash is not served")

    def test_the_failure_status_set_is_derived_from_the_terminal_vocabulary(self) -> None:
        """A terminal FAILURE status added later must show up here on its own;
        only the two non-failure terminals are carved out."""
        self.assertEqual(
            set(reviews_module.DIAGNOSTIC_FAILURE_STATUSES),
            set(reviews_module.REVIEW_STATUSES_TERMINAL) - {"DONE", "SUPERSEDED"},
        )

    def test_newest_first(self) -> None:
        self._as_admin()
        self.reviews.rows = [
            _review_row("r-old", status_value="ERROR", created_at="1000", reason="x"),
            _review_row("r-new", status_value="ERROR", created_at="3000", reason="x"),
            _review_row("r-mid", status_value="ERROR", created_at="2000", reason="x"),
        ]
        self.assertEqual(self._ids(), ["r-new", "r-mid", "r-old"])


# ---------------------------------------------------------------------------
# 3. The negative assertion: nothing but the five fields crosses the boundary
# ---------------------------------------------------------------------------


class TestNothingSensitiveIsEchoed(DiagnosticsRouteTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._as_admin()
        self.reviews.rows = [
            _review_row(
                "r-leaky",
                status_value="ERROR",
                created_at="1700000000",
                reason="model_account_out_of_credits",
                failing_stage="run_review",
                sensitive=True,
            )
        ]

    def test_row_shape_is_exactly_the_five_documented_fields(self) -> None:
        row = self._get().json()["failures"][0]
        self.assertEqual(
            sorted(row.keys()),
            sorted(["review_id", "created_at", "failing_stage", "reason", "status"]),
        )
        self.assertEqual(row["reason"], "model_account_out_of_credits")
        self.assertEqual(row["failing_stage"], "run_review")
        self.assertEqual(row["status"], "ERROR")

    def test_no_sensitive_field_appears_anywhere_in_the_response(self) -> None:
        body = self._get().text
        for field, value in SENSITIVE_FIELDS.items():
            with self.subTest(field=field):
                self.assertNotIn(field, body)
                for marker in str(value).split():
                    if marker.startswith(("SENTINEL", "sk-or-v1", "arn:aws")):
                        self.assertNotIn(marker, body)

    def test_a_field_added_to_the_row_tomorrow_cannot_appear(self) -> None:
        """The projection is an allowlist, so an unknown field is invisible by
        construction -- not by anyone remembering to redact it."""
        self.reviews.rows[0]["some_future_field"] = "SENTINEL-FUTURE"
        body = self._get().text
        self.assertNotIn("some_future_field", body)
        self.assertNotIn("SENTINEL-FUTURE", body)


# ---------------------------------------------------------------------------
# 4. Bounded
# ---------------------------------------------------------------------------


class TestBounded(DiagnosticsRouteTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._as_admin()
        self.reviews.rows = [
            _review_row(
                f"r-{i:05d}",
                status_value="ERROR",
                created_at=f"{2000000000 - i}",
                reason="unhandled_exception",
                failing_stage="run_review",
            )
            for i in range(reviews_module.RECENT_FAILURES_MAX_LIMIT + 250)
        ]

    def _count(self, query: str = "") -> int:
        resp = self._get(query)
        self.assertEqual(resp.status_code, 200)
        return len(resp.json()["failures"])

    def test_default_limit(self) -> None:
        self.assertEqual(self._count(), reviews_module.RECENT_FAILURES_DEFAULT_LIMIT)

    def test_a_hostile_limit_cannot_dump_the_table(self) -> None:
        self.assertEqual(
            self._count("?limit=100000"), reviews_module.RECENT_FAILURES_MAX_LIMIT
        )

    def test_zero_and_negative_limits_clamp_up_rather_than_returning_everything(self) -> None:
        self.assertEqual(self._count("?limit=0"), 1)
        self.assertEqual(self._count("?limit=-5"), 1)

    def test_a_smaller_limit_is_honored(self) -> None:
        self.assertEqual(self._count("?limit=3"), 3)


# ---------------------------------------------------------------------------
# 6. The production Decimal hazard
# ---------------------------------------------------------------------------


class TestDecimalRowsDoNotFiveHundred(DiagnosticsRouteTestBase):
    def test_a_decimal_created_at_is_serialized_rather_than_500ing(self) -> None:
        """boto3's resource API returns Decimal for every stored number.
        `GET /api/users` 500'd in production on exactly this (issue #440)
        while the suite stayed green, because the fakes store plain ints."""
        self._as_admin()
        self.reviews.rows = [
            _review_row(
                "r-decimal",
                status_value="ERROR",
                created_at=decimal.Decimal("1700000000"),
                reason="model_rate_limited",
                failing_stage="run_review",
            )
        ]
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        row = resp.json()["failures"][0]
        self.assertEqual(row["created_at"], 1700000000)
        # And it really is JSON, not a repr of a Decimal.
        json.loads(resp.text)


# ---------------------------------------------------------------------------
# 7. One token->prose table, shared by both surfaces
# ---------------------------------------------------------------------------


class TestFrontendSharesTheExplanationMap(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            ADMIN_DIAGNOSTICS_TSX.is_file(),
            "frontend/src/AdminDiagnostics.tsx is missing -- issue #443's Diagnostics tab",
        )
        self.source = ADMIN_DIAGNOSTICS_TSX.read_text(encoding="utf-8")

    def test_it_imports_the_shared_explanation_helpers(self) -> None:
        self.assertIn("./ReviewSubmission", self.source)
        self.assertIn("explainFailure", self.source)

    def test_it_does_not_define_a_second_token_to_prose_table(self) -> None:
        """Two copies of the map is exactly how the reviewer-facing copy and
        the admin-facing copy would drift apart."""
        self.assertNotIn("const REASON_EXPLANATIONS", self.source)
        self.assertNotIn("const STAGE_EXPLANATIONS", self.source)

    def test_review_submission_still_owns_and_exports_the_table(self) -> None:
        source = REVIEW_SUBMISSION_TSX.read_text(encoding="utf-8")
        self.assertIn("export const REASON_EXPLANATIONS", source)
        self.assertIn("export function explainFailure", source)

    def test_load_failures_route_their_technical_detail_through_the_shared_helper(self) -> None:
        """Issue #425: the raw endpoint + status string goes to the CONSOLE via
        `friendlyErrorMessage`, and only its user-safe fallback reaches the
        DOM. Every other admin screen does this; a bare
        `setError(`... HTTP ${status}`)` here would put a status code on
        screen."""
        self.assertIn("friendlyErrorMessage", self.source)
        lines = self.source.splitlines()
        for i, line in enumerate(lines):
            if "HTTP $" not in line:
                continue
            window = "\n".join(lines[max(0, i - 3) : i + 2])
            self.assertIn(
                "friendlyErrorMessage",
                window,
                f"a raw HTTP status appears outside friendlyErrorMessage: {line.strip()}",
            )


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestAdminGate,
        TestWhichRowsAppear,
        TestNothingSensitiveIsEchoed,
        TestBounded,
        TestDecimalRowsDoNotFiveHundred,
        TestFrontendSharesTheExplanationMap,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
