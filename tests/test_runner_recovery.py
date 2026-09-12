#!/usr/bin/env python3
"""
Issue #62: a container restart must not strand an in-process review.

## What was broken

On the Docker Compose target a review runs on an in-process
`ThreadPoolExecutor` inside the API container
(`backend/src/pipeline_runner.py::InProcessStepFunctionsClient`). Nothing
outside that process knows the run exists, so a Coolify redeploy killed
whatever was mid-flight with the row still `RUNNING` and nothing left alive
to write a terminal: History showed a review that never ended, the
worst-case daily-spend reservation was never settled, and
`GET /api/admin/health` counted it as in-flight forever. The AWS target has
`infra/lambda/orphan_reconciler` for the equivalent condition; this target
had nothing.

## THE SELECTOR CORRECTION THIS FILE PINS

Issue #62 specified the orphan selector as "no `execution_arn` (in-process,
not Step Functions)". Those are not the same set on this codebase, and the
difference is the entire feature:

    reviews.ensure_execution_started records the ARN its client returns onto
    the REVIEWS row for BOTH targets, and the in-process client returns the
    pseudo-ARN `inprocess:<execution-name>`.

So an in-process review that actually STARTED — the overwhelming majority of
what a redeploy strands, and the only kind that has spent money — carries an
`execution_arn`. An `attribute_not_exists(execution_arn)` selector would have
recovered nothing in production while passing a perfectly green test suite.
`runner_recovery.is_orphaned` therefore asks the question
`reviews.stop_running_execution` already asks of the same field, through the
same predicate (`reviews.is_step_functions_execution`): is there a REAL
execution behind this row? Row `a-inprocess-arn` below is that case, and it
is deliberately part of the expected result.

## Fixture fidelity

Every reviews row, submission row and spend reservation here is written by
the PRODUCTION writers, in the order production calls them:

    reviews._create_review_row            -> the PENDING row
    reviews.create_submission_record      -> the submission/idempotency row
    reviews.reserve_spend                 -> the real worst-case reservation
    reviews._record_spend_reservation     -> reservation id onto the submission
    reviews.ensure_execution_started      -> the execution_arn onto both rows,
                                             driven by the REAL
                                             InProcessStepFunctionsClient for
                                             the in-process rows and by an
                                             AWS-shaped client for the Step
                                             Functions row
    pipeline_runner._mark_running         -> PENDING -> RUNNING (on every row
                                             except the queued orphan, which
                                             never reached a runner)
    pipeline_runner._write_progress_stage -> the `progress_stage` a live run
                                             carries

The ONE synthetic step is `_backdate`, which rewrites `updated_at` on the
rows meant to predate this process. It stands in for wall-clock time passing
between a previous process's last write and this one's boot — not for a row
shape, which is why nothing else is hand-built.

`failing_stage` is deliberately NOT asserted as "preserved" by seeding one:
no production writer puts `failing_stage` on a non-terminal row
(`reviews.record_stage_failure` writes it together with a terminal status),
so a RUNNING row carrying one is a state the system cannot reach. What is
pinned instead is the real claim behind that requirement — the recovery
write NAMES no `failing_stage` at all, asserted both on the row and on the
UpdateExpression the module actually emits — plus that the live-run field a
RUNNING row really does carry, `progress_stage`, survives.

## BOTH members of the non-terminal vocabulary are seeded

`runner_recovery.recover_orphaned_reviews` iterates
`reviews.REVIEW_STATUSES_NON_TERMINAL` and `_relabel`'s ConditionExpression
repeats those members as hand-written literals (`:pending`, `:running`). A
fixture that seeded only RUNNING rows would leave the PENDING iteration and
the `:pending` literal green whatever they said. So `ORPHAN_PENDING` is
seeded PENDING and deliberately never run: that is the row
`InProcessStepFunctionsClient.shutdown()`'s `cancel_futures=True` really
manufactures, because a review still queued on the pool is dropped before
its runner reaches `_mark_running`.

The moto reviews table declares `status-index` exactly as
`infra/lib/nested/data-stack.ts` and `deploy/dts/bootstrap.py` do, so the
boot-time read is exercised through the real GSI rather than through
`_query_by_status`'s no-`.query()` fallback.

Fully offline: moto for every AWS call, no network.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-recovery62-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-recovery62-test")
os.environ.setdefault(
    "REVIEW_SUBMISSIONS_TABLE", "contract-toaster-submissions-recovery62-test"
)
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-recovery62-test")
os.environ.setdefault("USERS_TABLE", "contract-toaster-users-recovery62-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-recovery62-test")
os.environ.setdefault("SYNC_STATUS_TABLE", "contract-toaster-sync-recovery62-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:000000000000:stateMachine:contract-toaster-test",
)
# Six REAL reservations, one per seeded review, against the real
# `reserve_spend` cap check -- and one worst-case reservation is a large
# fraction of the $20.00 default, so the later submissions would legitimately
# be refused 429. The env cap (`model_settings.env_daily_spend_cap_cents`,
# the documented deployment knob, well under MAX_DAILY_SPEND_CAP_USD_CENTS)
# is raised so the fixture can seed a realistic backlog. The cap is not what
# this file is about, and softening `reserve_spend` itself would be.
os.environ.setdefault("DAILY_SPEND_CAP_USD_CENTS", "500000")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.main as backend_main  # noqa: E402
import src.pipeline_runner as pipeline_runner  # noqa: E402
import src.purge_scheduler as purge_scheduler  # noqa: E402
import src.reviews as reviews  # noqa: E402
import src.runner_recovery as runner_recovery  # noqa: E402

REGION = "us-east-1"
OWNER = "sub-reviewer-1"
BUNDLE_HASH = "bundle-hash-1"
HOUR = 3600

# The four shapes issue #62 enumerates, plus the fifth the ticket's own
# selector would have missed and the sixth THIS diff manufactures.
# `orphan` is the expected outcome.
#   id                  -> (arn kind, age, orphan?)
ORPHAN_NO_ARN = "r-a-no-arn"           # RUNNING, died before StartExecution
ORPHAN_INPROCESS = "r-a-inprocess-arn"  # RUNNING, the ordinary stranded run
# PENDING, never started: `InProcessStepFunctionsClient.shutdown()` passes
# `cancel_futures=True`, so a review queued on the pool when the container
# went down was dropped before its runner reached
# `pipeline_runner._mark_running`. Its row therefore died PENDING with an
# `inprocess:` ARN already on it -- the second half of
# `REVIEW_STATUSES_NON_TERMINAL`, and the case `src/main.py`'s shutdown
# docstring names ("a queued review that never ran has a row this process
# must still speak for"). Without it the PENDING iteration of the query loop
# and the `:pending` literal in `_relabel`'s ConditionExpression are green
# forever whatever they say.
ORPHAN_PENDING = "r-a-pending-queued"   # PENDING, dropped by cancel_futures
LIVE_STEPFUNCTIONS = "r-b-sfn-arn"      # RUNNING on AWS: another owner's row
LIVE_THIS_PROCESS = "r-c-fresh"         # RUNNING, touched after this boot
ALREADY_DONE = "r-d-done"               # terminal: nothing to recover

# Every row this recovery is expected to claim, and every row it must not.
ORPHANED_IDS = (ORPHAN_NO_ARN, ORPHAN_INPROCESS, ORPHAN_PENDING)
RUNNING_ORPHAN_IDS = (ORPHAN_NO_ARN, ORPHAN_INPROCESS)
UNTOUCHED_IDS = (LIVE_STEPFUNCTIONS, LIVE_THIS_PROCESS, ALREADY_DONE)

# The status each orphan is relabelled FROM -- asserted per row, because the
# audit entry's `previous_status` is the only place the vocabulary the query
# loop iterated shows up in the record.
PREVIOUS_STATUS_BY_ID = {
    ORPHAN_NO_ARN: "RUNNING",
    ORPHAN_INPROCESS: "RUNNING",
    ORPHAN_PENDING: "PENDING",
}


class AwsShapedSfnClient:
    """A Step Functions client that answers with a REAL-shaped execution ARN.

    Deliberately not a blanket mock: `ensure_execution_started` stores
    whatever `executionArn` comes back, and the whole point of the row it
    produces is that `reviews.is_step_functions_execution` says True for it.
    A stand-in that returned `inprocess:...` (or a bare string) would make
    the "AWS row is left alone" assertion below vacuous.
    """

    class exceptions:  # noqa: N801 - botocore's own spelling
        class ExecutionAlreadyExists(Exception):
            pass

    def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> dict:  # noqa: A002,N803
        prefix = stateMachineArn.replace(":stateMachine:", ":execution:")
        return {"executionArn": f"{prefix}:{name}", "startDate": int(time.time())}


class RecordingPool:
    """A ThreadPoolExecutor stand-in that ACCEPTS work and never runs it.

    That is the queued-at-shutdown state, and it is what leaves an id in
    `in_flight_review_ids()` for the shutdown assertions. `shutdown` records
    its kwargs rather than asserting on them, so the test — not the double —
    says what the contract is.
    """

    def __init__(self) -> None:
        self.submitted: list[tuple[Any, tuple[Any, ...]]] = []
        self.shutdown_calls: list[dict[str, Any]] = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))
        return None

    def shutdown(self, **kwargs: Any) -> None:
        self.shutdown_calls.append(kwargs)


def _reviews_table_spec(name: str) -> dict[str, Any]:
    """The reviews table as infra/lib/nested/data-stack.ts and
    deploy/dts/bootstrap.py declare the parts this read touches."""
    return {
        "TableName": name,
        "KeySchema": [{"AttributeName": "review_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "review_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "status-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        "BillingMode": "PAY_PER_REQUEST",
    }


def _submissions_table_spec(name: str) -> dict[str, Any]:
    return {
        "TableName": name,
        "KeySchema": [{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "idempotency_key", "AttributeType": "S"},
            {"AttributeName": "review_id", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "review_id-index",
                "KeySchema": [{"AttributeName": "review_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        "BillingMode": "PAY_PER_REQUEST",
    }


class RecoveryFixture(unittest.TestCase):
    """Shared moto world, seeded through the production writers."""

    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)

        self.ddb = boto3.resource("dynamodb", region_name=REGION)
        self.ddb.create_table(**_reviews_table_spec(os.environ["REVIEWS_TABLE"]))
        self.ddb.create_table(
            **_submissions_table_spec(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        )
        self.ddb.create_table(
            TableName=os.environ["DAILY_SPEND_TABLE"],
            KeySchema=[{"AttributeName": "spend_date", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "spend_date", "AttributeType": "S"}],
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
        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.submissions_table = self.ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        self.audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])

    # -- seeding ---------------------------------------------------------

    def _submit(self, review_id: str, *, sfn_client: Any | None) -> None:
        """One submission, through the real submit-path writers in order."""
        reviews._create_review_row(
            review_id, OWNER, "synthetic-generic", BUNDLE_HASH, self.ddb
        )
        submission = reviews.create_submission_record(
            idempotency_key=f"idem-{review_id}",
            owner_sub=OWNER,
            upload_pointer=f"uploads/{OWNER}/{review_id}/in.docx",
            release_bundle_hash=BUNDLE_HASH,
            reservation_id=None,
            review_id=review_id,
            execution_name=f"exec-{review_id}",
            execution_input=json.dumps({"review_id": review_id}),
            dynamodb_resource=self.ddb,
        )
        reservation_id = reviews.reserve_spend(review_id, self.ddb)
        reviews._record_spend_reservation(submission, reservation_id, self.ddb)
        if sfn_client is not None:
            reviews.ensure_execution_started(
                submission, submission["execution_input"], self.ddb, sfn_client
            )

    def _run(self, review_id: str) -> None:
        """PENDING -> RUNNING plus the progress marker a live run carries."""
        pipeline_runner._mark_running(review_id, self.ddb)
        pipeline_runner._write_progress_stage(review_id, "critic_pass", self.ddb)

    def _backdate(self, review_id: str, seconds: int) -> None:
        """The only synthetic step: wall-clock time passing (see docstring)."""
        row = self.reviews_table.get_item(Key={"review_id": review_id})["Item"]
        self.reviews_table.update_item(
            Key={"review_id": review_id},
            UpdateExpression="SET updated_at = :then",
            ExpressionAttributeValues={":then": str(int(row["updated_at"]) - seconds)},
        )

    def _inprocess_client(self) -> pipeline_runner.InProcessStepFunctionsClient:
        """The REAL in-process client, with a pool that accepts and drops the
        work — the pseudo-ARN it records is the thing under test, not the
        pipeline body."""
        return pipeline_runner.InProcessStepFunctionsClient(
            runner=lambda _rid, _payload: None, pool=RecordingPool()
        )

    def seed_world(self) -> int:
        """Seed all six rows; return the `process_started_at` to recover at."""
        inproc = self._inprocess_client()

        self._submit(ORPHAN_NO_ARN, sfn_client=None)
        self._run(ORPHAN_NO_ARN)

        self._submit(ORPHAN_INPROCESS, sfn_client=inproc)
        self._run(ORPHAN_INPROCESS)

        # Deliberately NOT passed through `_run`: this is the review that was
        # still sitting on the pool's queue when the process died, so no
        # runner ever reached `_mark_running` and the row is still PENDING.
        self._submit(ORPHAN_PENDING, sfn_client=inproc)

        self._submit(LIVE_STEPFUNCTIONS, sfn_client=AwsShapedSfnClient())
        self._run(LIVE_STEPFUNCTIONS)

        self._submit(LIVE_THIS_PROCESS, sfn_client=self._inprocess_client())
        self._run(LIVE_THIS_PROCESS)

        self._submit(ALREADY_DONE, sfn_client=self._inprocess_client())
        self._run(ALREADY_DONE)
        self.reviews_table.update_item(
            Key={"review_id": ALREADY_DONE},
            UpdateExpression="SET #s = :done",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":done": "DONE"},
        )

        for review_id in (*ORPHANED_IDS, LIVE_STEPFUNCTIONS, ALREADY_DONE):
            self._backdate(review_id, HOUR)

        # This process boots here: the five backdated rows predate it.
        started = int(time.time())
        # ...and LIVE_THIS_PROCESS is then touched by the runner THIS process
        # owns, through the real progress writer. Ordered after `started` on
        # purpose: seeding six submissions takes long enough to cross a
        # second boundary, so a row written before the boot stamp would
        # sometimes be a second older than it and the "still live" case would
        # pass or fail on the clock.
        pipeline_runner._write_progress_stage(LIVE_THIS_PROCESS, "critic_pass", self.ddb)
        return started

    def rows(self) -> dict[str, dict[str, Any]]:
        return {
            item["review_id"]: item
            for item in self.reviews_table.scan()["Items"]
        }


# ---------------------------------------------------------------------------
# 1. Selection + the terminal write
# ---------------------------------------------------------------------------


class TestRecoverOrphanedReviews(RecoveryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.process_started_at = self.seed_world()
        self.before = copy.deepcopy(self.rows())

    def test_the_seed_really_is_the_shape_this_test_claims(self) -> None:
        """A guard on the fixture itself: if `ensure_execution_started` ever
        stops recording the in-process pseudo-ARN, the correction this file
        is built around is no longer the live behaviour and every assertion
        below is measuring the wrong thing."""
        self.assertNotIn("execution_arn", self.before[ORPHAN_NO_ARN])
        self.assertTrue(
            str(self.before[ORPHAN_INPROCESS]["execution_arn"]).startswith("inprocess:")
        )
        self.assertTrue(
            reviews.is_step_functions_execution(
                self.before[LIVE_STEPFUNCTIONS]["execution_arn"]
            )
        )
        self.assertFalse(
            reviews.is_step_functions_execution(
                self.before[ORPHAN_INPROCESS]["execution_arn"]
            )
        )
        for review_id in (*RUNNING_ORPHAN_IDS, LIVE_STEPFUNCTIONS, LIVE_THIS_PROCESS):
            self.assertEqual(self.before[review_id]["status"], "RUNNING")
            self.assertEqual(self.before[review_id]["progress_stage"], "critic_pass")
        self.assertEqual(self.before[ALREADY_DONE]["status"], "DONE")
        # The queued-but-never-started row: PENDING, with the pseudo-ARN
        # `start_execution` already put on it and no progress marker, because
        # no runner ever got as far as writing one.
        pending = self.before[ORPHAN_PENDING]
        self.assertEqual(pending["status"], "PENDING")
        self.assertTrue(str(pending["execution_arn"]).startswith("inprocess:"))
        self.assertNotIn("progress_stage", pending)
        # Both members of the closed vocabulary the query loop iterates are
        # really present in this world.
        self.assertEqual(
            {self.before[rid]["status"] for rid in ORPHANED_IDS},
            reviews.REVIEW_STATUSES_NON_TERMINAL,
        )

    def test_it_returns_exactly_the_orphaned_in_process_reviews(self) -> None:
        recovered = runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )
        self.assertEqual(recovered, sorted(ORPHANED_IDS))

    def test_the_orphans_become_error_runner_restarted(self) -> None:
        runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )
        after = self.rows()
        for review_id in ORPHANED_IDS:
            with self.subTest(review=review_id):
                row = after[review_id]
                self.assertEqual(row["status"], "ERROR")
                self.assertEqual(row["reason"], reviews.RUNNER_RESTARTED_REASON)
                self.assertEqual(row["reason"], "runner_restarted")
                self.assertTrue(str(row["failed_at"]).isdigit())
                # Recovery invents no stage it cannot know.
                self.assertNotIn("failing_stage", row)
        for review_id in RUNNING_ORPHAN_IDS:
            with self.subTest(review=review_id):
                # The live-run field a RUNNING row really carries survives.
                self.assertEqual(after[review_id]["progress_stage"], "critic_pass")
        # ...and the queued row still has none invented for it.
        self.assertNotIn("progress_stage", after[ORPHAN_PENDING])

    def test_the_recovery_write_names_no_failing_stage(self) -> None:
        """The source-level half of the same promise: #62 asks for
        `failing_stage` to be KEPT, and the only way to keep a field is not
        to write it. A RUNNING row cannot carry one (see this file's
        docstring), so the row assertion above cannot prove it alone."""
        source = (BACKEND_ROOT / "src" / "runner_recovery.py").read_text(encoding="utf-8")
        update_expression = source.split("UpdateExpression=(", 1)[1].split("),", 1)[0]
        self.assertNotIn("failing_stage", update_expression)

    def test_the_other_three_rows_are_untouched(self) -> None:
        runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )
        after = self.rows()
        for review_id in UNTOUCHED_IDS:
            with self.subTest(review=review_id):
                self.assertEqual(after[review_id], self.before[review_id])

    def test_a_second_pass_recovers_nothing_further(self) -> None:
        """Idempotence: the rows it already relabelled are terminal, so a
        crash-loop that boots this repeatedly cannot re-relabel them.

        Deliberately the SAME `process_started_at`, which is the one thing
        that makes this an idempotence assertion. A LATER one would (rightly)
        find `LIVE_THIS_PROCESS` stale as well — from a genuinely new
        process's point of view it IS an orphan — and the test would then be
        measuring the clock rather than the behaviour.
        """
        runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )
        again = runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )
        self.assertEqual(again, [])

    def test_a_row_that_reached_a_terminal_under_us_keeps_it(self) -> None:
        """The race the conditional write exists for: the runner's own
        terminal lands between the query and the update. A persisted DONE
        review must not be relabelled a failure (issue #446's promise)."""
        orphan_row = self.reviews_table.get_item(Key={"review_id": ORPHAN_INPROCESS})["Item"]

        real_query = reviews._query_by_status

        def query_then_finish(table, status_value, **kwargs):
            items = real_query(table, status_value, **kwargs)
            if any(i.get("review_id") == ORPHAN_INPROCESS for i in items):
                self.reviews_table.update_item(
                    Key={"review_id": ORPHAN_INPROCESS},
                    UpdateExpression="SET #s = :done",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":done": "DONE"},
                )
            return items

        with patch.object(reviews, "_query_by_status", query_then_finish):
            recovered = runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )

        self.assertNotIn(ORPHAN_INPROCESS, recovered)
        after = self.reviews_table.get_item(Key={"review_id": ORPHAN_INPROCESS})["Item"]
        self.assertEqual(after["status"], "DONE")
        self.assertNotIn("reason", after)
        self.assertEqual(after["progress_stage"], orphan_row["progress_stage"])

    def test_the_boot_read_uses_the_status_index_and_never_scans(self) -> None:
        """#52's invariant holds for this read too: one `status-index` query
        per non-terminal status, no Scan of the reviews table."""
        calls: list[tuple[str, str | None]] = []
        inner = self.ddb

        class LoggingTable:
            def __init__(self, real: Any) -> None:
                self._real = real

            def query(self, **kwargs: Any) -> Any:
                calls.append(("query", kwargs.get("IndexName")))
                return self._real.query(**kwargs)

            def scan(self, **kwargs: Any) -> Any:
                calls.append(("scan", None))
                return self._real.scan(**kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

        class LoggingResource:
            def Table(self, name: str) -> Any:  # noqa: N802 - boto3's spelling
                real = inner.Table(name)
                return LoggingTable(real) if name == os.environ["REVIEWS_TABLE"] else real

        runner_recovery.recover_orphaned_reviews(
            LoggingResource(), process_started_at=self.process_started_at
        )
        self.assertEqual([kind for kind, _ in calls if kind == "scan"], [])
        self.assertEqual(
            sorted({index for kind, index in calls if kind == "query"}),
            ["status-index"],
        )


# ---------------------------------------------------------------------------
# 2. Settlement — the money half
# ---------------------------------------------------------------------------


class TestReservationSettlement(RecoveryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.process_started_at = self.seed_world()
        self.spend_table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])

    def _spend_date(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time()))

    def _reserved_cents(self) -> int:
        item = self.spend_table.get_item(Key={"spend_date": self._spend_date()})["Item"]
        return int(item["reserved_usd_cents"])

    def _submission(self, review_id: str) -> dict[str, Any]:
        return self.submissions_table.get_item(
            Key={"idempotency_key": f"idem-{review_id}"}
        )["Item"]

    def test_exactly_the_three_orphans_are_settled(self) -> None:
        """Against the REAL reservations `reserve_spend` wrote — six of
        them, one per seeded review — so this measures a credit-back, not a
        spy call count."""
        per_review = reviews.compute_worst_case_reservation_usd_cents(self.ddb)
        self.assertEqual(self._reserved_cents(), 6 * per_review)

        runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )

        self.assertEqual(self._reserved_cents(), 3 * per_review)
        for review_id in ORPHANED_IDS:
            self.assertTrue(self._submission(review_id)["reservation_released"])
        for review_id in UNTOUCHED_IDS:
            self.assertNotIn("reservation_released", self._submission(review_id))

    def test_settle_reservation_for_cancel_is_called_once_per_orphan(self) -> None:
        """The call-shape assertion #62 asks for, alongside the real one
        above: the cancel route's own settlement function, once each, with
        the orphan ids and nothing else."""
        seen: list[str] = []
        real = reviews.settle_reservation_for_cancel

        def spy(review_id: str, dynamodb_resource: Any) -> None:
            seen.append(review_id)
            real(review_id, dynamodb_resource)

        with patch.object(reviews, "settle_reservation_for_cancel", spy):
            runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )
        self.assertEqual(sorted(seen), sorted(ORPHANED_IDS))

    def test_a_failed_settlement_still_reports_the_relabelled_review(self) -> None:
        """The relabel has already landed, so the row IS `ERROR` — leaving it
        out of the returned ids would hide it from the only count an operator
        sees after a restart. The settlement failure is logged, not silent,
        and the reservation is picked up by nothing else, so this is the
        signal that something needs looking at."""
        with patch.object(
            reviews,
            "settle_reservation_for_cancel",
            side_effect=RuntimeError("dynamodb is unreachable"),
        ):
            recovered = runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )
        self.assertEqual(recovered, sorted(ORPHANED_IDS))
        for review_id in ORPHANED_IDS:
            row = self.reviews_table.get_item(Key={"review_id": review_id})["Item"]
            self.assertEqual(row["status"], "ERROR")
            self.assertEqual(row["reason"], "runner_restarted")

    def test_one_failing_row_does_not_strand_the_rest(self) -> None:
        """A single bad row must not leave the rest of the backlog RUNNING —
        that is the condition the whole module exists to clear."""
        real = reviews.settle_reservation_for_cancel

        def explode_on_the_first(review_id: str, dynamodb_resource: Any) -> None:
            if review_id == ORPHAN_INPROCESS:
                raise RuntimeError("dynamodb is unreachable")
            real(review_id, dynamodb_resource)

        with patch.object(
            reviews, "settle_reservation_for_cancel", explode_on_the_first
        ):
            recovered = runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )
        self.assertEqual(recovered, sorted(ORPHANED_IDS))
        self.assertTrue(self._submission(ORPHAN_NO_ARN)["reservation_released"])
        self.assertTrue(self._submission(ORPHAN_PENDING)["reservation_released"])
        self.assertNotIn("reservation_released", self._submission(ORPHAN_INPROCESS))

    def test_a_second_pass_does_not_credit_the_reservation_twice(self) -> None:
        """Same `process_started_at` for the same reason as
        `test_a_second_pass_recovers_nothing_further` above."""
        per_review = reviews.compute_worst_case_reservation_usd_cents(self.ddb)
        for _ in range(2):
            runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )
        self.assertEqual(self._reserved_cents(), 3 * per_review)


