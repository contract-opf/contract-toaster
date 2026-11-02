#!/usr/bin/env python3
"""
GET /api/admin/retention reads back a stored window instead of 500ing.

## The latent failure this locks fixed

`retention.get_retention_settings` returned the `retention_settings` row
verbatim (`dict(item)`). The window is stored as a number whenever an admin
picks a bounded one (`POST /api/admin/retention` writes the int it was
given), and boto3's DynamoDB resource API hands every stored number back as
`decimal.Decimal` -- which `JSONResponse` cannot encode:

    TypeError: Object of type Decimal is not JSON serializable

So the first bounded window ever saved would take the Retention panel down
on every load, the same outage GET /api/users (#440) and GET
/api/reviews/{id} (2026-10-28) had in production. The #94 suite never saw
it: its retention-settings table is an in-memory fake that stores plain
ints. This drives a REAL (moto) table, which round-trips numbers to
`Decimal` exactly as DynamoDB does, through the shipped route.

Run standalone: `python3 tests/test_retention_settings_decimal.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
# moto needs a region and (fake) credentials for boto3.resource("dynamodb").
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402, I001
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402

ROUTE = "/api/admin/retention"
ADMIN_ROW = {"cognito_sub": "admin-decimal", "status": "active", "is_admin": True}


class TestRetentionSettingsSerialize(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.settings = self.ddb.create_table(
            TableName=os.environ["RETENTION_SETTINGS_TABLE"],
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
        self.app = backend_main.app
        self.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[backend_main.get_active_user_row] = lambda: ADMIN_ROW
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()
        self.mock.stop()

    def test_the_real_table_hands_numbers_back_as_decimal(self) -> None:
        """The precondition: without it this suite would prove nothing."""
        self.settings.put_item(Item={"setting_id": "global", "retention_window_days": 30})
        item = self.settings.get_item(Key={"setting_id": "global"})["Item"]
        self.assertIsInstance(item["retention_window_days"], Decimal)

    def test_a_window_saved_through_the_route_reads_back_as_an_int(self) -> None:
        # A forward-looking change (90 -> 180) applies immediately, single-admin,
        # and is what writes the number in the first place.
        saved = self.client.post(ROUTE, json={"retention_window_days": 180})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "APPLIED")

        resp = self.client.get(ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["retention_window_days"], 180)
        # An integral Decimal must come back an int, not 180.0.
        self.assertIsInstance(body["retention_window_days"], int)

    def test_a_pending_reduction_reads_back_with_plain_numbers(self) -> None:
        # The row shape a retroactive reduction leaves, seeded directly.
        self.settings.put_item(
            Item={
                "setting_id": "global",
                "retention_window_days": 365,
                "pending_reduction": {
                    "new_window_days": 30,
                    "requested_by": "admin-decimal",
                    "requested_at": Decimal("1761696000"),
                },
            }
        )
        resp = self.client.get(ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        pending = resp.json()["pending_reduction"]
        self.assertEqual(pending["new_window_days"], 30)
        self.assertIsInstance(pending["new_window_days"], int)
        self.assertEqual(pending["requested_at"], 1761696000)

    def test_the_forever_sentinel_still_reads_back_as_a_string(self) -> None:
        self.settings.put_item(Item={"setting_id": "global", "retention_window_days": "forever"})
        resp = self.client.get(ROUTE)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["retention_window_days"], "forever")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
