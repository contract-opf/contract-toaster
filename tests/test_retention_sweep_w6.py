#!/usr/bin/env python3
"""
Executable tests for the retention sweep fixes in QA remediation W6.

  W6.3  One terminal-status set. `retention.py` used its own copy of the
        terminal statuses, which left out CANCELLED, so on DTS a review the
        user stopped kept its uploaded contract forever. Retention now reads
        `reviews.REVIEW_STATUSES_TERMINAL`, and the purge worker Lambda (which
        cannot import backend/src) is held equal to it here.

Everything runs against moto (DynamoDB + S3), with the reviews table built by
`tests/ddb_fixtures.py::create_reviews_table` so the sweep's `status-index`
reads hit a real index.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
TESTS_DIR = REPO_ROOT / "tests"
PURGE_WORKER = REPO_ROOT / "infra" / "lambda" / "purge_worker" / "handler.py"

for _p in (str(BACKEND_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-w6-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-w6-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-w6-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-w6-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-w6-test")

import boto3  # noqa: E402, I001
from moto import mock_aws  # noqa: E402

from src import retention, reviews  # noqa: E402
from ddb_fixtures import create_reviews_table  # noqa: E402

DAY = 86400
OWNER = "owner-1"


def _load_purge_worker():
    spec = importlib.util.spec_from_file_location("_purge_worker_w6", PURGE_WORKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_simple_table(ddb, name: str, hash_key: str, range_key: str | None = None):
    schema = [{"AttributeName": hash_key, "KeyType": "HASH"}]
    attrs = [{"AttributeName": hash_key, "AttributeType": "S"}]
    if range_key:
        schema.append({"AttributeName": range_key, "KeyType": "RANGE"})
        attrs.append({"AttributeName": range_key, "AttributeType": "S"})
    return ddb.create_table(
        TableName=name, KeySchema=schema, AttributeDefinitions=attrs,
        BillingMode="PAY_PER_REQUEST",
    )


class SweepTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.reviews = create_reviews_table(self.ddb)
        self.settings = _create_simple_table(
            self.ddb, os.environ["RETENTION_SETTINGS_TABLE"], "setting_id"
        )
        self.audit = _create_simple_table(
            self.ddb, os.environ["AUDIT_TABLE"], "partition", "timestamp"
        )
        for bucket in (os.environ["UPLOADS_BUCKET"], os.environ["OUTPUTS_BUCKET"]):
            self.s3.create_bucket(Bucket=bucket)

    def tearDown(self) -> None:
        self._mock.stop()

    def seed(self, review_id: str, status: str, *, age_days: float = 30,
             window_days: int = 1, **extra) -> str:
        key = f"uploads/{OWNER}/{review_id}/in.docx"
        self.s3.put_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=key, Body=b"doc")
        row = {
            "review_id": review_id,
            "status": status,
            "owner_sub": OWNER,
            "created_at": str(int(time.time() - age_days * DAY)),
            "retention_window_at_creation": window_days,
            "upload_s3_key": key,
            "summary": "substance",
            **extra,
        }
        self.reviews.put_item(Item=row)
        return key

    def object_exists(self, key: str) -> bool:
        listing = self.s3.list_objects_v2(Bucket=os.environ["UPLOADS_BUCKET"], Prefix=key)
        return any(o["Key"] == key for o in listing.get("Contents", []))

    def audit_rows(self, action: str | None = None) -> list[dict]:
        rows = self.audit.scan()["Items"]
        return [r for r in rows if action is None or r.get("action") == action]


class TestOneTerminalStatusSet(unittest.TestCase):
    def test_retention_uses_the_reviews_vocabulary(self) -> None:
        self.assertEqual(
            set(retention.TERMINAL_REVIEW_STATUSES), set(reviews.REVIEW_STATUSES_TERMINAL)
        )
        self.assertIn("CANCELLED", retention.TERMINAL_REVIEW_STATUSES)

    def test_the_lambda_copy_equals_the_reviews_vocabulary(self) -> None:
        worker = _load_purge_worker()
        self.assertEqual(
            set(worker.TERMINAL_REVIEW_STATUSES), set(reviews.REVIEW_STATUSES_TERMINAL)
        )

    def test_terminal_and_non_terminal_never_overlap(self) -> None:
        self.assertEqual(
            set(retention.TERMINAL_REVIEW_STATUSES) & reviews.REVIEW_STATUSES_NON_TERMINAL,
            set(),
        )


class TestCancelledIsPurgedOnDts(SweepTestBase):
    def test_a_cancelled_review_past_its_window_is_purged(self) -> None:
        key = self.seed("r-cancelled", "CANCELLED")
        result = retention.run_purge_sweep_now(self.s3, self.ddb)
        self.assertIn("r-cancelled", result["deleted_reviews"])
        self.assertFalse(self.object_exists(key))
        row = self.reviews.get_item(Key={"review_id": "r-cancelled"})["Item"]
        self.assertNotIn("summary", row)
        self.assertIn("purged_at", row)

    def test_the_preview_counts_it_too(self) -> None:
        self.seed("r-cancelled", "CANCELLED")
        preview = retention.preview_purge_sweep(1, {"is_admin": True, "cognito_sub": "a"}, self.ddb)
        self.assertIn("r-cancelled", preview["review_ids"])

    def test_a_running_review_is_still_never_purged(self) -> None:
        key = self.seed("r-running", "RUNNING")
        result = retention.run_purge_sweep_now(self.s3, self.ddb)
        self.assertIn("r-running", result["skipped_active"])
        self.assertTrue(self.object_exists(key))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestOneTerminalStatusSet))
    suite.addTests(loader.loadTestsFromTestCase(TestCancelledIsPurgedOnDts))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
