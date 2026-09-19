#!/usr/bin/env python3
"""
Slice test for issue #113: "a tracked paragraph-mark deletion or insertion
(merge/split) is accepted with no disclosure at all."

## The defect this pins fixed

Word records "merge this paragraph with the next one" as a DELETED paragraph
mark and "split this paragraph in two" as an INSERTED one, both written into
the paragraph mark's own run properties:
`<w:pPr><w:rPr><w:del/></w:rPr></w:pPr>` and its `<w:ins/>` twin. That marker
wraps no run, and `extraction_normalization_stage._walk_content` walks only
`list(p_el)`'s run-level content -- it never descends into `<w:pPr>` at all.
So no cluster was built, `_normalize_paragraph` saw no revision, and no
normalization note was produced. In BYTE space `_splice_accept_all` stripped
the marker like any other `<w:ins>`/`<w:del>` (tallying it indistinguishably
as a plain `del`/`ins`, which `accepted_revision_disclosure` then filtered
out as "already covered by the per-paragraph notes" -- which for a paragraph
mark was false) while never merging or splitting the `<w:p>` siblings the
marker named.

Net effect before this issue: the counterparty proposed a paragraph
restructure, the toaster silently declined to apply it, and told the
attorney nothing -- contradicting ARCHITECTURE.md's "never silent" rule for
the accept-all path.

## What the fix does, and deliberately does NOT do

It DISCLOSES. The merge/split semantics are still not applied -- that
limitation is unchanged and still pinned by
`tests/test_accept_all_materializer.py` -- so every assertion below that
touches text, block ids, spans or materialized structure demands the SAME
answer a marker-free document gives. What changes is that the proposal is
now named, in both disclosure channels:

  * text space -- a per-paragraph `normalization_notes` sentence, from the
    new `paragraph_mark` revision record
    (`extraction_normalization_stage._paragraph_mark_revisions` ->
    `normalize_input._normalize_paragraph`); and
  * byte space -- its own `accepted_revision_disclosure` sentence, from the
    `paragraphMarkDel`/`paragraphMarkIns` tally keys `_count_accepted` now
    writes instead of a plain `del`/`ins`.

## Fixture provenance (what production actually writes)

Every fixture here except the two named below is real `.docx` OOXML, built
with the repo's dependency-free `zipfile`-only convention (raw `w:ins`/
`w:del` markup is not reachable through python-docx's public API) and
driven through the real production entry points
`extraction_normalization_stage.extract_and_normalize` and
`materialize_accept_all_with_report` -- exactly as `scripts/review_spine.py`
calls them. The paragraph-mark shape is verbatim what Word writes when a
reviewer with track changes on presses Delete at the end of a paragraph (or
Enter in the middle of one); `_paragraph_mark_revisions` is the only
producer of the `paragraph_mark` record shape these tests assert on. Every
`.docx` is built with a FIXED zip timestamp, per tests/README.md ->
"Writing a test that will not be skipped by accident".

The two exceptions, both deliberate and both flagged in their own
docstrings:

  * `test_an_unknown_paragraph_mark_operation_fails_the_document_closed`
    drives `normalize_input._normalize_paragraph` directly with an
    `operation` value the OOXML extractor can never emit (it only ever
    writes `"deleted"`/`"inserted"`). It exercises the same
    documented-contract-for-other-producers case issue #93 and issue #98
    already cover for a malformed `tracked_change`: `_normalize_paragraph`
    is an independently-callable schema contract, and its unknown-`status`
    sibling branch is unreachable from the extractor for precisely the same
    reason.
  * `test_the_disclosure_sentence_cannot_break_the_receipt_edit_count`
    reads its two literals OUT of `frontend/src/toaster/receipt.ts` rather
    than restating them, so the check cannot silently drift from the parser
    it is protecting; it fails closed if either literal stops being
    findable.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage as stage  # noqa: E402
import normalize_input  # noqa: E402

RECEIPT_TS = REPO_ROOT / "frontend" / "src" / "toaster" / "receipt.ts"

# ---------------------------------------------------------------------------
# Minimal dependency-free .docx builder (same convention as
# tests/test_heading_revision_disclosure_98.py, with the fixed zip timestamp
# tests/README.md requires so a rebuilt fixture is byte-stable).
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_NS_DECL = f'xmlns:w="{_WORD_NS}"'
_AUTHOR = "counterparty-counsel"
_DATE = "2026-01-01T00:00:00Z"
_ZIP_EPOCH = (2026, 1, 1, 0, 0, 0)

_ACCEPT_ALL_TAIL = "accepted-all into the operative draft."


def _w(tag: str) -> str:
    return f"{{{_WORD_NS}}}{tag}"


def _build_docx_bytes(body_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_W_NS_DECL}><w:body>{body_xml}<w:sectPr/></w:body></w:document>"
    )

    def _entry(name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        return info

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_entry("[Content_Types].xml"), _CONTENT_TYPES_XML)
        zf.writestr(_entry("_rels/.rels"), _RELS_XML)
        zf.writestr(_entry("word/document.xml"), document_xml)
    return buf.getvalue()


def _heading_p(text: str) -> str:
    return (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _body_p(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _mark_ppr(op: str, author: str = _AUTHOR, extra: str = "") -> str:
    """The paragraph mark's own run properties carrying a tracked deletion
    (a proposed MERGE with the following paragraph) or insertion (a proposed
    SPLIT). Verbatim the shape Word writes."""
    return (
        f'<w:pPr><w:rPr><w:{op} w:id="90" w:author="{author}" w:date="{_DATE}"/>'
        f"{extra}</w:rPr></w:pPr>"
    )


def _marked_p(op: str, text: str, author: str = _AUTHOR, extra: str = "") -> str:
    """A body `<w:p>` carrying `text` AND a tracked paragraph mark."""
    return (
        f"<w:p>{_mark_ppr(op, author, extra)}"
        f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
    )


def _content_revision(original: str, resulting: str) -> str:
    """An ordinary pending content revision: `original` struck, `resulting`
    inserted -- the shape that DOES produce a cluster, a per-paragraph
    accept-all note, and the tail `frontend/src/toaster/receipt.ts` counts."""
    return (
        f'<w:del w:id="1" w:author="{_AUTHOR}" w:date="{_DATE}">'
        f'<w:r><w:delText xml:space="preserve">{original}</w:delText></w:r></w:del>'
        f'<w:ins w:id="2" w:author="{_AUTHOR}" w:date="{_DATE}">'
        f'<w:r><w:t xml:space="preserve">{resulting}</w:t></w:r></w:ins>'
    )


def _blocks(out: dict) -> list[tuple]:
    """The identity-bearing projection of a normalized document: what must
    NOT move when a paragraph mark is disclosed."""
    return [
        (
            p["block_id"],
            p["heading"],
            p["text"],
            tuple(tuple(span) for span in p["physical_spans"]),
            tuple(p["physical_p_indexes"]),
        )
        for p in out.get("paragraphs", [])
    ]


def _paragraph_count(docx_bytes: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))  # noqa: S314
    return sum(1 for _ in root.iter(_w("p")))


def _notes(docx_bytes: bytes) -> str:
    """Both disclosure channels joined exactly as `scripts/review_spine.py`
    joins them before persisting `normalization_notes`."""
    out = stage.extract_and_normalize(docx_bytes)
    _, report = stage.materialize_accept_all_with_report(docx_bytes)
    return " ".join(
        part
        for part in (
            out.get("normalization_notes"),
            stage.accepted_revision_disclosure(report),
        )
        if part
    )


def _paragraph_mark_revision(operation: str) -> dict:
    return {
        "heading": "Term",
        "text": "Payment terms.",
        "revisions": [
            {
                "type": "paragraph_mark",
                "status": "unresolved",
                "operation": operation,
                "author": _AUTHOR,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_a_deleted_paragraph_mark_is_disclosed() -> None:
    """The ticket's own `C_para_mark_deleted_merge` evidence row: a proposed
    MERGE produced no note and no disclosure line at all."""
    docx_bytes = _build_docx_bytes(
        _heading_p("Term") + _marked_p("del", "First half") + _body_p("second half.")
    )
    out = stage.extract_and_normalize(docx_bytes)
    _, report = stage.materialize_accept_all_with_report(docx_bytes)
    disclosure = stage.accepted_revision_disclosure(report)

    notes = out.get("normalization_notes")
    assert notes, (
        "a pending tracked paragraph-mark DELETION produced no normalization "
        "note -- accepted in silence, the exact defect"
    )
    assert "paragraph-mark deletion" in notes, f"the note must name the markup: {notes!r}"
    assert "merging this paragraph with the one that follows it" in notes, (
        f"the note must name what the counterparty PROPOSED (a merge), not "
        f"only the OOXML it used: {notes!r}"
    )
    assert "not applied" in notes, (
        f"the note must say the proposal is NOT applied -- the documented "
        f"limitation is the whole reason it must be disclosed: {notes!r}"
    )
    assert _AUTHOR in notes, f"the note must name the revision author: {notes!r}"
    assert disclosure and "deleted paragraph mark" in disclosure, (
        f"the byte-space report must disclose the stripped marker too: "
        f"{disclosure!r}"
    )


def test_an_inserted_paragraph_mark_is_disclosed() -> None:
    """The ticket's `Z4_ins_para_mark_new_para_split` evidence row: the
    mirror-image proposal, a SPLIT, was equally silent. Seeded separately
    from the deletion above because `_normalize_paragraph` branches on
    `operation` -- a deletion-only fixture leaves this half green forever."""
    docx_bytes = _build_docx_bytes(
        _heading_p("Fees")
        + _marked_p("ins", "First sentence.")
        + _body_p("Second sentence.")
    )
    out = stage.extract_and_normalize(docx_bytes)
    _, report = stage.materialize_accept_all_with_report(docx_bytes)
    disclosure = stage.accepted_revision_disclosure(report)

    notes = out.get("normalization_notes")
    assert notes, "a pending tracked paragraph-mark INSERTION produced no note"
    assert "paragraph-mark insertion" in notes, f"must name the markup: {notes!r}"
    assert "splitting this paragraph into two" in notes, (
        f"an inserted paragraph mark proposes a SPLIT, not a merge: {notes!r}"
    )
    assert "merging" not in notes, (
        f"an inserted paragraph mark must not be reported as a merge: {notes!r}"
    )
    assert disclosure and "inserted paragraph mark" in disclosure, (
        f"the byte-space report must disclose the stripped marker: {disclosure!r}"
    )


def test_the_byte_space_report_names_the_marker_under_its_own_key() -> None:
    """The tally half. As a plain `del`/`ins` the marker was filtered out of
    `accepted_revision_disclosure` -- correct for a CONTENT revision, whose
    per-paragraph accept-all note already covers it; wrong for a paragraph
    mark, which had no per-paragraph note at all. Both kinds are seeded, and
    so is the plural, because the sentence branches on count."""
    docx_bytes = _build_docx_bytes(
        _heading_p("Term")
        + _marked_p("del", "A")
        + _marked_p("del", "B")
        + _marked_p("ins", "C")
        + _body_p("D")
    )
    _, report = stage.materialize_accept_all_with_report(docx_bytes)

    assert report["accepted"] == {"paragraphMarkDel": 2, "paragraphMarkIns": 1}, (
        f"paragraph-mark markers must be tallied under their own keys, never "
        f"as plain del/ins: {report['accepted']!r}"
    )
    assert not report["unapplied"], (
        f"the splice strips these markers, so nothing is left unapplied in "
        f"markup space: {report['unapplied']!r}"
    )
    disclosure = stage.accepted_revision_disclosure(report) or ""
    assert "2 deleted paragraph marks" in disclosure, (
        f"the disclosure must pluralize a repeated marker: {disclosure!r}"
    )
    assert "1 inserted paragraph mark" in disclosure, (
        f"the insertion is missing from {disclosure!r}"
    )
    assert "1 inserted paragraph marks" not in disclosure, (
        f"a single marker must stay singular: {disclosure!r}"
    )
    assert "not applied" in disclosure, (
        f"the byte-space sentence must state that the merge/split is NOT "
        f"applied -- otherwise it claims a paragraph restructure the "
        f"delivered redline does not contain: {disclosure!r}"
    )


def test_disclosure_changes_no_text_no_block_id_and_no_materialized_structure() -> None:
    """The limitation is unchanged: disclosing it must move nothing. Each
    marked document is compared against its own marker-free twin -- same
    runs, same styles, only the `<w:pPr><w:rPr>` marker added -- across the
    operative text, the block ids/spans/identities, and the materialized
    `<w:p>` count.

    Also pins the invariant that made this change safe: the RAW read
    (stage 1, `review_spine`) and the MATERIALIZED read (stage 5,
    `redline_generate`) must assign the same block ids to the same text.
    The EMPTY-spacer variant is the one that exercises it -- a `<w:p>` with
    no text and only a paragraph mark is skipped outright as a spacer in the
    materialized read, and must therefore contribute no block in the raw one
    either, or every later block id shifts between the two stages.
    """
    variants = {
        "merge, body paragraph": (
            _marked_p("del", "First half") + _body_p("second half."),
            _body_p("First half") + _body_p("second half."),
        ),
        "split, body paragraph": (
            _marked_p("ins", "First sentence.") + _body_p("Second sentence."),
            _body_p("First sentence.") + _body_p("Second sentence."),
        ),
        "merge beside a content revision": (
            f'<w:p>{_mark_ppr("del")}<w:r><w:t xml:space="preserve">Pay in </w:t></w:r>'
            f'{_content_revision("60", "45")}'
            '<w:r><w:t xml:space="preserve"> days</w:t></w:r></w:p>'
            + _body_p("of invoice."),
            '<w:p><w:r><w:t xml:space="preserve">Pay in </w:t></w:r>'
            f'{_content_revision("60", "45")}'
            '<w:r><w:t xml:space="preserve"> days</w:t></w:r></w:p>'
            + _body_p("of invoice."),
        ),
        "merge on an EMPTY spacer paragraph": (
            f"<w:p>{_mark_ppr('del')}</w:p>" + _body_p("Body."),
            "<w:p/>" + _body_p("Body."),
        ),
        "merge on an EMPTY spacer before any heading": (
            f"<w:p>{_mark_ppr('del')}</w:p>" + _heading_p("Second") + _body_p("Body."),
            "<w:p/>" + _heading_p("Second") + _body_p("Body."),
        ),
        "merge on a heading paragraph": (
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/><w:rPr>'
            f'<w:del w:id="90" w:author="{_AUTHOR}" w:date="{_DATE}"/>'
            "</w:rPr></w:pPr><w:r><w:t>Term</w:t></w:r></w:p>" + _body_p("Body."),
            _heading_p("Term") + _body_p("Body."),
        ),
    }
    for label, (marked_body, plain_body) in variants.items():
        marked = _build_docx_bytes(_heading_p("Agreement") + marked_body)
        plain = _build_docx_bytes(_heading_p("Agreement") + plain_body)
        marked_out = stage.extract_and_normalize(marked)
        plain_out = stage.extract_and_normalize(plain)

        assert marked_out["status"] == "normalized", (
            f"[{label}] a paragraph mark must never fail the document closed: "
            f"{marked_out['status']!r}"
        )
        assert _blocks(marked_out) == _blocks(plain_out), (
            f"[{label}] disclosing a paragraph mark moved the operative text "
            f"or the block identities:\n  marked={_blocks(marked_out)}\n"
            f"  plain ={_blocks(plain_out)}"
        )
        assert marked_out.get("normalization_notes"), (
            f"[{label}] the marked variant disclosed nothing"
        )

        materialized, _ = stage.materialize_accept_all_with_report(marked)
        plain_materialized, _ = stage.materialize_accept_all_with_report(plain)
        assert _paragraph_count(materialized) == _paragraph_count(plain_materialized), (
            f"[{label}] the merge/split is still NOT applied in byte space "
            f"(the documented limitation): expected "
            f"{_paragraph_count(plain_materialized)} <w:p> elements, got "
            f"{_paragraph_count(materialized)}"
        )
        re_read = stage.extract_and_normalize(materialized)
        assert _blocks(re_read) == _blocks(marked_out), (
            f"[{label}] stage 1 (raw bytes) and stage 5 (materialized bytes) "
            f"must assign the same block ids to the same text:\n"
            f"  raw         ={_blocks(marked_out)}\n"
            f"  materialized={_blocks(re_read)}"
        )


def test_a_formatting_change_on_the_pilcrow_is_not_reported_as_a_merge() -> None:
    """`<w:pPr><w:rPr>` also holds `w:rPrChange` -- an ordinary tracked
    FORMATTING change to the paragraph mark, already accepted and disclosed
    by `PROPERTY_CHANGE_TAGS` since issue #685. Only `w:ins`/`w:del` change
    meaning inside the paragraph mark, so a sibling `w:rPrChange` must keep
    its own name and stay in the formatting sentence. Seeded alongside a
    real marker so BOTH branches of `_count_accepted`'s retag are exercised
    on one document."""
    docx_bytes = _build_docx_bytes(
        _heading_p("Term")
        + _marked_p(
            "del",
            "Half",
            extra=(
                f'<w:rPrChange w:id="91" w:author="{_AUTHOR}" w:date="{_DATE}">'
                "<w:rPr/></w:rPrChange>"
            ),
        )
        + _body_p("rest.")
    )
    _, report = stage.materialize_accept_all_with_report(docx_bytes)
    assert report["accepted"] == {"paragraphMarkDel": 1, "rPrChange": 1}, (
        f"a w:rPrChange on the pilcrow is a formatting revision, not a merge "
        f"-- it must keep its own tally key: {report['accepted']!r}"
    )
    disclosure = stage.accepted_revision_disclosure(report) or ""
    assert "1 rPrChange" in disclosure, (
        f"the formatting sentence must still name the rPrChange: {disclosure!r}"
    )
    assert "deleted paragraph mark" in disclosure, (
        f"the paragraph-mark sentence must still be present beside it: "
        f"{disclosure!r}"
    )


def test_a_document_with_no_paragraph_mark_gets_no_spurious_note() -> None:
    """The negative control -- including the two shapes closest to the
    marker: a `<w:pPr>` with ordinary run properties on the pilcrow, and an
    ordinary content `<w:del>` inside the paragraph BODY, which must keep
    producing its own accept-all note and nothing else."""
    clean = _build_docx_bytes(
        _heading_p("Term")
        + "<w:p><w:pPr><w:rPr><w:b/></w:rPr></w:pPr>"
        + "<w:r><w:t>Bold pilcrow.</w:t></w:r></w:p>"
        + '<w:p><w:r><w:t xml:space="preserve">Pay in </w:t></w:r>'
        + _content_revision("60", "45")
        + '<w:r><w:t xml:space="preserve"> days.</w:t></w:r></w:p>'
    )
    out = stage.extract_and_normalize(clean)
    _, report = stage.materialize_accept_all_with_report(clean)
    notes = out.get("normalization_notes") or ""

    assert "paragraph-mark" not in notes and "paragraph mark" not in notes, (
        f"a document with no tracked paragraph mark must produce no "
        f"paragraph-mark note: {notes!r}"
    )
    assert _ACCEPT_ALL_TAIL in notes, (
        f"fixture premise broken: the ordinary content revision must still "
        f"produce its own accept-all note: {notes!r}"
    )
    assert not set(report["accepted"]) - {"ins", "del"}, (
        f"no paragraph-mark key may appear for a document without one: "
        f"{report['accepted']!r}"
    )
    assert stage.accepted_revision_disclosure(report) is None, (
        "a document whose only revisions are content ins/del is disclosed "
        "per-paragraph, and must add no byte-space sentence"
    )


def test_the_disclosure_sentence_cannot_break_the_receipt_edit_count() -> None:
    """`frontend/src/toaster/receipt.ts::acceptedChangesSummary` COUNTS the
    literal tail `accepted-all into the operative draft.` and drops the
    attorney's whole pending-edit line whenever its structured parse
    accounts for fewer sentences than that count finds. A paragraph-mark
    sentence that borrowed the tail without matching the structured shape
    would therefore silently delete a line the receipt gets today.

    Both literals are read OUT of receipt.ts rather than restated here, so
    this check cannot drift away from the parser it protects -- and it fails
    closed if either stops being findable."""
    source = RECEIPT_TS.read_text(encoding="utf-8")
    tail_match = re.search(r"notes\.match\(/(.+?)/g\)", source)
    pattern_match = re.search(r"^\s*(/Paragraph .+/g);\s*$", source, re.MULTILINE)
    assert tail_match and pattern_match, (
        "could not locate acceptedChangesSummary's tail regex and/or "
        "structured pattern in receipt.ts -- this guard must fail closed "
        "rather than pass by finding nothing to check"
    )
    tail_re = re.compile(tail_match.group(1))
    structured_re = re.compile(pattern_match.group(1)[1:-2])

    # One paragraph carrying BOTH an ordinary content revision (which owns
    # the tail) and a tracked paragraph mark (which must not).
    docx_bytes = _build_docx_bytes(
        _heading_p("Term")
        + f'<w:p>{_mark_ppr("del")}<w:r><w:t xml:space="preserve">Pay in </w:t></w:r>'
        + _content_revision("60", "45")
        + '<w:r><w:t xml:space="preserve"> days</w:t></w:r></w:p>'
        + _body_p("of invoice.")
    )
    notes = _notes(docx_bytes)
    assert "paragraph-mark deletion" in notes, (
        f"fixture premise broken: no marker note in {notes!r}"
    )
    tails = len(tail_re.findall(notes))
    parsed = len(structured_re.findall(notes))
    assert tails == 1, (
        f"exactly ONE accept-all tail belongs in these notes (the content "
        f"revision's); the paragraph-mark sentences must not add their own. "
        f"Found {tails} in {notes!r}"
    )
    assert parsed == tails, (
        f"receipt.ts drops its whole pending-edit line when the structured "
        f"parse ({parsed}) accounts for fewer sentences than the tail count "
        f"({tails}). Notes: {notes!r}"
    )


def test_an_unknown_paragraph_mark_operation_fails_the_document_closed() -> None:
    """The fail-closed guard, driven at the `_normalize_paragraph` contract
    level.

    NOT a shape the OOXML extractor can produce:
    `_PARAGRAPH_MARK_OPERATIONS` maps exactly two tags to exactly two
    values, so a real `.docx` always arrives with `deleted` or `inserted`.
    This mirrors the module's own unknown-`status` branch for a
    `tracked_change`, which is unreachable from the extractor for precisely
    the same reason and is likewise covered at this level (see issue #93's
    and issue #98's malformed-record cases). `_normalize_paragraph` is a
    documented, independently-callable schema contract, and an unrecognised
    `operation` has no documented disposition -- guessing one is the "apply
    the closest match" move this pipeline prohibits."""
    result = normalize_input._normalize_paragraph(_paragraph_mark_revision("reflowed"))
    assert not result["normalizable"], (
        f"an unrecognised paragraph-mark operation has no documented "
        f"disposition and must fail closed: {result!r}"
    )
    assert "unknown operation" in result["note"], (
        f"the refusal must say why: {result['note']!r}"
    )

    # And the two documented values must NOT fail closed, so the guard
    # cannot be satisfied by refusing everything.
    for operation in ("deleted", "inserted"):
        ok = normalize_input._normalize_paragraph(_paragraph_mark_revision(operation))
        assert ok["normalizable"], (
            f"operation {operation!r} is documented and must normalize: {ok!r}"
        )
        assert ok["clean_text"] == "Payment terms.", (
            f"a paragraph mark is disclosure-only and must not touch the "
            f"operative text: {ok['clean_text']!r}"
        )
        assert _ACCEPT_ALL_TAIL not in ok["note"], (
            f"nothing was accepted into the operative text, so this note must "
            f"not carry receipt.ts's accept-all tail: {ok['note']!r}"
        )


def test_a_struck_paragraph_that_also_carries_a_mark_keeps_both_dispositions() -> None:
    """`_normalize_paragraph` has three normalizable exits -- the
    no-pending-change one and the two `deleted_in_full` ones -- and the
    paragraph-mark sentence must reach all of them. This drives the
    accept-all `deleted_in_full` exit (a wholly struck `<w:p>` whose
    paragraph mark is ALSO struck, which is exactly what deleting a whole
    paragraph with track changes on produces in Word) and checks that the
    marker sentence is appended rather than replacing the strike-out
    disposition, or displacing it from first position."""
    docx_bytes = _build_docx_bytes(
        _heading_p("Term")
        + f'<w:p>{_mark_ppr("del")}'
        + f'<w:del w:id="1" w:author="{_AUTHOR}" w:date="{_DATE}">'
        + '<w:r><w:delText xml:space="preserve">struck in full</w:delText></w:r>'
        + "</w:del></w:p>"
        + _body_p("Survivor.")
    )
    out = stage.extract_and_normalize(docx_bytes)
    assert out["status"] == "normalized", f"expected a normalized document: {out['status']!r}"
    notes = out.get("normalization_notes") or ""

    assert "The struck paragraph is omitted from the operative draft." in notes, (
        f"the whole-paragraph strike-out disposition must survive beside the "
        f"marker note: {notes!r}"
    )
    assert "paragraph-mark deletion" in notes, (
        f"the marker note must reach the deleted_in_full exit too: {notes!r}"
    )
    assert notes.index(_ACCEPT_ALL_TAIL) < notes.index("paragraph-mark deletion"), (
        "the accept-all sentence must stay FIRST -- it is the one receipt.ts's "
        "structured parse reads"
    )
    assert [p["text"] for p in out["paragraphs"]] == ["Survivor."], (
        f"the struck paragraph must still contribute nothing: "
        f"{[p['text'] for p in out['paragraphs']]!r}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_a_deleted_paragraph_mark_is_disclosed,
    test_an_inserted_paragraph_mark_is_disclosed,
    test_the_byte_space_report_names_the_marker_under_its_own_key,
    test_disclosure_changes_no_text_no_block_id_and_no_materialized_structure,
    test_a_formatting_change_on_the_pilcrow_is_not_reported_as_a_merge,
    test_a_document_with_no_paragraph_mark_gets_no_spurious_note,
    test_the_disclosure_sentence_cannot_break_the_receipt_edit_count,
    test_an_unknown_paragraph_mark_operation_fails_the_document_closed,
    test_a_struck_paragraph_that_also_carries_a_mark_keeps_both_dispositions,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL: {test.__name__} raised {type(exc).__name__}: {exc}")
        else:
            print(f"PASS: {test.__name__}")

    print()
    if failed:
        print(f"FAIL: {failed} test(s) failed (issue #113).")
        return 1
    print(
        "PASS: a tracked paragraph-mark merge/split is disclosed in both "
        "channels and still applied in neither (issue #113)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
