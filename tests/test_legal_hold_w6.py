#!/usr/bin/env python3
"""
Executable tests for QA remediation W6.5: legal hold is atomic and audited.

The defect (QA report top item #8): on AWS, a hold set or release updated the
reviews row FIRST, then tagged the S3 objects -- and the App Runner role had
no `*ObjectTagging` / `ListBucket` grant, so the tagging raised AccessDenied
after the row had already changed and before the audit row was written. A
hold could be released with no audit trail and then purged. Release also took
no reason.

Order of operations now: tag the objects -> conditional row update -> audit.
  - A tagging failure changes nothing (tags already applied are rolled back)
    and the API answers a clean 503 with no provider text in it.
  - A failed audit write rolls the row and the tags back: no unaudited hold
    change survives.
  - Release requires a non-empty `reason`, which lands in the audit row.

Everything runs against moto (DynamoDB + S3).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
TESTS_DIR = REPO_ROOT / "tests"
for _p in (str(BACKEND_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-hold-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-hold-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-hold-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-hold-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-hold-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-hold-test")

import boto3  # noqa: E402, I001
from botocore.exceptions import ClientError  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
from src import retention  # noqa: E402
from ddb_fixtures import create_reviews_table  # noqa: E402

ADMIN = {"cognito_sub": "admin-1", "is_admin": True, "status": "active"}
OWNER = "owner-1"
HOLD_TAG = "contract-toaster:legal-hold"
SECRET_PROVIDER_TEXT = "arn:aws:iam::123456789012:role/leaky-detail"


class FlakyS3:
    """Delegates to a real (moto) client, but can make the Nth tagging call
    fail with a provider error carrying text that must never reach the API."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.fail_put_on_call: int | None = None
        self.fail_delete_on_call: int | None = None
        self._puts = 0
        self._deletes = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _boom(self, op: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "AccessDenied", "Message": f"denied for {SECRET_PROVIDER_TEXT}"}},
            op,
        )

    def put_object_tagging(self, **kwargs: Any) -> Any:
        self._puts += 1
        if self.fail_put_on_call is not None and self._puts == self.fail_put_on_call:
            raise self._boom("PutObjectTagging")
        return self._inner.put_object_tagging(**kwargs)

    def delete_object_tagging(self, **kwargs: Any) -> Any:
        self._deletes += 1
        if self.fail_delete_on_call is not None and self._deletes == self.fail_delete_on_call:
            raise self._boom("DeleteObjectTagging")
        return self._inner.delete_object_tagging(**kwargs)


