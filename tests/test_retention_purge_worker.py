#!/usr/bin/env python3
"""
Executable tests for issue #61 — retention purge worker
(infra/lambda/purge_worker/handler.py).

These are unit tests against in-memory fakes for DynamoDB and S3 (no live
AWS, no moto/boto3 dependency required) -- same third-party-stubbing
convention as tests/test_orphan_reconciler_e2e.py and
tests/test_review_submission_e2e.py, so the suite runs in CI without extra
installs.

Covers the five purge invariants from docs/data-handling.md ->
"Document retention and purge safety":

  1. Terminal reviews only -- PENDING/RUNNING reviews are never touched,
     even at a 0-day retroactive purge.
  2. Snapshot-at-creation -- a review is purged against its own
     `retention_window_at_creation`, not today's global setting.
  3. Legal hold overrides everything -- a held review's uploads/outputs
     objects and substance fields are never deleted, regardless of age
     or a 0-day window.
  4. Documents, then matched substance fields -- deleting a document also
     clears the Confidential substance fields (`verdict_summary`,
     `issue_rationale_text`) on the matching terminal `reviews` row; the
     non-substantive fields (review_id, status, cost, hashes) remain.
  5. Dual-control / delay for retroactive reductions -- a single-admin
     retroactive reduction is rejected; a valid second-admin confirmation
     or an expired 72h delay is required before the sweep is allowed to
     run at the lowered window.

Also proves the purge worker cannot delete an S3 object carrying the
`contract-toaster:legal-hold=true` tag (storage-layer enforcement backstop, issue #61
AC: "tests prove held S3 objects cannot be deleted by the purge role").

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PURGE_WORKER_DIR = REPO_ROOT / "infra" / "lambda" / "purge_worker"

if str(PURGE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(PURGE_WORKER_DIR))


def _stub_third_party() -> None:
    """Inject minimal stubs for boto3/botocore if absent (handler.py imports
    `boto3` and `botocore.exceptions.ClientError` at module scope)."""
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
                "the test -- tests must monkeypatch handler.boto3 first."
            )

        boto3_mod.resource = _unset
        boto3_mod.client = _unset
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")

import handler as _handler_module  # noqa: E402

ClientError = sys.modules["botocore.exceptions"].ClientError


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------

class FakeTable:
    """Tiny in-memory DynamoDB Table stand-in (same shape/contract as the
    fakes in tests/test_orphan_reconciler_e2e.py)."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}
        # When set, scan() returns at most this many items per call and sets
        # LastEvaluatedKey when more remain -- simulates DynamoDB's ~1MB
        # per-call scan page limit so pagination-handling code can be
        # exercised without a live table.
        self.scan_page_size: int | None = None
        self.scan_calls: list[dict] = []

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ExpressionAttributeNames=None, ConditionExpression=None):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))
        vals = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}

        # REMOVE clause support (substance-field clearing).
        if UpdateExpression.strip().upper().startswith("REMOVE"):
            fields = [f.strip() for f in UpdateExpression[len("REMOVE"):].split(",")]
            for f in fields:
                attr = names.get(f, f)
                item.pop(attr, None)
            return

        # SET clause support (generic key=value assignment interpreter,
        # good enough for the small set of expressions handler.py issues).
        set_part = UpdateExpression.split("SET", 1)[-1]
        assignments = [a.strip() for a in set_part.split(",")]
        for assignment in assignments:
            if "=" not in assignment:
                continue
            lhs, rhs = [p.strip() for p in assignment.split("=", 1)]
            attr = names.get(lhs, lhs)
            rhs_key = rhs.strip()
            if rhs_key in vals:
                item[attr] = vals[rhs_key]

    def scan(self, FilterExpression=None, ExpressionAttributeNames=None,
              ExpressionAttributeValues=None, ExclusiveStartKey=None):
        self.scan_calls.append({"ExclusiveStartKey": ExclusiveStartKey})
        all_keys = list(self.items.keys())

        if self.scan_page_size is None:
            return {"Items": [dict(self.items[k]) for k in all_keys]}

        start_index = 0
        if ExclusiveStartKey is not None:
            start_key = ExclusiveStartKey[self.key_name]
            start_index = all_keys.index(start_key) + 1

        page_keys = all_keys[start_index:start_index + self.scan_page_size]
        page = {"Items": [dict(self.items[k]) for k in page_keys]}

        next_index = start_index + self.scan_page_size
        if next_index < len(all_keys):
            # LastEvaluatedKey is the key of the LAST item actually
            # returned in this page (real DynamoDB semantics) -- the next
            # call's ExclusiveStartKey resumes immediately after it.
            last_returned_key = page_keys[-1]
            page["LastEvaluatedKey"] = {self.key_name: last_returned_key}

        return page


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            key_name = {
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ["RETENTION_SETTINGS_TABLE"]: "setting_id",
            }.get(name, "id")
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class FakeS3Client:
    """Fake S3 client that honors the legal-hold tag DENY semantics of the
    real bucket policy: delete_object raises AccessDenied when the object
    carries contract-toaster:legal-hold=true, exactly like data-stack.ts
    _addLegalHoldPolicy's bucket-policy DENY does against a real S3 bucket."""

    def __init__(self):
        # key -> {"tags": {...}, "deleted": bool}
        self.objects: dict[tuple[str, str], dict] = {}
        self.delete_attempts: list[tuple[str, str]] = []

    def put_test_object(self, bucket: str, key: str, tags: dict | None = None):
        self.objects[(bucket, key)] = {"tags": tags or {}, "deleted": False}

    def get_object_tagging(self, Bucket, Key):
        obj = self.objects.get((Bucket, Key))
        tags = obj["tags"] if obj else {}
        return {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}

    def list_objects_v2(self, Bucket, Prefix):
        contents = [
            {"Key": key}
            for (bucket, key) in self.objects
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": contents} if contents else {}

    def delete_object(self, Bucket, Key):
        self.delete_attempts.append((Bucket, Key))
        obj = self.objects.get((Bucket, Key))
        if obj and obj["tags"].get("contract-toaster:legal-hold") == "true":
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": "Denied by bucket policy: contract-toaster:legal-hold=true",
                    }
                },
                "DeleteObject",
            )
        if obj:
            obj["deleted"] = True
        return {}


