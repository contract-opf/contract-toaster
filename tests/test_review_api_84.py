#!/usr/bin/env python3
"""
Executable tests for issue #84: Review API real submit/list/detail/download
with idempotency, auth, and defined failure copy.

Drives the real `src.review_routes.router` end-to-end via a FastAPI
`TestClient`, mounted on a local `FastAPI()` app (NOT `src.main.app` --
issue #186, which depends on this ticket, owns mounting this router onto
the shipped app plus the minimal frontend UI; see review_routes.py's module
docstring). AWS is mocked:
  - S3 (uploads + outputs buckets) via real `moto.mock_aws`.
  - DynamoDB via the in-memory `FakeDynamoDBResource` established by
    tests/test_review_submission_e2e.py and reused by
    tests/test_active_bundle_resolver_194.py -- real moto 5.2.2 cannot parse
    `reserve_spend`'s atomic ConditionExpression (arithmetic inside an OR;
    see that file's FakeTable docstring for the verified moto error), so
    every test that reaches `submit_review` needs the fake, not moto, for
    DynamoDB.

No live Bedrock/network: these routes never invoke a model directly (the
pipeline stages that do are started asynchronously via Step Functions,
itself faked here the same way tests/test_review_submission_e2e.py fakes
it) -- so `src.model_client.FakeBedrockClient` has nothing to stand in for
on this synchronous request path.

Covers the issue's "Required verification" contract assertions:
  (1) a duplicate submission collides idempotently -- both the
      client-supplied-key path and the derived-key (no client key) path.
  (2) a non-owner gets 404 on GET /api/reviews/{id} detail and 403 on
      /output (see review_routes.py's docstring for why the two routes
      deliberately differ: detail hides existence, output preserves
      download.py's existing, separately-tested 403).
  (3) a cap-exceeded submission surfaces "Daily spend limit reached".
  (4) the no-active-bundle state surfaces its defined 503 "no active
      playbook" refusal (issue #41).
  (5) a form-match short-circuit (issue #18) surfaces the fixed
      MANUAL_REVIEW_REQUIRED user-facing message.
  (6) the detail result payload schema includes provenance (per-issue),
      critic deltas, and confidence band (#35/#36).
  (7) a download writes an audit row; the presigned URL's TTL is short and
      bounded (contract-level "expires" check -- same convention
      tests/test_download_auth_attack.py already established, rather than
      a live wall-clock HTTP fetch through moto).

This test MUST FAIL on the pre-fix tree (src/review_routes.py does not
exist) and PASS after the fix. Run standalone: `python tests/test_review_api_84.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import io
import os
import sys
import time
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
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import seed_active_bundle  # noqa: E402
import src.download as download_module  # noqa: E402
import src.review_routes as review_routes  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

PLAYBOOK_ID = "eiaa"
UNSEEDED_PLAYBOOK_ID = "no-active-bundle-for-this-one"


# ---------------------------------------------------------------------------
# Minimal, well-formed, benign .docx bytes (same shape as
# tests/test_upload_hostile_file_gauntlet.py's `_build_valid_docx`) -- passes
# the hostile-file gauntlet's magic-number / MIME / entity / relationship
# checks so the submission path under test is idempotency+auth, not the
# gauntlet itself (that is tests/test_upload_hostile_file_gauntlet.py's job).
# ---------------------------------------------------------------------------

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
# In-memory DynamoDB fake -- see module docstring for why real moto cannot
# be used here. Reused verbatim (shape) from
# tests/test_active_bundle_resolver_194.py's FakeTable/FakeDynamoDBResource.
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
# Router-mounted sanity check (independent of auth outcome).
# ---------------------------------------------------------------------------


class TestRoutesExist(unittest.TestCase):
    def test_all_four_routes_registered(self):
        registered = {
            (getattr(r, "path", None), method)
            for r in review_routes.router.routes
            for method in getattr(r, "methods", set())
        }
        self.assertIn(("/api/reviews", "POST"), registered)
        self.assertIn(("/api/reviews", "GET"), registered)
        self.assertIn(("/api/reviews/{review_id}", "GET"), registered)
        self.assertIn(("/api/reviews/{review_id}/output", "GET"), registered)


# ---------------------------------------------------------------------------
# Shared test base: local FastAPI app mounting the real router, AWS faked.
# ---------------------------------------------------------------------------


class ReviewApiTestBase(unittest.TestCase):
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

        self.app = FastAPI()
        self.app.include_router(review_routes.router)
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

    def _submissions_table(self):
        return self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])

    def _audit_table(self):
        return self.ddb.Table(os.environ["AUDIT_TABLE"])


# -- (1) duplicate submission collides idempotently --------------------------


class TestDuplicateSubmissionCollides(ReviewApiTestBase):
    def test_client_supplied_key_collides(self):
        first = self._submit("owner-dup-a", idempotency_key="fixed-key-a")
        second = self._submit("owner-dup-a", idempotency_key="fixed-key-a")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["review_id"], second.json()["review_id"])
        self.assertFalse(first.json()["resumed"])
        self.assertTrue(second.json()["resumed"])
        self.assertEqual(len(self._submissions_table().items), 1)
        self.assertEqual(len(self._reviews_table().items), 1)

    def test_derived_key_same_bucket_collides(self):
        """No client key: identical owner/file/bundle within the same
        timestamp bucket must collide on the derived key (the OTHER
        idempotency-key path, per issue #59's reconciled spec)."""
        first = self._submit("owner-dup-b", body_text="same body")
        second = self._submit("owner-dup-b", body_text="same body")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["review_id"], second.json()["review_id"])
        self.assertEqual(len(self._reviews_table().items), 1)

        # Only ONE upload object was actually written -- the duplicate never
        # re-uploaded to S3.
        review_id = first.json()["review_id"]
        objects = self.s3.list_objects_v2(
            Bucket=os.environ["UPLOADS_BUCKET"], Prefix=f"uploads/owner-dup-b/{review_id}/"
        )
        self.assertEqual(objects.get("KeyCount", 0), 1)


