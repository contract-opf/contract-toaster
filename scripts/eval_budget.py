#!/usr/bin/env python3
"""
CI evaluation spend budget plumbing — issue #62 item 2.

Per docs/evaluation.md -> "CI eval budget, gate tiers, and gold-set growth
policy" and the #62 scope decision ("Budget plumbing: CI eval spend routed
through the existing reservation/ledger pattern (backend/src/reviews.py),
gold-set size x runs capped against a documented ceiling, fails loudly
rather than truncating coverage"):

This module is the CI-side analogue of backend/src/reviews.py's
reserve_spend(): a single atomic conditional increment against a
documented ceiling that fails closed (raises) rather than silently
truncating gold-set coverage. It intentionally mirrors that function's
shape (compute a reservation, atomically check-and-increment against a
cap, raise on overshoot) but targets a CI-scoped ledger instead of the
production DynamoDB daily_spend table, because:

  - CI eval spend is a SEPARATE ceiling from the production $20/day ledger
    (docs/evaluation.md -> "Separate CI eval budget") — it must not be
    routed through backend/src/reviews.py's reserve_spend() itself, which
    would silently consume production review budget for eval runs.
  - CI runners do not have production DynamoDB credentials (dev/prod
    account boundary; see ARCHITECTURE.md -> Environments), so the ledger
    here is a small JSON file (local disk in CI, or an S3/DynamoDB-backed
    path can be swapped in via CI_EVAL_LEDGER_PATH without changing the
    reservation logic).

Documented ceilings (docs/evaluation.md -> "Explicit CI eval budget caps"):
    Per-run (full stochastic gate):  $200/run
    Monthly aggregate:               $1,000/month

The ledger increments are ATOMIC via an exclusive-create lockfile
(os.O_CREAT | os.O_EXCL) around the read-check-write, so two concurrent CI
runners cannot both observe "under cap" and jointly overshoot it — the same
race the production reserve_spend() closes with a single conditional
DynamoDB UpdateExpression.

CLI usage:
    python3 scripts/eval_budget.py reserve --run-cost-usd 41.00
    python3 scripts/eval_budget.py reserve --gold-set-size 39 --stochastic-runs 5

Exit codes: 0 = reservation succeeded (within budget), 1 = would exceed
budget (fails loudly; does not truncate coverage).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Documented ceilings — must match docs/evaluation.md -> "Explicit CI eval
# budget caps" exactly. If you change either figure, update evaluation.md in
# the same commit (tests/test_eval_economics.py gates the doc; this module
# is the code-side enforcement of the same numbers).
# ---------------------------------------------------------------------------

CI_EVAL_PER_RUN_CAP_USD = 200.00
CI_EVAL_MONTHLY_CAP_USD = 1000.00

# Per-case-per-stochastic-run cost estimate used to size a *planned* run
# before it executes, so the harness can refuse BEFORE spending rather than
# after (docs/evaluation.md -> "fails loudly ... rather than silently
# truncating coverage"). Derived from the per-run cost derivation in
# evaluation.md: ~$205 uncached / 39 cases / 5 runs ~= $1.05/case-run;
# rounded up slightly for a conservative (fail-loud-early) estimate.
ESTIMATED_COST_PER_CASE_RUN_USD = 1.10

DEFAULT_LEDGER_PATH = REPO_ROOT / ".ci-eval-ledger.json"


class BudgetExceededError(RuntimeError):
    """Raised when a planned or actual CI eval spend would exceed the
    documented ceiling. The harness must let this propagate and fail the
    build — it must never catch this and silently drop gold cases to fit
    under budget."""


@dataclass
class ReservationResult:
    reservation_id: str
    amount_usd: float
    period: str  # "run" or "month"
    period_key: str
    total_after_usd: float
    cap_usd: float


def _ledger_path() -> Path:
    return Path(os.environ.get("CI_EVAL_LEDGER_PATH", str(DEFAULT_LEDGER_PATH)))


def _month_key(now_epoch: float | None = None) -> str:
    now_epoch = time.time() if now_epoch is None else now_epoch
    return time.strftime("%Y-%m", time.gmtime(now_epoch))


def _acquire_lock(lock_path: Path, timeout_s: float = 30.0) -> None:
    """Exclusive-create spin lock. Mirrors the atomicity guarantee of the
    production reserve_spend()'s single conditional DynamoDB update: only
    one caller can hold the lock while it reads-checks-writes the ledger,
    so two concurrent CI runners cannot both observe 'under cap' and
    jointly overshoot it."""
    deadline = time.time() + timeout_s
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            if time.time() > deadline:
                raise BudgetExceededError(
                    f"Could not acquire CI eval ledger lock at {lock_path} within "
                    f"{timeout_s}s; refusing to proceed rather than risk an "
                    f"unsynchronized double-reservation."
                )
            time.sleep(0.05)


def _release_lock(lock_path: Path) -> None:
    try:
        os.remove(str(lock_path))
    except FileNotFoundError:
        pass


def _read_ledger(path: Path) -> dict:
    if not path.exists():
        return {"months": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_ledger(path: Path, data: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def estimate_run_cost_usd(gold_set_size: int, stochastic_runs: int) -> float:
    """Estimate the dollar cost of a planned eval run BEFORE it executes,
    so reserve_ci_eval_spend() can fail loudly ahead of spending rather
    than truncating case coverage mid-run to stay under budget."""
    if gold_set_size < 0 or stochastic_runs < 0:
        raise ValueError("gold_set_size and stochastic_runs must be >= 0")
    return round(gold_set_size * stochastic_runs * ESTIMATED_COST_PER_CASE_RUN_USD, 2)


def reserve_ci_eval_spend(
    run_cost_usd: float,
    ledger_path: Path | None = None,
    now_epoch: float | None = None,
    per_run_cap_usd: float = CI_EVAL_PER_RUN_CAP_USD,
    monthly_cap_usd: float = CI_EVAL_MONTHLY_CAP_USD,
) -> ReservationResult:
    """Atomically reserve `run_cost_usd` against the CI eval ledger.

    Two gates, both fail-closed (raise BudgetExceededError), matching the
    #62 AC "fails loudly rather than truncating coverage":

      1. Per-run cap: a single run may not plan to spend more than
         per_run_cap_usd (default $200/run, docs/evaluation.md).
      2. Monthly cap: the reservation must not push the current calendar
         month's cumulative reserved spend over monthly_cap_usd (default
         $1,000/month, docs/evaluation.md).

    This function reserves BEFORE the run executes (worst-case, like
    backend/src/reviews.py's reserve_spend()) — it does not wait to see
    actual spend and then decide retroactively whether coverage should
    have been truncated. A caller that wants to run anyway after this
    raises is the caller explicitly overriding the documented ceiling
    (e.g. via a human-approved emergency cap bump), not this module
    silently allowing it.
    """
    if run_cost_usd < 0:
        raise ValueError("run_cost_usd must be >= 0")

    if run_cost_usd > per_run_cap_usd:
        raise BudgetExceededError(
            f"Planned CI eval run cost ${run_cost_usd:.2f} exceeds the documented "
            f"per-run cap of ${per_run_cap_usd:.2f} (docs/evaluation.md -> "
            f"'Explicit CI eval budget caps'). Refusing to run rather than "
            f"silently truncate gold-set coverage to fit under budget. "
            f"Reduce gold-set size / stochastic-run count, or raise the "
            f"documented cap via the joint Legal+Engineering process."
        )

    path = ledger_path or _ledger_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    month_key = _month_key(now_epoch)

    _acquire_lock(lock_path)
    try:
        ledger = _read_ledger(path)
        months = ledger.setdefault("months", {})
        month_entry = months.setdefault(month_key, {"reserved_usd": 0.0, "reservations": []})
        current_total = float(month_entry["reserved_usd"])
        prospective_total = round(current_total + run_cost_usd, 2)

        if prospective_total > monthly_cap_usd:
            raise BudgetExceededError(
                f"Reserving ${run_cost_usd:.2f} for this CI eval run would bring "
                f"{month_key} cumulative CI eval spend to ${prospective_total:.2f}, "
                f"exceeding the documented monthly cap of ${monthly_cap_usd:.2f} "
                f"(docs/evaluation.md -> 'Explicit CI eval budget caps'). Refusing "
                f"to run rather than silently truncate coverage. Wait for next "
                f"month's window, or raise the documented cap via the joint "
                f"Legal+Engineering process."
            )

        reservation_id = f"{month_key}-{len(month_entry['reservations']) + 1:04d}"
        month_entry["reserved_usd"] = prospective_total
        month_entry["reservations"].append(
            {
                "reservation_id": reservation_id,
                "amount_usd": run_cost_usd,
                "timestamp": now_epoch if now_epoch is not None else time.time(),
            }
        )
        _write_ledger(path, ledger)
    finally:
        _release_lock(lock_path)

    return ReservationResult(
        reservation_id=reservation_id,
        amount_usd=run_cost_usd,
        period="month",
        period_key=month_key,
        total_after_usd=prospective_total,
        cap_usd=monthly_cap_usd,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    reserve_p = sub.add_parser("reserve", help="Reserve CI eval spend for a planned run.")
    reserve_p.add_argument("--run-cost-usd", type=float, default=None)
    reserve_p.add_argument("--gold-set-size", type=int, default=None)
    reserve_p.add_argument("--stochastic-runs", type=int, default=None)

    args = parser.parse_args(argv)

    if args.command == "reserve":
        if args.run_cost_usd is not None:
            cost = args.run_cost_usd
        elif args.gold_set_size is not None and args.stochastic_runs is not None:
            cost = estimate_run_cost_usd(args.gold_set_size, args.stochastic_runs)
        else:
            print(
                "reserve requires either --run-cost-usd or both --gold-set-size "
                "and --stochastic-runs",
                file=sys.stderr,
            )
            return 2

        try:
            result = reserve_ci_eval_spend(cost)
        except BudgetExceededError as exc:
            print(f"CI EVAL BUDGET EXCEEDED — refusing to run.\n{exc}", file=sys.stderr)
            return 1

        print(
            f"Reserved ${result.amount_usd:.2f} (reservation {result.reservation_id}); "
            f"{result.period_key} total now ${result.total_after_usd:.2f} of "
            f"${result.cap_usd:.2f} cap."
        )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
