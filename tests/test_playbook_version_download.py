#!/usr/bin/env python3
"""
Tests for the playbook-version download endpoint:
GET /api/admin/playbooks/{playbook_id}/versions/{version}/download.

Verifies:
  1. Route exists and is admin-gated (HTTP 403 for non-admin).
  2. 404 for unknown (playbook_id, version) or version without a storage_key.
  3. 403 for invalid/unscoped storage keys (e.g. traversal or wrong prefix).
  4. 410 for purged/missing S3 artifacts (HEAD check).
  5. 200 on success returning a 60-second presigned URL with Cache-Control: no-store
     and attachment Content-Disposition.
  6. Writing of the immutable playbook_version_downloaded audit entry.
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-download-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-download-test"
)
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-download-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-download-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402

ADMIN_SUB = "admin-user-1"
NON_ADMIN_SUB = "regular-user-1"
PLAYBOOK_ID = "generic-playbook"
VERSION = "v1.0.0"


def _put_user(table, sub: str, is_admin: bool, status_: str = "active") -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": status_,
            "is_admin": is_admin,
        }
    )


class TestPlaybookVersionDownload(unittest.TestCase):
    def setUp(self):
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

        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])

        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
        self.audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])

        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[backend_main.get_s3_client] = lambda: self.s3

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": f"{sub}@example.com", "token_use": "access"}
        )

    def _download_url(self, playbook_id: str = PLAYBOOK_ID, version: str = VERSION) -> str:
        return f"/api/admin/playbooks/{playbook_id}/versions/{version}/download"

    def test_non_admin_forbidden(self):
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self.client.get(self._download_url())
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Admin privilege required", resp.json().get("detail", ""))

    def test_version_not_found_returns_404(self):
        self._authenticate_as(ADMIN_SUB)
        resp = self.client.get(self._download_url(PLAYBOOK_ID, "v9.9.9"))
        self.assertEqual(resp.status_code, 404)
        self.assertIn("not found", resp.json().get("detail", ""))

    def test_version_without_storage_key_returns_404(self):
        self._authenticate_as(ADMIN_SUB)
        self.versions_table.put_item(
            Item={
                "playbook_id": PLAYBOOK_ID,
                "version": VERSION,
                "status": "draft",
                "uploaded_by": ADMIN_SUB,
                "uploaded_at": 1700000000,
            }
        )
        resp = self.client.get(self._download_url())
        self.assertEqual(resp.status_code, 404)
        self.assertIn("no stored artifact", resp.json().get("detail", ""))

    def test_unscoped_storage_key_returns_403(self):
        self._authenticate_as(ADMIN_SUB)
        self.versions_table.put_item(
            Item={
                "playbook_id": PLAYBOOK_ID,
                "version": VERSION,
                "status": "draft",
                "uploaded_by": ADMIN_SUB,
                "uploaded_at": 1700000000,
                "storage_key": "playbooks/other-playbook/bundle.json",
            }
        )
        resp = self.client.get(self._download_url())
        self.assertEqual(resp.status_code, 403)
        self.assertIn("not scoped", resp.json().get("detail", ""))

    def test_purged_s3_object_returns_410(self):
        self._authenticate_as(ADMIN_SUB)
        storage_key = f"playbooks/{PLAYBOOK_ID}/bundle-v1.json"
        self.versions_table.put_item(
            Item={
                "playbook_id": PLAYBOOK_ID,
                "version": VERSION,
                "status": "active",
                "uploaded_by": ADMIN_SUB,
                "uploaded_at": 1700000000,
                "storage_key": storage_key,
            }
        )
        # S3 object is NOT put, so HEAD will fail
        resp = self.client.get(self._download_url())
        self.assertEqual(resp.status_code, 410)
        self.assertIn("no longer available", resp.json().get("detail", ""))

    def test_successful_download_url_generation_and_audit(self):
        self._authenticate_as(ADMIN_SUB)
        storage_key = f"playbooks/{PLAYBOOK_ID}/artifact-hash.json"
        artifact_bytes = json.dumps({"schema_version": "opf-0.3", "sections": []}).encode("utf-8")

        self.s3.put_object(
            Bucket=os.environ["UPLOADS_BUCKET"],
            Key=storage_key,
            Body=artifact_bytes,
        )
        self.versions_table.put_item(
            Item={
                "playbook_id": PLAYBOOK_ID,
                "version": VERSION,
                "status": "active",
                "uploaded_by": ADMIN_SUB,
                "uploaded_at": 1700000000,
                "storage_key": storage_key,
            }
        )

        resp = self.client.get(self._download_url())
        self.assertEqual(resp.status_code, 200)

        # Cache-Control: no-store check
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

        data = resp.json()
        self.assertEqual(data.get("playbook_id"), PLAYBOOK_ID)
        self.assertEqual(data.get("version"), VERSION)
        self.assertEqual(data.get("expires_in"), 60)
        url = data.get("url", "")
        self.assertTrue(url.startswith("https://") or url.startswith("http://"))
        self.assertIn(storage_key, url)
        self.assertIn("response-content-disposition", url.lower())
        self.assertIn(f"{PLAYBOOK_ID}-v{VERSION}.json", url)

        # Audit entry check
        audit_items = self.audit_table.scan().get("Items", [])
        self.assertEqual(len(audit_items), 1)
        audit = audit_items[0]
        self.assertEqual(audit.get("actor"), ADMIN_SUB)
        self.assertEqual(audit.get("action"), "playbook_version_downloaded")
        self.assertEqual(audit.get("target"), f"{PLAYBOOK_ID}#{VERSION}")
        self.assertEqual(audit.get("target_type"), "playbook_version")
        self.assertEqual(audit.get("outcome"), "success")
        self.assertEqual(audit.get("storage_key"), storage_key)
        self.assertEqual(audit.get("playbook_id"), PLAYBOOK_ID)
        self.assertEqual(audit.get("version"), VERSION)


if __name__ == "__main__":
    unittest.main()