# -- (2) non-owner: 404 on detail, 403 on output ------------------------------


class TestNonOwnerAuth(ReviewApiTestBase):
    def setUp(self):
        super().setUp()
        submit_resp = self._submit("owner-auth", idempotency_key="auth-owner-key")
        self.review_id = submit_resp.json()["review_id"]

    def test_non_owner_gets_404_on_detail(self):
        self._authenticate_as("attacker")
        resp = self.client.get(f"/api/reviews/{self.review_id}")
        self.assertEqual(resp.status_code, 404)

    def test_owner_gets_200_on_detail(self):
        self._authenticate_as("owner-auth")
        resp = self.client.get(f"/api/reviews/{self.review_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["review_id"], self.review_id)

    def test_admin_gets_200_on_detail_for_someone_elses_review(self):
        self._authenticate_as("admin-user", is_admin=True)
        resp = self.client.get(f"/api/reviews/{self.review_id}")
        self.assertEqual(resp.status_code, 200)

    def test_unknown_review_id_also_404s(self):
        """The non-owner 404 and the unknown-id 404 must be
        indistinguishable -- non-enumerable review ids."""
        self._authenticate_as("attacker")
        resp = self.client.get("/api/reviews/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_non_owner_gets_403_on_output(self):
        self._authenticate_as("attacker")
        resp = self.client.get(f"/api/reviews/{self.review_id}/output")
        self.assertEqual(resp.status_code, 403)


# -- (3) cap-exceeded -> "daily limit reached" --------------------------------


class TestCapExceeded(ReviewApiTestBase):
    def test_cap_exceeded_returns_429_daily_limit_reached(self):
        cap_cents = reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT
        spend_date = time.strftime("%Y-%m-%d", time.gmtime())
        self.ddb.Table(os.environ["DAILY_SPEND_TABLE"]).items[spend_date] = {
            "spend_date": spend_date,
            "reserved_usd_cents": cap_cents,
            "daily_cap_usd_cents": cap_cents,
        }

        resp = self._submit("owner-cap", body_text="cap test unique body")

        self.assertEqual(resp.status_code, 429)
        self.assertIn("Daily spend limit reached", resp.json()["detail"])


# -- (4) no-active-bundle -> 503 "no active playbook" -------------------------


class TestNoActiveBundle(ReviewApiTestBase):
    def test_unseeded_playbook_returns_503_no_active_playbook(self):
        resp = self._submit("owner-noactive", playbook_id=UNSEEDED_PLAYBOOK_ID)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "no active playbook")
        # Nothing was created: the refusal fires before any submission
        # record or reviews row.
        self.assertEqual(self._reviews_table().items, {})


# -- (5) form-match short-circuit (#18) surfaces the fixed message -----------


class TestManualReviewRequiredMessage(ReviewApiTestBase):
    def test_form_match_short_circuit_surfaces_fixed_message(self):
        review_id = "review-form-match"
        self._reviews_table().items[review_id] = {
            "review_id": review_id,
            "owner_sub": "owner-form-match",
            "status": "MANUAL_REVIEW_REQUIRED",
            "reason": "not_derivative_of_standard_form",
            "playbook_id": PLAYBOOK_ID,
            "created_at": "1000",
            "updated_at": "1000",
        }

        self._authenticate_as("owner-form-match")
        resp = self.client.get(f"/api/reviews/{review_id}")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(body["reason"], "not_derivative_of_standard_form")
        self.assertEqual(
            body["message"], reviews_module.STATUS_USER_MESSAGES["MANUAL_REVIEW_REQUIRED"]
        )
        self.assertIn("legal admin will review it", body["message"])
        # De-brand posture: user-facing copy must never say Exos/EXOS.
        self.assertNotIn("Exos", body["message"])
        self.assertNotIn("EXOS", body["message"])


# -- (6) detail schema includes provenance / critic deltas / confidence -----


class TestResultPayloadSchema(ReviewApiTestBase):
    def test_detail_includes_provenance_critic_delta_confidence_band(self):
        review_id = "review-done-rich"
        self._reviews_table().items[review_id] = {
            "review_id": review_id,
            "owner_sub": "owner-rich",
            "status": "DONE",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "LOW_CONFIDENCE",
            "confidence_band": "LOW_CONFIDENCE",
            "issues": [
                {
                    "section_ref": "8 Limitation on Liability",
                    "section_title": "Limitation on Liability",
                    "counterparty_change_summary": "Cap removed.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Standard cap required.",
                    "proposed_replacement_text": "...",
                    "playbook_topic_id": "liability-cap",
                    "internal_precedent_citation": None,
                    "provenance": "detector:liability-cap-removed",
                }
            ],
            "critic_delta": {
                "added_issues": [],
                "contested_replacements": [
                    {
                        "section_ref": "8 Limitation on Liability",
                        "primary_replacement_text": "...",
                        "critic_objection": "Drifts from playbook position.",
                    }
                ],
                "rationale_objections": [],
            },
            "verdict_summary": None,
            "playbook_id": PLAYBOOK_ID,
            "created_at": "2000",
            "updated_at": "2000",
        }

        self._authenticate_as("owner-rich")
        resp = self.client.get(f"/api/reviews/{review_id}")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["confidence_band"], "LOW_CONFIDENCE")
        self.assertEqual(body["issues"][0]["provenance"], "detector:liability-cap-removed")
        self.assertEqual(
            body["critic_delta"]["contested_replacements"][0]["critic_objection"],
            "Drifts from playbook position.",
        )


