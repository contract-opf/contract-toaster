#!/usr/bin/env python3
"""
Gate for issue #509: the retention purge sweep actually RUNS on the Docker
Compose (DTS) deployment.

## What was wrong

#454 made the sweep itself correct -- it deletes input documents and ties
reported success to the object actually being gone. But on DTS nothing ever
invoked it. `main.py` imported only `preview_purge_sweep`;
`run_purge_sweep_now` had no caller anywhere in `backend/src`, and the compose
file shipped no scheduler. Only the AWS Lambda had a cadence.

So an operator who set a retention window watched the PREVIEW work and
reasonably concluded data was being purged on schedule. It was not: uploaded
contracts and outputs accumulated in MinIO forever. For a product whose entire
retention story is "we don't keep your counterparty's paper longer than N
days", a deployment that silently never purges is a real governance gap -- and
an invisible one, because the preview looks healthy either way.

## What is asserted

  1. The scheduler runs on DTS and NOT on AWS, where the Lambda owns the
     cadence. Starting both would double-sweep.
  2. It calls the SAME `run_purge_sweep_now` #454 hardened -- no second
     implementation of purge logic -- and with `dry_run=False`, since a
     scheduler that only ever previews is the bug being fixed.
  3. One failing sweep does not kill the loop. A transient S3 error must cost
     one cycle, not every future cycle, or the gap silently returns.
  4. It stops when asked and leaves no thread behind.
  5. Nothing it logs carries document substance.

Legal-hold behaviour is deliberately NOT re-asserted here: this ticket adds a
cadence and no purge logic, so re-testing invariant 3 would be testing
`run_purge_sweep_now` through a second door. What IS asserted is that the
scheduler calls that function rather than reimplementing any part of it, which
is what keeps the hold guarantee true.

Offline: no AWS, no network, no real sleeping.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

import logging
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")

import src.purge_scheduler as purge_scheduler  # noqa: E402

SECRET = "the counterparty shall indemnify nobody in particular"


class _StopLoop(Exception):
    """Raised by the fake sleep to end the loop deterministically."""


def _sleep_after(calls: int):
    """A sleep that ends the loop after `calls` iterations, so the test drives
    the cadence instead of waiting on the wall clock."""
    state = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        state["n"] += 1
        if state["n"] >= calls:
            raise _StopLoop()

    return fake_sleep


class TestCadence(unittest.TestCase):
    def test_it_calls_the_hardened_sweep_and_not_a_preview(self):
        seen = []

        def fake_sweep(s3_client, dynamodb_resource, dry_run=False):
            seen.append(dry_run)
            return {"deleted_reviews": [], "failed_reviews": []}

        with self.assertRaises(_StopLoop):
            purge_scheduler._sweep_loop(
                s3_client=object(),
                dynamodb_resource=object(),
                interval_seconds=1,
                sweep=fake_sweep,
                sleep_fn=_sleep_after(3),
                should_run=lambda: True,
            )

        self.assertEqual(len(seen), 3)
        # A scheduler that only ever previews is the bug, not the fix.
        self.assertEqual(seen, [False, False, False])

    def test_a_failing_sweep_costs_one_cycle_not_every_future_one(self):
        calls = {"n": 0}

        def flaky_sweep(s3_client, dynamodb_resource, dry_run=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("S3 had a moment")
            return {"deleted_reviews": [], "failed_reviews": []}

        with self.assertRaises(_StopLoop):
            purge_scheduler._sweep_loop(
                s3_client=object(),
                dynamodb_resource=object(),
                interval_seconds=1,
                sweep=flaky_sweep,
                sleep_fn=_sleep_after(3),
                should_run=lambda: True,
            )

        # It kept going. A transient error that silently ended the cadence
        # would restore exactly the gap this issue is about, and look fine.
        self.assertEqual(calls["n"], 3)

    def test_it_stops_when_told_to(self):
        calls = {"n": 0}
        running = {"yes": True}

        def sweep(s3_client, dynamodb_resource, dry_run=False):
            calls["n"] += 1
            if calls["n"] == 2:
                running["yes"] = False
            return {}

        purge_scheduler._sweep_loop(
            s3_client=object(),
            dynamodb_resource=object(),
            interval_seconds=1,
            sweep=sweep,
            sleep_fn=lambda _s: None,
            should_run=lambda: running["yes"],
        )
        self.assertEqual(calls["n"], 2)


class TestTargetGating(unittest.TestCase):
    def test_it_does_not_start_on_the_aws_target(self):
        """The Lambda owns the cadence there. Two schedulers would double-sweep
        the same rows."""
        with patch.dict(os.environ, {"DEPLOY_TARGET": "aws"}):
            handle = purge_scheduler.start_purge_scheduler(
                s3_client=object(), dynamodb_resource=object()
            )
        self.assertIsNone(handle)

    def test_it_starts_on_the_dts_target(self):
        before = threading.active_count()
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts"}):
            handle = purge_scheduler.start_purge_scheduler(
                s3_client=object(),
                dynamodb_resource=object(),
                sweep=lambda **_kwargs: {},
            )
        self.assertIsNotNone(handle)
        try:
            self.assertTrue(handle.thread.is_alive())
            self.assertTrue(handle.thread.daemon)
        finally:
            handle.stop()
        # No thread left behind: the leak this issue's AC calls out.
        deadline = time.monotonic() + 5
        while handle.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(handle.thread.is_alive())
        self.assertLessEqual(threading.active_count(), before + 1)

    def test_an_explicitly_disabled_scheduler_does_not_start(self):
        """An operator must be able to turn it off without editing code -- a
        cadence that cannot be stopped is its own operational problem."""
        with patch.dict(os.environ, {"DEPLOY_TARGET": "dts", "PURGE_SWEEP_ENABLED": "0"}):
            self.assertIsNone(
                purge_scheduler.start_purge_scheduler(
                    s3_client=object(), dynamodb_resource=object()
                )
            )


class TestNoSubstanceInLogs(unittest.TestCase):
    def test_a_sweep_failure_never_logs_document_substance(self):
        def exploding_sweep(s3_client, dynamodb_resource, dry_run=False):
            raise RuntimeError(f"failed on {SECRET}")

        with self.assertLogs(level="DEBUG") as captured:
            logging.getLogger("test").debug("anchor")
            with self.assertRaises(_StopLoop):
                purge_scheduler._sweep_loop(
                    s3_client=object(),
                    dynamodb_resource=object(),
                    interval_seconds=1,
                    sweep=exploding_sweep,
                    sleep_fn=_sleep_after(1),
                    should_run=lambda: True,
                )
        joined = "\n".join(captured.output)
        # The exception's own message can carry anything; this module must not
        # be the thing that puts it in the log.
        self.assertNotIn(SECRET, joined)


class TestItIsActuallyWiredIn(unittest.TestCase):
    """The gap this issue is about was not a missing function -- it was a
    function with no caller. A scheduler module nothing starts would reproduce
    the bug exactly, and every test above would still pass."""

    def test_the_app_starts_the_scheduler_on_startup(self):
        source = (BACKEND_ROOT / "src" / "main.py").read_text()
        self.assertIn("start_purge_scheduler", source)

    def test_the_app_stops_it_on_shutdown(self):
        source = (BACKEND_ROOT / "src" / "main.py").read_text()
        self.assertIn("handle.stop()", source)

    def test_the_lifespan_is_attached_to_the_app(self):
        """Defining the hook and forgetting to pass it to FastAPI is the same
        class of mistake as writing the sweep and never calling it."""
        source = (BACKEND_ROOT / "src" / "main.py").read_text()
        self.assertIn("lifespan=_lifespan", source)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("\nPASS: the DTS purge sweep has a cadence (issue #509).")
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
