#!/usr/bin/env python3
"""
Slice test for issue #99: "the field-code disposition note quotes the whole
paragraph's clause text into normalization_notes and mislabels it as the
field's value".

## The defect this pins fixed

`scripts/normalize_input.py`'s field-code accept-all branch (issue #530)
ended its disclosure with:

    note += f" The field now resolves to '{resulting_text}'."

`resulting_text` is the WHOLE PARAGRAPH's accept-all text.
`extraction_normalization_stage._build_paragraph_record` computes it once per
`<w:p>` (`"".join(builder.resulting_parts).strip()`) and stamps that one
value onto EVERY cluster on the paragraph, so a DATE field resolving to
`Feb 1` inside a longer sentence was disclosed as resolving to the entire
sentence around it. The note was simply false about the one question the
branch exists to answer.

The extractor now stamps the field's OWN accept-all display text on each
`inside_field_code` cluster as `field_resulting_text`, and the note quotes
that instead:

  | Key                    | Scope                                       |
  |------------------------|---------------------------------------------|
  | `resulting_text`       | the whole paragraph -- the operative text   |
  | `field_resulting_text` | this field alone -- what the note quotes    |

## Why every pre-#99 fixture stayed green over it

Both `tests/redline/fixtures/pending_change_inside_field_code.json` and
`tests/test_extraction_normalization_stage_80.py`'s
`_field_code_conflict_p` used a paragraph that is NOTHING BUT the field. For
that shape the two values are byte-identical, so no assertion could tell
them apart, and `"New York" in notes` passed either way. The shapes below
deliberately surround the field with clause prose -- the shape a real
cross-reference, auto-numbering or date field actually has -- so the two
values differ and the assertion bites.

## Fixture provenance (what production actually writes)

Every shape below is driven through the REAL producer:
`extraction_normalization_stage.extract_and_normalize` /
`_build_paragraph_record` over real OOXML bytes, built with `zipfile` +
`ElementTree` only (the repo's dependency-free `.docx` convention).
`field_resulting_text` is written by `_process_fld_simple` and by nothing
else in the tree -- no test here hand-builds that key onto a record and then
asserts over it.

The ONE hand-built record is `test_absent_field_text_quotes_nothing`, and it
is hand-built on purpose: it is the shape the extractor CANNOT emit (it
always stamps the key), reaching `normalize_input.normalize()`'s documented
dict schema from another caller. That is stated plainly rather than dressed
up as a production path it does not have.

Branch coverage seeds more than the happy variant: a field inside prose AND
a field that is the whole paragraph; one field AND two fields; one cluster
AND two authors in one field; a resolved field AND a struck one; a clean
field result AND a hostile one.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import clause_boundaries  # noqa: E402
import extraction_normalization_stage as stage  # noqa: E402
import normalize_input  # noqa: E402

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_DECL = f'xmlns:w="{W_NS}"'

# The invariant tail `frontend/src/toaster/receipt.ts::acceptedChangesSummary`
# counts accept-all dispositions by. Issue #530 put the field's resolved text
# in a SECOND sentence precisely so this tail stays exact; #99 changes only
# what that second sentence quotes, so the tail must be untouched.
_TAIL = "accepted-all into the operative draft."

# The clause prose that surrounds the field in the embedded shapes. It is the
# text that must NEVER be quoted back as the field's resolved value.
_SURROUNDING = "and continues for two years unless terminated earlier for cause"


# ---------------------------------------------------------------------------
# OOXML builders (the real producer's input)
# ---------------------------------------------------------------------------


def _docx(body: str) -> bytes:
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {NS_DECL}><w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def _heading(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _ins(inner: str, author: str = "counterparty", rev_id: int = 1) -> str:
    return (
        f'<w:ins w:id="{rev_id}" w:author="{author}" '
        f'w:date="2026-01-01T00:00:00Z">{inner}</w:ins>'
    )


def _del(text: str, author: str = "counterparty", rev_id: int = 2) -> str:
    return (
        f'<w:del w:id="{rev_id}" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f'<w:r><w:delText xml:space="preserve">{text}</w:delText></w:r></w:del>'
    )


def _field(inner: str, instr: str = " DATE ") -> str:
    return f'<w:fldSimple w:instr="{instr}">{inner}</w:fldSimple>'


def _edited_field(old: str, new: str, *, author: str = "counterparty", base: int = 1) -> str:
    """A field whose cached result is under a live counterparty edit --
    `_process_fld_simple`'s `inside_field_code` path."""
    return _field(_del(old, author, base + 1) + _ins(_run(new), author, base))