# ---------------------------------------------------------------------------
# 3. The audit row
# ---------------------------------------------------------------------------


class TestAuditTrail(RecoveryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.process_started_at = self.seed_world()

    def test_one_audit_entry_per_recovered_review(self) -> None:
        runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )
        entries = [
            item
            for item in self.audit_table.scan()["Items"]
            if item["action"] == runner_recovery.RUNNER_RESTARTED_AUDIT_ACTION
        ]
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            sorted(e["target"] for e in entries),
            sorted(ORPHANED_IDS),
        )
        for entry in entries:
            with self.subTest(target=entry["target"]):
                self.assertEqual(entry["action"], "review_runner_restarted")
                self.assertEqual(entry["target_type"], "review")
                self.assertEqual(entry["outcome"], "success")
                # Issue #253: the attribute the audit `review_id-index` needs.
                self.assertEqual(entry["review_id"], entry["target"])
                self.assertEqual(entry["reason"], "runner_restarted")
                # Per row, not one blanket value: the queued orphan was
                # PENDING, and recording it as RUNNING would misstate the
                # record and hide the vocabulary member it came from.
                self.assertEqual(
                    entry["previous_status"],
                    PREVIOUS_STATUS_BY_ID[str(entry["target"])],
                )
        self.assertEqual(
            {str(e["previous_status"]) for e in entries},
            reviews.REVIEW_STATUSES_NON_TERMINAL,
        )

    def test_the_audit_row_carries_no_review_substance(self) -> None:
        """Identifiers and status tokens only (ARCHITECTURE.md -> audit
        posture). Checked against the fields a reviews row really can hold."""
        runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )
        forbidden = {
            "verdict_summary",
            "issues",
            "toaster_guidance",
            "upload_s3_key",
            "output_s3_key",
            "original_filename",
            "owner_sub",
        }
        for item in self.audit_table.scan()["Items"]:
            self.assertEqual(forbidden & set(item), set())

    def test_an_unwritable_audit_table_does_not_stop_recovery(self) -> None:
        """Audit is best-effort; a boot must not die on it."""
        with patch.dict(os.environ, {"AUDIT_TABLE": ""}):
            recovered = runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )
        self.assertEqual(recovered, sorted(ORPHANED_IDS))
        self.assertEqual(self.rows()[ORPHAN_INPROCESS]["status"], "ERROR")


