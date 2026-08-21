#!/usr/bin/env python3
"""
Executable tests for issue #486: wiring `backend/src/disposition.py`'s
already-implemented capture path (`record_disposition`) into a real route.

Before this ticket, `main.py` never imported `disposition` and no route
referenced it -- every `attorney_disposition*` field the reviews table
schema anticipated was permanently null in the running system. These tests
drive the real `src.review_routes.router` (the same router `src.main.app`
mounts) end-to-end via a FastAPI `TestClient` on a LOCAL `FastAPI()` app --
same convention as tests/test_review_api_84.py.

Covers the issue's acceptance criteria:
  - Complete a review -> record EDITED + note -> the response AND the
    review detail projection carry it; `legal_triage_status` is set to
    PENDING_TRIAGE; an audit row is written (issue AC 1).
  - Another user's review id -> 403 (issue AC 2's scoping test; this route
    is scoped like the download routes -- 403, not the detail route's
    non-enumerable 404).
  - This module's capture path has route-level tests; `disposition.py`
    stops being unreachable (issue AC 4).
  - Idempotent update: latest value wins (issue body's explicit spec).
  - The free-text note is never copied into the audit row (Environment
    notes: "never log document text, prompt text, secrets").

DynamoDB is an in-memory fake -- the same update-expression matching
tests/test_disposition_capture_74.py's FakeReviewsTable already
established for `record_disposition`'s write path, extended with a second
fake table for audit rows. No moto/AWS needed: this route never touches S3.

This test MUST FAIL on the pre-fix tree (no POST /api/reviews/{id}/
disposition route exists) and PASS after the fix.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.disposition as disposition_module  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

# ---------------------------------------------------------------------------
# In-memory fakes.
# ---------------------------------------------------------------------------


class FakeReviewsTable:
    """In-memory stand-in for the reviews DynamoDB Table resource -- the
    SAME update-expression matching as
    tests/test_disposition_capture_74.py's FakeReviewsTable, since this
    exercises the identical `disposition.record_disposition` write path,
    now reached through the route rather than called directly."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        item = self.items.get(Key["review_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        self.items[Item["review_id"]] = dict(Item)

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
    ):
        review_id = Key["review_id"]
        item = self.items.setdefault(review_id, dict(Key))
        vals = ExpressionAttributeValues or {}
        if "attorney_disposition = :disposition" in UpdateExpression:
            item["attorney_disposition"] = vals[":disposition"]
            item["attorney_disposition_reason_codes"] = vals[":reason_codes"]
            item["attorney_disposition_topic_ids"] = vals[":topic_ids"]
            item["attorney_disposition_note"] = vals[":note"]
            item["attorney_disposition_recorded_at"] = vals[":recorded_at"]
            item["legal_triage_status"] = vals[":triage_status"]
            item["updated_at"] = vals[":now"]
            return

    def scan(self, **kwargs):  # noqa: ARG002 - unused kwargs accepted for shape parity
        return {"Items": [dict(v) for v in self.items.values()]}


class FakeAuditTable:
    """Append-only stand-in -- only `put_item` is exercised; audit rows are
    never read back through the resource API in this router."""

    def __init__(self):
        self.items: list[dict] = []

    def put_item(self, Item):
        self.items.append(dict(Item))


class FakeDynamoDBResource:
    def __init__(self):
        self.reviews = FakeReviewsTable()
        self.audit = FakeAuditTable()

    def Table(self, name: str):
        if name == os.environ["REVIEWS_TABLE"]:
            return self.reviews
        if name == os.environ["AUDIT_TABLE"]:
            return self.audit
        raise KeyError(f"Unexpected table name in test: {name!r}")


def _caller_row(sub: str, is_admin: bool = False) -> dict:
    return {
        "cognito_sub": sub,
        "email": f"{sub}@example.com",
        "status": "active",
        "is_admin": is_admin,
    }


