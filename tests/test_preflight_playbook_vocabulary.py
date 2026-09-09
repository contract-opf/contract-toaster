#!/usr/bin/env python3
"""
Executable tests for issue #659: the preflight classifier's agreement-type
vocabulary is built from the INSTALLED playbooks' ACTIVE versions, not from
a list shipped in `scripts/preflight_pass.py`.

## The defect this pins fixed

`preflight_pass.known_agreement_types()` returned `CANONICAL_AGREEMENT_TYPES`
-- nine contract types plus the "Other" null answer, hard-coded into the tool
-- unioned with the agreement types it could read out of
`playbooks/registry.json`. A playbook installed
through the admin upload flow lives in the `playbook_versions` DynamoDB
table and its artifact in S3, not in that file, so its agreement type was
never offered to the classifier at all. The system prompt tells the model to
"pick the single closest match from the allowed list", so an educational
affiliation agreement was reported to the reviewer as "This reads like a
Non-Disclosure Agreement on the counterparty's paper" -- while the same
response's free-text `one_line_summary` described it correctly.

Half two of the same defect: `review_routes._resolve_playbook_agreement_type`
read `data["playbook"]["agreement_type"]` -- the legacy v1 shape -- off the
same on-disk registry. An OPF playbook carries `agreement_type` as a
TOP-LEVEL `{id, name, aliases}` object, so that read returned `(None, [])`,
`compute_match_verdict` returned "unclear", and the amber
`review-preflight-match-unlikely` banner never rendered: a wrong guess was
presented as a flat, unqualified statement with no hedge.

## What this file proves, and how

Everything here goes through the REAL producers, against moto-backed S3 and
DynamoDB -- no hand-written `playbook_versions` rows and no hand-placed S3
objects:

  `playbook_upload.validate_playbook_upload` (the same schema dispatch,
  content-hash verification, injection scan and agreement-type match the
  admin upload route runs) -> a content-addressed `put_object` at
  `playbook_upload.storage_key_for` -> `playbook_versions
  .record_playbook_version_upload` -> `playbook_versions
  .activate_playbook_version`

`_install_opf_version` below is that exact sequence, in that order, copied
from `backend/src/main.py`'s `POST /api/admin/playbooks/{id}/versions`
handler. So every row and object these tests assert over is a shape
production writes; nothing asserts over a state the system cannot reach.

Coverage (the issue's "Required verification" list for this file):
  - OPF 0.3 and OPF 0.2 artifacts (both schemas declare an identical
    top-level `agreement_type` object), and the legacy v1 registry shape.
  - Active-vs-inactive version selection: a second uploaded version whose
    `agreement_type.name` differs leaves the vocabulary alone until it is
    activated.
  - Alias matching, case-insensitively, on the OPF `aliases` list, on the
    machine `id`, and on a v1 playbook's `agreement_aliases`.
  - An active artifact whose bytes cannot be read is skipped, never fatal.
  - With nothing installed that yields a type, the route degrades to
    `classification: "unavailable"` and never calls the model.
  - No shipped contract type survives anywhere in `scripts/preflight_pass.py`.

Run standalone: `python tests/test_preflight_playbook_vocabulary.py`.
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"
_TESTS_DIR = Path(__file__).resolve().parent

for _path in (BACKEND_ROOT, SCRIPTS_DIR, _TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Set BEFORE importing the router: these names are read at import time by the
# modules under test, and moto creates real tables/buckets under them below.
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "ct-review-submissions-659")
os.environ.setdefault("REVIEWS_TABLE", "ct-reviews-659")
os.environ.setdefault("DAILY_SPEND_TABLE", "ct-daily-spend-659")
os.environ.setdefault("PLAYBOOKS_TABLE", "ct-playbooks-659")
os.environ.setdefault("PLAYBOOK_VERSIONS_TABLE", "ct-playbook-versions-659")
os.environ.setdefault("AUDIT_TABLE", "ct-audit-659")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)
os.environ.setdefault("UPLOADS_BUCKET", "ct-uploads-659")
os.environ.setdefault("OUTPUTS_BUCKET", "ct-outputs-659")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")

import boto3  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.playbook_upload as playbook_upload  # noqa: E402
import src.playbook_versions as playbook_versions  # noqa: E402
import src.review_routes as review_routes  # noqa: E402
import opf_canonicalize  # noqa: E402
import preflight_pass  # noqa: E402

# The .docx builders (a real, well-formed document with headings and body)
# already live in the preflight route's own test file -- reused rather than
# re-derived, same convention that file uses for test_review_api_84's
# harness. Importing it is import-time-inert: it defines classes and
# builders and starts no AWS mock of its own.
import test_preflight_491 as preflight491  # noqa: E402

# The two committed OPF fixtures. Both are real, schema-valid, identity-
# sealed artifacts the loader accepts today (tests/test_opf_ingest_03.py
# loads this exact pair), so what is uploaded here is what an admin would
# upload.
OPF_03_FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
OPF_02_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"

# `playbook_upload._check_agreement_type_match` refuses an upload whose
# playbook_id is not one of the artifact's own agreement_type keys (id +
# aliases), so these ids are not free choices -- they are the ids these two
# artifacts can actually be installed under.
OPF_03_PLAYBOOK_ID = "acme-university"  # agreement_type.name: Educational Affiliation Agreement
OPF_02_PLAYBOOK_ID = "eiaa"  # ...: Synthetic Educational Internship & Affiliation Agreement


def _with_perspective(doc: dict[str, Any]) -> dict[str, Any]:
    """Ensure *doc* carries a top-level `perspective` block, re-sealing its
    identity afterwards.

    Issue #676 made `perspective` an upload-time minimum
    (`playbook_upload.OPF_UPLOAD_MINIMUMS`): schema-OPTIONAL in both OPF
    0.2 and 0.3, but refused by the admin upload path this harness drives.
    The 0.3 gold fixture already carries one, so this is a no-op there; the
    0.2 fixture (`tests/fixtures/opf/synthetic-eiaa.opf.json`) does not.

    Added here rather than to the committed 0.2 file because that file's
    identity is load-bearing elsewhere: `tests/test_bind_bundle.py` check 6
    compares the committed `playbooks/bundles/synthetic-eiaa.bundle-v2.json`
    against a fresh `bind_bundle()` over that fixture, so re-sealing it
    would require regenerating a committed artifact for a reason that has
    nothing to do with this module. Nothing here tests perspective at all:
    what is under test is `agreement_type` resolution, which the block does
    not touch.

    Re-sealed with `scripts/opf_canonicalize.py`, the same functions the
    real compiler seals with, so the loader still accepts the artifact.
    """
    doc = copy.deepcopy(doc)
    if doc.get("perspective"):
        return doc
    doc["perspective"] = {
        "party": "FixtureCorp",
        "counterparty_type": "Educational Institution",
    }
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    doc["identity"]["section_digests"] = opf_canonicalize.compute_section_digests(doc)
    return doc


OPF_03_TYPE_NAME = "Educational Affiliation Agreement"
OPF_02_TYPE_NAME = "Synthetic Educational Internship & Affiliation Agreement"

# The playbook `playbooks/registry.json` ships (legacy v1, on-disk).
V1_PLAYBOOK_ID = "synthetic-nda-sample"
V1_TYPE_NAME = "Non-Disclosure Agreement"

OTHER = preflight_pass.UNCLASSIFIED_AGREEMENT_TYPE


def _reseal(doc: dict[str, Any]) -> dict[str, Any]:
    """Recompute `identity` so a mutated OPF document hashes honestly again
    -- the loader verifies `identity.content_hash` and would otherwise
    refuse the upload. Same helper shape as tests/test_opf_ingest_03.py's."""
    doc = copy.deepcopy(doc)
    original_sections = set(doc["identity"].get("section_digests") or {})
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    digests = opf_canonicalize.compute_section_digests(doc)
    doc["identity"]["section_digests"] = {
        k: v for k, v in digests.items() if k in original_sections
    }
    return doc


