#!/usr/bin/env python3
"""
Executable tests for the DEPLOY-TIME SEED that installs the playbook the
image ships with -- issue #433, which replaced issue #402/#412's bespoke
one-click "activate the bundled sample" path (a registry marker, a
`has_bundled_sample` catalog field, and a dedicated
`POST /api/admin/playbooks/{id}/activate-sample` route) with an ordinary
install through the same functions any admin-uploaded version goes through.

Renamed from tests/test_sample_playbook_activation.py: what is under test is
the seed, not an activation route that no longer exists.

## What this proves

  1. The shipped playbook (playbooks/samples/synthetic-nda-sample-v1.0.0.json)
     is real content -- schema-valid, brand-free, and genuinely different
     from the OLD "coming soon" placeholder stub (playbooks/nda-v0.1.0.json,
     left untouched here -- see tests/test_nda_policy.py's own
     harvested-but-not-wired gate, which this ticket does not touch). It
     carries NO `bundled_sample` marker -- issue #433 removed that field
     from the registry and the `PlaybookEntry` dataclass entirely.
  2. `src.sample_playbooks.seed_shipped_playbook` fails closed on what it
     will install -- `SampleNotAvailableError` for an unregistered
     playbook_id and for a `"test_only": true` fixtures entry
     (`synthetic-generic`), `SampleInvalidError` for content that fails
     runtime validation. No playbook_id literal gates this; a registry
     field does (issue #289's type-blindness convention).
  3. THE FRESH-DEPLOY ACCEPTANCE CRITERION: driving the REAL bootstrap
     entry point (`deploy/dts/bootstrap.py::seed_shipped_playbook`) against
     EMPTY tables leaves "synthetic-nda-sample" ACTIVE, with the same
     version-row/audit-trail shape an admin-uploaded-and-activated playbook
     would have: a `playbook_versions` row written by
     `record_playbook_version_upload`, flipped to `active` by
     `activate_playbook_version`, carrying the registry's `seed_notes` as
     its admin-editable `notes`, and `playbooks.active_release_bundle_hash`
     pointing at the same real content hash
     `scripts/seed_active_bundle.py` computes.
  4. INSTALL ONCE, NEVER RE-STOMP. The bootstrap re-runs on every container
     start, so a second run must be a no-op: no duplicate version row, no
     second audit row, an admin's edited note preserved, an admin's newer
     uploaded version left active, and -- the sharp one -- a playbook an
     admin REMOVED stays removed rather than being resurrected on restart.
  5. The seeded playbook is an ordinary playbook: it accepts a new uploaded
     version, rolls back, renames, and removes through plain
     `src.playbook_versions` calls, with zero special-casing.
  6. With it installed, a submitted review runs a REAL review end to end --
     backend/src/pipeline_runner.py's `run_real_pipeline` (issue
     #259/#401), driven by a `FakeBedrockClient` (no live model call, per
     this repo's standing "no network in any test" rule), reaches DONE
     with a genuinely computed REQUEST_CHANGE decision and a real
     tracked-changes output object -- never QUARANTINED, never stuck
     PENDING/RUNNING. The contrasting case (submitting the SAME payload
     without seeding first) is quarantined at #401's verify_active_bundle
     gate, `_load_playbook_bundle` never called -- proving the seed is
     actually load-bearing, not cosmetic.

Run standalone: `python3 tests/test_shipped_playbook_seed.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

# `backend/` (not just `backend/src/`) so `src.review_routes` -- which
# imports its own siblings as `from src import ...` -- is importable here;
# issue #412's rename/remove must be asserted through the real catalog
# builder. Same convention as tests/test_playbook_catalog_endpoint.py.
BACKEND_ROOT = REPO_ROOT / "backend"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, BACKEND_ROOT):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")
os.environ.setdefault("PLAYBOOK_VERSIONS_TABLE", "playbook-versions-test")
os.environ.setdefault("AUDIT_TABLE", "audit-test")

import model_client as model_client_module  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402
import playbook_versions as pv  # noqa: E402
import reviews as reviews_module  # noqa: E402
import sample_playbooks  # noqa: E402
import seed_active_bundle  # noqa: E402
import src.review_routes as review_routes  # noqa: E402 -- issue #412 rename/remove reach the catalog

# The REAL deploy bootstrap (issue #433's only install path). Imported by
# file location because `deploy/dts/` is not an importable package -- this
# test drives the actual entry point, not a re-implementation of it.
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "dts_bootstrap", REPO_ROOT / "deploy" / "dts" / "bootstrap.py"
)
dts_bootstrap = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
_BOOTSTRAP_SPEC.loader.exec_module(dts_bootstrap)

PLAYBOOK_ID = "synthetic-nda-sample"
REVIEW_ID = "00000000-0000-4000-a000-000000000402"

# ---------------------------------------------------------------------------
# Minimal in-memory fakes -- same shapes/conventions as
# tests/test_dts_pipeline_runner_real_review.py / tests/
# test_playbook_catalog_endpoint.py, reproduced here (this repo's own
# "each test file owns its own copy" convention) with one addition: a real
# fake AUDIT_TABLE, since this flow (unlike those two files) actually
# writes one.
# ---------------------------------------------------------------------------


def _conditional() -> Exception:
    exc = Exception("ConditionalCheckFailedException")
    exc.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    return exc


class FakeReviewsTable:
    def __init__(self, status: str = "PENDING"):
        self.item: dict[str, Any] = {"review_id": REVIEW_ID, "status": status}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                     ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        cur_status = self.item.get("status")
        if ConditionExpression == "#s = :pending" and cur_status != values.get(":pending"):
            raise _conditional()
        if (
            ConditionExpression == "attribute_not_exists(#s) OR #s <> :error"
            and cur_status == values.get(":error")
        ):
            raise _conditional()
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            self.item[field] = values[val_token.strip()]


class FakePlaybooksTable:
    """PLAYBOOKS_TABLE (PK: playbook_id) -- reviews._read_active_release_bundle_hash
    reads `active_release_bundle_hash` back off this row with get_item;
    `src.sample_playbooks.seed_shipped_playbook` and
    `src.playbook_versions` write it with update_item (see update_item's own
    note below)."""

    def __init__(self):
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key):
        item = self.items.get(Key["playbook_id"])
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[Item["playbook_id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None):
        """`playbook_versions.rename_playbook` / `remove_playbook` set the
        catalog override attributes (`display_name`, `removed`) on this row,
        and `sample_playbooks.seed_shipped_playbook` writes
        `active_release_bundle_hash` here the same way (update_item, never
        put_item, so it cannot clobber those overrides). The real table's
        update_item creates the item if absent, so this does too."""
        item = self.items.setdefault(Key["playbook_id"], dict(Key))
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            item[field] = values[val_token.strip()]


class FakeAuditTable:
    """AUDIT_TABLE (PK: partition, SK: timestamp#event_id) -- append-only,
    so this is just a list; nothing in this test suite reads it back by
    key, only inspects what was appended."""

    def __init__(self):
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item):
        self.items.append(dict(Item))


class _ConditionalCheckFailedException(Exception):
    pass


class _FakeExceptions:
    ConditionalCheckFailedException = _ConditionalCheckFailedException


class _FakeMetaClient:
    exceptions = _FakeExceptions()


class _FakeMeta:
    client = _FakeMetaClient()


class FakePlaybookVersionsTable:
    """PLAYBOOK_VERSIONS_TABLE (PK: playbook_id, SK: version) --
    `src.sample_playbooks.seed_shipped_playbook` writes/notes/activates a
    REAL version row for the shipped playbook via `src.playbook_versions`'
    own functions, the exact same ones every other playbook_id's governed
    upload/activate/rollback/notes lifecycle uses.
    `.meta.client.exceptions.ConditionalCheckFailedException` mirrors just
    enough of boto3's real `Table` shape for
    `playbook_versions.record_playbook_version_upload`'s append-only
    conflict check (a concurrent bootstrap re-uploading the same
    `(playbook_id, version)`) to behave exactly like the real table."""

    meta = _FakeMeta()

    def __init__(self):
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, Item, ConditionExpression=None):
        key = (Item["playbook_id"], Item["version"])
        if ConditionExpression and key in self.items:
            raise _ConditionalCheckFailedException("ConditionalCheckFailedException")
        self.items[key] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["playbook_id"], Key["version"]))
        return {"Item": item} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        key = (Key["playbook_id"], Key["version"])
        item = self.items.setdefault(key, dict(Key))
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            item[field] = values[val_token.strip()]

    def delete_item(self, Key):
        """Issue #412: `playbook_versions.remove_playbook` deletes every
        version row for a removed playbook."""
        self.items.pop((Key["playbook_id"], Key["version"]), None)

    def query(self, KeyConditionExpression, **_kwargs):
        # Duck-typed boto3.dynamodb.conditions.Key access -- same convention
        # as tests/test_example_playbook_registry.py's own FakeTable.query.
        key_obj, value = KeyConditionExpression.get_expression()["values"]
        items = [item for item in self.items.values() if item.get(key_obj.name) == value]
        return {"Items": items}

    def scan(self, **_kwargs):
        # Issue #485/#490: `playbook_versions.list_all_version_playbook_ids`
        # (the catalog's DB-only-playbook union) does a full table scan.
        # This fake never holds enough rows to need real pagination, so one
        # call always returns everything and no `LastEvaluatedKey`.
        return {"Items": list(self.items.values())}


class FakeDDB:
    """One fake dynamodb_resource sharing ONE playbooks table + ONE
    playbook_versions table + ONE audit table across activation and the
    subsequent pipeline run -- the same object a real
    boto3.resource("dynamodb") would be, so activating against it and then
    submitting against it are genuinely the SAME runtime activation record,
    not two disconnected fakes."""

    def __init__(self, reviews_table: FakeReviewsTable):
        self._reviews = reviews_table
        self._playbooks = FakePlaybooksTable()
        self._playbook_versions = FakePlaybookVersionsTable()
        self._audit = FakeAuditTable()

    def Table(self, name: str):
        if name == os.environ["REVIEWS_TABLE"]:
            return self._reviews
        if name == os.environ["PLAYBOOKS_TABLE"]:
            return self._playbooks
        if name == os.environ["PLAYBOOK_VERSIONS_TABLE"]:
            return self._playbook_versions
        if name == os.environ["AUDIT_TABLE"]:
            return self._audit
        raise AssertionError(f"unexpected table requested in this test: {name!r}")


class FakeS3:
    def __init__(self, uploads: dict[str, bytes] | None = None):
        self._uploads = uploads or {}
        self.puts: list[dict[str, Any]] = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._uploads[Key])}

    def put_object(self, Bucket, Key, Body, **_kwargs):
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _seed(ddb: Any, playbook_id: str = PLAYBOOK_ID) -> dict[str, Any]:
    """Run the deploy-time seed the way the bootstrap does. `actor_identity`
    is deliberately left at its default so the audit assertions below pin
    the real `SEED_ACTOR`, not a test-supplied string."""
    return sample_playbooks.seed_shipped_playbook(playbook_id, ddb)


# ---------------------------------------------------------------------------
# A tiny, valid, synthetic .docx. nda's topics are all not_in_standard
# (issue #402's sample ships with no standard-form docx, and issue #380
# already retired the standard-form diff from issue-generation for every
# playbook), so this needs no anchor-matched structure -- just an
# extractable paragraph carrying the exact text the fake primary response's
# source_quote below will name.
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

_DRAFT_CONFIDENTIALITY_TEXT = (
    "This confidentiality obligation shall survive in perpetuity with no end date."
)


def _heading_p(text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes() -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>"
        + _heading_p("Confidentiality")
        + _body_p(_DRAFT_CONFIDENTIALITY_TEXT)
        + "<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _primary_request_change_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "section_ref": "Confidentiality Obligations",
                    "section_title": "Confidentiality Obligations",
                    "counterparty_change_summary": (
                        "Counterparty proposed an unbounded, perpetual "
                        "confidentiality obligation with no end date."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": (
                        "The confidentiality obligation must survive for a "
                        "defined, bounded period, not indefinitely."
                    ),
                    "proposed_replacement_text": (
                        "This confidentiality obligation survives termination "
                        "of this agreement for three (3) years from the date "
                        "of disclosure."
                    ),
                    "playbook_topic_id": "nda-term-survival",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                    "source_quote": _DRAFT_CONFIDENTIALITY_TEXT,
                }
            ],
            "critic_delta": None,
            "verdict_summary": (
                "One issue identified in the confidentiality section, "
                "requiring attention before this can be accepted."
            ),
        }
    )


def _critic_no_delta_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _fake_client() -> Any:
    """A FakeBedrockClient keyed by the OPENROUTER model ids -- pipeline_runner
    patches the bundle's metadata to OpenRouter-form ids before calling
    run_review (see pipeline_runner._bundle_with_openrouter_model_ids)."""
    primary_id = model_client_module.openrouter_primary_model_id()
    critic_id = model_client_module.openrouter_critic_model_id()
    return model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response()],
            critic_id: [_critic_no_delta_response()],
        }
    )


def _iter_strings(value: Any):
    """Yield every string VALUE nested inside `value` -- never a dict key.
    Same helper as tests/test_example_playbook_registry.py's own
    de-identification check, reproduced here per this repo's "each test
    file owns its own copy" convention."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _payload(release_bundle_hash: str) -> dict[str, Any]:
    return {
        "review_id": REVIEW_ID,
        "owner_sub": "user-1",
        "playbook_id": PLAYBOOK_ID,
        "upload_s3_key": f"uploads/user-1/{REVIEW_ID}/in.docx",
        "release_bundle_hash": release_bundle_hash,
    }


# ---------------------------------------------------------------------------
# 1. The shipped playbook is real, schema-valid, brand-free content.
# ---------------------------------------------------------------------------


class TestShippedPlaybookIsRealContent(unittest.TestCase):
    def test_registry_ships_synthetic_nda_sample_as_the_default(self):
        entry = playbook_registry.resolve_playbook(PLAYBOOK_ID)
        self.assertEqual(entry.playbook_path.name, "synthetic-nda-sample-v1.0.0.json")
        self.assertEqual(playbook_registry.profile(entry), "knowledge")
        self.assertFalse(entry.test_only)
        self.assertEqual(playbook_registry.default_playbook_id(), PLAYBOOK_ID)

    def test_no_bundled_sample_marker_survives_anywhere(self):
        """Issue #433 acceptance: the registry no longer carries a
        `bundled_sample` marker, and `PlaybookEntry` no longer has the
        field -- there is no sample-only branch left to key off."""
        raw = playbook_registry.load_registry()["playbooks"]
        self.assertNotIn("bundled_sample", raw[PLAYBOOK_ID])
        self.assertNotIn("bundled_sample", json.dumps(raw))
        self.assertFalse(
            hasattr(playbook_registry.resolve_playbook(PLAYBOOK_ID), "bundled_sample")
        )

    def test_sample_validates_against_the_runtime_schema(self):
        doc = playbook_validation.load_and_validate_playbook(PLAYBOOK_ID)
        self.assertEqual(doc["playbook"]["id"], "synthetic-nda-sample")
        self.assertGreaterEqual(len(doc["hard_rejections"]), 1)

    def test_sample_is_not_the_old_coming_soon_stub(self):
        """playbooks/nda-v0.1.0.json (untouched by this ticket -- still the
        harvest source tests/test_nda_policy.py pins) is a stub whose
        general_principles[0] says it "governs no production review". The
        bundled sample must be genuinely different content -- proven here
        by a topic id that only exists in the new sample, and by the
        stub's own self-description being absent."""
        doc = playbook_validation.load_and_validate_playbook(PLAYBOOK_ID)
        topic_ids = {t["id"] for t in doc["topics"]}
        self.assertIn("nda-compelled-disclosure", topic_ids)
        blob = json.dumps(doc).lower()
        self.assertNotIn("coming soon", blob)
        self.assertNotIn("governs no production review", blob)
        self.assertNotIn("placeholder", blob)

    def test_sample_is_brand_free(self):
        """No user-facing the tenant name anywhere in the new content -- checks
        string VALUES only (same convention as
        tests/test_example_playbook_registry.py's own de-identification
        check): 'exos_party' is a schema FIELD NAME, unrelated to this
        ticket's brand-free requirement for the content itself."""
        doc = playbook_validation.load_and_validate_playbook(PLAYBOOK_ID)
        offending = [s for s in _iter_strings(doc) if "exos" in s.lower()]
        self.assertEqual(offending, [], f"'Exos' found in sample playbook content: {offending}")


# ---------------------------------------------------------------------------
# 2. src.sample_playbooks.seed_shipped_playbook -- what it refuses to
#    install, and what a fresh install actually writes.
# ---------------------------------------------------------------------------


class TestSeedRefusals(unittest.TestCase):
    def test_nothing_is_active_before_the_seed_runs(self):
        ddb = FakeDDB(FakeReviewsTable())
        self.assertIsNone(reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb))

    def test_test_only_registry_entry_is_refused(self):
        """synthetic-generic is a real, registered, schema-valid playbook --
        but it is marked `"test_only": true`, so it exists for the fixtures
        suite and must never be installed into a deployment. Nothing is
        written when it is refused."""
        ddb = FakeDDB(FakeReviewsTable())
        with self.assertRaises(sample_playbooks.SampleNotAvailableError):
            _seed(ddb, "synthetic-generic")
        self.assertEqual(ddb._playbook_versions.items, {})
        self.assertEqual(ddb._audit.items, [])

    def test_unregistered_playbook_id_is_refused(self):
        ddb = FakeDDB(FakeReviewsTable())
        with self.assertRaises(sample_playbooks.SampleNotAvailableError):
            _seed(ddb, "not-a-real-playbook")

    def test_content_that_fails_runtime_validation_is_refused(self):
        """Fail closed rather than install content that would not itself
        resolve as valid -- and leave NOTHING behind (no version row, no
        active hash), so a broken image cannot half-install a playbook."""
        ddb = FakeDDB(FakeReviewsTable())
        with patch.object(
            playbook_validation,
            "load_and_validate_playbook",
            side_effect=playbook_validation.PlaybookValidationError("boom"),
        ):
            with self.assertRaises(sample_playbooks.SampleInvalidError):
                _seed(ddb)
        self.assertEqual(ddb._playbook_versions.items, {})
        self.assertIsNone(reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb))


