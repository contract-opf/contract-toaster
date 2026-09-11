#!/usr/bin/env python3
"""
Issue #53 (audit finding F4, action A4): the upload put fails CLOSED on a
body the object store did not receive intact.

Before this change `review_routes._put_upload_object` called
`put_object(Bucket, Key, Body)` with no checksum. The row is written after
the object, so a put that FAILED already left no review behind -- but a put
S3 ACCEPTED with a body that differed from what the route hashed (a proxy
that mangled the stream, a partial write on a flaky link) was never
detected: the pipeline later read bytes whose sha256 disagreed with the
`file_sha256` recorded on the row.

Now the route sends `ChecksumSHA256` -- the base64 of the SAME digest it
records -- so the store recomputes it server-side and refuses a mismatch,
and that refusal (`ClientError`) is mapped to a 502 with a fixed `detail`
and NO reviews row, NO submissions row.

Driven through the REAL router mounted on `src.main.app` (a FastAPI
`TestClient`), not the helper in isolation: what this pins is that the
route's own `file_sha256` is the value on the wire, and that a refused put
happens BEFORE any row is written. The object store is a recording fake
rather than moto because moto 5.2.2 stores a supplied `x-amz-checksum-*`
header without verifying it (its `_get_checksum` carries a TODO saying so),
which would make "the store rejects a mismatch" a rubber stamp. The fake
therefore REJECTS what real S3 rejects -- a `ChecksumSHA256` that is not
the sha256 of the body -- and the first test proves that of the fake
itself before anything leans on it.

DynamoDB / Step Functions / the caller row are the same in-memory fakes
tests/test_review_routes_mounted_186.py drives the router with (imported
from there, the repo's established cross-test convention -- see
tests/test_attempt_diagnostics_669.py).

Run standalone: `.venv/bin/python tests/test_upload_checksum_53.py`.
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"

for _path in (BACKEND_ROOT, SCRIPTS_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# The mounted-router harness sets every env var the app needs on import.
from test_review_routes_mounted_186 import (  # noqa: E402
    PLAYBOOK_ID,
    FakeDynamoDBResource,
    FakeSfnClient,
    FakeUsersDynamoDBClient,
    _caller_row,
    _valid_docx_bytes,
)

from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import seed_active_bundle  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The exact reviewer-facing sentence the issue specifies. Asserted verbatim
# so a rewording is a deliberate change to this file, not a drift.
EXPECTED_502_DETAIL = (
    "The uploaded file could not be stored intact. Nothing was submitted; "
    "please try again."
)


def _b64_sha256(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


class RecordingS3Client:
    """A boto3-S3-client-shaped fake that records every `put_object` and
    behaves like the real service on the one thing this file is about.

    Real S3 recomputes the sha256 of the body it RECEIVED and refuses the
    put with `BadDigest` when the supplied `ChecksumSHA256` disagrees; so
    does this fake. `refuse_with` simulates the store refusing a put the
    route believed was correct -- the corrupted-in-transit case, which no
    in-process test can produce by actually corrupting the bytes -- with an
    error message that deliberately names the bucket, the key and a
    service-side phrase, so the "none of that reaches `detail`" assertion
    has something real to catch.
    """

    def __init__(self, refuse_with: str | None = None) -> None:
        self.puts: list[dict] = []
        self.refuse_with = refuse_with

    def put_object(self, **kwargs):  # noqa: N803 -- boto3 keyword casing
        self.puts.append(dict(kwargs))
        if self.refuse_with:
            raise ClientError(
                {
                    "Error": {
                        "Code": self.refuse_with,
                        "Message": (
                            "The SHA256 you specified did not match the calculated "
                            f"checksum. bucket={kwargs.get('Bucket')} key={kwargs.get('Key')}"
                        ),
                    }
                },
                "PutObject",
            )
        supplied = kwargs.get("ChecksumSHA256")
        if supplied is not None and supplied != _b64_sha256(kwargs["Body"]):
            raise ClientError(
                {"Error": {"Code": "BadDigest", "Message": "checksum mismatch"}},
                "PutObject",
            )
        return {}


class UploadChecksumTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.ddb = FakeDynamoDBResource()
        self.sfn = FakeSfnClient()
        self.users_ddb_client = FakeUsersDynamoDBClient()
        seed_active_bundle.seed_active_bundle(PLAYBOOK_ID, self.ddb)

        self.app = backend_main.app
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[review_routes.get_sfn_client] = lambda: self.sfn
        self.app.dependency_overrides[review_routes.get_dynamodb_client] = (
            lambda: self.users_ddb_client
        )
        self.app.dependency_overrides[review_routes.get_env_name] = lambda: "dev"
        self.app.dependency_overrides[review_routes.get_av_client] = (
            lambda: review_routes.NullAvClient()
        )
        row = _caller_row("owner-53")
        self.app.dependency_overrides[review_routes.get_active_user_row] = lambda: row
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def _use_s3(self, s3: RecordingS3Client) -> RecordingS3Client:
        self.app.dependency_overrides[review_routes.get_s3_client] = lambda: s3
        return s3

    def _submit(self, contents: bytes):
        return self.client.post(
            "/api/reviews",
            files={"file": ("in.docx", contents, DOCX_CONTENT_TYPE)},
            data={"playbook_id": PLAYBOOK_ID, "idempotency_key": "key-53"},
        )

    def _reviews_items(self) -> dict:
        return self.ddb.Table(os.environ["REVIEWS_TABLE"]).items

    def _submissions_items(self) -> dict:
        return self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"]).items


class TestTheFakeRejectsWhatS3Rejects(unittest.TestCase):
    """Watched-red for the double: a fake that accepted any checksum would
    make every assertion below a rubber stamp."""

    def test_mismatched_checksum_is_refused_with_bad_digest(self) -> None:
        s3 = RecordingS3Client()
        with self.assertRaises(ClientError) as ctx:
            s3.put_object(
                Bucket="b", Key="k", Body=b"the bytes", ChecksumSHA256=_b64_sha256(b"other bytes")
            )
        self.assertEqual(ctx.exception.response["Error"]["Code"], "BadDigest")

    def test_matching_checksum_is_accepted(self) -> None:
        s3 = RecordingS3Client()
        s3.put_object(Bucket="b", Key="k", Body=b"the bytes", ChecksumSHA256=_b64_sha256(b"the bytes"))
        self.assertEqual(len(s3.puts), 1)


class TestUploadPutCarriesTheRecordedHash(UploadChecksumTestBase):
    def test_put_object_receives_checksum_sha256_of_the_stored_bytes(self) -> None:
        s3 = self._use_s3(RecordingS3Client())
        contents = _valid_docx_bytes("Checksum me")

        resp = self._submit(contents)

        self.assertEqual(resp.status_code, 202, resp.text)
        self.assertEqual(len(s3.puts), 1, "exactly one upload put")
        put = s3.puts[0]
        body = put["Body"]
        # The value on the wire is base64 of the RAW digest of the body that
        # was put -- the issue's exact formula, computed independently here.
        expected = base64.b64encode(bytes.fromhex(hashlib.sha256(body).hexdigest())).decode()
        self.assertEqual(put.get("ChecksumSHA256"), expected)
        self.assertEqual(put["Bucket"], os.environ["UPLOADS_BUCKET"])
        # And it is the route's OWN hash of the bytes it went on to record:
        # the submission row points at exactly this object.
        review_id = resp.json()["review_id"]
        self.assertEqual(put["Key"], f"uploads/owner-53/{review_id}/in.docx")
        submissions = self._submissions_items()
        self.assertEqual(len(submissions), 1)
        (submission,) = submissions.values()
        self.assertEqual(submission["upload_pointer"], put["Key"])
        self.assertIn(review_id, self._reviews_items())

    def test_helper_is_the_route_path_not_a_parallel_one(self) -> None:
        """The checksum is produced by `checksum_sha256_b64` from the hex
        digest the route already computes -- one formula, one place."""
        digest_hex = hashlib.sha256(b"abc").hexdigest()
        self.assertEqual(
            review_routes.checksum_sha256_b64(digest_hex),
            base64.b64encode(hashlib.sha256(b"abc").digest()).decode(),
        )


class TestRefusedPutFailsClosed(UploadChecksumTestBase):
    def test_bad_digest_answers_502_with_fixed_detail_and_writes_no_rows(self) -> None:
        s3 = self._use_s3(RecordingS3Client(refuse_with="BadDigest"))
        contents = _valid_docx_bytes("Corrupted in transit")

        resp = self._submit(contents)

        self.assertEqual(resp.status_code, 502, resp.text)
        detail = resp.json()["detail"]
        self.assertEqual(detail, EXPECTED_502_DETAIL)
        # Neither the storage layout nor the service's words reach the
        # reviewer: the fake's message named all three on purpose.
        self.assertEqual(len(s3.puts), 1)
        self.assertNotIn(os.environ["UPLOADS_BUCKET"], detail)
        self.assertNotIn(s3.puts[0]["Key"], detail)
        self.assertNotIn("uploads/", detail)
        self.assertNotIn("BadDigest", detail)
        self.assertNotIn("did not match the calculated checksum", detail)
        # Nothing was submitted: the put happens BEFORE either row is written,
        # and a refused put must leave the tables exactly as they were.
        self.assertEqual(self._reviews_items(), {})
        self.assertEqual(self._submissions_items(), {})
        self.assertEqual(self.sfn.start_execution_call_count, 0)

    def test_other_store_refusals_map_the_same_way(self) -> None:
        """MinIO-flavoured refusal codes (`InvalidRequest`,
        `XAmzContentSHA256Mismatch`) are the same failure to the reviewer."""
        for code in ("InvalidRequest", "XAmzContentSHA256Mismatch"):
            with self.subTest(code=code):
                self.setUp()
                self._use_s3(RecordingS3Client(refuse_with=code))
                resp = self._submit(_valid_docx_bytes(f"refused as {code}"))
                self.assertEqual(resp.status_code, 502)
                self.assertEqual(resp.json()["detail"], EXPECTED_502_DETAIL)
                self.assertNotIn(code, resp.json()["detail"])
                self.assertEqual(self._reviews_items(), {})
                self.assertEqual(self._submissions_items(), {})
                self.tearDown()


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