@contextlib.contextmanager
def _temp_registry(registry: dict[str, Any]):
    """A real `playbooks/registry.json` on disk, so the code under test
    reads it exactly the way it reads the shipped one."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "registry.json"
        path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        yield str(path)


def _renamed_type(fixture_path: Path, new_name: str) -> dict[str, Any]:
    """The fixture with a DIFFERENT `agreement_type.name` -- and the same
    `id`/`aliases`, so it is still installable under the same playbook_id
    (that is what makes it a second VERSION of one playbook rather than a
    different playbook)."""
    doc = json.loads(fixture_path.read_text(encoding="utf-8"))
    doc["agreement_type"]["name"] = new_name
    return _reseal(doc)


class _CountingS3:
    """Pass-through proxy over the real (moto) S3 client that counts
    `get_object` calls. Everything it does not intercept is delegated, so it
    accepts and refuses exactly what the real client does -- it is an
    observer, not a stand-in with its own idea of what S3 allows."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.get_object_keys: list[str] = []

    def get_object(self, **kwargs: Any) -> Any:
        self.get_object_keys.append(kwargs.get("Key", ""))
        return self._inner.get_object(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class PreflightVocabularyTestBase(unittest.TestCase):
    """Real router, real producers, moto S3 + DynamoDB."""

    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)

        self.raw_s3 = boto3.client("s3", region_name="us-east-1")
        self.raw_s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.raw_s3.create_bucket(Bucket=os.environ["OUTPUTS_BUCKET"])
        self.s3 = _CountingS3(self.raw_s3)

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb.create_table(
            TableName=os.environ["PLAYBOOKS_TABLE"],
            KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
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
        self.ddb.create_table(
            TableName=os.environ["DAILY_SPEND_TABLE"],
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # The resolver's in-process artifact cache outlives a single test in
        # this process; cleared here so each test observes the state IT set
        # up. (Its keying is itself under test -- see
        # `TestActiveVersionCache`.)
        review_routes._AGREEMENT_TYPE_CACHE.clear()

        self.registry_path = REPO_ROOT / "playbooks" / "registry.json"
        self.app = FastAPI()
        self.app.include_router(review_routes.router)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: self.ddb
        self.app.dependency_overrides[review_routes.get_s3_client] = lambda: self.s3
        self.app.dependency_overrides[review_routes.get_playbook_registry_path] = (
            lambda: self.registry_path
        )
        self.app.dependency_overrides[review_routes.get_av_client] = (
            lambda: review_routes.NullAvClient()
        )
        self.app.dependency_overrides[review_routes.get_active_user_row] = lambda: {
            "cognito_sub": "reviewer-659",
            "email": "reviewer-659@example.com",
            "status": "active",
            "is_admin": False,
        }
        self.app.dependency_overrides[review_routes.get_preflight_model_client] = (
            lambda: None
        )
        self.client = TestClient(self.app)

    # -- the real admin upload+activate sequence -----------------------------

    def _install_opf_version(
        self,
        playbook_id: str,
        doc: dict[str, Any],
        version: str,
        *,
        activate: bool = True,
    ) -> str:
        """`backend/src/main.py::upload_playbook_version`'s own sequence:
        validate the artifact, write the canonical text to the uploads
        bucket at its content-addressed key, record the (draft) version row,
        then optionally activate it. Returns the storage key."""
        contents = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        validated = playbook_upload.validate_playbook_upload(
            filename=f"{playbook_id}.opf.json",
            contents=contents,
            playbook_id=playbook_id,
            accept_stub_basis=True,
        )
        storage_bytes = validated.storage_text.encode("utf-8")
        storage_key = playbook_upload.storage_key_for(
            playbook_id, hashlib.sha256(storage_bytes).hexdigest()
        )
        self.raw_s3.put_object(
            Bucket=os.environ["UPLOADS_BUCKET"], Key=storage_key, Body=storage_bytes
        )
        playbook_versions.record_playbook_version_upload(
            playbook_id=playbook_id,
            version=version,
            uploader_identity="admin-659",
            dynamodb_resource=self.ddb,
            content_hash="sha256:" + hashlib.sha256(contents).hexdigest(),
            artifact_kind=validated.artifact_kind,
            opf_content_hash=validated.opf_content_hash,
            storage_key=storage_key,
            accepted_stub_basis=validated.accepted_stub_basis,
        )
        if activate:
            playbook_versions.activate_playbook_version(
                playbook_id, version, "admin-659", self.ddb
            )
        return storage_key

    def _install_opf_fixture(
        self, playbook_id: str, fixture: Path, version: str = "v1", **kwargs: Any
    ) -> str:
        return self._install_opf_version(
            playbook_id,
            _with_perspective(json.loads(fixture.read_text(encoding="utf-8"))),
            version,
            **kwargs,
        )

    # -- helpers -------------------------------------------------------------

    def _vocabulary(self) -> list[str]:
        return review_routes._preflight_agreement_vocabulary(
            self.registry_path, self.ddb, self.s3
        )

    def _resolve(self, playbook_id: str) -> tuple[str | None, list[str]]:
        return review_routes._resolve_playbook_agreement_type(
            playbook_id, self.ddb, self.s3
        )

    def _preflight(self, docx_bytes: bytes, *, playbook_id: str):
        return self.client.post(
            "/api/reviews/preflight",
            files={
                "file": (
                    "in.docx",
                    docx_bytes,
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document",
                )
            },
            data={"playbook_id": playbook_id},
        )

    def _match(self, guess: str, *, playbook_id: str) -> str:
        resp = self.client.post(
            "/api/reviews/preflight/match",
            data={"agreement_type_guess": guess, "playbook_id": playbook_id},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()["match"]

    def _model_client(self, guess: str) -> Any:
        client = preflight491.FakePreflightModelClient(
            {
                "agreement_type_guess": guess,
                "paper_side": "counterparty",
                "confidence": 0.9,
                "one_line_summary": "A template agreement.",
            },
            served_model="test/cheap-model",
            usage={"prompt_tokens": 900, "completion_tokens": 60},
        )
        self.app.dependency_overrides[review_routes.get_preflight_model_client] = (
            lambda: client
        )
        return client


# ---------------------------------------------------------------------------
# The headline: a DB-installed OPF playbook's own type reaches the model.
# ---------------------------------------------------------------------------


class TestDbInstalledOpfPlaybookReachesTheVocabulary(PreflightVocabularyTestBase):
    def test_opf_03_active_type_is_in_the_vocabulary_and_the_model_schema(self):
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)

        self.assertIn(OPF_03_TYPE_NAME, self._vocabulary())

        client = self._model_client(OPF_03_TYPE_NAME)
        resp = self._preflight(
            preflight491._nda_shaped_docx_bytes(), playbook_id=OPF_03_PLAYBOOK_ID
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["classification"], "ok")
        # The enum the model was actually handed -- not just what a helper
        # returns in isolation.
        enum = client.invocations[0]["output_schema"]["properties"][
            "agreement_type_guess"
        ]["enum"]
        self.assertIn(OPF_03_TYPE_NAME, enum)
        # And it survives `sanitize_classification`'s exact-match gate, so
        # it is what the reviewer sees.
        self.assertEqual(body["agreement_type_guess"], OPF_03_TYPE_NAME)
        # Half two of the defect: the selected playbook's own type resolves,
        # so the verdict is a real comparison instead of "unclear".
        self.assertEqual(body["match"], "likely")

    def test_opf_02_active_type_resolves_the_same_way(self):
        """0.2 and 0.3 declare an identical `agreement_type` object
        (`playbooks/opf/playbook.schema-0.2.json` /
        `...-0.3.json`), so one resolver covers both -- asserted, not
        assumed, against a real 0.2 artifact."""
        self._install_opf_fixture(OPF_02_PLAYBOOK_ID, OPF_02_FIXTURE)

        name, aliases = self._resolve(OPF_02_PLAYBOOK_ID)
        self.assertEqual(name, OPF_02_TYPE_NAME)
        # `aliases` plus the machine `id`, per the OPF schema's own
        # "e.g. a consuming app's own dial key" note.
        self.assertIn("educational-internship-affiliation", aliases)
        self.assertIn("eiaa", aliases)
        self.assertIn(OPF_02_TYPE_NAME, self._vocabulary())

    def test_the_registry_v1_playbook_still_resolves_alongside_it(self):
        """The legacy v1 shape (`playbook.agreement_type` /
        `playbook.agreement_aliases`, read off the registry's on-disk JSON)
        must not regress while the OPF branch is added."""
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)

        name, aliases = self._resolve(V1_PLAYBOOK_ID)
        self.assertEqual(name, V1_TYPE_NAME)
        self.assertIn("NDA", aliases)

        vocabulary = self._vocabulary()
        self.assertIn(V1_TYPE_NAME, vocabulary)
        self.assertIn(OPF_03_TYPE_NAME, vocabulary)
        self.assertIn(OTHER, vocabulary)


# ---------------------------------------------------------------------------
# No shipped list survives.
# ---------------------------------------------------------------------------


class TestNoShippedContractTypeSurvives(PreflightVocabularyTestBase):
    def test_the_hard_coded_list_is_gone_from_the_module(self):
        self.assertFalse(hasattr(preflight_pass, "CANONICAL_AGREEMENT_TYPES"))
        source = (SCRIPTS_DIR / "preflight_pass.py").read_text(encoding="utf-8")
        for shipped in (
            "Master Services Agreement",
            "Statement of Work",
            "Software License Agreement",
            "Lease Agreement",
        ):
            self.assertNotIn(shipped, source)

    def test_only_installed_types_plus_the_null_answer_are_offered(self):
        """The vocabulary is exactly what this deployment has installed. A
        type no installed playbook declares -- however common a contract
        type it is -- must not be offerable."""
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)

        self.assertEqual(
            sorted(self._vocabulary()),
            sorted([OPF_03_TYPE_NAME, V1_TYPE_NAME, OTHER]),
        )

    def test_other_is_always_offered_and_is_not_a_contract_type(self):
        """"Other" is the null answer, not a type: it is offered even with
        playbooks installed, and a vocabulary of ONLY it counts as empty
        (`_vocabulary_has_installed_types`)."""
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)
        self.assertIn(OTHER, self._vocabulary())
        self.assertTrue(review_routes._vocabulary_has_installed_types(self._vocabulary()))
        self.assertFalse(review_routes._vocabulary_has_installed_types([OTHER]))