class TestFreshDeploySeed(unittest.TestCase):
    """THE ISSUE #433 ACCEPTANCE CRITERION: a fresh deployment (empty
    tables) comes up with the shipped playbook already ACTIVE, with the
    same version-row/audit-trail shape any admin-uploaded-and-activated
    playbook would have.

    `test_the_real_bootstrap_entry_point_seeds_an_active_playbook` drives
    `deploy/dts/bootstrap.py` itself rather than calling
    `sample_playbooks.seed_shipped_playbook` directly -- otherwise this
    file would keep passing if the bootstrap stopped calling the seed at
    all, which is exactly the regression that would ship an empty catalog.
    """

    def test_seed_writes_the_real_content_hash_and_makes_it_active(self):
        ddb = FakeDDB(FakeReviewsTable())
        result = _seed(ddb)

        expected_hash = seed_active_bundle.compute_seed_hash(PLAYBOOK_ID)
        self.assertTrue(expected_hash.startswith("sha256:"))
        self.assertEqual(result["content_hash"], expected_hash)
        self.assertEqual(result["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(result["status"], "active")

        active_hash = reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb)
        self.assertEqual(active_hash, expected_hash)

    def test_seed_writes_a_real_version_row_through_the_ordinary_functions(self):
        """The row an admin-uploaded version would produce: written by
        `record_playbook_version_upload` (so it carries `uploaded_by` /
        `uploaded_at` and shows up in the trail), flipped to `active` by
        `activate_playbook_version`, carrying the registry's `seed_notes`
        as the admin-editable `notes` that `GET /api/playbooks` reads."""
        ddb = FakeDDB(FakeReviewsTable())
        entry = playbook_registry.resolve_playbook(PLAYBOOK_ID)
        self.assertTrue(entry.seed_notes, "registry entry must carry seed_notes to prove anything here")

        result = _seed(ddb)

        row = ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SEED_VERSION)]
        self.assertEqual(row["status"], pv.STATUS_ACTIVE)
        self.assertEqual(row["notes"], entry.seed_notes)
        self.assertEqual(row["content_hash"], result["content_hash"])
        self.assertEqual(row["uploaded_by"], sample_playbooks.SEED_ACTOR)

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, ddb)
        self.assertEqual([t["version"] for t in trail], [sample_playbooks._SEED_VERSION])

        # The exact read path GET /api/playbooks uses.
        self.assertEqual(pv.get_active_version_notes(PLAYBOOK_ID, ddb), entry.seed_notes)

    def test_seed_writes_its_own_audit_entry_alongside_the_lifecycle_ones(self):
        """The seed appends one row of its own; `src.playbook_versions`
        appends the ordinary `release_bundle_activate` (and, because this
        playbook ships a note, `playbook_version_notes_update`) rows -- so
        the trail is indistinguishable from an ordinary install apart from
        the actor being the deploy step."""
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)

        own = [e for e in ddb._audit.items if e["action"] == "shipped_playbook_seeded"]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["actor"], sample_playbooks.SEED_ACTOR)
        self.assertEqual(own[0]["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(own[0]["outcome"], "success")

        actions = [e.get("action") for e in ddb._audit.items]
        self.assertIn("release_bundle_activate", actions)

    def test_the_real_bootstrap_entry_point_seeds_an_active_playbook(self):
        """Drive `deploy/dts/bootstrap.py::seed_shipped_playbook` against
        EMPTY tables -- the actual deploy step, not a re-implementation."""
        ddb = FakeDDB(FakeReviewsTable())
        self.assertIsNone(reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb))

        with patch.object(dts_bootstrap, "_ddb_resource", return_value=ddb):
            dts_bootstrap.seed_shipped_playbook()

        self.assertEqual(
            reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb),
            seed_active_bundle.compute_seed_hash(PLAYBOOK_ID),
            "FRESH DEPLOY ACTIVE: False -- the bootstrap did not leave the "
            "shipped playbook active",
        )
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SEED_VERSION)]["status"],
            pv.STATUS_ACTIVE,
        )

    def test_a_fresh_deployment_catalog_is_never_empty(self):
        """The user-visible consequence: after the bootstrap runs, the
        catalog `GET /api/playbooks` serves has at least one ACTIVE entry,
        so the SPA never opens on the empty-shell state out of the box."""
        ddb = FakeDDB(FakeReviewsTable())
        with patch.object(dts_bootstrap, "_ddb_resource", return_value=ddb):
            dts_bootstrap.seed_shipped_playbook()

        catalog = review_routes._load_playbook_catalog(review_routes.PLAYBOOK_REGISTRY_PATH, ddb)
        active = [row for row in catalog if row["status"] == "active"]
        self.assertEqual([row["playbook_id"] for row in active], [PLAYBOOK_ID])
        self.assertNotIn(
            "has_bundled_sample",
            catalog[0],
            "issue #433: the catalog carries no sample-only field any more",
        )


