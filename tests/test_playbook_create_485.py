#!/usr/bin/env python3
"""
Executable tests for issue #485 blocker 1: `POST /api/admin/playbooks`, the
route that creates a brand-new playbook_id AND its first version, atomically.

## Root problem this proves fixed

Before this route, the only way a new `playbook_id` could come to exist was
a `playbooks/registry.json` entry -- a file baked into the Docker image.
`POST /api/admin/playbooks/{playbook_id}/versions` (issue #430/#478) can
only ever ADD a version to a `playbook_id` that already exists somewhere
(the registry, or a playbook this route itself already created); it takes
`playbook_id` as a URL path parameter, so there was no way for an admin to
install a genuinely new contract type without a new image.

This route derives the new playbook_id from the uploaded OPF document's own
`agreement_type.id` (never operator free-text -- `scripts/opf_load.py::
agreement_type_keys`), refuses a document whose identity already matches a
registered playbook (`scripts/opf_load.py::match_registry_playbook`,
previously dead code) or an already-created DB playbook, and otherwise
reuses the exact same tested validate/store engine
(`backend/src/playbook_upload.py`) the existing versions-upload route uses.

## Fixture note

Same convention as tests/test_playbook_upload_478.py: the committed gold
OPF 0.3 fixture (`tests/gold-fixtures-opf/acme-university.opf.json`,
agreement_type.id "educational-affiliation", aliases ["acme-university",
"eiaa-fixture"]) stands in for a real playbook-engine artifact. That id is
NOT a registered playbook_id anywhere in the real `playbooks/registry.json`
(which only lists "synthetic-generic" and "synthetic-nda-sample"), so
uploading it through this route is a genuine "brand-new playbook_id" case
end-to-end, against the REAL registry file (no synthetic override) --
mirroring tests/test_playbook_catalog_endpoint.py's
`RealCatalogShipsExactlyOneSampleTest`, since `match_registry_playbook` and
`_require_registered_playbook` both resolve against the real, non-injectable
registry path.

This test MUST FAIL on the pre-fix tree (`POST /api/admin/playbooks` does
not exist -- 404) and PASS after the fix.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
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

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-create485-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-create485-test"
)
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-create485-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-create485-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-create485-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import opf_canonicalize  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.playbook_versions as pv  # noqa: E402

OPF03_JSON_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
OPF03_HTML_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.html"
V1_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

# The document's own agreement_type.id (the identity `POST /api/admin/
# playbooks` derives the new playbook_id from) -- NOT a registered
# playbook_id in the real playbooks/registry.json.
DERIVED_PLAYBOOK_ID = "educational-affiliation"

ADMIN_SUB = "admin-1"
NON_ADMIN_SUB = "reviewer-1"

CREATE_PATH = "/api/admin/playbooks"


def _opf03_doc() -> dict:
    return json.loads(OPF03_JSON_PATH.read_text(encoding="utf-8"))


def _reseal(doc: dict) -> dict:
    """Recompute identity.content_hash (+ section digests) after mutating
    *doc* so it hashes honestly again -- same technique as
    tests/test_playbook_upload_478.py::_reseal."""
    doc = copy.deepcopy(doc)
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    doc["identity"]["section_digests"] = opf_canonicalize.compute_section_digests(doc)
    return doc


def _valid_v1_bytes() -> bytes:
    with open(V1_FIXTURE_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    return json.dumps(doc).encode("utf-8")


def _put_user(table, sub: str, is_admin: bool) -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": "active",
            "is_admin": is_admin,
        }
    )


class PlaybookCreateTestBase(unittest.TestCase):
    def setUp(self):
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

        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])
        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.ddb
        )
        backend_main.app.dependency_overrides[backend_main.get_s3_client] = lambda: self.s3
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": ADMIN_SUB, "email": f"{ADMIN_SUB}@example.com", "token_use": "access"}
        )

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()

    def _authenticate_as(self, sub: str) -> None:
        backend_main.app.dependency_overrides[backend_main.get_current_user] = (
            lambda: {"sub": sub, "email": f"{sub}@example.com", "token_use": "access"}
        )

    def _create(self, content: bytes, filename: str, version: str = "1.0.0", **form):
        data: dict[str, str] = {"version": version, **form}
        return self.client.post(
            CREATE_PATH,
            files={"file": (filename, content, "application/octet-stream")},
            data=data,
        )


class TestCreateNewOpfPlaybookHappyPath(PlaybookCreateTestBase):
    def test_derives_identity_from_the_artifact_and_creates_playbook_and_version(self):
        doc_bytes = json.dumps(_opf03_doc()).encode("utf-8")
        resp = self._create(doc_bytes, "playbook.opf.json", version="1.0.0")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()

        # Identity came from the document, never from an operator-supplied
        # path parameter (this route has none) -- the response tells the
        # caller what playbook_id was actually created.
        self.assertEqual(body["playbook_id"], DERIVED_PLAYBOOK_ID)
        self.assertEqual(body["version"], "1.0.0")
        self.assertEqual(body["status"], pv.STATUS_DRAFT)
        self.assertEqual(body["artifact_kind"], "opf-0.3")
        self.assertIsNotNone(body["storage_key"])
        self.assertTrue(body["storage_key"].startswith(f"playbooks/{DERIVED_PLAYBOOK_ID}/"))

        # The version row landed for real, exactly as record_playbook_
        # version_upload's own contract promises for the existing route.
        row = self.versions_table.get_item(
            Key={"playbook_id": DERIVED_PLAYBOOK_ID, "version": "1.0.0"}
        ).get("Item")
        self.assertIsNotNone(row)
        self.assertEqual(row["uploaded_by"], ADMIN_SUB)

    def test_html_bundle_form_is_also_accepted(self):
        resp = self._create(
            OPF03_HTML_PATH.read_bytes(), "playbook.opf.html", version="1.0.0"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["playbook_id"], DERIVED_PLAYBOOK_ID)
        self.assertEqual(resp.json()["artifact_kind"], "opf-0.3")


class TestCreateRequiresAdmin(PlaybookCreateTestBase):
    def test_non_admin_gets_403(self):
        self._authenticate_as(NON_ADMIN_SUB)
        doc_bytes = json.dumps(_opf03_doc()).encode("utf-8")
        resp = self._create(doc_bytes, "playbook.opf.json")
        self.assertEqual(resp.status_code, 403)


class TestCreateRejectsLegacyV1(PlaybookCreateTestBase):
    def test_legacy_v1_json_is_refused_since_it_has_no_identity_to_derive(self):
        resp = self._create(_valid_v1_bytes(), "playbook.json")
        self.assertEqual(resp.status_code, 400)
        detail = resp.json()["detail"]
        self.assertIn("OPF", detail)
        # No row should ever land for a refused creation.
        scan = self.versions_table.scan()
        self.assertEqual(scan["Items"], [])


class TestCreateRejectsRegistryConflict(PlaybookCreateTestBase):
    def test_agreement_type_matching_a_registered_playbook_is_refused(self):
        doc = _opf03_doc()
        # Retarget this document's identity at a REAL, registered
        # playbook_id (the actual playbooks/registry.json this route
        # checks against, not a synthetic override).
        doc["agreement_type"]["id"] = "synthetic-generic"
        doc["agreement_type"]["aliases"] = []
        doc = _reseal(doc)

        resp = self._create(json.dumps(doc).encode("utf-8"), "playbook.opf.json")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn("synthetic-generic", resp.json()["detail"])

        # Nothing was created for the conflicting id.
        self.assertEqual(
            self.versions_table.scan()["Items"],
            [],
        )


class TestCreateRejectsDbConflict(PlaybookCreateTestBase):
    def test_second_create_for_the_same_derived_id_is_refused(self):
        doc_bytes = json.dumps(_opf03_doc()).encode("utf-8")
        first = self._create(doc_bytes, "playbook.opf.json", version="1.0.0")
        self.assertEqual(first.status_code, 200, first.text)

        second = self._create(doc_bytes, "playbook.opf.json", version="2.0.0")
        self.assertEqual(second.status_code, 409, second.text)
        self.assertIn(DERIVED_PLAYBOOK_ID, second.json()["detail"])
        self.assertIn("already exists", second.json()["detail"])

        # Only the first version landed; the "create" attempt never wrote a
        # second row for the already-existing playbook_id.
        self.assertIsNone(
            self.versions_table.get_item(
                Key={"playbook_id": DERIVED_PLAYBOOK_ID, "version": "2.0.0"}
            ).get("Item")
        )


class TestCreateStubBasisWatermark(PlaybookCreateTestBase):
    """Proves `_finish_opf` is genuinely reused (not bypassed) by the create
    route -- same stub-basis gate `TestOpfHtmlBundleUpload`'s sibling class
    in tests/test_playbook_upload_478.py exercises against the existing
    versions-upload route."""

    def test_stub_basis_watermark_is_refused_without_accept_and_allowed_with_it(self):
        doc = _opf03_doc()
        doc["compiler"]["stub_basis_present"] = True
        doc = _reseal(doc)
        doc_bytes = json.dumps(doc).encode("utf-8")

        refused = self._create(doc_bytes, "playbook.opf.json", version="1.0.0")
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("stub_basis_present", refused.json()["detail"])
        self.assertEqual(self.versions_table.scan()["Items"], [])

        accepted = self._create(
            doc_bytes, "playbook.opf.json", version="1.0.0", accept_stub_basis="true"
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["accepted_stub_basis"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