def _para(inner: str) -> str:
    return f"<w:p>{inner}</w:p>"


def _embedded_field_para(old: str, new: str) -> str:
    """The shape issue #99 exists for: the field is a SMALL part of a longer
    sentence, so the field's resolved text and the paragraph's accept-all
    text are different strings."""
    return _para(
        _run("This Agreement commences on ")
        + _edited_field(old, new)
        + _run(f" {_SURROUNDING}.")
    )


def _record(paragraph_xml: str) -> dict[str, Any]:
    """The raw per-`<w:p>` record the real extractor emits."""
    root = ET.fromstring(  # noqa: S314 - fixture XML this file builds itself
        f"<w:document {NS_DECL}><w:body>{paragraph_xml}</w:body></w:document>"
    )
    return stage._build_paragraph_record(root[0][0])


def _notes(body: str) -> tuple[dict[str, Any], str]:
    """The result plus its notes, from whichever field carries them: a
    normalized run puts them at the top level, a fail-closed run puts them
    inside the analysis report (`normalize_input.build_unnormalizable_report`)."""
    result = stage.extract_and_normalize(_docx(body))
    notes = result.get("normalization_notes")
    if not notes:
        notes = (result.get("analysis_report") or {}).get("normalization_notes")
    return result, (notes or "")


# ---------------------------------------------------------------------------
# 1. The record shape -- the two keys exist and are genuinely different
# ---------------------------------------------------------------------------


def test_extractor_stamps_the_fields_own_text_alongside_the_paragraphs(
    failures: list[str],
) -> None:
    """`field_resulting_text` is the FIELD's accept-all text;
    `resulting_text` is the PARAGRAPH's. For an embedded field they must not
    be the same string -- if they are, every assertion below is vacuous."""
    record = _record(_embedded_field_para("Jan 1", "Feb 1"))
    revisions = [r for r in record["revisions"] if r.get("inside_field_code")]
    if len(revisions) != 1:
        failures.append(f"[R1] expected exactly one field-code revision. Got: {record!r}")
        return
    rev = revisions[0]
    if rev.get("field_resulting_text") != "Feb 1":
        failures.append(
            f"[R2] field_resulting_text must be the FIELD's resolved text. Got: {rev!r}"
        )
    if rev.get("resulting_text") != (
        f"This Agreement commences on Feb 1 {_SURROUNDING}."
    ):
        failures.append(
            f"[R3] resulting_text must stay the WHOLE PARAGRAPH's accept-all text. Got: {rev!r}"
        )
    if rev.get("field_resulting_text") == rev.get("resulting_text"):
        failures.append(
            f"[R4] the two keys collapsed to one value -- this fixture cannot "
            f"distinguish the bug from the fix. Got: {rev!r}"
        )


def test_a_static_field_still_emits_no_field_code_revision(failures: list[str]) -> None:
    """The OTHER branch of `_process_fld_simple`: a field with no pending
    edit in its result region resolves to a `field` revision and carries no
    `inside_field_code` cluster at all. Pins that #99 added its key to the
    edited-field path only."""
    record = _record(_para(_run("See ") + _field(_run("Exhibit A")) + _run(" attached.")))
    if any(r.get("inside_field_code") for r in record["revisions"]):
        failures.append(f"[R5] a static field must emit no field-code cluster. Got: {record!r}")
    if any("field_resulting_text" in r for r in record["revisions"]):
        failures.append(
            f"[R6] a static field must not carry field_resulting_text. Got: {record!r}"
        )
    fields = [r for r in record["revisions"] if r.get("type") == "field"]
    if len(fields) != 1 or fields[0].get("field_result") != "Exhibit A":
        failures.append(f"[R7] the static field must still RESOLVE. Got: {record!r}")


