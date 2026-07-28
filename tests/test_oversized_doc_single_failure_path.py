#!/usr/bin/env python3
"""
CI gate for issue #24: single authoritative failure path for oversized documents.

Three invariants asserted by this gate:

  GATE 1 — Single failure point: step-14 cap terminates with MANUAL_REVIEW_REQUIRED
    ARCHITECTURE.md step 14 must name the status MANUAL_REVIEW_REQUIRED and the
    reason code `document_too_large` as the single failure point for oversized
    documents, and must state that this check fires *before* any model call.
    The cap check at step 14 is the single authoritative failure point — there
    must be no secondary "manually segment" procedure or model-side
    ValidationException path that suggests a parallel or alternative failure flow.

  GATE 2 — No reachable model-side input-too-long ValidationException
    ARCHITECTURE.md must assert (or clearly imply) that a ValidationException
    "input is too long" / "input_too_long" is unreachable when the step-14 cap
    is correctly enforced — i.e. the cap is the guard and a model-side overflow
    indicates cap misconfiguration, not a normal operational condition.
    The step-14 description must make the caps authoritative (they are enforced
    before any model call in step 15/16).

  GATE 3 — RUNBOOK ValidationException entry names cap misconfiguration as the
    sole cause; no "manually segment" procedure survives
    RUNBOOK.md must:
      (a) NOT contain a surviving "manually segment" instruction that presents
          manual segmentation as an admin procedure for oversized documents
          (because step 14 is the single failure point — a human never manually
          segments; they fix the config or contact support), AND
      (b) Document that a ValidationException "input is too long" points at cap
          misconfiguration as the *only* cause, not a normal user-facing error.

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"
OUTPUT_CONTRACT_PATH = REPO_ROOT / "docs" / "output-contract.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE 1: Step-14 cap is the single authoritative failure point
# ---------------------------------------------------------------------------
#
# ARCHITECTURE.md step 14 currently says "Enforce caps: document size, extracted
# tokens, sections, top-K per section, output tokens" without naming a status or
# reason code.  The fix must:
#   (a) name the status MANUAL_REVIEW_REQUIRED for oversized-document termination,
#   (b) name the reason code `document_too_large`,
#   (c) state it fires before any model call (i.e. before step 15).

# Pattern (a): step 14 names MANUAL_REVIEW_REQUIRED as the oversized-doc status
STEP14_STATUS_PATTERN = re.compile(
    r"(?:step.{0,20}14|step\s+14|14\..{0,30}Assemble|Enforce\s+caps).{0,600}"
    r"MANUAL_REVIEW_REQUIRED",
    re.IGNORECASE | re.DOTALL,
)

# Pattern (b): document_too_large reason code named in ARCHITECTURE.md
REASON_CODE_PATTERN = re.compile(
    r"document_too_large",
    re.IGNORECASE,
)

# Pattern (c): the cap check fires *before* any model call
# We accept any wording that places the cap enforcement before step 15 / before
# "any model call" / "before the primary review".
BEFORE_MODEL_CALL_PATTERN = re.compile(
    r"(?:before\s+(?:any\s+)?model\s+call"
    r"|before\s+(?:the\s+)?(?:primary\s+review|step\s+15|Bedrock)"
    r"|cap\s+check.{0,80}before"
    r"|terminates.{0,120}before.{0,80}model)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# GATE 2: model-side ValidationException is unreachable when cap is enforced
# ---------------------------------------------------------------------------
#
# ARCHITECTURE.md must assert that an "input is too long" ValidationException
# can only occur if the step-14 cap is misconfigured — i.e. it is not a normal
# user-visible error path, it is a cap-misconfiguration signal.

# Pattern: ValidationException "input is too long" signals cap misconfiguration
VALIDATION_EXCEPTION_PATTERN = re.compile(
    r"ValidationException.{0,400}"
    r"(?:misconfigur|cap\s+misconfigur|cap\s+is\s+(?:wrong|incorrect|misconfigured)"
    r"|only\s+cause|sole\s+cause|indicates?.{0,40}misconfigur)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# GATE 3: RUNBOOK "manually segment" procedure removed; ValidationException
#         entry points at cap misconfiguration only
# ---------------------------------------------------------------------------
#
# The RUNBOOK.md currently reads (under "Bedrock returns errors"):
#   "If it happens, the user sees a clear error message; the admin should review
#   the document and decide whether to manually segment it."
#
# This must be replaced with:
#   - No surviving "manually segment" instruction for the oversized-document case
#   - A statement that a ValidationException "input is too long" means the
#     step-14 cap is misconfigured (not that the user has a big document to fix)

# Pattern: an affirmative instruction to "manually segment" must NOT survive.
# We only flag it when the phrase appears as an affirmative directive (e.g.
# "should ... manually segment", "decide whether to manually segment",
# "manually segment it/the document").  A prohibition ("do not manually segment")
# is correct and must not be flagged.
MANUALLY_SEGMENT_PATTERN = re.compile(
    r"(?:"
    r"should\s+.{0,60}manually\s+segment"
    r"|decide\s+whether\s+to\s+manually\s+segment"
    r"|(?<!not\s)manually\s+segment\s+(?:it|the\s+document|the\s+file)"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# Pattern: RUNBOOK ValidationException entry must point at cap misconfiguration
RUNBOOK_VALIDATION_EXCEPTION_PATTERN = re.compile(
    r"ValidationException.{0,400}"
    r"(?:misconfigur|cap\s+(?:is\s+)?(?:wrong|incorrect|misconfigured)|step.{0,10}14"
    r"|cap\s+misconfigur)",
    re.IGNORECASE | re.DOTALL,
)

# Pattern: RUNBOOK must document the user-facing message for oversized documents
# (the user sees MANUAL_REVIEW_REQUIRED, not a model error)
RUNBOOK_USER_MESSAGE_PATTERN = re.compile(
    r"(?:document_too_large|MANUAL_REVIEW_REQUIRED.{0,200}oversized"
    r"|oversized.{0,200}MANUAL_REVIEW_REQUIRED"
    r"|document.{0,80}(?:too\s+large|exceeds?.{0,40}cap).{0,200}MANUAL_REVIEW_REQUIRED)",
    re.IGNORECASE | re.DOTALL,
)


def gate_1_step14_single_failure_point(arch_text: str) -> list[str]:
    failures = []

    if not STEP14_STATUS_PATTERN.search(arch_text):
        failures.append(
            "  Gate 1a: ARCHITECTURE.md step 14 does not name MANUAL_REVIEW_REQUIRED\n"
            "  as the status for oversized-document termination.\n"
            "  Required: step 14 must state that a document exceeding the cap\n"
            "  terminates with status=MANUAL_REVIEW_REQUIRED.\n"
            f"  Missing pattern: {STEP14_STATUS_PATTERN.pattern!r}"
        )

    if not REASON_CODE_PATTERN.search(arch_text):
        failures.append(
            "  Gate 1b: ARCHITECTURE.md does not name the reason code `document_too_large`.\n"
            "  Required: ARCHITECTURE.md must define `document_too_large` as the\n"
            "  canonical reason code for oversized-document terminations.\n"
            f"  Missing pattern: {REASON_CODE_PATTERN.pattern!r}"
        )

    if not BEFORE_MODEL_CALL_PATTERN.search(arch_text):
        failures.append(
            "  Gate 1c: ARCHITECTURE.md does not assert that the step-14 cap check\n"
            "  fires *before* any model call.\n"
            "  Required: ARCHITECTURE.md must state that the oversized-document cap\n"
            "  check terminates the review before any Bedrock/model call is made\n"
            "  (i.e. it fires at step 14, before the primary review at step 15).\n"
            f"  Missing pattern: {BEFORE_MODEL_CALL_PATTERN.pattern!r}"
        )

    return failures


def gate_2_validation_exception_unreachable(arch_text: str) -> list[str]:
    failures = []

    if not VALIDATION_EXCEPTION_PATTERN.search(arch_text):
        failures.append(
            "  Gate 2: ARCHITECTURE.md does not assert that a ValidationException\n"
            "  'input is too long' indicates cap misconfiguration (not a normal\n"
            "  operational condition reachable by oversized documents).\n"
            "  Required: ARCHITECTURE.md must state that this ValidationException\n"
            "  is unreachable when the step-14 cap is correctly enforced, and\n"
            "  that its occurrence means the cap itself is misconfigured.\n"
            f"  Missing pattern: {VALIDATION_EXCEPTION_PATTERN.pattern!r}"
        )

    return failures


def gate_3_runbook_updated(runbook_text: str) -> list[str]:
    failures = []

    # (a) "manually segment" must not survive as an admin procedure
    if MANUALLY_SEGMENT_PATTERN.search(runbook_text):
        failures.append(
            "  Gate 3a: RUNBOOK.md still contains a 'manually segment' instruction.\n"
            "  The step-14 cap check is the single failure point — there is no\n"
            "  manual segmentation procedure.  Remove or rewrite the entry under\n"
            "  'Bedrock returns errors → ValidationException' to remove this phrase.\n"
            f"  Found pattern: {MANUALLY_SEGMENT_PATTERN.pattern!r}"
        )

    # (b) RUNBOOK ValidationException entry must point at cap misconfiguration
    if not RUNBOOK_VALIDATION_EXCEPTION_PATTERN.search(runbook_text):
        failures.append(
            "  Gate 3b: RUNBOOK.md ValidationException entry does not name cap\n"
            "  misconfiguration as the cause.\n"
            "  Required: the RUNBOOK.md 'Bedrock returns errors' section must state\n"
            "  that a ValidationException 'input is too long' means the step-14 cap\n"
            "  is misconfigured — it should not be reachable in normal operation.\n"
            f"  Missing pattern: {RUNBOOK_VALIDATION_EXCEPTION_PATTERN.pattern!r}"
        )

    # (c) RUNBOOK must document the user-facing outcome (MANUAL_REVIEW_REQUIRED)
    if not RUNBOOK_USER_MESSAGE_PATTERN.search(runbook_text):
        failures.append(
            "  Gate 3c: RUNBOOK.md does not document the user-facing outcome for\n"
            "  oversized documents (MANUAL_REVIEW_REQUIRED + document_too_large).\n"
            "  Required: RUNBOOK.md must state that an oversized document results in\n"
            "  MANUAL_REVIEW_REQUIRED with reason document_too_large, not a raw model\n"
            "  error — so the operator knows what the user sees.\n"
            f"  Missing pattern: {RUNBOOK_USER_MESSAGE_PATTERN.pattern!r}"
        )

    return failures


def main() -> int:
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    try:
        runbook_text = read_text(RUNBOOK_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    g1 = gate_1_step14_single_failure_point(arch_text)
    g2 = gate_2_validation_exception_unreachable(arch_text)
    g3 = gate_3_runbook_updated(runbook_text)

    print("Gate 1: Step-14 cap is the single authoritative failure point")
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    else:
        print("  PASS")

    print()
    print("Gate 2: Model-side ValidationException is unreachable when cap is enforced")
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    print("Gate 3: RUNBOOK updated — no 'manually segment' procedure, cap-misconfiguration cause named")
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
            "See issue #24 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all oversized-document single-failure-path gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
