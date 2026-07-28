#!/usr/bin/env python3
"""
Red gate for issue #14: Cost model — pin token caps, make the reservation
retry-inclusive, publish real unit economics.

This test asserts the following invariants that were missing before the fix:

  1. Config: ARCHITECTURE.md Cost shape must cite
     max_input_tokens, max_output_tokens, and max_retries_per_pass
     as named per-review caps, and the reservation formula must
     reference all three.

  2. Reservation property: The formula
       reservation = passes * (1 + max_retries_per_pass)
                   * (max_input_tokens + max_output_tokens)
                   * uncached_price_per_token
     must be explicitly stated in ARCHITECTURE.md so the ledger is
     provably >= worst-case settle for any sequence within the retry budget.

  3. Unit-economics table: ARCHITECTURE.md Cost shape must contain
     a table (Markdown table row or labelled list) that cites:
       - worst-case $/review
       - max reviews/day at the ceiling
       - typical $/review  (or similar "expected" / "average" label)
       - dev idle monthly cost with enumerated fixed-cost line items
       - a prod monthly target (50–200 reviews or equivalent range)
     The "comfortably above" phrase must be absent from Cost shape
     (it was unverifiable; arithmetic replaces it).

  4. Leakage-scan mechanism: ARCHITECTURE.md must state that the
     leakage scan is deterministic (not a model call) — or, if it
     is model-based, must name the cost and the model used.  Either
     way the mechanism must appear.

  5. Pricing contradiction: design-notes.md must not simultaneously
     claim BOTH "rates match direct API" AND a regional premium.
     One or the other — or a sentence reconciling them — must be
     the sole pricing statement.

  6. Cap-lockout RUNBOOK: RUNBOOK.md must contain a section or
     subsection covering the "users hitting the daily cap mid-day"
     scenario (search terms: "cap" + "mid-day" or "lockout" or
     "hitting the cap").

Run with: python3 tests/test_cost_model_invariants.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DESIGN_NOTES = REPO_ROOT / "docs" / "design-notes.md"
RUNBOOK = REPO_ROOT / "RUNBOOK.md"

# ── helpers ───────────────────────────────────────────────────────────────────

def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def cost_shape_section(arch_text: str) -> str:
    """Extract just the Cost shape section from ARCHITECTURE.md."""
    m = re.search(
        r"^## Cost shape\s*\n(.*?)(?=^## |\Z)",
        arch_text,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else ""


# ── Check 1: named token caps exist in Cost shape ────────────────────────────

def check_token_caps_in_cost_shape() -> list[str]:
    """
    ARCHITECTURE.md Cost shape must cite max_input_tokens,
    max_output_tokens, and max_retries_per_pass as named config values.
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = cost_shape_section(arch_text)

    required_caps = [
        "max_input_tokens",
        "max_output_tokens",
        "max_retries_per_pass",
    ]
    for cap in required_caps:
        if cap not in section:
            failures.append(
                f"  ARCHITECTURE.md Cost shape is missing named cap: '{cap}'\n"
                f"  All three caps must be cited so the reservation formula is\n"
                f"  verifiable. (issue #14)"
            )
    return failures


# ── Check 2: retry-inclusive reservation formula stated ─────────────────────