# ---------------------------------------------------------------------------
# 4. The token itself
# ---------------------------------------------------------------------------


class TestReasonToken(unittest.TestCase):
    def test_the_token_is_named_once_and_resolves_to_error(self) -> None:
        self.assertEqual(reviews.RUNNER_RESTARTED_REASON, "runner_restarted")
        self.assertEqual(
            reviews.STAGE_FAILURE_REASON_STATUS[reviews.RUNNER_RESTARTED_REASON],
            "ERROR",
        )

    def test_it_has_reader_facing_prose_in_the_ui(self) -> None:
        source = (REPO_ROOT / "frontend" / "src" / "ReviewSubmission.tsx").read_text(
            encoding="utf-8"
        )
        block = source.split("const REASON_EXPLANATIONS", 1)[1].split(
            "const STAGE_EXPLANATIONS", 1
        )[0]
        self.assertIn(f"{reviews.RUNNER_RESTARTED_REASON}: {{", block)

    def test_in_process_rows_are_selected_and_step_functions_rows_are_not(self) -> None:
        """The correction, as a unit: `inprocess:` is an execution_arn, and
        it is exactly the one this recovery owns."""
        started = 2_000_000_000
        old = {"updated_at": str(started - HOUR)}
        self.assertTrue(
            runner_recovery.is_orphaned({**old}, process_started_at=started)
        )
        self.assertTrue(
            runner_recovery.is_orphaned(
                {**old, "execution_arn": "inprocess:exec-1"}, process_started_at=started
            )
        )
        self.assertFalse(
            runner_recovery.is_orphaned(
                {
                    **old,
                    "execution_arn": (
                        "arn:aws:states:us-east-1:000000000000:execution:sm:exec-1"
                    ),
                },
                process_started_at=started,
            )
        )
        self.assertFalse(
            runner_recovery.is_orphaned(
                {"updated_at": str(started)}, process_started_at=started
            )
        )
        # No timestamp at all: not recoverable on a guess.
        self.assertFalse(runner_recovery.is_orphaned({}, process_started_at=started))


