#!/usr/bin/env python3
"""
CI gate for issue #115: a presign failure must not burn the caller's daily
download-request quota.

## The bug this guards against

`backend/src/download.py::generate_presigned_download_url` charged the
per-user daily slot (`_check_per_user_limits`, a DynamoDB conditional
`update_item`) BEFORE calling `generate_presigned_url`. When presigning
itself fails — e.g. a misconfigured `S3_PUBLIC_ENDPOINT_URL` on the DTS
target building a second, broken `boto3.client` at the presign step — the
route raises HTTP 503 having already spent one of the caller's
`MAX_DAILY_REVIEWS` slots for a download that delivered no bytes. Repeated
clicks against a misconfigured deployment silently lock the reviewer out of
their own, otherwise-healthy downloads for the rest of the day, with no
audit row to explain why (the audit row is correctly not written on this
path, so quota and audit disagree about how many downloads happened).

SigV4 presigning is entirely local (no network round trip, no bytes
delivered), so charging the quota only AFTER a presign attempt has actually
succeeded closes the gap without opening a free-download path.

## Why the existing suite stayed green

`tests/test_download_auth_attack.py` and
`tests/test_download_daily_limit_528.py` both exercise
`_check_per_user_limits` and `generate_presigned_download_url` on the HAPPY
presign path only — `mock_s3.generate_presigned_url` always returns a URL.
Neither file drives a presign FAILURE through the full route, so neither
could see the quota get charged for a request that delivered nothing.

Checks (all must pass; exit 1 on any failure):

  1. `generate_presigned_url` raising `ClientError` still raises HTTP 503
     up through the route (behaviour must be unchanged).
  2. The user-row quota `update_item` was NOT called (or, if the fix shape
     compensates instead of reordering, was called and then reversed) —
     either way `call_count` must not settle at a net 1 for a request that
     delivered no URL. This assertion fails today: `call_count == 1`.
  3. The immediately-following, correctly-configured request still succeeds
     and is charged exactly once — proving the fix does not also swallow
     legitimate charges (a caller cannot get more than `MAX_DAILY_REVIEWS`
     real downloads by presign failures alone).
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import download as download_module  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi import HTTPException  # noqa: E402

generate_presigned_download_url = download_module.generate_presigned_download_url

OWNER_SUB = "owner-sub-115"
REVIEW_ID = "00000000-0000-4000-a000-0000000001a1"
S3_KEY = f"outputs/{REVIEW_ID}/result.json"
ENV_NAME = "test115"


class _CountingDynamoDBClient:
    """Tracks net quota `update_item` calls: +1 per charge, -1 per any
    compensating call this module's fix might issue (e.g. a decrement on
    the except path). A real DynamoDB conditional-increment client would
    accept either shape; this fake only needs to count, since #115 is not
    about the expression syntax (that is #528's gate) but about whether a
    charge survives a downstream failure.
    """

    def __init__(self) -> None:
        self.update_item_calls: list[dict] = []
        self._counters: dict[str, int] = {}

    def update_item(self, **kwargs):
        self.update_item_calls.append(kwargs)
        key = kwargs["Key"]["cognito_sub"]["S"]
        expr_values = kwargs.get("ExpressionAttributeValues", {})
        # A decrement/compensation call is recognisable by :one being
        # subtracted rather than added — accept either an UpdateExpression
        # spelling "- :one" or a caller-supplied ":minus_one" value so a
        # compensating fix of either shape is modelled correctly.
        update_expr = kwargs.get("UpdateExpression", "")
        if "- :one" in update_expr or ":minus_one" in expr_values:
            self._counters[key] = self._counters.get(key, 0) - 1
        else:
            self._counters[key] = self._counters.get(key, 0) + 1
        return {}

    def net_charge(self, user_sub: str) -> int:
        return self._counters.get(user_sub, 0)


def _caller_row() -> dict:
    return {"cognito_sub": OWNER_SUB, "email": "owner@example.com", "is_admin": False}


def _call(s3_client, dynamodb_client):
    return generate_presigned_download_url(
        review_id=REVIEW_ID,
        review_owner_sub=OWNER_SUB,
        s3_key=S3_KEY,
        caller_user_row=_caller_row(),
        env_name=ENV_NAME,
        s3_client=s3_client,
        dynamodb_client=dynamodb_client,
        bucket_name="contract-toaster-outputs-test115",
    )


class PresignFailureDoesNotBurnQuota(unittest.TestCase):
    def test_1_presign_failure_still_raises_503(self) -> None:
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "InternalError",
                    "Message": "presign backend misconfigured",
                }
            },
            operation_name="GeneratePresignedUrl",
        )
        fake_ddb = _CountingDynamoDBClient()

        with self.assertRaises(HTTPException) as ctx:
            _call(mock_s3, fake_ddb)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_2_failed_presign_does_not_charge_the_quota(self) -> None:
        """This is the #115 regression check. Fails today: net_charge == 1
        for a request that delivered no URL."""
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.side_effect = ClientError(
            error_response={"Error": {"Code": "InternalError", "Message": "boom"}},
            operation_name="GeneratePresignedUrl",
        )
        fake_ddb = _CountingDynamoDBClient()

        with self.assertRaises(HTTPException):
            _call(mock_s3, fake_ddb)

        self.assertEqual(
            fake_ddb.net_charge(OWNER_SUB),
            0,
            "A presign failure must not leave the caller's daily quota net-"
            f"charged; got net_charge={fake_ddb.net_charge(OWNER_SUB)} for a "
            "download that delivered no URL.",
        )

    def test_3_a_subsequent_successful_download_is_still_charged_once(self) -> None:
        """The fix must not also swallow legitimate charges: a failed
        presign followed by a working one must still cost exactly one
        quota slot overall."""
        mock_s3_fail = MagicMock()
        mock_s3_fail.generate_presigned_url.side_effect = ClientError(
            error_response={"Error": {"Code": "InternalError", "Message": "boom"}},
            operation_name="GeneratePresignedUrl",
        )
        fake_ddb = _CountingDynamoDBClient()

        with self.assertRaises(HTTPException):
            _call(mock_s3_fail, fake_ddb)

        mock_s3_ok = MagicMock()
        mock_s3_ok.generate_presigned_url.return_value = (
            "https://s3.amazonaws.com/bucket/key?X-Amz-Expires=60&sig=..."
        )
        response = _call(mock_s3_ok, fake_ddb)
        body = json.loads(response.body)
        self.assertIn("url", body)

        self.assertEqual(
            fake_ddb.net_charge(OWNER_SUB),
            1,
            "Exactly one real download must cost exactly one quota slot; "
            f"got net_charge={fake_ddb.net_charge(OWNER_SUB)}.",
        )


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(PresignFailureDoesNotBurnQuota)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        print("\nFAIL: issue #115 presign-failure-charges-quota checks failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
