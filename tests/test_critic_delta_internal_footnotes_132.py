#!/usr/bin/env python3
"""
Issue #132: every critic disagreement is explained inside an INTERNAL
footnote of the delivered `.docx` when the review's notes mode carries
internal content -- the fulsome half of the owner's 2026-09-16 decision on
#96 (the receipt's counts-only line is the brief half).

## What this proves, on the generated document bytes

  1. `notes_mode="both"`: `word/footnotes.xml` carries, each inside its own
     `[INTERNAL NOTE: ...]` marking,
       - every contested replacement's section, the reviewer's text, the
         critic's objection and the critic's suggested text;
       - every rationale objection and the section it concerns;
       - every critic-added issue's section and rationale.
     A disagreement rides on the footnote of the issue that edits ITS
     section when there is one (part 1b pins that the contested
     replacement on Section 2 is on Section 2's footnote, not merely the
     first one), and on the first footnote otherwise -- a critic-added
     issue has no edits of its own under the add-only merge, so it usually
     has no clause of its own to hang on. The fixture also carries the
     optional and blank shapes v3 allows: a contested replacement with no
     suggested text renders no suggestion fragment (1l), one with a blank
     objection renders no note at all (1m), and a blank `section_ref` is
     named as an unnamed section (1n).
  2. `notes_mode="external"` (and `none`): none of that text anywhere in
     the package, and no internal marking at all.
  3. No critic-delta text appears anywhere outside an internal footnote --
     not in the body, not in another part, not in the unmarked external
     half of a footnote.
  4. The leakage scan still gates the critic delta on the EXTERNAL channel
     before anything renders: an objection that carries internal-strategy
     phrasing blocks the whole review in `both` mode, with no document.
     4b: a system-prompt gram (check 1, blocked on both channels) planted in
     EACH field the notes render -- including `primary_replacement_text`
     and the locators, which nothing scanned before #132 -- blocks the
     review with no document, against a control run of the same corpus
     over the clean fixture that delivers.
  5. A host whose edits ALL fail to compile cannot carry a footnote, so its
     notes are re-hosted on an issue that did land, never silently lost.
  6. #572's kill switch: with `NOTES_MODE_ENABLED` off the two modes that
     render any of this are refused at intake, so only parts 2's modes are
     reachable -- and they render nothing.
  7. Nothing in the document inserts text (every edit is a deletion): no
     footnote can be anchored, so nothing is rendered rather than anything
     being written untracked into the counterparty's text.
  8. A pure deletion never hosts a note, even when it is first in order and
     edits the note's own section: it has no `<w:ins>` to carry a footnote.

Each hosting rule in `redline_generate._host_critic_notes` and the re-host
in `generate_redline_from_blocks` has a part that goes red without it: the
notes-mode gate (part 2), the section match (1g), the first-footnote
fallback (1e/1i), the insert-only candidate rule (8) and the re-host of a
host that failed to compile (5). So do the renderer's skips (1l, 1m, 1n)
and each scan `leakage_scan._scan_critic_delta_fields` gained for #132
(4b).

## The fixture's producer is the real one

`critic_delta` is never hand-assembled here. Both model responses are run
through the REAL validator against the active v3 artifact
(`primary_review_pass.validate_model_response`), and merged by the REAL
`reconciliation.reconcile` -- the function that builds `critic_delta` in
production (it re-keys the critic's added issue, which collides with the
primary's `I1`, exactly as it does on a live review). The document is a
python-docx build addressed through its own block map.

Synthetic throughout: invented sections, invented notes, no real
counterparty text or party names.

Run standalone: `.venv/bin/python tests/test_critic_delta_internal_footnotes_132.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_DIR = REPO_ROOT / "backend"

for _dir in (SCRIPTS_DIR, BACKEND_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import extraction_normalization_stage  # noqa: E402
import footnote_audience  # noqa: E402
import leakage_scan  # noqa: E402
import primary_review_pass  # noqa: E402
import reconciliation  # noqa: E402
import redline_generate  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
FOOTNOTES_PART = "word/footnotes.xml"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Synthetic document: three headed sections. Section 1's body is TWO
# physical paragraphs, so part 5 can write an edit that proves against the
# block's text but cannot compile (a delete crossing the "\n" join).
# ---------------------------------------------------------------------------

TERM_P1 = "The Term shall be sixty (60) days from the Effective Date."
TERM_P2 = "The Fee shall be one hundred dollars per month."
NOTICE_P1 = "Notices shall be sent by prepaid post to the address above."
LAW_P1 = "This Agreement is governed by the laws of the State of Delaware."

SECTIONS = [
    ("Section 1. Term and Fee", [TERM_P1, TERM_P2]),
    ("Section 2. Notices", [NOTICE_P1]),
    ("Section 3. Governing Law", [LAW_P1]),
]

SEC1, SEC2, SEC3 = "Section 1", "Section 2", "Section 3"

EXT_1 = "The term is shorter than the standard position."
EXT_2 = "Electronic notice is the standard position."

# The critic delta's prose. Each string is distinctive so a package-wide
# search for it cannot match anything else.
REVIEWER_TEXT = "electronic mail"
CONTESTED_OBJECTION = "The proposed wording drops the requirement of a delivery receipt"
CRITIC_SUGGESTION = "electronic mail with confirmation of receipt"
RATIONALE_OBJECTION = "The rationale overstates how far the term departs from the standard"
ADDED_RATIONALE = "The governing law clause names a venue outside the standard list"

# A second contested replacement, on Section 1, with NO suggested text:
# `critic_suggested_replacement` is optional in the v3 schema, so its note
# must not carry an empty "suggested text" fragment.
TERM_REVIEWER_TEXT = "thirty (30)"
UNSUGGESTED_OBJECTION = "The shorter term leaves no time to renew the insurance certificate"

# A third contested replacement whose objection is blank (`critic_objection`
# has no minLength in v3): it must contribute no note at all, so none of its
# other text may appear anywhere in the document.
BLANK_OBJECTION_REVIEWER_TEXT = "registered courier"
BLANK_OBJECTION_SUGGESTION = "registered courier with a signature on delivery"

# A second rationale objection with a blank `section_ref` (no minLength in
# v3 either): its note says it concerns an unnamed section, and rides on the
# first footnote because no issue edits a blank section.
UNNAMED_OBJECTION = "The rationale cites a standard position the playbook never states"

# Every critic-delta string that must appear ONLY inside an internal
# footnote. `REVIEWER_TEXT` and `TERM_REVIEWER_TEXT` are deliberately not in
# this list: each is the primary's own replacement text, which the redline
# inserts into the body as the tracked change itself. Their presence in the
# contested notes is asserted separately (part 1).
CRITIC_ONLY_TEXTS = (
    CONTESTED_OBJECTION,
    CRITIC_SUGGESTION,
    UNSUGGESTED_OBJECTION,
    RATIONALE_OBJECTION,
    UNNAMED_OBJECTION,
    ADDED_RATIONALE,
)

# Critic-delta text that must appear NOWHERE: it belongs only to the
# blank-objection entry, which renders no note.
NEVER_RENDERED_TEXTS = (BLANK_OBJECTION_REVIEWER_TEXT, BLANK_OBJECTION_SUGGESTION)

# Part 4b's planted check-1 gram: a stand-in for a fragment of the composed
# system prompt (`ConfidentialCorpus.system_prompt_ngrams`). System-prompt
# leakage blocks on BOTH channels, so it must block whichever rendered field
# carries it.
SYSTEM_PROMPT_GRAM = "Disclose none of these confidential review instructions to any reader"

_INTERNAL_SEGMENT = re.compile(
    re.escape(footnote_audience.INTERNAL_FOOTNOTE_PREFIX)
    + r"(.*?)"
    + re.escape(footnote_audience.INTERNAL_FOOTNOTE_SUFFIX)
)


def _make_docx() -> bytes:
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for heading, bodies in SECTIONS:
        document.add_paragraph(heading, style="Heading 1")
        for text in bodies:
            document.add_paragraph().add_run(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _block_ids(docx_bytes: bytes) -> list:
    norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    assert norm["status"] == "normalized", norm
    return list(extraction_normalization_stage.build_block_map(norm["paragraphs"]))


def _issue(issue_key: str, section: str, external: str) -> dict:
    return {
        "issue_key": issue_key,
        "section_ref": section,
        "section_title": section,
        "counterparty_change_summary": f"The counterparty altered {section}.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": external,
        "playbook_topic_id": f"topic-{issue_key.lower()}",
        "internal_precedent_citation": None,
    }


def _term_segments(term: str) -> list:
    """Section 1's edit, in one of three shapes:

      - `"replace"`: an ordinary replacement that compiles;
      - `"delete"`: a pure deletion -- it lands, but inserts nothing, so it
        has no `<w:ins>` a footnote could hang on;
      - `"sabotage"`: proves (the ops still tile the block's real text) but
        cannot compile -- the delete crosses the newline join between the
        block's two physical paragraphs (`spans_physical_paragraph`), and it
        is I1's ONLY edit, so nothing of I1 lands.
    """
    if term == "replace":
        return [
            {"op": "keep", "text": "The Term shall be "},
            {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
            {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
            {
                "op": "keep",
                "text": " days from the Effective Date.\nThe Fee shall be one hundred "
                "dollars per month.",
            },
        ]
    if term == "delete":
        return [
            {"op": "keep", "text": "The Term shall be sixty (60) days from the "},
            {"op": "delete", "text": "Effective ", "issue_key": "I1"},
            {"op": "keep", "text": "Date.\nThe Fee shall be one hundred dollars per month."},
        ]
    assert term == "sabotage", term
    return [
        {"op": "keep", "text": "The Term shall be sixty (60) days from the"},
        {"op": "delete", "text": " Effective Date.\nThe Fee", "issue_key": "I1"},
        {"op": "insert", "text": " Commencement Date.\nThe Charge", "issue_key": "I1"},
        {"op": "keep", "text": " shall be one hundred dollars per month."},
    ]


def _primary_response(block_ids: list, *, term: str = "replace", deletions_only=False):
    term_block, notice_block = block_ids[0], block_ids[1]
    if deletions_only:
        patches = [
            {
                "block_id": notice_block,
                "segments": [
                    {"op": "keep", "text": "Notices shall be sent by "},
                    {"op": "delete", "text": "prepaid ", "issue_key": "I2"},
                    {"op": "keep", "text": "post to the address above."},
                ],
            }
        ]
    else:
        patches = [
            {"block_id": term_block, "segments": _term_segments(term)},
            {
                "block_id": notice_block,
                "segments": [
                    {"op": "keep", "text": "Notices shall be sent by "},
                    {"op": "delete", "text": "prepaid post", "issue_key": "I2"},
                    {"op": "insert", "text": REVIEWER_TEXT, "issue_key": "I2"},
                    {"op": "keep", "text": " to the address above."},
                ],
            },
        ]
    issues = [_issue("I2", SEC2, EXT_2)] if deletions_only else [
        _issue("I1", SEC1, EXT_1),
        _issue("I2", SEC2, EXT_2),
    ]
    return {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "verdict_summary": "Departures from the standard form.",
        "issues": issues,
        "block_patches": patches,
    }


def _critic_response(
    *,
    objection: str = CONTESTED_OBJECTION,
    added_internal: str | None = None,
    critic_edit=None,
) -> dict:
    # The critic numbers its own issues from "I1", colliding with the
    # primary's -- `reconcile` re-keys it, as on a live review.
    added = _issue("I1", SEC3, ADDED_RATIONALE)
    added["section_title"] = "Governing Law"
    if added_internal is not None:
        added[redline_generate.INTERNAL_RATIONALE_FIELD] = added_internal
    response = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [],
        "critic_delta": {
            "added_issues": [added],
            "contested_replacements": [
                {
                    "section_ref": SEC2,
                    "primary_replacement_text": REVIEWER_TEXT,
                    "critic_objection": objection,
                    "critic_suggested_replacement": CRITIC_SUGGESTION,
                },
                {
                    "section_ref": SEC1,
                    "primary_replacement_text": TERM_REVIEWER_TEXT,
                    "critic_objection": UNSUGGESTED_OBJECTION,
                },
                {
                    "section_ref": SEC3,
                    "primary_replacement_text": BLANK_OBJECTION_REVIEWER_TEXT,
                    "critic_objection": "   ",
                    "critic_suggested_replacement": BLANK_OBJECTION_SUGGESTION,
                },
            ],
            "rationale_objections": [
                {"section_ref": SEC1, "objection": RATIONALE_OBJECTION},
                {"section_ref": "", "objection": UNNAMED_OBJECTION},
            ],
        },
    }
    if critic_edit is not None:
        critic_edit(response["critic_delta"])
    return response


def _validated(response: dict) -> dict:
    ok, parsed = primary_review_pass.validate_model_response(
        json.dumps(response), schema_path=primary_review_pass.OUTPUT_SCHEMA_V3_PATH
    )
    assert ok, f"fixture response is not schema-valid v3: {parsed}"
    return parsed


def _reconciled(block_ids: list, **kwargs) -> dict:
    critic = {
        key: kwargs.pop(key)
        for key in ("objection", "added_internal", "critic_edit")
        if key in kwargs
    }
    return reconciliation.reconcile(
        primary_result=_validated(_primary_response(block_ids, **kwargs)),
        critic_result=_validated(_critic_response(**critic)),
    )


def _run(notes_mode: str, *, corpus=None, **kwargs) -> dict:
    docx_bytes = _make_docx()
    reconciled = _reconciled(_block_ids(docx_bytes), **kwargs)
    return redline_generate.generate_redline_from_blocks(
        reconciled_result=reconciled,
        corpus=corpus if corpus is not None else leakage_scan.ConfidentialCorpus(),
        normalized_docx_bytes=docx_bytes,
        review_id="review-132",
        notes_mode=notes_mode,
    )


# ---------------------------------------------------------------------------
# Document-bytes readers
# ---------------------------------------------------------------------------


def _footnote_texts(docx_bytes: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        if FOOTNOTES_PART not in zf.namelist():
            return []
        root = ET.fromstring(zf.read(FOOTNOTES_PART))  # noqa: S314
    return [
        "".join(t.text or "" for t in fn.iter(_qn("t"))).strip()
        for fn in root.iter(_qn("footnote"))
        if not fn.get(_qn("type"))
    ]


def _other_parts_text(docx_bytes: bytes) -> str:
    """Every part of the package EXCEPT the footnotes part, raw."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return "\n".join(
            zf.read(name).decode("utf-8", errors="replace")
            for name in zf.namelist()
            if name != FOOTNOTES_PART
        )


