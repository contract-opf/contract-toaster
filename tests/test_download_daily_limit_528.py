#!/usr/bin/env python3
"""
CI gate for issue #528: the per-user daily download limiter must speak valid
DynamoDB, proven against a REAL DynamoDB — not a MagicMock.

## The bug this guards against

`backend/src/download.py::_check_per_user_limits` built its expressions as:

    UpdateExpression="SET dailyReviewCount_#day = if_not_exists(dailyReviewCount_#day, :zero) + :one"
    ExpressionAttributeNames={"#day": "2026-08-03"}

A DynamoDB `#name` placeholder is a COMPLETE path component, never a suffix
glued onto a literal prefix, so the service rejects that string outright with
ValidationException. The route translates any non-conditional ClientError into
HTTP 503, so **every authenticated redline download 503'd** — sitting directly
behind the #465 outputs-bucket 503 that fired first and hid it. Fixing #465
alone would have traded one 503 for another on every deployment.

## Why the existing suite stayed green

`tests/test_download_auth_attack.py` drives this function with a bare
`MagicMock` DynamoDB client. A MagicMock accepts ANY string as an
UpdateExpression, so it asserts the 429 path and the `cognito_sub` key name
(#193) without ever parsing the expression. A syntactically invalid expression
passed every test.

That is the real lesson and the reason this file exists: **a test that asserts
DynamoDB behaviour through a MagicMock proves nothing about DynamoDB.** These
checks run against `moto`, which parses expressions the way the service does,
so an invalid expression fails here instead of in production.

Checks (all must pass; exit 1 on any failure):

  1. The first call succeeds against a real table and sets the day's counter
     to 1 (this is the check that fails on the pre-fix expression).
  2. The counter is named `dailyReviewCount_<YYYY-MM-DD>` and increments.
  3. Exceeding MAX_DAILY_REVIEWS raises HTTP 429 — against a real table.
  4. Counters are independent per user and per day.
  5. The table name is resolved from USERS_TABLE (the name every other
     module uses) rather than a second, separately-derived literal.
"""
import os
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import download as download_module  # noqa: E402

_check_per_user_limits = download_module._check_per_user_limits
MAX_DAILY_REVIEWS = download_module.MAX_DAILY_REVIEWS

USERS_TABLE = "contract-toaster-users-test528"
ENV_NAME = "test528"


def _today_attr() -> str:
    return "dailyReviewCount_" + time.strftime("%Y-%m-%d", time.gmtime())


def _make_users_table(client) -> None:
    client.create_table(
        TableName=USERS_TABLE,
        KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _counter(client, sub: str) -> int:
    item = client.get_item(
        TableName=USERS_TABLE, Key={"cognito_sub": {"S": sub}}
    ).get("Item", {})
    raw = item.get(_today_attr())
    return int(raw["N"]) if raw else 0


class DailyLimitAgainstRealDynamo(unittest.TestCase):
    """Every check here runs against moto, so the expressions are parsed."""

    def setUp(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self.client = boto3.client("dynamodb", region_name="us-east-1")
        _make_users_table(self.client)
        self._prev = os.environ.get("USERS_TABLE")
        os.environ["USERS_TABLE"] = USERS_TABLE

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("USERS_TABLE", None)
        else:
            os.environ["USERS_TABLE"] = self._prev
        self._mock.stop()

    def test_1_first_call_succeeds_and_sets_the_counter(self) -> None:
        """The pre-fix expression raises ValidationException -> HTTP 503 here."""
        _check_per_user_limits(
            user_sub="user-a", env_name=ENV_NAME, dynamodb_client=self.client
        )
        self.assertEqual(_counter(self.client, "user-a"), 1)

    def test_2_counter_is_day_scoped_and_increments(self) -> None:
        for expected in (1, 2, 3):
            _check_per_user_limits(
                user_sub="user-a", env_name=ENV_NAME, dynamodb_client=self.client
            )
            self.assertEqual(_counter(self.client, "user-a"), expected)
        item = self.client.get_item(
            TableName=USERS_TABLE, Key={"cognito_sub": {"S": "user-a"}}
        )["Item"]
        self.assertIn(_today_attr(), item)

    def test_3_exceeding_the_limit_raises_429(self) -> None:
        for _ in range(MAX_DAILY_REVIEWS):
            _check_per_user_limits(
                user_sub="user-a", env_name=ENV_NAME, dynamodb_client=self.client
            )
        with self.assertRaises(HTTPException) as ctx:
            _check_per_user_limits(
                user_sub="user-a", env_name=ENV_NAME, dynamodb_client=self.client
            )
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(_counter(self.client, "user-a"), MAX_DAILY_REVIEWS)

    def test_4_counters_are_independent_per_user(self) -> None:
        for _ in range(3):
            _check_per_user_limits(
                user_sub="user-a", env_name=ENV_NAME, dynamodb_client=self.client
            )
        _check_per_user_limits(
            user_sub="user-b", env_name=ENV_NAME, dynamodb_client=self.client
        )
        self.assertEqual(_counter(self.client, "user-a"), 3)
        self.assertEqual(_counter(self.client, "user-b"), 1)

    def test_5_a_previous_days_counter_does_not_bind_today(self) -> None:
        """Yesterday's attribute must not gate today's request."""
        yesterday = "dailyReviewCount_" + time.strftime(
            "%Y-%m-%d", time.gmtime(time.time() - 86400)
        )
        self.client.put_item(
            TableName=USERS_TABLE,
            Item={
                "cognito_sub": {"S": "user-c"},
                yesterday: {"N": str(MAX_DAILY_REVIEWS)},
            },
        )
        _check_per_user_limits(
            user_sub="user-c", env_name=ENV_NAME, dynamodb_client=self.client
        )
        self.assertEqual(_counter(self.client, "user-c"), 1)

    def test_6_table_name_comes_from_USERS_TABLE(self) -> None:
        """Resolved from the env var every other module uses, so the users
        table can never drift into two independently-derived names (#465)."""
        os.environ["USERS_TABLE"] = "some-other-users-table"
        with self.assertRaises(HTTPException) as ctx:
            _check_per_user_limits(
                user_sub="user-a", env_name=ENV_NAME, dynamodb_client=self.client
            )
        # A missing table is a 503, proving the name was taken from the env
        # var rather than derived from env_name.
        self.assertEqual(ctx.exception.status_code, 503)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(DailyLimitAgainstRealDynamo)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        print("\nFAIL: issue #528 daily-limit checks failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