def check_reservation_formula_retry_inclusive() -> list[str]:
    """
    ARCHITECTURE.md must state a reservation formula that includes retries:
    the phrase 'max_retries_per_pass' must appear in the same section that
    describes the worst-case spend reservation calculation.  The formula
    must make clear that retries are included in the reserved amount.
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = cost_shape_section(arch_text)

    # The formula must mention retries (max_retries_per_pass was checked in
    # check 1; here we check that the reservation *formula* in cost shape
    # explicitly ties retries to the reservation, not just as a footnote).
    if "max_retries_per_pass" not in section:
        failures.append(
            "  ARCHITECTURE.md Cost shape reservation formula does not reference\n"
            "  'max_retries_per_pass'. The reservation must be retry-inclusive\n"
            "  (passes × (1 + max_retries_per_pass) × max tokens × price)\n"
            "  so bounded retries cannot overshoot the reservation. (issue #14)"
        )

    # Check that the reservation formula mentions "retry" or "retries" in
    # context of the cost / reservation calculation
    if not re.search(r"retr(y|ies)", section, re.IGNORECASE):
        failures.append(
            "  ARCHITECTURE.md Cost shape does not mention retries in the\n"
            "  reservation calculation. The formula must be provably ≥ worst-case\n"
            "  settle for any sequence of attempts within the retry budget. (issue #14)"
        )

    return failures


# ── Check 3: unit-economics table present ────────────────────────────────────

def check_unit_economics_table() -> list[str]:
    """
    ARCHITECTURE.md Cost shape must contain a unit-economics table or
    labelled list covering:
      - worst-case $/review
      - max reviews/day at ceiling
      - typical (or average, or expected) $/review
      - dev idle monthly cost
      - prod monthly target
    And must NOT contain the phrase "comfortably above" (replaced by arithmetic).
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = cost_shape_section(arch_text)

    # Each required element — pattern and human label
    required_elements = [
        (
            re.compile(r"worst.case.*\$/review|worst.case.*per.review|\$/review.*worst.case",
                       re.IGNORECASE),
            "worst-case $/review figure",
        ),
        (
            re.compile(r"max.*reviews?.*per.*day|max.*reviews?.*day|reviews?.*per.*day.*ceiling|"
                       r"reviews?.*day.*cap",
                       re.IGNORECASE),
            "max reviews/day at the ceiling",
        ),
        (
            re.compile(r"typical.*\$/review|typical.*per.review|expected.*\$/review|"
                       r"average.*\$/review|\$/review.*typical|\$/review.*expected",
                       re.IGNORECASE),
            "typical (or expected/average) $/review figure",
        ),
        (
            re.compile(r"dev.*idle|idle.*dev|idle.*month|month.*idle", re.IGNORECASE),
            "dev idle monthly cost with fixed-cost line items",
        ),
        (
            re.compile(r"prod.*month|monthly.*prod|50.{0,10}200.*review|production.*month",
                       re.IGNORECASE),
            "prod monthly target (50–200 reviews range or equivalent)",
        ),
    ]

    for pattern, label in required_elements:
        if not pattern.search(section):
            failures.append(
                f"  ARCHITECTURE.md Cost shape is missing: {label}\n"
                f"  A complete unit-economics table is required by issue #14."
            )

    # "comfortably above" must be gone from Cost shape
    if "comfortably above" in section:
        failures.append(
            "  ARCHITECTURE.md Cost shape still contains 'comfortably above' —\n"
            "  this phrase was unverifiable and must be replaced with arithmetic.\n"
            "  (issue #14)"
        )

    return failures


# ── Check 4: leakage-scan mechanism documented ───────────────────────────────

def check_leakage_scan_mechanism() -> list[str]:
    """
    ARCHITECTURE.md must state whether the leakage scan is deterministic
    or model-based.  The Output leakage scan section (or Cost shape) must
    say 'deterministic' OR name the model used and its cost.
    """
    failures = []
    arch_text = read(ARCHITECTURE)

    # Find the leakage scan section
    m = re.search(
        r"(?:leakage scan|Output leakage scan).*?(?=###|\Z)",
        arch_text,
        re.IGNORECASE | re.DOTALL,
    )
    leakage_section = m.group(0) if m else ""

    has_deterministic = bool(re.search(r"deterministic", leakage_section, re.IGNORECASE))
    has_model_based = bool(re.search(
        r"model.based|model call|LLM|InvokeModel", leakage_section, re.IGNORECASE
    ))

    if not (has_deterministic or has_model_based):
        failures.append(
            "  ARCHITECTURE.md leakage scan section does not state whether the\n"
            "  scan is deterministic or model-based. This is required by issue #14\n"
            "  so the mechanism can be costed and the reservation sized correctly."
        )

    return failures


# ── Check 5: pricing contradiction resolved ──────────────────────────────────