# ---------------------------------------------------------------------------
# Only the ACTIVE version's type.
# ---------------------------------------------------------------------------


class TestOnlyTheActiveVersionIsOffered(PreflightVocabularyTestBase):
    RENAMED = "Clinical Placement Agreement"

    def test_an_uploaded_but_unactivated_version_changes_nothing(self):
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE, version="v1")
        self.assertIn(OPF_03_TYPE_NAME, self._vocabulary())

        self._install_opf_version(
            OPF_03_PLAYBOOK_ID,
            _renamed_type(OPF_03_FIXTURE, self.RENAMED),
            "v2",
            activate=False,
        )

        vocabulary = self._vocabulary()
        self.assertIn(OPF_03_TYPE_NAME, vocabulary)
        self.assertNotIn(self.RENAMED, vocabulary)

    def test_activating_that_version_changes_it(self):
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE, version="v1")
        self._install_opf_version(
            OPF_03_PLAYBOOK_ID,
            _renamed_type(OPF_03_FIXTURE, self.RENAMED),
            "v2",
            activate=False,
        )
        self.assertNotIn(self.RENAMED, self._vocabulary())

        playbook_versions.activate_playbook_version(
            OPF_03_PLAYBOOK_ID, "v2", "admin-659", self.ddb
        )

        vocabulary = self._vocabulary()
        self.assertIn(self.RENAMED, vocabulary)
        self.assertNotIn(OPF_03_TYPE_NAME, vocabulary)

    def test_a_playbook_with_no_active_version_contributes_nothing(self):
        """A `coming_soon` DB playbook -- uploaded, never activated -- is in
        the catalog but has no active artifact, so it yields no type rather
        than the type of a version nobody activated."""
        self._install_opf_fixture(
            OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE, version="v1", activate=False
        )

        self.assertEqual(self._resolve(OPF_03_PLAYBOOK_ID), (None, []))
        self.assertNotIn(OPF_03_TYPE_NAME, self._vocabulary())