# ---------------------------------------------------------------------------
# 2. The note -- the defect itself
# ---------------------------------------------------------------------------


def test_note_quotes_the_field_not_the_surrounding_clause(failures: list[str]) -> None:
    """THE REGRESSION. Before #99 this note read `The field now resolves to
    'This Agreement commences on Feb 1 and continues for two years ...'`."""
    result, notes = _notes(_heading("Term") + _embedded_field_para("Jan 1", "Feb 1"))
    if result.get("status") != "normalized":
        failures.append(f"[N1] an edited field must still accept-all. Got: {result!r}")
        return
    if "The field now resolves to 'Feb 1'." not in notes:
        failures.append(f"[N2] the note must quote the FIELD's resolved text. Got: {notes!r}")
    if _SURROUNDING in notes:
        failures.append(
            f"[N3] the note quoted the surrounding clause prose as if it were "
            f"the field's value. Got: {notes!r}"
        )
    if "This Agreement commences on" in notes:
        failures.append(f"[N4] the note quoted the paragraph body. Got: {notes!r}")


def test_the_operative_text_is_still_the_whole_paragraph(failures: list[str]) -> None:
    """#99 changes the NOTE only. The accept-all text folded into the
    operative draft must still be the whole paragraph -- narrowing the note
    must not narrow the document."""
    result, _ = _notes(_heading("Term") + _embedded_field_para("Jan 1", "Feb 1"))
    texts = [p["text"] for p in result.get("paragraphs", [])]
    expected = f"This Agreement commences on Feb 1 {_SURROUNDING}."
    if expected not in texts:
        failures.append(f"[N5] the operative paragraph text changed. Got: {texts!r}")
    if any("Jan 1" in t for t in texts):
        failures.append(f"[N6] the pre-edit field text must not survive. Got: {texts!r}")


def test_field_that_is_the_whole_paragraph_still_reads_correctly(
    failures: list[str],
) -> None:
    """The issue #530 shape (field == whole paragraph) is the case where the
    two values coincide. It must keep working, not regress into silence."""
    body = _heading("Governing Law") + _para(_edited_field("Delaware", "New York"))
    result, notes = _notes(body)
    if result.get("status") != "normalized":
        failures.append(f"[N7] the #530 shape must still accept-all. Got: {result!r}")
    if "The field now resolves to 'New York'." not in notes:
        failures.append(f"[N8] the #530 shape lost its disclosure. Got: {notes!r}")


def test_two_fields_in_one_paragraph_are_both_named(failures: list[str]) -> None:
    """A paragraph can carry pending edits in MORE THAN ONE field. Each
    field's own text must be named, and neither may be reported as the
    other's -- the plural branch of the sentence builder."""
    body = _heading("Term") + _para(
        _run("From ")
        + _edited_field("Jan 1", "Feb 1", base=1)
        + _run(" until ")
        + _edited_field("Dec 1", "Nov 1", base=11)
        + _run(" inclusive.")
    )
    result, notes = _notes(body)
    if result.get("status") != "normalized":
        failures.append(f"[N9] two edited fields must accept-all. Got: {result!r}")
        return
    if "The fields now resolve to 'Feb 1' and 'Nov 1'." not in notes:
        failures.append(f"[N10] both fields must be named, in order. Got: {notes!r}")
    if "until" in notes or "inclusive" in notes:
        failures.append(f"[N11] the note quoted the paragraph body. Got: {notes!r}")


def test_two_authors_in_one_field_name_that_field_once(failures: list[str]) -> None:
    """Two authors editing ONE field's result back-to-back are two clusters
    but still one field. Naming its text twice would claim a second field the
    paragraph does not have -- the dedupe branch."""
    body = _heading("Term") + _para(
        _run("Due ")
        + _field(
            _del("Jan 1", "counterparty", 2)
            + _ins(_run("Feb "), "counterparty", 1)
            + _ins(_run("2"), "opposing counsel", 3)
        )
        + _run(" each year.")
    )
    result, notes = _notes(body)
    if result.get("status") != "normalized":
        failures.append(f"[N12] a two-author field edit must accept-all. Got: {result!r}")
        return
    if "The field now resolves to 'Feb 2'." not in notes:
        failures.append(f"[N13] one field must be named once, singular. Got: {notes!r}")
    if "The fields now resolve" in notes:
        failures.append(
            f"[N14] two clusters inside ONE field were reported as two fields. Got: {notes!r}"
        )


