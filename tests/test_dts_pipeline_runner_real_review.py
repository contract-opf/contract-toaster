#!/usr/bin/env python3
"""
Unit tests for backend/src/pipeline_runner.py's Phase 2 REAL pipeline body
(issue #259: "wire the review spine into the DTS in-process runner via
OpenRouterModelClient").

## Root problem this proves fixed

Before this slice, the DTS in-process runner's ONLY body was
`run_mock_pipeline` -- a canned, pre-baked eiaa fixture copy
(the eiaa registry entry's `mock_output_key`), never a genuinely computed review. This
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
     computed decision and a redline `.docx` PUT to the outputs bucket at
     `outputs/{review_id}/out.docx` -- real bytes from the composed spine,
     never the eiaa registry entry's `mock_output_key`.
  3. An ACCEPT-producing draft (identical to the standard form) reaches
     DONE with decision=ACCEPT and no output object written.
  4. An unrecoverable exception mid-pipeline (unregistered playbook_id)
     lands the review in a terminal state via the SHARED
     `reviews.record_stage_failure` (issue #258) -- carrying the actual
     failing stage name -- rather than leaving it wedged PENDING/RUNNING.

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

import pipeline_runner as pr  # noqa: E402
import diff_standard_form as dsf_module  # noqa: E402
import model_client as model_client_module  # noqa: E402
import playbook_registry  # noqa: E402

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
    standard = dsf_module.load_standard_form_paragraphs(docx_path=None)
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
    with open(REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json", encoding="utf-8") as fh:
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


class FakeDDB:
    def __init__(self, reviews_table: FakeReviewsTable):
        self._reviews = reviews_table

    def Table(self, name):
        return self._reviews


class FakeS3:
    def __init__(self, uploads: dict[str, bytes] | None = None):
        self._uploads = uploads or {}
        self.puts: list[dict[str, Any]] = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._uploads[Key])}

    def put_object(self, Bucket, Key, Body):
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _payload(playbook_id: str = "eiaa") -> dict[str, Any]:
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
    def test_request_change_reaches_done_with_real_redline(self) -> None:
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
        self.assertEqual(
            reviews_table.item["output_s3_key"], f"outputs/{REVIEW_ID}/out.docx"
        )
        self.assertEqual(len(s3.puts), 1)
        self.assertEqual(s3.puts[0]["Key"], f"outputs/{REVIEW_ID}/out.docx")
        redline_bytes = s3.puts[0]["Body"]
        self.assertIsInstance(redline_bytes, (bytes, bytearray))
        self.assertGreater(len(redline_bytes), 0)
        # The real spine's own computed bytes, never the mock's canned
        # pre-baked fixture pointer (issue #289: that pointer now lives on
        # the registry entry's mock_output_key, not a module constant).
        mock_pre_baked_key = playbook_registry.resolve_playbook("eiaa").mock_output_key
        self.assertNotEqual(reviews_table.item["output_s3_key"], mock_pre_baked_key)
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
        self.assertEqual(s3.puts, [])

    def test_unhandled_exception_records_stage_failure_not_wedged(self) -> None:
        """An unregistered playbook_id blows up at the load-playbook stage
        (PlaybookNotRegisteredError) -- the review must land on a terminal
        status via reviews.record_stage_failure (issue #258), carrying the
        real failing stage, rather than staying RUNNING forever."""
        reviews_table = FakeReviewsTable()
        s3 = FakeS3({})

        with patch.object(pr, "_settle_reservation") as settle:
            pr.run_real_pipeline(
                REVIEW_ID, _payload(playbook_id="not-a-real-playbook"),
                dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
                model_client=object(),
            )

        self.assertNotIn(reviews_table.item["status"], ("PENDING", "RUNNING"))
        self.assertEqual(reviews_table.item.get("failing_stage"), "load_playbook")
        settle.assert_called_once()


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestDefaultRunnerSelectsRealVsMock))
    suite.addTests(loader.loadTestsFromTestCase(TestRunRealPipeline))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
