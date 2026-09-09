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

## What issue #524 added (epic #519 item G)

Issue #513 retired the framing; the rest of epic #519 then built the
machinery the docs had been describing prematurely. Item G reconciles the
two, and three more gates hold that reconciliation in place:

  GATE 5 — RUNBOOK.md: the marker section states what a ship-ready
    document actually CONTAINS, per notes mode
    - names all four notes modes (`none`/`external`/`internal`/`both`) in
      that section, with what each one puts in the delivered `.docx`
    - says footnotes survive accept-all -- the `<w:footnoteReference>` sits
      inside `<w:ins>`, so accepting all tracked changes PROMOTES a
      footnote to body text rather than removing it. Without this, "accept
      all and send" reads like a clean-copy procedure; it is not one.
    - does not promise a first-page cover note on ANY path: issue #629
      moved third-party paper onto the shared block compiler and issue
      #631 deleted the standalone writer that was the last emitter, so
      every delivered redline takes the header/footer placement
    - says the download filename carries no internal-notes signpost
      (`backend/src/download.py::redline_filename_for` emits
      `<stem>-redline.docx` in every mode; the filename half of epic #519
      item E did not land -- `frontend/src/notesMode.ts` says so too)

  GATE 6 — docs/threat-model.md: the leakage-scan categories describe
    #522's machinery, and #521's citation decision is recorded
    - the pre-#522 claim "Any rationale marked internal-only is held back
      from the external-facing footnote" is gone: nothing filters an
      internal-marked rationale out of the counterparty-facing footnote.
      What shipped is a SEPARATE field (`internal_rationale_for_footnote`)
      solicited only in `internal`/`both` and rendered only behind the
      `[INTERNAL]` marking.
    - records #521's decision at threat-model level: on the internal
      channel `citation_leakage` PERMITS naming a past counterparty and
      citing a past deal (`scripts/leakage_scan.py` check 3), which is a
      statement about what an internal-notes artifact IS -- a document that
      may carry a third party's confidential terms -- and the justification
      for keeping the export marker at all.

  GATE 7 — ARCHITECTURE.md: documented marker placement matches the code
    path being described
    - no "redundant export marker" framing: the marker is conditional on
      the review's notes mode (`include_marker`), not belt-and-braces
    - every "cover note" mention is marked RETIRED. No shipping path
      emits one: `redline_generate.inject_export_marker_and_footnotes`
      (reached by first-party and third-party paper alike) emits header +
      footer ONLY, and the standalone writer that placed a cover note was
      deleted by issue #631 -- so an unqualified cover-note claim is false
      for every redline anyone can obtain.

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

# The retired literal marker string (the standalone writer's old
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
# GATE 5 — RUNBOOK.md: what a ship-ready document contains, per notes mode
# ---------------------------------------------------------------------------

