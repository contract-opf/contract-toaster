#!/usr/bin/env python3
"""
Executable tests for QA remediation W6.7: real dual control for retroactive
retention reductions.

The defect (QA report top item #7): the "second admin" was a free-text
`second_admin_confirmation.actor` string in the SAME request, so one admin
could type any other name and apply a 0-day window immediately.

Now:
  - A reduction always creates a PENDING record (with a required reason);
    nothing is lowered by the request itself.
  - Only a DIFFERENT authenticated admin (strict `is_admin is True`, via
    authz) can confirm it, through
    `POST /api/admin/retention/pending/{pending_id}/confirm`; the requester
    cannot, and neither can a caller whose flag is merely truthy.
  - Any admin can cancel it (`.../cancel`).
  - Otherwise it applies itself after 72 hours -- on DTS from the purge
    sweep (`retention.apply_ready_pending_reduction`), as the Lambda already
    did on AWS. Every step is audited.
  - The old `second_admin_confirmation` body field is refused outright.

Everything runs against moto through the shipped routes.

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
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-dc-test")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-dc-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-dc-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-dc-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-dc-test")

import boto3  # noqa: E402, I001
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
from src import retention  # noqa: E402
from ddb_fixtures import create_reviews_table  # noqa: E402

ROUTE = "/api/admin/retention"
ADMIN_A = {"cognito_sub": "admin-a", "status": "active", "is_admin": True}
ADMIN_B = {"cognito_sub": "admin-b", "status": "active", "is_admin": True}
TRUTHY_B = {"cognito_sub": "admin-b", "status": "active", "is_admin": "true"}
REVIEWER = {"cognito_sub": "rev-1", "status": "active", "is_admin": False}
DELAY = 72 * 3600


def _load_purge_worker():
    spec = importlib.util.spec_from_file_location("_purge_worker_dc", PURGE_WORKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DualControlBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in (os.environ["UPLOADS_BUCKET"], os.environ["OUTPUTS_BUCKET"]):
            self.s3.create_bucket(Bucket=bucket)
        create_reviews_table(self.ddb)
        self.settings = self.ddb.create_table(
            TableName=os.environ["RETENTION_SETTINGS_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
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
        self.settings.put_item(Item={"setting_id": "global", "retention_window_days": 365})
        self.app = backend_main.app
        self.caller = ADMIN_A
        self.app.dependency_overrides[backend_main.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[backend_main.get_s3_client] = lambda: self.s3
        self.app.dependency_overrides[backend_main.get_active_user_row] = lambda: self.caller
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()
        self._mock.stop()

    def row(self) -> dict:
        return self.settings.get_item(Key={"setting_id": "global"}, ConsistentRead=True)["Item"]

    def actions(self) -> list[str]:
        return sorted(r["action"] for r in self.audit.scan()["Items"])

    def audit_row(self, action: str) -> dict:
        rows = [r for r in self.audit.scan()["Items"] if r["action"] == action]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def request_reduction(self, days: int = 30, reason: str = "Quarterly cleanup") -> dict:
        resp = self.client.post(ROUTE, json={"retention_window_days": days, "reason": reason})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "PENDING_SECOND_APPROVAL")
        return body

    def confirm(self, pending_id: str):
        return self.client.post(f"{ROUTE}/pending/{pending_id}/confirm")


class TestRequestingAReduction(DualControlBase):
    def test_a_reduction_is_parked_with_its_reason_and_lowers_nothing(self) -> None:
        body = self.request_reduction()
        row = self.row()
        self.assertEqual(int(row["retention_window_days"]), 365)
        pending = row["pending_reduction"]
        self.assertEqual(pending["pending_id"], body["pending_id"])
        self.assertEqual(pending["requested_by"], "admin-a")
        self.assertEqual(pending["reason"], "Quarterly cleanup")
        self.assertEqual(int(pending["apply_after"]) - int(pending["requested_at"]), DELAY)
        requested = self.audit_row("retention_reduction_requested")
        self.assertEqual(requested["actor"], "admin-a")
        self.assertEqual(requested["pending_id"], body["pending_id"])
        self.assertEqual(requested["reason"], "Quarterly cleanup")

    def test_a_reduction_needs_a_reason(self) -> None:
        for body in ({"retention_window_days": 30}, {"retention_window_days": 30, "reason": "  "}):
            resp = self.client.post(ROUTE, json=body)
            self.assertEqual(resp.status_code, 400, resp.text)
        self.assertNotIn("pending_reduction", {k for k, v in self.row().items() if v})

    def test_the_free_text_second_admin_field_is_refused(self) -> None:
        resp = self.client.post(ROUTE, json={
            "retention_window_days": 0, "reason": "x",
            "second_admin_confirmation": {"actor": "admin-b"},
        })
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(int(self.row()["retention_window_days"]), 365)

    def test_a_second_reduction_while_one_is_pending_is_409(self) -> None:
        self.request_reduction()
        resp = self.client.post(ROUTE, json={"retention_window_days": 0, "reason": "again"})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(int(self.row()["pending_reduction"]["new_window_days"]), 30)

    def test_forward_changes_still_apply_immediately(self) -> None:
        resp = self.client.post(ROUTE, json={"retention_window_days": 1095})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "APPLIED")
        self.assertEqual(int(self.row()["retention_window_days"]), 1095)

    def test_get_tells_each_viewer_whether_they_may_confirm(self) -> None:
        self.request_reduction()
        mine = self.client.get(ROUTE).json()["pending_reduction"]
        self.assertFalse(mine["viewer_can_confirm"])
        self.assertEqual(mine["reason"], "Quarterly cleanup")
        self.caller = ADMIN_B
        theirs = self.client.get(ROUTE).json()["pending_reduction"]
        self.assertTrue(theirs["viewer_can_confirm"])


class TestConfirmingAReduction(DualControlBase):
    def test_the_requester_cannot_confirm_their_own_reduction(self) -> None:
        pending_id = self.request_reduction()["pending_id"]
        resp = self.confirm(pending_id)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(int(self.row()["retention_window_days"]), 365)

    def test_a_truthy_but_not_true_admin_flag_cannot_confirm(self) -> None:
        pending_id = self.request_reduction()["pending_id"]
        self.caller = TRUTHY_B
        self.assertEqual(self.confirm(pending_id).status_code, 403)
        self.caller = REVIEWER
        self.assertEqual(self.confirm(pending_id).status_code, 403)
        self.assertEqual(int(self.row()["retention_window_days"]), 365)

    def test_a_different_admin_confirms_and_both_are_audited(self) -> None:
        pending_id = self.request_reduction()["pending_id"]
        self.caller = ADMIN_B
        resp = self.confirm(pending_id)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "APPLIED")
        row = self.row()
        self.assertEqual(int(row["retention_window_days"]), 30)
        self.assertIsNone(row.get("pending_reduction"))
        confirmed = self.audit_row("retention_reduction_confirmed")
        self.assertEqual(confirmed["actor"], "admin-b")
        self.assertEqual(confirmed["requested_by"], "admin-a")
        self.assertEqual(confirmed["pending_id"], pending_id)
        self.assertEqual(int(confirmed["before_retention_window_days"]), 365)
        self.assertEqual(int(confirmed["after_retention_window_days"]), 30)

    def test_an_unknown_or_stale_id_is_404(self) -> None:
        self.request_reduction()
        self.caller = ADMIN_B
        self.assertEqual(self.confirm("not-the-id").status_code, 404)
        self.assertEqual(int(self.row()["retention_window_days"]), 365)


class TestCancellingAReduction(DualControlBase):
    def test_any_admin_can_cancel_and_the_id_is_then_dead(self) -> None:
        pending_id = self.request_reduction()["pending_id"]
        resp = self.client.post(f"{ROUTE}/pending/{pending_id}/cancel", json={"reason": "typo"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(self.row().get("pending_reduction"))
        cancelled = self.audit_row("retention_reduction_cancelled")
        self.assertEqual(cancelled["pending_id"], pending_id)
        self.assertEqual(cancelled["cancel_reason"], "typo")
        self.caller = ADMIN_B
        self.assertEqual(self.confirm(pending_id).status_code, 404)
        self.assertEqual(int(self.row()["retention_window_days"]), 365)

    def test_a_non_admin_cannot_cancel(self) -> None:
        pending_id = self.request_reduction()["pending_id"]
        self.caller = REVIEWER
        resp = self.client.post(f"{ROUTE}/pending/{pending_id}/cancel", json={})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(self.row().get("pending_reduction"))


class TestTheDelayAppliesItOnBothTargets(DualControlBase):
    def _age_pending(self, seconds: int) -> None:
        row = self.row()
        pending = dict(row["pending_reduction"])
        pending["requested_at"] = int(time.time()) - seconds
        pending["apply_after"] = int(pending["requested_at"]) + DELAY
        self.settings.update_item(
            Key={"setting_id": "global"},
            UpdateExpression="SET pending_reduction = :p",
            ExpressionAttributeValues={":p": pending},
        )

    def test_the_dts_sweep_applies_a_ready_reduction_and_audits_it(self) -> None:
        pending_id = self.request_reduction()["pending_id"]
        self._age_pending(DELAY + 60)
        retention.run_purge_sweep_now(self.s3, self.ddb)
        row = self.row()
        self.assertEqual(int(row["retention_window_days"]), 30)
        self.assertIsNone(row.get("pending_reduction"))
        applied = self.audit_row("retention_reduction_applied_after_delay")
        self.assertEqual(applied["pending_id"], pending_id)
        self.assertEqual(applied["requested_by"], "admin-a")

    def test_the_dts_sweep_leaves_a_reduction_inside_the_delay(self) -> None:
        self.request_reduction()
        self._age_pending(DELAY - 60)
        retention.run_purge_sweep_now(self.s3, self.ddb)
        self.assertEqual(int(self.row()["retention_window_days"]), 365)
        self.assertNotIn("retention_reduction_applied_after_delay", self.actions())

    def test_the_lambda_applies_a_ready_reduction_and_audits_it(self) -> None:
        pending_id = self.request_reduction()["pending_id"]
        self._age_pending(DELAY + 60)
        worker = _load_purge_worker()
        worker.run_purge_sweep()
        self.assertEqual(int(self.row()["retention_window_days"]), 30)
        applied = self.audit_row("retention_reduction_applied_after_delay")
        self.assertEqual(applied["pending_id"], pending_id)

    def test_the_lambda_has_no_free_text_confirmation_path(self) -> None:
        worker = _load_purge_worker()
        with self.assertRaises(TypeError):
            worker.request_retention_change(
                0, "admin-a", second_admin_confirmation={"actor": "admin-b"}
            )
        result = worker.request_retention_change(0, "admin-a", reason="x")
        self.assertEqual(result["status"], "PENDING_SECOND_APPROVAL")
        self.assertEqual(int(self.row()["retention_window_days"]), 365)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestRequestingAReduction,
        TestConfirmingAReduction,
        TestCancellingAReduction,
        TestTheDelayAppliesItOnBothTargets,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