class Boto3Stub:
    def __init__(self, ddb: FakeDynamoDBResource, s3: FakeS3Client):
        self._ddb = ddb
        self._s3 = s3

    def resource(self, service_name):
        assert service_name == "dynamodb"
        return self._ddb

    def client(self, service_name):
        assert service_name == "s3"
        return self._s3


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class PurgeWorkerTestCase(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDynamoDBResource()
        self.s3 = FakeS3Client()
        self._orig_boto3 = _handler_module.boto3
        _handler_module.boto3 = Boto3Stub(self.ddb, self.s3)
        self._seed_default_settings()

    def tearDown(self):
        _handler_module.boto3 = self._orig_boto3

    def _seed_default_settings(self, window_days: int = 90):
        table = self.ddb.Table(os.environ["RETENTION_SETTINGS_TABLE"])
        table.items["global"] = {
            "setting_id": "global",
            "retention_window_days": window_days,
            "pending_reduction": None,
        }

    def _seed_review(self, review_id: str, *, status: str, age_days: int,
                      retention_window_at_creation: int = 90,
                      legal_hold: bool = False, has_upload: bool = True,
                      has_output: bool = True) -> None:
        now = _handler_module.now_epoch()
        created_at = now - age_days * 86400
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        reviews_table.items[review_id] = {
            "review_id": review_id,
            "status": status,
            "created_at": str(int(created_at)),
            "retention_window_at_creation": retention_window_at_creation,
            "legal_hold": legal_hold,
            "verdict_summary": "some substantive summary",
            "issue_rationale_text": "some substantive rationale",
            "owner_sub": "user-1",
        }
        if has_upload:
            self.s3.put_test_object(
                os.environ["UPLOADS_BUCKET"],
                f"uploads/{review_id}/contract.docx",
                tags={"contract-toaster:legal-hold": "true" if legal_hold else "false"},
            )
        if has_output:
            self.s3.put_test_object(
                os.environ["OUTPUTS_BUCKET"],
                f"outputs/{review_id}/redline.docx",
                tags={"contract-toaster:legal-hold": "true" if legal_hold else "false"},
            )

    # -- Invariant 1: terminal reviews only -----------------------------

    def test_pending_review_never_purged_even_at_zero_day_window(self):
        self._seed_default_settings(window_days=0)
        self._seed_review("review-pending", status="PENDING", age_days=1000,
                           retention_window_at_creation=0)

        result = _handler_module.run_purge_sweep()

        self.assertNotIn("review-pending", result["deleted_reviews"])
        self.assertIn("review-pending", result["skipped_active"])
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.assertEqual(reviews_table.items["review-pending"]["status"], "PENDING")
        self.assertTrue(
            self.s3.objects[(os.environ["UPLOADS_BUCKET"], "uploads/review-pending/contract.docx")]["deleted"] is False
        )

    def test_running_review_never_purged(self):
        self._seed_default_settings(window_days=0)
        self._seed_review("review-running", status="RUNNING", age_days=1000,
                           retention_window_at_creation=0)

        result = _handler_module.run_purge_sweep()

        self.assertIn("review-running", result["skipped_active"])

    # -- Invariant 2: snapshot-at-creation --------------------------------

    def test_purge_uses_snapshotted_window_not_todays_global_setting(self):
        # Global setting is now 90 days, but this review snapshotted 30 days
        # at creation and is 45 days old -- it must purge on ITS window (30),
        # not today's global window (90).
        self._seed_default_settings(window_days=90)
        self._seed_review("review-snapshot", status="DONE", age_days=45,
                           retention_window_at_creation=30)

        result = _handler_module.run_purge_sweep()

        self.assertIn("review-snapshot", result["deleted_reviews"])

    def test_review_not_yet_past_its_own_snapshotted_window_is_kept(self):
        self._seed_default_settings(window_days=0)
        self._seed_review("review-fresh", status="DONE", age_days=5,
                           retention_window_at_creation=90)

        result = _handler_module.run_purge_sweep()

        self.assertNotIn("review-fresh", result["deleted_reviews"])

    # -- Invariant 3: legal hold overrides everything ---------------------

    def test_legal_hold_review_never_purged_even_at_zero_day_retroactive(self):
        self._seed_default_settings(window_days=0)
        self._seed_review("review-held", status="DONE", age_days=1000,
                           retention_window_at_creation=0, legal_hold=True)

        result = _handler_module.run_purge_sweep()

        self.assertNotIn("review-held", result["deleted_reviews"])
        self.assertIn("review-held", result["skipped_hold"])
        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.assertEqual(
            reviews_table.items["review-held"]["verdict_summary"],
            "some substantive summary",
            "Substance fields on a held review must not be cleared.",
        )
        self.assertFalse(
            self.s3.objects[(os.environ["UPLOADS_BUCKET"], "uploads/review-held/contract.docx")]["deleted"]
        )
        self.assertFalse(
            self.s3.objects[(os.environ["OUTPUTS_BUCKET"], "outputs/review-held/redline.docx")]["deleted"]
        )

    def test_purge_role_delete_attempt_on_held_object_is_denied_by_storage_layer(self):
        """Storage-layer backstop (issue #61 AC): even if application logic
        somehow attempted the delete, the (simulated) bucket-policy DENY on
        the contract-toaster:legal-hold=true tag blocks it. Proves the purge role cannot
        delete a held object -- not just that the app chooses not to try."""
        self.s3.put_test_object(
            os.environ["UPLOADS_BUCKET"], "uploads/review-x/contract.docx",
            tags={"contract-toaster:legal-hold": "true"},
        )
        with self.assertRaises(ClientError) as ctx:
            self.s3.delete_object(
                Bucket=os.environ["UPLOADS_BUCKET"],
                Key="uploads/review-x/contract.docx",
            )
        self.assertEqual(ctx.exception.response["Error"]["Code"], "AccessDenied")
        self.assertFalse(
            self.s3.objects[(os.environ["UPLOADS_BUCKET"], "uploads/review-x/contract.docx")]["deleted"]
        )

    # -- Invariant 4: documents, then matched substance fields ------------

    def test_purged_review_clears_substance_fields_but_keeps_non_substantive_row(self):
        self._seed_default_settings(window_days=0)
        self._seed_review("review-clear", status="DONE", age_days=1000,
                           retention_window_at_creation=0)

        _handler_module.run_purge_sweep()

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        row = reviews_table.items["review-clear"]
        self.assertNotIn("verdict_summary", row)
        self.assertNotIn("issue_rationale_text", row)
        # Non-substantive audit-bearing fields remain.
        self.assertEqual(row["review_id"], "review-clear")
        self.assertEqual(row["status"], "DONE")
        self.assertEqual(row["owner_sub"], "user-1")

    def test_purged_review_documents_actually_deleted_from_s3(self):
        self._seed_default_settings(window_days=0)
        self._seed_review("review-del", status="DONE", age_days=1000,
                           retention_window_at_creation=0)

        _handler_module.run_purge_sweep()

        self.assertTrue(
            self.s3.objects[(os.environ["UPLOADS_BUCKET"], "uploads/review-del/contract.docx")]["deleted"]
        )
        self.assertTrue(
            self.s3.objects[(os.environ["OUTPUTS_BUCKET"], "outputs/review-del/redline.docx")]["deleted"]
        )

    # -- Pagination: reviews_table.scan() must follow LastEvaluatedKey ----

    def test_purge_sweep_paginates_through_all_scan_pages(self):
        """issue #170: a DynamoDB scan() only returns up to ~1MB per call
        and sets LastEvaluatedKey when more items remain. run_purge_sweep()
        must keep calling scan(ExclusiveStartKey=...) until the table is
        exhausted -- otherwise reviews past the first page are silently
        never evaluated for purge eligibility, ever."""
        self._seed_default_settings(window_days=0)
        # 5 eligible reviews, forced into pages of 2 -- 3 scan() calls
        # required (2 + 2 + 1) to see every row.
        review_ids = [f"review-page-{i}" for i in range(5)]
        for review_id in review_ids:
            self._seed_review(review_id, status="DONE", age_days=1000,
                               retention_window_at_creation=0)

        reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        reviews_table.scan_page_size = 2

        result = _handler_module.run_purge_sweep()

        self.assertEqual(
            sorted(result["deleted_reviews"]), sorted(review_ids),
            "Every review across every scan page must be purged, not just "
            "the first page's.",
        )
        self.assertGreaterEqual(
            len(reviews_table.scan_calls), 3,
            "Expected scan() to be called once per page (3 pages of size "
            "2 over 5 items) -- got fewer calls, so pagination did not run.",
        )
        # Every call after the first must carry forward the prior page's
        # LastEvaluatedKey.
        self.assertIsNone(reviews_table.scan_calls[0]["ExclusiveStartKey"])
        for call in reviews_table.scan_calls[1:]:
            self.assertIsNotNone(call["ExclusiveStartKey"])
        # All 5 S3 upload objects (one per review) must actually be deleted
        # -- proves the tail-page reviews were purged for real, not just
        # counted.
        for review_id in review_ids:
            self.assertTrue(
                self.s3.objects[(os.environ["UPLOADS_BUCKET"],
                                  f"uploads/{review_id}/contract.docx")]["deleted"],
                f"{review_id}'s upload was never deleted -- likely on a "
                "scan page that was never fetched.",
            )

    # -- Invariant 5: dual-control / delay for retroactive reductions -----

    def test_single_admin_retroactive_reduction_is_rejected(self):
        self._seed_default_settings(window_days=90)

        result = _handler_module.request_retention_change(
            new_window_days=30,
            actor="admin-1",
            second_admin_confirmation=None,
        )

        self.assertEqual(result["status"], "PENDING_SECOND_APPROVAL")
        self.assertFalse(result["applied_immediately"])
        settings_table = self.ddb.Table(os.environ["RETENTION_SETTINGS_TABLE"])
        # Window must NOT have moved yet.
        self.assertEqual(settings_table.items["global"]["retention_window_days"], 90)

    def test_forward_looking_increase_applies_single_admin_immediately(self):
        self._seed_default_settings(window_days=30)

        result = _handler_module.request_retention_change(
            new_window_days=90,
            actor="admin-1",
            second_admin_confirmation=None,
        )

        self.assertEqual(result["status"], "APPLIED")
        self.assertTrue(result["applied_immediately"])
        settings_table = self.ddb.Table(os.environ["RETENTION_SETTINGS_TABLE"])
        self.assertEqual(settings_table.items["global"]["retention_window_days"], 90)

    def test_retroactive_reduction_with_second_admin_confirmation_applies(self):
        self._seed_default_settings(window_days=90)

        result = _handler_module.request_retention_change(
            new_window_days=30,
            actor="admin-1",
            second_admin_confirmation={"actor": "admin-2", "reason": "quarterly cleanup"},
        )

        self.assertEqual(result["status"], "APPLIED")
        settings_table = self.ddb.Table(os.environ["RETENTION_SETTINGS_TABLE"])
        self.assertEqual(settings_table.items["global"]["retention_window_days"], 30)

    def test_retroactive_reduction_same_admin_confirming_itself_is_rejected(self):
        """A confirmation must come from a DIFFERENT admin than the requester,
        or dual-control is trivially bypassable by one compromised session."""
        self._seed_default_settings(window_days=90)

        result = _handler_module.request_retention_change(
            new_window_days=30,
            actor="admin-1",
            second_admin_confirmation={"actor": "admin-1", "reason": "self-confirm"},
        )

        self.assertEqual(result["status"], "PENDING_SECOND_APPROVAL")
        settings_table = self.ddb.Table(os.environ["RETENTION_SETTINGS_TABLE"])
        self.assertEqual(settings_table.items["global"]["retention_window_days"], 90)

    def test_pending_reduction_sweep_blocked_before_delay_expires(self):
        self._seed_default_settings(window_days=90)
        _handler_module.request_retention_change(
            new_window_days=0, actor="admin-1", second_admin_confirmation=None,
        )
        settings_table = self.ddb.Table(os.environ["RETENTION_SETTINGS_TABLE"])
        pending = settings_table.items["global"]["pending_reduction"]
        self.assertIsNotNone(pending)
        # Simulate "just now" -- delay has not elapsed.
        pending["requested_at"] = _handler_module.now_epoch()

        allowed = _handler_module.is_pending_reduction_ready(pending)
        self.assertFalse(allowed)

    def test_pending_reduction_sweep_allowed_after_72h_delay_elapses(self):
        self._seed_default_settings(window_days=90)
        _handler_module.request_retention_change(
            new_window_days=0, actor="admin-1", second_admin_confirmation=None,
        )
        settings_table = self.ddb.Table(os.environ["RETENTION_SETTINGS_TABLE"])
        pending = settings_table.items["global"]["pending_reduction"]
        pending["requested_at"] = _handler_module.now_epoch() - (73 * 3600)

        allowed = _handler_module.is_pending_reduction_ready(pending)
        self.assertTrue(allowed)


if __name__ == "__main__":
    result = unittest.main(exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