# ---------------------------------------------------------------------------
# The match verdict: name OR alias, case-insensitively.
# ---------------------------------------------------------------------------


class TestMatchVerdictUsesTheRealTypeAndAliases(PreflightVocabularyTestBase):
    def test_the_opf_name_matches(self):
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)
        self.assertEqual(
            self._match(OPF_03_TYPE_NAME, playbook_id=OPF_03_PLAYBOOK_ID), "likely"
        )

    def test_an_opf_alias_matches_case_insensitively(self):
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)
        # "eiaa-fixture" is one of the artifact's declared `aliases`;
        # "educational-affiliation" is its machine `id`.
        self.assertEqual(
            self._match("EIAA-Fixture", playbook_id=OPF_03_PLAYBOOK_ID), "likely"
        )
        self.assertEqual(
            self._match("Educational-Affiliation", playbook_id=OPF_03_PLAYBOOK_ID),
            "likely",
        )

    def test_a_v1_alias_matches_too(self):
        self.assertEqual(self._match("nda", playbook_id=V1_PLAYBOOK_ID), "likely")

    def test_a_non_matching_guess_is_unlikely_not_unclear(self):
        """The banner this restores: before the fix an OPF playbook resolved
        to `(None, [])`, so a wrong guess produced "unclear" and the amber
        `review-preflight-match-unlikely` note never rendered."""
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)
        self.assertEqual(
            self._match(V1_TYPE_NAME, playbook_id=OPF_03_PLAYBOOK_ID), "unlikely"
        )

    def test_an_unresolvable_playbook_is_still_unclear_never_an_error(self):
        self.assertEqual(self._match(V1_TYPE_NAME, playbook_id="no-such-playbook"), "unclear")


