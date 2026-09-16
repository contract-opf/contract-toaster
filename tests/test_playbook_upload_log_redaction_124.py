#!/usr/bin/env python3
"""
Regression test for issue #124 (review fix round 1, finding 1): the
server-side log the #124 fix added must not become a BIGGER leak than the
HTTP body it sealed.

## Root problem this proves fixed

Issue #124 moved the exception off the HTTP response and onto
`logger.error(..., %r, exc)`. For every other exception that diff logs
(`botocore` ClientError, PyJWT DecodeError, `expat.ExpatError`) the repr is
a bounded message. `UnicodeDecodeError` is the exception:

    UnicodeDecodeError.args == (encoding, object, start, end, reason)

so `repr(exc)` embeds the ENTIRE object being decoded. On
`POST /api/admin/playbooks` that object is the uploaded playbook file,
bounded only by `backend/src/upload_validation.py::MAX_UPLOAD_SIZE_BYTES`
(25 MiB). `%r` there would have written the whole document into CloudWatch,
which ARCHITECTURE.md's "Prompt / output logging policy" prohibits outright:
raw document text is **prohibited from CloudWatch logs**.

`test_repr_of_the_decode_error_would_have_carried_the_document` below is the
producer proof that the hazard is real rather than theoretical, and
`test_no_uploaded_bytes_reach_the_log` is the assertion that the handler no
longer emits it.

This test MUST FAIL on the `%r` spelling of the handler
(`backend/src/main.py`, `PLAYBOOK_UPLOAD_UTF8_DECODE_FAILED`) and PASS on the
named-field spelling that replaced it.

## Fixture note

The uploaded bytes are built here, not read from a file: the point of the
fixture is a payload big enough that a repr dump is unmistakable (130 KB) and
carrying a sentinel string that could only have come from the document body.
The trailing `\\xff` is what makes the UTF-8 decode fail.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-logredact124-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-logredact124-test"
)
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-logredact124-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-logredact124-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-logredact124-test")

import boto3  # noqa: E402, I001
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402

CREATE_PATH = "/api/admin/playbooks"
ADMIN_SUB = "admin-1"

# A string that can only have come out of the uploaded document body.
SENTINEL = "CONFIDENTIAL-MERGER-CONSIDERATION-CLAUSE-124"

# The logger `backend/src/main.py` binds (`logging.getLogger(__name__)`).
MAIN_LOGGER = "src.main"

LOG_EVENT = "PLAYBOOK_UPLOAD_UTF8_DECODE_FAILED"


def _undecodable_playbook_bytes() -> bytes:
    """A 130 KB would-be playbook whose last byte is not valid UTF-8."""
    body = (
        '{"opf_version": "0.3", "agreement_type": {"id": "x"}, "pad": "'
        + SENTINEL
        + "A" * 130_000
        + '"}'
    )
    return body.encode("utf-8") + b"\xff"


class PlaybookUploadLogRedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])

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
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
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

        self.ddb.Table(os.environ["USERS_TABLE"]).put_item(
            Item={
                "cognito_sub": ADMIN_SUB,
                "email": f"{ADMIN_SUB}@example.com",
                "status": "active",
                "is_admin": True,
            }
        )

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[backend_main.get_s3_client] = lambda: self.s3
        backend_main.app.dependency_overrides[backend_main.get_current_user] = lambda: {
            "sub": ADMIN_SUB,
            "email": f"{ADMIN_SUB}@example.com",
            "token_use": "access",
        }

    def tearDown(self) -> None:
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _upload_undecodable(self) -> tuple[object, list[logging.LogRecord]]:
        with self.assertLogs(MAIN_LOGGER, level="ERROR") as captured:
            resp = self.client.post(
                CREATE_PATH,
                files={
                    "file": (
                        "playbook.opf.json",
                        _undecodable_playbook_bytes(),
                        "application/octet-stream",
                    )
                },
                data={"version": "1.0.0"},
            )
        return resp, captured.records

    # ------------------------------------------------------------------
    # Producer proof: the hazard this test guards is real, not theoretical.
    # ------------------------------------------------------------------
    def test_repr_of_the_decode_error_would_have_carried_the_document(self) -> None:
        raw = _undecodable_playbook_bytes()
        with self.assertRaises(UnicodeDecodeError) as caught:
            raw.decode("utf-8")
        exc = caught.exception

        # This is precisely why `%r` is unusable for this exception type.
        self.assertIn(SENTINEL, repr(exc))
        self.assertGreater(len(repr(exc)), 130_000)

        # ...while the named fields (and `str`) carry no payload at all.
        self.assertNotIn(SENTINEL, str(exc))
        self.assertEqual(exc.encoding, "utf-8")
        self.assertIsInstance(exc.start, int)

    # ------------------------------------------------------------------
    # The assertion the fix has to satisfy.
    # ------------------------------------------------------------------
    def test_no_uploaded_bytes_reach_the_log(self) -> None:
        resp, records = self._upload_undecodable()
        self.assertEqual(resp.status_code, 400, resp.text)

        decode_records = [r for r in records if LOG_EVENT in r.getMessage()]
        self.assertTrue(
            decode_records,
            f"expected a {LOG_EVENT} record on {MAIN_LOGGER}; got "
            f"{[r.getMessage()[:120] for r in records]}",
        )

        for record in records:
            message = record.getMessage()
            self.assertNotIn(SENTINEL, message)
            self.assertNotIn("A" * 200, message)
            # A 25 MiB repr dump cannot hide inside a few hundred characters.
            self.assertLess(
                len(message),
                500,
                "log record is far longer than a redacted summary should be",
            )

    def test_the_log_still_says_where_and_why_the_decode_failed(self) -> None:
        # Redaction must not degrade into logging nothing useful: the record
        # still has to be actionable from CloudWatch alone.
        _resp, records = self._upload_undecodable()
        message = next(r.getMessage() for r in records if LOG_EVENT in r.getMessage())

        self.assertIn("error_id=", message)
        self.assertIn("encoding=utf-8", message)
        self.assertIn("reason=", message)
        self.assertIn("start=", message)
        self.assertIn("end=", message)

    def test_response_body_carries_no_document_text_either(self) -> None:
        resp, _records = self._upload_undecodable()
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertNotIn(SENTINEL, resp.text)
        self.assertEqual(resp.json()["detail"], "Upload is not valid UTF-8 text.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
