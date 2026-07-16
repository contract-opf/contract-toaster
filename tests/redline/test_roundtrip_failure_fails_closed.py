#!/usr/bin/env python3
"""
RED test — a round-trip verification failure must fail closed, never raise
an uncaught exception (issue #263).

## Root problem this proves fixed

Every other gate in `scripts/redline_generate.py::generate_redline` fails
closed to a status dict (leakage scan -> `ERROR_MANUAL_REVIEW_REQUIRED`;
output OOXML scan -> `ERROR_MANUAL_REVIEW_REQUIRED`; anchor/hash mismatch at
patch time -> `MANUAL_REVIEW_REQUIRED`). The round-trip verification step
was the ONE exception: `verify_docx_round_trip()` raises a bare `ValueError`
with no `try` around it in `generate_redline`, so a round-trip failure
propagates as an uncaught exception out of `generate_redline` itself.

In the DTS in-process runner (`backend/src/pipeline_runner.py::
run_real_pipeline`), that exception is only ever caught by the OUTER,
generic `except Exception` around the entire `review_spine.run_review`
call -- it lands the review in a terminal state, but as a blunt,
whole-`run_review`-stage `unhandled_exception`, not the specific,
attributable round-trip-failure condition this module's own docstring
otherwise guarantees every other gate produces. The future Lambda stage
(not yet built) would have no such outer safety net at all and would wedge
non-terminal on this path.

Two layers of proof:

  `TestGenerateRedlineFailsClosed` forces `verify_docx_round_trip` to fail (a
  writer bug producing bytes that do not re-open cleanly) and asserts
  `generate_redline` itself:

    1. Never raises -- returns a status dict instead, exactly like every
       other gate in this module.
    2. The returned status is TERMINAL and fail-closed: `status` =
       `ERROR_MANUAL_REVIEW_REQUIRED` (mirrors `output_ooxml_scan_failed`'s
       shape immediately above it in the pipeline -- a writer-produced
       artifact that cannot be safely delivered), `reason` =
       `round_trip_verification_failed`, `docx_bytes` is `None` (the
       corrupt bytes are never delivered to a caller).

  `TestRoundTripFailureViaInProcessRunner` drives the SAME condition through
  `backend/src/pipeline_runner.py::run_real_pipeline` end to end -- the
  actual DTS in-process runner path (issue #259) that calls
  `scripts/review_spine.py::run_review`, which in turn calls
  `generate_redline` -- so AC 2 ("Behavior verified through the in-process
  runner path") is proven directly against the runner, not just asserted by
  docstring, and asserts:

    1. `run_real_pipeline` never raises.
    2. The review's terminal DynamoDB row lands on
       `ERROR_MANUAL_REVIEW_REQUIRED` / `reason=round_trip_verification_failed`
       -- never left PENDING/RUNNING (never wedged non-terminal).
    3. No output object is written to the outputs bucket (the corrupt bytes
       are never delivered).
    4. The reservation is still settled, same as every other terminal path.

This test FAILS today (RED) because `generate_redline` lets
`verify_docx_round_trip`'s `ValueError` propagate uncaught instead of
catching it and returning a fail-closed status dict -- in the in-process
runner, that propagates all the way out to the outer generic except block,
so the runner-path test fails closed too (differently attributed: reason
`unhandled_exception` / stage `run_review`, not the specific round-trip
reason).

Run standalone: `python tests/redline/test_roundtrip_failure_fails_closed.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
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

import redline_generate as rg  # noqa: E402
import diff_standard_form as dsf_module  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import model_client as model_client_module  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000263"


# ---------------------------------------------------------------------------
# Part 1: direct generate_redline unit coverage.
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base_hunks_and_paragraphs():
    sec8_text = "Each party's liability shall not exceed $150,000."
    hunks = [
        {
            "anchor": "sec-8",
            "kind": "modified_new",
            "text": sec8_text,
            "source_text_hash": _sha256_text(sec8_text),
        }
    ]
    return hunks, {"sec-8": sec8_text}


def _reconciled_request_change() -> dict:
    return {
        "schema_version": "output-schema-v1",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [
            {
                "section_ref": "sec-8",
                "section_title": "Limitation on Liability",
                "counterparty_change_summary": "Deletes the liability cap.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "Restores the standard liability cap.",
                "proposed_replacement_text": "Each party's liability is uncapped.",
                "playbook_topic_id": "limitation-of-liability",
                "internal_precedent_citation": None,
                "provenance": "model",
            }
        ],
        "critic_delta": None,
        "verdict_summary": None,
    }


class TestGenerateRedlineFailsClosed(unittest.TestCase):
    def test_round_trip_failure_never_raises_and_fails_closed(self) -> None:
        hunks, current_paragraphs_by_anchor = _base_hunks_and_paragraphs()
        reconciled = _reconciled_request_change()
        corpus = rg.leakage_scan.ConfidentialCorpus()

        simulated_error = "word/document.xml did not parse: simulated writer bug"
        with patch.object(
            rg, "verify_docx_round_trip", side_effect=ValueError(simulated_error)
        ):
            try:
                result = rg.generate_redline(
                    reconciled_result=reconciled,
                    hunks=hunks,
                    current_paragraphs_by_anchor=current_paragraphs_by_anchor,
                    corpus=corpus,
                    normalized_docx_bytes=_build_docx_bytes(_body_p(hunks[0]["text"])),
                )
            except ValueError as exc:  # pragma: no cover - the RED-run path
                self.fail(
                    "generate_redline raised an uncaught ValueError instead "
                    "of returning a fail-closed status dict -- every other "
                    "gate in this module fails closed to a status, this one "
                    f"must too (issue #263). Raised: {exc!r}"
                )

        self.assertEqual(result.get("status"), rg.ERROR_MANUAL_REVIEW_REQUIRED)
        self.assertEqual(result.get("reason"), "round_trip_verification_failed")
        self.assertIsNone(result.get("docx_bytes"))
        # A round-trip verification failure is a SYSTEM status, never a
        # legal decision.
        self.assertNotIn("decision", result)
        # The review must never be left non-terminal: some status is always
        # returned (never None, never a bare exception that skips a return).
        self.assertTrue(result.get("status"))


# ---------------------------------------------------------------------------
# Part 2: the SAME condition driven end-to-end through the DTS in-process
# runner (backend/src/pipeline_runner.py::run_real_pipeline), issue #259's
# real-pipeline path -- proving AC 2 ("Behavior verified through the
# in-process runner path") against the actual runner, not just against
# generate_redline in isolation. Fixture/harness recipe mirrors
# tests/test_dts_pipeline_runner_real_review.py (issue #259) exactly, so a
# passing result here is a passing result on the real runner path with no
# separate integration layer to drift.
# ---------------------------------------------------------------------------

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
    standard = dsf_module.load_standard_form_paragraphs(docx_path=None, playbook_id="eiaa")
    parts = []
    for std_para in standard:
        if std_para.get("absent_from_form", False):
            continue
        text = overrides.get(std_para["anchor"], std_para["text"])
        parts.append(_heading_p(std_para["heading"]))
        parts.append(_body_p(text))
    return _build_docx_bytes("".join(parts))


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
                    "proposed_replacement_text": (
                        "$150,000 mutual aggregate liability cap."
                    ),
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


def _fake_client(primary_response: str, critic_response: str) -> Any:
    primary_id = model_client_module.openrouter_primary_model_id()
    critic_id = model_client_module.openrouter_critic_model_id()
    return model_client_module.FakeBedrockClient(
        {primary_id: [primary_response], critic_id: [critic_response]}
    )


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


class TestRoundTripFailureViaInProcessRunner(unittest.TestCase):
    def test_round_trip_failure_fails_closed_through_run_real_pipeline(self) -> None:
        docx_bytes = _build_draft_docx({"sec-8": _SEC8_DRAFT_TEXT})
        client = _fake_client(_primary_request_change_response(), _critic_no_delta_response())
        reviews_table = FakeReviewsTable()
        s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        simulated_error = "word/document.xml did not parse: simulated writer bug"
        with patch.object(pr, "_settle_reservation") as settle, patch.object(
            rg, "verify_docx_round_trip", side_effect=ValueError(simulated_error)
        ):
            try:
                pr.run_real_pipeline(
                    REVIEW_ID, _payload(),
                    dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
                    model_client=client,
                )
            except ValueError as exc:  # pragma: no cover - the RED-run path
                self.fail(
                    "run_real_pipeline let an uncaught ValueError escape "
                    "the in-process runner path instead of persisting a "
                    f"fail-closed terminal status (issue #263). Raised: {exc!r}"
                )

        # Never left wedged in PENDING/RUNNING -- always terminal.
        self.assertNotIn(reviews_table.item["status"], ("PENDING", "RUNNING"))
        self.assertEqual(reviews_table.item["status"], rg.ERROR_MANUAL_REVIEW_REQUIRED)
        self.assertEqual(reviews_table.item.get("reason"), "round_trip_verification_failed")
        # The corrupt bytes are never delivered.
        self.assertEqual(s3.puts, [])
        self.assertNotIn("output_s3_key", reviews_table.item)
        # The reservation is still settled, same as every other terminal path.
        settle.assert_called_once()


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestGenerateRedlineFailsClosed))
    suite.addTests(loader.loadTestsFromTestCase(TestRoundTripFailureViaInProcessRunner))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
