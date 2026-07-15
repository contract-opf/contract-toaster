#!/usr/bin/env python3
"""
Red gate for issue #15: Eval economics — separate CI budget, tiered gates,
gold-set growth policy.

This test asserts the following invariants that were missing before the fix:

  1. Separate CI eval budget: docs/evaluation.md must name an explicit
     per-run dollar ceiling (e.g. $200/run) for the CI eval budget — distinct
     from the production $20/day ledger.  The words "CI" and a dollar amount
     must appear together in an eval-budget context.

  2. Monthly CI budget cap: docs/evaluation.md must name an explicit monthly
     CI eval budget (e.g. $1,000/mo or similar) so the per-run ceiling is
     bounded in aggregate.

  3. Production ledger separation: docs/evaluation.md must state that the CI
     eval budget is SEPARATE from the production spend ledger (not routed
     through the same $20/day ceiling).

  4. Gate tiers — every-change tier: evaluation.md must define an
     "every-change" (or "every change") gate tier and cap its case count
     (e.g. ≤ 40 cases, a smoke subset).

  5. Gate tiers — release-candidate trigger: evaluation.md must name a
     release-candidate (or "release candidate") trigger for the full
     stochastic gold-set run.

  6. Gate tiers — quarterly recert trigger: evaluation.md must name a
     quarterly recertification trigger for the full stochastic gold-set run.

  7. Gold-set growth policy — candidate tier: evaluation.md must describe
     a "candidate" tier (or candidate pool) for new gold cases before they
     enter the every-change tier.

  8. Gold-set growth policy — every-change tier cap: evaluation.md must
     state that the every-change tier is capped (a case count limit) to
     prevent unbounded growth.

  9. Gold-set growth policy — Legal retirement: evaluation.md must mention
     that Legal can retire or prune redundant cases from the gold set.

  10. Per-run cost stated: evaluation.md must state an expected per-run
      cost estimate (e.g. a dollar amount for a full stochastic run).

  11. Wall-clock stated: evaluation.md must state expected eval wall-clock
      time (hours or minutes) for a full run.

  12. CI budget routing: evaluation.md must state that the harness refuses
      (fails, errors, or aborts) when it would exceed the CI budget — not
      truncate coverage silently.

Run with: python3 tests/test_eval_economics.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALUATION = REPO_ROOT / "docs" / "evaluation.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── helpers ──────────────────────────────────────────────────────────────────

def eval_economics_section(text: str) -> str:
    """Extract the CI Budget / Eval Economics section from evaluation.md.

    Looks for a section heading containing 'budget' or 'economics' or
    'tiered' — fall back to the full document if no such section exists.
    """
    m = re.search(
        r"^#{1,4}[^\n]*(budget|economics|tiered|gate tier)[^\n]*\n(.*?)(?=^#{1,4} |\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return m.group(0) if m else text


# ── Check 1: explicit per-run CI dollar ceiling ───────────────────────────────

def check_ci_per_run_budget() -> list[str]:
    """
    evaluation.md must name an explicit per-run dollar ceiling for the
    CI eval budget (e.g. '$200/run', '$200 per run', '200/run').
    """
    failures = []
    text = read(EVALUATION)
    # Accept patterns like: $200/run, $200 per run, $150/run, etc.
    pattern = re.compile(r"\$\d+(?:,\d{3})?(?:\.\d+)?[^\n]{0,20}(?:per.?run|/run)", re.IGNORECASE)
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not state an explicit per-run CI eval dollar ceiling.\n"
            "  Required: a phrase like '$200/run' or '$200 per run' to bound each CI eval\n"
            "  run separately from the production $20/day ledger. (issue #15)"
        )
    return failures


# ── Check 2: monthly CI budget cap ───────────────────────────────────────────

def check_ci_monthly_budget() -> list[str]:
    """
    evaluation.md must name an explicit monthly CI eval budget
    (e.g. '$1,000/mo', '$1000/month', '$500/month').
    """
    failures = []
    text = read(EVALUATION)
    pattern = re.compile(
        r"\$\d+(?:,\d{3})?(?:\.\d+)?[^\n]{0,20}(?:per.?month|/month|/mo\b)",
        re.IGNORECASE,
    )
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not state an explicit monthly CI eval budget cap.\n"
            "  Required: a phrase like '$1,000/mo' or '$1,000/month' to bound aggregate\n"
            "  monthly CI eval spend. (issue #15)"
        )
    return failures


# ── Check 3: production ledger separation stated ─────────────────────────────

def check_ci_separate_from_production_ledger() -> list[str]:
    """
    evaluation.md must state that the CI eval budget is a SEPARATE ceiling
    from the production spend ledger (not the same $20/day ceiling).
    """
    failures = []
    text = read(EVALUATION)
    # Accept: 'separate', 'distinct', 'not the production', 'not routed through the $20'
    pattern = re.compile(
        r"(?:separate|distinct|dedicated)[^\n]{0,80}(?:CI|eval)[^\n]{0,80}(?:budget|ceiling|ledger)"
        r"|(?:CI|eval)[^\n]{0,80}(?:separate|distinct|dedicated)[^\n]{0,80}(?:budget|ceiling|ledger)"
        r"|not.*(?:production|prod).*ledger|separate.*from.*(?:\$20|production.*ledger|prod.*ledger)",
        re.IGNORECASE | re.DOTALL,
    )
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not clearly state that the CI eval budget is\n"
            "  SEPARATE from the production $20/day spend ledger.\n"
            "  Required: a statement like 'a separate, dedicated CI eval ceiling' or\n"
            "  'distinct from the production ledger'. (issue #15)"
        )
    return failures


# ── Check 4: every-change tier with case-count cap ───────────────────────────

def check_every_change_tier() -> list[str]:
    """
    evaluation.md must define an 'every-change' or 'every change' gate tier
    and cap its case count.
    """
    failures = []
    text = read(EVALUATION)

    if not re.search(r"every.change", text, re.IGNORECASE):
        failures.append(
            "  docs/evaluation.md does not define an 'every-change' gate tier.\n"
            "  Required: a named tier that runs on every change (as opposed to only\n"
            "  on release candidates or quarterly recerts). (issue #15)"
        )
        return failures

    # The every-change tier must have a cap expressed as a number of cases.
    # Accept patterns near 'every-change': 'capped at N', '≤ N cases', 'N-case subset', etc.
    cap_pattern = re.compile(
        r"every.change[^\n]{0,200}(?:cap|≤|<=|at most|subset|smoke)[^\n]{0,100}\d+"
        r"|(?:cap|≤|<=|at most|subset|smoke)[^\n]{0,100}\d+[^\n]{0,200}every.change",
        re.IGNORECASE | re.DOTALL,
    )
    if not cap_pattern.search(text):
        failures.append(
            "  docs/evaluation.md defines an 'every-change' tier but does not cap\n"
            "  its case count (e.g. '≤ 40 cases' or 'smoke subset of N cases').\n"
            "  A case-count cap is required to bound every-change CI cost. (issue #15)"
        )
    return failures


# ── Check 5: release-candidate trigger for full stochastic run ───────────────

def check_release_candidate_trigger() -> list[str]:
    """
    evaluation.md must name a release-candidate trigger for the full
    stochastic gold-set run.
    """
    failures = []
    text = read(EVALUATION)
    pattern = re.compile(r"release.candidate", re.IGNORECASE)
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not name a 'release-candidate' trigger for\n"
            "  the full stochastic gold-set run.\n"
            "  Required: a statement that the full stochastic eval runs on release\n"
            "  candidates (not on every change). (issue #15)"
        )
    return failures


# ── Check 6: quarterly recertification trigger ───────────────────────────────

def check_quarterly_recert_trigger() -> list[str]:
    """
    evaluation.md must name a quarterly recertification trigger for the
    full stochastic gold-set run.
    """
    failures = []
    text = read(EVALUATION)
    pattern = re.compile(r"quarterly", re.IGNORECASE)
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not name a 'quarterly' recertification\n"
            "  trigger for the full stochastic gold-set run.\n"
            "  Required: a statement that the full stochastic eval also runs on\n"
            "  quarterly recertification cycles. (issue #15)"
        )
    return failures


# ── Check 7: gold-set growth policy — candidate tier ─────────────────────────

def check_growth_policy_candidate_tier() -> list[str]:
    """
    evaluation.md must describe a 'candidate' tier for new gold cases
    before they enter the every-change tier.
    """
    failures = []
    text = read(EVALUATION)
    # Accept 'candidate tier', 'candidate pool', 'candidate status', 'enter as candidate'
    pattern = re.compile(r"candidate[^\n]{0,50}tier|tier[^\n]{0,50}candidate|candidate.*pool|candidate.*cases?", re.IGNORECASE)
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not describe a 'candidate' tier for new gold\n"
            "  cases. Required: new cases should enter a candidate tier and be\n"
            "  promoted to the every-change tier after validation (to prevent\n"
            "  unbounded growth of the expensive every-change set). (issue #15)"
        )
    return failures


# ── Check 8: every-change tier case count cap (growth policy) ────────────────

def check_growth_policy_tier_cap() -> list[str]:
    """
    evaluation.md must state that the every-change tier is capped at a
    specific case count so it cannot grow unboundedly.
    """
    failures = []
    text = read(EVALUATION)
    # Looking for a cap figure near 'every-change': already handled partly in
    # check 4, but here we verify it's framed as a growth-policy constraint.
    pattern = re.compile(
        r"every.change[^\n]{0,300}(?:capped|cap|≤|<=|limit)[^\n]{0,100}\d+"
        r"|(?:capped|cap|≤|<=|limit)[^\n]{0,100}\d+[^\n]{0,300}every.change",
        re.IGNORECASE | re.DOTALL,
    )
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not cap the every-change tier case count in\n"
            "  the growth policy context. Required: explicit case count limit so the\n"
            "  every-change tier cannot grow unboundedly as the gold set expands.\n"
            "  (issue #15)"
        )
    return failures


# ── Check 9: gold-set growth policy — Legal retirement/pruning ───────────────

def check_growth_policy_retirement() -> list[str]:
    """
    evaluation.md must mention that Legal periodically retires or prunes
    redundant cases from the gold set.
    """
    failures = []
    text = read(EVALUATION)
    pattern = re.compile(r"retir|prune|pruning|redundant", re.IGNORECASE)
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not mention that Legal periodically retires\n"
            "  or prunes redundant gold cases.\n"
            "  Required: a statement that Legal reviews the gold set and retires cases\n"
            "  that are no longer needed (to prevent unbounded growth). (issue #15)"
        )
    return failures


# ── Check 10: per-run cost estimate stated ────────────────────────────────────

def check_per_run_cost_stated() -> list[str]:
    """
    evaluation.md must state an expected per-run cost estimate for a full
    stochastic eval run (e.g. '$X for a full stochastic run').
    """
    failures = []
    text = read(EVALUATION)
    # Look for a dollar figure near 'run' or 'full' stochastic context
    # Accept: '$70', '$150', '$70–$150', etc. near 'full', 'stochastic', or 'run'
    pattern = re.compile(
        r"\$\d+[^\n]{0,50}(?:full|stochastic|per.run|run)[^\n]{0,50}"
        r"|(?:full|stochastic|per.run|run)[^\n]{0,50}\$\d+",
        re.IGNORECASE,
    )
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not state an expected per-run cost estimate\n"
            "  for a full stochastic eval run.\n"
            "  Required: a dollar figure (e.g. '$70–$150 for a full stochastic run')\n"
            "  so cost can be compared against the CI budget. (issue #15)"
        )
    return failures


# ── Check 11: wall-clock time stated ─────────────────────────────────────────

def check_wall_clock_stated() -> list[str]:
    """
    evaluation.md must state expected eval wall-clock time for a full run
    (hours or minutes).
    """
    failures = []
    text = read(EVALUATION)
    # 'wall-clock' or 'wall clock' already appears; but we need it in the
    # context of an expected time value.
    pattern = re.compile(
        r"wall.clock[^\n]{0,80}(?:hour|minute|min\b|hr\b|\d+\s*(?:hour|min|hr))"
        r"|(?:\d+\s*(?:hour|min|hr)[^\n]{0,80}wall.clock)",
        re.IGNORECASE,
    )
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not state expected eval wall-clock time\n"
            "  (hours or minutes) for a full stochastic run.\n"
            "  Required: a wall-clock estimate (e.g. '~2–4 hours wall-clock') so\n"
            "  CI can be sized accordingly. (issue #15)"
        )
    return failures


# ── Check 12: harness refuses on budget exceeded (not silent truncation) ─────

def check_harness_refuses_on_budget_exceeded() -> list[str]:
    """
    evaluation.md must state that the harness refuses / fails / errors when
    it would exceed the CI budget — it must not silently truncate coverage.
    """
    failures = []
    text = read(EVALUATION)
    # Already partially present: 'fails loudly' — but must be in CI budget context.
    # Accept: 'fail', 'refuse', 'abort', 'error' near 'CI' + 'budget' or 'ceiling'
    pattern = re.compile(
        r"(?:fail|refuse|abort|error)[^\n]{0,100}(?:CI|eval)[^\n]{0,100}(?:budget|ceiling)"
        r"|(?:CI|eval)[^\n]{0,100}(?:budget|ceiling)[^\n]{0,100}(?:fail|refuse|abort|error)",
        re.IGNORECASE,
    )
    if not pattern.search(text):
        failures.append(
            "  docs/evaluation.md does not state that the harness refuses/fails\n"
            "  when the CI eval budget would be exceeded (rather than silently\n"
            "  truncating coverage).\n"
            "  Required: a statement that the harness fails loudly on budget\n"
            "  exhaustion. (issue #15)"
        )
    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("1",  "Per-run CI eval dollar ceiling stated in evaluation.md",
         check_ci_per_run_budget),
        ("2",  "Monthly CI eval budget cap stated in evaluation.md",
         check_ci_monthly_budget),
        ("3",  "CI eval budget stated as SEPARATE from production $20/day ledger",
         check_ci_separate_from_production_ledger),
        ("4",  "Every-change gate tier defined with case-count cap",
         check_every_change_tier),
        ("5",  "Release-candidate trigger defined for full stochastic run",
         check_release_candidate_trigger),
        ("6",  "Quarterly recertification trigger defined",
         check_quarterly_recert_trigger),
        ("7",  "Gold-set growth policy: candidate tier described",
         check_growth_policy_candidate_tier),
        ("8",  "Gold-set growth policy: every-change tier cap stated",
         check_growth_policy_tier_cap),
        ("9",  "Gold-set growth policy: Legal retirement/pruning mentioned",
         check_growth_policy_retirement),
        ("10", "Expected per-run cost for full stochastic run stated",
         check_per_run_cost_stated),
        ("11", "Expected eval wall-clock time stated",
         check_wall_clock_stated),
        ("12", "Harness refuses (not silently truncates) on CI budget exceeded",
         check_harness_refuses_on_budget_exceeded),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code:>2}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All eval-economics invariant checks passed.")
        return 0
    else:
        print("One or more eval-economics invariant checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
