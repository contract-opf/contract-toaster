#!/usr/bin/env python3
"""
Gate for issue #563: "accept-all as a physical stage-1 document transform --
one canonical document under review" (#530 rescoped).

## What this proves

`scripts/extraction_normalization_stage.py::materialize_accept_all` applies
the SAME accept-all disposition `normalize_paragraphs` already applies in
TEXT SPACE directly to `word/document.xml`'s BYTES: every `<w:del>` is
removed (including its content) and every `<w:ins>` is unwrapped (its
children spliced up, the wrapper dropped) -- everywhere in the part, at any
nesting depth, touching nothing else.

## The gap this closes (measured, not assumed)

Before this materializer exists, `redline_quote_apply.apply_quote_patches`
locates a `source_quote` against the ACCEPT-ALL text
(`extraction_normalization_stage.extract_and_normalize`'s own paragraph
text) but then hands `docx-editor` the RAW, still-marked-up bytes to edit.
`docx-editor`'s own text-map search happens to compute a compatible
accepted view for LOCATING the match -- so the patch does not fail to
locate. What it does NOT do is clean up the counterparty's own pending
`<w:ins>`/`<w:del>` once its text has been folded into a match: `replace()`
only rewrites the VISIBLE span, so the counterparty's original pending
revisions survive in the delivered document, nested awkwardly around (and,
for a mid-cluster match, literally inside) the toaster's own new redline --
three authors' tracked changes tangled into one clause, not the "redline
delivered ON the accepted document" this issue's Notes decide on. This was
confirmed empirically while building this test (see
`test_raw_bytes_still_leak_the_counterpartys_pending_markup` below) before
being encoded as a permanent regression gate.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage as stage  # noqa: E402
import redline_generate  # noqa: E402
import redline_quote_apply as rqa  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder -- same zipfile-only
# convention as tests/test_extraction_normalization_stage_80.py and
# tests/test_reserved_namespace_prefix_560.py: real w:ins/w:del OOXML a
# genuine parser (ElementTree, docx-editor) accepts, built programmatically
# by parameterized helper functions, never a single hand-fixed blob.
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
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

# A part this module never touches (not word/document.xml) -- planted to
# prove materialize_accept_all copies every OTHER zip entry through
# byte-for-byte (issue #563 scope item (c): "touches nothing else").
_CORE_PROPS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    "<coreProperties>UNTOUCHED_SENTINEL</coreProperties>"
)


def _build_docx_bytes(
    body_paragraphs_xml: str,
    extra_parts: dict[str, str] | None = None,
    extra_root_ns: str = "",
) -> bytes:
    """`extra_root_ns` (issue #563 follow-up finding 4, mirroring
    `tests/test_reserved_namespace_prefix_560.py::_document`'s
    `extra_root_ns` parameter): raw extra `xmlns[:prefix]="uri"`
    declaration(s) spliced onto the root `<w:document>` open tag -- lets a
    fixture declare a reserved `ns<digits>` prefix (or any other) on the
    root, exactly like a real round-tripped document does."""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        f"{extra_root_ns}>"
        f"<w:body>{body_paragraphs_xml}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
        for name, content in (extra_parts or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _mid_span_pending_p(prefix: str, original: str, resulting: str, suffix: str, author: str) -> str:
    """Plain text, then a pending tracked change mid-paragraph, then more
    plain text -- the shape that exercises quote-locate spanning a pending
    boundary."""
    return (
        "<w:p>"
        f"<w:r><w:t>{prefix}</w:t></w:r>"
        f'<w:del w:id="1" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        f'<w:ins w:id="2" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting}</w:t></w:r></w:ins>"
        f"<w:r><w:t>{suffix}</w:t></w:r>"
        "</w:p>"
    )


def _two_cluster_two_author_p() -> str:
    """Two SEPARATE pending clusters from two DIFFERENT authors on one
    paragraph -- the flagship multi-author scenario issue #563 stops
    refusing outright."""
    return (
        "<w:p>"
        "<w:r><w:t>The Term is </w:t></w:r>"
        '<w:del w:id="1" w:author="alice" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:delText>one (1) year</w:delText></w:r></w:del>"
        '<w:ins w:id="2" w:author="alice" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>two (2) years</w:t></w:r></w:ins>"
        "<w:r><w:t>, renewable upon </w:t></w:r>"
        '<w:del w:id="3" w:author="bob" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:delText>written notice</w:delText></w:r></w:del>"
        '<w:ins w:id="4" w:author="bob" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>mutual agreement</w:t></w:r></w:ins>"
        "<w:r><w:t>.</w:t></w:r>"
        "</w:p>"
    )


def _nested_ins_wrapping_del_p(prefix: str, inserted_then_deleted: str, suffix: str) -> str:
    """`<w:ins>` wrapping `<w:del>` -- text inserted, then deleted, before
    ever being accepted. Accept-all must EXCLUDE it entirely (module
    docstring, "Nested markup")."""
    return (
        "<w:p>"
        f"<w:r><w:t>{prefix}</w:t></w:r>"
        '<w:ins w:id="1" w:author="alice" w:date="2026-01-01T00:00:00Z">'
        '<w:del w:id="2" w:author="bob" w:date="2026-01-02T00:00:00Z">'
        f"<w:r><w:delText>{inserted_then_deleted}</w:delText></w:r>"
        "</w:del></w:ins>"
        f"<w:r><w:t>{suffix}</w:t></w:r>"
        "</w:p>"
    )


def _plain_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _field_code_pending_change_p(prefix: str, original: str, resulting: str, suffix: str, author: str) -> str:
    """A pending tracked change living INSIDE a `<w:fldSimple>` field's
    cached-result region -- issue #530's byte-space half: `_splice_accept_
    all` must strip the `<w:del>`/unwrap the `<w:ins>` inside the field
    WITHOUT corrupting the field itself (its `w:instr` attribute, and the
    `<w:fldSimple>` wrapper, must both survive untouched)."""
    return (
        "<w:p>"
        f"<w:r><w:t>{prefix}</w:t></w:r>"
        '<w:fldSimple w:instr=" REF SectionFour \\h ">'
        f'<w:del w:id="1" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        f'<w:ins w:id="2" w:author="{author}" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting}</w:t></w:r></w:ins>"
        "</w:fldSimple>"
        f"<w:r><w:t>{suffix}</w:t></w:r>"
        "</w:p>"
    )


def _content_revision_with_deleted_paragraph_mark_p(
    prefix: str, original: str, resulting: str, tail: str, next_sibling_text: str
) -> str:
    """Two sibling `<w:p>` elements: the FIRST carries an ordinary content
    revision (`prefix` + pending `original` -> `resulting` + `tail`) AND an
    accepted DELETED PARAGRAPH MARK -- `<w:pPr><w:rPr><w:del/></w:rPr>
    </w:pPr>`, Word's record of a proposed merge with the SECOND (plain)
    sibling paragraph. Neither paragraph carries a heading style, so
    `extract_document_paragraphs` groups them as physical-paragraph
    siblings under one `<untitled>` logical paragraph -- `normalize_
    paragraphs` joins their clean texts with a newline (issue #564; was a
    space before that issue) regardless of the paragraph-mark marker (issue
    #563 follow-up finding 3: this is WHY text-space already reads as one
    merged paragraph even though nothing in extraction understands the
    paragraph-mark semantics either -- see `_build_paragraph_record`/
    `_walk_content`, which never walks into `<w:pPr>` at all)."""
    return (
        "<w:p><w:pPr><w:rPr>"
        '<w:del w:id="9" w:author="counterparty" w:date="2026-01-01T00:00:00Z"/>'
        "</w:rPr></w:pPr>"
        f"<w:r><w:t>{prefix}</w:t></w:r>"
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>{original}</w:delText></w:r></w:del>"
        '<w:ins w:id="2" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:t>{resulting}</w:t></w:r></w:ins>"
        f"<w:r><w:t>{tail}</w:t></w:r>"
        "</w:p>"
        f"<w:p><w:r><w:t>{next_sibling_text}</w:t></w:r></w:p>"
    )


def _revision_authors(docx_bytes: bytes) -> set[str]:
    """Every `w:author` attribute on a `<w:ins>` or `<w:del>` anywhere in
    `word/document.xml` -- used to prove no leftover foreign-author markup
    survives (or, on the raw-bytes control, that it does)."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return {
        el.get(_w("author"))
        for el in root.iter()
        if el.tag in (_w("ins"), _w("del")) and el.get(_w("author"))
    }


def _revision_tag_count(docx_bytes: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return sum(1 for el in root.iter() if el.tag in (_w("ins"), _w("del")))


def _paragraph_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(_w("t")))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_cluster_del_removed_ins_unwrapped(failures: list) -> None:
    docx_bytes = _build_docx_bytes(
        _mid_span_pending_p(
            "Payment is due within ", "30", "45", " days of invoice.", "counterparty"
        )
    )
    materialized = stage.materialize_accept_all(docx_bytes)

    if _revision_tag_count(materialized) != 0:
        failures.append(
            "single-cluster materialize left w:ins/w:del markup behind"
        )

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body_text = "".join(_paragraph_text(p) for p in root.iter(_w("p")))
    if body_text != "Payment is due within 45 days of invoice.":
        failures.append(
            f"single-cluster materialize produced wrong text: {body_text!r}"
        )


def test_multi_cluster_multi_author_all_accepted(failures: list) -> None:
    docx_bytes = _build_docx_bytes(_two_cluster_two_author_p())
    materialized = stage.materialize_accept_all(docx_bytes)

    if _revision_tag_count(materialized) != 0:
        failures.append(
            "multi-cluster/multi-author materialize left w:ins/w:del markup behind"
        )

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body_text = "".join(_paragraph_text(p) for p in root.iter(_w("p")))
    expected = "The Term is two (2) years, renewable upon mutual agreement."
    if body_text != expected:
        failures.append(
            f"multi-cluster/multi-author materialize produced wrong text: "
            f"{body_text!r} (expected {expected!r})"
        )


def test_nested_ins_wrapping_del_is_excluded_entirely(failures: list) -> None:
    """The Notes' explicit edge case: inserted-then-deleted text contributes
    NOTHING to the materialized document -- neither inserted nor kept."""
    docx_bytes = _build_docx_bytes(
        _nested_ins_wrapping_del_p("Payment is due within ", "30 ", "days.")
    )
    materialized = stage.materialize_accept_all(docx_bytes)

    if _revision_tag_count(materialized) != 0:
        failures.append("nested w:ins/w:del markup was not fully removed")

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body_text = "".join(_paragraph_text(p) for p in root.iter(_w("p")))
    if body_text != "Payment is due within days.":
        failures.append(
            f"inserted-then-deleted span was not excluded: {body_text!r}"
        )
    if "30" in body_text:
        failures.append(
            "inserted-then-deleted text leaked into the materialized document"
        )


def test_round_trips_and_carries_no_revision_markup(failures: list) -> None:
    """Acceptance criterion, verbatim: 'The materialized docx round-trips
    and contains no w:ins/w:del elements.' One document combining every
    pattern above."""
    body = "".join(
        [
            _mid_span_pending_p("Payment is due within ", "30", "45", " days.", "counterparty"),
            _two_cluster_two_author_p(),
            _nested_ins_wrapping_del_p("Notice period is ", "thirty days", "immediate."),
            _plain_p("This paragraph carries no revisions at all."),
        ]
    )
    docx_bytes = _build_docx_bytes(body)

    try:
        materialized = stage.materialize_accept_all(docx_bytes)
    except ValueError as exc:
        failures.append(f"materialize_accept_all raised on a valid document: {exc}")
        return

    # verify_docx_round_trip is already called internally; call it again
    # explicitly so a future refactor that drops the internal call is still
    # caught here, not just by trusting materialize_accept_all's own claim.
    try:
        redline_generate.verify_docx_round_trip(materialized)
    except ValueError as exc:
        failures.append(f"materialized docx failed its own round-trip check: {exc}")

    if _revision_tag_count(materialized) != 0:
        failures.append(
            f"materialized docx still contains {_revision_tag_count(materialized)} "
            f"w:ins/w:del element(s)"
        )


def test_untouched_content_survives_byte_for_byte(failures: list) -> None:
    """'Touches nothing else': a paragraph with no revisions, and a zip
    entry other than word/document.xml, both survive unchanged."""
    body = "".join(
        [
            _plain_p("This clause was never edited by anyone."),
            _mid_span_pending_p("Payment is due within ", "30", "45", " days.", "counterparty"),
        ]
    )
    docx_bytes = _build_docx_bytes(body, extra_parts={"docProps/core.xml": _CORE_PROPS_XML})
    materialized = stage.materialize_accept_all(docx_bytes)

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        if zf.read("docProps/core.xml").decode("utf-8") != _CORE_PROPS_XML:
            failures.append("a zip entry other than word/document.xml was modified")
        root = ET.fromstring(zf.read("word/document.xml"))
    paragraphs = list(root.iter(_w("p")))
    if _paragraph_text(paragraphs[0]) != "This clause was never edited by anyone.":
        failures.append(
            "an untouched paragraph's text changed during materialization: "
            f"{_paragraph_text(paragraphs[0])!r}"
        )


def test_multi_author_document_now_normalizes_with_a_disclosure_note(failures: list) -> None:
    """Acceptance criterion, verbatim: 'A doc with pending changes from two
    authors on one paragraph normalizes (was: whole-document refusal), with
    a disclosure note.'"""
    docx_bytes = _build_docx_bytes(_two_cluster_two_author_p())
    result = stage.extract_and_normalize(docx_bytes)

    if result["status"] != "normalized":
        failures.append(
            f"a two-author pending-change paragraph must normalize, not "
            f"refuse: got status={result['status']!r}"
        )
        return

    paragraphs = result.get("paragraphs") or []
    if not paragraphs or paragraphs[0]["text"] != (
        "The Term is two (2) years, renewable upon mutual agreement."
    ):
        failures.append(f"unexpected normalized text: {paragraphs}")

    notes = result.get("normalization_notes") or ""
    if not notes:
        failures.append("multi-author accept-all must record a disclosure note, never silently")
    if "2" not in notes:
        failures.append(f"disclosure note should name the cluster/author counts: {notes!r}")


def test_raw_bytes_still_leak_the_counterpartys_pending_markup(failures: list) -> None:
    """THE GAP, characterized: this test PASSES by asserting that the
    pre-materialization defect is still reproducible against raw bytes -- a
    redline patch applied straight to RAW, unmaterialized bytes locates and
    applies (see below), but the counterparty's own pending revisions
    survive in the delivered document, tangled around (and, here, literally
    nested inside) the toaster's new redline -- not the single canonical,
    already-accepted document issue #563 requires. A green run here means
    the defect still exists (i.e. `materialize_accept_all` remains
    load-bearing); it does NOT mean the defect was closed.

    Note on the ticket's premise: issue #563 said to "watch this test fail
    first" against raw bytes, expecting a LOCATE failure. That did not
    reproduce -- `apply_quote_patches` locates and applies cleanly against
    raw bytes (`result["applied"]` is True below). The actual pre-fix defect
    is not a locate/apply failure; it is that the counterparty's own pending
    tracked-change authors (alice/bob) survive into the delivered redline
    when the editor is fed raw, unmaterialized bytes."""
    docx_bytes = _build_docx_bytes(_two_cluster_two_author_p())
    normalized = stage.extract_and_normalize(docx_bytes)
    source_quote = normalized["paragraphs"][0]["text"]

    result = rqa.apply_quote_patches(
        docx_bytes,
        [{
            "source_quote": source_quote,
            "new_text": "three (3) years, renewable upon written consent of both parties",
            "rationale": "regression fixture",
        }],
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    if not result["applied"] or result["docx_bytes"] is None:
        failures.append(
            "expected the patch to apply against raw bytes (locate succeeds "
            "even though the result is not canonical) -- if this now fails "
            "to apply at all, this test's premise has changed and it needs "
            "re-examining, not just re-asserting"
        )
        return

    authors = _revision_authors(result["docx_bytes"])
    if not ({"alice", "bob"} & authors):
        failures.append(
            "EXPECTED (documenting the pre-materialization gap): the "
            "counterparty's own pending revisions (alice/bob) should still "
            "leak into a patch applied against raw bytes. They did not -- "
            "if apply_quote_patches or docx-editor changed to clean these "
            "up on its own, materialize_accept_all may no longer be "
            "load-bearing for this property; re-examine before treating "
            "this as a regression."
        )


def test_materialized_bytes_deliver_a_clean_single_author_redline(failures: list) -> None:
    """THE FIX, end to end (issue #563's motivating property): the SAME
    source_quote, drawn from the accept-all text of a paragraph whose raw
    XML had pending changes mid-span from two authors, LOCATES and APPLIES
    via `redline_quote_apply.apply_quote_patches` against the MATERIALIZED
    bytes -- and, unlike the raw-bytes case above, the delivered document
    carries ONLY the toaster's own new redline. No counterparty author
    survives: the pending changes were already physically accepted before
    the patch ever ran."""
    docx_bytes = _build_docx_bytes(_two_cluster_two_author_p())
    normalized = stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        failures.append(f"fixture must normalize; got {normalized['status']!r}")
        return
    source_quote = normalized["paragraphs"][0]["text"]

    materialized = stage.materialize_accept_all(docx_bytes)

    result = rqa.apply_quote_patches(
        materialized,
        [{
            "source_quote": source_quote,
            "new_text": "three (3) years, renewable upon written consent of both parties",
            "rationale": "regression fixture",
        }],
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )

    if not result["applied"] or result["docx_bytes"] is None:
        failures.append(
            f"the patch must locate and apply against materialized bytes; "
            f"applied={result['applied']} flag_only={result['flag_only']}"
        )
        return

    authors = _revision_authors(result["docx_bytes"])
    if authors != {"contract-toaster"}:
        failures.append(
            f"the delivered redline must carry ONLY the toaster's own "
            f"revisions once the input was materialized; got authors={authors!r}"
        )

    with zipfile.ZipFile(io.BytesIO(result["docx_bytes"])) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body_text = "".join(_paragraph_text(p) for p in root.iter(_w("p")))
    if "three (3) years" not in body_text:
        failures.append(f"the new proposed text is missing from the delivered document: {body_text!r}")
    if "one (1) year" in body_text or "written notice" in body_text:
        failures.append(
            f"the counterparty's original pre-revision text leaked into the "
            f"delivered document: {body_text!r}"
        )


def test_deleted_paragraph_mark_pins_known_unmerged_limitation(failures: list) -> None:
    """Issue #563 follow-up finding 3, pinned as documented current
    behavior (see `materialize_accept_all`'s and `_splice_accept_all`'s
    KNOWN LIMITATION docstring sections, and ARCHITECTURE.md's Input
    normalization section): a paragraph carrying both a content revision
    and an accepted DELETED PARAGRAPH MARK normalizes to ONE logical
    paragraph in TEXT space, but `materialize_accept_all` does NOT merge
    the two `<w:p>` siblings -- the materialized bytes still carry two
    `<w:p>` elements, one logical paragraph short of what stage 1 already
    reads. This test pins that gap rather than silently tolerating it: if
    a future change adds paragraph-mark-merge semantics, this test must be
    updated (not just re-asserted) to expect ONE `<w:p>`."""
    body = _content_revision_with_deleted_paragraph_mark_p(
        "Payment is due within ", "60", "45", " days", "of invoice."
    )
    docx_bytes = _build_docx_bytes(body)

    normalized = stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        failures.append(f"fixture must normalize; got {normalized['status']!r}")
        return
    text_space_paragraphs = normalized["paragraphs"]
    if len(text_space_paragraphs) != 1:
        failures.append(
            f"fixture premise broken: expected ONE logical (text-space) "
            f"paragraph, got {len(text_space_paragraphs)}: {text_space_paragraphs}"
        )
        return
    text_space_body = text_space_paragraphs[0]["text"]
    if text_space_body != "Payment is due within 45 days\nof invoice.":
        failures.append(
            f"fixture premise broken: unexpected text-space merged text: "
            f"{text_space_body!r}"
        )

    materialized = stage.materialize_accept_all(docx_bytes)
    if _revision_tag_count(materialized) != 0:
        failures.append(
            "deleted-paragraph-mark materialize left w:ins/w:del markup behind"
        )

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    materialized_paragraphs = list(root.iter(_w("p")))

    # PINS THE LIMITATION: two <w:p> elements survive, not one merged
    # paragraph -- the counterparty's proposed paragraph-mark deletion was
    # stripped but never applied.
    if len(materialized_paragraphs) != 2:
        failures.append(
            f"expected the documented limitation (paragraph mark stripped "
            f"but siblings NOT merged -> 2 <w:p> elements survive), got "
            f"{len(materialized_paragraphs)}: "
            f"{[_paragraph_text(p) for p in materialized_paragraphs]}"
        )
        return

    first_text = _paragraph_text(materialized_paragraphs[0])
    second_text = _paragraph_text(materialized_paragraphs[1])
    if first_text != "Payment is due within 45 days":
        failures.append(f"unexpected first materialized paragraph text: {first_text!r}")
    if second_text != "of invoice.":
        failures.append(f"unexpected second materialized paragraph text: {second_text!r}")

    # The stripped paragraph-mark marker leaves a vestigial empty
    # <w:pPr><w:rPr/></w:pPr> behind on the first paragraph (docstrings'
    # own claim) -- pinned here too.
    first_ppr = materialized_paragraphs[0].find(_w("pPr"))
    if first_ppr is None:
        failures.append(
            "expected a vestigial <w:pPr> to remain on the first paragraph "
            "after the paragraph-mark <w:del> was stripped"
        )
    else:
        first_rpr = first_ppr.find(_w("rPr"))
        if first_rpr is None:
            failures.append("expected a vestigial <w:pPr><w:rPr/></w:pPr> to remain")
        elif list(first_rpr):
            failures.append(
                f"expected the vestigial <w:rPr/> to be empty (its own <w:del> "
                f"stripped), still has children: {[c.tag for c in first_rpr]}"
            )


def test_field_code_pending_change_materializes_without_corrupting_the_field(failures: list) -> None:
    """Issue #530's byte-space half, verbatim from the ticket's Notes:
    'confirm materialize_accept_all strips w:del/unwraps w:ins inside a
    field code without corrupting the field.' `_splice_accept_all` has no
    field-specific special case at all -- it recurses into every element
    generically, so a `<w:fldSimple>` wrapping a pending change is stripped/
    unwrapped exactly like ordinary run content, and the `<w:fldSimple>`
    element itself (its `w:instr` attribute in particular) is never touched
    since it is neither a `<w:ins>` nor a `<w:del>`. Round-trip verified via
    the SAME `materialize_accept_all` call every other test here uses, not
    a separate, weaker check."""
    docx_bytes = _build_docx_bytes(
        _field_code_pending_change_p(
            "Obligations under ", "this Section 4", "this Section 5", " survive termination.", "counterparty"
        )
    )

    try:
        materialized = stage.materialize_accept_all(docx_bytes)
    except ValueError as exc:
        failures.append(f"materialize_accept_all raised on a field-code pending change: {exc}")
        return

    # Round-trips as a valid, openable .docx -- asserted, not eyeballed.
    try:
        redline_generate.verify_docx_round_trip(materialized)
    except ValueError as exc:
        failures.append(f"materialized field-code document failed its own round-trip check: {exc}")

    if _revision_tag_count(materialized) != 0:
        failures.append("field-code materialize left w:ins/w:del markup behind")

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")
        root = ET.fromstring(document_xml)

    # The field itself survives, untouched -- its w:instr attribute is
    # exactly what it was before materialization.
    field_els = list(root.iter(_w("fldSimple")))
    if len(field_els) != 1:
        failures.append(f"expected exactly one surviving <w:fldSimple>, got {len(field_els)}")
    elif field_els[0].get(_w("instr")) != " REF SectionFour \\h ":
        failures.append(
            f"the field's w:instr attribute was corrupted by materialization: "
            f"{field_els[0].get(_w('instr'))!r}"
        )

    body_text = "".join(_paragraph_text(p) for p in root.iter(_w("p")))
    if body_text != "Obligations under this Section 5 survive termination.":
        failures.append(f"field-code materialize produced wrong text: {body_text!r}")
    if "this Section 4" in body_text:
        failures.append(
            f"the field's pre-edit text leaked into the materialized document: {body_text!r}"
        )


def test_reserved_root_namespace_prefix_survives_materialization(failures: list) -> None:
    """Issue #563 follow-up finding 4: `materialize_accept_all`'s docstring
    claims it survives a reserved `ns<digits>` prefix via the guarded
    `redline_inplace.register_declared_namespaces` reuse (issue #560/#561),
    but nothing in this gate exercised that claim. Mirrors
    `tests/test_reserved_namespace_prefix_560.py::test_a_reserved_prefix_still_produces_a_redline`
    for THIS writer: a document declaring `xmlns:ns0` on the root, carrying
    a genuine pending tracked change, must still materialize successfully
    (no `ValueError`) and the `ns0` binding must survive in the output."""
    docx_bytes = _build_docx_bytes(
        _mid_span_pending_p(
            "Payment is due within ", "30", "45", " days of invoice.", "counterparty"
        ),
        extra_root_ns=' xmlns:ns0="http://schemas.example.com/round-tripped"',
    )

    try:
        materialized = stage.materialize_accept_all(docx_bytes)
    except ValueError as exc:
        failures.append(
            f"a reserved ns<digits> root prefix still raises out of "
            f"materialize_accept_all: {type(exc).__name__}: {exc}"
        )
        return

    if _revision_tag_count(materialized) != 0:
        failures.append("reserved-root-prefix materialize left w:ins/w:del markup behind")

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")
    if 'xmlns:ns0="http://schemas.example.com/round-tripped"' not in document_xml:
        failures.append(
            "the ns0 binding did not survive materialization -- the prefix "
            f"either vanished or was rebound: {document_xml!r}"
        )

    root = ET.fromstring(document_xml.encode("utf-8"))
    body_text = "".join(_paragraph_text(p) for p in root.iter(_w("p")))
    if body_text != "Payment is due within 45 days of invoice.":
        failures.append(
            f"reserved-root-prefix materialize produced wrong text: {body_text!r}"
        )


def test_non_root_namespace_prefix_survives_materialization(failures: list) -> None:
    """Companion to the reserved-root-prefix test above: a real Word
    document can declare a prefix on a NON-root element (`materialize_
    accept_all`'s own docstring, "declare a prefix on a non-root element
    that ElementTree hoists to the root") rather than on the document root
    itself. `redline_inplace._merge_hoisted_namespaces` is the half of the
    shared machinery that carries a HOISTED declaration back into the
    preserved original root tag -- exercised here for this writer for the
    first time."""
    body = (
        '<w:p xmlns:cust="http://schemas.example.com/non-root">'
        # A namespace declaration alone is not enough to make ElementTree
        # preserve/hoist it -- it only tracks a binding actually USED to
        # qualify some element or attribute name (same reason
        # `redline_inplace._merge_hoisted_namespaces`'s own docstring cites
        # a real `<w:drawing>` subtree, not a bare unused declaration). This
        # throwaway `<cust:meta>` element is what makes the prefix "used":
        # `_walk_content` skips it without recursion (not `w:r`/`w:ins`/
        # `w:del`/`w:fldSimple`), same as any other non-allowlisted tag.
        '<cust:meta>ignored</cust:meta>'
        "<w:r><w:t>Notice period is </w:t></w:r>"
        '<w:del w:id="1" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:delText>thirty (30) days</w:delText></w:r></w:del>"
        '<w:ins w:id="2" w:author="counterparty" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>sixty (60) days</w:t></w:r></w:ins>"
        "<w:r><w:t>.</w:t></w:r>"
        "</w:p>"
    )
    docx_bytes = _build_docx_bytes(body)

    try:
        materialized = stage.materialize_accept_all(docx_bytes)
    except ValueError as exc:
        failures.append(
            f"a non-root namespace prefix still raises out of "
            f"materialize_accept_all: {type(exc).__name__}: {exc}"
        )
        return

    if _revision_tag_count(materialized) != 0:
        failures.append("non-root-prefix materialize left w:ins/w:del markup behind")

    with zipfile.ZipFile(io.BytesIO(materialized)) as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")
    if "http://schemas.example.com/non-root" not in document_xml:
        failures.append(
            "the non-root namespace URI did not survive materialization at "
            f"all: {document_xml!r}"
        )

    root = ET.fromstring(document_xml.encode("utf-8"))
    body_text = "".join(_paragraph_text(p) for p in root.iter(_w("p")))
    if body_text != "Notice period is sixty (60) days.":
        failures.append(
            f"non-root-prefix materialize produced wrong text: {body_text!r}"
        )


TESTS = [
    test_single_cluster_del_removed_ins_unwrapped,
    test_multi_cluster_multi_author_all_accepted,
    test_nested_ins_wrapping_del_is_excluded_entirely,
    test_round_trips_and_carries_no_revision_markup,
    test_untouched_content_survives_byte_for_byte,
    test_multi_author_document_now_normalizes_with_a_disclosure_note,
    test_raw_bytes_still_leak_the_counterpartys_pending_markup,
    test_materialized_bytes_deliver_a_clean_single_author_redline,
    test_deleted_paragraph_mark_pins_known_unmerged_limitation,
    test_field_code_pending_change_materializes_without_corrupting_the_field,
    test_reserved_root_namespace_prefix_survives_materialization,
    test_non_root_namespace_prefix_survives_materialization,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        print(("PASS: " if len(failures) == before else "FAIL: ") + test.__name__)

    if failures:
        print()
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print("\nPASS: accept-all is a physical stage-1 document transform (issue #563).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
