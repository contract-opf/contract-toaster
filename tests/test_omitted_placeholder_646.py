#!/usr/bin/env python3
"""
Slice test for issue #646: "a fully-deleted clause must read
`[Intentionally omitted.]`, not lose its heading".

## The bug this proves fixed, and the one it proves un-fixed

Observed in the first successful LIVE-MODEL redline: the model struck an
uncapped indemnity in full with `block_ops: delete_block`, the compiler
deleted the clause's body, and the ACCEPTED document came back reading

    2. Confidentiality      Each party shall protect...
    3. Indemnification                                   <- nothing under it
    4. Term and Termination The Receiving Party's...

A clause's heading is a separate `<w:p>` from its body
(`extraction_normalization_stage.extract_document_paragraphs` lifts the
boundary paragraph into `heading` and appends only the rest to
`physical_paragraphs`), and it carries no block of its own for the delete to
address. Issue #645 answered that by deleting the heading too. The OWNER
REVERSED it in issue #646, reviewing that same production artifact:

    "When we delete the full text of a section, you correctly did not delete
    the section heading, but we should replace the Section text with
    [Intentionally omitted.] to avoid a situation where the clean document
    with track changes accepted just shows a section heading with nothing
    under it that could look like a mistake."

So the heading SURVIVES, the body is tracked-deleted, and
`block_transcript.OMITTED_CLAUSE_PLACEHOLDER` is tracked-INSERTED in its
place:

    3. Indemnification
    [Intentionally omitted.]

The whole point is what the CLEAN COPY says, so every outcome assertion here
is made on the ACCEPTED document (`materialize_accept_all`, then the same
extraction stage), never on the redline XML alone.

## What this file asserts

  1. A `delete_block` on a clause with a heading leaves the accepted document
     reading heading + `[Intentionally omitted.]` -- and leaves every
     NEIGHBOUR untouched. Rejecting the same redline is BYTE-FAITHFUL to the
     source in text space, `<w:p>` for `<w:p>`: the placeholder is content
     the compiler injects, so "it is fully rejectable" is the property that
     matters most about it.
  1b. The mechanism that makes the placeholder survivable, asserted
     directly: the LAST struck `<w:p>` keeps its paragraph MARK (every other
     one has its mark deleted, as an ordinary whole-paragraph deletion
     does). Delete that mark and accept-all merges the paragraph away, so the
     placeholder would land inside the NEXT clause instead.
  2. A clause whose body SURVIVES gets no placeholder: a heading with two
     physical body paragraphs, one of them struck by a span delete, is still
     a heading with a body under it afterwards and needs no omission notice.
  2b. The same property in the shape that would have REGRESSED PRODUCTION: a
     `delete_block` paired with an `insert_block_after` on the same block is
     a clause replaced IN PLACE (that op pair is what
     `third_party_output_integration.build_third_party_block_edits` emits),
     and the replacement belongs under the struck clause's own heading. A
     placeholder there would announce an omission directly above the clause
     that replaced it. The `anchor_block_id == "start"` form of the same
     pairing is driven too -- `"start"` lands the new paragraph under the
     FIRST block's heading.
  2c. The SIBLING half of the gate, driven directly against
     `_plan_omitted_clause_placeholders`: two blocks under one heading, only
     one struck, no placeholder. See "One heading, one block" below for why
     this one is a unit probe and not a document fixture.
  3. The premise the rule is gated on -- one heading `<w:p>` heads exactly
     one logical block -- is itself asserted against the real extractor, on
     a fixture with headings, multi-paragraph bodies, a preamble and a
     spacer. `_plan_omitted_clause_placeholders` re-derives that grouping
     from the paragraph list rather than assuming it, so if this ever stops
     holding the gate keeps its hands off a heading with a surviving body;
     this test is what says so out loud.
  4. A block with NO heading paragraph at all -- the preamble before the
     document's first boundary paragraph, `heading_p_index is None` -- is
     deleted alone, with no placeholder: there is no heading that could be
     left looking empty, and nothing is guessed at.
  5. An empty spacer `<w:p>` between a heading and its body is NOT mistaken
     for the heading: the heading is identified by carried identity
     (`heading_p_index`), never as "the `<w:p>` before the block".
  5b. The TEXT-EQUALITY GUARD on that carried index: a heading `<w:p>` whose
     live accepted text is no longer the text the record was extracted from
     is not treated as evidence about what the accepted document will show,
     so the clause is struck the plain way and no placeholder is written.
     This one is load-bearing and has no backstop -- the projection proofs
     compare the whole document reject-all and accept-all, so they are blind
     to WHICH `<w:p>` a change landed on (forcing the heading index off by
     one delivers bytes with both proofs green). The case driven is a
     counterparty draft carrying a PENDING `<w:ins>` in a heading, where
     `heading_source_text` (the paragraph's ORIGINAL text) and the writer's
     accepted-view comparison legitimately disagree.
  6. A multi-paragraph clause is struck whole and gets exactly ONE
     placeholder, in its LAST `<w:p>`.
  7. Rejecting the redline restores the clause and drops the placeholder.
     This one is enforced by the compiler itself -- `redline_projections`'
     proof 1 compares block HEADINGS as well as texts and no bytes are
     returned unless it holds -- and is asserted here directly as well.

## One heading, one block (and what this file therefore cannot drive)

Issue #646 asks for the placeholder to be withheld where a heading has TWO
body blocks and only one is deleted. `extract_document_paragraphs` starts a
NEW logical block at every boundary paragraph, so a heading `<w:p>` heads
exactly one block: that state is not one this pipeline can produce, and no
DOCUMENT fixture here fabricates it. Test 2c drives the gate function
directly with a hand-built paragraph list instead, which is honest about
what it is -- a unit probe that keeps the branch from rotting, NOT a claim
that production reaches it -- and test 3 pins the 1:1 premise so the day the
extractor changes, this file says so.

What the ticket was reaching for -- "do not announce an omission where
something is still there" -- is reachable in two other shapes, and both are
driven end to end: a heading with two BODY PARAGRAPHS, one of them struck
(test 2), and a struck clause that is REPLACED rather than removed (test
2b), which is the case that would otherwise have regressed the third-party
redline path.

Fixtures are built with python-docx (a test-only dependency, matching
`tests/test_redline_block_ops.py`) plus a dependency-free raw-OOXML builder
for the spacer shape python-docx will not author. All SYNTHETIC: never a
real document, never real party names.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import redline_block_apply  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

AUTHOR = "contract-toaster"
TIMESTAMP = "2026-01-01T00:00:00Z"

# Bound to its single definition, never re-spelled: a test that hard-codes
# the string would still pass if the compiler and the constant drifted apart.
PLACEHOLDER = block_transcript.OMITTED_CLAUSE_PLACEHOLDER


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Synthetic fixture text -- invented for this test, start to finish.
# ---------------------------------------------------------------------------

_TERM_HEADING = "Section 1. Term"
_TERM_BODY = "The Term shall be sixty (60) days from the Effective Date."
_INDEMNITY_HEADING = "Section 2. Indemnification"
_INDEMNITY_BODY = "The Provider shall indemnify the Customer without limitation."
_RENEWAL_HEADING = "Section 3. Renewal"
_RENEWAL_BODY = "The Agreement renews automatically for successive terms."
_PREAMBLE = "This Agreement is entered into by the parties as of the date last written below."
_FIRST_LIMB = "The Provider shall indemnify the Customer for third-party claims."
_SECOND_LIMB = "The Customer shall indemnify the Provider for misuse of the platform."
_SHORT_TERM = "The Term shall be thirty (30) days from the Effective Date."
# A counterparty's own pending edit to a HEADING. See
# `test_a_heading_whose_live_text_moved_is_left_alone`.
_HEADING_AMENDMENT = " (as amended)"
_CAPPED_INDEMNITY = (
    "The Provider shall indemnify the Customer up to the fees paid in the "
    "twelve (12) months before the claim."
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_docx(parts: list) -> bytes:
    """`[("heading", text) | ("body", text) | ("list", text)]` -> docx bytes.

    A heading paragraph carries a real Heading style, so
    `clause_boundaries.is_boundary_paragraph_ooxml` starts a logical block at
    it exactly as it does for a styled counterparty draft.
    """
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for kind, text in parts:
        if kind == "heading":
            document.add_paragraph(text, style="Heading 1")
        elif kind == "list":
            document.add_paragraph(text, style="List Number")
        else:
            paragraph = document.add_paragraph()
            paragraph.add_run(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
    'document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _build_raw_docx(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{WORD_NS}">'
        f"<w:body>{body_paragraphs_xml}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helpers: the real validator, and readers of the ACCEPTED document
# ---------------------------------------------------------------------------


def _prove(docx_bytes: bytes, *, patches=None, ops=None):
    """Run the REAL #620 validator over this transcript against the fixture's
    own block map, so every op the compiler consumes was proven against the
    document's own bytes -- never hand-written."""
    norm = ens.extract_and_normalize(docx_bytes)
    assert norm["status"] == "normalized", norm
    block_map = ens.build_block_map(norm["paragraphs"])
    proven = block_transcript.validate_block_patches(
        list(patches or []), list(ops or []), block_map
    )
    return proven, norm