# The RUNBOOK heading Gate 1 already requires. Gate 5's checks are scoped to
# THAT section rather than the whole file: "the word `both` appears somewhere
# in a 2,000-line runbook" proves nothing about the marker procedure, and the
# operator reading this section must not have to hunt elsewhere for what the
# document in their hands actually contains.
RUNBOOK_MARKER_SECTION_SLICE = re.compile(
    r"^###\s+(?:Internal.notes\s+marker|Export\s+marker|The\s+export\s+marker)[^\n]*\n"
    r"(?P<body>(?:.|\n)*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern R5b: footnotes survive accept-all (promoted to body text), so
# "accept all changes and send it" is not a clean-copy procedure.
RUNBOOK_ACCEPT_ALL_PROMOTION_PATTERN = re.compile(
    r"accept(?:ing)?[- ]all(?:.|\n){0,300}(?:promot|does\s+not\s+remove|survive)"
    r"|(?:promot|survive)(?:.|\n){0,300}accept(?:ing)?[- ]all",
    re.IGNORECASE,
)

# Pattern R5c: the download filename is NOT a signpost. Documenting a
# safeguard the pipeline does not apply is the failure this whole file
# exists to prevent -- `backend/src/download.py::redline_filename_for`
# returns `<stem>-redline.docx` in every notes mode.
RUNBOOK_FILENAME_NOT_A_SIGNPOST_PATTERN = re.compile(
    r"filename(?:.|\n){0,300}(?:no\s+signpost|carries\s+no|is\s+not\s+a\s+signpost"
    r"|does\s+not\s+(?:say|signal|indicate|change))"
    r"|(?:no\s+signpost|not\s+a\s+signpost)(?:.|\n){0,300}filename",
    re.IGNORECASE,
)

# The four notes-mode ids, as the operator will see them written.
_NOTES_MODE_IDS = ("`none`", "`external`", "`internal`", "`both`")


def gate_5_runbook_ship_ready(runbook_text: str) -> list[str]:
    """RUNBOOK.md's marker section must state what a ship-ready document
    CONTAINS under each notes mode (issue #524) -- the marker alone does not
    answer that, and the earlier procedure never mentioned footnotes at all
    even though accept-all preserves them."""
    failures: list[str] = []

    match = RUNBOOK_MARKER_SECTION_SLICE.search(runbook_text)
    if match is None:
        return [
            "  Gate R5: could not locate the RUNBOOK.md internal-notes marker section\n"
            "  to check it (Gate 1 covers the heading itself)."
        ]
    section = match.group("body")

    missing_modes = [mode for mode in _NOTES_MODE_IDS if mode not in section]
    if missing_modes:
        failures.append(
            "  Gate R5a: RUNBOOK.md's marker section does not state, per notes mode,\n"
            "  what the delivered .docx contains. Missing mode id(s): "
            f"{', '.join(missing_modes)}.\n"
            "  Required: all four modes (`none`, `external`, `internal`, `both`) with\n"
            "  the footnotes and marker each one produces."
        )

    if "footnote" not in section.lower():
        failures.append(
            "  Gate R5b: RUNBOOK.md's marker section never mentions footnotes, so it\n"
            "  does not say what the document actually carries -- the marker is a\n"
            "  signpost for content the footnotes hold."
        )
    elif not RUNBOOK_ACCEPT_ALL_PROMOTION_PATTERN.search(section):
        failures.append(
            "  Gate R5b: RUNBOOK.md's marker section does not state that footnotes\n"
            "  survive accept-all (the <w:footnoteReference> sits inside <w:ins>, so\n"
            "  accepting all tracked changes PROMOTES the footnote to body text).\n"
            "  Without it, 'accept all and send' reads like a clean-copy procedure.\n"
            f"  Missing pattern: {RUNBOOK_ACCEPT_ALL_PROMOTION_PATTERN.pattern[:160]!r}"
        )

    # R5d: no delivered redline carries a first-page cover note. Issue #629
    # moved third-party counterparty paper onto the block compiler and issue
    # #631 deleted the standalone writer that was the last emitter, so a
    # RUNBOOK sentence promising the operator a cover page -- on ANY path --
    # sends them looking for a page that is not there.
    for sentence in re.split(r"(?<=[.!?])\s+", section):
        lowered_sentence = sentence.lower()
        if "cover note" not in lowered_sentence and "cover page" not in lowered_sentence:
            continue
        if any(token in lowered_sentence for token in _COVER_NOTE_RETIREMENT_TOKENS):
            continue  # an explicitly retired/historical statement is fine
        failures.append(
            "  Gate R5d: RUNBOOK.md's marker section still promises a first-page\n"
            "  cover note without marking it retired. Issue #629 moved third-party\n"
            "  paper onto the block compiler and issue #631 deleted the standalone\n"
            "  writer that was the last emitter -- every path now places the marker\n"
            "  in the header/footer only.\n"
            f"  Offending sentence: {sentence.strip()[:200]!r}"
        )

    if not RUNBOOK_FILENAME_NOT_A_SIGNPOST_PATTERN.search(section):
        failures.append(
            "  Gate R5c: RUNBOOK.md's marker section does not say that the download\n"
            "  filename carries no internal-notes signpost.\n"
            "  backend/src/download.py::redline_filename_for emits <stem>-redline.docx\n"
            "  in every notes mode; the filename half of epic #519 item E did not land.\n"
            f"  Missing pattern: {RUNBOOK_FILENAME_NOT_A_SIGNPOST_PATTERN.pattern[:160]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 6 — docs/threat-model.md: #522's machinery, #521's citation decision
# ---------------------------------------------------------------------------

# The pre-#522 claim. It described a filter that never existed: nothing
# takes a rationale "marked internal-only" and holds it back from the
# external-facing footnote. Checked as a plain substring in both dash
# spellings, like the retired marker strings above.
_RETIRED_HOLDBACK_STRINGS = (
    "Any rationale marked internal-only is held back from the external-facing footnote",
    "any rationale marked internal only is held back from the external-facing footnote",
)

# Pattern T3: what actually shipped -- a separate internal field, solicited
# only in `internal`/`both`, rendered only behind the `[INTERNAL]` marking.
THREAT_INTERNAL_FIELD_MACHINERY_PATTERN = re.compile(
    r"internal_rationale_for_footnote(?:.|\n){0,600}\[INTERNAL\]"
    r"|\[INTERNAL\](?:.|\n){0,600}internal_rationale_for_footnote",
)

# Pattern T4: #521's citation decision, recorded as a threat-model statement
# about the artifact rather than only as a comment in the scanner
# (`scripts/leakage_scan.py` check 3 permits on the internal channel).
THREAT_CITATION_DECISION_PATTERN = re.compile(
    r"(?:may|can)\s+name\s+a\s+past\s+counterparty",
    re.IGNORECASE,
)

# Pattern T5: and what that makes the artifact.
THREAT_THIRD_PARTY_TERMS_PATTERN = re.compile(
    r"third.part(?:y|ies)(?:’|')?s?\s+confidential"
    r"|confidential\s+terms\s+of\s+a\s+third\s+part",
    re.IGNORECASE,
)


def gate_6_threat_model_notes_machinery(threat_text: str) -> list[str]:
    """docs/threat-model.md must describe the internal-notes machinery that
    #522 actually built, and must record #521's citation decision (issue
    #524)."""
    failures: list[str] = []

    lowered = threat_text.lower()
    for retired in _RETIRED_HOLDBACK_STRINGS:
        if retired.lower() in lowered:
            failures.append(
                "  Gate T3: docs/threat-model.md still claims "
                f"{retired!r}.\n"
                "  No such filter exists. #522 built a SEPARATE field\n"
                "  (internal_rationale_for_footnote), solicited only in the\n"
                "  `internal`/`both` notes modes and rendered only behind the\n"
                "  [INTERNAL] marking -- internal content is requested to exist, never\n"
                "  generated into a counterparty-bound field and filtered on the way out."
            )

    if not THREAT_INTERNAL_FIELD_MACHINERY_PATTERN.search(threat_text):
        failures.append(
            "  Gate T3: docs/threat-model.md does not describe the shipped machinery\n"
            "  (internal_rationale_for_footnote rendered behind the [INTERNAL] marking).\n"
            f"  Missing pattern: {THREAT_INTERNAL_FIELD_MACHINERY_PATTERN.pattern[:160]!r}"
        )

    if not THREAT_CITATION_DECISION_PATTERN.search(threat_text):
        failures.append(
            "  Gate T4: docs/threat-model.md does not record issue #521's citation\n"
            "  decision -- that an internal note MAY name a past counterparty and cite\n"
            "  a past deal (scripts/leakage_scan.py check 3 permits citation leakage on\n"
            "  the internal channel). Today it lives only in the scanner's comments.\n"
            f"  Missing pattern: {THREAT_CITATION_DECISION_PATTERN.pattern[:160]!r}"
        )

    if not THREAT_THIRD_PARTY_TERMS_PATTERN.search(threat_text):
        failures.append(
            "  Gate T5: docs/threat-model.md does not state what that decision makes\n"
            "  the artifact -- an internal-notes document is a document that may carry\n"
            "  a THIRD PARTY's confidential terms, which is the justification for\n"
            "  keeping the export marker at all.\n"
            f"  Missing pattern: {THREAT_THIRD_PARTY_TERMS_PATTERN.pattern[:160]!r}"
        )

    return failures


# ---------------------------------------------------------------------------
# GATE 7 — ARCHITECTURE.md: marker placement matches the path described
# ---------------------------------------------------------------------------

# Retired belt-and-braces framing: the marker was "redundant" (cover note
# AND header AND footer) only while it was unconditional. Issue #513 made it
# conditional on the review's notes mode.
_ARCHITECTURE_REDUNDANT_MARKER_PHRASE = "redundant export marker"

# A "cover note" claim is true of NO path as of issue #631. It belonged to
# the standalone whole-document writer, which is deleted; every surviving
# path (`redline_generate.inject_export_marker_and_footnotes`, reached by
# first-party and third-party paper alike since issue #629) emits header +
# footer ONLY. So an unqualified cover-note sentence is false for every
# redline anyone can obtain, and only an explicitly RETIRED/historical
# mention is allowed. Any of these tokens on the same line marks it as such.
#
# (Before #631 this list named the path that emitted one -- "standalone
# writer", "fixture generation". Those tokens are deliberately NOT accepted
# any more: a sentence attributing a cover note to the standalone writer in
# the present tense is now just as wrong as an unattributed one, because
# that writer no longer exists.)
_COVER_NOTE_RETIREMENT_TOKENS = (
    "retired",
    "deleted",
    "#631",
    "no delivered redline",
    "gone with it",
)


def gate_7_architecture_marker_placement(architecture_text: str) -> list[str]:
    """ARCHITECTURE.md must not describe the marker as redundant/
    unconditional, and every cover-note mention must be marked RETIRED --
    no shipping path emits one (issue #524, updated by issue #631)."""
    failures: list[str] = []

    if _ARCHITECTURE_REDUNDANT_MARKER_PHRASE in architecture_text.lower():
        failures.append(
            "  Gate A1: ARCHITECTURE.md still calls the export marker "
            f"{_ARCHITECTURE_REDUNDANT_MARKER_PHRASE!r}.\n"
            "  It is conditional on the review's notes mode (`include_marker`, issue\n"
            "  #513), not a belt-and-braces triple placement on every redline."
        )

    for lineno, line in enumerate(architecture_text.splitlines(), start=1):
        if "cover note" not in line.lower():
            continue
        lowered_line = line.lower()
        if not any(token in lowered_line for token in _COVER_NOTE_RETIREMENT_TOKENS):
            failures.append(
                f"  Gate A2: ARCHITECTURE.md line {lineno} claims a first-page cover\n"
                "  note without marking it retired. NO shipping path emits one: the\n"
                "  standalone writer that did was deleted by issue #631, and every\n"
                "  surviving path emits header + footer only, so an unqualified claim\n"
                "  is false for every redline anyone can obtain.\n"
                f"  Expected one of {_COVER_NOTE_RETIREMENT_TOKENS} on that line."
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
    g4 = gate_4_additional_surfaces()
    g5 = gate_5_runbook_ship_ready(runbook_text)
    g6 = gate_6_threat_model_notes_machinery(threat_text)
    try:
        g7 = gate_7_architecture_marker_placement(read_text(ARCHITECTURE_PATH))
    except FileNotFoundError as e:
        g7 = [f"  {e}"]

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
    print(
        "Gate 5: RUNBOOK.md — what a ship-ready document contains per notes "
        "mode (four modes, footnotes survive accept-all, filename is not a "
        "signpost)"
    )
    if g5:
        for f in g5:
            print(f)
        all_failures.extend(g5)
    else:
        print("  PASS")

    print()
    print(
        "Gate 6: docs/threat-model.md — describes #522's internal-notes "
        "machinery; records #521's citation decision and what it makes the "
        "artifact"
    )
    if g6:
        for f in g6:
            print(f)
        all_failures.extend(g6)
    else:
        print("  PASS")

    print()
    print(
        "Gate 7: ARCHITECTURE.md — marker is conditional, and every "
        "cover-note claim is marked retired"
    )
    if g7:
        for f in g7:
            print(f)
        all_failures.extend(g7)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #513 (gates 1-4) and issue #524 (gates 5-7) for the "
            "full remediation plan."
        )
        return 1

    print(
        "PASS: attorney-approval framing retired; export marker is conditional "
        "(issue #513); docs match the notes-mode machinery that shipped (issue #524)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