# -- (7) download writes an audit row; presigned URL is short-lived ---------


class TestDownloadAudit(ReviewApiTestBase):
    def test_download_writes_audit_row_and_url_is_short_lived(self):
        review_id = "review-with-output"
        # download.py's _validate_s3_key_bound_to_review requires the key be
        # scoped to exactly outputs/<review_id>/ (see that module's docstring
        # -- AC2 IDOR / path-traversal defence), not outputs/<owner_sub>/<review_id>/.
        output_key = f"outputs/{review_id}/out.docx"
        self.s3.put_object(
            Bucket=os.environ["S3_OUTPUTS_BUCKET"], Key=output_key, Body=b"redline bytes"
        )
        self._reviews_table().items[review_id] = {
            "review_id": review_id,
            "owner_sub": "owner-dl",
            "status": "DONE",
            "decision": "REQUEST_CHANGE",
            "output_s3_key": output_key,
            "playbook_id": PLAYBOOK_ID,
            "created_at": "3000",
            "updated_at": "3000",
        }

        self._authenticate_as("owner-dl")
        resp = self.client.get(f"/api/reviews/{review_id}/output")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("url", body)
        self.assertEqual(body["expires_in"], download_module.PRESIGNED_URL_TTL_SECONDS)
        self.assertGreater(download_module.PRESIGNED_URL_TTL_SECONDS, 0)
        self.assertLessEqual(
            download_module.PRESIGNED_URL_TTL_SECONDS, 300,
            "Presigned URL TTL must be short-lived so a leaked/expired URL fails quickly.",
        )
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

        audit_items = list(self._audit_table().items.values())
        matching = [
            i for i in audit_items
            if i.get("action") == "review_output_downloaded" and i.get("target") == review_id
        ]
        self.assertEqual(
            len(matching), 1, "Exactly one audit row for this successful download."
        )
        self.assertEqual(matching[0]["actor"], "owner-dl")

    def test_no_output_yet_is_404(self):
        review_id = "review-pending"
        self._reviews_table().items[review_id] = {
            "review_id": review_id,
            "owner_sub": "owner-pending",
            "status": "RUNNING",
            "playbook_id": PLAYBOOK_ID,
            "created_at": "4000",
            "updated_at": "4000",
        }
        self._authenticate_as("owner-pending")
        resp = self.client.get(f"/api/reviews/{review_id}/output")
        self.assertEqual(resp.status_code, 404)


