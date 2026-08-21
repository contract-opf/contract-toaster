#!/usr/bin/env python3
"""
CI eval spend budget plumbing -- originally issue #62, checks 6-8.

Issue #400 retired scripts/eval_harness.py's detector-scoring API (moved to
the LLM-native review path) and deleted tests/test_eval_harness_62.py
wholesale along with it. scripts/eval_budget.py itself is untouched by that
rewrite -- it enforces the documented $200/run and $1,000/month CI eval
ceilings (docs/evaluation.md -> "CI eval budget, gate tiers, and gold-set
growth policy") and fails loudly (raises) rather than truncating coverage,
regardless of which pipeline (detector-only or LLM-native) the spend was
for. These three checks are ported here, unchanged, so eval_budget.py does
not lose its only test coverage.

Run with: python3 tests/test_eval_budget.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import eval_budget  # noqa: E402


# ── CI eval budget plumbing fails loudly over the documented caps ───────────

def check_budget_fails_loudly_over_per_run_cap() -> list[str]:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        try:
            eval_budget.reserve_ci_eval_spend(
                run_cost_usd=eval_budget.CI_EVAL_PER_RUN_CAP_USD + 1.0,
                ledger_path=ledger_path,
            )
            failures.append(
                "  reserve_ci_eval_spend() did not raise when run_cost_usd exceeded "
                "the documented per-run cap — the harness must fail loudly, not "
                "silently truncate coverage."
            )
        except eval_budget.BudgetExceededError:
            pass  # expected
    return failures


def check_budget_fails_loudly_over_monthly_cap() -> list[str]:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        now = 1_700_000_000.0  # fixed epoch so both reservations land in the same month
        try:
            # First reservation consumes most of the monthly cap...
            eval_budget.reserve_ci_eval_spend(
                run_cost_usd=eval_budget.CI_EVAL_MONTHLY_CAP_USD - 10.0,
                ledger_path=ledger_path,
                now_epoch=now,
            )
            # ...second reservation should push it over the monthly cap and raise.
            eval_budget.reserve_ci_eval_spend(
                run_cost_usd=20.0,
                ledger_path=ledger_path,
                now_epoch=now,
            )
            failures.append(
                "  reserve_ci_eval_spend() did not raise when cumulative monthly "
                "spend would exceed the documented monthly cap."
            )
        except eval_budget.BudgetExceededError:
            pass  # expected
    return failures


def check_budget_succeeds_under_cap_and_is_atomic() -> list[str]:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "ledger.json"
        now = 1_700_000_000.0
        try:
            result = eval_budget.reserve_ci_eval_spend(
                run_cost_usd=10.0, ledger_path=ledger_path, now_epoch=now
            )
        except eval_budget.BudgetExceededError as exc:
            failures.append(f"  reserve_ci_eval_spend() unexpectedly raised under cap: {exc}")
            return failures

        if result.total_after_usd != 10.0:
            failures.append(f"  expected total_after_usd == 10.0, got {result.total_after_usd}")

        # A second reservation must accumulate, not overwrite.
        result2 = eval_budget.reserve_ci_eval_spend(
            run_cost_usd=5.0, ledger_path=ledger_path, now_epoch=now
        )
        if result2.total_after_usd != 15.0:
            failures.append(f"  expected cumulative total_after_usd == 15.0, got {result2.total_after_usd}")

        if ledger_path.with_suffix(".json.lock").exists():
            failures.append("  lockfile was not released after reservation completed.")
    return failures


def main() -> int:
    checks = [
        ("1", "CI eval budget: fails loudly over the per-run cap", check_budget_fails_loudly_over_per_run_cap),
        ("2", "CI eval budget: fails loudly over the monthly cap", check_budget_fails_loudly_over_monthly_cap),
        ("3", "CI eval budget: succeeds and accumulates atomically under cap", check_budget_succeeds_under_cap_and_is_atomic),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All CI eval budget checks passed.")
        return 0
    else:
        print("One or more CI eval budget checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
