#!/usr/bin/env python3
"""
Executable tests for the `summary` / `verdict_summary` attribute-name bug.

## The bug

`scripts/review_spine.py` deliberately renames the model's `verdict_summary`
output key to `summary` when it assembles the review result, and ALL THREE
writers persist it under that name:

  * `backend/src/pipeline_runner.py::_write_terminal`      ("summary = :sum")
  * `backend/src/pipeline_runner.py::_write_real_terminal` ("summary = :sum")
  * `infra/lambda/persist/handler.py`                      ("summary = :summary")

Every READER, however, read the pre-rename key `verdict_summary`, which no
writer has ever written -- confirmed against the repository's full history:

  * `backend/src/reviews.py::get_review_detail`   -> item.get("verdict_summary")
  * `backend/src/review_routes.py`'s cover-note route (issue #499)

so `GET /api/reviews/{id}` returned `verdict_summary: null` for EVERY real
review, and the cover-note drafter never saw the narrative summary (its
"Overall: ..." prompt line was silently absent from every real draft).

Both purge implementations had the same phantom name in their clear-list
(`backend/src/retention.py`'s REMOVE clause and
`infra/lambda/purge_worker/handler.py`'s SUBSTANCE_FIELDS), so the single most
substance-bearing prose field on the row was never actually cleared by a purge
sweep on either deployment target -- a data-retention gap, not just a display
bug.

## Why no existing test caught it

Every pre-existing fixture (tests/test_review_api_84.py,
tests/test_cover_note_499.py, tests/test_retention_purge_prefix_454.py, ...)
hand-constructs a `reviews` row via `put_item` with the READER's key,
`verdict_summary`, and so agreed with the bug: writer and reader were never
exercised against each other. That is the fixture-fidelity failure mode this
repo has been burned by before -- a fake that accepts what the real dependency
rejects is not a test.

So EVERY test below drives a REAL writer (`_write_terminal` /
`_write_real_terminal`) into a real (moto) DynamoDB table and then reads back
through a REAL reader (`reviews.get_review_detail`) or a REAL purge sweep.
Nothing here hand-seeds the field under test.

This test MUST FAIL on the pre-fix tree:
  * the round-trip tests fail because `verdict_summary` comes back None;
  * the purge tests fail because the summary text survives the sweep.

Run standalone: `python3 tests/test_summary_attribute_roundtrip.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
PURGE_WORKER_DIR = REPO_ROOT / "infra" / "lambda" / "purge_worker"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(PURGE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(PURGE_WORKER_DIR))

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-summary-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-summary-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-summary-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-summary-test")
os.environ.setdefault(
    "RETENTION_SETTINGS_TABLE", "contract-toaster-retention-settings-summary-test"
)

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.retention as retention_module  # noqa: E402
import src.reviews as reviews_module  # noqa: E402

# The production purge-worker Lambda -- a separate deployable, imported
# independently of backend/src (same convention as
# tests/test_retention_purge_prefix_454.py).
import handler as purge_handler_module  # noqa: E402

DAY = 86400

# The narrative prose under test. Distinctive enough that a substring assertion
# cannot pass by accident against some other field's contents.
SUMMARY_TEXT = (
    "We restored the liability cap and made the indemnity mutual; the "
    "remaining terms were acceptable as drafted."
)

# toaster_guidance (issue #398): the submitter's own free-text prose for this
# specific review, Confidential per docs/data-handling.md's field table.
# Distinctive enough that its presence/absence on a row is unambiguous.
GUIDANCE_TEXT = (
    "Be lenient on the payment terms for this one -- long-standing partner, "
    "we want to close quickly."
)

OWNER = {"cognito_sub": "owner-summary", "email": "owner@example.com"}


class SummaryAttributeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()

        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self.s3 = boto3.client("s3", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["RETENTION_SETTINGS_TABLE"],
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
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
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.s3.create_bucket(Bucket=os.environ["OUTPUTS_BUCKET"])

        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

    def tearDown(self) -> None:
        self._mock_aws.stop()

    # -- seeding -------------------------------------------------------

    def _seed_bare_row(
        self,
        review_id: str,
        *,
        age_days: float = 0,
        window_days: Any = 90,
        legal_hold: bool = False,
    ) -> None:
        """The row as it exists BEFORE the terminal write -- deliberately
        carrying no summary-ish attribute of any name, so the only way one
        can appear is a real writer putting it there."""
        now = retention_module.now_epoch()
        self.reviews_table.put_item(
            Item={
                "review_id": review_id,
                "owner_sub": OWNER["cognito_sub"],
                "status": "RUNNING",
                "created_at": str(int(now - age_days * DAY)),
                "updated_at": str(int(now - age_days * DAY)),
                "retention_window_at_creation": window_days,
                "legal_hold": legal_hold,
            }
        )

    def _row(self, review_id: str) -> dict[str, Any]:
        return self.reviews_table.get_item(Key={"review_id": review_id})["Item"]


# ---------------------------------------------------------------------------
# 1. The real writers put the prose somewhere the real reader can find it.
# ---------------------------------------------------------------------------


class TestWriterReaderRoundTrip(SummaryAttributeTestBase):
    def test_write_terminal_summary_reaches_get_review_detail(self):
        """`_write_terminal` (the mock/in-process persist-stage equivalent)
        -> `get_review_detail`. Fails pre-fix: the reader looked for an
        attribute name no writer has ever written, so this came back None."""
        self._seed_bare_row("rid-terminal")

        pipeline_runner._write_terminal(
            "rid-terminal",
            {"decision": "REQUEST_CHANGE", "summary": SUMMARY_TEXT},
            False,
            self.ddb,
        )

        detail = reviews_module.get_review_detail("rid-terminal", OWNER, self.ddb)
        self.assertEqual(
            detail["verdict_summary"],
            SUMMARY_TEXT,
            "get_review_detail must surface the prose the REAL writer just "
            "persisted. Pre-fix this was None on every real review because "
            "the reader read `verdict_summary` and the writer wrote `summary`.",
        )

    def test_write_real_terminal_summary_reaches_get_review_detail(self):
        """The same round trip through the REAL pipeline's terminal writer --
        the one that runs for an actual model-backed review, as opposed to
        `_write_terminal`'s mock path above. Both must agree."""
        self._seed_bare_row("rid-real")

        pipeline_runner._write_real_terminal(
            "rid-real",
            {"status": "OK", "decision": "REQUEST_CHANGE", "summary": SUMMARY_TEXT},
            None,  # output_s3_key -- irrelevant to the field under test
            self.ddb,
        )

        detail = reviews_module.get_review_detail("rid-real", OWNER, self.ddb)
        self.assertEqual(detail["verdict_summary"], SUMMARY_TEXT)

    def test_no_writer_produces_an_attribute_called_verdict_summary(self):
        """The anti-regression assertion the whole suite was missing: after a
        REAL write, the row carries `summary` and NOT `verdict_summary`.

        A future change that reintroduces the phantom name on either side --
        writer or reader -- breaks here rather than silently returning None to
        every caller for another several months."""
        self._seed_bare_row("rid-names")

        pipeline_runner._write_terminal(
            "rid-names",
            {"decision": "ACCEPT", "summary": SUMMARY_TEXT},
            False,
            self.ddb,
        )

        row = self._row("rid-names")
        self.assertEqual(row.get("summary"), SUMMARY_TEXT)
        self.assertNotIn(
            "verdict_summary",
            row,
            "No writer has ever produced an attribute literally called "
            "`verdict_summary`. If this now exists, the write path changed "
            "and every reader/purge list must be re-checked in lockstep.",
        )

    def test_a_review_with_no_summary_still_reads_back_as_none(self):
        """The genuinely-absent case stays None rather than becoming a KeyError
        or a stray empty string -- `get_review_detail` is a faithful
        projection, and an ACCEPT review may legitimately carry no prose."""
        self._seed_bare_row("rid-nosummary")

        pipeline_runner._write_terminal(
            "rid-nosummary", {"decision": "ACCEPT"}, False, self.ddb
        )

        detail = reviews_module.get_review_detail("rid-nosummary", OWNER, self.ddb)
        self.assertIsNone(detail["verdict_summary"])


