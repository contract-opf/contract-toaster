#!/usr/bin/env python3
"""
Red gate for issue #62: Evaluation harness + gold test set (model-free
harness skeleton, per the issue's "Reconciliation with the 2026-06-11
architecture review" scope decision).

The scope decision on #62 narrows the AFK-gradable slice to six items
buildable BEFORE the LLM pipeline stages (#80-#83) exist:

  1. Model-free harness skeleton: a runner that submits a gold case to the
     (detector-only, model-free) pipeline and a comparator/scorer that
     produces pass/fail + a report.
  2. CI eval spend budget plumbing: an atomic reservation against a
     documented ceiling that fails loudly rather than truncating coverage.
  3. D1/D2/D3 detector-correctness gates (buildable now against
     tests/detector/ + .github/workflows/detector-correctness.yml).
  4. Redline-patch fixture checks using scripts/redline_patch.py against
     synthetic fixtures (covered by the existing redline test suite; this
     file does not duplicate it).
  5. A mechanically-derived subset of the gold set: one synthetic planted-
     violation case per existing hard_rejection rule in
     playbooks/eiaa-v1.0.0.json.
  6. The harness runs against synthetic documents only, wired into CI.

This test asserts items 1, 2, 5, and 6 (the net-new pieces added by #62):
scripts/eval_harness.py exists and correctly scores the gold fixture set;
every hard_rejection rule has at least one gold-case detector fixture
(topic-coverage gate, docs/evaluation.md -> "Regression gates" #2, restated
here for the detector-only subset); scripts/eval_budget.py enforces the
documented $200/run and $1,000/month CI eval ceilings and fails loudly
(raises) rather than truncating; and every generated fixture is tagged
"provenance": "synthetic" (item 6 — no production documents).

Run with: python3 tests/test_eval_harness_62.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import eval_budget  # noqa: E402
import eval_harness  # noqa: E402

FIXTURES_PATH = REPO_ROOT / "tests" / "gold-fixtures"
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"


# ── Check 1: harness module has the runner + comparator/scorer API ──────────

def check_harness_api_present() -> list[str]:
    failures = []
    required = ["load_gold_cases", "run_detectors_on_case", "score_case", "score_all", "main"]
    for name in required:
        if not hasattr(eval_harness, name):
            failures.append(f"  scripts/eval_harness.py is missing required API: {name}()")
    return failures


# ── Check 2: harness scores every gold fixture and every one PASSes ─────────

def check_harness_scores_all_fixtures_pass() -> list[str]:
    failures = []
    results = eval_harness.score_all()
    if not results:
        failures.append("  scripts/eval_harness.py score_all() returned zero results — expected gold fixtures to be scored.")
        return failures
    failed_cases = [r for r in results if not r.passed]
    for r in failed_cases:
        failures.append(f"  gold case {r.case_id!r} scored FAIL: {r.reasons}")
    return failures


# ── Check 3: every hard_rejection rule has >=1 gold-case detector fixture ───

def check_every_hard_rejection_rule_has_gold_coverage() -> list[str]:
    failures = []
    missing = eval_harness.missing_rule_coverage()
    if missing:
        failures.append(
            f"  {len(missing)} hard_rejection rule(s) have no gold-case detector "
            f"fixture: {missing}. Every rule must have at least one planted-violation "
            f"gold case (docs/evaluation.md topic-coverage gate; #62 item 5)."
        )
    return failures


# ── Check 4: detector runner handles both on_insert and on_remove_or_alter ──

def check_runner_handles_on_remove_or_alter() -> list[str]:
    """The pre-#62 lint (tests/lint-gold-fixtures.py) and the pre-#62 harness
    only simulated on_insert rules. #62 must extend detector simulation to
    on_remove_or_alter (protects.required_tokens) since 6 of the 15
    hard_rejection rules are on_remove_or_alter and previously had zero gold
    coverage."""
    failures = []
    playbook = eval_harness.load_playbook(PLAYBOOK_PATH)
    remove_or_alter_rules = [r for r in playbook["hard_rejections"] if r["kind"] == "on_remove_or_alter"]
    if not remove_or_alter_rules:
        failures.append("  expected at least one on_remove_or_alter hard_rejection rule in the playbook to test against.")
        return failures

    rule = remove_or_alter_rules[0]
    required_tokens = rule["protects"]["required_tokens"]
    topic_id = rule["applies_to_topics"][0]

    # A hunk that omits every required token must fire.
    fires = eval_harness.run_on_remove_or_alter_rule(rule, "innocuous replacement text", topic_id)
    if not fires:
        failures.append(
            f"  run_on_remove_or_alter_rule() did not fire for {rule['id']!r} when "
            f"required_tokens {required_tokens!r} were entirely absent from the hunk."
        )

    # A hunk that retains every required token must NOT fire.
    retained_hunk = "This clause retains: " + "; ".join(required_tokens)
    no_fires = eval_harness.run_on_remove_or_alter_rule(rule, retained_hunk, topic_id)
    if no_fires:
        failures.append(
            f"  run_on_remove_or_alter_rule() incorrectly fired for {rule['id']!r} "
            f"when all required_tokens were retained verbatim: {no_fires}"
        )
    return failures


# ── Check 5: all #62-generated fixtures are provenance: synthetic ───────────

def check_generated_fixtures_are_synthetic() -> list[str]:
    failures = []
    for fixture_path in sorted(FIXTURES_PATH.glob("*.json")):
        with open(fixture_path, encoding="utf-8") as f:
            fixture = json.load(f)
        if fixture.get("generated_by") == "scripts/generate_gold_fixtures.py":
            if fixture.get("provenance") != "synthetic":
                failures.append(
                    f"  {fixture_path.name} was mechanically generated but is not "
                    f"tagged provenance=synthetic (docs/evaluation.md de-identification "
                    f"standard only exempts synthetic fixtures from GC sign-off; "
                    f"#62 requires synthetic-only fixtures, no production data)."
                )
    return failures


# ── Check 6: CI eval budget plumbing fails loudly over the documented caps ──

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


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("1", "eval_harness.py exposes the runner + comparator/scorer API", check_harness_api_present),
        ("2", "harness scores every gold fixture and every fixture passes", check_harness_scores_all_fixtures_pass),
        ("3", "every hard_rejection rule has >=1 gold-case detector fixture", check_every_hard_rejection_rule_has_gold_coverage),
        ("4", "detector runner handles on_remove_or_alter rules correctly", check_runner_handles_on_remove_or_alter),
        ("5", "all script-generated fixtures are provenance=synthetic", check_generated_fixtures_are_synthetic),
        ("6", "CI eval budget: fails loudly over the per-run cap", check_budget_fails_loudly_over_per_run_cap),
        ("7", "CI eval budget: fails loudly over the monthly cap", check_budget_fails_loudly_over_monthly_cap),
        ("8", "CI eval budget: succeeds and accumulates atomically under cap", check_budget_succeeds_under_cap_and_is_atomic),
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
        print("All issue #62 eval-harness invariant checks passed.")
        return 0
    else:
        print("One or more issue #62 eval-harness invariant checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
