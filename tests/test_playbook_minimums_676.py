#!/usr/bin/env python3
"""
Executable tests for issue #676: reject an OPF document that omits a
spec-normative minimum, starting with `perspective`.

## What is being gated, and why here

`playbooks/opf/playbook.schema-0.3.json` describes `/perspective` as
"Whose perspective this playbook is reviewed 'as' -- an open-standard OPF
instance must say who 'us' is", but leaves it OUT of the schema's own
`required` list (verified in this file by
`TestSchemaPremise.test_perspective_is_normative_prose_but_not_schema_required`
-- if a future schema bump ever adds it to `required`, that test fails and
this whole gate can be reconsidered rather than quietly duplicating the
schema). Nothing enforced it, so a playbook with no principal reached
review composition and produced the defect #675 documents: a fluent
surgical redline drafted for the counterparty.

The gate lives in `backend/src/playbook_upload.py::_load_opf_from_bytes`,
which is the ONE function BOTH surfaces already share:

  - upload: `validate_playbook_upload` (POST /api/admin/playbooks/{id}
    /versions) and `main.py`'s create-playbook route (POST
    /api/admin/playbooks) both call it;
  - review: `pipeline_runner._load_opf_bundle_if_active` re-validates the
    stored artifact through it on EVERY review rather than trusting the
    activated row.

So one check covers the already-activated artifact too, with no migration
and no separate review-path gate -- which is the whole point, because the
broken playbook is one that is already activated.

## Fixture reachability (nothing here is a shape production cannot make)

Every document under test is a committed gold OPF fixture with its
`perspective` block removed (or blanked) and then RE-SEALED -- its
`identity.content_hash` and `identity.section_digests` recomputed with
`scripts/opf_canonicalize.py`, the same functions the real compiler seals
with. That is not a hand-forged shape: `perspective` is schema-OPTIONAL in
both `playbook.schema-0.2.json` and `playbook.schema-0.3.json`, so
`scripts/opf_load.py::load_opf_document` accepted such a document before
this change and every pre-#676 upload of one succeeded. The
currently-activated production EIAA playbook is exactly this shape (issue
#676, issue #675).

The activated-row half is reached through the real production writers
(`playbook_versions.record_playbook_version_upload` ->
`activate_release_bundle`, Gate 7 included) rather than a hand-built
DynamoDB item. `validate_playbook_upload` is deliberately NOT in that
path for the perspective-less document, because after this change it
refuses it -- and the state under test is precisely the PRE-GATE one that
production is in today: a row recorded when the upload path still accepted
these bytes. `_load_opf_from_bytes` is what must catch it now.

Run standalone: `python3 tests/test_playbook_minimums_676.py`
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_ROOT, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-676-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-676-test"
)
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-676-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-676-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-676-test")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import opf_canonicalize  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.playbook_upload as playbook_upload  # noqa: E402
import src.playbook_versions as playbook_versions  # noqa: E402

OPF_FIXTURE_DIR = REPO_ROOT / "tests" / "gold-fixtures-opf"
FULL_FIXTURE_PATH = OPF_FIXTURE_DIR / "acme-university.opf.json"
REAL_SHAPE_FIXTURE_PATH = OPF_FIXTURE_DIR / "acme-university-real-shape.opf.json"
SCHEMA_03_PATH = REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.3.json"
SCHEMA_02_PATH = REPO_ROOT / "playbooks" / "opf" / "playbook.schema-0.2.json"

PLAYBOOK_ID = "acme-university"  # an agreement_type.aliases entry of both fixtures
VERSIONS_PATH = f"/api/admin/playbooks/{PLAYBOOK_ID}/versions"
CREATE_PATH = "/api/admin/playbooks"
ADMIN_SUB = "admin-676"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _reseal(doc: dict[str, Any]) -> dict[str, Any]:
    """Recompute identity.content_hash + section digests after mutating
    *doc*, with the same `scripts/opf_canonicalize.py` functions the real
    compiler seals with -- so what is under test is a self-consistent
    artifact the loader accepts, never a hash-tampered one that would be
    refused for an unrelated reason."""
    doc = copy.deepcopy(doc)
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    doc["identity"]["section_digests"] = opf_canonicalize.compute_section_digests(doc)
    return doc


def _without_perspective(path: Path = FULL_FIXTURE_PATH) -> dict[str, Any]:
    doc = _load(path)
    doc.pop("perspective", None)
    return _reseal(doc)


def _blank_party(path: Path = FULL_FIXTURE_PATH) -> dict[str, Any]:
    """A `perspective` block that satisfies the schema (both required
    sub-fields present, both strings) yet still names no principal. The
    gate branches on the sub-field values, not just the key, so this
    variant keeps that branch honest -- a key-presence-only check would
    leave it green forever."""
    doc = _load(path)
    doc["perspective"] = {"party": "   ", "counterparty_type": "Educational Institution"}
    return _reseal(doc)


def _blank_counterparty_type(path: Path = FULL_FIXTURE_PATH) -> dict[str, Any]:
    doc = _load(path)
    doc["perspective"] = {"party": "FixtureCorp", "counterparty_type": ""}
    return _reseal(doc)


def _as_bytes(doc: dict[str, Any]) -> bytes:
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


# ---------------------------------------------------------------------------
# The premise this gate rests on
# ---------------------------------------------------------------------------


def _normative_but_optional(schema: dict[str, Any]) -> set[str]:
    """Top-level properties whose own description carries normative
    language (must/required/shall) that the schema's `required` list does
    NOT back -- #676's scoping scan, re-run here rather than trusted."""
    required = set(schema["required"])
    return {
        name
        for name, sub in schema["properties"].items()
        if name not in required
        and re.search(r"\bmust\b|\brequired\b|\bshall\b", (sub.get("description") or "").lower())
    }


