#!/usr/bin/env python3
"""
Executable tests for issue #446: "every successful review is marked ERROR by a
failed reservation settle (missing GSI, unguarded clobber)".

## The production failure this reproduces

On the live Docker Compose deployment, a review that COMPLETED SUCCESSFULLY was
reported to the user as "The review finished, but the result could not be saved.
Failed at stage `persist_result`". The order of operations inside
`run_real_pipeline`'s persist_result block is:

  1. `_write_real_output`     -> the redline .docx is PUT to object storage. OK.
  2. `_write_real_terminal`   -> `status=DONE` + `output_s3_key` are written. OK.
  3. `_settle_reservation`    -> RAISES (`ValidationException: The table does
                                 not have the specified index: review_id-index`).
  4. the bare `except`        -> `reviews.record_stage_failure(...)` overwrites
                                 the row's `DONE` with `ERROR`.

So the review was complete and saved, and step 4 destroyed it. Two independent
defects, both proven here against REAL AWS semantics (moto), never a hand-rolled
fake table -- the fakes are exactly what let this ship green:

  (a) `deploy/dts/bootstrap.py::create_tables` was CREATE-ONLY. A table that
      predates the `review_id-index` declaration is reported "already exists"
      and skipped forever, so an existing deployment can never converge no
      matter how many times it is redeployed.
  (b) `reviews.record_stage_failure` issued an UNCONDITIONAL `update_item` on
      `status`, so ANY late-stage failure after a successful terminal write
      silently converts a good review into a failed one.

## Why moto and not an in-memory fake

Neither defect is observable through a stand-in table: a fake ignores the
`ConditionExpression` it is handed, and a fake that doesn't implement GSIs
cannot raise the ValidationException that started this. Every assertion below
runs against moto's real DynamoDB/S3 behavior, and the pipeline test drives the
REAL `run_real_pipeline` end to end (with only the MODEL faked, per this repo's
standing "no network in any test" rule) against a submissions table genuinely
missing the index -- i.e. the exact shape of the live deployment.

## What each class pins

  1. `TestRecordStageFailureCannotDowngradeADoneReview` -- a `DONE` row survives
     a `record_stage_failure` call; a non-terminal row is still recorded as
     failed exactly as before (the guard must not disarm the normal path).
  2. `TestSettleFailureLeavesTheReviewDone` -- with `review_id-index` absent, a
     review still ends `DONE`, keeps its `output_s3_key`, and its document is
     still in the outputs bucket. A settle failure is an ACCOUNTING problem, not
     a failure of the review.
  3. `TestBootstrapConvergesAMissingGsi` -- the real bootstrap entry point adds
     the missing index to a PRE-EXISTING table, is a no-op once present, and the
     keyed lookup that failed in production works afterwards.

Pre-fix: 1 fails (the row is clobbered to ERROR), 2 fails (status ERROR,
failing_stage persist_result), 3 fails (the index is never added).

Run standalone: `python3 tests/test_review_result_not_clobbered_446.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

REVIEWS_TABLE = "contract-toaster-reviews-446"
REVIEW_SUBMISSIONS_TABLE = "contract-toaster-review-submissions-446"
DAILY_SPEND_TABLE = "contract-toaster-daily-spend-446"
PLAYBOOKS_TABLE = "contract-toaster-playbooks-446"
UPLOADS_BUCKET = "contract-toaster-uploads-446"
OUTPUTS_BUCKET = "contract-toaster-outputs-446"

os.environ.setdefault("REVIEWS_TABLE", REVIEWS_TABLE)
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", REVIEW_SUBMISSIONS_TABLE)
os.environ.setdefault("DAILY_SPEND_TABLE", DAILY_SPEND_TABLE)
os.environ.setdefault("PLAYBOOKS_TABLE", PLAYBOOKS_TABLE)
os.environ.setdefault("UPLOADS_BUCKET", UPLOADS_BUCKET)
os.environ.setdefault("OUTPUTS_BUCKET", OUTPUTS_BUCKET)
os.environ.setdefault("AWS_REGION", "us-east-1")

import boto3  # noqa: E402
import time  # noqa: E402
from moto import mock_aws  # noqa: E402

import diff_standard_form as dsf_module  # noqa: E402
import model_client as model_client_module  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import reviews  # noqa: E402

# The REAL deploy bootstrap, imported by file location because `deploy/dts/`
# is not an importable package (same convention as
# tests/test_shipped_playbook_seed.py) -- this drives the actual entry point
# a `docker compose up` runs, not a re-implementation of it.
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "dts_bootstrap_446", REPO_ROOT / "deploy" / "dts" / "bootstrap.py"
)
dts_bootstrap = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
_BOOTSTRAP_SPEC.loader.exec_module(dts_bootstrap)

REVIEW_ID = "00000000-0000-4000-a000-000000000446"
IDEMPOTENCY_KEY = "idem-446"
RESERVATION_ID = "res-446"
PLAYBOOK_ID = "synthetic-generic"
BUNDLE_HASH = "hash-446"
UPLOAD_KEY = f"uploads/user-1/{REVIEW_ID}/in.docx"
OUTPUT_KEY = f"outputs/{REVIEW_ID}/out.docx"

GSI_NAME = "review_id-index"


# ---------------------------------------------------------------------------
# moto table/bucket provisioning helpers.
# ---------------------------------------------------------------------------

def _create_reviews_table(ddb: Any) -> Any:
    ddb.create_table(
        TableName=REVIEWS_TABLE,
        KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(REVIEWS_TABLE)


def _create_submissions_table_without_gsi(ddb: Any) -> Any:
    """The live deployment's shape: the table exists, the index does not.

    Deliberately NOT the CDK schema (infra/lib/nested/data-stack.ts), which has
    always carried `review_id-index` -- this reproduces a table created before
    the index was declared, which is precisely what the create-only bootstrap
    could never repair."""
    ddb.create_table(
        TableName=REVIEW_SUBMISSIONS_TABLE,
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(REVIEW_SUBMISSIONS_TABLE)


def _create_daily_spend_table(ddb: Any) -> Any:
    ddb.create_table(
        TableName=DAILY_SPEND_TABLE,
        KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(DAILY_SPEND_TABLE)


def _create_playbooks_table(ddb: Any) -> Any:
    ddb.create_table(
        TableName=PLAYBOOKS_TABLE,
        KeySchema=[{"AttributeName": "playbook_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "playbook_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(PLAYBOOKS_TABLE)


def _gsi_names(client: Any, table_name: str) -> set[str]:
    desc = client.describe_table(TableName=table_name)["Table"]
    return {idx["IndexName"] for idx in desc.get("GlobalSecondaryIndexes") or []}


# ---------------------------------------------------------------------------
# .docx + fake-model fixtures (same recipe as
# tests/test_dts_pipeline_runner_real_review.py -- each test file owns its own
# copy, this repo's convention).
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

_SEC8_STANDARD_TEXT = (
    "$150,000 mutual aggregate liability cap; mutual exclusion of "
    "consequential, special, punitive, incidental, and indirect damages; "
    "no implied warranties beyond those expressly set forth. Neither party "
    "shall be liable to the other for consequential damages."
)
_SEC8_DRAFT_TEXT = "Each party's liability under this Agreement shall be unlimited."


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
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
    standard = dsf_module.load_standard_form_paragraphs(docx_path=None, playbook_id=PLAYBOOK_ID)
    parts = []
    for std_para in standard:
        if std_para.get("absent_from_form", False):
            continue
        text = overrides.get(std_para["anchor"], std_para["text"])
        parts.append(_heading_p(std_para["heading"]))
        parts.append(_body_p(text))
    return _build_docx_bytes("".join(parts))


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


def _fake_model_client() -> Any:
    primary_id = model_client_module.openrouter_primary_model_id()
    critic_id = model_client_module.openrouter_critic_model_id()
    return model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response()],
            critic_id: [_critic_no_delta_response()],
        }
    )


def _payload() -> dict[str, Any]:
    return {
        "review_id": REVIEW_ID,
        "owner_sub": "user-1",
        "playbook_id": PLAYBOOK_ID,
        "upload_s3_key": UPLOAD_KEY,
        "release_bundle_hash": BUNDLE_HASH,
    }


class MotoBackedTestCase(unittest.TestCase):
    """Shared moto lifecycle. Real DynamoDB/S3 semantics, no network."""

    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.ddb_client = boto3.client("dynamodb", region_name="us-east-1")


# ---------------------------------------------------------------------------
# Defect (b): record_stage_failure clobbered a terminal row.
# ---------------------------------------------------------------------------

class TestRecordStageFailureCannotDowngradeADoneReview(MotoBackedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.reviews_table = _create_reviews_table(self.ddb)

    def test_a_done_review_survives_a_late_stage_failure(self) -> None:
        """The #446 invariant: a review that finished must stay finished.

        This is the trap that armed the whole outage -- it is not specific to
        the settle: ANY exception raised after `_write_real_terminal` succeeded
        reached this unconditional write and turned a good review into a failed
        one, taking the user's downloadable document with it."""
        self.reviews_table.put_item(
            Item={
                "review_id": REVIEW_ID,
                "owner_sub": "user-1",
                "playbook_id": PLAYBOOK_ID,
                "status": "DONE",
                "decision": "REQUEST_CHANGE",
                "output_s3_key": OUTPUT_KEY,
            }
        )

        written = reviews.record_stage_failure(
            REVIEW_ID, "persist_result", "unhandled_exception", self.ddb
        )

        item = self.reviews_table.get_item(Key={"review_id": REVIEW_ID})["Item"]
        self.assertEqual(item["status"], "DONE")
        self.assertEqual(item["decision"], "REQUEST_CHANGE")
        self.assertEqual(item["output_s3_key"], OUTPUT_KEY)
        self.assertNotIn("failing_stage", item)
        # The return value is the status the row actually holds -- callers that
        # log/branch on it must not be told ERROR was written when it wasn't.
        self.assertEqual(written, "DONE")

    def test_a_running_review_is_still_recorded_as_failed(self) -> None:
        """The guard must not disarm the normal path: an in-flight review that
        genuinely fails still records status/failing_stage/reason (#258/#442)."""
        self.reviews_table.put_item(
            Item={
                "review_id": REVIEW_ID,
                "owner_sub": "user-1",
                "playbook_id": PLAYBOOK_ID,
                "status": "RUNNING",
            }
        )

        written = reviews.record_stage_failure(
            REVIEW_ID, "run_review", "model_account_out_of_credits", self.ddb
        )

        item = self.reviews_table.get_item(Key={"review_id": REVIEW_ID})["Item"]
        self.assertEqual(written, "ERROR")
        self.assertEqual(item["status"], "ERROR")
        self.assertEqual(item["failing_stage"], "run_review")
        self.assertEqual(item["reason"], "model_account_out_of_credits")

    def test_a_failed_review_can_still_be_re_recorded(self) -> None:
        """Only a SUCCESS terminal is protected. An ERROR row is still
        writable, so nothing about retry/reclassification behavior changes."""
        self.reviews_table.put_item(
            Item={"review_id": REVIEW_ID, "status": "ERROR", "failing_stage": "run_review"}
        )

        written = reviews.record_stage_failure(
            REVIEW_ID, "persist_result", "unhandled_exception", self.ddb
        )

        item = self.reviews_table.get_item(Key={"review_id": REVIEW_ID})["Item"]
        self.assertEqual(written, "ERROR")
        self.assertEqual(item["failing_stage"], "persist_result")