def _compile(case: str, docx_bytes: bytes, proven: dict, failures: list):
    """Compile a proven transcript and return the delivered bytes, or None
    (having recorded a failure) when nothing was delivered."""
    result = redline_block_apply.apply_block_transcript(
        docx_bytes, proven, author=AUTHOR, timestamp_iso=TIMESTAMP
    )
    if result["failures"]:
        failures.append(f"[{case}] unexpected compiler failures: {result['failures']!r}")
    if not result["docx_bytes"]:
        failures.append(
            f"[{case}] no docx_bytes returned -- the compilation did not pass the "
            f"round-trip and projection gates"
        )
        return None
    return result["docx_bytes"]


def _accepted_blocks(case: str, docx_bytes: bytes, failures: list):
    """`[(heading, text), ...]` of the ACCEPTED document -- the clean copy a
    lawyer sends out, which is the only view an orphaned heading shows up
    in."""
    accepted = ens.extract_and_normalize(ens.materialize_accept_all(docx_bytes))
    if accepted.get("status") != "normalized":
        failures.append(f"[{case}] the accepted document does not normalize: {accepted!r}")
        return None
    return [
        (paragraph.get("heading", ""), paragraph.get("text", ""))
        for paragraph in accepted["paragraphs"]
    ]


def _reject_all(docx_bytes: bytes) -> bytes:
    """The REJECTED document, via the projection module's own rejection --
    the one proof 1 is computed with, so this reader and the compiler's own
    gate disagree about nothing."""
    import redline_projections  # noqa: PLC0415 - kept local to this reader

    return redline_projections.materialize_reject_all(docx_bytes)