# ---------------------------------------------------------------------------
# 5. The lifespan — boot half and shutdown half
# ---------------------------------------------------------------------------


class LifespanFixture(RecoveryFixture):
    """Drives the REAL `src/main.py::_lifespan` in-process.

    `verify_required_env` and `ensure_entity_roster_table` are patched out:
    each is the subject of its own test file (#655, #59) and neither is what
    this one is asking about — a full required-env set for this process would
    be a second, drifting copy of that file's fixture. The purge scheduler is
    patched so no background thread outlives the test.
    """

    def setUp(self) -> None:
        super().setUp()
        self.process_started_at = self.seed_world()
        for name in ("verify_required_env", "ensure_entity_roster_table"):
            patcher = patch.object(backend_main.startup_checks, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        scheduler = patch.object(
            backend_main.purge_scheduler, "start_purge_scheduler", return_value=None
        )
        scheduler.start()
        self.addCleanup(scheduler.stop)
        # The singleton is module state; never leak one between tests.
        self.addCleanup(setattr, pipeline_runner, "_SINGLETON", None)
        pipeline_runner._SINGLETON = None

    def run_lifespan(self) -> None:
        async def drive() -> None:
            async with backend_main._lifespan(None):
                pass

        asyncio.run(drive())


class TestLifespanRecovery(LifespanFixture):
    def setUp(self) -> None:
        super().setUp()
        self.before_snapshot = copy.deepcopy(self.rows())

    def test_recovery_runs_on_the_docker_target(self) -> None:
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}, clear=False):
            os.environ.pop("PURGE_SWEEP_ENABLED", None)
            self.assertTrue(purge_scheduler.scheduler_enabled())
            self.run_lifespan()
        after = self.rows()
        self.assertEqual(after[ORPHAN_INPROCESS]["status"], "ERROR")
        self.assertEqual(after[ORPHAN_INPROCESS]["reason"], "runner_restarted")
        self.assertEqual(after[ORPHAN_NO_ARN]["status"], "ERROR")
        self.assertEqual(after[ORPHAN_PENDING]["status"], "ERROR")
        self.assertEqual(after[ORPHAN_PENDING]["reason"], "runner_restarted")
        for review_id in UNTOUCHED_IDS:
            self.assertEqual(after[review_id], self.before_snapshot[review_id])

    def test_recovery_does_not_run_on_the_aws_target(self) -> None:
        with patch.dict(os.environ, {"DEPLOY_TARGET": "aws"}, clear=False):
            self.assertFalse(purge_scheduler.scheduler_enabled())
            self.run_lifespan()
        self.assertEqual(self.rows(), self.before_snapshot)

    def test_recovery_still_runs_when_the_purge_cadence_is_switched_off(self) -> None:
        """`PURGE_SWEEP_ENABLED=0` turns the retention cadence off. It is not
        a request to leave orphaned reviews RUNNING forever, which is why the
        target test is `deploy_target`, not the whole of
        `scheduler_enabled()`."""
        with patch.dict(
            os.environ, {"DEPLOY_TARGET": "dts", "PURGE_SWEEP_ENABLED": "0"}, clear=False
        ):
            self.assertFalse(purge_scheduler.scheduler_enabled())
            self.run_lifespan()
        self.assertEqual(self.rows()[ORPHAN_INPROCESS]["status"], "ERROR")

    def test_a_failing_recovery_does_not_stop_the_api_booting(self) -> None:
        with patch.object(
            backend_main.runner_recovery,
            "recover_orphaned_reviews",
            side_effect=RuntimeError("dynamodb is unreachable"),
        ):
            with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}, clear=False):
                self.run_lifespan()  # must not raise


