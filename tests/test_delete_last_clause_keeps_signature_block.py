#!/usr/bin/env python3
"""
Slice test: deleting the LAST numbered clause must not strike the signature
blocks that follow it.

## The defect this proves fixed

Seen on the production default model (an Opus-class reviewer, the first model
to author `delete_block` ops against a real document): a review struck
"11. Indemnification" and "12. Exclusivity" whole. The redline struck clause
12's body AND the two signature-block paragraphs after it, so the ACCEPTED
document ended

    12. Exclusivity
    [Intentionally omitted.]

with no signature blocks at all. Rejecting the redline restored the original
exactly, which is why no gate noticed: only the accept path was wrong.

The mechanism is structural, not a model slip. `block_ops` named ONE block
id -- clause 12's. The signature paragraphs have no heading of their own, so
`extraction_normalization_stage.extract_document_paragraphs` files them as
physical siblings under the most recent boundary paragraph, i.e. as the BODY
of clause 12, and `redline_block_apply` strikes every physical paragraph of
the block it is told to delete. The fix therefore lives in the compiler (and
the projection proof that mirrors it), not in the prompt: a model that
reviews the text it was shown cannot tell the signature block is not part of
clause 12 any better than the extractor can.

`signature_block.find_signature_tail` recognises a trailing signature block;
the compiler leaves it untouched, and refuses (a named per-edit failure, the
issue degrading to flag-only) whenever it cannot tell clause text from
signature text with certainty.

## What this file asserts

  1. The demo's own shape -- numbered clauses, the last one followed by a
     spacer and two unlabeled signature paragraphs, each a party line, a soft
     line break and a `By:` / `Date:` line -- compiles with the signature
     paragraphs UNTOUCHED (no `<w:del>`, mark not deleted), clause 12's
     heading kept, its body struck, and `[Intentionally omitted.]` left under
     the heading. Clause 11 is unchanged from before. Rejecting the redline
     reproduces the source's paragraph stream and block list exactly.
  2. The other common signature shapes are retained too: one paragraph per
     line (party / `By:` / `Name:` / `Title:` / `Date:`), a party line BELOW
     the signature line, and an `IN WITNESS WHEREOF` lead-in.
  3. Clause text is never retained by the heuristic: a final clause whose
     body ends in lines that merely RESEMBLE a signature block (contact
     labels, a blank-field fee line, a short sentence) is struck whole, with
     nothing left behind.
  4. Fail closed, with a named reason and no document change for that edit,
     when the compiler cannot split clause text from signature text: a
     paragraph that carries a signature line AND prose; signature lines
     ahead of text the heuristic does not recognise; and a block that is
     nothing but a signature block. A sibling edit in the same batch still
     lands -- the refusal is per edit.
  5. A clause REPLACED in place (`delete_block` + `insert_block_after` on one
     block) still compiles through the projection proofs, with the signature
     block surviving.
  6. The heuristic itself, as a table, so an edge moves a named case rather
     than a document.

Fixtures are python-docx documents, SYNTHETIC end to end: no real party,
no real contract.

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
import signature_block  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

AUTHOR = "contract-toaster"
TIMESTAMP = "2026-01-01T00:00:00Z"

# Bound to its single definition, never re-spelled.
PLACEHOLDER = block_transcript.OMITTED_CLAUSE_PLACEHOLDER


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Synthetic fixture text -- invented for this test, start to finish.
# ---------------------------------------------------------------------------

_TITLE = "MUTUAL NON-DISCLOSURE AGREEMENT"
_INTRO = "This Agreement is entered into by the parties as of the Effective Date."
_PURPOSE_HEADING = "1. Purpose"
_PURPOSE_BODY = "The parties wish to evaluate a possible business relationship."
_GENERAL_HEADING = "10. General"
_GENERAL_BODY = "This Agreement may be amended only in a writing signed by both parties."
_INDEMNITY_HEADING = "11. Indemnification"
_INDEMNITY_BODY = "Company shall indemnify Counterparty without limitation as to amount."
_EXCLUSIVITY_HEADING = "12. Exclusivity"
_EXCLUSIVITY_BODY = (
    "During the term of this Agreement and for five (5) years afterward, Company "
    "shall not discuss any business relationship with any third party."
)

# The demo's two signature paragraphs: party line, soft line break, signature
# and date line -- ONE `<w:p>` each, so the party line is not a paragraph of
# its own.
_US_SIGNATURE = "Your Organization, LLC\nBy: ______________________    Date: ____________"
_THEM_SIGNATURE = (
    "Fictional Harbor Laboratories, Incorporated\n"
    "By: ______________________    Date: ____________"
)


def _demo_parts() -> list:
    """The shape the failing production run had: numbered clauses, the last
    one followed by a spacer and two unlabeled signature paragraphs."""
    return [
        ("body", _TITLE),
        ("body", _INTRO),
        ("heading", _PURPOSE_HEADING),
        ("body", _PURPOSE_BODY),
        ("heading", _GENERAL_HEADING),
        ("body", _GENERAL_BODY),
        ("heading", _INDEMNITY_HEADING),
        ("body", _INDEMNITY_BODY),
        ("heading", _EXCLUSIVITY_HEADING),
        ("body", _EXCLUSIVITY_BODY),
        ("body", ""),
        ("body", _US_SIGNATURE),
        ("body", _THEM_SIGNATURE),
    ]


# ---------------------------------------------------------------------------
# Fixture builder and readers
# ---------------------------------------------------------------------------


def _make_docx(parts: list) -> bytes:
    """`[("heading" | "body", text), ...]` -> docx bytes. A `"\\n"` inside a
    body text becomes a soft line break (`<w:br/>`), exactly as the demo
    document's signature paragraphs carry theirs."""
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for kind, text in parts:
        if kind == "heading":
            document.add_paragraph(text, style="Heading 1")
        else:
            document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _norm(docx_bytes: bytes) -> dict:
    norm = ens.extract_and_normalize(docx_bytes)
    assert norm["status"] == "normalized", norm
    return norm


