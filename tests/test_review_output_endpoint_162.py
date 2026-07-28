#!/usr/bin/env python3
"""
Executable endpoint-level test for issue #162: GET /api/reviews/{review_id}/output
must derive `s3_key` (and `owner_sub`) from the authoritative `reviews`
DynamoDB record -- never from any request-supplied value -- and must preserve
every control `download.py` enforces, exercised end-to-end through the real
route rather than by calling its functions directly.

## State note (per issue #162's own "Required verification" correction)

The wiring itself already landed: `src.review_routes.get_review_output`
already derives `owner_sub` / `output_s3_key` from the stored `reviews` row
and calls `download.generate_presigned_download_url` (see that route's own
docstring). The one remaining acceptance criterion was the missing
endpoint-level test -- this file is that test, not a behavior change.

Covers the issue's "Required verification" contract, end-to-end through the
REAL `src.review_routes.router` mounted on a local `FastAPI()` app (same
convention as tests/test_review_api_84.py):

  (1) `s3_key` is derived from the stored review record, not any request
      parameter -- proven by injecting attacker-supplied `s3_key` /
      `output_s3_key` query parameters that point at a DIFFERENT review's
      output, and asserting the presigned URL is generated for the
      DB-derived key, never the injected one (a `_SpyS3Client` records the
      exact `Key` passed to `generate_presigned_url`, so this is checked
      directly rather than by parsing the returned URL string).
  (2) An `output_s3_key` on the reviews row that is not bound to
      `outputs/<review_id>/` (cross-review IDOR, or path traversal) is
      rejected 403 -- the endpoint-level companion to
      tests/test_download_auth_attack.py::TestKeyBoundToReviewId, which only
      exercises `download._validate_s3_key_bound_to_review` directly. This
      proves the same enforcement holds even when the corruption is in the
      stored row itself (e.g. a hypothetical persist-stage bug), reached
      through the real HTTP route rather than the bare function.
  (3) Owner/admin gate: a non-owner gets 403, the owner and an admin
      (non-owner) both get 200.
  (4) The response carries `Cache-Control: no-store` and `expires_in` equal
      to `download.PRESIGNED_URL_TTL_SECONDS` (60s).
  (5) The per-user daily download-request limit is enforced through the
      endpoint: the (`MAX_DAILY_REVIEWS` + 1)th request in a day for the same
      caller gets 429.

## AWS mocking strategy

  - S3 (outputs bucket) and the DynamoDB `reviews` table use the REAL
    `moto.mock_aws` backend: both are exercised here only via simple
    `put_object`/`generate_presigned_url` and `get_item`/`put_item` calls,
    which moto 5.2.2 handles correctly.
  - The per-user daily-limit table (`contract-toaster-users-<env>`) uses
    `FakeUsersDynamoDBClient`, imported from tests/test_review_api_84.py
    rather than reimplemented here (same cross-file fixture-reuse convention
    tests/test_registry_profiles.py and tests/test_sample_agreement_policy.py
    already use). Real moto 5.2.2 cannot parse
    `download._check_per_user_limits`'s UpdateExpression -- confirmed directly
    against moto 5.2.2 while writing this test:
    `ValidationException: Invalid UpdateExpression: Syntax error; token: "d",
    near: "SET dailyReviewCount_#day"` (moto's expression parser rejects a
    `#name` placeholder fused onto a literal prefix with no separator, even
    though real DynamoDB accepts it). tests/test_review_api_84.py hit the
    same class of moto limitation for `reviews.reserve_spend`'s conditional
    (arithmetic inside an `OR`) and adopted the same fix: a schema-shaped
    in-memory fake standing in for the real backend on that one table.

This file never calls `POST /api/reviews` / `submit_review` -- review rows
are seeded directly into the `reviews` table, so none of the submission-time
machinery (playbook registry, idempotency, spend caps, Step Functions) is
exercised or required here.

This test MUST FAIL if the endpoint stops deriving `s3_key`/`owner_sub` from
the stored record (e.g. a future change that reads either from client
input), or if any of the five controls above regresses. Run standalone:
`python tests/test_review_output_endpoint_162.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import os
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
BACKEND_ROOT = REPO_ROOT / "backend"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("S3_OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")
# Explicitly disabled (not merely left unset): importing test_review_api_84
# below runs ITS module-level `os.environ.setdefault("AUDIT_TABLE", ...)`,
# which would otherwise turn on review_routes._write_audit_row's real
# put_item path with no matching table created here. Audit-row behavior is
# already covered by tests/test_review_api_84.py::TestDownloadAudit and is
# out of scope for issue #162 -- setting this to "" (falsy) up front keeps
# `_write_audit_row`'s documented best-effort no-op ("never gate the request
# itself on audit-table config") in effect for every test in this file.
os.environ["AUDIT_TABLE"] = ""

import boto3  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

# Reused rather than reimplemented -- same cross-file fixture convention
# tests/test_registry_profiles.py (imports test_form_coverage /
# test_heading_hash_drift) and tests/test_sample_agreement_policy.py (imports
# test_policy_document) already establish. FakeUsersDynamoDBClient is the
# schema-shaped per-user-limit fake this file's module docstring explains the
# need for; _caller_row builds the same caller-row shape
# `get_active_user_row` is overridden to return everywhere else in this
# router's tests.
from test_review_api_84 import FakeUsersDynamoDBClient, _caller_row  # noqa: E402

import src.download as download_module  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

PLAYBOOK_ID = "eiaa"
AUTH_REVIEW_ID = "22222222-2222-4222-a222-222222222222"
OTHER_REVIEW_ID = "33333333-3333-4333-a333-333333333333"
OWNER_SUB = "owner-162"


class _SpyS3Client:
    """Wraps the real moto-backed S3 client, recording every
    `generate_presigned_url` call's `Params` -- so AC1 (derived from the
    stored record, never from a request parameter) can be asserted directly
    against the Key that was actually presigned, rather than by parsing the
    returned URL string. Delegates everything else (bucket/object setup in
    test bodies) straight through to the real client via `__getattr__`.
    """

    def __init__(self, real_client):
        self._real = real_client
        self.presign_calls: list[dict] = []

    def generate_presigned_url(self, ClientMethod, Params=None, ExpiresIn=None):
        self.presign_calls.append({"Params": dict(Params or {}), "ExpiresIn": ExpiresIn})
        return self._real.generate_presigned_url(ClientMethod, Params=Params, ExpiresIn=ExpiresIn)

    def __getattr__(self, name):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Shared test base: local FastAPI app mounting the real router, AWS faked.
# ---------------------------------------------------------------------------


class ReviewOutputEndpointTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=os.environ["S3_OUTPUTS_BUCKET"])
        self.spy_s3 = _SpyS3Client(self.s3)

        self.ddb_resource = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb_resource.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.reviews_table = self.ddb_resource.Table(os.environ["REVIEWS_TABLE"])

        self.users_ddb_client = FakeUsersDynamoDBClient()

        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = (
            lambda: self.ddb_resource
        )
        self.app.dependency_overrides[review_routes.get_s3_client] = lambda: self.spy_s3
        self.app.dependency_overrides[review_routes.get_dynamodb_client] = (
            lambda: self.users_ddb_client
        )
        self.app.dependency_overrides[review_routes.get_env_name] = lambda: "dev"
        self.client = TestClient(self.app)

    def tearDown(self):
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str, is_admin: bool = False) -> None:
        row = _caller_row(sub, is_admin=is_admin)
        self.app.dependency_overrides[review_routes.get_active_user_row] = lambda: row

    def _seed_review(
        self,
        review_id: str,
        *,
        owner_sub: str,
        output_s3_key: str,
        status: str = "DONE",
    ) -> None:
        self.reviews_table.put_item(
            Item={
                "review_id": review_id,
                "owner_sub": owner_sub,
                "status": status,
                "output_s3_key": output_s3_key,
                "playbook_id": PLAYBOOK_ID,
                "created_at": "1000",
                "updated_at": "1000",
            }
        )

    def _put_output_object(self, s3_key: str, body: bytes = b"redline bytes") -> None:
        self.s3.put_object(Bucket=os.environ["S3_OUTPUTS_BUCKET"], Key=s3_key, Body=body)


# -- (1) s3_key derived from the stored review record, never a request param -


class TestKeyDerivedFromRecordNotRequest(ReviewOutputEndpointTestBase):
    def test_query_param_s3_key_is_ignored_derived_key_is_used(self):
        legit_key = f"outputs/{AUTH_REVIEW_ID}/legit-out.docx"
        decoy_key = f"outputs/{OTHER_REVIEW_ID}/decoy.docx"
        self._put_output_object(legit_key)
        self._put_output_object(decoy_key)
        self._seed_review(AUTH_REVIEW_ID, owner_sub=OWNER_SUB, output_s3_key=legit_key)

        self._authenticate_as(OWNER_SUB)
        # The route declares no query parameter that could bind to either of
        # these names -- this proves an attacker cannot influence the
        # presigned key by adding request parameters, only by (hypothetically)
        # controlling the DB row, which AC2's tests cover separately.
        resp = self.client.get(
            f"/api/reviews/{AUTH_REVIEW_ID}/output",
            params={"s3_key": decoy_key, "output_s3_key": decoy_key},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.spy_s3.presign_calls), 1)
        presigned_key = self.spy_s3.presign_calls[0]["Params"]["Key"]
        self.assertEqual(
            presigned_key,
            legit_key,
            "The presigned Key must be the DB-derived output_s3_key, never a "
            "request-supplied value.",
        )
        self.assertNotEqual(presigned_key, decoy_key)


# -- (2) endpoint-level companion to TestKeyBoundToReviewId -------------------


class TestKeyBindingEnforcedAtEndpoint(ReviewOutputEndpointTestBase):
    """Even if the stored `output_s3_key` itself is wrong (a hypothetical
    persist-stage bug, a corrupted row, etc.), the endpoint must not presign
    it: download.py's independent `_validate_s3_key_bound_to_review` gate
    must still fire. Companion to
    tests/test_download_auth_attack.py::TestKeyBoundToReviewId, which only
    calls that function directly -- this proves the same enforcement holds
    when reached through the real HTTP route."""

    def test_output_key_pointing_at_another_review_is_403(self):
        cross_review_key = f"outputs/{OTHER_REVIEW_ID}/out.docx"
        self._put_output_object(cross_review_key)
        self._seed_review(AUTH_REVIEW_ID, owner_sub=OWNER_SUB, output_s3_key=cross_review_key)

        self._authenticate_as(OWNER_SUB)
        resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")

        self.assertEqual(resp.status_code, 403)

    def test_output_key_with_path_traversal_is_403(self):
        traversal_key = f"outputs/{AUTH_REVIEW_ID}/../{OTHER_REVIEW_ID}/secret.docx"
        self._seed_review(AUTH_REVIEW_ID, owner_sub=OWNER_SUB, output_s3_key=traversal_key)

        self._authenticate_as(OWNER_SUB)
        resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")

        self.assertEqual(resp.status_code, 403)


# -- (3) owner/admin gate ------------------------------------------------------


class TestOwnerAdminGate(ReviewOutputEndpointTestBase):
    def setUp(self):
        super().setUp()
        self.output_key = f"outputs/{AUTH_REVIEW_ID}/out.docx"
        self._put_output_object(self.output_key)
        self._seed_review(AUTH_REVIEW_ID, owner_sub=OWNER_SUB, output_s3_key=self.output_key)

    def test_non_owner_gets_403(self):
        self._authenticate_as("attacker-162")
        resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")
        self.assertEqual(resp.status_code, 403)

    def test_admin_gets_200_for_someone_elses_review(self):
        self._authenticate_as("admin-162", is_admin=True)
        resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")
        self.assertEqual(resp.status_code, 200)

    def test_owner_gets_200(self):
        self._authenticate_as(OWNER_SUB)
        resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")
        self.assertEqual(resp.status_code, 200)


# -- (4) Cache-Control: no-store + 60s presigned TTL ---------------------------


class TestCacheControlAndTtl(ReviewOutputEndpointTestBase):
    def test_response_is_no_store_with_60s_ttl(self):
        output_key = f"outputs/{AUTH_REVIEW_ID}/out.docx"
        self._put_output_object(output_key)
        self._seed_review(AUTH_REVIEW_ID, owner_sub=OWNER_SUB, output_s3_key=output_key)

        self._authenticate_as(OWNER_SUB)
        resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "no-store")
        self.assertEqual(resp.json()["expires_in"], download_module.PRESIGNED_URL_TTL_SECONDS)
        self.assertEqual(download_module.PRESIGNED_URL_TTL_SECONDS, 60)


# -- (5) per-user daily download-request limit ---------------------------------


class TestPerUserDailyLimit(ReviewOutputEndpointTestBase):
    def test_max_daily_reviews_plus_one_gets_429(self):
        output_key = f"outputs/{AUTH_REVIEW_ID}/out.docx"
        self._put_output_object(output_key)
        self._seed_review(AUTH_REVIEW_ID, owner_sub=OWNER_SUB, output_s3_key=output_key)
        self._authenticate_as(OWNER_SUB)

        for i in range(download_module.MAX_DAILY_REVIEWS):
            resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")
            self.assertEqual(
                resp.status_code, 200, f"request {i + 1} should succeed, got {resp.status_code}"
            )

        resp = self.client.get(f"/api/reviews/{AUTH_REVIEW_ID}/output")
        self.assertEqual(
            resp.status_code,
            429,
            f"request {download_module.MAX_DAILY_REVIEWS + 1} must be rejected with 429",
        )


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestKeyDerivedFromRecordNotRequest,
        TestKeyBindingEnforcedAtEndpoint,
        TestOwnerAdminGate,
        TestCacheControlAndTtl,
        TestPerUserDailyLimit,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