class TestSeedInstallsOnceAndNeverRestomps(unittest.TestCase):
    """The bootstrap re-runs on every container start. Each test here is a
    second run over state a fresh install (or an admin) already produced."""

    def test_second_run_is_a_no_op_skip(self):
        ddb = FakeDDB(FakeReviewsTable())
        first = _seed(ddb)
        second = _seed(ddb)

        self.assertEqual(first["status"], "active")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "already_installed")

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, ddb)
        self.assertEqual(len(trail), 1, "a second run must not duplicate the version row")
        own = [e for e in ddb._audit.items if e["action"] == "shipped_playbook_seeded"]
        self.assertEqual(len(own), 1, "a skipped run must not append an audit row")
        self.assertEqual(
            reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb),
            first["content_hash"],
        )

    def test_admin_edited_note_survives_a_second_run(self):
        """`notes` is admin-editable exactly like any other playbook's note
        (issue #411's `update_playbook_version_notes`); a container restart
        must never revert it to the shipped seed text."""
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)

        edited_note = "OUR INTERNAL NDA GUIDANCE"
        pv.update_playbook_version_notes(
            PLAYBOOK_ID, sample_playbooks._SEED_VERSION, edited_note, "admin-1", ddb
        )

        _seed(ddb)

        self.assertEqual(
            pv.get_active_version_notes(PLAYBOOK_ID, ddb),
            edited_note,
            "ADMIN EDIT SURVIVED: False -- a re-run must not reseed the "
            "shipped note over an admin's edit",
        )

    def test_a_removed_playbook_is_not_resurrected_by_a_restart(self):
        """The sharp one. `remove_playbook` DELETES the version rows as
        part of tombstoning, so a seed that only checked "are there version
        rows?" would happily re-install on the next container start and
        undo the admin's removal."""
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)
        pv.remove_playbook(PLAYBOOK_ID, "admin-1", ddb)

        result = _seed(ddb)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "removed_by_admin")
        self.assertEqual(pv.list_playbook_version_trail(PLAYBOOK_ID, ddb), [])
        self.assertIsNone(
            reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb),
            "STAYED REMOVED: False -- a restart resurrected a playbook the "
            "admin deliberately removed",
        )
        catalog = review_routes._load_playbook_catalog(review_routes.PLAYBOOK_REGISTRY_PATH, ddb)
        self.assertEqual([row["playbook_id"] for row in catalog], [])

    def test_an_admin_uploaded_newer_version_is_left_alone_by_a_restart(self):
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)
        pv.record_playbook_version_upload(
            PLAYBOOK_ID, "1.1.0", "admin-1", ddb, content_hash="sha256:" + ("c" * 64)
        )
        pv.activate_playbook_version(PLAYBOOK_ID, "1.1.0", "admin-1", ddb)

        result = _seed(ddb)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, "1.1.0")]["status"], pv.STATUS_ACTIVE
        )
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SEED_VERSION)]["status"],
            pv.STATUS_RETIRED,
        )

    def test_a_renamed_playbook_keeps_its_name_when_the_seed_runs_again(self):
        """The seed's resolver wiring uses `update_item`, never `put_item`,
        so it can never clobber the `display_name` / `removed` attributes
        an admin set on the same `playbooks` row."""
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)
        pv.rename_playbook(PLAYBOOK_ID, "Our House NDA", "admin-1", ddb)

        _seed(ddb)

        self.assertEqual(
            ddb._playbooks.items[PLAYBOOK_ID]["display_name"],
            "Our House NDA",
        )

    def test_seeded_playbook_accepts_a_new_version_and_rolls_back_like_any_playbook(self):
        """The seeded row is genuinely first-class: an admin uploads 1.1.0
        through the normal governed `record_playbook_version_upload` path,
        activates it (which RETIRES the seeded 1.0.0 row, never deletes
        it), and rolls back to 1.0.0 -- all via plain `src.playbook_versions`
        calls, with zero special-casing."""
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)

        new_version = "1.1.0"
        pv.record_playbook_version_upload(
            PLAYBOOK_ID, new_version, "admin-1", ddb, content_hash="sha256:" + ("c" * 64)
        )
        pv.activate_playbook_version(PLAYBOOK_ID, new_version, "admin-1", ddb)

        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, new_version)]["status"], pv.STATUS_ACTIVE
        )
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SEED_VERSION)]["status"],
            pv.STATUS_RETIRED,
            "uploading and activating a new version must retire, never delete, the seeded row",
        )

        pv.rollback_playbook_version(PLAYBOOK_ID, sample_playbooks._SEED_VERSION, "admin-1", ddb)

        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SEED_VERSION)]["status"],
            pv.STATUS_ACTIVE,
        )
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, new_version)]["status"], pv.STATUS_RETIRED
        )
        entry = playbook_registry.resolve_playbook(PLAYBOOK_ID)
        self.assertEqual(
            pv.get_active_version_notes(PLAYBOOK_ID, ddb),
            entry.seed_notes,
            "rolling back to the seeded version must restore its seeded note",
        )