def _block_id(norm: dict, heading: str) -> str:
    for paragraph in norm["paragraphs"]:
        if paragraph["heading"] == heading or paragraph["heading"] == heading.split(". ", 1)[-1]:
            return paragraph["block_id"]
    raise AssertionError(f"no block headed {heading!r} in {[p['heading'] for p in norm['paragraphs']]!r}")


def _prove(docx_bytes: bytes, ops: list):
    norm = _norm(docx_bytes)
    block_map = ens.build_block_map(norm["paragraphs"])
    proven = block_transcript.validate_block_patches([], list(ops), block_map)
    return proven, norm


def _delete(norm: dict, heading: str, issue_key: str) -> dict:
    return {"op": "delete_block", "block_id": _block_id(norm, heading), "issue_key": issue_key}


def _compile_raw(docx_bytes: bytes, proven: dict) -> dict:
    return redline_block_apply.apply_block_transcript(
        docx_bytes, proven, author=AUTHOR, timestamp_iso=TIMESTAMP
    )


def _compile(case: str, docx_bytes: bytes, proven: dict, failures: list):
    result = _compile_raw(docx_bytes, proven)
    if result["failures"]:
        failures.append(f"[{case}] unexpected compiler failures: {result['failures']!r}")
    if not result["docx_bytes"]:
        failures.append(
            f"[{case}] no docx_bytes returned -- the compilation did not pass the "
            f"round-trip and projection gates"
        )
        return None
    return result["docx_bytes"]