class TestSchemaPremise(unittest.TestCase):
    def test_perspective_is_normative_prose_but_not_schema_required(self):
        """#676's scoping claim, asserted rather than trusted: `/perspective`
        carries `must` language in its own description and is NOT in the
        schema's `required` list. Both halves matter -- the first is why a
        gate is warranted, the second is why the schema alone cannot be it."""
        for path in (SCHEMA_03_PATH, SCHEMA_02_PATH):
            with self.subTest(schema=path.name):
                schema = _load(path)
                self.assertIn("perspective", schema["properties"])
                self.assertIn(
                    "must", schema["properties"]["perspective"]["description"].lower()
                )
                self.assertNotIn("perspective", schema["required"])

    def test_the_minimums_table_is_exactly_the_normative_but_optional_set(self):
        """#676: "Keep the list of minimums in ONE named place with a comment
        per entry saying why it is there and where the requirement comes
        from" -- and "the failure mode to avoid is this list quietly growing
        into a second, undocumented schema".

        Asserted as an INVARIANT against both schema files rather than as a
        hardcoded ["perspective"]: the table must hold every top-level field
        the spec's own prose calls mandatory but the schema's `required` list
        does not enforce, and nothing else. A schema bump that promotes
        `perspective` into `required` (making this gate a duplicate), or that
        adds a second such field (making this gate incomplete), turns this
        red instead of drifting silently."""
        expected: set[str] = set()
        for path in (SCHEMA_03_PATH, SCHEMA_02_PATH):
            expected |= _normative_but_optional(_load(path))
        self.assertEqual(expected, {"perspective"})  # the scan behind #676's scoping

        minimums = playbook_upload.OPF_UPLOAD_MINIMUMS
        self.assertEqual({m.field for m in minimums}, expected)
        for minimum in minimums:
            # Every entry states where the requirement comes from and what
            # the field is for -- both are surfaced to the operator.
            self.assertTrue(minimum.requirement_source.strip())
            self.assertTrue(minimum.purpose.strip())


# ---------------------------------------------------------------------------
# Upload surface -- the shared validator
# ---------------------------------------------------------------------------