# ---------------------------------------------------------------------------
# The end-to-end production shape: a settle that raises must not destroy a
# persisted result.
# ---------------------------------------------------------------------------

class TestSettleFailureLeavesTheReviewDone(MotoBackedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.reviews_table = _create_reviews_table(self.ddb)
        self.submissions_table = _create_submissions_table_without_gsi(self.ddb)
        self.spend_table = _create_daily_spend_table(self.ddb)
        self.playbooks_table = _create_playbooks_table(self.ddb)

        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=UPLOADS_BUCKET)
        self.s3.create_bucket(Bucket=OUTPUTS_BUCKET)

        self.reviews_table.put_item(
            Item={
                "review_id": REVIEW_ID,
                "owner_sub": "user-1",
                "playbook_id": PLAYBOOK_ID,
                "status": "PENDING",
            }
        )
        self.playbooks_table.put_item(
            Item={"playbook_id": PLAYBOOK_ID, "active_release_bundle_hash": BUNDLE_HASH}
        )
        self.submissions_table.put_item(
            Item={
                "idempotency_key": IDEMPOTENCY_KEY,
                "review_id": REVIEW_ID,
                "spend_reservation_id": RESERVATION_ID,
            }
        )
        self.spend_table.put_item(
            Item={
                "spend_date": time.strftime("%Y-%m-%d", time.gmtime()),
                "reserved_usd_cents": reviews.compute_worst_case_reservation_usd_cents(),
                "daily_cap_usd_cents": 2000,
            }
        )
        self.s3.put_object(
            Bucket=UPLOADS_BUCKET,
            Key=UPLOAD_KEY,
            Body=_build_draft_docx({"sec-8": _SEC8_DRAFT_TEXT}),
        )

    def test_missing_gsi_does_not_destroy_a_persisted_result(self) -> None:
        """The live incident, reproduced end to end.

        `review_id-index` is absent, so `_settle_reservation`'s keyed lookup
        raises for real -- AFTER the redline has been PUT and `status=DONE`
        written. (moto rejects the missing index as `ResourceNotFoundException:
        Invalid index`, DynamoDB-Local as `ValidationException: The table does
        not have the specified index`; the CONDITION is identical and neither
        is caught anywhere between here and the fail-closed handler.) The
        review must still be DONE, still carry
        its `output_s3_key`, and the document must still be downloadable: an
        unsettled spend reservation is an accounting problem, not a failure of
        the review."""
        pr.run_real_pipeline(
            REVIEW_ID,
            _payload(),
            dynamodb_resource=self.ddb,
            s3_client=self.s3,
            model_client=_fake_model_client(),
        )

        item = self.reviews_table.get_item(Key={"review_id": REVIEW_ID})["Item"]
        self.assertEqual(item["status"], "DONE")
        self.assertEqual(item["decision"], "REQUEST_CHANGE")
        self.assertEqual(item["output_s3_key"], OUTPUT_KEY)
        self.assertNotIn("failing_stage", item)

        # The user's document is genuinely there, not merely referenced.
        body = self.s3.get_object(Bucket=OUTPUTS_BUCKET, Key=OUTPUT_KEY)["Body"].read()
        self.assertTrue(body.startswith(b"PK"), "output object is not a .docx package")

    def test_the_settle_failure_is_not_silently_swallowed(self) -> None:
        """Not failing the review is not the same as pretending nothing
        happened: the unsettled reservation must be logged loudly enough for an
        operator to find it (this is real money left reserved)."""
        with self.assertLogs(pr.logger, level="ERROR") as captured:
            pr.run_real_pipeline(
                REVIEW_ID,
                _payload(),
                dynamodb_resource=self.ddb,
                s3_client=self.s3,
                model_client=_fake_model_client(),
            )
        self.assertTrue(
            any(REVIEW_ID in message for message in captured.output),
            f"no operator-visible log names the review: {captured.output}",
        )


