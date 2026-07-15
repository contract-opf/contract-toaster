#!/usr/bin/env python3
"""
CI gate for issue #39: Audited 'export clean copy' affordance for approved redlines.

Because the clean-copy feature is deferred in v1, this issue delivers:
  1. A RUNBOOK procedure for correctly de-marking an approved document
     (so attorneys have a documented, safe path after approval).
  2. docs/output-contract.md updated to state that the marker is the default
     and clean export is the deliberate approval exit.
  3. docs/threat-model.md external-communication section updated to reflect
     the controlled exit path.

Three gates, matching the TDD plan and acceptance criteria:

  GATE 1 — RUNBOOK: de-marking procedure exists
    RUNBOOK.md must contain a named section (or clearly-labelled procedure)
    describing how to correctly remove the export marker from an approved
    document, with:
      - reference to the cover page / every-page header+footer placement
      - guidance that both locations must be removed to fully de-mark
      - note that the document must have attorney approval before de-marking
      - note that this is an audited step (or a reference to audit / recording)

  GATE 2 — output-contract.md: marker is default; clean export is approval exit
    docs/output-contract.md must state:
      - the export-warning marker is the default on every generated redline
      - the clean copy (or clean export / de-marked copy) is the deliberate
        approval exit (i.e. the intended path for documents after attorney
        approval), NOT the default
      - a cross-reference to the RUNBOOK for the de-marking procedure (or an
        explicit statement that the procedure lives there)

  GATE 3 — threat-model.md: controlled exit reflected
    docs/threat-model.md external-communication guardrail section must:
      - acknowledge that there is a controlled / deliberate removal path
        after attorney approval (the "approval exit" or "clean exit")
      - state that this is distinct from accidental or unauthorized removal
        (i.e. the friction model is preserved; the approval exit is
        explicitly different from "a determined user can still remove it")

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"
OUTPUT_CONTRACT_PATH = REPO_ROOT / "docs" / "output-contract.md"
THREAT_MODEL_PATH = REPO_ROOT / "docs" / "threat-model.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE 1 — RUNBOOK: de-marking procedure
# ---------------------------------------------------------------------------

# Pattern R1: RUNBOOK has a section/heading for de-marking / removing the marker
# after attorney approval.
RUNBOOK_DEMARK_SECTION_PATTERN = re.compile(
    r"(?:###?\s+(?:Removing|Demark|De-mark|Clean.copy|Export.clean|Approved.redline"
    r"|Removing.the.export.marker|Exporting.an.approved"
    r"|After.attorney.approval"
    r"|Clean.export))",
    re.IGNORECASE,
)

# Pattern R2: RUNBOOK mentions both the cover page AND the header/footer must be removed
RUNBOOK_COVER_AND_HEADER_PATTERN = re.compile(
    r"(?:cover\s+(?:page|note)|cover.page)"
    r"(?:.|\n){0,800}"
    r"(?:header|footer)",
    re.IGNORECASE,
)

# Pattern R3: RUNBOOK ties de-marking to attorney approval
RUNBOOK_APPROVAL_REQUIRED_PATTERN = re.compile(
    r"(?:attorney\s+approv|approv(?:al|ed)\s+(?:by\s+)?attorney|after\s+attorney\s+approv"
    r"|attorney\s+has\s+approv|approv(?:al|ed).{0,80}(?:before|prior|required)"
    r"|only\s+after\s+approv|must\s+be\s+approv)",
    re.IGNORECASE,
)

# Pattern R4: RUNBOOK references audit / recording the step
RUNBOOK_AUDIT_PATTERN = re.compile(
    r"(?:audit|record(?:ed?)?|log(?:ged?)?)"
    r"(?:.|\n){0,400}"
    r"(?:de.mark|export.marker|clean.cop|cover.page.{0,200}header|export.approv)",
    re.IGNORECASE,
)

# Alternate audit pattern: de-marking procedure is near audit language
RUNBOOK_AUDIT_ALT_PATTERN = re.compile(
    r"(?:de.mark|clean.cop|export.marker|removing.the.marker|cover.page)"
    r"(?:.|\n){0,600}"
    r"(?:audit|record(?:ed?)?|log(?:ged?)?)",
    re.IGNORECASE,
)


def gate_1_runbook(runbook_text: str) -> list[str]:
    """RUNBOOK must contain a documented de-marking procedure."""
    failures = []

    if not RUNBOOK_DEMARK_SECTION_PATTERN.search(runbook_text):
        failures.append(
            "  Gate R1: RUNBOOK.md does not contain a named section for removing the\n"
            "  export marker from an approved document (e.g., '### Removing the export\n"
            "  marker from an approved redline' or '### Clean-copy export after approval').\n"
            "  Required: a clearly-labelled RUNBOOK procedure for correctly de-marking\n"
            "  an approved document.\n"
            f"  Missing pattern: {RUNBOOK_DEMARK_SECTION_PATTERN.pattern[:120]!r}"
        )

    if not RUNBOOK_COVER_AND_HEADER_PATTERN.search(runbook_text):
        failures.append(
            "  Gate R2: RUNBOOK.md de-marking procedure does not reference both the\n"
            "  cover page AND the running header/footer that must be removed.\n"
            "  Required: the procedure must guide the attorney to remove both the\n"
            "  first-page cover note and the every-page header/footer marker.\n"
            f"  Missing pattern: {RUNBOOK_COVER_AND_HEADER_PATTERN.pattern[:120]!r}"
        )

    if not RUNBOOK_APPROVAL_REQUIRED_PATTERN.search(runbook_text):
        failures.append(
            "  Gate R3: RUNBOOK.md de-marking procedure does not tie de-marking to\n"
            "  attorney approval.\n"
            "  Required: the procedure must state that de-marking is only appropriate\n"
            "  after attorney approval, to preserve the friction model for unapproved docs.\n"
            f"  Missing pattern: {RUNBOOK_APPROVAL_REQUIRED_PATTERN.pattern[:120]!r}"
        )

    has_audit = (
        RUNBOOK_AUDIT_PATTERN.search(runbook_text)
        or RUNBOOK_AUDIT_ALT_PATTERN.search(runbook_text)
    )
    if not has_audit:
        failures.append(
            "  Gate R4: RUNBOOK.md de-marking procedure does not reference audit/recording\n"
            "  of the de-marking step.\n"
            "  Required: the procedure must note that the de-marking action should be\n"
            "  audited or recorded (e.g., in the review disposition, the attorney's record,\n"
            "  or by saving the clean copy with its review ID).\n"
            f"  Missing patterns: {RUNBOOK_AUDIT_PATTERN.pattern[:120]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 2 — output-contract.md: marker default + clean exit
# ---------------------------------------------------------------------------

# Pattern O1: marker is stated as the default on every generated redline
OUTPUT_MARKER_DEFAULT_PATTERN = re.compile(
    r"(?:marker\s+(?:is\s+)?(?:the\s+)?default|default.{0,80}marker"
    r"|marker\s+remains?\s+(?:the\s+)?default|remains?\s+on\s+every"
    r"|marker\s+on\s+every\s+generated|every\s+generated.{0,80}marker"
    r"|default\s+(?:on|for)\s+(?:every|all)\s+generated)",
    re.IGNORECASE,
)

# Pattern O2: clean export / clean copy is the approval exit
OUTPUT_CLEAN_EXIT_PATTERN = re.compile(
    r"(?:clean.(?:copy|export)|de.mark(?:ed)?\s+cop|export.without.marker"
    r"|clean\s+version|marker.free)"
    r"(?:.|\n){0,600}"
    r"(?:approv(?:al|ed)|deliberate.{0,80}exit|approval\s+exit|exit\s+(?:path|point)"
    r"|after\s+(?:attorney\s+)?approv|intended\s+(?:path|use))",
    re.IGNORECASE,
)

# Alternate: approval exit → clean export
OUTPUT_CLEAN_EXIT_ALT_PATTERN = re.compile(
    r"(?:approv(?:al|ed).{0,200}clean.(?:copy|export)"
    r"|approval\s+exit.{0,200}clean"
    r"|deliberate\s+(?:approval\s+)?exit.{0,200}clean"
    r"|clean.(?:copy|export).{0,200}approv)",
    re.IGNORECASE,
)

# Pattern O3: cross-reference to RUNBOOK for de-marking procedure
OUTPUT_RUNBOOK_XREF_PATTERN = re.compile(
    r"(?:RUNBOOK|runbook)"
    r"(?:.|\n){0,400}"
    r"(?:de.mark|clean.cop|export.marker|removing.the.marker|procedure)",
    re.IGNORECASE,
)

# Alternate cross-reference pattern
OUTPUT_RUNBOOK_XREF_ALT_PATTERN = re.compile(
    r"(?:de.mark|clean.cop|export.marker|removing.the.marker)"
    r"(?:.|\n){0,400}"
    r"(?:RUNBOOK|see\s+RUNBOOK|procedure\s+in|documented\s+in)",
    re.IGNORECASE,
)


def gate_2_output_contract(output_contract_text: str) -> list[str]:
    """docs/output-contract.md: marker is default; clean export is approval exit."""
    failures = []

    if not OUTPUT_MARKER_DEFAULT_PATTERN.search(output_contract_text):
        failures.append(
            "  Gate O1: docs/output-contract.md does not state that the export-warning\n"
            "  marker is the default on every generated redline.\n"
            "  Required: a statement that the marker remains the default (so clean export\n"
            "  is not the default — it is the deliberate approval exit).\n"
            f"  Missing pattern: {OUTPUT_MARKER_DEFAULT_PATTERN.pattern[:120]!r}"
        )

    has_clean_exit = (
        OUTPUT_CLEAN_EXIT_PATTERN.search(output_contract_text)
        or OUTPUT_CLEAN_EXIT_ALT_PATTERN.search(output_contract_text)
    )
    if not has_clean_exit:
        failures.append(
            "  Gate O2: docs/output-contract.md does not state that the clean copy /\n"
            "  de-marked export is the deliberate approval exit path.\n"
            "  Required: state that the clean export (marker-free version) is the\n"
            "  intended / deliberate exit for approved documents — distinct from the\n"
            "  default (marked) state.\n"
            f"  Missing patterns: {OUTPUT_CLEAN_EXIT_PATTERN.pattern[:120]!r}\n"
            f"               and: {OUTPUT_CLEAN_EXIT_ALT_PATTERN.pattern[:120]!r}"
        )

    has_xref = (
        OUTPUT_RUNBOOK_XREF_PATTERN.search(output_contract_text)
        or OUTPUT_RUNBOOK_XREF_ALT_PATTERN.search(output_contract_text)
    )
    if not has_xref:
        failures.append(
            "  Gate O3: docs/output-contract.md does not cross-reference the RUNBOOK\n"
            "  for the de-marking / clean-copy procedure.\n"
            "  Required: a cross-reference (e.g., 'see RUNBOOK.md → Removing the export\n"
            "  marker') so attorneys and operators know where to find the procedure.\n"
            f"  Missing patterns: {OUTPUT_RUNBOOK_XREF_PATTERN.pattern[:120]!r}\n"
            f"               and: {OUTPUT_RUNBOOK_XREF_ALT_PATTERN.pattern[:120]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 3 — threat-model.md: controlled exit reflected
# ---------------------------------------------------------------------------

# Pattern T1: threat-model acknowledges a controlled/deliberate removal path
# after attorney approval (the approval exit)
THREAT_CONTROLLED_EXIT_PATTERN = re.compile(
    r"(?:controlled\s+(?:exit|removal|path)|deliberate\s+(?:exit|removal)"
    r"|approv(?:al|ed)\s+exit|clean\s+exit|clean.copy\s+exit"
    r"|exit\s+path.{0,200}approv"
    r"|approv.{0,200}(?:clean.cop|de.mark|remove.the.marker)"
    r"|after\s+(?:attorney\s+)?approv.{0,200}(?:clean|de.mark|marker))",
    re.IGNORECASE,
)

# Pattern T2: the controlled exit is distinct from unauthorized/accidental removal
# (friction model is preserved)
THREAT_FRICTION_PRESERVED_PATTERN = re.compile(
    r"(?:distinct\s+from|not\s+the\s+same\s+as|separate\s+from"
    r"|accidental.{0,200}(?:authorized|controlled|approved)"
    r"|(?:authorized|controlled|approved).{0,200}accidental"
    r"|friction.{0,400}(?:controlled|approv|authorized)"
    r"|(?:controlled|approv|authorized).{0,400}friction)",
    re.IGNORECASE,
)

# Alternate: the threat model just needs to say the clean/de-marked path is the
# deliberate approval exit while keeping the friction for unapproved docs
THREAT_APPROVAL_EXIT_ALT_PATTERN = re.compile(
    r"(?:clean.cop|de.mark|clean\s+export|marker.free)"
    r"(?:.|\n){0,600}"
    r"(?:approv|deliberate|intended|attorney)",
    re.IGNORECASE,
)


def gate_3_threat_model(threat_text: str) -> list[str]:
    """docs/threat-model.md: controlled exit reflected in external-communication section."""
    failures = []

    has_controlled_exit = (
        THREAT_CONTROLLED_EXIT_PATTERN.search(threat_text)
        or THREAT_APPROVAL_EXIT_ALT_PATTERN.search(threat_text)
    )
    if not has_controlled_exit:
        failures.append(
            "  Gate T1: docs/threat-model.md external-communication guardrail does not\n"
            "  reflect a controlled / deliberate removal path after attorney approval.\n"
            "  Required: state that after attorney approval, there is a deliberate\n"
            "  clean-copy / de-marking exit path (distinct from unauthorized removal),\n"
            "  and that this path is documented in the RUNBOOK.\n"
            f"  Missing patterns: {THREAT_CONTROLLED_EXIT_PATTERN.pattern[:120]!r}\n"
            f"               and: {THREAT_APPROVAL_EXIT_ALT_PATTERN.pattern[:120]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        runbook_text = read_text(RUNBOOK_PATH)
        output_contract_text = read_text(OUTPUT_CONTRACT_PATH)
        threat_text = read_text(THREAT_MODEL_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    g1 = gate_1_runbook(runbook_text)
    g2 = gate_2_output_contract(output_contract_text)
    g3 = gate_3_threat_model(threat_text)

    print(
        "Gate 1: RUNBOOK — de-marking procedure exists (section, cover+header, "
        "approval gate, audit)"
    )
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    else:
        print("  PASS")

    print()
    print(
        "Gate 2: docs/output-contract.md — marker is default; clean export is "
        "approval exit; RUNBOOK xref"
    )
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    print(
        "Gate 3: docs/threat-model.md — controlled exit reflected in "
        "external-communication guardrail"
    )
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
            "See issue #39 for the full remediation plan."
        )
        return 1

    print("PASS: all clean-copy export / de-marking gates satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