def _rejected_blocks(case: str, docx_bytes: bytes, failures: list):
    """`[(heading, text), ...]` of the REJECTED document."""
    rejected = ens.extract_and_normalize(_reject_all(docx_bytes))
    if rejected.get("status") != "normalized":
        failures.append(f"[{case}] the rejected document does not normalize: {rejected!r}")
        return None
    return [
        (paragraph.get("heading", ""), paragraph.get("text", ""))
        for paragraph in rejected["paragraphs"]
    ]


def _document_paragraph_texts(docx_bytes: bytes) -> list:
    """One entry per `<w:p>`: every `<w:t>` AND `<w:delText>` it carries."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return [
        "".join(
            el.text or "" for el in p.iter() if el.tag in (_qn("t"), _qn("delText"))
        )
        for p in root.iter(_qn("p"))
    ]


def _accepted_paragraph_text(docx_bytes: bytes, p_index: int) -> str:
    """The ACCEPTED-view text of the `<w:p>` at `p_index`, read through the
    writer's own reader (`redline_block_apply._accepted_text`) over the
    writer's own paragraph numbering -- so a test about the writer's
    text-equality guard sees exactly what the guard sees."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    paragraphs = redline_block_apply._body_paragraph_elements(root)
    return redline_block_apply._accepted_text(paragraphs[p_index])


def _deleted_paragraph_flags(docx_bytes: bytes) -> list:
    """One bool per `<w:p>`: does it carry any deletion this compiler wrote?"""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return [bool(list(p.iter(_qn("del")))) for p in root.iter(_qn("p"))]


def _deleted_paragraph_mark_flags(docx_bytes: bytes) -> list:
    """One bool per `<w:p>`: is its paragraph MARK marked deleted
    (`<w:pPr><w:rPr><w:del/></w:rPr></w:pPr>`)?

    Distinct from `_deleted_paragraph_flags`, which sees any `<w:del>`
    anywhere in the paragraph: this reads ONLY the mark, which is what
    decides whether accept-all merges the paragraph into its successor.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    flags = []
    for p in root.iter(_qn("p")):
        pPr = p.find(_qn("pPr"))
        rpr = None if pPr is None else pPr.find(_qn("rPr"))
        flags.append(rpr is not None and rpr.find(_qn("del")) is not None)
    return flags


def _inserted_paragraph_texts(docx_bytes: bytes) -> list:
    """One entry per `<w:p>`: the text of every `<w:ins>` THIS COMPILER wrote
    into it, joined. `""` for a paragraph it inserted nothing into.

    Scoped by author, because a counterparty draft can arrive carrying its
    own pending `<w:ins>` (see `test_a_heading_whose_live_text_moved_...`)
    and "did the compiler write a placeholder here?" must not be answerable
    by somebody else's edit.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    texts = []
    for p in root.iter(_qn("p")):
        parts = []
        for ins in p.iter(_qn("ins")):
            if ins.get(_qn("author")) != AUTHOR:
                continue
            parts.extend(el.text or "" for el in ins.iter(_qn("t")))
        texts.append("".join(parts))
    return texts


# ---------------------------------------------------------------------------
# 1. The placeholder itself, in the accepted document
# ---------------------------------------------------------------------------