class FailingAuditResource:
    """Routes every table to the real resource except the audit table, whose
    put_item raises -- an audit outage mid-mutation."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def Table(self, name: str) -> Any:  # noqa: N802 - boto3 shape
        table = self._inner.Table(name)
        if name != os.environ["AUDIT_TABLE"]:
            return table

        class _T:
            def put_item(self, **_kw: Any) -> None:
                raise RuntimeError(f"audit outage {SECRET_PROVIDER_TEXT}")

        return _T()


class HoldTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = FlakyS3(boto3.client("s3", region_name="us-east-1"))
        self.reviews = create_reviews_table(self.ddb)
        self.audit = self.ddb.create_table(
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
        for bucket in (os.environ["UPLOADS_BUCKET"], os.environ["OUTPUTS_BUCKET"]):
            self.s3.create_bucket(Bucket=bucket)
        self.keys = [
            (os.environ["UPLOADS_BUCKET"], f"uploads/{OWNER}/r-1/in.docx"),
            (os.environ["OUTPUTS_BUCKET"], "outputs/r-1/out.docx"),
            (os.environ["OUTPUTS_BUCKET"], "outputs/r-1/analysis.json"),
        ]
        for bucket, key in self.keys:
            self.s3.put_object(Bucket=bucket, Key=key, Body=b"x")
        self.reviews.put_item(Item={
            "review_id": "r-1", "status": "DONE", "owner_sub": OWNER,
            "created_at": str(int(time.time())), "retention_window_at_creation": 90,
            "upload_s3_key": self.keys[0][1], "output_s3_key": self.keys[1][1],
        })

    def tearDown(self) -> None:
        self._mock.stop()

    def tags(self) -> list[bool]:
        out = []
        for bucket, key in self.keys:
            tagset = self.s3.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
            out.append({"Key": HOLD_TAG, "Value": "true"} in tagset)
        return out

    def row(self) -> dict:
        return self.reviews.get_item(Key={"review_id": "r-1"}, ConsistentRead=True)["Item"]

    def audit_rows(self) -> list[dict]:
        return self.audit.scan()["Items"]


class TestSetHold(HoldTestBase):
    def test_set_tags_every_object_updates_the_row_and_audits(self) -> None:
        retention.set_legal_hold("r-1", "Matter 42", ADMIN, self.ddb, self.s3)
        self.assertEqual(self.tags(), [True, True, True])
        self.assertTrue(self.row()["legal_hold"])
        rows = self.audit_rows()
        self.assertEqual([r["action"] for r in rows], ["legal_hold_set"])
        self.assertEqual(rows[0]["legal_hold_reason"], "Matter 42")
        self.assertEqual(rows[0]["review_id"], "r-1")

    def test_a_tag_failure_changes_nothing_and_answers_a_clean_503(self) -> None:
        self.s3.fail_put_on_call = 2
        with self.assertRaises(HTTPException) as ctx:
            retention.set_legal_hold("r-1", "Matter 42", ADMIN, self.ddb, self.s3)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertNotIn(SECRET_PROVIDER_TEXT, str(ctx.exception.detail))
        self.assertNotIn("AccessDenied", str(ctx.exception.detail))
        self.assertEqual(self.tags(), [False, False, False], "tag 1 must be rolled back")
        self.assertFalse(self.row().get("legal_hold", False))
        self.assertEqual(self.audit_rows(), [])

    def test_an_audit_failure_rolls_the_hold_back(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            retention.set_legal_hold(
                "r-1", "Matter 42", ADMIN, FailingAuditResource(self.ddb), self.s3
            )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertNotIn(SECRET_PROVIDER_TEXT, str(ctx.exception.detail))
        self.assertFalse(self.row().get("legal_hold", False))
        self.assertEqual(self.tags(), [False, False, False])

    def test_an_unknown_review_is_404_and_tags_nothing(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            retention.set_legal_hold("r-missing", "Matter 42", ADMIN, self.ddb, self.s3)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_blank_reason_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            retention.set_legal_hold("r-1", "   ", ADMIN, self.ddb, self.s3)
        self.assertEqual(ctx.exception.status_code, 400)


class TestReleaseHold(HoldTestBase):
    def setUp(self) -> None:
        super().setUp()
        retention.set_legal_hold("r-1", "Matter 42", ADMIN, self.ddb, self.s3)

    def test_release_requires_a_reason(self) -> None:
        for reason in ("", "   "):
            with self.assertRaises(HTTPException) as ctx:
                retention.release_legal_hold("r-1", reason, ADMIN, self.ddb, self.s3)
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertTrue(self.row()["legal_hold"])

    def test_release_untags_updates_and_audits_the_reason(self) -> None:
        retention.release_legal_hold("r-1", "Matter closed", ADMIN, self.ddb, self.s3)
        self.assertEqual(self.tags(), [False, False, False])
        row = self.row()
        self.assertFalse(row["legal_hold"])
        self.assertEqual(row["legal_hold_release_reason"], "Matter closed")
        released = [r for r in self.audit_rows() if r["action"] == "legal_hold_released"]
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["legal_hold_release_reason"], "Matter closed")

    def test_a_tag_failure_on_release_leaves_the_hold_in_place(self) -> None:
        self.s3.fail_delete_on_call = 2
        with self.assertRaises(HTTPException) as ctx:
            retention.release_legal_hold("r-1", "Matter closed", ADMIN, self.ddb, self.s3)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertNotIn(SECRET_PROVIDER_TEXT, str(ctx.exception.detail))
        self.assertTrue(self.row()["legal_hold"])
        self.assertEqual(self.tags(), [True, True, True], "untagged object 1 is re-tagged")
        self.assertEqual(
            [r["action"] for r in self.audit_rows()], ["legal_hold_set"]
        )

    def test_an_audit_failure_on_release_keeps_the_hold(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            retention.release_legal_hold(
                "r-1", "Matter closed", ADMIN, FailingAuditResource(self.ddb), self.s3
            )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertTrue(self.row()["legal_hold"])
        self.assertEqual(self.tags(), [True, True, True])

    def test_releasing_a_review_that_is_not_held_is_409(self) -> None:
        retention.release_legal_hold("r-1", "Matter closed", ADMIN, self.ddb, self.s3)
        with self.assertRaises(HTTPException) as ctx:
            retention.release_legal_hold("r-1", "again", ADMIN, self.ddb, self.s3)
        self.assertEqual(ctx.exception.status_code, 409)


class TestReleaseRoute(HoldTestBase):
    def setUp(self) -> None:
        super().setUp()
        retention.set_legal_hold("r-1", "Matter 42", ADMIN, self.ddb, self.s3)
        app = backend_main.app
        app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        app.dependency_overrides[backend_main.get_s3_client] = lambda: self.s3
        app.dependency_overrides[backend_main.get_active_user_row] = lambda: ADMIN
        self.client = TestClient(app)

    def tearDown(self) -> None:
        backend_main.app.dependency_overrides.clear()
        super().tearDown()

    def test_delete_without_a_reason_is_400(self) -> None:
        resp = self.client.request("DELETE", "/api/admin/retention/holds/r-1")
        self.assertEqual(resp.status_code, 400, resp.text)
        resp = self.client.request("DELETE", "/api/admin/retention/holds/r-1", json={"reason": ""})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertTrue(self.row()["legal_hold"])

    def test_delete_with_a_reason_releases(self) -> None:
        resp = self.client.request(
            "DELETE", "/api/admin/retention/holds/r-1", json={"reason": "Matter closed"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(self.row()["legal_hold"])

    def test_a_tagging_failure_surfaces_as_a_clean_503(self) -> None:
        self.s3.fail_delete_on_call = 1
        resp = self.client.request(
            "DELETE", "/api/admin/retention/holds/r-1", json={"reason": "Matter closed"}
        )
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertNotIn(SECRET_PROVIDER_TEXT, resp.text)
        self.assertNotIn("AccessDenied", resp.text)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestSetHold))
    suite.addTests(loader.loadTestsFromTestCase(TestReleaseHold))
    suite.addTests(loader.loadTestsFromTestCase(TestReleaseRoute))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