# ---------------------------------------------------------------------------
# Defect (a): the bootstrap could never repair an existing deployment.
# ---------------------------------------------------------------------------

class TestBootstrapConvergesAMissingGsi(MotoBackedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.submissions_table = _create_submissions_table_without_gsi(self.ddb)
        self.submissions_table.put_item(
            Item={
                "idempotency_key": IDEMPOTENCY_KEY,
                "review_id": REVIEW_ID,
                "spend_reservation_id": RESERVATION_ID,
            }
        )
        self.assertNotIn(
            GSI_NAME,
            _gsi_names(self.ddb_client, REVIEW_SUBMISSIONS_TABLE),
            "precondition: the table must start WITHOUT the index",
        )

    def test_bootstrap_adds_the_missing_index_to_a_pre_existing_table(self) -> None:
        """`create_table` raising ResourceInUseException must not end the story.

        The bootstrap runs on every `docker compose up`, so it is the ONLY
        mechanism that can repair a deployment whose table predates the index
        declaration."""
        dts_bootstrap.create_tables()

        self.assertIn(GSI_NAME, _gsi_names(self.ddb_client, REVIEW_SUBMISSIONS_TABLE))

    def test_the_repaired_index_actually_serves_the_keyed_lookup(self) -> None:
        """Convergence is only real if the query that failed in production
        succeeds afterwards -- assert the behavior, not the declaration."""
        dts_bootstrap.create_tables()

        found = pr._find_submission_by_review_id(self.submissions_table, REVIEW_ID)
        self.assertIsNotNone(found)
        self.assertEqual(found["idempotency_key"], IDEMPOTENCY_KEY)

    def test_re_running_the_bootstrap_is_a_no_op(self) -> None:
        """Idempotent in the strong sense: converged stays converged, and a
        second pass neither raises nor duplicates the index."""
        dts_bootstrap.create_tables()
        dts_bootstrap.create_tables()

        desc = self.ddb_client.describe_table(TableName=REVIEW_SUBMISSIONS_TABLE)["Table"]
        index_names = [idx["IndexName"] for idx in desc.get("GlobalSecondaryIndexes") or []]
        self.assertEqual(index_names, [GSI_NAME])


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestRecordStageFailureCannotDowngradeADoneReview,
        TestSettleFailureLeavesTheReviewDone,
        TestBootstrapConvergesAMissingGsi,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
