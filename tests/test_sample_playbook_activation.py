#!/usr/bin/env python3
"""
Executable tests for issue #402 (bundle a synthetic NDA sample playbook +
first-run activate flow) and issue #412 (rename it to the one shipped
"Synthetic NDA Sample" playbook, and give it a real playbook_versions row
with a seeded, admin-editable note).

## What this proves

  1. The bundled sample (playbooks/samples/synthetic-nda-sample-v1.0.0.json)
     is real content -- schema-valid, brand-free, and genuinely different
     from the OLD "coming soon" placeholder stub (playbooks/nda-v0.1.0.json,
     left untouched here -- see tests/test_nda_policy.py's own
     harvested-but-not-wired gate, which this ticket does not touch).
  2. registry.json's "synthetic-nda-sample" entry marks itself
     `"bundled_sample": true` and is the registry's `default_playbook_id`,
     and ONLY a playbook_id the registry marks that way is reachable
     through the low-ceremony activation path (src.sample_playbooks) --
     issue #289's type-blindness convention, extended: no playbook_id
     literal gates activation, a registry field does.
  3. Before activation, "synthetic-nda-sample" resolves as NOT active (the
     empty-shell state issue #401 built).
     `src.sample_playbooks.activate_bundled_sample`:
       - 403s a non-admin caller.
       - raises `SampleNotAvailableError` for a playbook_id with no
         bundled sample (unregistered, or registered without the marker).
       - for the real, admin, bundled-sample case: writes the SAME
         content_hash `scripts/seed_active_bundle.py` would compute onto
         `PLAYBOOKS_TABLE`, appends one of its own audit rows, and (issue
         #412) gives the sample a real `playbook_versions` row via
         `src.playbook_versions`' own upload/activate/notes functions.
  4. After activation, "synthetic-nda-sample" resolves as active
     (`reviews._read_active_release_bundle_hash`) -- the same read
     `GET /api/playbooks` (issue #272, exercised directly in
     tests/test_playbook_catalog_endpoint.py) and the submission path both
     depend on. The registry's `seed_notes` survives activation as that
     version row's `notes` -- the same field `GET /api/playbooks` surfaces
     (issue #411's `get_active_version_notes`) and an admin can edit like
     any other playbook's note.
  5. THE ACCEPTANCE CRITERION: with "synthetic-nda-sample" activated, a
     submitted review against it runs a REAL review end to end --
     backend/src/pipeline_runner.py's `run_real_pipeline` (issue
     #259/#401), driven by a `FakeBedrockClient` (no live model call, per
     this repo's standing "no network in any test" rule), reaches DONE
     with a genuinely computed REQUEST_CHANGE decision and a real
     tracked-changes output object -- never QUARANTINED, never stuck
     PENDING/RUNNING. The contrasting case (submitting the SAME payload
     without activating first) is quarantined at #401's
     verify_active_bundle gate, `_load_playbook_bundle` never called --
     proving activation is actually load-bearing, not cosmetic.

Run standalone: `python3 tests/test_sample_playbook_activation.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

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

from fastapi import HTTPException  # noqa: E402

import model_client as model_client_module  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import playbook_registry  # noqa: E402
import playbook_validation  # noqa: E402
import playbook_versions as pv  # noqa: E402
import reviews as reviews_module  # noqa: E402
import sample_playbooks  # noqa: E402
import seed_active_bundle  # noqa: E402
import src.review_routes as review_routes  # noqa: E402 -- issue #412 rename/remove reach the catalog

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
    """PLAYBOOKS_TABLE (PK: playbook_id) -- seed_active_bundle.seed_active_bundle
    (which src.sample_playbooks.activate_bundled_sample reuses) writes here
    with a plain put_item; reviews._read_active_release_bundle_hash reads
    it back with get_item."""

    def __init__(self):
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key):
        item = self.items.get(Key["playbook_id"])
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[Item["playbook_id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None):
        """Issue #412: `playbook_versions.rename_playbook` / `remove_playbook`
        and `sample_playbooks._clear_removed_tombstone` set the catalog
        override attributes (`display_name`, `removed`) on this row -- the
        real table's update_item creates the item if absent, so this does
        too."""
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
    """PLAYBOOK_VERSIONS_TABLE (PK: playbook_id, SK: version) -- issue #412:
    `src.sample_playbooks.activate_bundled_sample` now writes/activates/
    notes a REAL version row for the bundled sample via
    `src.playbook_versions`' own functions, the exact same ones every other
    playbook_id's governed upload/activate/rollback/notes lifecycle uses.
    `.meta.client.exceptions.ConditionalCheckFailedException` mirrors just
    enough of boto3's real `Table` shape for
    `playbook_versions.record_playbook_version_upload`'s append-only
    conflict check (a repeat activation re-uploading the same
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

    def put_object(self, Bucket, Key, Body):
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _admin_row() -> dict[str, Any]:
    return {"cognito_sub": "admin-1", "status": "active", "is_admin": True}


def _non_admin_row() -> dict[str, Any]:
    return {"cognito_sub": "attorney-1", "status": "active", "is_admin": False}


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
# 1. The bundled sample is real, schema-valid, brand-free content.
# ---------------------------------------------------------------------------


class TestBundledSampleIsRealContent(unittest.TestCase):
    def test_registry_marks_synthetic_nda_sample_as_a_bundled_sample(self):
        entry = playbook_registry.resolve_playbook(PLAYBOOK_ID)
        self.assertTrue(entry.bundled_sample)
        self.assertEqual(entry.playbook_path.name, "synthetic-nda-sample-v1.0.0.json")
        self.assertEqual(playbook_registry.profile(entry), "knowledge")
        self.assertEqual(playbook_registry.default_playbook_id(), PLAYBOOK_ID)

    def test_other_registered_playbooks_are_not_marked_bundled_sample(self):
        """The marker is specific to synthetic-nda-sample -- it must not
        accidentally leak onto synthetic-generic (the renamed, test-only
        "eiaa" entry), which would let the low-ceremony path activate it
        with no Gate 7."""
        entry = playbook_registry.resolve_playbook("synthetic-generic")
        self.assertFalse(entry.bundled_sample)

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
# 2. src.sample_playbooks.activate_bundled_sample.
# ---------------------------------------------------------------------------


class TestActivateBundledSample(unittest.TestCase):
    def test_nda_is_not_active_before_activation(self):
        ddb = FakeDDB(FakeReviewsTable())
        self.assertIsNone(reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb))

    def test_non_admin_caller_is_refused(self):
        ddb = FakeDDB(FakeReviewsTable())
        with self.assertRaises(HTTPException) as ctx:
            sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _non_admin_row(), ddb)
        self.assertEqual(ctx.exception.status_code, 403)
        # Refused before anything is written.
        self.assertIsNone(reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb))

    def test_playbook_id_with_no_bundled_sample_is_refused(self):
        """synthetic-generic is a real, registered, schema-valid playbook --
        but it is NOT marked "bundled_sample", so this low-ceremony path
        must refuse it exactly like an unregistered id (Gate 7 stays the
        only way to activate it)."""
        ddb = FakeDDB(FakeReviewsTable())
        with self.assertRaises(sample_playbooks.SampleNotAvailableError):
            sample_playbooks.activate_bundled_sample("synthetic-generic", _admin_row(), ddb)

    def test_unregistered_playbook_id_is_refused(self):
        ddb = FakeDDB(FakeReviewsTable())
        with self.assertRaises(sample_playbooks.SampleNotAvailableError):
            sample_playbooks.activate_bundled_sample("not-a-real-playbook", _admin_row(), ddb)

    def test_activation_seeds_the_real_content_hash_and_makes_nda_active(self):
        ddb = FakeDDB(FakeReviewsTable())
        result = sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        expected_hash = seed_active_bundle.compute_seed_hash(PLAYBOOK_ID)
        self.assertTrue(expected_hash.startswith("sha256:"))
        self.assertEqual(result["content_hash"], expected_hash)
        self.assertEqual(result["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(result["status"], "active")

        active_hash = reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb)
        self.assertEqual(active_hash, expected_hash)

    def test_activation_writes_its_own_audit_entry(self):
        """Issue #412: activation now ALSO writes to the playbook_versions
        row's own audit trail (release_bundle_activate, and
        playbook_version_notes_update since this sample carries seed_notes)
        -- so this asserts THIS module's own `bundled_sample_activate` entry
        is present, rather than asserting the audit table's total row
        count."""
        ddb = FakeDDB(FakeReviewsTable())
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        own_entries = [e for e in ddb._audit.items if e["action"] == "bundled_sample_activate"]
        self.assertEqual(len(own_entries), 1)
        entry = own_entries[0]
        self.assertEqual(entry["actor"], "admin-1")
        self.assertEqual(entry["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(entry["outcome"], "success")

    def test_activation_is_repeatable(self):
        """A second click (e.g. after a page reload) is a no-op on the
        active hash, not an error -- same idempotent-write posture as
        seed_active_bundle's own put_item. Each click still appends its
        own audit row (append-only, never deduped)."""
        ddb = FakeDDB(FakeReviewsTable())
        first = sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)
        second = sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)
        self.assertEqual(first["content_hash"], second["content_hash"])
        own_entries = [e for e in ddb._audit.items if e["action"] == "bundled_sample_activate"]
        self.assertEqual(len(own_entries), 2)

    def test_activation_seeds_a_real_playbook_versions_row_with_the_note(self):
        """THE #412 ACCEPTANCE CRITERION: the bundled sample gets a REAL
        `playbook_versions` row (not a bundled-sample special case), and
        the registry's `seed_notes` survives activation as that row's
        admin-editable `notes` -- exactly what `GET /api/playbooks` (issue
        #411's `get_active_version_notes`) reads."""
        ddb = FakeDDB(FakeReviewsTable())
        entry = playbook_registry.resolve_playbook(PLAYBOOK_ID)
        self.assertTrue(entry.seed_notes, "registry entry must carry seed_notes to prove anything here")

        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        row = ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION)]
        self.assertEqual(row["status"], pv.STATUS_ACTIVE)
        self.assertEqual(row["notes"], entry.seed_notes)

        # The exact read path GET /api/playbooks uses.
        self.assertEqual(pv.get_active_version_notes(PLAYBOOK_ID, ddb), entry.seed_notes)

    def test_second_activation_does_not_duplicate_or_lose_the_version_row(self):
        ddb = FakeDDB(FakeReviewsTable())
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        trail = pv.list_playbook_version_trail(PLAYBOOK_ID, ddb)
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["version"], sample_playbooks._SAMPLE_VERSION)
        entry = playbook_registry.resolve_playbook(PLAYBOOK_ID)
        self.assertEqual(pv.get_active_version_notes(PLAYBOOK_ID, ddb), entry.seed_notes)

    def test_admin_edited_note_survives_a_second_activation(self):
        """Regression for issue #412 fix-round-1 finding 1: a re-click of
        activate-sample must NOT silently revert an admin's edited note back
        to the shipped seed text. `_activate_version_row` must seed `notes`
        only when the version row is newly created, never on a repeat
        activation -- `notes` is admin-editable exactly like any other
        playbook's note (issue #411's `update_playbook_version_notes`)."""
        ddb = FakeDDB(FakeReviewsTable())
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        edited_note = "OUR INTERNAL NDA GUIDANCE"
        pv.update_playbook_version_notes(
            PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION, edited_note, "admin-1", ddb
        )
        self.assertEqual(pv.get_active_version_notes(PLAYBOOK_ID, ddb), edited_note)

        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        self.assertEqual(
            pv.get_active_version_notes(PLAYBOOK_ID, ddb),
            edited_note,
            "ADMIN EDIT SURVIVED: False -- a second activation must not "
            "reseed the shipped note over an admin's edit",
        )

    def test_content_hash_is_reconciled_on_a_second_activation_after_a_content_change(self):
        """Regression for issue #412 fix-round-1 finding 2: because
        `_SAMPLE_VERSION` is a fixed literal, a second activation after the
        on-disk sample content changed must not leave the `playbook_versions`
        row's `content_hash` stale relative to `playbooks.
        active_release_bundle_hash` -- both must end up in sync, and the
        reconciliation must be audited."""
        ddb = FakeDDB(FakeReviewsTable())
        first = sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        changed_hash = "sha256:" + ("a" * 64)
        self.assertNotEqual(first["content_hash"], changed_hash)

        with patch.object(seed_active_bundle, "compute_seed_hash", return_value=changed_hash):
            second = sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        self.assertEqual(second["content_hash"], changed_hash)

        row = ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION)]
        active_hash = reviews_module._read_active_release_bundle_hash(PLAYBOOK_ID, ddb)
        self.assertEqual(
            row["content_hash"],
            active_hash,
            f"IN SYNC: False -- version row content_hash={row['content_hash']!r} vs "
            f"playbooks.active_release_bundle_hash={active_hash!r}",
        )
        self.assertEqual(row["content_hash"], changed_hash)

        reconcile_entries = [
            e for e in ddb._audit.items if e["action"] == "bundled_sample_content_hash_reconciled"
        ]
        self.assertEqual(len(reconcile_entries), 1)
        self.assertEqual(reconcile_entries[0]["previous_content_hash"], first["content_hash"])
        self.assertEqual(reconcile_entries[0]["content_hash"], changed_hash)

    def test_sample_accepts_a_new_uploaded_version_and_rolls_back_like_any_playbook(self):
        """Partial coverage for issue #412's Scope bullet ('accepts a new
        uploaded version, rolls back ... through the normal admin paths'),
        added per fix-round-1 finding 5. Proves the sample's REAL
        `playbook_versions` row is genuinely first-class for the two
        lifecycle operations that already exist in this codebase for every
        playbook_id: an admin uploads version 1.1.0 through the normal
        governed `record_playbook_version_upload` path, activates it (which
        retires the seeded 1.0.0 row, never deletes it), and rolls back to
        1.0.0 -- all via plain `src.playbook_versions` calls, with zero
        special-casing for the bundled sample.

        Rename/remove are covered separately by
        `TestBundledSampleRenameAndRemove` below (issue #412 fix-round-1
        finding 5, implemented rather than waived)."""
        ddb = FakeDDB(FakeReviewsTable())
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        new_version = "1.1.0"
        new_hash = "sha256:" + ("c" * 64)
        pv.record_playbook_version_upload(
            PLAYBOOK_ID, new_version, "admin-1", ddb, content_hash=new_hash
        )
        pv.activate_playbook_version(PLAYBOOK_ID, new_version, "admin-1", ddb)

        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, new_version)]["status"], pv.STATUS_ACTIVE
        )
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION)]["status"],
            pv.STATUS_RETIRED,
            "uploading and activating a new version must retire, never delete, the seeded row",
        )

        pv.rollback_playbook_version(
            PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION, "admin-1", ddb
        )

        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION)]["status"],
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


