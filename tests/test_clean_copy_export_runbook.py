#!/usr/bin/env python3
"""
CI gate for issue #513: retire the attorney-approval framing and make the
export marker conditional on notes mode.

## What this file used to gate (issue #39, now superseded)

This file used to gate a documented "de-marking ritual" RUNBOOK procedure:
the export marker was unconditional on every generated redline, framed as
"tool recommendation only — attorney approval required", and an attorney
who wanted to send an approved redline externally had to manually strip a
first-page cover note plus a running header/footer from the `.docx` in
Word.

Issue #513 (owner decision, 2026-08-03) retired that framing wholesale.
The premise that justified an always-on marker — a haste-prone reviewer
distinct from an approving attorney — is explicitly withdrawn: the actual
user of this tool is the attorney, or is highly trained. The marker's only
remaining job is a narrower, honest one: a signpost that a generated
document carries internal-audience notes (per-review notes mode) and is
therefore not for external transmission. A review with no internal notes
in scope produces a document with **no marker in any part**, and there is
deliberately **no manual de-marking procedure** — stripping the marker
text would not remove the internal-audience content the notes mode
actually put in the document, so it would make an unsafe document merely
LOOK safe.

## What this file gates now

Four gates, matching issue #513's acceptance criteria:

  GATE 1 — RUNBOOK.md: the internal-notes marker is documented accurately
    - a named section describing the marker
    - states the marker is conditional on notes mode / internal notes
    - states there is no manual de-marking / marker-stripping procedure
    - does not tie the marker (or anything else) to a requirement for
      attorney approval
    - the retired literal marker string is gone

  GATE 2 — docs/output-contract.md: marker is conditional, not the default;
    no approval semantics; cross-references RUNBOOK/threat-model
    - states the marker is present iff internal notes are in scope
    - states it carries no approval semantics / nothing is "required"
    - cross-references RUNBOOK.md or docs/threat-model.md for detail
    - the retired literal marker string is gone

  GATE 3 — docs/threat-model.md: trained-user premise, conditional marker
    - states the withdrawn haste-prone-reviewer premise / the actual user
      being the attorney or highly trained
    - describes the marker as conditional on notes mode
    - the retired literal marker string is gone

  GATE 4 — regression sweep across every other shipped AC1 surface (fix
    round 1, issue #513): ARCHITECTURE.md, README.md, docs/REVIEW-GUIDE.md,
    docs/phase-0-issues.md, playbooks/schema.json,
    frontend/public/manifest.json, and the committed mock fixture
    (infra/fixtures/mock-outputs/eiaa/pre-baked-redline.docx, unzipped) --
    none of these may state or imply that the product requires or enforces
    attorney approval, or carry the retired literal marker string. AC1 says
    "UI, generated .docx, docs, RUNBOOK" -- Gates 1-3 only ever spot-checked
    three docs, which is exactly how the attorney-approval framing survived
    in the fixture .docx, the PWA manifest, two infra stack headers, and a
    phase-0 issue doc through a prior fix round.

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"
OUTPUT_CONTRACT_PATH = REPO_ROOT / "docs" / "output-contract.md"
THREAT_MODEL_PATH = REPO_ROOT / "docs" / "threat-model.md"

# Gate 4's widened surface set (fix round 1, issue #513 finding 8). Plain
# text files are read directly; the fixture is a .docx (a zip of XML parts)
# and is unzipped and text-extracted instead -- see _extract_docx_text.
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
README_PATH = REPO_ROOT / "README.md"
REVIEW_GUIDE_PATH = REPO_ROOT / "docs" / "REVIEW-GUIDE.md"
PHASE_0_ISSUES_PATH = REPO_ROOT / "docs" / "phase-0-issues.md"
PLAYBOOKS_SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"
MANIFEST_PATH = REPO_ROOT / "frontend" / "public" / "manifest.json"
FIXTURE_DOCX_PATH = (
    REPO_ROOT / "infra" / "fixtures" / "mock-outputs" / "eiaa" / "pre-baked-redline.docx"
)

# The retired literal marker string (scripts/redline_docx_writer.py's old
# MARKER_TEXT, pre-#513), plus its short form (the leading clause quoted on
# its own elsewhere in these docs, e.g. the old attorney-approval watermark
# copy). Its continued presence anywhere in these three docs would mean a
# doc still quotes text the generated `.docx` no longer contains -- checked
# as plain substrings (not regex) since it must match byte-for-byte or not
# at all. Both the long two-clause form and the short form are checked in
# both em-dash and hyphen spellings.
_RETIRED_MARKER_STRINGS = (
    "tool recommendation only — attorney approval required; do not send externally before attorney approval",
    "tool recommendation only - attorney approval required; do not send externally before attorney approval",
    "tool recommendation only — attorney approval required",
    "tool recommendation only - attorney approval required",
)

# Gate 4's broader denylist (fix round 1, issue #513 finding 8). These are
# not the one specific literal MARKER_TEXT string above -- they are the
# other attorney-approval-requirement phrasings that actually shipped on
# surfaces Gates 1-3 never look at: the fixture's tracked-change insertion
# ("Attorney approval required; do not rely on this document."), the PWA
# manifest description ("...that still needs attorney approval."), and the
# infra stack-header/doc comments ("...carries attorney-approval watermark
# ..." / "...attorney-approval watermark and the ACCEPT framing"). Checked
# case-insensitively since capitalization varies by surface.
_ATTORNEY_APPROVAL_REQUIREMENT_PHRASES = (
    "attorney approval required",
    "needs attorney approval",
    "requires attorney approval",
    "attorney-approval watermark",
    "attorney approval watermark",
)


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


def _extract_docx_text(docx_path: Path) -> str:
    """Unzip a .docx and concatenate the text content of every XML part
    (document, headers, footers, footnotes, ...) with tags stripped, so
    Gate 4 can substring-scan the fixture's actual rendered content rather
    than trusting the generator script that produced it."""
    if not docx_path.exists():
        raise FileNotFoundError(f"Required file missing: {docx_path}")
    with zipfile.ZipFile(docx_path, "r") as z:
        parts = [
            z.read(name).decode("utf-8", errors="replace")
            for name in z.namelist()
            if name.startswith("word/") and name.endswith(".xml")
        ]
    return "\n".join(re.sub(r"<[^>]+>", " ", part) for part in parts)


def _check_retired_string_gone(text: str, doc_name: str) -> list[str]:
    failures = []
    for retired in _RETIRED_MARKER_STRINGS:
        if retired in text:
            failures.append(
                f"  Retired-string check: {doc_name} still contains the retired literal "
                f"marker string {retired!r}. The generated .docx no longer contains this "
                f"text (issue #513) -- quoting it here is now simply false."
            )
    return failures


def _check_no_approval_requirement_framing(text: str, doc_name: str) -> list[str]:
    """Gate 4's broader check: doc_name must not state or imply, in any
    phrasing, that the product requires or enforces attorney approval."""
    failures = []
    lowered = text.lower()
    for phrase in _ATTORNEY_APPROVAL_REQUIREMENT_PHRASES:
        if phrase in lowered:
            failures.append(
                f"  AC1 sweep: {doc_name} still contains the retired approval-requirement "
                f"phrase {phrase!r} (issue #513 AC1: no shipped surface may state or imply "
                f"the product requires or enforces attorney approval)."
            )
    return failures


# ---------------------------------------------------------------------------
# GATE 1 — RUNBOOK.md: internal-notes marker documented accurately
# ---------------------------------------------------------------------------

# Pattern R1: RUNBOOK has a section/heading about the marker.
RUNBOOK_MARKER_SECTION_PATTERN = re.compile(
    r"(?:###?\s+(?:Internal.notes\s+marker|Export\s+marker|Removing\s+the\s+export\s+marker"
    r"|The\s+export\s+marker))",
    re.IGNORECASE,
)

# Pattern R2: RUNBOOK ties the marker's presence to notes mode / internal notes.
RUNBOOK_CONDITIONAL_PATTERN = re.compile(
    r"(?:notes\s+mode)(?:.|\n){0,400}(?:internal)"
    r"|(?:internal)(?:.|\n){0,400}(?:notes\s+mode)"
    r"|iff(?:.|\n){0,120}internal\s+notes"
    r"|internal\s+notes(?:.|\n){0,120}(?:in\s+scope|carries?\s+internal)",
    re.IGNORECASE,
)

# Pattern R3: RUNBOOK states there is NO manual de-marking / stripping procedure.
RUNBOOK_NO_PROCEDURE_PATTERN = re.compile(
    r"(?:no\s+(?:de.mark|manual\s+de.mark)|not\s+.{0,40}strip"
    r"|no\s+de.marking\s+procedure|none\s+is\s+coming"
    r"|do\s+not\s+edit\s+a\s+marked)",
    re.IGNORECASE,
)

# Pattern R4: RUNBOOK does not tie the marker (or anything) to a REQUIREMENT
# for attorney approval. This is a positive statement that no such
# requirement exists -- distinct from a bare absence check, which would also
# reject an accurate NEGATION ("does not require attorney approval").
RUNBOOK_NO_APPROVAL_REQUIREMENT_PATTERN = re.compile(
    r"(?:no(?:thing)?\s+.{0,60}requires?\s+or\s+enforces?\s+attorney\s+approval"
    r"|carries?\s+no\s+approval\s+semantics"
    r"|does\s+not\s+(?:require|enforce)\s+attorney\s+approval)",
    re.IGNORECASE,
)


def gate_1_runbook(runbook_text: str) -> list[str]:
    """RUNBOOK.md must document the marker as a conditional, honest
    signpost with no de-marking ritual and no approval requirement."""
    failures = _check_retired_string_gone(runbook_text, "RUNBOOK.md")

    if not RUNBOOK_MARKER_SECTION_PATTERN.search(runbook_text):
        failures.append(
            "  Gate R1: RUNBOOK.md does not contain a named section describing the\n"
            "  export/internal-notes marker (e.g. '### Internal-notes marker on a\n"
            "  generated redline').\n"
            f"  Missing pattern: {RUNBOOK_MARKER_SECTION_PATTERN.pattern[:120]!r}"
        )

    if not RUNBOOK_CONDITIONAL_PATTERN.search(runbook_text):
        failures.append(
            "  Gate R2: RUNBOOK.md does not tie the marker's presence to notes mode /\n"
            "  internal notes being in scope for the review.\n"
            "  Required: state that the marker is present iff this review's notes mode\n"
            "  put internal-audience content in scope.\n"
            f"  Missing pattern: {RUNBOOK_CONDITIONAL_PATTERN.pattern[:160]!r}"
        )

    if not RUNBOOK_NO_PROCEDURE_PATTERN.search(runbook_text):
        failures.append(
            "  Gate R3: RUNBOOK.md does not state that there is no manual de-marking /\n"
            "  marker-stripping procedure.\n"
            "  Required: state that a marked document is never edited to look\n"
            "  external-safe -- there is no supported de-marking ritual.\n"
            f"  Missing pattern: {RUNBOOK_NO_PROCEDURE_PATTERN.pattern[:120]!r}"
        )

    if not RUNBOOK_NO_APPROVAL_REQUIREMENT_PATTERN.search(runbook_text):
        failures.append(
            "  Gate R4: RUNBOOK.md does not state that nothing requires or enforces\n"
            "  attorney approval.\n"
            "  Required: an explicit statement that the marker (and the product) does\n"
            "  not require or enforce attorney approval.\n"
            f"  Missing pattern: {RUNBOOK_NO_APPROVAL_REQUIREMENT_PATTERN.pattern[:160]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 2 — docs/output-contract.md: conditional marker, no approval semantics
# ---------------------------------------------------------------------------

# Pattern O1: marker present iff internal notes in scope (not the default).
OUTPUT_CONDITIONAL_PATTERN = re.compile(
    r"(?:iff|if\s+and\s+only\s+if)(?:.|\n){0,120}internal"
    r"|conditional\s+on\s+notes\s+mode"
    r"|present\s+iff",
    re.IGNORECASE,
)

# Pattern O2: no approval semantics.
OUTPUT_NO_APPROVAL_SEMANTICS_PATTERN = re.compile(
    r"no\s+approval\s+semantics"
    r"|does\s+not\s+(?:require|enforce)\s+attorney\s+approval"
    r"|nothing\s+.{0,60}(?:require|enforce|gate|record)s?\s+attorney\s+approval",
    re.IGNORECASE,
)

# Pattern O3: cross-reference to RUNBOOK or threat-model for detail.
OUTPUT_XREF_PATTERN = re.compile(
    r"RUNBOOK\.md|threat-model\.md",
    re.IGNORECASE,
)


def gate_2_output_contract(output_contract_text: str) -> list[str]:
    """docs/output-contract.md: marker is conditional, not the default; no
    approval semantics; cross-referenced for detail."""
    failures = _check_retired_string_gone(output_contract_text, "docs/output-contract.md")

    if not OUTPUT_CONDITIONAL_PATTERN.search(output_contract_text):
        failures.append(
            "  Gate O1: docs/output-contract.md does not state that the marker is\n"
            "  present iff internal notes are in scope (i.e. that it is conditional,\n"
            "  not the default on every redline).\n"
            f"  Missing pattern: {OUTPUT_CONDITIONAL_PATTERN.pattern[:160]!r}"
        )

    if not OUTPUT_NO_APPROVAL_SEMANTICS_PATTERN.search(output_contract_text):
        failures.append(
            "  Gate O2: docs/output-contract.md does not state that the marker (or the\n"
            "  product generally) carries no approval semantics / does not require or\n"
            "  enforce attorney approval.\n"
            f"  Missing pattern: {OUTPUT_NO_APPROVAL_SEMANTICS_PATTERN.pattern[:160]!r}"
        )

    if not OUTPUT_XREF_PATTERN.search(output_contract_text):
        failures.append(
            "  Gate O3: docs/output-contract.md does not cross-reference RUNBOOK.md or\n"
            "  docs/threat-model.md for the marker's operational/threat detail.\n"
            f"  Missing pattern: {OUTPUT_XREF_PATTERN.pattern[:120]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 3 — docs/threat-model.md: trained-user premise, conditional marker
# ---------------------------------------------------------------------------

# Pattern T1: the withdrawn haste-prone-reviewer premise / trained-user framing.
THREAT_TRAINED_USER_PATTERN = re.compile(
    r"actual\s+user\s+of\s+this\s+tool\s+is\s+the\s+attorney"
    r"|highly\s+trained"
    r"|premise\s+is\s+explicitly\s+withdrawn"
    r"|superseded\s+framing\s+withdrawn",
    re.IGNORECASE,
)

# Pattern T2: marker described as conditional on notes mode / internal notes.
THREAT_CONDITIONAL_PATTERN = re.compile(
    r"(?:iff|if\s+and\s+only\s+if)(?:.|\n){0,120}internal"
    r"|notes\s+mode(?:.|\n){0,300}internal"
    r"|internal(?:.|\n){0,300}notes\s+mode",
    re.IGNORECASE,
)


def gate_3_threat_model(threat_text: str) -> list[str]:
    """docs/threat-model.md: the trained-user premise replaces the withdrawn
    haste-prone-reviewer framing, and the marker is described as
    conditional on notes mode."""
    failures = _check_retired_string_gone(threat_text, "docs/threat-model.md")

    if not THREAT_TRAINED_USER_PATTERN.search(threat_text):
        failures.append(
            "  Gate T1: docs/threat-model.md does not state the trained-user premise\n"
            "  (the actual user of this tool is the attorney, or is highly trained) or\n"
            "  that the earlier haste-prone-reviewer premise is withdrawn.\n"
            f"  Missing pattern: {THREAT_TRAINED_USER_PATTERN.pattern[:160]!r}"
        )

    if not THREAT_CONDITIONAL_PATTERN.search(threat_text):
        failures.append(
            "  Gate T2: docs/threat-model.md does not describe the export marker as\n"
            "  conditional on notes mode / internal notes being in scope.\n"
            f"  Missing pattern: {THREAT_CONDITIONAL_PATTERN.pattern[:160]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 4 — regression sweep across every other shipped AC1 surface
# ---------------------------------------------------------------------------

# (path, human-readable name) pairs for every plain-text surface Gate 4
# sweeps, beyond the three docs Gates 1-3 already cover.
_GATE_4_TEXT_SURFACES = (
    (ARCHITECTURE_PATH, "ARCHITECTURE.md"),
    (README_PATH, "README.md"),
    (REVIEW_GUIDE_PATH, "docs/REVIEW-GUIDE.md"),
    (PHASE_0_ISSUES_PATH, "docs/phase-0-issues.md"),
    (PLAYBOOKS_SCHEMA_PATH, "playbooks/schema.json"),
    (MANIFEST_PATH, "frontend/public/manifest.json"),
)


def gate_4_additional_surfaces() -> list[str]:
    """AC1 says "UI, generated .docx, docs, RUNBOOK" -- sweep every other
    shipped surface that AC1 covers but Gates 1-3 don't reach: the
    remaining docs, the PWA manifest, and the committed mock fixture
    (unzipped), for both the retired literal marker string and the broader
    attorney-approval-requirement phrasing."""
    failures: list[str] = []

    for path, name in _GATE_4_TEXT_SURFACES:
        try:
            text = read_text(path)
        except FileNotFoundError as e:
            failures.append(f"  {e}")
            continue
        failures.extend(_check_retired_string_gone(text, name))
        failures.extend(_check_no_approval_requirement_framing(text, name))

    fixture_name = "infra/fixtures/mock-outputs/eiaa/pre-baked-redline.docx"
    try:
        fixture_text = _extract_docx_text(FIXTURE_DOCX_PATH)
    except (FileNotFoundError, zipfile.BadZipFile, KeyError) as e:
        failures.append(f"  {fixture_name}: could not read/unzip fixture -- {e}")
    else:
        failures.extend(_check_retired_string_gone(fixture_text, fixture_name))
        failures.extend(_check_no_approval_requirement_framing(fixture_text, fixture_name))

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
    g4 = gate_4_additional_surfaces()

    print(
        "Gate 1: RUNBOOK.md — internal-notes marker documented accurately "
        "(section, conditional, no de-marking ritual, no approval requirement)"
    )
    if g1:
        for f in g1:
            print(f)
        all_failures.extend(g1)
    else:
        print("  PASS")

    print()
    print(
        "Gate 2: docs/output-contract.md — marker conditional, not default; "
        "no approval semantics; cross-referenced"
    )
    if g2:
        for f in g2:
            print(f)
        all_failures.extend(g2)
    else:
        print("  PASS")

    print()
    print(
        "Gate 3: docs/threat-model.md — trained-user premise; marker conditional "
        "on notes mode"
    )
    if g3:
        for f in g3:
            print(f)
        all_failures.extend(g3)
    else:
        print("  PASS")

    print()
    print(
        "Gate 4: regression sweep (ARCHITECTURE.md, README.md, "
        "docs/REVIEW-GUIDE.md, docs/phase-0-issues.md, playbooks/schema.json, "
        "frontend/public/manifest.json, mock fixture .docx) — no retired "
        "marker string, no approval-requirement framing"
    )
    if g4:
        for f in g4:
            print(f)
        all_failures.extend(g4)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #513 for the full remediation plan."
        )
        return 1

    print("PASS: attorney-approval framing retired; export marker is conditional (issue #513).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
