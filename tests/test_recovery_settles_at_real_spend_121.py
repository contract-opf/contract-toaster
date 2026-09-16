#!/usr/bin/env python3
"""
Issue #121: `runner_restarted` and cancel settlement must not credit the
day's cap at 0 actual cents when passes were actually billed.

## What was broken

`reviews.settle_reservation_for_cancel` (called by both
`runner_recovery.recover_orphaned_reviews` -- issue #62, every review a
container restart strands -- and the cancel route's real `StopExecution`
path) always settled the day's-cap reservation at a hardcoded 0 actual
cents. 0 there does not mean "nothing was spent" -- it means "real spend,
unknown to this caller": the process that ran the review is gone (a
restart) or was just stopped (a cancel), so there is no live
`OpenRouterModelClient.cumulative_usage` to read the way the in-process
success/failure paths do (`pipeline_runner.py`'s `_actual_cents_from_client`
call sites). But there IS a durable, independent record of what was
billed -- the #414 model-invocation ledger (`MODEL_INVOCATIONS_TABLE`),
written in each attempt's own `finally` path as it happens, before the
pipeline ever reaches its own terminal write. Settling at a flat 0
regardless credited the WHOLE worst-case reservation back for a review that
may have completed a full primary pass (or more) before the restart,
quietly starving the daily spend cap of real spend.

## What this test proves

`backend/src/reviews.py::_actual_cents_from_ledger` (new, issue #121) reads
that ledger for one review, sums its REAL provider-reported usage
(`actual_input_tokens` / `actual_output_tokens`, never the *_est fields --
see `model_client.ModelInvocationRecord`), and prices it through the same
`compute_actual_usd_cents_from_usage` the in-process settlement paths use.
`settle_reservation_for_cancel` now threads that figure into `settle_spend`
instead of a hardcoded 0.

This file seeds TWO priced ledger attempts for one orphaned review through
the REAL production writer (`invocation_ledger.make_ledger_write` +
`model_client.ModelInvocationRecord`, the exact objects
`scripts/primary_review_pass.py::run_primary_pass` and
`scripts/critic_review_pass.py::run_critic_pass` build and hand to
`ledger_write` in their own `finally` blocks -- see those modules and
`backend/src/invocation_ledger.py`): one `pass_name="primary"` row and one
`pass_name="critic"` row.

BOTH PASS NAMES, because `_actual_cents_from_ledger` BRANCHES on that exact
field and routes the two into SEPARATE rate slots, which
`reviews._active_provider_rates` resolves independently and at genuinely
different rates (5.50/27.50 against 3.30/16.50 per million on the Bedrock
constants this suite runs at; the OpenRouter path resolves the two roles
independently too). A restart that strands a review after its critic pass
is precisely the scenario this ticket exists for, and a fixture holding
only the primary variant would leave the critic slot populated by no test
in the repo -- an inverted, swapped, summed-into-one-slot or dropped bucket
assignment would change every real settled figure and stay green forever.
The two attempts therefore carry deliberately ASYMMETRIC token counts, so
each of those mistakes lands on a different figure than the correct one
(see `test_the_bucket_split_is_what_is_being_asserted`, which pins that).

It then runs `runner_recovery.recover_orphaned_reviews` end to end and
asserts:

  1. `reviews.settle_spend` is called with `actual_usd_cents > 0` for the
     review that ledgered priced attempts (the ticket's own required
     verification, spied the same way `test_runner_recovery.py`'s own
     `TestReservationSettlement` spies on
     `settle_reservation_for_cancel`).
  2. `DAILY_SPEND_TABLE`'s real `settled_usd_cents` counter equals
     `compute_actual_usd_cents_from_usage` priced with BOTH slots populated
     separately -- not just a spy call, and not one summed slot.

FIXTURE FIDELITY: every reviews row, submission row, and spend reservation
is seeded by `test_runner_recovery.RecoveryFixture`'s real production
writers (`reviews._create_review_row`, `reviews.create_submission_record`,
`reviews.reserve_spend`, `pipeline_runner._mark_running`, ...); this file
adds exactly one new fixture shape, a ledger row, built through
`invocation_ledger.make_ledger_write` -- the same sink `run_real_pipeline`
wires into `review_spine.run_review` on every real review -- so the rows
this test seeds are exactly the shape production writes, not hand-typed
dicts.

Run standalone: `python3 tests/test_recovery_settles_at_real_spend_121.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MODEL_INVOCATIONS_TABLE = "contract-toaster-model-invocations-recovery121-test"
os.environ.setdefault("MODEL_INVOCATIONS_TABLE", MODEL_INVOCATIONS_TABLE)

# The two ledgered attempts' REAL provider-reported usage. Deliberately
# asymmetric BETWEEN the passes and between input and output: with these
# four numbers the correct figure, the bucket-swapped figure, the
# both-summed-into-one-slot figure and the critic-dropped figure are all
# different, so `test_daily_spend_table_settles_the_real_amount`'s equality
# assertion actually discriminates the pass_name split it is there to pin.
PRIMARY_ACTUAL_INPUT_TOKENS = 100_000
PRIMARY_ACTUAL_OUTPUT_TOKENS = 20_000
CRITIC_ACTUAL_INPUT_TOKENS = 60_000
CRITIC_ACTUAL_OUTPUT_TOKENS = 8_000

import src.invocation_ledger as invocation_ledger  # noqa: E402
import src.model_client as model_client  # noqa: E402
import src.reviews as reviews  # noqa: E402
import src.runner_recovery as runner_recovery  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_model_invocation_ledger.py's own import of
# test_dts_pipeline_runner_real_review): reuse the real, production-writer
# seeded orphan fixture from #62 rather than duplicating it.
import test_runner_recovery as recovery62  # noqa: E402


class TestRecoverySettlesAtRealSpend(recovery62.RecoveryFixture):
    """Two priced ledger attempts -- one `primary`, one `critic` -- for
    `ORPHAN_INPROCESS`, the orphan shape that actually started and so is the
    only one capable of having spent money (see `test_runner_recovery.py`'s
    own selector-correction docstring), on top of the unmodified #62
    six-review world."""

    def setUp(self) -> None:
        super().setUp()
        self.ddb.create_table(
            TableName=MODEL_INVOCATIONS_TABLE,
            KeySchema=[
                {"AttributeName": "review_id", "KeyType": "HASH"},
                {"AttributeName": "record_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "review_id", "AttributeType": "S"},
                {"AttributeName": "record_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.process_started_at = self.seed_world()
        self.spend_table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])

        # THE PRODUCTION WRITER: the same call
        # `pipeline_runner.run_real_pipeline` makes for `review_spine.
        # run_review`, building the same `ModelInvocationRecord`
        # `run_primary_pass`'s and `run_critic_pass`'s own `finally` paths
        # build on a real success. Both attempts go through this one writer,
        # under the one review_id partition, exactly as a real review that
        # got through its primary AND critic pass before the restart leaves
        # them.
        ledger_write = invocation_ledger.make_ledger_write(
            recovery62.ORPHAN_INPROCESS, self.ddb
        )
        ledger_write(
            model_client.ModelInvocationRecord(
                review_id=recovery62.ORPHAN_INPROCESS,
                pass_name="primary",
                model_id="synthetic-primary",
                attempt_number=1,
                outcome="success",
                input_tokens_est=1_000,
                output_tokens_est=200,
                actual_input_tokens=PRIMARY_ACTUAL_INPUT_TOKENS,
                actual_output_tokens=PRIMARY_ACTUAL_OUTPUT_TOKENS,
            )
        )
        ledger_write(
            model_client.ModelInvocationRecord(
                review_id=recovery62.ORPHAN_INPROCESS,
                pass_name="critic",
                model_id="synthetic-critic",
                attempt_number=1,
                outcome="success",
                input_tokens_est=900,
                output_tokens_est=150,
                actual_input_tokens=CRITIC_ACTUAL_INPUT_TOKENS,
                actual_output_tokens=CRITIC_ACTUAL_OUTPUT_TOKENS,
            )
        )

    def _expected_settled_cents(self) -> int:
        """What the two ledgered attempts really cost, priced the way every
        other settlement in this codebase prices usage -- with the primary
        and critic slots populated SEPARATELY, so the rate split
        `_actual_cents_from_ledger` performs is the thing being asserted."""
        return reviews.compute_actual_usd_cents_from_usage(
            {
                "input_tokens": PRIMARY_ACTUAL_INPUT_TOKENS,
                "output_tokens": PRIMARY_ACTUAL_OUTPUT_TOKENS,
            },
            {
                "input_tokens": CRITIC_ACTUAL_INPUT_TOKENS,
                "output_tokens": CRITIC_ACTUAL_OUTPUT_TOKENS,
            },
            rates=reviews._active_provider_rates(self.ddb),
        )

    def _spend_date(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time()))

    def test_settle_spend_receives_real_actual_cents(self) -> None:
        """The ticket's own required verification: `settle_spend` must be
        called with `actual_cents > 0` for the review that ledgered priced
        attempts. Fails on the untouched tree -- every call there receives
        the hardcoded 0."""
        seen_actual_cents: list[int] = []
        real_settle_spend = reviews.settle_spend

        def spy_settle_spend(
            review_id: str,
            reservation_id: str,
            actual_usd_cents: int,
            dynamodb_resource: Any,
            now_epoch: float | None = None,
        ) -> None:
            seen_actual_cents.append(actual_usd_cents)
            real_settle_spend(
                review_id,
                reservation_id,
                actual_usd_cents,
                dynamodb_resource,
                now_epoch=now_epoch,
            )

        with patch.object(reviews, "settle_spend", spy_settle_spend):
            runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )

        self.assertTrue(seen_actual_cents, "settle_spend was never called")
        self.assertGreater(
            max(seen_actual_cents),
            0,
            "every settle_spend call received actual_cents == 0 -- the "
            "ledgered attempt was not priced",
        )

    def test_daily_spend_table_settles_the_real_amount(self) -> None:
        """Not just a spy call -- the REAL `DAILY_SPEND_TABLE` state must
        reflect what was actually billed, priced the same way
        `compute_actual_usd_cents_from_usage` prices every other settlement
        in this codebase: the primary attempt at the PRIMARY rate slot and
        the critic attempt at the CRITIC one. Mis-bucketing either -- swapped,
        summed into a single slot, or dropped -- misses this figure."""
        expected_cents = self._expected_settled_cents()
        self.assertGreater(expected_cents, 0, "fixture rates price these attempts at $0")

        runner_recovery.recover_orphaned_reviews(
            self.ddb, process_started_at=self.process_started_at
        )

        item = self.spend_table.get_item(Key={"spend_date": self._spend_date()})["Item"]
        self.assertEqual(int(item.get("settled_usd_cents", 0)), expected_cents)

    def test_the_bucket_split_is_what_is_being_asserted(self) -> None:
        """The equality above only pins the pass_name split if the WRONG
        bucket assignments price differently from the right one. They do, on
        this fixture's rates and these deliberately asymmetric token counts:
        swapping the two slots, summing both passes into the primary slot,
        and dropping the critic row entirely each land on a different figure.
        Without this, a future rate table that happened to price primary and
        critic identically would silently turn the assertion above back into
        the single-slot test it replaced."""
        rates = reviews._active_provider_rates(self.ddb)
        primary = {
            "input_tokens": PRIMARY_ACTUAL_INPUT_TOKENS,
            "output_tokens": PRIMARY_ACTUAL_OUTPUT_TOKENS,
        }
        critic = {
            "input_tokens": CRITIC_ACTUAL_INPUT_TOKENS,
            "output_tokens": CRITIC_ACTUAL_OUTPUT_TOKENS,
        }
        correct = self._expected_settled_cents()

        swapped = reviews.compute_actual_usd_cents_from_usage(critic, primary, rates=rates)
        summed = reviews.compute_actual_usd_cents_from_usage(
            {
                "input_tokens": primary["input_tokens"] + critic["input_tokens"],
                "output_tokens": primary["output_tokens"] + critic["output_tokens"],
            },
            None,
            rates=rates,
        )
        critic_dropped = reviews.compute_actual_usd_cents_from_usage(
            primary, None, rates=rates
        )

        self.assertNotEqual(correct, swapped, "swapping the rate slots prices identically")
        self.assertNotEqual(correct, summed, "one summed slot prices identically")
        self.assertNotEqual(
            correct, critic_dropped, "dropping the critic row prices identically"
        )

    def test_untouched_orphans_still_settle_at_zero(self) -> None:
        """The other two orphans (`ORPHAN_NO_ARN`, `ORPHAN_PENDING`)
        ledgered nothing -- they must keep settling at 0, exactly as the
        whole backlog did before issue #121. This covers the ROWS-vs-NO-ROWS
        branch only (an all-priced world would leave a regression that always
        prices SOMETHING, even garbage, green); the pass_name branch the
        pricing itself takes is covered by the two-slot fixture above."""
        seen: dict[str, int] = {}
        real_settle_spend = reviews.settle_spend

        def spy_settle_spend(
            review_id: str,
            reservation_id: str,
            actual_usd_cents: int,
            dynamodb_resource: Any,
            now_epoch: float | None = None,
        ) -> None:
            seen[review_id] = actual_usd_cents
            real_settle_spend(
                review_id,
                reservation_id,
                actual_usd_cents,
                dynamodb_resource,
                now_epoch=now_epoch,
            )

        # Patched onto reviews_module, matching runner_recovery.py's own
        # `reviews_module.settle_reservation_for_cancel` call path; the
        # bare `settle_spend(...)` call inside `settle_reservation_for_cancel`
        # resolves through the SAME module attribute, so this spy sees every
        # call regardless of which caller reaches it.
        with patch.object(reviews, "settle_spend", spy_settle_spend):
            recovered = runner_recovery.recover_orphaned_reviews(
                self.ddb, process_started_at=self.process_started_at
            )

        self.assertEqual(sorted(seen), sorted(recovered))
        self.assertEqual(seen[recovery62.ORPHAN_NO_ARN], 0)
        self.assertEqual(seen[recovery62.ORPHAN_PENDING], 0)
        self.assertGreater(seen[recovery62.ORPHAN_INPROCESS], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
