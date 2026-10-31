#!/usr/bin/env python3
"""
Executable tests for QA remediation W6.8: the GC alarm on a retention
reduction request, on both deployment targets.

  AWS  The backend writes ONE structured log line,
       `RETENTION_REDUCTION_REQUESTED {json}`, per parked reduction. A
       CloudWatch metric filter on the App Runner application log group
       counts it and the `contract-toaster-retention-reduction-{env}` alarm
       pages the alarms topic (asserted against the synthesized template in
       tests/test_observability_dashboard_cloudtrail.py, AC4).
  DTS  When `RETENTION_ALERT_URL` is set (an Uptime Kuma push URL, the same
       mechanism `AUTOPULL_HEARTBEAT_URL` uses), the request also sends one
       `status=down&msg=...` push. Unset, nothing is sent. A failing push
       never fails the request.

Neither signal carries the admin's free-text reason (it is in the audit row,
which is where substance-adjacent text belongs) -- ids, window values and the
apply-at time only.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-alert-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-alert-test")

import boto3  # noqa: E402, I001
from moto import mock_aws  # noqa: E402

from src import retention, retention_alerts  # noqa: E402

ADMIN = {"cognito_sub": "admin-a", "status": "active", "is_admin": True}
SECRET_REASON = "Matter Zorblax v. Quux settlement"


class AlertBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        settings = self.ddb.create_table(
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
        settings.put_item(Item={"setting_id": "global", "retention_window_days": 365})
        self.sent: list[str] = []
        patcher = mock.patch.object(retention_alerts, "_open_url", side_effect=self._record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._mock.stop()

    def _record(self, url: str) -> None:
        self.sent.append(url)

    def reduce(self) -> dict:
        return retention.request_retention_change(30, ADMIN, self.ddb, reason=SECRET_REASON)


class TestTheLogLineTheAwsAlarmCounts(AlertBase):
    def test_one_structured_line_per_parked_reduction(self) -> None:
        with (
            mock.patch.dict(os.environ, {"RETENTION_ALERT_URL": ""}),
            self.assertLogs(retention_alerts.logger, level="WARNING") as logs,
        ):
            result = self.reduce()
        lines = [r.getMessage() for r in logs.records
                 if r.getMessage().startswith(retention_alerts.LOG_MARKER)]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0][len(retention_alerts.LOG_MARKER):])
        self.assertEqual(payload["pending_id"], result["pending_id"])
        self.assertEqual(payload["before_retention_window_days"], 365)
        self.assertEqual(payload["after_retention_window_days"], 30)
        self.assertEqual(payload["requested_by"], "admin-a")
        self.assertNotIn("Zorblax", lines[0])

    def test_a_forward_change_raises_no_alarm(self) -> None:
        with (
            mock.patch.dict(os.environ, {"RETENTION_ALERT_URL": "https://kuma.example/api/push/t"}),
            self.assertNoLogs(retention_alerts.logger, level="WARNING"),
        ):
            retention.request_retention_change(1095, ADMIN, self.ddb)
        self.assertEqual(self.sent, [])


class TestTheDtsPush(AlertBase):
    def test_unset_sends_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"RETENTION_ALERT_URL": ""}):
            self.reduce()
        self.assertEqual(self.sent, [])

    def test_set_sends_one_down_push_without_the_reason(self) -> None:
        with mock.patch.dict(os.environ, {"RETENTION_ALERT_URL": "https://kuma.example/api/push/tok"}):
            result = self.reduce()
        self.assertEqual(len(self.sent), 1)
        parts = urlsplit(self.sent[0])
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}",
                         "https://kuma.example/api/push/tok")
        query = parse_qs(parts.query)
        self.assertEqual(query["status"], ["down"])
        self.assertIn("30", query["msg"][0])
        self.assertIn(result["pending_id"][:8], query["msg"][0])
        self.assertNotIn("Zorblax", self.sent[0])

    def test_an_existing_query_string_is_kept(self) -> None:
        with mock.patch.dict(os.environ, {"RETENTION_ALERT_URL": "https://k.example/p?ping="}):
            self.reduce()
        query = parse_qs(urlsplit(self.sent[0]).query, keep_blank_values=True)
        self.assertIn("ping", query)
        self.assertEqual(query["status"], ["down"])

    def test_a_non_http_url_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"RETENTION_ALERT_URL": "file:///etc/passwd"}):
            self.reduce()
        self.assertEqual(self.sent, [])

    def test_a_failing_push_never_fails_the_request(self) -> None:
        with (
            mock.patch.object(retention_alerts, "_open_url", side_effect=OSError("down")),
            mock.patch.dict(os.environ, {"RETENTION_ALERT_URL": "https://k.example/p"}),
        ):
            result = self.reduce()
        self.assertEqual(result["status"], "PENDING_SECOND_APPROVAL")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestTheLogLineTheAwsAlarmCounts))
    suite.addTests(loader.loadTestsFromTestCase(TestTheDtsPush))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