def _seed_review(table: FakeReviewsTable, review_id: str, owner_sub: str, status_: str) -> None:
    table.items[review_id] = {
        "review_id": review_id,
        "owner_sub": owner_sub,
        "status": status_,
        "decision": "REQUEST_CHANGE",
        "created_at": "1000",
        "updated_at": "1000",
    }


# ---------------------------------------------------------------------------
# Router-mounted sanity check.
# ---------------------------------------------------------------------------


class TestRouteRegistered(unittest.TestCase):
    def test_disposition_route_registered(self):
        registered = {
            (getattr(r, "path", None), method)
            for r in review_routes.router.routes
            for method in getattr(r, "methods", set())
        }
        self.assertIn(("/api/reviews/{review_id}/disposition", "POST"), registered)


# ---------------------------------------------------------------------------
# Shared test base: local FastAPI app mounting the real router.
# ---------------------------------------------------------------------------


class DispositionRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDynamoDBResource()
        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.client = TestClient(self.app)

    def _authenticate_as(self, sub: str, is_admin: bool = False) -> None:
        self.app.dependency_overrides[review_routes.get_active_user_row] = (
            lambda: _caller_row(sub, is_admin=is_admin)
        )


class TestRecordDispositionRoute(DispositionRouteTestBase):
    def test_edited_with_note_updates_detail_and_sets_triage_status(self):
        _seed_review(self.ddb.reviews, "review-1", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-1/disposition",
            json={"disposition": "EDITED", "note": "Narrowed the indemnification carve-out."},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["attorney_disposition"], "EDITED")
        self.assertEqual(body["legal_triage_status"], disposition_module.TRIAGE_STATUS_PENDING)

        # AC: "review detail API carries it."
        detail_resp = self.client.get("/api/reviews/review-1")
        self.assertEqual(detail_resp.status_code, 200)
        detail = detail_resp.json()
        self.assertEqual(detail["attorney_disposition"], "EDITED")
        self.assertEqual(
            detail["attorney_disposition_note"], "Narrowed the indemnification carve-out."
        )
        self.assertEqual(detail["legal_triage_status"], disposition_module.TRIAGE_STATUS_PENDING)

        # disposition.py AC: "does not turn the tool into an approval
        # workflow" -- the pipeline's own verdict is untouched.
        self.assertEqual(detail["decision"], "REQUEST_CHANGE")
        self.assertEqual(detail["status"], "DONE")

    def test_accepted_never_sets_triage_status(self):
        _seed_review(self.ddb.reviews, "review-2", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-2/disposition", json={"disposition": "ACCEPTED"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["legal_triage_status"])

    def test_idempotent_update_latest_wins(self):
        """Issue body: 'Idempotent update (latest wins, prior value kept in
        the audit trail).'"""
        _seed_review(self.ddb.reviews, "review-3", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        first = self.client.post(
            "/api/reviews/review-3/disposition", json={"disposition": "ACCEPTED"}
        )
        second = self.client.post(
            "/api/reviews/review-3/disposition", json={"disposition": "EDITED"}
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["attorney_disposition"], "EDITED")
        detail = self.client.get("/api/reviews/review-3").json()
        self.assertEqual(detail["attorney_disposition"], "EDITED")
        # Both writes reached the audit trail -- the prior value is not lost.
        recorded = [
            row["disposition"]
            for row in self.ddb.audit.items
            if row.get("action") == "review_disposition_recorded"
        ]
        self.assertEqual(recorded, ["ACCEPTED", "EDITED"])

    # -----------------------------------------------------------------
    # Fix-round-2 finding #1: History's change-of-mind call never sends a
    # `note` at all -- an ABSENT `note` key must preserve whatever note is
    # already on the row, not overwrite it with None. The second POST body
    # below is EXACTLY what frontend/src/ReviewHistory.tsx's
    # `recordDisposition(reviewId, outcome)` call sends (no `note`
    # argument at all); frontend/src/disposition.ts's `recordDisposition`
    # only adds a "note" key to the body when a caller passes a non-empty
    # one, so this shape is not hypothetical.
    # -----------------------------------------------------------------

    def test_history_disposition_change_without_note_preserves_prior_note(self):
        _seed_review(self.ddb.reviews, "review-14", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        first = self.client.post(
            "/api/reviews/review-14/disposition",
            json={"disposition": "EDITED", "note": "Narrowed the indemnification carve-out."},
        )
        self.assertEqual(first.status_code, 200)

        # History's EXACT body -- no "note" key present at all.
        second = self.client.post(
            "/api/reviews/review-14/disposition", json={"disposition": "ACCEPTED"}
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["attorney_disposition"], "ACCEPTED")

        detail = self.client.get("/api/reviews/review-14").json()
        self.assertEqual(detail["attorney_disposition"], "ACCEPTED")
        self.assertEqual(
            detail["attorney_disposition_note"],
            "Narrowed the indemnification carve-out.",
        )

    def test_invalid_outcome_400(self):
        _seed_review(self.ddb.reviews, "review-4", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-4/disposition", json={"disposition": "APPROVED"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_disposition_field_400(self):
        _seed_review(self.ddb.reviews, "review-4b", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post("/api/reviews/review-4b/disposition", json={})
        self.assertEqual(resp.status_code, 400)

    def test_not_yet_completed_review_409(self):
        _seed_review(self.ddb.reviews, "review-5", "owner-1", "RUNNING")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-5/disposition", json={"disposition": "ACCEPTED"}
        )
        self.assertEqual(resp.status_code, 409)

    def test_unknown_review_id_404s(self):
        self._authenticate_as("owner-1")
        resp = self.client.post(
            "/api/reviews/does-not-exist/disposition", json={"disposition": "ACCEPTED"}
        )
        self.assertEqual(resp.status_code, 404)

    # -----------------------------------------------------------------
    # Fix-round-1 finding #2: reason_codes/topic_ids/note body validation.
    #
    # docs/data-handling.md classifies reason_codes/topic_ids as
    # "structured codes only ... not document substance" with Indefinite
    # (audit) retention, and the purge sweep deliberately never clears
    # them. These tests pin the bound the route now enforces so that
    # classification stays true of what actually lands in those columns.
    # -----------------------------------------------------------------

    def test_reason_codes_non_string_element_rejected_400(self):
        _seed_review(self.ddb.reviews, "review-7", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-7/disposition",
            json={"disposition": "EDITED", "reason_codes": ["ok", 123]},
        )
        self.assertEqual(resp.status_code, 400)
        # Rejected before any write landed.
        self.assertIsNone(self.ddb.reviews.items["review-7"].get("attorney_disposition"))

    def test_topic_ids_non_string_element_rejected_400(self):
        _seed_review(self.ddb.reviews, "review-8", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-8/disposition",
            json={"disposition": "EDITED", "topic_ids": [None]},
        )
        self.assertEqual(resp.status_code, 400)

    def test_reason_codes_too_many_items_rejected_400(self):
        _seed_review(self.ddb.reviews, "review-9", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        too_many = [f"code-{i}" for i in range(review_routes.MAX_DISPOSITION_LIST_ITEMS + 1)]
        resp = self.client.post(
            "/api/reviews/review-9/disposition",
            json={"disposition": "EDITED", "reason_codes": too_many},
        )
        self.assertEqual(resp.status_code, 400)

    def test_reason_code_element_too_long_rejected_400(self):
        _seed_review(self.ddb.reviews, "review-10", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-10/disposition",
            json={
                "disposition": "EDITED",
                "reason_codes": ["x" * (review_routes.MAX_DISPOSITION_CODE_CHARS + 1)],
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_note_too_long_rejected_400(self):
        _seed_review(self.ddb.reviews, "review-11", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-11/disposition",
            json={
                "disposition": "EDITED",
                "note": "x" * (review_routes.MAX_DISPOSITION_NOTE_CHARS + 1),
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_valid_bounded_reason_codes_and_topic_ids_accepted(self):
        _seed_review(self.ddb.reviews, "review-12", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-12/disposition",
            json={
                "disposition": "EDITED",
                "reason_codes": ["scope-narrowed"],
                "topic_ids": ["indemnification"],
            },
        )
        self.assertEqual(resp.status_code, 200)
        detail = self.client.get("/api/reviews/review-12").json()
        self.assertEqual(detail["attorney_disposition_reason_codes"], ["scope-narrowed"])
        self.assertEqual(detail["attorney_disposition_topic_ids"], ["indemnification"])

    # -----------------------------------------------------------------
    # Fix-round-1 finding #3: the LIST projection, not just the detail
    # route above -- ReviewHistory.tsx reads `GET /api/reviews?scope=mine`,
    # never the detail route, to render its Disposition column.
    # -----------------------------------------------------------------

    def test_list_scope_mine_includes_attorney_disposition(self):
        """Issue #486 AC: 'Include the disposition ... in the review
        detail AND list projections so History can render it.' Deleting
        `attorney_disposition` from `reviews._REVIEW_LIST_ITEM_FIELDS`
        must turn this test RED even though the detail-route tests above
        stay green."""
        _seed_review(self.ddb.reviews, "review-13", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        post = self.client.post(
            "/api/reviews/review-13/disposition", json={"disposition": "EDITED"}
        )
        self.assertEqual(post.status_code, 200)

        listing = self.client.get("/api/reviews?scope=mine")
        self.assertEqual(listing.status_code, 200)
        rows = {row["review_id"]: row for row in listing.json()["reviews"]}
        self.assertIn("review-13", rows)
        self.assertEqual(rows["review-13"]["attorney_disposition"], "EDITED")

    def test_audit_row_written_on_success_without_the_note(self):
        _seed_review(self.ddb.reviews, "review-6", "owner-1", "DONE")
        self._authenticate_as("owner-1")

        resp = self.client.post(
            "/api/reviews/review-6/disposition",
            json={"disposition": "REJECTED", "note": "Confidential document substance."},
        )
        self.assertEqual(resp.status_code, 200)

        recorded = [
            row for row in self.ddb.audit.items if row.get("action") == "review_disposition_recorded"
        ]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["target"], "review-6")
        self.assertEqual(recorded[0]["disposition"], "REJECTED")
        # Environment notes: "never log document text, prompt text,
        # secrets" -- the free-text note must never reach the audit row.
        self.assertNotIn("note", recorded[0])
        for value in recorded[0].values():
            self.assertNotIn("Confidential document substance", str(value))


class TestDispositionScoping(DispositionRouteTestBase):
    """Issue AC: 'Another user's review id -> 403/404 (scoping test)' --
    this route copies the download routes' owner-or-admin 403, per the
    issue body's explicit instruction ('same scoping as the download
    routes')."""

    def setUp(self):
        super().setUp()
        _seed_review(self.ddb.reviews, "review-scoped", "owner-real", "DONE")

    def test_non_owner_gets_403(self):
        self._authenticate_as("attacker")
        resp = self.client.post(
            "/api/reviews/review-scoped/disposition", json={"disposition": "ACCEPTED"}
        )
        self.assertEqual(resp.status_code, 403)
        # And the review itself is genuinely untouched.
        self.assertIsNone(self.ddb.reviews.items["review-scoped"].get("attorney_disposition"))

    def test_owner_gets_200(self):
        self._authenticate_as("owner-real")
        resp = self.client.post(
            "/api/reviews/review-scoped/disposition", json={"disposition": "ACCEPTED"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_admin_can_record_for_someone_elses_review(self):
        self._authenticate_as("admin-user", is_admin=True)
        resp = self.client.post(
            "/api/reviews/review-scoped/disposition", json={"disposition": "ACCEPTED"}
        )
        self.assertEqual(resp.status_code, 200)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRouteRegistered))
    suite.addTests(loader.loadTestsFromTestCase(TestRecordDispositionRoute))
    suite.addTests(loader.loadTestsFromTestCase(TestDispositionScoping))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