class TestSeededPlaybookRenameAndRemove(unittest.TestCase):
    """The shipped playbook must be RENAMABLE and REMOVABLE through the
    normal admin path -- Marc's verbatim intent, "make it removable and
    updatable and renamable etc just like a real playbook".

    Both are DB overrides on the `playbooks` row rather than edits to
    `playbooks/registry.json`, because the registry is a file baked into the
    image: a rename written there would not survive a deploy, and the file
    is not writable at runtime. These tests pin that the override actually
    reaches the catalog."""

    def _catalog(self, ddb):
        return review_routes._load_playbook_catalog(
            review_routes.PLAYBOOK_REGISTRY_PATH, ddb
        )

    def _entry(self, ddb, playbook_id=None):
        target = playbook_id or PLAYBOOK_ID
        return next(
            (row for row in self._catalog(ddb) if row["playbook_id"] == target), None
        )

    def test_rename_overrides_the_shipped_display_name_in_the_catalog(self):
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)
        shipped = self._entry(ddb)["display_name"]

        pv.rename_playbook(PLAYBOOK_ID, "Our House NDA", "admin-1", ddb)

        renamed = self._entry(ddb)
        self.assertEqual(renamed["display_name"], "Our House NDA")
        self.assertNotEqual(renamed["display_name"], shipped)
        self.assertEqual(
            renamed["playbook_id"],
            PLAYBOOK_ID,
            "a rename must NOT change the playbook_id every version row and "
            "review record is keyed on",
        )

    def test_rename_to_empty_string_restores_the_shipped_name(self):
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)
        shipped = self._entry(ddb)["display_name"]

        pv.rename_playbook(PLAYBOOK_ID, "Temporary", "admin-1", ddb)
        pv.rename_playbook(PLAYBOOK_ID, "", "admin-1", ddb)

        self.assertEqual(self._entry(ddb)["display_name"], shipped)

    def test_remove_drops_it_from_the_catalog_and_deletes_its_version_rows(self):
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)
        self.assertIsNotNone(self._entry(ddb))

        result = pv.remove_playbook(PLAYBOOK_ID, "admin-1", ddb)

        self.assertIsNone(
            self._entry(ddb),
            "a removed playbook must not re-appear in the catalog -- the "
            "registry file still lists it, so the DB tombstone is what "
            "makes removal real",
        )
        self.assertEqual(result["versions_deleted"], 1)
        self.assertEqual(
            [k for k in ddb._playbook_versions.items if k[0] == PLAYBOOK_ID],
            [],
            "remove must delete the playbook's version rows",
        )
        self.assertIsNone(
            reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb),
            "a removed playbook must never still resolve as active",
        )

    # NOTE (issue #433): the deleted `test_re_activating_a_removed_sample_
    # brings_it_back` asserted the OPPOSITE of what must now be true, and is
    # replaced by `TestSeedInstallsOnceAndNeverRestomps::
    # test_a_removed_playbook_is_not_resurrected_by_a_restart`. It relied on
    # `sample_playbooks._clear_removed_tombstone` -- a sample-ONLY branch
    # that cleared the `removed` tombstone on every activation. As a
    # deploy-time seed that re-runs on every container start, that branch
    # would silently undo an admin's removal on the next restart, so it is
    # gone. The consequence is honest and shared with every other playbook:
    # `remove_playbook` is a one-way door via the API today (nothing in
    # `src.playbook_versions` clears the tombstone). Restoring a removed
    # playbook belongs to the Playbooks admin tab (#434), not to a
    # sample-only escape hatch here.

    def test_full_lifecycle_upload_activate_rollback_rename_remove(self):
        """The end-to-end lifecycle finding 5 asked for, in one pass."""
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)

        pv.record_playbook_version_upload(
            PLAYBOOK_ID, "1.1.0", "admin-1", ddb, content_hash="sha256:" + ("d" * 64)
        )
        pv.activate_playbook_version(PLAYBOOK_ID, "1.1.0", "admin-1", ddb)
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, "1.1.0")]["status"], pv.STATUS_ACTIVE
        )

        pv.rollback_playbook_version(
            PLAYBOOK_ID, sample_playbooks._SEED_VERSION, "admin-1", ddb
        )
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SEED_VERSION)][
                "status"
            ],
            pv.STATUS_ACTIVE,
        )

        pv.rename_playbook(PLAYBOOK_ID, "House NDA", "admin-1", ddb)
        self.assertEqual(self._entry(ddb)["display_name"], "House NDA")

        removed = pv.remove_playbook(PLAYBOOK_ID, "admin-1", ddb)
        self.assertIsNone(self._entry(ddb))
        self.assertEqual(
            removed["versions_deleted"], 2, "both 1.0.0 and 1.1.0 rows must be deleted"
        )

    def test_rename_and_remove_are_audited(self):
        ddb = FakeDDB(FakeReviewsTable())
        _seed(ddb)

        pv.rename_playbook(PLAYBOOK_ID, "Audited Name", "admin-1", ddb)
        pv.remove_playbook(PLAYBOOK_ID, "admin-1", ddb)

        actions = [row.get("action") for row in ddb._audit.items]
        self.assertIn("playbook_renamed", actions)
        self.assertIn("playbook_removed", actions)


