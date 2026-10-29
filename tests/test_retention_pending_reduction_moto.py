#!/usr/bin/env python3
"""
A retroactive retention reduction parks, confirms and expires on a real table.

## The latent failures this locks fixed

`retention.request_retention_change` parked a single-admin retroactive
reduction (say 365 -> 30 days) as

    pending_reduction = {..., "requested_at": time.time()}

-- a Python `float`. boto3's DynamoDB resource API refuses floats outright
on every put/update:

    TypeError: Float types are not supported. Use Decimal types instead.

so `POST /api/admin/retention` 500ed on the one path that exists to make a
lone admin wait out the 72-hour dual-control delay. The purge worker's
mirror (`infra/lambda/purge_worker/handler.py::request_retention_change`)
wrote the same float.

The reader had the inverse bug. boto3 hands every stored number back as
`decimal.Decimal`, and the worker's `is_pending_reduction_ready` computed
`now_epoch() - requested_at` -- `float - Decimal`, which Python refuses:

    TypeError: unsupported operand type(s) for -: 'float' and 'decimal.Decimal'

so even a correctly stored pending reduction would have crashed every
sweep that met it, and the delayed reduction would never have applied.

The #94 and #61 suites never saw either: their tables are in-memory fakes
that store whatever Python object they are handed. This drives a REAL
(moto) table, which enforces both boto3 type rules exactly as DynamoDB
does, through the shipped route and the shipped worker functions.

Run standalone: `python3 tests/test_retention_pending_reduction_moto.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
PURGE_WORKER_HANDLER = REPO_ROOT / "infra" / "lambda" / "purge_worker" / "handler.py"

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


def _load_purge_worker():
    """Load the worker under a name of its own. Every infra/lambda bundle
    ships a module called `handler` (tests/README.md -> "The `handler`
    collision"), so a bare `import handler` could bind another bundle's."""
    spec = importlib.util.spec_from_file_location("purge_worker_handler_moto", PURGE_WORKER_HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


purge_worker = _load_purge_worker()

ROUTE = "/api/admin/retention"
ADMIN_A = {"cognito_sub": "admin-a", "status": "active", "is_admin": True}
ADMIN_B = {"cognito_sub": "admin-b", "status": "active", "is_admin": True}
DELAY = purge_worker.RETROACTIVE_REDUCTION_DELAY_SECONDS


class RealTableTestCase(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.mock.stop()

    def _row(self) -> dict:
        return self.settings.get_item(Key={"setting_id": "global"})["Item"]


class TestTheRealTableEnforcesBothTypeRules(RealTableTestCase):
    """The preconditions: without them this suite would prove nothing."""

    def test_a_float_is_refused_on_write(self) -> None:
        with self.assertRaisesRegex(TypeError, "Float types are not supported"):
            self.settings.put_item(Item={"setting_id": "global", "requested_at": 1761696000.5})

    def test_a_number_comes_back_as_decimal(self) -> None:
        self.settings.put_item(Item={"setting_id": "global", "requested_at": 1761696000})
        self.assertIsInstance(self._row()["requested_at"], Decimal)


class TestRouteParksAndConfirmsAReduction(RealTableTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = backend_main.app
        self.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.caller = ADMIN_A
        self.app.dependency_overrides[backend_main.get_active_user_row] = lambda: self.caller
        self.client = TestClient(self.app)
        # Forward-looking (90 -> 365): applies immediately, single-admin.
        raised = self.client.post(ROUTE, json={"retention_window_days": 365})
        self.assertEqual(raised.status_code, 200, raised.text)
        self.assertEqual(raised.json()["status"], "APPLIED")

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()
        super().tearDown()

    def _request_reduction(self) -> None:
        resp = self.client.post(ROUTE, json={"retention_window_days": 30})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "PENDING_SECOND_APPROVAL")

    def test_a_lone_admin_reduction_is_parked_not_500ed(self) -> None:
        self._request_reduction()

        row = self._row()
        self.assertEqual(row["retention_window_days"], 365, "the window must not drop yet")
        pending = row["pending_reduction"]
        self.assertEqual(pending["new_window_days"], 30)
        self.assertEqual(pending["requested_by"], "admin-a")
        # Stored as a whole epoch second -- the 72h delay needs no finer grain.
        self.assertEqual(pending["requested_at"] % 1, 0)

        body = self.client.get(ROUTE).json()
        self.assertEqual(body["retention_window_days"], 365)
        self.assertIsInstance(body["pending_reduction"]["requested_at"], int)

    def test_a_second_admin_confirms_the_parked_reduction(self) -> None:
        self._request_reduction()

        self.caller = ADMIN_B
        resp = self.client.post(
            ROUTE,
            json={"retention_window_days": 30, "second_admin_confirmation": {"actor": "admin-a"}},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"status": "APPLIED", "applied_immediately": False})

        row = self._row()
        self.assertEqual(row["retention_window_days"], 30)
        self.assertIsNone(row["pending_reduction"])

    def test_a_self_confirmation_stays_parked(self) -> None:
        resp = self.client.post(
            ROUTE,
            json={"retention_window_days": 30, "second_admin_confirmation": {"actor": "admin-a"}},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "PENDING_SECOND_APPROVAL")
        self.assertEqual(self._row()["retention_window_days"], 365)


class TestPurgeWorkerExpiresAStoredReduction(RealTableTestCase):
    def _park(self, requested_at: int) -> None:
        self.settings.put_item(
            Item={
                "setting_id": "global",
                "retention_window_days": 365,
                "pending_reduction": {
                    "new_window_days": 30,
                    "requested_by": "admin-a",
                    "requested_at": requested_at,
                },
            }
        )

    def test_a_reduction_inside_the_delay_is_not_applied(self) -> None:
        self._park(int(purge_worker.now_epoch()))
        settings = purge_worker._apply_ready_pending_reduction(purge_worker.get_retention_settings())
        self.assertEqual(settings["retention_window_days"], 365)
        self.assertEqual(self._row()["retention_window_days"], 365)

    def test_a_reduction_past_the_delay_is_applied(self) -> None:
        self._park(int(purge_worker.now_epoch()) - DELAY - 3600)
        settings = purge_worker._apply_ready_pending_reduction(purge_worker.get_retention_settings())
        self.assertEqual(settings["retention_window_days"], 30)
        row = self._row()
        self.assertEqual(row["retention_window_days"], 30)
        self.assertIsNone(row["pending_reduction"])

    def test_the_workers_own_request_parks_on_a_real_table(self) -> None:
        self.settings.put_item(Item={"setting_id": "global", "retention_window_days": 365})
        result = purge_worker.request_retention_change(new_window_days=30, actor="admin-a")
        self.assertEqual(result["status"], "PENDING_SECOND_APPROVAL")
        pending = self._row()["pending_reduction"]
        self.assertFalse(purge_worker.is_pending_reduction_ready(pending))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
