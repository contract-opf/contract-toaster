#!/usr/bin/env python3
"""
Executable tests for issue #186: no user-facing review flow exists because
the fully-implemented review-API handlers (`src.review_routes.router`,
built by issue #84) were never mounted onto the shipped app
(`src.main.app`).

Unlike tests/test_review_api_84.py (which mounts the router on a throwaway
local `FastAPI()` instance to test the handlers themselves), this test
drives `src.main.app` -- the REAL, shipped application object App Runner
serves -- via a FastAPI `TestClient`, confirming the routes are reachable
on the actual app, not just implemented in an unmounted module.

AWS is mocked:
  - S3 (uploads + outputs buckets) via real `moto.mock_aws`.
  - DynamoDB via the same in-memory `FakeDynamoDBResource` fake
    tests/test_review_api_84.py and tests/test_active_bundle_resolver_194.py
    use (real moto 5.2.2 cannot parse `reserve_spend`'s atomic
    ConditionExpression -- see that file's FakeTable docstring).

No live Bedrock/network: `POST /api/reviews` starts the pipeline
asynchronously via (faked) Step Functions; it never calls a model directly
on this synchronous request path, so `src.model_client.FakeBedrockClient`
has nothing to stand in for here.

Per issue #186's "Required verification":
  (1) POST /api/reviews is mounted on src.main.app, accepts a multipart
      .docx, runs run_upload_gauntlet, writes to
      s3://uploads/{sub}/{review_id}/in.docx, and calls submit_review.
  (2) GET /api/reviews/{id} is mounted and returns get_review_status
      (reviews.get_review_detail)'s payload.
  (3) GET /api/reviews/{id}/output is mounted and returns the scoped
      presigned/streamed download.
  (4) src.main.app's registered route set now includes these paths (the
      pre-fix tree registers only /health, /version, /whoami, /api/users*,
      /api/admin/retention*, /api/corpus).

This test MUST FAIL on the pre-fix tree (src/main.py never imports or
mounts src.review_routes.router) and PASS after the fix. Run standalone:
`python tests/test_review_routes_mounted_186.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import io
import os
import sys
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("S3_OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import seed_active_bundle  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.review_routes as review_routes  # noqa: E402

PLAYBOOK_ID = "eiaa"

_CONTENT_TYPES_WORDPROCESSINGML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS_XML_BENIGN = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _valid_docx_bytes(body_text: str = "Hello") -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{body_text}</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_WORDPROCESSINGML)
        zf.writestr("_rels/.rels", _RELS_XML_BENIGN)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# In-memory DynamoDB fake -- see module docstring; identical shape to
# tests/test_review_api_84.py's FakeTable/FakeDynamoDBResource (real moto
# cannot parse reserve_spend's atomic ConditionExpression).
# ---------------------------------------------------------------------------


class FakeTable:
    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        if ConditionExpression == "attribute_not_exists(idempotency_key)" and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.items[key] = dict(Item)

    def scan(self):
        return {"Items": list(self.items.values())}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
    ):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))
        vals = ExpressionAttributeValues or {}

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return

        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return

        # Generic fallback: no-op for anything else exercised indirectly.


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            key_name = {
                os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ["DAILY_SPEND_TABLE"]: "spend_date",
                os.environ["PLAYBOOKS_TABLE"]: "playbook_id",
                os.environ["AUDIT_TABLE"]: "timestamp",
            }.get(name, "id")
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class ExecutionAlreadyExists(Exception):
    pass


class FakeSfnExceptions:
    ExecutionAlreadyExists = ExecutionAlreadyExists


class FakeSfnClient:
    def __init__(self):
        self.exceptions = FakeSfnExceptions()
        self.started_names: set[str] = set()
        self.start_execution_call_count = 0

    def start_execution(self, stateMachineArn, name, input):
        self.start_execution_call_count += 1
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"}


class FakeUsersDynamoDBClient:
    """Minimal boto3-DynamoDB-CLIENT-shaped fake for
    download.py::_check_per_user_limits (a raw client call, distinct from
    the resource-style FakeDynamoDBResource above)."""

    PARTITION_KEY = "cognito_sub"

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def update_item(self, **kwargs):
        key = kwargs["Key"]
        item_key = key[self.PARTITION_KEY]["S"]
        item = self._items.setdefault(item_key, {})

        expr_names = kwargs.get("ExpressionAttributeNames", {})
        expr_values = kwargs.get("ExpressionAttributeValues", {})
        cond_expr = kwargs.get("ConditionExpression")
        day_attr = expr_names.get("#day")

        current = int(item.get(day_attr, 0))
        if cond_expr:
            max_daily = int(expr_values.get(":maxDaily", {}).get("N", "0"))
            if day_attr in item and not (current < max_daily):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
        item[day_attr] = current + 1
        return {}


def _caller_row(sub: str, is_admin: bool = False) -> dict:
    return {
        "cognito_sub": sub,
        "email": f"{sub}@teamexos.com",
        "status": "active",
        "is_admin": is_admin,
    }


# ---------------------------------------------------------------------------
# (4) src.main.app's registered route set now includes the review paths.
# ---------------------------------------------------------------------------


class TestReviewRoutesMountedOnMainApp(unittest.TestCase):
    def test_registered_route_set_includes_review_paths(self):
        registered = {
            (getattr(r, "path", None), method)
            for r in backend_main.app.routes
            for method in getattr(r, "methods", set())
        }
        self.assertIn(("/api/reviews", "POST"), registered)
        self.assertIn(("/api/reviews", "GET"), registered)
        self.assertIn(("/api/reviews/{review_id}", "GET"), registered)
        self.assertIn(("/api/reviews/{review_id}/output", "GET"), registered)

    def test_pre_existing_routes_are_still_mounted(self):
        """Mounting the review router must not disturb the pre-existing
        route set (issue body: "the current tree registers only /health,
        /version, /whoami, /api/users*, /api/admin/retention*")."""
        registered_paths = {getattr(r, "path", None) for r in backend_main.app.routes}
        for path in ("/health", "/version", "/whoami", "/api/users", "/api/admin/retention"):
            self.assertIn(path, registered_paths)


# ---------------------------------------------------------------------------
# Shared test base: the REAL src.main.app, AWS faked, review_routes'
# per-module dependencies overridden the same way tests/test_review_api_84.py
# overrides them (the router's dependency functions are distinct objects
# from src.main's identically-named ones -- see review_routes.py's module
# docstring on why they are deliberately duplicated rather than shared).
# ---------------------------------------------------------------------------


class ReviewApiOnMainAppTestBase(unittest.TestCase):
    def setUp(self):
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.s3.create_bucket(Bucket=os.environ["S3_OUTPUTS_BUCKET"])

        self.ddb = FakeDynamoDBResource()
        self.sfn = FakeSfnClient()
        self.users_ddb_client = FakeUsersDynamoDBClient()

        self.seeded_bundle_hash = seed_active_bundle.seed_active_bundle(PLAYBOOK_ID, self.ddb)

        self.app = backend_main.app
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[review_routes.get_s3_client] = lambda: self.s3
        self.app.dependency_overrides[review_routes.get_sfn_client] = lambda: self.sfn
        self.app.dependency_overrides[review_routes.get_dynamodb_client] = (
            lambda: self.users_ddb_client
        )
        self.app.dependency_overrides[review_routes.get_env_name] = lambda: "dev"
        self.app.dependency_overrides[review_routes.get_av_client] = (
            lambda: review_routes.NullAvClient()
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str, is_admin: bool = False) -> dict:
        row = _caller_row(sub, is_admin=is_admin)
        self.app.dependency_overrides[review_routes.get_active_user_row] = lambda: row
        return row

    def _submit(self, owner_sub: str, *, body_text: str = "Hello", playbook_id: str = PLAYBOOK_ID,
                idempotency_key: str | None = None):
        self._authenticate_as(owner_sub)
        data = {"playbook_id": playbook_id}
        if idempotency_key is not None:
            data["idempotency_key"] = idempotency_key
        return self.client.post(
            "/api/reviews",
            files={
                "file": (
                    "in.docx",
                    _valid_docx_bytes(body_text),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data=data,
        )

    def _reviews_table(self):
        return self.ddb.Table(os.environ["REVIEWS_TABLE"])


# -- (1) POST /api/reviews: gauntlet runs, S3 write happens, submit_review is called --


class TestPostReviewsEndToEnd(ReviewApiOnMainAppTestBase):
    def test_upload_runs_gauntlet_writes_s3_and_submits(self):
        resp = self._submit("owner-186", idempotency_key="key-186-a")

        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("review_id", body)
        self.assertFalse(body["resumed"])

        review_id = body["review_id"]

        # run_upload_gauntlet ran and the object was actually written to
        # s3://uploads/{sub}/{review_id}/in.docx.
        obj = self.s3.get_object(
            Bucket=os.environ["UPLOADS_BUCKET"], Key=f"uploads/owner-186/{review_id}/in.docx"
        )
        self.assertEqual(obj["Body"].read(), _valid_docx_bytes("Hello"))

        # submit_review was called: a reviews row now exists for this id.
        self.assertIn(review_id, self._reviews_table().items)
        self.assertEqual(self._reviews_table().items[review_id]["owner_sub"], "owner-186")

    def test_hostile_upload_is_rejected_before_submission(self):
        """A non-.docx (hostile-gauntlet failure) upload never reaches
        submit_review -- run_upload_gauntlet is genuinely wired in."""
        self._authenticate_as("owner-186-hostile")
        resp = self.client.post(
            "/api/reviews",
            files={"file": ("evil.docx", b"not a real docx", "application/octet-stream")},
            data={"playbook_id": PLAYBOOK_ID},
        )
        self.assertGreaterEqual(resp.status_code, 400)
        self.assertLess(resp.status_code, 500)
        self.assertEqual(self._reviews_table().items, {})


# -- (2) GET /api/reviews/{id} returns get_review_status's payload -----------


class TestGetReviewStatus(ReviewApiOnMainAppTestBase):
    def test_get_review_status_returns_submitted_review(self):
        submit_resp = self._submit("owner-186-status", idempotency_key="key-186-status")
        review_id = submit_resp.json()["review_id"]

        self._authenticate_as("owner-186-status")
        resp = self.client.get(f"/api/reviews/{review_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["review_id"], review_id)
        self.assertIn("status", resp.json())


# -- (3) GET /api/reviews/{id}/output returns the scoped download ------------


class TestGetReviewOutput(ReviewApiOnMainAppTestBase):
    def test_get_review_output_returns_presigned_download(self):
        review_id = "review-186-output"
        output_key = f"outputs/{review_id}/out.docx"
        self.s3.put_object(
            Bucket=os.environ["S3_OUTPUTS_BUCKET"], Key=output_key, Body=b"redline bytes"
        )
        self._reviews_table().items[review_id] = {
            "review_id": review_id,
            "owner_sub": "owner-186-dl",
            "status": "DONE",
            "decision": "REQUEST_CHANGE",
            "output_s3_key": output_key,
            "playbook_id": PLAYBOOK_ID,
            "created_at": "3000",
            "updated_at": "3000",
        }

        self._authenticate_as("owner-186-dl")
        resp = self.client.get(f"/api/reviews/{review_id}/output")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("url", resp.json())
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

    def test_non_owner_denied_on_output(self):
        review_id = "review-186-output-2"
        self._reviews_table().items[review_id] = {
            "review_id": review_id,
            "owner_sub": "owner-186-dl-2",
            "status": "DONE",
            "output_s3_key": f"outputs/{review_id}/out.docx",
            "playbook_id": PLAYBOOK_ID,
            "created_at": "3000",
            "updated_at": "3000",
        }
        self._authenticate_as("attacker-186")
        resp = self.client.get(f"/api/reviews/{review_id}/output")
        self.assertEqual(resp.status_code, 403)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestReviewRoutesMountedOnMainApp,
        TestPostReviewsEndToEnd,
        TestGetReviewStatus,
        TestGetReviewOutput,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
