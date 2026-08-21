#!/usr/bin/env python3
"""
Unit tests for backend/src/pipeline_runner.py's Phase 2 REAL pipeline body
(issue #259: "wire the review spine into the Docker Compose in-process runner via
OpenRouterModelClient").

## Root problem this proves fixed

Before this slice, the Docker Compose in-process runner's ONLY body was
`run_mock_pipeline` -- a canned, pre-baked synthetic-generic fixture copy
(the synthetic-generic registry entry's `mock_output_key`), never a genuinely computed review. This
test drives the NEW real-pipeline entry point (`pipeline_runner.
run_real_pipeline`) with `scripts/review_spine.py::run_review` (issue #239)
and a `FakeBedrockClient` (backend/src/model_client.py) injected in place
of a live OpenRouter call -- fully offline, no network -- and asserts:

  1. `InProcessStepFunctionsClient`'s default runner selects the real body
     when `MODEL_PROVIDER=openrouter`, and keeps selecting the existing
     mock body otherwise (the "flag/env var" the ticket's Scope asks for --
     `run_mock_pipeline` itself is UNCHANGED and remains directly callable,
     see tests/test_pipeline_runner_inprocess.py).
  2. A REQUEST_CHANGE-producing draft reaches DONE with a genuinely
     computed decision from the composed spine, never the synthetic-generic registry
     entry's `mock_output_key`, AND a real tracked-changes output object in
     S3 -- the fake model issue's `source_quote` matches the planted
     counterparty text verbatim, so issue #379's quote-based patcher
     (`scripts/redline_quote_apply.py::apply_quote_patches`) locates and
     applies it, and `_write_real_output` PUTs the result.
  3. An ACCEPT-producing draft (identical to the standard form) reaches
     DONE with decision=ACCEPT and no output object written.
  4. An unrecoverable exception mid-pipeline (an empty S3 upload object,
     since #401's new activation gate below now runs -- and passes --
     BEFORE `_fetch_upload_bytes`, an unregistered playbook_id no longer
     reaches `load_playbook` at all, see #401's own gate test) lands the
     review in a terminal state via the SHARED `reviews.record_stage_failure`
     (issue #258) -- carrying the actual failing stage name -- rather than
     leaving it wedged PENDING/RUNNING.
  5. Issue #401 (empty-shell foundation): `run_real_pipeline` re-resolves
     the active release bundle from `PLAYBOOKS_TABLE` (via
     `reviews.verify_submission_time_bundle`) before ever calling
     `_load_playbook_bundle` -- `FakeDDB`/`FakePlaybooksTable` below default
     every existing "synthetic-generic" call site to an already-matching active hash, so
     tests 2-4 exercise the SAME real-review behavior as before #401 landed;
     the dedicated gate behavior (empty store / stale hash -> quarantine,
     `_load_playbook_bundle` never called) is proven in
     tests/test_runtime_playbook_loading.py, this issue's own required-
     verification file.
  6. Issue #563: `_write_real_terminal` persists `normalization_notes` from
     a `run_review` result onto the reviews row when present, and leaves the
     key ABSENT (never a null placeholder) when the result carries none --
     the same convention `decision`/`summary`/`reason` already follow.

Run standalone: `python3 tests/test_dts_pipeline_runner_real_review.py`
Exit codes: 0 = pass, 1 = fail

## Convention note

The ticket's "Required verification" names
`python3 backend/tests/test_dts_pipeline_runner_real_review.py`, but
`backend/tests/` does not exist anywhere in this repo -- every sibling
ticket in this cluster (#80/#81/#82/#83/#204/#239) and `scripts/check.sh`'s
own discovery loop use `tests/test_*.py` at the repo root only. Treated as
the same drafting-convention slip #239's PR (#295) already flagged; this
test lives at `tests/test_dts_pipeline_runner_real_review.py`, consistent
with every sibling ticket and picked up by the `scripts/check.sh` gate.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "submissions-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import pipeline_runner as pr  # noqa: E402
import diff_standard_form as dsf_module  # noqa: E402
import model_client as model_client_module  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000099"

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

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


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    import zipfile

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body_paragraphs_xml}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _build_draft_docx(overrides: dict[str, str]) -> bytes:
    """Every standard-form anchor carried over VERBATIM except the anchors
    in `overrides` -- same recipe as tests/test_review_spine.py's
    _build_draft_docx (issue #239), so every anchor NOT overridden diffs as
    "unchanged" and only the planted anchors produce real hunks."""
    standard = dsf_module.load_standard_form_paragraphs(docx_path=None, playbook_id="synthetic-generic")
    parts = []
    for std_para in standard:
        if std_para.get("absent_from_form", False):
            continue
        text = overrides.get(std_para["anchor"], std_para["text"])
        parts.append(_heading_p(std_para["heading"]))
        parts.append(_body_p(text))
    return _build_docx_bytes("".join(parts))


_SEC8_STANDARD_TEXT = (
    "$150,000 mutual aggregate liability cap; mutual exclusion of "
    "consequential, special, punitive, incidental, and indirect damages; "
    "no implied warranties beyond those expressly set forth. Neither party "
    "shall be liable to the other for consequential damages."
)
_SEC8_DRAFT_TEXT = "Each party's liability under this Agreement shall be unlimited."


def _primary_request_change_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "section_ref": "sec-8",
                    "section_title": "Limitation on Liability",
                    "counterparty_change_summary": (
                        "Counterparty removed the liability cap and "
                        "consequential-damages exclusion from Section 8."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": (
                        "Section 8 must retain the standard aggregate "
                        "liability cap and mutual damages exclusions."
                    ),
                    "proposed_replacement_text": _SEC8_STANDARD_TEXT,
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                    "source_quote": _SEC8_DRAFT_TEXT,
                }
            ],
            "critic_delta": None,
            "verdict_summary": (
                "One issue identified in Section 8 requiring attention "
                "before your organization can accept this draft."
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


def _primary_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": "No changes identified relative to your standard positions.",
        }
    )


def _critic_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _load_bundle() -> dict[str, Any]:
    with open(
        REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json",
        encoding="utf-8",
    ) as fh:
        return json.load(fh)


def _fake_client(primary_response: str, critic_response: str) -> Any:
    """A FakeBedrockClient keyed by the OPENROUTER model ids (issue #259
    patches the bundle's metadata to OpenRouter-form ids before calling
    run_review -- see pipeline_runner._bundle_with_openrouter_model_ids),
    proving the real path never falls back to the Bedrock-form ids the raw
    playbook bundle carries."""
    primary_id = model_client_module.openrouter_primary_model_id()
    critic_id = model_client_module.openrouter_critic_model_id()
    return model_client_module.FakeBedrockClient(
        {primary_id: [primary_response], critic_id: [critic_response]}
    )


# ---------------------------------------------------------------------------
# Fakes: generic DynamoDB reviews table (parses SET ... clauses / simple
# ConditionExpressions -- the exact three shapes pipeline_runner emits:
# _mark_running, _write_real_terminal, reviews.record_stage_failure) + S3.
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
    """Minimal stand-in for PLAYBOOKS_TABLE (issue #401): only `get_item` is
    needed -- run_real_pipeline's new verify_active_bundle stage
    (reviews.verify_submission_time_bundle) only ever READS this table on
    the matching-hash path this file exercises; the mismatch/quarantine
    path (which also writes to REVIEWS_TABLE) is proven separately in
    tests/test_runtime_playbook_loading.py."""

    def __init__(self, active_hashes: dict[str, str]):
        self._active_hashes = dict(active_hashes)

    def get_item(self, Key):
        playbook_id = Key["playbook_id"]
        active_hash = self._active_hashes.get(playbook_id)
        if active_hash is None:
            return {}
        return {"Item": {"playbook_id": playbook_id, "active_release_bundle_hash": active_hash}}


class FakeDDB:
    def __init__(
        self,
        reviews_table: FakeReviewsTable,
        playbooks_table: "FakePlaybooksTable | None" = None,
    ):
        self._reviews = reviews_table
        # Issue #401: defaults "synthetic-generic" to already resolving as active with
        # the SAME hash _payload() stamps on release_bundle_hash, so every
        # pre-existing call site below that doesn't care about the
        # activation gate itself keeps exercising the SAME real-review
        # behavior (REQUEST_CHANGE/ACCEPT/exception-recovery) as before
        # #401's gate was added.
        self._playbooks = playbooks_table or FakePlaybooksTable({"synthetic-generic": "hash-1"})

    def Table(self, name):
        if name == os.environ["PLAYBOOKS_TABLE"]:
            return self._playbooks
        return self._reviews


class FakeS3:
    def __init__(self, uploads: dict[str, bytes] | None = None):
        self._uploads = uploads or {}
        self.puts: list[dict[str, Any]] = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._uploads[Key])}

    def put_object(self, Bucket, Key, Body, **_kwargs):
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _payload(playbook_id: str = "synthetic-generic") -> dict[str, Any]:
    return {
        "review_id": REVIEW_ID,
        "owner_sub": "user-1",
        "playbook_id": playbook_id,
        "upload_s3_key": f"uploads/user-1/{REVIEW_ID}/in.docx",
        "release_bundle_hash": "hash-1",
    }


class TestDefaultRunnerSelectsRealVsMock(unittest.TestCase):
    def test_openrouter_flag_selects_real_pipeline(self) -> None:
        with patch.object(pr, "run_real_pipeline") as real, \
             patch.object(pr, "run_mock_pipeline") as mock, \
             patch.object(pr, "_ddb_resource", return_value="ddb"), \
             patch.object(pr, "_s3_client", return_value="s3"), \
             patch.dict(os.environ, {"MODEL_PROVIDER": "openrouter"}):
            pr.InProcessStepFunctionsClient._default_runner(REVIEW_ID, _payload())
        real.assert_called_once()
        mock.assert_not_called()

    def test_unset_flag_keeps_selecting_mock_pipeline(self) -> None:
        env = dict(os.environ)
        env.pop("MODEL_PROVIDER", None)
        with patch.object(pr, "run_real_pipeline") as real, \
             patch.object(pr, "run_mock_pipeline") as mock, \
             patch.object(pr, "_ddb_resource", return_value="ddb"), \
             patch.object(pr, "_s3_client", return_value="s3"), \
             patch.dict(os.environ, env, clear=True):
            pr.InProcessStepFunctionsClient._default_runner(REVIEW_ID, _payload())
        mock.assert_called_once()
        real.assert_not_called()


class TestRunRealPipeline(unittest.TestCase):
    def test_request_change_reaches_done_with_output_object(self) -> None:
        """Issue #379: REQUEST_CHANGE reaches DONE with a genuinely computed
        decision from the composed spine AND a real tracked-changes output
        object in S3 -- the fake model issue's source_quote matches the
        planted counterparty text verbatim, so the quote-based patcher
        (scripts/redline_quote_apply.py::apply_quote_patches) locates and
        applies it. This differs from the mock pipeline (run_mock_pipeline,
        unaffected by #379), which still PUTs its own pre-baked fixture --
        the two paths are independent by design."""
        docx_bytes = _build_draft_docx({"sec-8": _SEC8_DRAFT_TEXT})
        client = _fake_client(_primary_request_change_response(), _critic_no_delta_response())
        reviews_table = FakeReviewsTable()
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_real_pipeline(
                REVIEW_ID, _payload(),
                dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
                model_client=client,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(reviews_table.item["decision"], "REQUEST_CHANGE")
        self.assertEqual(reviews_table.item.get("output_s3_key"), f"outputs/{REVIEW_ID}/out.docx")
        # Issue #416 added a second put -- outputs/{rid}/analysis.json -- so this
        # asserts THE REDLINE was written rather than a total object count,
        # which is what it always meant.
        keys = [put["Key"] for put in s3.puts]
        self.assertIn(f"outputs/{REVIEW_ID}/out.docx", keys)
        self.assertIn(f"outputs/{REVIEW_ID}/analysis.json", keys)
        settle.assert_called_once()

    def test_accept_reaches_done_with_no_output_object(self) -> None:
        docx_bytes = _build_draft_docx({})  # identical to the standard form
        client = _fake_client(_primary_accept_response(), _critic_accept_response())
        reviews_table = FakeReviewsTable()
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                REVIEW_ID, _payload(),
                dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
                model_client=client,
            )

        self.assertEqual(reviews_table.item["status"], "DONE")
        self.assertEqual(reviews_table.item["decision"], "ACCEPT")
        self.assertNotIn("output_s3_key", reviews_table.item)
        # No output DOCUMENT, which is what this test is named for: an ACCEPT
        # has nothing to redline. Issue #416's analysis artifact is a
        # different thing and IS written here -- an accepted review is one
        # somebody may well want to interrogate later ("why was this fine?"),
        # so persisting the reasoning is exactly as useful as on the
        # REQUEST_CHANGE path.
        keys = [put["Key"] for put in s3.puts]
        self.assertNotIn(f"outputs/{REVIEW_ID}/out.docx", keys)
        self.assertEqual(keys, [f"outputs/{REVIEW_ID}/analysis.json"])

    def test_unhandled_exception_records_stage_failure_not_wedged(self) -> None:
        """A missing upload object blows up at the fetch-upload stage
        (botocore ClientError / KeyError) -- the review must land on a
        terminal status via reviews.record_stage_failure (issue #258),
        carrying the real failing stage, rather than staying RUNNING
        forever.

        Uses the default (registered, active-per-FakeDDB) "synthetic-generic"
        playbook_id and empty S3 uploads, so the failure is unambiguously
        at fetch_upload -- NOT at #401's verify_active_bundle gate, which
        this test does not exercise (see
        tests/test_runtime_playbook_loading.py for that: an unregistered
        playbook_id now fails CLOSED at verify_active_bundle, before
        load_playbook, via the exact same "no active bundle" mechanism as
        a genuinely empty playbook store -- a real behavior improvement
        this test previously could not have distinguished from a generic
        unhandled exception)."""
        reviews_table = FakeReviewsTable()
        s3 = FakeS3({})

        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_real_pipeline(
                REVIEW_ID, _payload(),
                dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
                model_client=object(),
            )

        self.assertNotIn(reviews_table.item["status"], ("PENDING", "RUNNING"))
        self.assertEqual(reviews_table.item.get("failing_stage"), "fetch_upload")
        settle.assert_called_once()

    def test_no_active_bundle_never_reaches_load_playbook(self) -> None:
        """Issue #401: an empty/non-matching PLAYBOOKS_TABLE must stop
        run_real_pipeline BEFORE it ever calls _load_playbook_bundle (the
        one function that reads playbooks/<id>.json off disk) -- the
        review is quarantined via the existing, now-wired step-10 check
        (reviews.verify_submission_time_bundle) instead."""
        reviews_table = FakeReviewsTable()
        empty_playbooks = FakePlaybooksTable({})  # nothing active anywhere
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": b"must never be read"})

        with patch.object(pr, "_settle_reservation") as settle, patch.object(
            pr, "_load_playbook_bundle"
        ) as load_bundle:
            pr.run_real_pipeline(
                REVIEW_ID, _payload(),
                dynamodb_resource=FakeDDB(reviews_table, empty_playbooks), s3_client=s3,
                model_client=object(),
            )

        load_bundle.assert_not_called()
        self.assertEqual(reviews_table.item["status"], "QUARANTINED")
        self.assertEqual(reviews_table.item["quarantine_reason"], "submission_time_bundle_retired")
        settle.assert_called_once()


class TestNormalizationNotesPersisted563(unittest.TestCase):
    """Issue #563: `_write_real_terminal` persists `normalization_notes`
    from a `run_review` result onto the reviews row the SAME "absent, never
    a null placeholder" way `decision`/`summary`/`reason` already are --
    mirroring the established assertNotIn precedent for this convention at
    tests/test_review_opf_lineage.py:457 and :483."""

    def test_normalization_notes_present_reaches_the_review_row(self) -> None:
        notes = (
            "Paragraph 'Limitation on Liability': pending tracked change "
            "(author: counterparty, status: unresolved) accepted-all into "
            "the operative draft."
        )
        table = FakeReviewsTable(status="RUNNING")
        pr._write_real_terminal(
            REVIEW_ID,
            {"status": "OK", "decision": "REQUEST_CHANGE", "normalization_notes": notes},
            output_s3_key=None,
            dynamodb_resource=FakeDDB(table),
        )
        self.assertEqual(table.item.get("normalization_notes"), notes)

    def test_normalization_notes_absent_leaves_the_key_absent(self) -> None:
        table = FakeReviewsTable(status="RUNNING")
        pr._write_real_terminal(
            REVIEW_ID,
            {"status": "OK", "decision": "ACCEPT"},
            output_s3_key=None,
            dynamodb_resource=FakeDDB(table),
        )
        self.assertNotIn("normalization_notes", table.item)


class TestRequotePersisted569(unittest.TestCase):
    """Issue #569 fix round 1, finding 4: `_write_real_terminal` persists
    `requote` from a `run_review` result onto the reviews row the SAME
    "absent, never a null placeholder" way `normalization_notes` already is
    (`TestNormalizationNotesPersisted563` above) -- previously covered by
    NO staged test: `tests/test_review_api_84.py`'s existing `requote`
    reference only covers the pre-existing READ side
    (`reviews.get_review_detail`), never this write side."""

    def test_requote_present_reaches_the_review_row(self) -> None:
        requote = {"attempted": 1, "recovered": 1, "still_failed": 0}
        table = FakeReviewsTable(status="RUNNING")
        pr._write_real_terminal(
            REVIEW_ID,
            {"status": "OK", "decision": "REQUEST_CHANGE", "requote": requote},
            output_s3_key=None,
            dynamodb_resource=FakeDDB(table),
        )
        self.assertEqual(table.item.get("requote"), requote)

    def test_requote_absent_leaves_the_key_absent(self) -> None:
        table = FakeReviewsTable(status="RUNNING")
        pr._write_real_terminal(
            REVIEW_ID,
            {"status": "OK", "decision": "ACCEPT"},
            output_s3_key=None,
            dynamodb_resource=FakeDDB(table),
        )
        self.assertNotIn("requote", table.item)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestDefaultRunnerSelectsRealVsMock))
    suite.addTests(loader.loadTestsFromTestCase(TestRunRealPipeline))
    suite.addTests(loader.loadTestsFromTestCase(TestNormalizationNotesPersisted563))
    suite.addTests(loader.loadTestsFromTestCase(TestRequotePersisted569))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