def test_a_struck_field_result_is_not_quoted_as_empty(failures: list[str]) -> None:
    """The field's displayed result is struck in full while the paragraph
    survives. `The field now resolves to ''` reads as a parse failure (the
    reasoning issue #93 applied to a struck paragraph), so nothing is
    quoted -- but the disposition sentence must still be there."""
    body = _heading("Term") + _para(
        _run("Commences on ") + _field(_del("Jan 1")) + _run(f" {_SURROUNDING}.")
    )
    result, notes = _notes(body)
    if result.get("status") != "normalized":
        failures.append(f"[N15] a struck field result must accept-all. Got: {result!r}")
        return
    if "resolves to ''" in notes:
        failures.append(f"[N16] an empty field result must not be quoted. Got: {notes!r}")
    if "inside a field code " + _TAIL not in notes:
        failures.append(f"[N17] the disposition must still be disclosed. Got: {notes!r}")
    if _SURROUNDING in notes:
        failures.append(f"[N18] the note fell back to the paragraph body. Got: {notes!r}")


def test_a_wholly_struck_paragraph_names_the_deletion_only(failures: list[str]) -> None:
    """Paragraph AND field struck together (issue #93's shape inside a
    field code): the deletion sentence is the disposition, and no
    `resolves to` sentence may contradict it."""
    body = _heading("Term") + _para(_field(_del("Jan 1")))
    _result, notes = _notes(body)
    if "resolves to" in notes:
        failures.append(f"[N19] a struck paragraph must not name a field value. Got: {notes!r}")
    if "struck paragraph is omitted" not in notes:
        failures.append(f"[N20] the deletion must still be named. Got: {notes!r}")


# ---------------------------------------------------------------------------
# 3. The escaped-text / fail-closed invariants the note sits behind
# ---------------------------------------------------------------------------


def test_a_hostile_field_result_still_fails_closed(failures: list[str]) -> None:
    """The control-character screen (issue #632) must run before the note is
    built, on the value the note now quotes. A zero-width space planted
    inside the FIELD's own result must fail the document closed and must
    never be echoed back."""
    hostile = "Feb​1"
    body = _heading("Term") + _para(
        _run("Commences on ")
        + _edited_field("Jan 1", hostile)
        + _run(f" {_SURROUNDING}.")
    )
    result, notes = _notes(body)
    if result.get("status") == "normalized":
        failures.append(f"[S1] a zero-width space in the field result was not screened. Got: {result!r}")
    if hostile in notes:
        failures.append("[S2] the screened field text was echoed back into the note")
    if normalize_input.CONTROL_CHARACTER_NOTE_PREFIX not in notes:
        failures.append(f"[S3] the refusal must be the counts-only screen note. Got: {notes!r}")


def test_the_receipt_tail_is_untouched(failures: list[str]) -> None:
    """`acceptedChangesSummary` counts dispositions off the literal tail
    `accepted-all into the operative draft.` -- exactly once per accepted
    paragraph. #99 rewrites the SECOND sentence only; the tail count must
    still equal the number of accepted paragraphs."""
    body = (
        _heading("Term")
        + _embedded_field_para("Jan 1", "Feb 1")
        + _heading("Notice")
        + _para(_run("Notice is given ") + _ins(_run("in writing")) + _run("."))
    )
    result, notes = _notes(body)
    if result.get("status") != "normalized":
        failures.append(f"[S4] the document must normalize. Got: {result!r}")
        return
    if notes.count(_TAIL) != 2:
        failures.append(f"[S5] expected one tail per accepted paragraph. Got: {notes!r}")
    if notes.count("The field now resolves to") != 1:
        failures.append(f"[S6] exactly one field disclosure expected. Got: {notes!r}")


# ---------------------------------------------------------------------------
# 4. Text-space is untouched (the downstream consumers of paragraph text)
# ---------------------------------------------------------------------------