# ---------------------------------------------------------------------------
# 2. A purge actually clears the prose -- on BOTH deployment targets.
# ---------------------------------------------------------------------------


class TestPurgeActuallyClearsTheSummary(SummaryAttributeTestBase):
    def _seed_written_and_past_window(self, review_id: str, **kwargs: Any) -> None:
        """A past-retention row whose summary was put there by the REAL
        writer -- never by a hand-written put_item carrying the field under
        test, which is exactly how this gap stayed invisible."""
        self._seed_bare_row(review_id, age_days=400, window_days=90, **kwargs)
        pipeline_runner._write_terminal(
            review_id,
            {"decision": "REQUEST_CHANGE", "summary": SUMMARY_TEXT},
            False,
            self.ddb,
        )
        self.assertEqual(self._row(review_id).get("summary"), SUMMARY_TEXT)

    def test_backend_sweep_clears_the_real_summary_attribute(self):
        self._seed_written_and_past_window("rid-purge-backend")

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertIn("rid-purge-backend", summary["deleted_reviews"])
        row = self._row("rid-purge-backend")
        self.assertNotIn(
            "summary",
            row,
            "The purge must clear the narrative prose the writer actually "
            "persisted. Pre-fix the REMOVE clause named `verdict_summary` -- "
            "an attribute nothing writes -- so this Confidential field "
            "survived every sweep.",
        )

    def test_production_worker_lambda_clears_the_real_summary_attribute(self):
        """The copy that actually runs on AWS -- fixing only
        backend/src/retention.py would leave the deployment leaking."""
        self._seed_written_and_past_window("rid-purge-lambda")

        summary = purge_handler_module.run_purge_sweep()

        self.assertIn("rid-purge-lambda", summary["deleted_reviews"])
        self.assertNotIn("summary", self._row("rid-purge-lambda"))

    def test_a_legal_held_row_keeps_its_summary(self):
        """Legal hold overrides purge for every substance field -- the fix
        must not widen WHAT is cleared, only correct the name."""
        self._seed_written_and_past_window("rid-purge-held", legal_hold=True)

        retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertEqual(self._row("rid-purge-held").get("summary"), SUMMARY_TEXT)

    def test_purged_row_reads_back_with_no_summary_through_the_real_reader(self):
        """End to end, the property that actually matters: after a purge, the
        API surface no longer discloses the prose."""
        self._seed_written_and_past_window("rid-purge-detail")

        retention_module.run_purge_sweep_now(self.s3, self.ddb)

        detail = reviews_module.get_review_detail("rid-purge-detail", OWNER, self.ddb)
        self.assertIsNone(detail["verdict_summary"])