def _document_root(docx_bytes: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return ET.fromstring(zf.read("word/document.xml"))  # noqa: S314


def _paragraph_texts(docx_bytes: bytes) -> list:
    """One entry per `<w:p>`: every `<w:t>` and `<w:delText>` it carries, with
    a soft line break read as `"\\n"` -- the text layer of the document."""
    texts = []
    for p in _document_root(docx_bytes).iter(_qn("p")):
        pieces = []
        for el in p.iter():
            if el.tag in (_qn("t"), _qn("delText")):
                pieces.append(el.text or "")
            elif el.tag == _qn("br"):
                pieces.append("\n")
        texts.append("".join(pieces))
    return texts


def _accepted_paragraph_texts(docx_bytes: bytes) -> list:
    return _paragraph_texts(ens.materialize_accept_all(docx_bytes))


def _reject_all(docx_bytes: bytes) -> bytes:
    import redline_projections  # noqa: PLC0415 - kept local to this reader

    return redline_projections.materialize_reject_all(docx_bytes)


def _deleted_flags(docx_bytes: bytes) -> list:
    """One bool per `<w:p>`: does it carry any `<w:del>`?"""
    return [bool(list(p.iter(_qn("del")))) for p in _document_root(docx_bytes).iter(_qn("p"))]


def _deleted_mark_flags(docx_bytes: bytes) -> list:
    """One bool per `<w:p>`: is its paragraph MARK marked deleted?"""
    flags = []
    for p in _document_root(docx_bytes).iter(_qn("p")):
        pPr = p.find(_qn("pPr"))
        rpr = None if pPr is None else pPr.find(_qn("rPr"))
        flags.append(rpr is not None and rpr.find(_qn("del")) is not None)
    return flags


def _failure_reasons(result: dict) -> list:
    return [(failure["block_id"], failure["reason"]) for failure in result["failures"]]


# ---------------------------------------------------------------------------
# 1. The demo's shape
# ---------------------------------------------------------------------------


def test_deleting_the_last_clause_keeps_the_signature_blocks(failures: list) -> None:
    case = "demo_shape"
    docx_bytes = _make_docx(_demo_parts())
    norm = _norm(docx_bytes)
    proven, _ = _prove(
        docx_bytes,
        [
            _delete(norm, _INDEMNITY_HEADING, "IND-1"),
            _delete(norm, _EXCLUSIVITY_HEADING, "EXC-1"),
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return

    # The premise the whole defect rests on: the signature paragraphs ARE part
    # of the last clause's block as far as the extractor is concerned.
    last = norm["paragraphs"][-1]
    if len(last["physical_spans"]) != 3:
        failures.append(
            f"[{case}] the fixture's last block has {len(last['physical_spans'])} physical "
            f"paragraphs, expected 3 (body + the two signature paragraphs); the premise "
            f"of the defect has moved"
        )

    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return

    source = _paragraph_texts(docx_bytes)
    accepted = _accepted_paragraph_texts(out)
    want = [
        _TITLE,
        _INTRO,
        _PURPOSE_HEADING,
        _PURPOSE_BODY,
        _GENERAL_HEADING,
        _GENERAL_BODY,
        _INDEMNITY_HEADING,
        PLACEHOLDER,
        _EXCLUSIVITY_HEADING,
        PLACEHOLDER,
        "",
        _US_SIGNATURE,
        _THEM_SIGNATURE,
    ]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r} -- the "
            f"signature blocks must survive the deletion of the last clause, and both "
            f"struck clauses keep their heading and read {PLACEHOLDER!r}"
        )

    # The signature paragraphs are not merely present: no revision touches them.
    deleted = _deleted_flags(out)
    marks = _deleted_mark_flags(out)
    for index in (10, 11, 12):
        if deleted[index] or marks[index]:
            failures.append(
                f"[{case}] paragraph {index} (the spacer / a signature paragraph) carries a "
                f"tracked deletion (del={deleted[index]}, mark={marks[index]}); the compiler "
                f"must leave it alone"
            )
    # ... while the clause's own body IS struck, and the paragraph carrying the
    # placeholder keeps its mark (issue #646's mechanism, unchanged).
    if not deleted[9]:
        failures.append(f"[{case}] clause 12's own body paragraph is not struck")
    if marks[9]:
        failures.append(
            f"[{case}] clause 12's last STRUCK paragraph has its mark deleted, so accept-all "
            f"would merge the placeholder away"
        )

    # Rejecting the redline reproduces the original exactly, in the text layer.
    rejected = _paragraph_texts(_reject_all(out))
    if rejected != source:
        failures.append(
            f"[{case}] rejecting the redline gives {rejected!r}, expected the source's "
            f"{source!r}"
        )
    rejected_blocks = [
        (p["heading"], p["text"]) for p in _norm(_reject_all(out))["paragraphs"]
    ]
    source_blocks = [(p["heading"], p["text"]) for p in norm["paragraphs"]]
    if rejected_blocks != source_blocks:
        failures.append(
            f"[{case}] the rejected document's blocks are {rejected_blocks!r}, expected "
            f"{source_blocks!r}"
        )


# ---------------------------------------------------------------------------
# 2. Other signature shapes are retained too
# ---------------------------------------------------------------------------

_ONE_LINE_PER_PARAGRAPH = [
    ("body", ""),
    ("body", "Your Organization, LLC"),
    ("body", "By: ______________________"),
    ("body", "Name: ______________________"),
    ("body", "Title: _____________________"),
    ("body", "Date: ______________________"),
    ("body", "Fictional Harbor Laboratories, Incorporated"),
    ("body", "By: ______________________"),
    ("body", "Name: Jane Q. Public"),
    ("body", "Title: Chief Executive Officer"),
    ("body", "Date: November 4, 2026"),
]

_PARTY_LINE_BELOW = [
    ("body", "By: ______________________"),
    ("body", "Your Organization, LLC"),
    ("body", "By: ______________________"),
    ("body", "Fictional Harbor Laboratories, Incorporated"),
]

_WITNESS_LEAD_IN = [
    (
        "body",
        "IN WITNESS WHEREOF, the parties have caused this Agreement to be executed "
        "by their duly authorized representatives as of the Effective Date.",
    ),
    ("body", "Your Organization, LLC"),
    ("body", "By: ______________________    Date: ____________"),
    ("body", "Fictional Harbor Laboratories, Incorporated"),
    ("body", "By: ______________________    Date: ____________"),
]


def _retained_shape(case: str, tail_parts: list, failures: list) -> None:
    parts = [
        ("heading", _PURPOSE_HEADING),
        ("body", _PURPOSE_BODY),
        ("heading", _EXCLUSIVITY_HEADING),
        ("body", _EXCLUSIVITY_BODY),
        *tail_parts,
    ]
    docx_bytes = _make_docx(parts)
    norm = _norm(docx_bytes)
    proven, _ = _prove(docx_bytes, [_delete(norm, _EXCLUSIVITY_HEADING, "EXC-1")])
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return
    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return
    accepted = _accepted_paragraph_texts(out)
    want = [
        _PURPOSE_HEADING,
        _PURPOSE_BODY,
        _EXCLUSIVITY_HEADING,
        PLACEHOLDER,
        *[text for _, text in tail_parts],
    ]
    if accepted != want:
        failures.append(f"[{case}] the accepted document reads {accepted!r}, expected {want!r}")
    if _paragraph_texts(_reject_all(out)) != _paragraph_texts(docx_bytes):
        failures.append(f"[{case}] rejecting the redline did not reproduce the source")


def test_one_line_per_paragraph_signature_blocks_are_kept(failures: list) -> None:
    _retained_shape("one_line_per_paragraph", _ONE_LINE_PER_PARAGRAPH, failures)


def test_a_party_line_below_the_signature_line_is_kept(failures: list) -> None:
    _retained_shape("party_line_below", _PARTY_LINE_BELOW, failures)


def test_an_in_witness_lead_in_is_kept(failures: list) -> None:
    _retained_shape("in_witness_lead_in", _WITNESS_LEAD_IN, failures)


# ---------------------------------------------------------------------------
# 3. Clause text is never retained
# ---------------------------------------------------------------------------


def test_lookalike_lines_are_struck_as_clause_text(failures: list) -> None:
    """The heuristic's false-positive direction. A last clause whose body ENDS
    in lines that resemble a signature block but are not one is struck whole:
    contact labels, a blank-field fee line, a short sentence. Retaining any of
    them would leave real clause text behind a deleted clause, silently."""
    case = "lookalike_lines"
    lookalikes = [
        "Notices to Company shall be sent to:",
        "Attn: General Counsel",
        "Email: counsel@example.com",
        "Monthly fee: $__________ per month",
        "Neither party shall assign this Agreement.",
    ]
    docx_bytes = _make_docx(
        [
            ("heading", _PURPOSE_HEADING),
            ("body", _PURPOSE_BODY),
            ("heading", _EXCLUSIVITY_HEADING),
            ("body", _EXCLUSIVITY_BODY),
            *[("body", line) for line in lookalikes],
        ]
    )
    norm = _norm(docx_bytes)
    proven, _ = _prove(docx_bytes, [_delete(norm, _EXCLUSIVITY_HEADING, "EXC-1")])
    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return
    # Non-empty paragraphs only: a multi-paragraph strike leaves an empty
    # `<w:p>` per struck paragraph in `materialize_accept_all`'s output (its
    # documented `_splice_accept_all` limitation -- it strips a deleted
    # paragraph MARK without applying the merge), which says nothing about
    # what the clean copy reads.
    accepted = [text for text in _accepted_paragraph_texts(out) if text]
    want = [_PURPOSE_HEADING, _PURPOSE_BODY, _EXCLUSIVITY_HEADING, PLACEHOLDER]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r} -- clause "
            f"text that merely resembles a signature block must be struck with its clause"
        )