def test_a_deleted_clause_reads_intentionally_omitted(failures: list) -> None:
    case = "deleted_clause_reads_intentionally_omitted"
    docx_bytes = _make_docx(
        [
            ("heading", _TERM_HEADING),
            ("body", _TERM_BODY),
            ("heading", _INDEMNITY_HEADING),
            ("body", _INDEMNITY_BODY),
            ("heading", _RENEWAL_HEADING),
            ("body", _RENEWAL_BODY),
        ]
    )
    proven, _ = _prove(
        docx_bytes,
        ops=[{"op": "delete_block", "block_id": "p0002", "issue_key": "IND-1"}],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [
        (_TERM_HEADING, _TERM_BODY),
        (_INDEMNITY_HEADING, PLACEHOLDER),
        (_RENEWAL_HEADING, _RENEWAL_BODY),
    ]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r} -- "
            f"issue #646: the heading stays and the clause reads {PLACEHOLDER!r}, so "
            f"the clean copy shows a deliberate striking rather than a mistake"
        )

    # ... and rejecting the redline is byte-faithful to the source in text
    # space. Compared `<w:p>` for `<w:p>`, not block by block: the placeholder
    # is content we INJECTED, so "it is fully rejectable" has to be asserted
    # over the paragraph stream, where an un-rejected insertion or a lost
    # paragraph would show up.
    rejected_bytes = _reject_all(out)
    if _document_paragraph_texts(rejected_bytes) != _document_paragraph_texts(docx_bytes):
        failures.append(
            f"[{case}] rejecting the redline gives paragraphs "
            f"{_document_paragraph_texts(rejected_bytes)!r}, expected the source's "
            f"{_document_paragraph_texts(docx_bytes)!r} -- the placeholder must leave "
            f"no trace when the change is rejected"
        )
    rejected = _rejected_blocks(case, out, failures)
    if rejected is None:
        return
    source = [
        (_TERM_HEADING, _TERM_BODY),
        (_INDEMNITY_HEADING, _INDEMNITY_BODY),
        (_RENEWAL_HEADING, _RENEWAL_BODY),
    ]
    if rejected != source:
        failures.append(
            f"[{case}] rejecting the redline reads {rejected!r}, expected the source "
            f"{source!r} -- the placeholder must be a TRACKED change like any other"
        )


def test_the_paragraph_carrying_the_placeholder_keeps_its_mark(failures: list) -> None:
    """The mechanism, asserted where it can be seen (issue #646 scope note 2).

    `_delete_paragraph` deletes the paragraph MARK of every `<w:p>` it
    strikes -- that is what makes a whole-paragraph deletion whole rather
    than "a paragraph emptied of text". The paragraph carrying the
    placeholder must be the exception: with its mark deleted, accept-all
    merges it into the FOLLOWING paragraph and the placeholder lands inside
    the next clause instead of under its own heading.

    Driven on a TWO-paragraph clause so both halves are visible in one
    document: the first struck `<w:p>` has its mark deleted, the last does
    not.
    """
    case = "placeholder_paragraph_keeps_its_mark"
    docx_bytes = _make_docx(
        [
            ("heading", _INDEMNITY_HEADING),
            ("list", _FIRST_LIMB),
            ("list", _SECOND_LIMB),
            ("heading", _RENEWAL_HEADING),
            ("body", _RENEWAL_BODY),
        ]
    )
    proven, norm = _prove(
        docx_bytes,
        ops=[{"op": "delete_block", "block_id": "p0001", "issue_key": "IND-1"}],
    )
    if len(norm["paragraphs"][0]["physical_spans"]) != 2:
        failures.append(f"[{case}] the fixture clause is not multi-paragraph")
        return
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    marks = _deleted_paragraph_mark_flags(out)
    if marks != [False, True, False, False, False]:
        failures.append(
            f"[{case}] deleted paragraph MARKS are {marks!r}, expected only the FIRST "
            f"struck <w:p>'s -- the last one carries the placeholder and must keep its "
            f"mark, or accept-all merges it into the next clause"
        )
    inserted = _inserted_paragraph_texts(out)
    if inserted != ["", "", PLACEHOLDER, "", ""]:
        failures.append(
            f"[{case}] inserted text per <w:p> is {inserted!r}; exactly one "
            f"{PLACEHOLDER!r} belongs in the clause's LAST <w:p>"
        )

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [(_INDEMNITY_HEADING, PLACEHOLDER), (_RENEWAL_HEADING, _RENEWAL_BODY)]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r} -- "
            f"the placeholder must stay under its OWN heading"
        )


# ---------------------------------------------------------------------------
# 2. Something still under the heading: no placeholder
# ---------------------------------------------------------------------------