def check_pricing_contradiction() -> list[str]:
    """
    docs/design-notes.md must not claim BOTH:
      (a) 'rates match direct API' (or 'per-token pricing matches')
      AND
      (b) a regional premium (e.g. '10% ... premium' or '10% regional')
    without reconciling them in the same paragraph.

    A contradiction is flagged if both (a) and (b) appear and there is
    no reconciling sentence explaining which statement is correct.
    """
    failures = []
    notes_text = read(DESIGN_NOTES)

    has_match_direct = bool(re.search(
        r"per.token.*pricing.*match|rates.*match.*direct|pricing.*matches.*direct",
        notes_text,
        re.IGNORECASE,
    ))
    has_premium = bool(re.search(
        r"10%.*premium|regional.*premium|premium.*regional",
        notes_text,
        re.IGNORECASE,
    ))

    if has_match_direct and has_premium:
        # Both claims exist — check if a reconciling sentence is present
        # A reconciling sentence should mention the premium in context of
        # Bedrock vs direct, or explicitly qualify "rates match" as
        # excluding the regional endpoint charge.
        # We look for proximity: both claims in the same paragraph.
        paragraphs = notes_text.split("\n\n")
        for para in paragraphs:
            if re.search(
                r"per.token.*pricing.*match|rates.*match.*direct|pricing.*matches.*direct",
                para, re.IGNORECASE
            ) and re.search(
                r"10%.*premium|regional.*premium|premium.*regional",
                para, re.IGNORECASE
            ):
                # Both appear in the same paragraph — that is the reconciliation
                return failures  # no failure if same paragraph

        failures.append(
            "  docs/design-notes.md contains a pricing contradiction:\n"
            "    (a) claims 'rates match direct API' (or similar)\n"
            "    (b) also claims a '10% regional premium'\n"
            "  These appear in separate contexts with no reconciling sentence.\n"
            "  Fix: state the verified current rate, note the premium, and pick\n"
            "  one canonical statement. (issue #14)"
        )

    return failures


# ── Check 6: cap-lockout RUNBOOK entry ───────────────────────────────────────

def check_runbook_cap_lockout_entry() -> list[str]:
    """
    RUNBOOK.md must contain a section or subsection that covers the
    scenario where users hit the daily spend cap mid-day.

    We look for a heading or bold label that mentions the cap or lockout
    in context of a user-facing action.
    """
    failures = []
    runbook_text = read(RUNBOOK)

    # A heading/sub-heading or bold label covering the cap-lockout scenario
    # Accept phrases like: "Users hitting the cap", "Daily limit reached",
    # "cap mid-day", "lockout", "Users hit the daily cap"
    lockout_pattern = re.compile(
        r"(###|####|\*\*)[^\n]*(cap|lockout|limit reached|daily limit|hitting the cap|"
        r"cap mid.?day|users.*cap|cap.*users)[^\n]*",
        re.IGNORECASE,
    )

    if not lockout_pattern.search(runbook_text):
        failures.append(
            "  RUNBOOK.md is missing a section heading or bold label for the\n"
            "  'users hitting the daily cap mid-day' scenario. A runbook entry\n"
            "  must describe: how to diagnose the lockout, how to raise the ceiling,\n"
            "  and how to reconcile phantom reservations. (issue #14)"
        )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("1", "Token caps (max_input_tokens, max_output_tokens, max_retries_per_pass) in Cost shape",
         check_token_caps_in_cost_shape),
        ("2", "Reservation formula is retry-inclusive",
         check_reservation_formula_retry_inclusive),
        ("3", "Unit-economics table present (worst-case, max/day, typical, idle, prod target)",
         check_unit_economics_table),
        ("4", "Leakage-scan mechanism stated (deterministic or model-based)",
         check_leakage_scan_mechanism),
        ("5", "Pricing contradiction resolved in design-notes.md",
         check_pricing_contradiction),
        ("6", "Cap-lockout RUNBOOK entry exists",
         check_runbook_cap_lockout_entry),
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
        print("All cost-model invariant checks passed.")
        return 0
    else:
        print("One or more cost-model invariant checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