class TestBundledSampleRenameAndRemove(unittest.TestCase):
    """Issue #412 fix-round-1 finding 5: the shipped sample must be
    RENAMABLE and REMOVABLE through the normal admin path, not just
    activatable -- Marc's verbatim intent, "make it removable and updatable
    and renamable etc just like a real playbook".

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
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)
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
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)
        shipped = self._entry(ddb)["display_name"]

        pv.rename_playbook(PLAYBOOK_ID, "Temporary", "admin-1", ddb)
        pv.rename_playbook(PLAYBOOK_ID, "", "admin-1", ddb)

        self.assertEqual(self._entry(ddb)["display_name"], shipped)

    def test_remove_drops_it_from_the_catalog_and_deletes_its_version_rows(self):
        ddb = FakeDDB(FakeReviewsTable())
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)
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

    def test_re_activating_a_removed_sample_brings_it_back(self):
        """Removal of the SHIPPED sample must be reversible -- otherwise a
        stray click permanently bricks the one playbook a fresh install
        ships with."""
        ddb = FakeDDB(FakeReviewsTable())
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)
        pv.remove_playbook(PLAYBOOK_ID, "admin-1", ddb)
        self.assertIsNone(self._entry(ddb))

        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        restored = self._entry(ddb)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["status"], "active")
        self.assertEqual(
            restored["notes"],
            playbook_registry.resolve_playbook(PLAYBOOK_ID).seed_notes,
            "a re-activated sample gets a freshly-seeded row, so its shipped "
            "note is back",
        )

    def test_full_lifecycle_upload_activate_rollback_rename_remove(self):
        """The end-to-end lifecycle finding 5 asked for, in one pass."""
        ddb = FakeDDB(FakeReviewsTable())
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        pv.record_playbook_version_upload(
            PLAYBOOK_ID, "1.1.0", "admin-1", ddb, content_hash="sha256:" + ("d" * 64)
        )
        pv.activate_playbook_version(PLAYBOOK_ID, "1.1.0", "admin-1", ddb)
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, "1.1.0")]["status"], pv.STATUS_ACTIVE
        )

        pv.rollback_playbook_version(
            PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION, "admin-1", ddb
        )
        self.assertEqual(
            ddb._playbook_versions.items[(PLAYBOOK_ID, sample_playbooks._SAMPLE_VERSION)][
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
        sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

        pv.rename_playbook(PLAYBOOK_ID, "Audited Name", "admin-1", ddb)
        pv.remove_playbook(PLAYBOOK_ID, "admin-1", ddb)

        actions = [row.get("action") for row in ddb._audit.items]
        self.assertIn("playbook_renamed", actions)
        self.assertIn("playbook_removed", actions)


# ---------------------------------------------------------------------------
# 3. The acceptance criterion: activating it -> reviews run.
# ---------------------------------------------------------------------------


class TestActivatedSampleRunsRealReviews(unittest.TestCase):
    def test_review_against_the_activated_sample_reaches_done(self):
        reviews_table = FakeReviewsTable()
        ddb = FakeDDB(reviews_table)

        activation = sample_playbooks.activate_bundled_sample(PLAYBOOK_ID, _admin_row(), ddb)

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
        self.assertEqual(len(s3.puts), 1)
        self.assertEqual(s3.puts[0]["Key"], f"outputs/{REVIEW_ID}/out.docx")
        settle.assert_called_once()

    def test_review_without_activation_is_quarantined_not_run(self):
        """The empty-shell contrast case: WITHOUT activating first, the
        exact same submission is quarantined at issue #401's
        verify_active_bundle gate -- it never falls through to a computed
        review, proving activation is actually load-bearing here, not
        cosmetic."""
        reviews_table = FakeReviewsTable()
        ddb = FakeDDB(reviews_table)  # nothing activated

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
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestBundledSampleIsRealContent,
        TestActivateBundledSample,
        TestBundledSampleRenameAndRemove,
        TestActivatedSampleRunsRealReviews,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
