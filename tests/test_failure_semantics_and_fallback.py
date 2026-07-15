#!/usr/bin/env python3
"""
CI gate for issue #16: critic-failure, retrieval-failure, and fallback-model semantics.

Three invariants asserted by this gate:

  GATE 1 — Critic failure is terminal (never silent single-pass DONE)
    ARCHITECTURE.md must contain an explicit statement that critic failure is
    terminal and must never produce a silent single-pass DONE.  The phrase
    "critic" must appear alongside a statement of terminality or the
    ERROR/MANUAL_REVIEW_REQUIRED status in the context of failure.

  GATE 2 — Retrieval failure and empty-retrieval semantics are specified
    ARCHITECTURE.md must specify both:
      (a) what happens when the KB query itself errors (retrieval failure
          after bounded retry must route to ERROR),
      (b) what happens when the query returns an empty result set (must
          either terminate to a documented degraded-mode that is recorded
          on the review row, or explicitly enumerate the condition).

  GATE 3 — Automatic fallback is prohibited; fallback_model_id is not
    present in the seed playbook without documented semantics
    ARCHITECTURE.md must contain an explicit statement that automatic
    failover is prohibited.  Additionally, the v1 seed playbook must NOT
    carry a fallback_model_id unless it has:
      - a corresponding eval_run_id in the release block (a certifying
        eval run — but the playbook is draft so release is absent), OR
      - ARCHITECTURE.md explicitly defines the fallback as manual-only
        with an eval-gate requirement.
    In the absence of a certifying eval run, the simplest compliant state
    is that fallback_model_id is absent from the seed playbook.

Exit codes: 0 = pass, 1 = fail
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ── Gate 1: Critic failure is terminal ───────────────────────────────────────

# We look for text in ARCHITECTURE.md that:
#   - mentions critic-pass failure / critic failure
#   - asserts it is terminal / routes to ERROR / MANUAL_REVIEW_REQUIRED
#   - explicitly says it is NOT silent single-pass DONE
#
# Required phrase patterns (all must match somewhere in the doc):
CRITIC_FAILURE_PATTERNS = [
    # The word "critic" appearing near "terminal" or "error" in the context of failure
    re.compile(
        r"critic.{0,120}(?:terminal|ERROR|MANUAL_REVIEW_REQUIRED|fail)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Explicit prohibition of silent single-pass DONE on critic failure
    re.compile(
        r"(?:never|not|no)\s+(?:a\s+)?silent\s+(?:single[- ]pass|single\s+pass)",
        re.IGNORECASE,
    ),
]

# ── Gate 2: Retrieval failure / empty-retrieval semantics ─────────────────────

# Required patterns — each must appear in ARCHITECTURE.md:
RETRIEVAL_FAILURE_PATTERNS = [
    # Retrieval failure after retries routes to ERROR (or similar)
    re.compile(
        r"retrieval.{0,200}(?:failure|error|fail).{0,200}(?:ERROR|terminal|MANUAL_REVIEW)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Empty retrieval / empty corpus result handling
    re.compile(
        r"(?:empty\s+(?:retrieval|corpus|result)|retrieval.{0,80}empty).{0,200}"
        r"(?:degrad|fallback|ERROR|MANUAL_REVIEW|recorded|documented|proceeds\s+only\s+if)",
        re.IGNORECASE | re.DOTALL,
    ),
    # degraded_mode flag is recorded on the review row (the requirement per issue)
    re.compile(
        r"degrad(?:ed)?[_\s]*mode",
        re.IGNORECASE,
    ),
]

# ── Gate 3: Automatic fallback prohibited; seed playbook has no uncertified fallback ──

# Required phrase in ARCHITECTURE.md:
AUTOMATIC_FAILOVER_PROHIBITED_PATTERN = re.compile(
    r"automatic\s+failover\s+(?:is\s+)?prohibited",
    re.IGNORECASE,
)

# If fallback_model_id is present in the playbook metadata, ARCHITECTURE.md must
# explicitly define it as manual-only with an eval-gate requirement:
MANUAL_ONLY_FALLBACK_PATTERN = re.compile(
    r"fallback.{0,200}manual[- ]only",
    re.IGNORECASE | re.DOTALL,
)
EVAL_GATE_FALLBACK_PATTERN = re.compile(
    r"fallback.{0,200}eval[- ](?:gate|run|certif)",
    re.IGNORECASE | re.DOTALL,
)


def gate_1_critic_failure(arch_text: str) -> list[str]:
    failures = []
    for i, pattern in enumerate(CRITIC_FAILURE_PATTERNS, 1):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 1.{i}: ARCHITECTURE.md does not contain the required "
                f"language about critic failure being terminal.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: ARCHITECTURE.md must explicitly state that critic "
                f"failure is terminal and never produces silent single-pass DONE."
            )
    return failures


def gate_2_retrieval_failure(arch_text: str) -> list[str]:
    failures = []
    labels = [
        "retrieval failure routes to ERROR after bounded retry",
        "empty retrieval result has documented semantics (degraded mode or ERROR)",
        "degraded_mode flag recorded on the review row",
    ]
    for i, (pattern, label) in enumerate(
        zip(RETRIEVAL_FAILURE_PATTERNS, labels), 1
    ):
        if not pattern.search(arch_text):
            failures.append(
                f"  Gate 2.{i}: ARCHITECTURE.md is missing semantics for: {label}.\n"
                f"  Missing pattern: {pattern.pattern!r}\n"
                f"  Required: ARCHITECTURE.md must specify {label}."
            )
    return failures


def gate_3_fallback(arch_text: str, playbook: dict) -> list[str]:
    failures = []

    # Check that automatic failover is prohibited in writing
    if not AUTOMATIC_FAILOVER_PROHIBITED_PATTERN.search(arch_text):
        failures.append(
            "  Gate 3a: ARCHITECTURE.md does not explicitly state that automatic "
            "failover is prohibited.\n"
            "  Required: ARCHITECTURE.md must contain the phrase "
            '"automatic failover is prohibited" (or equivalent).\n'
            "  Missing pattern: " + AUTOMATIC_FAILOVER_PROHIBITED_PATTERN.pattern
        )

    # Check the seed playbook
    metadata = playbook.get("playbook", {}).get("metadata", {})
    fallback = metadata.get("fallback_model_id")

    if fallback is not None:
        # Fallback is present — it must have documented manual-only + eval-gate semantics
        has_manual_only = MANUAL_ONLY_FALLBACK_PATTERN.search(arch_text)
        has_eval_gate = EVAL_GATE_FALLBACK_PATTERN.search(arch_text)

        # Also check for an eval_run_id in the release block (none for a draft playbook)
        release = playbook.get("playbook", {}).get("release", {})
        eval_run_id = release.get("eval_run_id")

        if not has_manual_only:
            failures.append(
                f"  Gate 3b: The seed playbook carries fallback_model_id={fallback!r} "
                "but ARCHITECTURE.md does not define fallback as manual-only.\n"
                "  Required: either remove fallback_model_id from the seed playbook, "
                "or add text to ARCHITECTURE.md explicitly stating the fallback is "
                "manual-only (admin action, audited, distinct release bundle).\n"
                "  Missing pattern: " + MANUAL_ONLY_FALLBACK_PATTERN.pattern
            )

        if not has_eval_gate:
            failures.append(
                f"  Gate 3c: The seed playbook carries fallback_model_id={fallback!r} "
                "but ARCHITECTURE.md does not require an eval-gate for fallback use.\n"
                "  Required: either remove fallback_model_id from the seed playbook, "
                "or add text to ARCHITECTURE.md stating that fallback requires a "
                "certifying eval run.\n"
                "  Missing pattern: " + EVAL_GATE_FALLBACK_PATTERN.pattern
            )

        if eval_run_id is None:
            failures.append(
                f"  Gate 3d: The seed playbook carries fallback_model_id={fallback!r} "
                "but has no release.eval_run_id (no certifying eval run for Haiku).\n"
                "  Required: remove fallback_model_id from the v1 seed until Haiku 4.5 "
                "has a certifying gold-set run, or define it as manual-only with an "
                "eval-gate reference in ARCHITECTURE.md and the release block."
            )

    return failures


def main() -> int:
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        with PLAYBOOK_PATH.open() as fh:
            playbook = json.load(fh)
    except Exception as e:
        print(f"FAIL: Could not load playbook: {e}")
        return 1

    all_failures: list[str] = []

    g1 = gate_1_critic_failure(arch_text)
    g2 = gate_2_retrieval_failure(arch_text)
    g3 = gate_3_fallback(arch_text, playbook)

    print("Gate 1: Critic failure is terminal (never silent single-pass DONE)")
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    else:
        print("  PASS")

    print()
    print("Gate 2: Retrieval failure / empty-retrieval semantics specified")
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    print("Gate 3: Automatic fallback prohibited; seed playbook fallback state")
    if g3:
        for f in g3:
            print(f)
        all_failures.extend(g3)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #16 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all failure-semantics and fallback gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