# ---------------------------------------------------------------------------
# Degrade honestly.
# ---------------------------------------------------------------------------


class TestUnreadableArtifactIsSkipped(PreflightVocabularyTestBase):
    def test_a_missing_active_artifact_costs_that_playbook_its_entry_only(self):
        storage_key = self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)
        # The activated row still points at these bytes; the bytes are gone.
        self.raw_s3.delete_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=storage_key)

        self.assertEqual(self._resolve(OPF_03_PLAYBOOK_ID), (None, []))
        vocabulary = self._vocabulary()
        self.assertNotIn(OPF_03_TYPE_NAME, vocabulary)
        # ...and the OTHER installed playbook is unaffected.
        self.assertIn(V1_TYPE_NAME, vocabulary)

    def test_preflight_still_returns_its_stats(self):
        storage_key = self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)
        self.raw_s3.delete_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=storage_key)

        self._model_client(V1_TYPE_NAME)
        resp = self._preflight(
            preflight491._nda_shaped_docx_bytes(), playbook_id=OPF_03_PLAYBOOK_ID
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertGreater(body["word_count"], 0)
        self.assertGreaterEqual(body["page_estimate"], 1)
        # The remaining playbook still gave the classifier something to
        # answer with, so this is not the empty-vocabulary path.
        self.assertEqual(body["classification"], "ok")
        # But the SELECTED playbook has no readable type, so the verdict is
        # honestly "unclear" rather than a manufactured mismatch.
        self.assertEqual(body["match"], "unclear")


class TestEmptyVocabularyDegradesToUnavailable(PreflightVocabularyTestBase):
    def setUp(self) -> None:
        super().setUp()
        # A catalog with nothing in it but a `test_only` entry -- which the
        # catalog excludes -- and no DB playbooks at all. Written as a real
        # registry file so `_catalog_playbook_entries` reads it exactly as
        # it reads the shipped one.
        registry = {
            "description": "Issue #659 test registry: no installed contract type.",
            "default_playbook_id": "synthetic-generic",
            "playbooks": {
                "synthetic-generic": {
                    "playbook_id": "synthetic-generic",
                    "playbook_path": (
                        "tests/fixtures/playbooks/synthetic-generic-v1.0.0.json"
                    ),
                    "test_only": True,
                }
            },
        }
        self.registry_path = Path(self.enterContext(_temp_registry(registry)))
        self.app.dependency_overrides[review_routes.get_playbook_registry_path] = (
            lambda: self.registry_path
        )

    def test_the_vocabulary_is_only_the_null_answer(self):
        self.assertEqual(self._vocabulary(), [OTHER])
        self.assertFalse(review_routes._vocabulary_has_installed_types(self._vocabulary()))

    def test_the_route_reports_unavailable_and_never_calls_the_model(self):
        client = self._model_client(V1_TYPE_NAME)

        resp = self._preflight(
            preflight491._nda_shaped_docx_bytes(), playbook_id=V1_PLAYBOOK_ID
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["classification"], "unavailable")
        self.assertIsNone(body["agreement_type_guess"])
        self.assertIsNone(body["match"])
        # The deterministic half still renders -- "never blocking".
        self.assertGreater(body["word_count"], 0)
        self.assertGreaterEqual(body["paragraph_count"], 1)
        # No shipped list to fall back on means no call, and no spend.
        self.assertEqual(client.invocations, [])
        spend = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"]).scan().get("Items", [])
        self.assertEqual(spend, [])


# ---------------------------------------------------------------------------
# The in-process cache.
# ---------------------------------------------------------------------------


class TestActiveVersionCache(PreflightVocabularyTestBase):
    def test_a_repeat_lookup_does_not_re_read_the_artifact(self):
        """Issue #659: "one preflight does not issue an S3 GET per installed
        playbook on every upload"."""
        storage_key = self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE)

        self._vocabulary()
        self.assertEqual(self.s3.get_object_keys.count(storage_key), 1)
        self._vocabulary()
        self._vocabulary()
        self.assertEqual(self.s3.get_object_keys.count(storage_key), 1)

    def test_activating_a_new_version_invalidates_it(self):
        """Keyed on the ACTIVE version's identity, not on the playbook_id --
        an id-keyed cache would serve the retired version's agreement type
        until the process restarted."""
        self._install_opf_fixture(OPF_03_PLAYBOOK_ID, OPF_03_FIXTURE, version="v1")
        self.assertIn(OPF_03_TYPE_NAME, self._vocabulary())

        self._install_opf_version(
            OPF_03_PLAYBOOK_ID,
            _renamed_type(OPF_03_FIXTURE, "Clinical Placement Agreement"),
            "v2",
            activate=True,
        )

        self.assertIn("Clinical Placement Agreement", self._vocabulary())