def _package_text(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return "\n".join(
            zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist()
        )


def _internal_segments(footnote_text: str) -> list:
    return _INTERNAL_SEGMENT.findall(footnote_text)


def _outside_internal(footnote_text: str) -> str:
    return _INTERNAL_SEGMENT.sub("", footnote_text)


def _delivered(result: dict, label: str, failures: list):
    if result.get("status") != "OK" or not result.get("docx_bytes"):
        failures.append(
            f"[{label}] expected a delivered document, got status={result.get('status')!r} "
            f"reason={result.get('reason')!r} detail={result.get('detail')!r}"
        )
        return None
    return result["docx_bytes"]


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


def part_1_both_mode_explains_every_disagreement(failures: list) -> None:
    docx_bytes = _delivered(_run("both"), "1", failures)
    if docx_bytes is None:
        return
    footnotes = _footnote_texts(docx_bytes)
    if len(footnotes) != 2:
        failures.append(f"[1a] expected one footnote per edited issue (2), got {footnotes}")
        return
    segments = [seg for text in footnotes for seg in _internal_segments(text)]

    def segment_with(needle: str):
        return next((seg for seg in segments if needle in seg), None)

    contested = segment_with(CONTESTED_OBJECTION)
    if contested is None:
        failures.append(f"[1b] the contested replacement's objection is in no internal note: {footnotes}")
    else:
        for want in (SEC2, REVIEWER_TEXT, CRITIC_SUGGESTION):
            if want not in contested:
                failures.append(
                    f"[1c] the contested-replacement note lacks {want!r} -- it must carry the "
                    f"section, the reviewer's text and the critic's alternative: {contested!r}"
                )
    rationale = segment_with(RATIONALE_OBJECTION)
    if rationale is None or SEC1 not in rationale:
        failures.append(
            f"[1d] the rationale objection and its section are not in one internal note: {footnotes}"
        )
    added = segment_with(ADDED_RATIONALE)
    if added is None or SEC3 not in added:
        failures.append(
            f"[1e] the critic-added issue's rationale and section are not in one internal note: "
            f"{footnotes}"
        )

    # Each disagreement sits on the footnote of the issue that edits its
    # section: Section 2's dispute on Section 2's footnote (I2, the second),
    # Section 1's on Section 1's (I1, the first). Section 3 has no edit, so
    # the added issue rides on the first footnote.
    sec1_note, sec2_note = footnotes
    if EXT_1 not in sec1_note or EXT_2 not in sec2_note:
        failures.append(f"[1f] footnotes are not in the expected issue order: {footnotes}")
    if CONTESTED_OBJECTION not in sec2_note:
        failures.append(
            f"[1g] the Section 2 contested replacement is not on Section 2's footnote: {footnotes}"
        )
    if RATIONALE_OBJECTION not in sec1_note:
        failures.append(
            f"[1h] the Section 1 rationale objection is not on Section 1's footnote: {footnotes}"
        )
    if ADDED_RATIONALE not in sec1_note:
        failures.append(
            f"[1i] the added issue (no edit of its own) is not on the first footnote: {footnotes}"
        )
    # Each disagreement has its OWN marking, after the issue's own text.
    # Section 1's footnote: the Section 1 contested replacement, the Section 1
    # rationale objection, the unnamed-section one and the added issue.
    # Section 2's: its contested replacement. The blank-objection entry adds
    # none.
    if len(_internal_segments(sec1_note)) != 4 or len(_internal_segments(sec2_note)) != 1:
        failures.append(
            f"[1j] expected one [INTERNAL NOTE: ...] per disagreement (4 + 1): {footnotes}"
        )
    for note, external in ((sec1_note, EXT_1), (sec2_note, EXT_2)):
        if not note.startswith(external):
            failures.append(f"[1k] the issue's own external rationale must come first: {note!r}")

    # A contested replacement with no suggested text: its note carries the
    # section, the reviewer's text and the objection, and NO suggestion
    # fragment -- not a label, not an empty pair of quotes.
    unsuggested = segment_with(UNSUGGESTED_OBJECTION)
    if unsuggested is None or UNSUGGESTED_OBJECTION not in sec1_note:
        failures.append(
            f"[1l] the Section 1 contested replacement is not on Section 1's footnote: {footnotes}"
        )
    else:
        for want in (SEC1, TERM_REVIEWER_TEXT):
            if want not in unsuggested:
                failures.append(f"[1l] the unsuggested note lacks {want!r}: {unsuggested!r}")
        for unwanted in ("suggested text", "“”", '""'):
            if unwanted in unsuggested:
                failures.append(
                    f"[1l] a contested replacement with no critic_suggested_replacement "
                    f"rendered {unwanted!r}: {unsuggested!r}"
                )

    # A blank objection contributes no [INTERNAL NOTE: ...] segment: none of
    # its entry's other text is anywhere in the package, and no segment is a
    # disagreement with nothing in it.
    package = _package_text(docx_bytes)
    for text in NEVER_RENDERED_TEXTS:
        if text in package:
            failures.append(
                f"[1m] the blank-objection contested replacement rendered {text!r}: {footnotes}"
            )
    for seg in segments:
        if re.search(r"objection:\s*(Critic's|$)", seg.strip()):
            failures.append(f"[1m] a note carries an empty objection: {seg!r}")

    # A blank `section_ref` names an unnamed section rather than nothing, and
    # rides on the first footnote.
    unnamed = segment_with(UNNAMED_OBJECTION)
    if unnamed is None or "an unnamed section" not in unnamed:
        failures.append(
            f"[1n] the rationale objection with a blank section_ref must say it concerns "
            f"an unnamed section: {unnamed!r}"
        )
    if UNNAMED_OBJECTION not in sec1_note:
        failures.append(
            f"[1n] the blank-section rationale objection is not on the first footnote: "
            f"{footnotes}"
        )


def part_1b_internal_mode_also_renders(failures: list) -> None:
    docx_bytes = _delivered(_run("internal"), "1b", failures)
    if docx_bytes is None:
        return
    joined = " ".join(_footnote_texts(docx_bytes))
    for text in CRITIC_ONLY_TEXTS:
        if text not in " ".join(_internal_segments(joined)):
            failures.append(f"[1b] notes_mode='internal' does not render {text!r}: {joined!r}")
    if EXT_1 in joined or EXT_2 in joined:
        failures.append("[1b] notes_mode='internal' rendered the counterparty-facing rationale")


def part_2_external_and_none_render_nothing(failures: list) -> None:
    for mode in ("external", "none"):
        docx_bytes = _delivered(_run(mode), f"2/{mode}", failures)
        if docx_bytes is None:
            continue
        package = _package_text(docx_bytes)
        for text in (*CRITIC_ONLY_TEXTS, "Critic"):
            if text in package:
                failures.append(
                    f"[2a/{mode}] critic-delta text {text!r} reached a {mode!r} document"
                )
        if footnote_audience.INTERNAL_FOOTNOTE_PREFIX in package:
            failures.append(f"[2b/{mode}] an internal marking reached a {mode!r} document")
    external = _delivered(_run("external"), "2c", failures)
    if external is not None and _footnote_texts(external) != [EXT_1, EXT_2]:
        failures.append(
            f"[2c] external mode must render exactly the two external rationales: "
            f"{_footnote_texts(external)}"
        )


def part_3_critic_text_only_inside_internal_footnotes(failures: list) -> None:
    for mode in ("both", "internal"):
        docx_bytes = _delivered(_run(mode), f"3/{mode}", failures)
        if docx_bytes is None:
            continue
        other_parts = _other_parts_text(docx_bytes)
        unmarked = " ".join(_outside_internal(text) for text in _footnote_texts(docx_bytes))
        for text in CRITIC_ONLY_TEXTS:
            if text in other_parts:
                failures.append(
                    f"[3a/{mode}] critic-delta text {text!r} appears outside word/footnotes.xml"
                )
            if text in unmarked:
                failures.append(
                    f"[3b/{mode}] critic-delta text {text!r} appears in a footnote OUTSIDE an "
                    f"[INTERNAL NOTE: ...] marking"
                )


def part_4_leakage_scan_still_gates_the_delta(failures: list) -> None:
    leaked = "This is internal-only guidance: do not concede our floor on this point."
    result = _run("both", objection=leaked)
    if result.get("status") != redline_generate.ERROR_MANUAL_REVIEW_REQUIRED:
        failures.append(
            f"[4a] an objection carrying internal-strategy phrasing must block the review in "
            f"'both' mode; got status={result.get('status')!r}"
        )
    if result.get("reason") != "leakage_detected" or result.get("field_name") != (
        "critic_delta.critic_objection"
    ):
        failures.append(
            f"[4b] expected leakage_detected on critic_delta.critic_objection, got "
            f"reason={result.get('reason')!r} field={result.get('field_name')!r}"
        )
    if result.get("docx_bytes") is not None:
        failures.append("[4c] a leakage-blocked review produced a document")


def _plant(array: str, index: int, field: str):
    """A `critic_edit` that appends `SYSTEM_PROMPT_GRAM` to one field of one
    critic-delta entry, keeping whatever text the fixture gave it."""

    def edit(critic_delta: dict) -> None:
        entry = critic_delta[array][index]
        entry[field] = f"{entry.get(field) or ''} {SYSTEM_PROMPT_GRAM}".strip()

    return edit


# Every critic-delta field the internal notes render, with the scanned field
# name(s) a block on it may report. The added issue's two rationales are
# also merged into `issues` by `reconcile` (the add-only merge), and the
# primary-issue walk runs first, so either name is the same block.
_RENDERED_FIELDS = (
    ("contested_replacements", 0, "primary_replacement_text",
     {"critic_delta.primary_replacement_text"}),
    ("contested_replacements", 0, "section_ref",
     {"critic_delta.contested_replacements.section_ref"}),
    ("contested_replacements", 0, "critic_objection", {"critic_delta.critic_objection"}),
    ("contested_replacements", 0, "critic_suggested_replacement",
     {"critic_delta.critic_suggested_replacement"}),
    ("rationale_objections", 0, "section_ref",
     {"critic_delta.rationale_objections.section_ref"}),
    ("rationale_objections", 0, "objection", {"critic_delta.rationale_objections.objection"}),
    ("added_issues", 0, "section_ref", {"critic_delta.added_issues.section_ref"}),
    ("added_issues", 0, "section_title", {"critic_delta.added_issues.section_title"}),
    ("added_issues", 0, "external_rationale_for_footnote",
     {"external_rationale_for_footnote",
      "critic_delta.added_issues.external_rationale_for_footnote"}),
    ("added_issues", 0, redline_generate.INTERNAL_RATIONALE_FIELD,
     {redline_generate.INTERNAL_RATIONALE_FIELD,
      f"critic_delta.added_issues.{redline_generate.INTERNAL_RATIONALE_FIELD}"}),
)


def part_4b_every_rendered_field_is_gated(failures: list) -> None:
    """A fragment of the system prompt in ANY field the internal notes
    render blocks the review before compile, with no document -- in `both`
    mode, where it would otherwise be written into word/footnotes.xml.
    `primary_replacement_text` and the four locators reached only the result
    payload before #132, and nothing scanned them."""
    corpus = leakage_scan.ConfidentialCorpus(system_prompt_ngrams=[SYSTEM_PROMPT_GRAM])

    # Control: the same corpus over the clean fixture delivers, so a block
    # below is the planted field's and nothing else's.
    control = _run("both", corpus=corpus)
    if control.get("status") != "OK" or not control.get("docx_bytes"):
        failures.append(
            f"[4b/control] the gram corpus must not block the clean fixture: "
            f"status={control.get('status')!r} field={control.get('field_name')!r}"
        )
        return

    for array, index, field, scanned_names in _RENDERED_FIELDS:
        label = f"4b/{array}[{index}].{field}"
        result = _run("both", corpus=corpus, critic_edit=_plant(array, index, field))
        if result.get("docx_bytes") is not None:
            leaked = SYSTEM_PROMPT_GRAM in _package_text(result["docx_bytes"])
            failures.append(
                f"[{label}] a system-prompt gram in this field produced a document "
                f"(gram in the package: {leaked}); status={result.get('status')!r}"
            )
            continue
        if (
            result.get("status") != redline_generate.ERROR_MANUAL_REVIEW_REQUIRED
            or result.get("reason") != "leakage_detected"
            or result.get("category") != leakage_scan.CATEGORY_SYSTEM_PROMPT
            or result.get("field_name") not in scanned_names
        ):
            failures.append(
                f"[{label}] expected a system-prompt leakage block on one of "
                f"{sorted(scanned_names)}, got status={result.get('status')!r} "
                f"reason={result.get('reason')!r} category={result.get('category')!r} "
                f"field={result.get('field_name')!r}"
            )


def part_5_failed_host_is_rehosted(failures: list) -> None:
    result = _run("both", term="sabotage")
    docx_bytes = _delivered(result, "5", failures)
    if docx_bytes is None:
        return
    flag_only = {
        entry["_source_issue"].get("issue_key"): entry for entry in result.get("flag_only") or []
    }
    if flag_only.get("I1", {}).get("reason") != "spans_physical_paragraph":
        failures.append(
            f"[5a] fixture drift: I1's only edit was meant to fail to compile, got {flag_only}"
        )
    footnotes = _footnote_texts(docx_bytes)
    if len(footnotes) != 1 or EXT_2 not in footnotes[0]:
        failures.append(f"[5b] expected only I2's footnote to land, got {footnotes}")
        return
    internal = " ".join(_internal_segments(footnotes[0]))
    # I1 was the first choice for the Section 1 rationale objection AND the
    # added issue's fallback; both must survive I1's failure on I2's footnote.
    for text in CRITIC_ONLY_TEXTS:
        if text not in internal:
            failures.append(
                f"[5c] {text!r} was lost when its first host failed to compile: {footnotes}"
            )


def part_6_kill_switch_refuses_the_rendering_modes(failures: list) -> None:
    from src import reviews  # noqa: PLC0415

    with mock.patch.dict(os.environ, {"NOTES_MODE_ENABLED": ""}):
        for mode in ("internal", "both"):
            try:
                reviews.resolve_notes_mode(mode)
            except ValueError:
                continue
            failures.append(
                f"[6a] with NOTES_MODE_ENABLED off, {mode!r} was accepted -- the critic "
                f"explanation would be reachable before epic #519 ships"
            )
        for mode in ("external", "none"):
            if reviews.resolve_notes_mode(mode) != mode:
                failures.append(f"[6b] {mode!r} must stay accepted with the switch off")


def part_7_nothing_to_anchor_renders_nothing(failures: list) -> None:
    docx_bytes = _delivered(_run("both", deletions_only=True), "7", failures)
    if docx_bytes is None:
        return
    package = _package_text(docx_bytes)
    for text in CRITIC_ONLY_TEXTS:
        if text in package:
            failures.append(
                f"[7a] {text!r} reached a document that has no tracked insertion to anchor "
                f"a footnote on"
            )


def part_8_pure_deletion_never_hosts(failures: list) -> None:
    """Section 1's issue lands as a pure deletion: it is first in `issues`
    order AND edits the rationale objection's section, so it would win both
    the section match and the fallback -- but it inserts nothing, so a
    footnote hung on it is skipped by the writer. Every note must reach
    Section 2's footnote instead."""
    result = _run("both", term="delete")
    docx_bytes = _delivered(result, "8", failures)
    if docx_bytes is None:
        return
    footnotes = _footnote_texts(docx_bytes)
    if len(footnotes) != 1 or EXT_2 not in footnotes[0]:
        failures.append(f"[8a] expected only I2's footnote (I1 inserts nothing), got {footnotes}")
        return
    internal = " ".join(_internal_segments(footnotes[0]))
    for text in CRITIC_ONLY_TEXTS:
        if text not in internal:
            failures.append(
                f"[8b] {text!r} was hosted on a pure deletion and lost: {footnotes}"
            )


def part_9_added_issue_internal_rationale(failures: list) -> None:
    """A critic-added issue that also carries an `internal_rationale_for_
    footnote` (the critic is asked for one in `internal`/`both`) explains
    itself with BOTH reasons, external first, in the one note."""
    internal_reason = "Our fallback venue list was updated last quarter"
    docx_bytes = _delivered(_run("both", added_internal=internal_reason), "9", failures)
    if docx_bytes is None:
        return
    segments = [
        seg for text in _footnote_texts(docx_bytes) for seg in _internal_segments(text)
    ]
    added = next((seg for seg in segments if ADDED_RATIONALE in seg), "")
    in_order = internal_reason in added and added.index(ADDED_RATIONALE) < added.index(
        internal_reason
    )
    if not in_order:
        failures.append(
            f"[9a] the added issue's note must carry its external then its internal "
            f"rationale: {segments}"
        )


def main() -> int:
    failures: list = []
    for part in (
        part_1_both_mode_explains_every_disagreement,
        part_1b_internal_mode_also_renders,
        part_2_external_and_none_render_nothing,
        part_3_critic_text_only_inside_internal_footnotes,
        part_4_leakage_scan_still_gates_the_delta,
        part_4b_every_rendered_field_is_gated,
        part_5_failed_host_is_rehosted,
        part_6_kill_switch_refuses_the_rendering_modes,
        part_7_nothing_to_anchor_renders_nothing,
        part_8_pure_deletion_never_hosts,
        part_9_added_issue_internal_rationale,
    ):
        part(failures)
    if failures:
        print(f"FAIL: {len(failures)} failure(s)")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: test_critic_delta_internal_footnotes_132")
    return 0


if __name__ == "__main__":
    sys.exit(main())