def test_segmentation_and_block_map_are_unchanged(failures: list[str]) -> None:
    """#99 adds a KEY to a revision record; it changes no paragraph text.
    `clause_boundaries.is_boundary_paragraph` decides headings off that text
    and `redline_block_apply` matches edits against it, so both are pinned
    here: the heading is still a boundary, the body paragraph is still not,
    and the block carries non-empty text."""
    body = _heading("Term") + _embedded_field_para("Jan 1", "Feb 1")
    result, _ = _notes(body)
    if result.get("status") != "normalized":
        failures.append(f"[T1] the document must normalize. Got: {result!r}")
        return
    if not clause_boundaries.is_boundary_paragraph("Term", style_name="Heading1"):
        failures.append("[T2] 'Term' stopped being a boundary paragraph")
    operative = f"This Agreement commences on Feb 1 {_SURROUNDING}."
    if clause_boundaries.is_boundary_paragraph(operative):
        failures.append(f"[T3] the body paragraph became a boundary: {operative!r}")
    block_map = stage.build_block_map(result["paragraphs"])
    if not block_map:
        failures.append(f"[T4] the block map is empty. Got: {result!r}")
    for block_id, block in block_map.items():
        if not (block.get("text") or "").strip():
            failures.append(f"[T5] block {block_id} carries no text: {block!r}")
    if [b["heading"] for b in block_map.values()] != ["Term"]:
        failures.append(f"[T6] the heading stopped grouping its body. Got: {block_map!r}")


# ---------------------------------------------------------------------------
# 5. The one shape the extractor cannot emit (documented dict schema)
# ---------------------------------------------------------------------------


def test_absent_field_text_quotes_nothing(failures: list[str]) -> None:
    """HAND-BUILT ON PURPOSE. `_process_fld_simple` always stamps
    `field_resulting_text`, so this record cannot come from the extractor --
    it is another caller using `normalize_input`'s documented dict schema.
    With no field text on the record, the note must disclose the disposition
    and quote NOTHING, rather than fall back to the paragraph text (which is
    the defect) or go silent (which would hide the disposition)."""
    paragraph = {
        "heading": "Term",
        "text": "This Agreement commences on Jan 1 and runs for a year.",
        "revisions": [
            {
                "type": "tracked_change",
                "status": "unresolved",
                "author": "counterparty",
                "inside_field_code": True,
                "original_text": "This Agreement commences on Jan 1 and runs for a year.",
                "resulting_text": "This Agreement commences on Feb 1 and runs for a year.",
            }
        ],
    }
    result = normalize_input._normalize_paragraph(paragraph)
    note = result.get("note") or ""
    if result.get("normalizable") is not True:
        failures.append(f"[H1] the record must still accept-all. Got: {result!r}")
    if "inside a field code " + _TAIL not in note:
        failures.append(f"[H2] the disposition must still be disclosed. Got: {note!r}")
    if "resolves to" in note:
        failures.append(
            f"[H3] with no field text on the record the note must quote "
            f"nothing, not the paragraph. Got: {note!r}"
        )
    if result.get("clean_text") != "This Agreement commences on Feb 1 and runs for a year.":
        failures.append(f"[H4] the operative text must be unchanged. Got: {result!r}")


TESTS = [
    test_extractor_stamps_the_fields_own_text_alongside_the_paragraphs,
    test_a_static_field_still_emits_no_field_code_revision,
    test_note_quotes_the_field_not_the_surrounding_clause,
    test_the_operative_text_is_still_the_whole_paragraph,
    test_field_that_is_the_whole_paragraph_still_reads_correctly,
    test_two_fields_in_one_paragraph_are_both_named,
    test_two_authors_in_one_field_name_that_field_once,
    test_a_struck_field_result_is_not_quoted_as_empty,
    test_a_wholly_struck_paragraph_names_the_deletion_only,
    test_a_hostile_field_result_still_fails_closed,
    test_the_receipt_tail_is_untouched,
    test_segmentation_and_block_map_are_unchanged,
    test_absent_field_text_quotes_nothing,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found (issue #99).")
        return 1
    print("PASS: the field-code note quotes the field's own resolved text (issue #99).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
