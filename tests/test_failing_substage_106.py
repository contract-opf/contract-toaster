#!/usr/bin/env python3
"""
Executable tests for issue #106: name the real sub-stage of a `run_review`
failure instead of the opaque "run_review" umbrella.

## The bug this locks down

`backend/src/pipeline_runner.py::run_real_pipeline` holds `stage =
"run_review"` for the ENTIRE duration of `scripts/review_spine.py::
run_review` -- normalization, OPF composition, the Floor judge,
reconciliation, the leakage scan, block compile and the OOXML round-trip all
happen inside that one call. An unhandled exception from ANY of those
sub-stages therefore recorded `failing_stage="run_review"` on the reviews
row, indistinguishable from an actual model failure -- and
`frontend/src/ReviewSubmission.tsx`'s `STAGE_EXPLANATIONS.run_review` copy
used to fill that gap by guessing "the model could not complete the review
... check the account, key and model", which is confidently wrong for a
defect anywhere else in that list (the #93 shape, had it raised instead of
refusing).

## The fix

`run_review`'s optional `on_progress` callback already reports the spine's
real sub-stage (`primary_pass` -> `critic_pass` -> `reconciliation` ->
`redline`, `scripts/review_spine.py`'s `PROGRESS_STAGES`) immediately before
each one starts -- wired since issue #447 to `_write_progress_stage` for the
live polling UI. `run_real_pipeline` now ALSO remembers the last token
reported locally, and its fail-closed `except` records THAT as
`failing_stage` instead of the literal `"run_review"` whenever at least one
was reported before the exception.

This test drives a `RuntimeError` raised from inside the (faked) leakage
stage -- i.e. after `report_progress(PROGRESS_REDLINE)` has already fired,
exactly where `scripts/review_spine.py` calls
`redline_generate.generate_redline[_from_blocks]` (which is what actually
invokes the leakage scan) -- and asserts the reviews row's `failing_stage`
names that sub-stage (`"redline"`), not `"run_review"`.

This test MUST FAIL on the pre-fix tree (`failing_stage` is unconditionally
`"run_review"` for every exception raised inside `run_review`).

The substitution is also FENCED, and that fence is tested here too (review
round 2): `last_progress_stage` outlives the `run_review` call, so it may
only stand in for `stage` while `stage` is still the `"run_review"` umbrella.
A failure in a LATER stage -- `run_review` returned, the redline exists, and
persisting it is what failed -- must keep `stage`'s own accurate value, or
the row would blame a sub-stage that actually succeeded.

Fully offline -- fake DynamoDB/S3, `review_spine.run_review` replaced with a
double that reports progress then raises, no network.
Run standalone: `python3 tests/test_failing_substage_106.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import os  # noqa: E402

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import pipeline_runner as pr  # noqa: E402
import review_spine  # noqa: E402

REVIEW_ID = "00000000-0000-4000-a000-000000000106"


# ---------------------------------------------------------------------------
# Offline fakes -- same shapes as tests/test_review_failure_reason_442.py,
# the file this one is the sibling of.
# ---------------------------------------------------------------------------

class FakeReviewsTable:
    """Generic-enough SET interpreter to apply whatever UpdateExpression
    record_stage_failure (or _write_progress_stage) emits."""

    def __init__(self, status: str = "PENDING"):
        self.item: dict[str, Any] = {"review_id": REVIEW_ID, "status": status}

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        # `_write_progress_stage`'s write is conditioned on status == RUNNING
        # and `record_stage_failure`'s guard is "not already the success
        # terminal" -- this fake does not model DynamoDB's real conditional
        # evaluation, so it simply always applies the SET, which is enough
        # for these tests: they only ever observe the LAST write anyway
        # (progress writes happen before the exception; the failure write
        # happens once, after).
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_clause = UpdateExpression.split("SET", 1)[1]
        for assignment in set_clause.split(","):
            field_token, _, val_token = assignment.strip().partition("=")
            field = names.get(field_token.strip(), field_token.strip())
            self.item[field] = values[val_token.strip()]


class FakePlaybooksTable:
    def __init__(self, active_hashes: dict[str, str]):
        self._active_hashes = dict(active_hashes)

    def get_item(self, Key):
        playbook_id = Key["playbook_id"]
        active_hash = self._active_hashes.get(playbook_id)
        if active_hash is None:
            return {}
        return {"Item": {"playbook_id": playbook_id, "active_release_bundle_hash": active_hash}}


class FakeDDB:
    def __init__(self, reviews_table: FakeReviewsTable):
        self._reviews = reviews_table
        self._playbooks = FakePlaybooksTable({"synthetic-generic": "hash-1"})

    def Table(self, name):
        if name == os.environ["PLAYBOOKS_TABLE"]:
            return self._playbooks
        return self._reviews


class FakeS3:
    def __init__(self, uploads: dict[str, bytes]):
        self._uploads = uploads

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._uploads[Key])}

    def put_object(self, Bucket, Key, Body, **_kwargs):
        pass


# A successful `run_review` return, minimal but real-shaped: the two keys
# `run_real_pipeline` reads off a result before it reaches the terminal write
# (`result["status"]` and `result.get("decision")`). Only used by the
# persist-stage test below, where `_write_real_output` -- the very first thing
# after `stage = "persist_result"` -- is patched to raise, so nothing actually
# reads these; they are here so the double cannot be mistaken for a shape no
# producer builds.
_SPINE_RESULT: dict[str, Any] = {"status": "OK", "decision": "REQUEST_CHANGE"}


def _payload() -> dict[str, Any]:
    return {
        "review_id": REVIEW_ID,
        "owner_sub": "user-1",
        "playbook_id": "synthetic-generic",
        "upload_s3_key": f"uploads/user-1/{REVIEW_ID}/in.docx",
        "release_bundle_hash": "hash-1",
    }


def _drive_pipeline(fake_run_review: Any, *extra_patches: Any) -> FakeReviewsTable:
    """Run `run_real_pipeline` fully offline against `fake_run_review`,
    applying any `extra_patches` on top of the standing offline stack."""
    reviews_table = FakeReviewsTable()
    s3 = FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": b"PK-not-a-real-docx"})

    with ExitStack() as stack:
        for patcher in (
            patch.object(pr, "_settle_reservation"),
            patch.object(pr, "_load_playbook_bundle", return_value={}),
            patch.object(
                pr,
                "_bundle_with_openrouter_model_ids",
                side_effect=lambda bundle, dynamodb_resource=None: bundle,
            ),
            patch.object(pr, "_fetch_upload_bytes", return_value=b"docx"),
            patch.object(pr.review_spine, "run_review", side_effect=fake_run_review),
            *extra_patches,
        ):
            stack.enter_context(patcher)
        pr.run_real_pipeline(
            REVIEW_ID, _payload(),
            dynamodb_resource=FakeDDB(reviews_table), s3_client=s3,
            model_client=object(),
        )
    return reviews_table


def _reporting_spine(progress_tokens: tuple[str, ...], outcome: Any) -> Any:
    """A `review_spine.run_review` double that reports each of
    `progress_tokens` via `on_progress` -- the SAME seam the real spine calls
    immediately before each sub-stage starts -- and then either raises
    `outcome` (an exception) or returns it (a result dict)."""

    def fake_run_review(*args: Any, on_progress=None, **kwargs: Any) -> Any:
        for token in progress_tokens:
            if on_progress is not None:
                on_progress(token)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return fake_run_review


def _run_with_spine_reporting_then_raising(
    progress_tokens: tuple[str, ...], exc: BaseException
) -> FakeReviewsTable:
    """`run_review` reports `progress_tokens`, then raises `exc` -- exactly
    as an unclassified defect anywhere past the last reported sub-stage
    would.
    """
    return _drive_pipeline(_reporting_spine(progress_tokens, exc))


def _run_with_spine_returning_then_persist_raising(
    progress_tokens: tuple[str, ...], exc: BaseException
) -> FakeReviewsTable:
    """`run_review` reports `progress_tokens` and then RETURNS normally, and
    the persist step that runs after it raises `exc`.

    The seam is `_write_real_output` -- the first thing `run_real_pipeline`
    does after `stage = "persist_result"`, and a genuinely fallible one (it
    is the S3 put of the redline object). This is the case that exercises
    the `stage == "run_review"` half of `recorded_stage`'s condition: by the
    time this exception is raised `last_progress_stage` is still set (to the
    last token `run_review` reported before succeeding), but `stage` has
    moved on, and the guard is what stops the later failure being
    misattributed to the earlier sub-stage.
    """
    return _drive_pipeline(
        _reporting_spine(progress_tokens, _SPINE_RESULT),
        patch.object(pr, "_write_real_output", side_effect=exc),
    )


class TestFailingSubstageNamedInsteadOfRunReviewUmbrella(unittest.TestCase):
    def test_exception_after_redline_progress_names_redline_not_run_review(self) -> None:
        """A `RuntimeError` raised inside the leakage stage -- which runs
        INSIDE `redline_generate.generate_redline[_from_blocks]`, called
        after `scripts/review_spine.py` reports `PROGRESS_REDLINE` -- must
        land `failing_stage` naming that sub-stage, not the opaque
        `"run_review"` umbrella. This is the exact scenario #106 exists
        for: a code defect past the last reported sub-stage used to be
        reported identically to a bare model failure."""
        table = _run_with_spine_reporting_then_raising(
            (review_spine.PROGRESS_PRIMARY_PASS, review_spine.PROGRESS_CRITIC_PASS,
             review_spine.PROGRESS_RECONCILIATION, review_spine.PROGRESS_REDLINE),
            RuntimeError("leakage scan blew up"),
        )
        self.assertEqual(table.item["failing_stage"], review_spine.PROGRESS_REDLINE)
        self.assertNotEqual(table.item["failing_stage"], "run_review")
        self.assertEqual(table.item["reason"], "unhandled_exception")
        self.assertEqual(table.item["status"], "ERROR")

    def test_exception_after_only_primary_pass_names_primary_pass(self) -> None:
        """Whichever sub-stage was LAST reported wins -- not merely the
        first or the last possible token -- so a failure early in the
        pipeline (e.g. inside the critic pass) is not misattributed to a
        later stage it never reached."""
        table = _run_with_spine_reporting_then_raising(
            (review_spine.PROGRESS_PRIMARY_PASS,),
            RuntimeError("critic pass blew up"),
        )
        self.assertEqual(table.item["failing_stage"], review_spine.PROGRESS_PRIMARY_PASS)

    def test_exception_before_any_progress_report_keeps_run_review(self) -> None:
        """An exception raised before `run_review` has reported ANYTHING
        (e.g. inside normalization, which runs before the first
        `report_progress` call) has no sub-stage to name, so `failing_stage`
        honestly stays the umbrella `"run_review"` -- this is the existing,
        already-pinned behavior (tests/test_review_failure_reason_442.py)
        and must not regress."""
        table = _run_with_spine_reporting_then_raising(
            (), RuntimeError("blew up before the first progress report"),
        )
        self.assertEqual(table.item["failing_stage"], "run_review")

    def test_a_failure_after_run_review_succeeded_is_not_blamed_on_a_sub_stage(self) -> None:
        """`last_progress_stage` outlives the `run_review` call, so it must
        only ever be substituted for `stage` while `stage` is still the
        "run_review" umbrella it stands in for.

        Here `run_review` reports all four PROGRESS_* markers and RETURNS
        successfully -- the redline was produced -- and the failure happens
        afterwards, in the persist step, where `stage` is already the
        accurate, specific `"persist_result"`. Without the `stage ==
        "run_review"` half of `recorded_stage`'s condition this row would
        record `"redline"`, and `STAGE_EXPLANATIONS.redline` would tell the
        reader "the marked-up copy of your document could not be produced"
        when it WAS produced and only saving it failed -- the same class of
        confidently-wrong message #106 exists to remove, just pointed one
        stage earlier.
        """
        table = _run_with_spine_returning_then_persist_raising(
            (review_spine.PROGRESS_PRIMARY_PASS, review_spine.PROGRESS_CRITIC_PASS,
             review_spine.PROGRESS_RECONCILIATION, review_spine.PROGRESS_REDLINE),
            RuntimeError("the redline object could not be written"),
        )
        self.assertEqual(table.item["failing_stage"], "persist_result")
        self.assertNotEqual(table.item["failing_stage"], review_spine.PROGRESS_REDLINE)
        self.assertEqual(table.item["reason"], "unhandled_exception")
        self.assertEqual(table.item["status"], "ERROR")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2)
    sys.exit(0 if result.result.wasSuccessful() else 1)