# ---------------------------------------------------------------------------
# Label normalization (the cap issue #659's Notes asked us to decide on).
# ---------------------------------------------------------------------------


class TestAgreementTypeLabelNormalization(PreflightVocabularyTestBase):
    def test_an_over_long_admin_supplied_name_is_capped_consistently(self):
        """A vocabulary entry is now admin-supplied text that reaches both
        the model's enum and the DOM, so it is capped like `one_line_summary`
        and `title`. The cap must be applied to the MATCH TARGET too -- if
        the vocabulary carried a truncated label and the verdict compared
        against the untruncated one, a correct guess would report
        `unlikely`."""
        # Deliberately lands the cap ON a word gap, so a naive
        # cap-then-nothing would leave a trailing space that a second
        # normalization pass strips -- the exact way the enum entry and the
        # match target can end up one character apart.
        long_name = "Extremely " * 40 + "Long Agreement"
        self.assertGreater(len(long_name), preflight_pass.AGREEMENT_TYPE_MAX_CHARS)
        self.assertTrue(long_name[preflight_pass.AGREEMENT_TYPE_MAX_CHARS - 1].isspace())
        self._install_opf_version(
            OPF_03_PLAYBOOK_ID, _renamed_type(OPF_03_FIXTURE, long_name), "v1"
        )

        capped = long_name[: preflight_pass.AGREEMENT_TYPE_MAX_CHARS].strip()
        vocabulary = self._vocabulary()
        self.assertIn(capped, vocabulary)
        self.assertNotIn(long_name, vocabulary)
        # The vocabulary entry and the match target are the SAME string...
        name, _aliases = self._resolve(OPF_03_PLAYBOOK_ID)
        self.assertEqual(name, capped)
        # ...so a guess echoing the enum entry verbatim still matches.
        self.assertEqual(self._match(capped, playbook_id=OPF_03_PLAYBOOK_ID), "likely")

    def test_control_characters_and_newlines_never_reach_a_label(self):
        self._install_opf_version(
            OPF_03_PLAYBOOK_ID,
            _renamed_type(OPF_03_FIXTURE, "Affiliation\tAgreement\n(Clinical)"),
            "v1",
        )
        name, _aliases = self._resolve(OPF_03_PLAYBOOK_ID)
        self.assertEqual(name, "Affiliation Agreement (Clinical)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