# ---------------------------------------------------------------------------
# 3. Seeding it -> reviews actually run against it.
# ---------------------------------------------------------------------------


class TestSeededPlaybookRunsRealReviews(unittest.TestCase):
    def test_review_against_the_seeded_playbook_reaches_done(self):
        reviews_table = FakeReviewsTable()
        ddb = FakeDDB(reviews_table)

        activation = _seed(ddb)

        docx_bytes = _build_docx_bytes()
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_real_pipeline(
                REVIEW_ID,
                _payload(activation["content_hash"]),
                dynamodb_resource=ddb,
                s3_client=s3,
                model_client=_fake_client(),
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(reviews_table.item["decision"], "REQUEST_CHANGE")
        self.assertEqual(reviews_table.item.get("output_s3_key"), f"outputs/{REVIEW_ID}/out.docx")
        # Issue #416 added a second put -- outputs/{rid}/analysis.json -- so this
        # asserts THE REDLINE was written rather than a total object count,
        # which is what it always meant.
        self.assertIn(f"outputs/{REVIEW_ID}/out.docx", [put["Key"] for put in s3.puts])
        settle.assert_called_once()

    def test_review_without_the_seed_is_quarantined_not_run(self):
        """The empty-shell contrast case: WITHOUT the seed having run, the
        exact same submission is quarantined at issue #401's
        verify_active_bundle gate -- it never falls through to a computed
        review, proving the seed is actually load-bearing here, not
        cosmetic."""
        reviews_table = FakeReviewsTable()
        ddb = FakeDDB(reviews_table)  # nothing installed

        docx_bytes = _build_docx_bytes()
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation") as settle, patch.object(
            pr, "_load_playbook_bundle"
        ) as load_bundle:
            pr.run_real_pipeline(
                REVIEW_ID,
                _payload("some-hash-that-was-never-activated"),
                dynamodb_resource=ddb,
                s3_client=s3,
                model_client=object(),
            )

        load_bundle.assert_not_called()
        self.assertEqual(reviews_table.item["status"], "QUARANTINED")
        self.assertEqual(reviews_table.item["quarantine_reason"], "submission_time_bundle_retired")
        settle.assert_called_once()


def _run_tests() -> int:
    # EXPLICIT class registry, NOT auto-discovery: a new TestCase class
    # silently does not run until it is added here.
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestShippedPlaybookIsRealContent,
        TestSeedRefusals,
        TestFreshDeploySeed,
        TestSeedInstallsOnceAndNeverRestomps,
        TestSeededPlaybookRenameAndRemove,
        TestSeededPlaybookRunsRealReviews,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