# ---------------------------------------------------------------------------
# 3. The two purge implementations still agree, field for field.
# ---------------------------------------------------------------------------


class TestPurgeListsStayIdentical(SummaryAttributeTestBase):
    def _backend_remove_fields(self) -> set[str]:
        """The field names `retention.run_purge_sweep_now`'s REMOVE clause
        actually clears, parsed out of the (implicitly concatenated) source
        literal.

        Robust to a `SET purged_at = :now` clause sharing the SAME
        UpdateExpression ahead of the REMOVE clause (DynamoDB allows
        `SET ... REMOVE ...` in one expression) -- this reassembles every
        adjacent quoted-string literal inside the `UpdateExpression=(...)`
        call into one string first, then extracts just the REMOVE clause
        out of it, rather than assuming the expression starts with the
        literal "REMOVE".
        """
        import inspect
        import re

        source = inspect.getsource(retention_module.run_purge_sweep_now)
        call_match = re.search(r"UpdateExpression=\(((?:\s*\"[^\"]*\")+)\s*\)", source)
        self.assertIsNotNone(call_match, "could not locate the UpdateExpression literal")
        expr = "".join(re.findall(r'"([^"]*)"', call_match.group(1)))
        remove_match = re.search(r"\bREMOVE\s+(.+)$", expr)
        self.assertIsNotNone(remove_match, "could not locate the REMOVE clause")
        return {f.strip() for f in remove_match.group(1).split(",") if f.strip()}

    def test_backend_remove_clause_matches_the_lambda_substance_fields(self):
        """Both implementations must clear the SAME set. Compared as parsed
        field names rather than by eyeballing two comment blocks, so a field
        added to one and forgotten in the other fails here."""
        self.assertEqual(
            self._backend_remove_fields(),
            set(purge_handler_module.SUBSTANCE_FIELDS),
            "backend/src/retention.py's REMOVE clause and the purge worker "
            "Lambda's SUBSTANCE_FIELDS must stay identical -- the two purge "
            "implementations are required to clear the same fields.",
        )

    def test_the_phantom_name_is_gone_from_both(self):
        self.assertNotIn("verdict_summary", purge_handler_module.SUBSTANCE_FIELDS)
        self.assertIn("summary", purge_handler_module.SUBSTANCE_FIELDS)

        # Parse the clause rather than substring-matching "REMOVE
        # verdict_summary": the phantom could reappear in ANY position
        # (`REMOVE summary, verdict_summary, ...`) and a prefix match would
        # sail straight past it -- the same not-quite-looking-at-the-real-
        # thing mistake that let this bug live in the first place.
        backend_fields = self._backend_remove_fields()
        self.assertNotIn("verdict_summary", backend_fields)
        self.assertIn("summary", backend_fields)

    def test_toaster_guidance_is_in_both(self):
        """toaster_guidance (issue #398, Confidential per-review free text --
        docs/data-handling.md) was in NEITHER purge list before this fix: a
        live retention leak, not a phantom-name bug like
        issue_rationale_text below."""
        self.assertIn("toaster_guidance", purge_handler_module.SUBSTANCE_FIELDS)
        self.assertIn("toaster_guidance", self._backend_remove_fields())

    def test_issue_rationale_text_is_gone_from_both(self):
        """No writer has ever produced an attribute literally called
        `issue_rationale_text` (see this file's module docstring and
        backend/src/retention.py's own comment on the REMOVE clause) --
        keeping it in a purge list cleared nothing. It must be gone from
        both, not just renamed in one."""
        self.assertNotIn("issue_rationale_text", purge_handler_module.SUBSTANCE_FIELDS)
        self.assertNotIn("issue_rationale_text", self._backend_remove_fields())

    def test_purged_at_is_a_set_not_a_remove(self):
        """purged_at is the durable marker THAT a purge happened -- it must
        be SET, never listed among the fields a purge REMOVEs (that would
        make the marker erase itself)."""
        self.assertNotIn("purged_at", self._backend_remove_fields())
        self.assertNotIn("purged_at", purge_handler_module.SUBSTANCE_FIELDS)