# ---------------------------------------------------------------------------
# 4. Fail closed, with a named reason
# ---------------------------------------------------------------------------


def _refused(case: str, tail_parts: list, reason: str, failures: list) -> None:
    """Delete clause 12 AND clause 10 in one batch; clause 12's delete must be
    refused with `reason`, clause 10's must still land, and clause 12 must be
    in the accepted document in full."""
    docx_bytes = _make_docx(
        [
            ("heading", _PURPOSE_HEADING),
            ("body", _PURPOSE_BODY),
            ("heading", _GENERAL_HEADING),
            ("body", _GENERAL_BODY),
            ("heading", _EXCLUSIVITY_HEADING),
            ("body", _EXCLUSIVITY_BODY),
            *tail_parts,
        ]
    )
    norm = _norm(docx_bytes)
    proven, _ = _prove(
        docx_bytes,
        [
            _delete(norm, _EXCLUSIVITY_HEADING, "EXC-1"),
            _delete(norm, _GENERAL_HEADING, "GEN-1"),
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return
    result = _compile_raw(docx_bytes, proven)
    exclusivity_block = _block_id(norm, _EXCLUSIVITY_HEADING)
    if _failure_reasons(result) != [(exclusivity_block, reason)]:
        failures.append(
            f"[{case}] failures are {_failure_reasons(result)!r}, expected exactly "
            f"{[(exclusivity_block, reason)]!r}"
        )
    if reason not in redline_block_apply.FAILURE_REASONS:
        failures.append(f"[{case}] {reason!r} is not in redline_block_apply.FAILURE_REASONS")
    for failure in result["failures"]:
        # Escaped text only: a refusal names the block and counts paragraphs,
        # and carries no excerpt of the document.
        if any(
            fragment in failure["detail"]
            for fragment in (_EXCLUSIVITY_BODY[:20], "Your Organization", "By:")
        ):
            failures.append(f"[{case}] the failure detail quotes document text: {failure['detail']!r}")
    if not result["docx_bytes"]:
        failures.append(f"[{case}] the sibling delete did not land: no docx_bytes returned")
        return
    accepted = _accepted_paragraph_texts(result["docx_bytes"])
    want = [
        _PURPOSE_HEADING,
        _PURPOSE_BODY,
        _GENERAL_HEADING,
        PLACEHOLDER,
        _EXCLUSIVITY_HEADING,
        _EXCLUSIVITY_BODY,
        *[text for _, text in tail_parts],
    ]
    if accepted != want:
        failures.append(
            f"[{case}] the accepted document reads {accepted!r}, expected {want!r} -- the "
            f"refused delete must leave its clause (and signature block) completely alone, "
            f"and the sibling delete must still land"
        )


def test_a_paragraph_mixing_prose_and_a_signature_line_is_refused(failures: list) -> None:
    _refused(
        "mixed_paragraph",
        [("body", "Company shall bear all costs of performance.\nBy: ______________________")],
        redline_block_apply.REASON_DELETE_BLOCK_SIGNATURE_AMBIGUOUS,
        failures,
    )


def test_signature_lines_ahead_of_unrecognised_text_are_refused(failures: list) -> None:
    """A signature line that is NOT in a clean tail -- here the party name and
    title sit under the line in a shape the heuristic does not know -- means
    the compiler cannot say where the clause ends. It refuses rather than
    strike a signature block or keep clause text."""
    _refused(
        "signature_lines_before_unrecognised_text",
        [
            ("body", "______________________"),
            ("body", "Jane Q. Public, speaking for the Company"),
        ],
        redline_block_apply.REASON_DELETE_BLOCK_SIGNATURE_AMBIGUOUS,
        failures,
    )


def test_a_block_that_is_only_a_signature_block_is_refused(failures: list) -> None:
    case = "signature_block_only"
    docx_bytes = _make_docx(
        [
            ("heading", _PURPOSE_HEADING),
            ("body", _PURPOSE_BODY),
            ("heading", "Execution"),
            ("body", _US_SIGNATURE),
            ("body", _THEM_SIGNATURE),
        ]
    )
    norm = _norm(docx_bytes)
    proven, _ = _prove(docx_bytes, [_delete(norm, "Execution", "SIG-1")])
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return
    result = _compile_raw(docx_bytes, proven)
    want = [(_block_id(norm, "Execution"), redline_block_apply.REASON_DELETE_BLOCK_SIGNATURE_BLOCK)]
    if _failure_reasons(result) != want:
        failures.append(f"[{case}] failures are {_failure_reasons(result)!r}, expected {want!r}")
    if result["docx_bytes"]:
        failures.append(f"[{case}] a redline was delivered for an edit that was refused outright")


# ---------------------------------------------------------------------------
# 5. A clause replaced in place keeps the signature block
# ---------------------------------------------------------------------------


def test_replacing_the_last_clause_in_place_keeps_the_signature_block(failures: list) -> None:
    """`delete_block` + `insert_block_after` on one block is a clause REPLACED
    in place. The projection proofs must hold for it with a retained tail, and
    the signature block must survive. Where the new paragraph sits relative to
    the tail is deliberately NOT asserted: an insertion anchors on the block's
    last physical paragraph, which is a separate behaviour (see the follow-up
    in the commit message)."""
    case = "replace_in_place"
    docx_bytes = _make_docx(_demo_parts())
    norm = _norm(docx_bytes)
    block_id = _block_id(norm, _EXCLUSIVITY_HEADING)
    replacement = "Company may discuss a business relationship with any third party."
    proven, _ = _prove(
        docx_bytes,
        [
            {"op": "delete_block", "block_id": block_id, "issue_key": "EXC-1"},
            {
                "op": "insert_block_after",
                "anchor_block_id": block_id,
                "new_text": replacement,
                "issue_key": "EXC-1",
            },
        ],
    )
    if proven["status"] != "proven":
        failures.append(f"[{case}] fixture transcript did not prove: {proven['failures']!r}")
        return
    out = _compile(case, docx_bytes, proven, failures)
    if out is None:
        return
    accepted = _accepted_paragraph_texts(out)
    for signature in (_US_SIGNATURE, _THEM_SIGNATURE):
        if signature not in accepted:
            failures.append(f"[{case}] a signature paragraph is missing from {accepted!r}")
    if replacement not in accepted:
        failures.append(f"[{case}] the replacement clause is missing from {accepted!r}")
    if _EXCLUSIVITY_BODY in accepted:
        failures.append(f"[{case}] the struck clause body survived in {accepted!r}")


# ---------------------------------------------------------------------------
# 6. The heuristic, as a table
# ---------------------------------------------------------------------------

#: (name, paragraph texts, expected `start`, expected `mixed_at`)
_TAIL_TABLE = [
    ("no paragraphs", [], 0, None),
    ("plain clause", ["Company shall pay.", "Company shall also report."], 2, None),
    ("one signature paragraph", ["Body.", "By: ________"], 1, None),
    (
        "party line + By line in one paragraph",
        ["Body.", "Acme Holdings, Inc.\nBy: ________    Date: ________"],
        1,
        None,
    ),
    (
        "label lines with values",
        ["Body.", "By: /s/ Jane Q. Public", "Name: Jane Q. Public", "Title: Chief Executive Officer"],
        1,
        None,
    ),
    ("slash-s signature", ["Body.", "/s/ Jane Q. Public"], 1, None),
    ("underscore line then party caption", ["Body.", "__________", "Acme Corp."], 1, None),
    (
        "party caption then signature line",
        ["Body.", "Acme Corp.", "By: ________"],
        1,
        None,
    ),
    (
        "lead-in then signature lines",
        ["Body.", "In witness whereof, the parties have executed this Agreement.", "By: ________"],
        1,
        None,
    ),
    ("lead-in alone is not a signature block", ["Body.", "In witness whereof, the parties agree."], 2, None),
    ("a lone party caption is not a signature block", ["Body.", "Acme Corp."], 2, None),
    ("bracketed signature page marker", ["Body.", "[Signature page follows]", "By: ________"], 1, None),
    # --- lookalikes: never retained -------------------------------------
    ("contact labels are not signature labels", ["Body.", "Attn: General Counsel", "Email: a@example.com"], 3, None),
    ("a blank-field line that does not start with a label", ["Body.", "Fee: $________ per month"], 2, None),
    ("a short sentence is not a caption", ["Body.", "Neither party shall assign."], 2, None),
    ("a colon-ended lead-in to a list", ["Body.", "The following are excluded:"], 2, None),
    # --- ambiguity: refuse ------------------------------------------------
    ("prose + signature line in one paragraph", ["Body.", "Company pays all costs.\nBy: ________"], 2, 1),
    ("label line with a long prose value", ["Body.", "Date: the later of the dates on which the parties each sign this Agreement below."], 2, 1),
    ("mixed paragraph above a clean tail", ["Body.", "Company pays.\nBy: ____", "By: ________"], 3, 1),
    # --- signature evidence that is not a clean tail ----------------------
    ("signature line then unrecognised text", ["Body.", "__________", "Jane Q. Public, speaking for the Company"], 3, None),
]


def test_the_signature_tail_heuristic_table(failures: list) -> None:
    for name, texts, want_start, want_mixed in _TAIL_TABLE:
        tail = signature_block.find_signature_tail(texts)
        # `start` for a refused / unrecognised case is not meaningful beyond
        # "there is no clean tail"; only the unambiguous cases pin it.
        ambiguous = tail.ambiguous
        expect_ambiguous = want_mixed is not None or name.startswith("signature line then")
        if ambiguous != expect_ambiguous:
            failures.append(
                f"[{name}] ambiguous={ambiguous!r}, expected {expect_ambiguous!r} ({tail!r})"
            )
            continue
        if ambiguous:
            continue
        if tail.start != want_start:
            failures.append(f"[{name}] tail starts at {tail.start}, expected {want_start} ({tail!r})")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_deleting_the_last_clause_keeps_the_signature_blocks,
    test_one_line_per_paragraph_signature_blocks_are_kept,
    test_a_party_line_below_the_signature_line_is_kept,
    test_an_in_witness_lead_in_is_kept,
    test_lookalike_lines_are_struck_as_clause_text,
    test_a_paragraph_mixing_prose_and_a_signature_line_is_refused,
    test_signature_lines_ahead_of_unrecognised_text_are_refused,
    test_a_block_that_is_only_a_signature_block_is_refused,
    test_replacing_the_last_clause_in_place_keeps_the_signature_block,
    test_the_signature_tail_heuristic_table,
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
    print("PASS: all delete-last-clause signature-block assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