# -- list route sanity (owner-scoped vs admin-all) ----------------------------


class TestListReviews(ReviewApiTestBase):
    def test_owner_sees_only_own_reviews(self):
        self._reviews_table().items["r1"] = {
            "review_id": "r1", "owner_sub": "owner-list-a", "status": "DONE",
            "playbook_id": PLAYBOOK_ID, "created_at": "1", "updated_at": "1",
        }
        self._reviews_table().items["r2"] = {
            "review_id": "r2", "owner_sub": "owner-list-b", "status": "DONE",
            "playbook_id": PLAYBOOK_ID, "created_at": "2", "updated_at": "2",
        }

        self._authenticate_as("owner-list-a")
        resp = self.client.get("/api/reviews")

        self.assertEqual(resp.status_code, 200)
        ids = {r["review_id"] for r in resp.json()["reviews"]}
        self.assertEqual(ids, {"r1"})

    def test_admin_sees_all_reviews(self):
        self._reviews_table().items["r1"] = {
            "review_id": "r1", "owner_sub": "owner-list-a", "status": "DONE",
            "playbook_id": PLAYBOOK_ID, "created_at": "1", "updated_at": "1",
        }
        self._reviews_table().items["r2"] = {
            "review_id": "r2", "owner_sub": "owner-list-b", "status": "DONE",
            "playbook_id": PLAYBOOK_ID, "created_at": "2", "updated_at": "2",
        }

        self._authenticate_as("admin-user", is_admin=True)
        resp = self.client.get("/api/reviews")

        self.assertEqual(resp.status_code, 200)
        ids = {r["review_id"] for r in resp.json()["reviews"]}
        self.assertEqual(ids, {"r1", "r2"})


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestRoutesExist,
        TestDuplicateSubmissionCollides,
        TestNonOwnerAuth,
        TestCapExceeded,
        TestNoActiveBundle,
        TestManualReviewRequiredMessage,
        TestResultPayloadSchema,
        TestDownloadAudit,
        TestListReviews,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