# ---------------------------------------------------------------------------
# 4. toaster_guidance (issue #398): a real retention leak, not a phantom name.
#
# Unlike `issue_rationale_text` above (a name nothing has ever written),
# `toaster_guidance` IS written to the row -- `reviews._create_review_row`
# (issue #431) -- and carries the submitter's own free-text prose,
# Confidential per docs/data-handling.md's field table. It was simply never
# added to either purge implementation's clear-list. These tests drive the
# REAL writer, `_create_review_row`, into a real (moto) DynamoDB table and
# then run the REAL purge sweep against it -- same discipline as section 2
# above.
# ---------------------------------------------------------------------------


class TestToasterGuidancePurge(SummaryAttributeTestBase):
    def _seed_written_and_past_window(self, review_id: str, legal_hold: bool = False) -> None:
        """toaster_guidance persisted by the REAL row-creation writer, then
        aged past its own snapshotted window and terminalized by a direct
        update -- `_create_review_row` itself is the writer under test; only
        the unrelated status/timing fields are hand-set afterward."""
        reviews_module._create_review_row(
            review_id,
            OWNER["cognito_sub"],
            "eiaa",
            "bundle-hash-guidance-test",
            self.ddb,
            toaster_guidance=GUIDANCE_TEXT,
        )
        self.assertEqual(self._row(review_id).get("toaster_guidance"), GUIDANCE_TEXT)

        self.reviews_table.update_item(
            Key={"review_id": review_id},
            UpdateExpression=(
                "SET #st = :status, created_at = :created_at, "
                "retention_window_at_creation = :window, legal_hold = :hold"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":status": "DONE",
                ":created_at": str(int(retention_module.now_epoch() - 400 * DAY)),
                ":window": 90,
                ":hold": legal_hold,
            },
        )

    def test_backend_sweep_clears_toaster_guidance(self):
        self._seed_written_and_past_window("rid-guidance-backend")

        summary = retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertIn("rid-guidance-backend", summary["deleted_reviews"])
        self.assertNotIn(
            "toaster_guidance",
            self._row("rid-guidance-backend"),
            "toaster_guidance is Confidential per-review free text and was "
            "in NEITHER purge list before this fix -- a live retention leak.",
        )

    def test_production_worker_lambda_clears_toaster_guidance(self):
        """The copy that actually runs on AWS -- fixing only
        backend/src/retention.py would leave the deployment leaking."""
        self._seed_written_and_past_window("rid-guidance-lambda")

        summary = purge_handler_module.run_purge_sweep()

        self.assertIn("rid-guidance-lambda", summary["deleted_reviews"])
        self.assertNotIn("toaster_guidance", self._row("rid-guidance-lambda"))

    def test_a_legal_held_row_keeps_toaster_guidance(self):
        """Legal hold overrides purge for every substance field -- the fix
        must not widen WHAT is cleared, only add the missing field."""
        self._seed_written_and_past_window("rid-guidance-held", legal_hold=True)

        retention_module.run_purge_sweep_now(self.s3, self.ddb)

        self.assertEqual(
            self._row("rid-guidance-held").get("toaster_guidance"), GUIDANCE_TEXT
        )