class TestValidatePlaybookUpload(unittest.TestCase):
    def test_document_without_perspective_is_rejected(self):
        with self.assertRaises(playbook_upload.PlaybookUploadRejected) as ctx:
            playbook_upload.validate_playbook_upload(
                filename="acme-university.opf.json",
                contents=_as_bytes(_without_perspective()),
                playbook_id=PLAYBOOK_ID,
            )
        message = str(ctx.exception)
        self.assertIn("perspective", message)

    def test_rejection_message_names_the_field_and_what_it_is_for(self):
        """#676 verification bar: "Assert the rejection message names the
        field. A gate nobody can act on is a support ticket." """
        with self.assertRaises(playbook_upload.PlaybookUploadRejected) as ctx:
            playbook_upload.validate_playbook_upload(
                filename="acme-university.opf.json",
                contents=_as_bytes(_without_perspective()),
                playbook_id=PLAYBOOK_ID,
            )
        message = str(ctx.exception)
        self.assertIn("/perspective", message)
        # Actionable: it must say what the field is for, not just that a
        # key is absent.
        self.assertIn("party", message)
        self.assertIn("counterparty_type", message)

    def test_blank_party_is_rejected_too(self):
        with self.assertRaises(playbook_upload.PlaybookUploadRejected) as ctx:
            playbook_upload.validate_playbook_upload(
                filename="acme-university.opf.json",
                contents=_as_bytes(_blank_party()),
                playbook_id=PLAYBOOK_ID,
            )
        self.assertIn("perspective", str(ctx.exception))

    def test_blank_counterparty_type_is_rejected_too(self):
        with self.assertRaises(playbook_upload.PlaybookUploadRejected) as ctx:
            playbook_upload.validate_playbook_upload(
                filename="acme-university.opf.json",
                contents=_as_bytes(_blank_counterparty_type()),
                playbook_id=PLAYBOOK_ID,
            )
        self.assertIn("perspective", str(ctx.exception))

    def test_real_shape_document_without_perspective_is_rejected(self):
        """#676's verification bar names "a real-shaped OPF document" --
        `acme-university-real-shape.opf.json` is the empty-posture,
        policy-less twin of the production EIAA artifact (the shape issue
        #479 AC1 runs end-to-end against), so the gate is proven on it and
        not only on the fuller gold fixture."""
        with self.assertRaises(playbook_upload.PlaybookUploadRejected) as ctx:
            playbook_upload.validate_playbook_upload(
                filename="acme-university.opf.json",
                contents=_as_bytes(_without_perspective(REAL_SHAPE_FIXTURE_PATH)),
                playbook_id=PLAYBOOK_ID,
            )
        self.assertIn("/perspective", str(ctx.exception))

    def test_document_with_perspective_still_validates(self):
        """#676: "prove you added a gate, not a wall"."""
        validated = playbook_upload.validate_playbook_upload(
            filename="acme-university.opf.json",
            contents=_as_bytes(_load(FULL_FIXTURE_PATH)),
            playbook_id=PLAYBOOK_ID,
        )
        self.assertTrue(validated.is_opf)
        self.assertEqual(validated.artifact_kind, "opf-0.3")

    def test_real_shape_document_with_perspective_still_validates(self):
        validated = playbook_upload.validate_playbook_upload(
            filename="acme-university.opf.json",
            contents=_as_bytes(_load(REAL_SHAPE_FIXTURE_PATH)),
            playbook_id=PLAYBOOK_ID,
        )
        self.assertTrue(validated.is_opf)

    def test_legacy_v1_upload_is_untouched_by_the_opf_minimums(self):
        """A v1 playbook has no OPF top-level blocks at all -- the minimums
        table is an OPF-document rule and must not reach the v1 path."""
        v1_path = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
        validated = playbook_upload.validate_playbook_upload(
            filename="synthetic-generic-v1.0.0.json",
            contents=v1_path.read_bytes(),
            playbook_id="eiaa",
        )
        self.assertFalse(validated.is_opf)
        self.assertEqual(validated.artifact_kind, playbook_upload.ARTIFACT_KIND_V1)


# ---------------------------------------------------------------------------
# Upload surface -- the real HTTP routes
# ---------------------------------------------------------------------------


class RouteTestBase(unittest.TestCase):
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
        _put_user(self.ddb.Table(os.environ["USERS_TABLE"]), ADMIN_SUB, is_admin=True)

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

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self._mock_aws.stop()


class TestVersionsUploadRoute(RouteTestBase):
    def _upload(self, doc: dict[str, Any], version: str = "1.0.0"):
        return self.client.post(
            VERSIONS_PATH,
            files={
                "file": (
                    "acme-university.opf.json",
                    _as_bytes(doc),
                    "application/octet-stream",
                )
            },
            data={"version": version},
        )

    def test_perspective_less_upload_is_a_400_naming_the_field(self):
        resp = self._upload(_without_perspective())
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("perspective", resp.json()["detail"])

    def test_perspective_less_upload_records_nothing(self):
        """A rejected artifact must leave no version row and no S3 object --
        the gate refuses BEFORE the writes, like every other refusal on this
        route."""
        self._upload(_without_perspective())
        row = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"]).get_item(
            Key={"playbook_id": PLAYBOOK_ID, "version": "1.0.0"}
        )
        self.assertIsNone(row.get("Item"))
        listed = self.s3.list_objects_v2(Bucket=os.environ["UPLOADS_BUCKET"])
        self.assertEqual(listed.get("KeyCount", 0), 0)

    def test_upload_with_perspective_still_succeeds(self):
        resp = self._upload(_load(FULL_FIXTURE_PATH))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["artifact_kind"], "opf-0.3")