def test_a_heading_with_a_surviving_body_gets_no_placeholder(failures: list) -> None:
    """Issue #646's second required check, at the level production reaches.

    A heading with TWO body paragraphs, one of them struck. The surviving
    limb is still under the heading, so nothing has been omitted and no
    notice belongs in the document.
    """
    case = "surviving_body_gets_no_placeholder"
    docx_bytes = _make_docx(
        [
            ("heading", _INDEMNITY_HEADING),
            ("list", _FIRST_LIMB),
            ("list", _SECOND_LIMB),
        ]
    )
    _, norm = _prove(docx_bytes)
    block = norm["paragraphs"][0]
    if len(block["physical_spans"]) != 2:
        failures.append(
            f"[{case}] the fixture clause is not two physical paragraphs: "
            f"{block['physical_spans']!r}"
        )
        return
    first = block["text"][block["physical_spans"][0][0] : block["physical_spans"][0][1]]
    second = block["text"][block["physical_spans"][1][0] : block["physical_spans"][1][1]]

    proven, _ = _prove(
        docx_bytes,
        patches=[
            {
                "block_id": "p0001",
                "segments": [
                    {"op": "delete", "text": first, "issue_key": "IND-1"},
                    {"op": "keep", "text": "\n" + second},
                ],
            }
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [(_INDEMNITY_HEADING, _SECOND_LIMB)]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r} -- "
            f"a heading with a body still under it needs no omission notice"
        )


def test_a_clause_replaced_in_place_gets_no_placeholder(failures: list) -> None:
    """A `delete_block` paired with an `insert_block_after` on the SAME block
    is a whole-clause replacement, not a removal -- the governed text lands
    under the struck clause's own heading, so a placeholder would announce an
    omission directly above the clause that replaced it.

    This op pair is not invented here: it is exactly what
    `third_party_output_integration.build_third_party_block_edits` emits for
    one `reject` finding on a `fixed`-mode topic (see its docstring), and
    `tests/test_third_party_output_integration.py`'s in-place-edit test drives
    it end to end through that producer. Driven here through the same
    validator the compiler consumes, so the gate has a test of its own.
    """
    case = "clause_replaced_in_place"
    docx_bytes = _make_docx(
        [
            ("heading", _TERM_HEADING),
            ("body", _TERM_BODY),
            ("heading", _INDEMNITY_HEADING),
            ("body", _INDEMNITY_BODY),
        ]
    )
    proven, _ = _prove(
        docx_bytes,
        ops=[
            {"op": "delete_block", "block_id": "p0002", "issue_key": "IND-1"},
            {
                "op": "insert_block_after",
                "anchor_block_id": "p0002",
                "new_text": _CAPPED_INDEMNITY,
                "issue_key": "IND-1",
            },
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [(_TERM_HEADING, _TERM_BODY), (_INDEMNITY_HEADING, _CAPPED_INDEMNITY)]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r} -- "
            f"the replacement clause, not an omission notice, goes under that heading"
        )


def test_an_insert_at_start_also_suppresses_the_placeholder(failures: list) -> None:
    """The `anchor_block_id == "start"` branch of the same gate. `"start"`
    names no block: the new paragraph goes BEFORE the first block's first
    physical `<w:p>` -- i.e. UNDER that block's heading -- so deleting the
    first block in the same batch is a replacement too."""
    case = "insert_at_start_suppresses_the_placeholder"
    docx_bytes = _make_docx(
        [
            ("heading", _TERM_HEADING),
            ("body", _TERM_BODY),
            ("heading", _INDEMNITY_HEADING),
            ("body", _INDEMNITY_BODY),
        ]
    )
    proven, _ = _prove(
        docx_bytes,
        ops=[
            {"op": "delete_block", "block_id": "p0001", "issue_key": "TERM-1"},
            {
                "op": "insert_block_after",
                "anchor_block_id": "start",
                "new_text": _SHORT_TERM,
                "issue_key": "TERM-1",
            },
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [(_TERM_HEADING, _SHORT_TERM), (_INDEMNITY_HEADING, _INDEMNITY_BODY)]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r}"
        )


