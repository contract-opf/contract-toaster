#!/usr/bin/env python3
"""
CI gate for issue #40: wrong-format rejection UX (PDF intake reality).

Three axes of coverage, matching the issue's TDD plan:

  AXIS A — Format-specific rejection copy in ARCHITECTURE.md (frontend section)
    The frontend section must describe a format-specific rejection path for
    PDF and .doc uploads that is distinct from the generic hostile-file
    rejection.  The copy for PDF and .doc must be tailored (not generic),
    and the v1 .docx-only scope decision must be recorded.

  AXIS B — Reviewer guidance documented (README.md and/or RUNBOOK.md)
    Reviewers must know what to do when a school sends a PDF:
      - Request the .docx original from the school
      - Conversion guidance and its tracked-changes caveats are noted
    At least one of README.md or RUNBOOK.md must carry this guidance.

  AXIS C — v1 .docx-only scope decision recorded in docs/design-notes.md
    docs/design-notes.md must record the decision to keep v1 intake as
    .docx-only and document why (PDF acceptance deferred).

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
README_PATH = REPO_ROOT / "README.md"
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"
DESIGN_NOTES_PATH = REPO_ROOT / "docs" / "design-notes.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AXIS A — Format-specific rejection copy
# ---------------------------------------------------------------------------
#
# ARCHITECTURE.md frontend section must:
#   A1. Name PDF and/or .doc as wrong-format cases that produce tailored
#       rejection messages (not the generic hostile-file error).
#   A2. Distinguish the format-specific path from the hostile-file/generic path.
#   A3. Record that v1 accepts .docx only and that PDF/old-.doc intake is
#       deliberately deferred (scope decision).

# Pattern A1: PDF and/or legacy .doc named alongside format-specific rejection
ARCH_PDF_REJECTION_PATTERN = re.compile(
    r"(?:PDF|\.pdf)"
    r"(?:.|\n){0,800}"
    r"(?:format.specific|tailored|wrong.format|incorrect.format"
    r"|not\s+(?:a\s+)?\.docx|only\s+accept|\.docx.only"
    r"|request.{0,60}\.docx|send.{0,60}Word)",
    re.IGNORECASE,
)

# Pattern A1b: .doc (legacy Word) named alongside format-specific rejection
ARCH_DOC_REJECTION_PATTERN = re.compile(
    r"(?:\.doc\b|legacy\s+Word|old.{0,20}format)"
    r"(?:.|\n){0,800}"
    r"(?:format.specific|tailored|wrong.format|incorrect.format"
    r"|convert|\.docx.only|not\s+(?:a\s+)?\.docx|request.{0,60}\.docx)",
    re.IGNORECASE,
)

# Pattern A2: format-specific rejection distinguished from hostile-file/generic error
ARCH_DISTINCT_FROM_HOSTILE_PATTERN = re.compile(
    r"(?:format.specific|wrong.format|PDF.rejection|\.pdf.rejection"
    r"|format.mismatch|unsupported.format)"
    r"(?:.|\n){0,800}"
    r"(?:distinct|different|separate|not.{0,60}generic|not.{0,60}hostile"
    r"|hostile.file|generic.error|malicious|security)",
    re.IGNORECASE,
)

# Alternative A2: any statement that PDF gets its own rejection copy (not generic hostile)
ARCH_PDF_OWN_COPY_PATTERN = re.compile(
    r"(?:PDF|\.pdf|\.doc\b)"
    r"(?:.|\n){0,400}"
    r"(?:tailored\s+(?:copy|message|error|rejection)"
    r"|format.specific\s+(?:copy|message|error|rejection)"
    r"|own\s+(?:copy|message|error|rejection)"
    r"|specific\s+(?:copy|message|guidance|error)"
    r"|separate\s+(?:copy|message|error|rejection))",
    re.IGNORECASE,
)

# Pattern A3: v1 .docx-only scope decision recorded in ARCHITECTURE.md
ARCH_DOCX_ONLY_SCOPE_PATTERN = re.compile(
    r"(?:v1|version\s+1|scope)"
    r"(?:.|\n){0,600}"
    r"(?:\.docx.only|docx.only|accepts?\s+only\s+\.docx|\.docx\s+only"
    r"|PDF\s+(?:intake\s+)?deferred|PDF\s+(?:support\s+)?deferred"
    r"|no\s+PDF|PDF\s+not\s+supported)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# AXIS B — Reviewer guidance in README.md or RUNBOOK.md
# ---------------------------------------------------------------------------
#
# At least one of README.md / RUNBOOK.md must:
#   B1. Tell reviewers to request the .docx original when a school sends a PDF.
#   B2. Note conversion guidance and/or tracked-changes caveats for conversions.

# Pattern B1: guidance to request the .docx original from the school
REVIEWER_REQUEST_DOCX_PATTERN = re.compile(
    r"(?:request|ask|contact|obtain).{0,200}"
    r"(?:\.docx|Word\s+(?:file|document|version|format)|original)"
    r"(?:.|\n){0,400}"
    r"(?:school|counterparty|partner|institution)",
    re.IGNORECASE,
)

# Alternative B1: school asks for Word file
REVIEWER_ASK_SCHOOL_PATTERN = re.compile(
    r"(?:school|counterparty|partner|institution).{0,400}"
    r"(?:request|ask|send|provide|share).{0,200}"
    r"(?:\.docx|Word\s+(?:file|document|version|format)|original)",
    re.IGNORECASE,
)

# Pattern B2: conversion guidance / tracked-changes caveats
REVIEWER_CONVERSION_CAVEATS_PATTERN = re.compile(
    r"(?:convert|conversion|PDF.to.docx|docx.to.pdf|re.type|retype)"
    r"(?:.|\n){0,600}"
    r"(?:tracked.changes?|track\s+changes?|revision\s+mark|revision\s+history"
    r"|caveat|caution|warn|note|limitation|lost)",
    re.IGNORECASE,
)

# Alternative B2: any mention that conversion loses tracked-changes fidelity
REVIEWER_CONVERSION_ALT_PATTERN = re.compile(
    r"(?:tracked.changes?|track\s+changes?)"
    r"(?:.|\n){0,400}"
    r"(?:convert|conversion|PDF|lost|strip|lost\s+in|not\s+preserved)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# AXIS C — v1 .docx-only scope decision in docs/design-notes.md
# ---------------------------------------------------------------------------
#
# docs/design-notes.md must record that v1 intake is .docx-only and that PDF
# intake was considered but deferred.

# Pattern C1: design-notes records v1 is .docx-only
DESIGN_NOTES_DOCX_ONLY_PATTERN = re.compile(
    r"(?:v1|version\s+1|scope|intake)"
    r"(?:.|\n){0,600}"
    r"(?:\.docx.only|docx.only|\.docx\s+only|accepts?\s+only\s+\.docx"
    r"|PDF\s+(?:intake\s+)?deferred|PDF\s+not\s+in\s+scope"
    r"|no\s+PDF\s+(?:support|intake)|PDF\s+support\s+deferred)",
    re.IGNORECASE,
)

# Pattern C2: rationale or decision about deferring PDF
DESIGN_NOTES_PDF_DEFERRED_PATTERN = re.compile(
    r"(?:PDF|\.pdf)"
    r"(?:.|\n){0,800}"
    r"(?:deferred|out.of.scope|not.in.scope|v1.scope|future|later"
    r"|decision|rationale|why\s+not|intentionally)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------

def gate_a_format_specific_rejection(arch_text: str) -> list[str]:
    """ARCHITECTURE.md frontend must describe format-specific PDF/.doc rejection."""
    failures = []

    # A1: PDF named as a wrong-format case
    has_pdf_rejection = (
        ARCH_PDF_REJECTION_PATTERN.search(arch_text)
        or ARCH_PDF_OWN_COPY_PATTERN.search(arch_text)
    )
    if not has_pdf_rejection:
        failures.append(
            "  Gate A1: ARCHITECTURE.md frontend section does not name PDF as a\n"
            "  wrong-format case with tailored rejection copy.\n"
            "  Required: ARCHITECTURE.md must state that a PDF upload receives a\n"
            "  format-specific rejection message (distinct from the generic\n"
            "  hostile-file error) with guidance such as requesting the .docx\n"
            "  original from the school.\n"
            f"  Missing pattern: {ARCH_PDF_REJECTION_PATTERN.pattern[:120]!r}"
        )

    # A1b: .doc (legacy Word) named as a wrong-format case
    has_doc_rejection = (
        ARCH_DOC_REJECTION_PATTERN.search(arch_text)
        or ARCH_PDF_OWN_COPY_PATTERN.search(arch_text)
    )
    if not has_doc_rejection:
        failures.append(
            "  Gate A1b: ARCHITECTURE.md frontend section does not name .doc (legacy\n"
            "  Word) as a wrong-format case with tailored rejection copy.\n"
            "  Required: ARCHITECTURE.md must state that a .doc upload (legacy Word\n"
            "  format) receives a format-specific rejection message with conversion\n"
            "  or request guidance, distinct from the hostile-file path.\n"
            f"  Missing pattern: {ARCH_DOC_REJECTION_PATTERN.pattern[:120]!r}"
        )

    # A2: format-specific path distinguished from hostile-file/generic error
    has_distinct = (
        ARCH_DISTINCT_FROM_HOSTILE_PATTERN.search(arch_text)
        or ARCH_PDF_OWN_COPY_PATTERN.search(arch_text)
    )
    if not has_distinct:
        failures.append(
            "  Gate A2: ARCHITECTURE.md does not distinguish the format-specific\n"
            "  rejection path from the generic hostile-file rejection.\n"
            "  Required: ARCHITECTURE.md must make clear that a PDF or .doc gets\n"
            "  a tailored, format-specific rejection message — not the same generic\n"
            "  hostile-file error produced by a zip-bomb or macro-laden file.\n"
            f"  Missing pattern: {ARCH_DISTINCT_FROM_HOSTILE_PATTERN.pattern[:120]!r}"
        )

    # A3: v1 .docx-only scope decision recorded in ARCHITECTURE.md
    if not ARCH_DOCX_ONLY_SCOPE_PATTERN.search(arch_text):
        failures.append(
            "  Gate A3: ARCHITECTURE.md does not record the v1 .docx-only scope\n"
            "  decision (PDF/legacy intake deferred).\n"
            "  Required: ARCHITECTURE.md must state that v1 accepts .docx only and\n"
            "  that PDF intake is deliberately out of scope for v1.\n"
            f"  Missing pattern: {ARCH_DOCX_ONLY_SCOPE_PATTERN.pattern[:120]!r}"
        )

    return failures


def gate_b_reviewer_guidance(readme_text: str, runbook_text: str) -> list[str]:
    """README.md or RUNBOOK.md must document what to do when a school sends a PDF."""
    failures = []
    combined = readme_text + "\n\n" + runbook_text

    # B1: guidance to request the .docx original
    has_request_docx = (
        REVIEWER_REQUEST_DOCX_PATTERN.search(combined)
        or REVIEWER_ASK_SCHOOL_PATTERN.search(combined)
    )
    if not has_request_docx:
        failures.append(
            "  Gate B1: README.md / RUNBOOK.md does not tell reviewers to request\n"
            "  the .docx original when a school sends a PDF.\n"
            "  Required: at least one of README.md or RUNBOOK.md must instruct\n"
            "  reviewers to ask the school for the Word (.docx) original when a\n"
            "  counterparty submits a PDF.\n"
            f"  Missing pattern A: {REVIEWER_REQUEST_DOCX_PATTERN.pattern[:120]!r}\n"
            f"  Missing pattern B: {REVIEWER_ASK_SCHOOL_PATTERN.pattern[:120]!r}"
        )

    # B2: conversion guidance and tracked-changes caveats
    has_conversion_caveats = (
        REVIEWER_CONVERSION_CAVEATS_PATTERN.search(combined)
        or REVIEWER_CONVERSION_ALT_PATTERN.search(combined)
    )
    if not has_conversion_caveats:
        failures.append(
            "  Gate B2: README.md / RUNBOOK.md does not document conversion guidance\n"
            "  or tracked-changes caveats when a .docx is obtained via PDF conversion.\n"
            "  Required: at least one of README.md or RUNBOOK.md must note that PDF-to-\n"
            "  .docx conversion can lose tracked-changes fidelity, and/or warn about\n"
            "  the caveats of using a converted document for review.\n"
            f"  Missing pattern A: {REVIEWER_CONVERSION_CAVEATS_PATTERN.pattern[:120]!r}\n"
            f"  Missing pattern B: {REVIEWER_CONVERSION_ALT_PATTERN.pattern[:120]!r}"
        )

    return failures


def gate_c_design_notes_scope_decision(design_notes_text: str) -> list[str]:
    """docs/design-notes.md must record the v1 .docx-only scope decision."""
    failures = []

    # C1: v1 .docx-only scope noted
    if not DESIGN_NOTES_DOCX_ONLY_PATTERN.search(design_notes_text):
        failures.append(
            "  Gate C1: docs/design-notes.md does not record that v1 intake is\n"
            "  .docx-only.\n"
            "  Required: docs/design-notes.md must state that v1 accepts .docx only\n"
            "  and that PDF intake is deferred / not in scope for v1.\n"
            f"  Missing pattern: {DESIGN_NOTES_DOCX_ONLY_PATTERN.pattern[:120]!r}"
        )

    # C2: rationale for deferring PDF
    if not DESIGN_NOTES_PDF_DEFERRED_PATTERN.search(design_notes_text):
        failures.append(
            "  Gate C2: docs/design-notes.md does not record a rationale for deferring\n"
            "  PDF intake.\n"
            "  Required: docs/design-notes.md must explain why PDF intake is deferred\n"
            "  (e.g. v1 scope, format constraints, conversion fidelity concerns).\n"
            f"  Missing pattern: {DESIGN_NOTES_PDF_DEFERRED_PATTERN.pattern[:120]!r}"
        )

    return failures


def main() -> int:
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
        readme_text = read_text(README_PATH)
        runbook_text = read_text(RUNBOOK_PATH)
        design_notes_text = read_text(DESIGN_NOTES_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    g_a = gate_a_format_specific_rejection(arch_text)
    g_b = gate_b_reviewer_guidance(readme_text, runbook_text)
    g_c = gate_c_design_notes_scope_decision(design_notes_text)

    print("Gate A: Format-specific rejection copy in ARCHITECTURE.md (PDF / .doc)")
    if g_a:
        for f in g_a:
            print(f)
        all_failures.extend(g_a)
    else:
        print("  PASS")

    print()
    print("Gate B: Reviewer guidance in README.md / RUNBOOK.md")
    if g_b:
        for f in g_b:
            print(f)
        all_failures.extend(g_b)
    else:
        print("  PASS")

    print()
    print("Gate C: v1 .docx-only scope decision in docs/design-notes.md")
    if g_c:
        for f in g_c:
            print(f)
        all_failures.extend(g_c)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #40 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all wrong-format rejection UX gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