class TestLifespanShutdown(LifespanFixture):
    """The shutdown half, isolated from the boot half.

    Boot recovery is patched out here on purpose. It is asserted in
    `TestLifespanRecovery` above, and leaving it live would make these tests
    race the clock: the seeded rows are written seconds before the lifespan
    computes its own `process_started_at`, so the very review being held
    in flight is (correctly) recovered on the way IN, and the cancel intent
    on the way out is then refused by its own conditional write. That is the
    right behaviour, and it is not what these three tests are measuring.
    """

    def setUp(self) -> None:
        super().setUp()
        recovery = patch.object(
            backend_main.runner_recovery, "recover_orphaned_reviews", return_value=[]
        )
        recovery.start()
        self.addCleanup(recovery.stop)

    def _arm_singleton(self, review_id: str) -> RecordingPool:
        """A real in-process client holding one queued review, installed as
        the process singleton exactly as `get_inprocess_sfn_client` would."""
        pool = RecordingPool()
        client = pipeline_runner.InProcessStepFunctionsClient(
            runner=lambda _rid, _payload: None, pool=pool
        )
        client.start_execution(
            stateMachineArn="sm",
            name=f"exec-{review_id}",
            input=json.dumps({"review_id": review_id}),
        )
        pipeline_runner._SINGLETON = client
        self.assertEqual(pipeline_runner.in_flight_review_ids(), [review_id])
        return pool

    def test_the_finally_shuts_the_pool_down_without_waiting(self) -> None:
        pool = self._arm_singleton(LIVE_THIS_PROCESS)
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}, clear=False):
            self.run_lifespan()
        self.assertEqual(pool.shutdown_calls, [{"wait": False, "cancel_futures": True}])

    def test_the_finally_stamps_a_cancel_intent_on_every_in_flight_review(self) -> None:
        self._arm_singleton(LIVE_THIS_PROCESS)
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}, clear=False):
            self.run_lifespan()
        row = self.reviews_table.get_item(Key={"review_id": LIVE_THIS_PROCESS})["Item"]
        self.assertTrue(str(row["cancel_requested_at"]).isdigit())
        self.assertTrue(reviews.cancel_requested(LIVE_THIS_PROCESS, self.ddb))

    def test_a_terminal_review_is_not_stamped(self) -> None:
        """`record_cancel_intent`'s conditional write is what keeps a
        finished review out of this: the pool can still be holding the id of
        a run whose terminal already landed."""
        self._arm_singleton(ALREADY_DONE)
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}, clear=False):
            self.run_lifespan()
        row = self.reviews_table.get_item(Key={"review_id": ALREADY_DONE})["Item"]
        self.assertNotIn("cancel_requested_at", row)
        self.assertEqual(row["status"], "DONE")

    def test_shutdown_is_a_no_op_when_no_review_ever_ran(self) -> None:
        """The AWS target never builds the singleton; asking must not build
        one seconds before the process exits."""
        pipeline_runner._SINGLETON = None
        with patch.dict(os.environ, {"DEPLOY_TARGET": "aws"}, clear=False):
            self.run_lifespan()
        self.assertIsNone(pipeline_runner._SINGLETON)
        self.assertEqual(pipeline_runner.in_flight_review_ids(), [])