# ---------------------------------------------------------------------------
# 5. purged_at: the durable marker that a row WAS purged.
#
# Stamped by BOTH purge implementations in the SAME update as their REMOVE
# clause -- read by the #499 cover-note gate
# (tests/test_cover_note_499.py::TestRetentionGating) as a definitive signal,
# ahead of and independent from that gate's existing (necessarily more
# conservative) prediction via `_is_past_retention`/`_is_legal_held`.
# ---------------------------------------------------------------------------


class TestPurgedAtMarker(SummaryAttributeTestBase):
    def _seed_written_and_past_window(self, review_id: str) -> None:
        self._seed_bare_row(review_id, age_days=400, window_days=90)
        pipeline_runner._write_terminal(
            review_id,
            {"decision": "REQUEST_CHANGE", "summary": SUMMARY_TEXT},
            False,
            self.ddb,
        )

    def test_purged_at_is_absent_before_any_sweep(self):
        self._seed_written_and_past_window("rid-purged-at-pre")

        self.assertIsNone(self._row("rid-purged-at-pre").get("purged_at"))

    def test_backend_sweep_stamps_purged_at(self):
        self._seed_written_and_past_window("rid-purged-at-backend")

        before = retention_module.now_epoch()
        retention_module.run_purge_sweep_now(self.s3, self.ddb)
        after = retention_module.now_epoch()

        stamped = self._row("rid-purged-at-backend").get("purged_at")
        self.assertIsNotNone(stamped, "the backend sweep must stamp purged_at")
        self.assertTrue(
            before - 5 <= float(stamped) <= after + 5,
            f"purged_at={stamped!r} is not a plausible epoch-seconds stamp "
            f"for a sweep that ran between {before} and {after}",
        )

    def test_production_worker_lambda_stamps_purged_at(self):
        """The copy that actually runs on AWS must stamp it too -- fixing
        only backend/src/retention.py would leave the deployment with no
        marker at all."""
        self._seed_written_and_past_window("rid-purged-at-lambda")

        before = retention_module.now_epoch()
        purge_handler_module.run_purge_sweep()
        after = retention_module.now_epoch()

        stamped = self._row("rid-purged-at-lambda").get("purged_at")
        self.assertIsNotNone(stamped, "the production Lambda must stamp purged_at")
        self.assertTrue(before - 5 <= float(stamped) <= after + 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