class TestCreatePlaybookRoute(RouteTestBase):
    """`POST /api/admin/playbooks` calls `_load_opf_from_bytes` directly,
    outside `validate_playbook_upload`, and caught only
    `opf_load.OpfValidationError` -- so the minimums rejection would escape
    as an unhandled 500. #676: "The upload route should surface the
    rejection as a clear 4xx to the admin, not a 500." """

    def _create(self, doc: dict[str, Any], version: str = "1.0.0"):
        return self.client.post(
            CREATE_PATH,
            files={
                "file": (
                    "acme-university.opf.json",
                    _as_bytes(doc),
                    "application/octet-stream",
                )
            },
            data={"version": version},
        )

    def test_perspective_less_create_is_a_4xx_not_a_500(self):
        resp = self._create(_without_perspective())
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("perspective", resp.json()["detail"])


# ---------------------------------------------------------------------------
# Review surface -- the already-activated artifact
# ---------------------------------------------------------------------------


class TestReviewTimeGate(RouteTestBase):
    def _activate(self, doc: dict[str, Any], *, playbook_id: str, version: str = "1.0.0"):
        """Record + Gate-7-approve + activate `doc` under `playbook_id`
        through the real production writers.

        `validate_playbook_upload` is deliberately not called here: for the
        perspective-less document it now refuses, and the state under test
        is the PRE-GATE one production is already in -- a row recorded back
        when the upload path accepted these bytes. The row shape itself is
        whatever `record_playbook_version_upload` writes, never hand-built.
        """
        raw_bytes = json.dumps(doc).encode("utf-8")
        storage_bytes = opf_canonicalize.canonicalize(doc).encode("utf-8")
        storage_hash_hex = hashlib.sha256(storage_bytes).hexdigest()
        storage_key = playbook_upload.storage_key_for(playbook_id, storage_hash_hex)
        self.s3.put_object(
            Bucket=os.environ["UPLOADS_BUCKET"], Key=storage_key, Body=storage_bytes
        )

        content_hash = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        playbook_versions.record_playbook_version_upload(
            playbook_id=playbook_id,
            version=version,
            uploader_identity="test-admin",
            dynamodb_resource=self.ddb,
            content_hash=content_hash,
            artifact_kind=f"opf-{doc['opf_version']}",
            opf_content_hash=doc["identity"]["content_hash"],
            storage_key=storage_key,
        )
        playbook_versions.record_legal_approval(
            playbook_id=playbook_id,
            version=version,
            content_hash=content_hash,
            actor_identity="test-admin",
            dynamodb_resource=self.ddb,
        )
        playbook_versions.activate_release_bundle(
            playbook_id=playbook_id,
            version=version,
            actor_identity="test-admin",
            dynamodb_resource=self.ddb,
        )

    def test_activated_perspective_less_artifact_is_refused_at_review_time(self):
        """The grandfathering case: bytes that were activated before this
        gate existed must be refused on the NEXT review, not trusted."""
        self._activate(_without_perspective(), playbook_id=PLAYBOOK_ID)
        with self.assertRaises(playbook_upload.PlaybookUploadRejected) as ctx:
            pipeline_runner._load_opf_bundle_if_active(PLAYBOOK_ID, self.ddb, self.s3)
        self.assertIn("perspective", str(ctx.exception))

    def test_activated_artifact_with_perspective_still_loads(self):
        doc = _load(FULL_FIXTURE_PATH)
        self._activate(doc, playbook_id=PLAYBOOK_ID)
        bundle = pipeline_runner._load_opf_bundle_if_active(PLAYBOOK_ID, self.ddb, self.s3)
        self.assertIsNotNone(bundle)
        self.assertEqual(
            bundle["opf_bundle_v2"]["opf"]["identity"]["content_hash"],
            doc["identity"]["content_hash"],
        )

    def test_load_playbook_bundle_propagates_the_refusal(self):
        """`_load_playbook_bundle` must not silently fall back to the
        registry's on-disk v1 bundle when the ACTIVATED artifact is refused
        -- that would run a review against content that is not what was
        activated."""
        self._activate(_without_perspective(), playbook_id=PLAYBOOK_ID)
        with self.assertRaises(playbook_upload.PlaybookUploadRejected):
            pipeline_runner._load_playbook_bundle(PLAYBOOK_ID, self.ddb, self.s3)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_case in (
        TestSchemaPremise,
        TestValidatePlaybookUpload,
        TestVersionsUploadRoute,
        TestCreatePlaybookRoute,
        TestReviewTimeGate,
    ):
        suite.addTests(loader.loadTestsFromTestCase(test_case))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