# ---------------------------------------------------------------------------
# 6. In-flight bookkeeping
# ---------------------------------------------------------------------------


class TestInFlightTracking(unittest.TestCase):
    def tearDown(self) -> None:
        pipeline_runner._SINGLETON = None

    def test_a_finished_review_leaves_the_in_flight_set(self) -> None:
        """The synchronous pool runs the body inline, so by the time
        `start_execution` returns the review is done — and must not still be
        claimed as in flight, or shutdown would stamp a cancel intent on
        reviews that finished hours ago."""

        class SyncPool:
            def submit(self, fn, *args):
                fn(*args)

        client = pipeline_runner.InProcessStepFunctionsClient(
            runner=lambda _rid, _payload: None, pool=SyncPool()
        )
        client.start_execution(
            stateMachineArn="sm", name="exec-1", input='{"review_id": "r1"}'
        )
        self.assertEqual(client.in_flight_review_ids(), [])

    def test_a_review_whose_body_raised_leaves_the_in_flight_set(self) -> None:
        class SyncPool:
            def submit(self, fn, *args):
                try:
                    fn(*args)
                except RuntimeError:
                    pass

        def boom(_rid: str, _payload: dict) -> None:
            raise RuntimeError("the pipeline blew up")

        client = pipeline_runner.InProcessStepFunctionsClient(
            runner=boom, pool=SyncPool()
        )
        client.start_execution(
            stateMachineArn="sm", name="exec-1", input='{"review_id": "r1"}'
        )
        self.assertEqual(client.in_flight_review_ids(), [])

    def test_a_queued_review_is_reported_in_flight(self) -> None:
        pool = RecordingPool()
        client = pipeline_runner.InProcessStepFunctionsClient(
            runner=lambda _rid, _payload: None, pool=pool
        )
        for index, review_id in enumerate(("r-b", "r-a")):
            client.start_execution(
                stateMachineArn="sm",
                name=f"exec-{index}",
                input=json.dumps({"review_id": review_id}),
            )
        self.assertEqual(client.in_flight_review_ids(), ["r-a", "r-b"])

    def test_the_module_accessor_builds_no_pool(self) -> None:
        pipeline_runner._SINGLETON = None
        self.assertEqual(pipeline_runner.in_flight_review_ids(), [])
        pipeline_runner.shutdown_inprocess_runner()
        self.assertIsNone(pipeline_runner._SINGLETON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