def test_a_sibling_block_under_one_heading_suppresses_the_placeholder(
    failures: list,
) -> None:
    """The SIBLING half of the gate, driven directly.

    Issue #646 requires that a heading with two body BLOCKS, only one of them
    struck, gets no placeholder. `extract_document_paragraphs` cannot produce
    two blocks under one heading (test 3 pins that premise), so there is no
    document fixture for this and none is fabricated: the gate function is
    called directly with a hand-built paragraph list, which is a unit probe
    of the branch and NOT a claim that production reaches this state.

    Both variants are seeded, because a one-variant fixture leaves the branch
    green forever: the SAME paragraph list with BOTH blocks struck must yield
    a placeholder, and exactly one -- a heading is claimed by at most one
    block.
    """
    case = "sibling_block_suppresses_the_placeholder"
    docx_bytes = _make_docx(
        [
            ("heading", _INDEMNITY_HEADING),
            ("list", _FIRST_LIMB),
            ("list", _SECOND_LIMB),
        ]
    )
    _, norm = _prove(docx_bytes)
    real = norm["paragraphs"][0]
    heading_p_index = real["heading_p_index"]
    heading_source_text = real["heading_source_text"]
    if heading_p_index is None:
        failures.append(f"[{case}] the fixture's clause carries no heading paragraph")
        return

    # Two logical blocks, both claiming the SAME heading `<w:p>` -- the shape
    # a future grouping change could introduce.
    paragraphs = [
        {
            "block_id": "p0001",
            "heading": real["heading"],
            "heading_p_index": heading_p_index,
            "heading_source_text": heading_source_text,
            "text": _FIRST_LIMB,
        },
        {
            "block_id": "p0002",
            "heading": real["heading"],
            "heading_p_index": heading_p_index,
            "heading_source_text": heading_source_text,
            "text": _SECOND_LIMB,
        },
    ]
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))

    only_one = redline_block_apply._plan_omitted_clause_placeholders(
        root, paragraphs, {"p0001"}, set()
    )
    if only_one != set():
        failures.append(
            f"[{case}] striking one of two blocks under a heading planned "
            f"placeholders {only_one!r} -- the other block is still under that "
            f"heading, so nothing has been omitted"
        )

    both = redline_block_apply._plan_omitted_clause_placeholders(
        root, paragraphs, {"p0001", "p0002"}, set()
    )
    if both != {"p0001"}:
        failures.append(
            f"[{case}] striking BOTH blocks under a heading planned {both!r}, expected "
            f"exactly one placeholder ({{'p0001'}}) -- one heading, one notice"
        )


def test_one_heading_paragraph_heads_exactly_one_block(failures: list) -> None:
    """The premise the gate is written over.

    `_plan_omitted_clause_placeholders` writes a placeholder only when EVERY
    block a heading heads is being deleted. Today that set always has one
    member, because `extract_document_paragraphs` starts a new logical block
    at every boundary paragraph -- which is why "two body BLOCKS under one
    heading" is not a state this pipeline can be driven into, and why no
    document fixture in this file fabricates one. This asserts that premise
    against the real extractor on a document with headings, a multi-paragraph
    clause, a preamble and a spacer, so a future grouping change shows up
    here rather than as an omission notice quietly written over a surviving
    clause.
    """
    case = "one_heading_one_block"
    docx_bytes = _make_docx(
        [
            ("body", _PREAMBLE),
            ("heading", _TERM_HEADING),
            ("body", _TERM_BODY),
            ("heading", _INDEMNITY_HEADING),
            ("list", _FIRST_LIMB),
            ("list", _SECOND_LIMB),
        ]
    )
    norm = ens.extract_and_normalize(docx_bytes)
    if norm.get("status") != "normalized":
        failures.append(f"[{case}] the fixture does not normalize: {norm!r}")
        return

    by_heading: dict = {}
    for paragraph in norm["paragraphs"]:
        heading_p_index = paragraph.get("heading_p_index")
        if heading_p_index is None:
            continue
        by_heading.setdefault(heading_p_index, []).append(paragraph["block_id"])
    shared = {index: ids for index, ids in by_heading.items() if len(ids) > 1}
    if shared:
        failures.append(
            f"[{case}] a heading <w:p> heads more than one block: {shared!r}. The "
            f"gate in redline_block_apply._plan_omitted_clause_placeholders is written "
            f"over this set and still holds, but issue #646's 'exactly one body block' "
            f"premise no longer does -- go read both."
        )
    if len(by_heading) != 2:
        failures.append(
            f"[{case}] expected two heading paragraphs in the fixture, got "
            f"{sorted(by_heading)!r}"
        )
    # The preamble carries no heading paragraph at all -- the other branch of
    # the same field, seeded here so it is never only the happy variant.
    if norm["paragraphs"][0].get("heading_p_index") is not None:
        failures.append(
            f"[{case}] the preamble block claims a heading paragraph: "
            f"{norm['paragraphs'][0]!r}"
        )


# ---------------------------------------------------------------------------
# 3. The guards: no heading, and a spacer that must not be mistaken for one
# ---------------------------------------------------------------------------


def test_a_block_with_no_heading_paragraph_is_deleted_alone(failures: list) -> None:
    """The document's preamble -- text before the first boundary paragraph --
    is a real, deletable block whose `heading_p_index` is None. There is no
    heading that could be left looking empty, so no placeholder is written
    and nothing before it may be touched."""
    case = "no_heading_paragraph"
    docx_bytes = _make_docx(
        [
            ("body", _PREAMBLE),
            ("heading", _TERM_HEADING),
            ("body", _TERM_BODY),
        ]
    )
    proven, norm = _prove(
        docx_bytes,
        ops=[{"op": "delete_block", "block_id": "p0001", "issue_key": "PRE-1"}],
    )
    if norm["paragraphs"][0].get("heading_p_index") is not None:
        failures.append(f"[{case}] the fixture's first block is not heading-less")
        return
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [(_TERM_HEADING, _TERM_BODY)]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r}"
        )
    deleted = _deleted_paragraph_flags(out)
    if deleted != [True, False, False]:
        failures.append(
            f"[{case}] deletion landed on {deleted!r}; only the preamble <w:p> may be "
            f"struck -- a heading-less block must never make the writer guess an index"
        )
    if _inserted_paragraph_texts(out) != ["", "", ""]:
        failures.append(
            f"[{case}] the redline inserted {_inserted_paragraph_texts(out)!r} -- a "
            f"block with no heading has no heading to be left looking empty"
        )


def test_a_spacer_between_heading_and_body_is_not_the_heading(failures: list) -> None:
    """An empty `<w:p>` between a heading and its clause -- routine in real
    paper, and skipped by the extractor. The heading is identified by carried
    identity, so the spacer must come through untouched and the placeholder
    must land in the BODY `<w:p>`, not in the spacer.
    """
    case = "spacer_is_not_the_heading"
    docx_bytes = _build_raw_docx(
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{_INDEMNITY_HEADING}</w:t></w:r></w:p>"
        "<w:p/>"
        f"<w:p><w:r><w:t>{_INDEMNITY_BODY}</w:t></w:r></w:p>"
    )
    proven, norm = _prove(
        docx_bytes,
        ops=[{"op": "delete_block", "block_id": "p0001", "issue_key": "IND-1"}],
    )
    if norm["paragraphs"][0].get("physical_p_indexes") != [2]:
        failures.append(
            f"[{case}] the fixture's spacer did not land between heading and body: "
            f"{norm['paragraphs'][0]!r}"
        )
        return
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    deleted = _deleted_paragraph_flags(out)
    if deleted != [False, False, True]:
        failures.append(
            f"[{case}] deletion landed on {deleted!r}, expected the BODY <w:p> only -- "
            f"the heading stays and the spacer <w:p> is not the heading"
        )
    inserted = _inserted_paragraph_texts(out)
    if inserted != ["", "", PLACEHOLDER]:
        failures.append(
            f"[{case}] inserted text per <w:p> is {inserted!r}; the placeholder belongs "
            f"in the clause's own <w:p>, never in the spacer"
        )
    texts = _document_paragraph_texts(out)
    if texts != [_INDEMNITY_HEADING, "", _INDEMNITY_BODY + PLACEHOLDER]:
        failures.append(f"[{case}] the redline's text space is {texts!r}")

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    if accepted != [(_INDEMNITY_HEADING, PLACEHOLDER)]:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}; the heading stays and "
            f"its clause reads {PLACEHOLDER!r}"
        )


def test_a_heading_whose_live_text_moved_gets_no_placeholder(failures: list) -> None:
    """The text-equality GUARD, on a document this pipeline actually reaches.

    `_plan_omitted_clause_placeholders` identifies the heading by carried
    index (`heading_p_index`) and then checks that the live `<w:p>` still
    holds the text that record was extracted from. Nothing else protects that
    index. The projection proofs are whole-document reject-all/accept-all
    comparisons and are structurally blind to WHICH element a change landed
    on: proof 1 rejects our revisions and so restores whatever `<w:p>` we
    wrote on, and proof 2 compares a grouping-independent text stream. Force
    the index off by one and both stay green (verified), so this guard is the
    only thing standing between a carried index and a placeholder written on
    a guess -- and it needs a test that fails when it is removed.

    The divergence driven here is not fabricated. A counterparty draft handed
    to the compiler un-materialized -- the input `redline_projections`'
    "What 'our' tracked changes means" section documents and scopes proof 1
    for -- can carry a PENDING `<w:ins>` inside a heading. The extractor takes
    `heading_source_text` from that paragraph's ORIGINAL (reject-all) text,
    while the writer compares the live ACCEPTED view, so the two legitimately
    disagree. The guard declines, the clause is struck the plain way, and the
    orphan it leaves is the pre-#645 outcome -- a worse document than #646
    wants, but strictly better than a placeholder written against a paragraph
    the record is no longer evidence about.
    """
    case = "heading_whose_live_text_moved"
    docx_bytes = _build_raw_docx(
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{_TERM_HEADING}</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>{_TERM_BODY}</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f"<w:r><w:t>{_INDEMNITY_HEADING}</w:t></w:r>"
        f'<w:ins w:id="9001" w:author="Counterparty" w:date="{TIMESTAMP}">'
        f'<w:r><w:t xml:space="preserve">{_HEADING_AMENDMENT}</w:t></w:r>'
        "</w:ins></w:p>"
        f"<w:p><w:r><w:t>{_INDEMNITY_BODY}</w:t></w:r></w:p>"
    )
    proven, norm = _prove(
        docx_bytes,
        ops=[{"op": "delete_block", "block_id": "p0002", "issue_key": "IND-1"}],
    )
    # The premise, asserted rather than assumed: the record's carried heading
    # text and the live paragraph's accepted view really do differ here.
    record = norm["paragraphs"][1]
    live_heading = _accepted_paragraph_text(docx_bytes, record["heading_p_index"])
    if record.get("heading_source_text") != _INDEMNITY_HEADING:
        failures.append(
            f"[{case}] the record carries heading_source_text "
            f"{record.get('heading_source_text')!r}, expected {_INDEMNITY_HEADING!r}"
        )
        return
    if live_heading != _INDEMNITY_HEADING + _HEADING_AMENDMENT:
        failures.append(
            f"[{case}] the live heading reads {live_heading!r} -- the fixture does not "
            f"put the record and the live paragraph in disagreement, so it exercises "
            f"nothing"
        )
        return
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    # THE assertion: the clause is struck WHOLE -- mark included -- and no
    # placeholder is written anywhere.
    if _inserted_paragraph_texts(out) != ["", "", "", ""]:
        failures.append(
            f"[{case}] the redline inserted {_inserted_paragraph_texts(out)!r} -- a "
            f"heading whose live text is no longer the text it was extracted with is "
            f"not evidence about what the accepted document will show under it"
        )
    marks = _deleted_paragraph_mark_flags(out)
    if marks != [False, False, False, True]:
        failures.append(
            f"[{case}] deleted paragraph MARKS are {marks!r}; with no placeholder to "
            f"carry, the struck clause is an ordinary whole-paragraph deletion"
        )
    if _accepted_paragraph_text(out, 2) != _INDEMNITY_HEADING + _HEADING_AMENDMENT:
        failures.append(
            f"[{case}] the surviving heading reads "
            f"{_accepted_paragraph_text(out, 2)!r} -- the counterparty's pending change "
            f"to it must come through untouched"
        )

    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [
        (_TERM_HEADING, _TERM_BODY),
        (_INDEMNITY_HEADING + _HEADING_AMENDMENT, ""),
    ]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r}"
        )


def test_a_multi_paragraph_clause_gets_one_placeholder(failures: list) -> None:
    case = "multi_paragraph_clause_gets_one_placeholder"
    docx_bytes = _make_docx(
        [
            ("heading", _TERM_HEADING),
            ("body", _TERM_BODY),
            ("heading", _INDEMNITY_HEADING),
            ("list", _FIRST_LIMB),
            ("list", _SECOND_LIMB),
        ]
    )
    proven, norm = _prove(
        docx_bytes,
        ops=[{"op": "delete_block", "block_id": "p0002", "issue_key": "IND-1"}],
    )
    if len(norm["paragraphs"][1]["physical_spans"]) != 2:
        failures.append(f"[{case}] the fixture clause is not multi-paragraph")
        return
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    deleted = _deleted_paragraph_flags(out)
    if deleted != [False, False, False, True, True]:
        failures.append(
            f"[{case}] deletion landed on {deleted!r}, expected both bodies of the "
            f"second clause and NOT its heading"
        )
    inserted = _inserted_paragraph_texts(out)
    if inserted != ["", "", "", "", PLACEHOLDER]:
        failures.append(
            f"[{case}] inserted text per <w:p> is {inserted!r}; a clause gets ONE "
            f"placeholder, in its last <w:p>"
        )
    accepted = _accepted_blocks(case, out, failures)
    if accepted is None:
        return
    want = [(_TERM_HEADING, _TERM_BODY), (_INDEMNITY_HEADING, PLACEHOLDER)]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_a_deleted_clause_reads_intentionally_omitted,
    test_the_paragraph_carrying_the_placeholder_keeps_its_mark,
    test_a_heading_with_a_surviving_body_gets_no_placeholder,
    test_a_clause_replaced_in_place_gets_no_placeholder,
    test_an_insert_at_start_also_suppresses_the_placeholder,
    test_a_sibling_block_under_one_heading_suppresses_the_placeholder,
    test_one_heading_paragraph_heads_exactly_one_block,
    test_a_block_with_no_heading_paragraph_is_deleted_alone,
    test_a_spacer_between_heading_and_body_is_not_the_heading,
    test_a_heading_whose_live_text_moved_gets_no_placeholder,
    test_a_multi_paragraph_clause_gets_one_placeholder,
]


def main() -> int:
    failures: list = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for entry in failures[before:]:
                print(f"FAIL: {entry}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all omitted-clause placeholder (issue #646) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
