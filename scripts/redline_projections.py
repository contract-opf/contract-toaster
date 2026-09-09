#!/usr/bin/env python3
"""
Projection proofs for a compiled block-mode redline (issue #623).

`scripts/redline_block_apply.py` compiles a PROVEN block transcript
(`scripts/block_transcript.py`) into OOXML tracked changes. Until this
module existed, the only gate on its output was
`redline_generate.verify_docx_round_trip` -- which proves the ZIP opens and
every XML part parses, and says NOTHING about whether the tracked changes
say what the transcript said. A writer bug that dropped a sentence, mangled
a character outside every revision, or smuggled a part into the package
would sail through it.

This module closes that gap with three FORMAL projections of the delivered
package, all three of which must hold before a document is delivered:

1. **Reject-all == source.** Take the output, reject every tracked change
   this compiler made (drop `<w:ins>` subtrees, unwrap `<w:del>` and retag
   its `<w:delText>` back to `<w:t>`, un-delete paragraph marks), extract
   the result through the SAME extraction stage the transcript was proven
   against, and require the block map to come back IDENTICAL to the input
   document's -- same ids, same HEADINGS, same texts, block by block. A
   clause's heading is its own extracted field, never part of `text`, so it
   has to be compared explicitly or a renumbered or re-titled section would
   pass. This is what proves the redline is SURGICAL: nothing outside a
   tracked change moved. Since issue #646 a `delete_block` that empties a
   clause also INSERTS `[Intentionally omitted.]` into it, and this proof is
   what says that insertion is fully rejectable -- the placeholder is new
   content the compiler injects, so "reject-all is byte-faithful to the
   original" is the assertion that matters most about it.
   What this proof does NOT say is WHICH `<w:p>` a revision landed on:
   rejecting our revisions restores whatever paragraph we wrote them on, and
   proof 2 compares a grouping-independent text STREAM, so an edit written
   into a neighbouring element reproduces both projections. Every writer
   here therefore carries its own carried-identity + text-equality guard
   (`redline_block_apply._resolve_physical_paragraphs` for a body paragraph,
   `_plan_omitted_clause_placeholders` for the heading a placeholder is
   decided by), and nothing in this module backstops one if it is weakened.

2. **Accept-all == final.** Take the output, accept every tracked change
   (`extraction_normalization_stage.materialize_accept_all`, reused rather
   than reimplemented), extract it, and require the resulting text to equal
   the model-approved final text: each edited block's ops as delivered
   (`block_transcript.delivered_final_text` -- its `final_text` less the
   duplicative boundary spaces issue #644 drops, which is the ONLY licensed
   divergence and is whitespace-only), every `delete_block`'s block absent
   (or, where the compiler left a placeholder under an emptied heading,
   reading exactly `block_transcript.OMITTED_CLAUSE_PLACEHOLDER` -- issue
   #646), every `insert_block_after`'s `new_text` present in its anchored
   position.

3. **Part allowlist.** Only the parts a redline legitimately touches may
   differ between input and output package; every other part must come back
   unchanged, and no part may appear or disappear outside that set.
   `word/settings.xml` is held to a narrower rule still -- an APPENDED
   `<w:rsid>` and nothing else -- because that one part also carries
   `<w:documentProtection>` and `<w:trackChanges>`, so exempting it wholesale
   would let a redline go out edit-locked, or with revision tracking
   switched off, and still pass the proof. `word/styles.xml` has a narrower
   rule of its own (issue #647): a redline may APPEND the two footnote
   styles a footnote number needs to render as superscript, and may create
   the part outright for a package that has none, but may not rewrite or
   drop a single style the document already defines.

## What "our" tracked changes means, and why proof 1 is scoped to them

`apply_block_transcript` is designed to run on the ACCEPT-ALL MATERIALIZED
input (`extraction_normalization_stage.materialize_accept_all`), which by
construction carries no pending revisions of its own -- there, "reject the
revisions we made" and "reject all revisions" are the same operation, which
is the proof as issue #623 states it.

They are NOT the same when a caller hands the compiler a document that
still carries a counterparty's pending tracked changes: `extract_and_
normalize` reads that document in its ACCEPT-ALL text-space disposition
(`normalize_input._normalize_paragraph`), so a projection that rejected the
counterparty's insertions too would be compared against a source that
accepted them, and would fail-closed on a document nothing is wrong with.
Passing `revision_ids=` scopes the rejection to exactly the `w:id`s this
compilation created (the union of `apply_block_transcript`'s
`revision_ids_by_issue`), which makes proof 1 read as what it actually
means: *our* redline, rejected, leaves the document we were handed exactly
as we were handed it. With `revision_ids=None` every revision is rejected,
which is the correct and stricter reading for a materialized input.

## Known limitation, inherited not introduced

Neither projection MERGES `<w:p>` siblings across a paragraph mark whose
insertion/deletion it disposed of -- the same limitation
`extraction_normalization_stage._splice_accept_all` documents at length for
the accept side. It does not weaken these proofs, because a paragraph left
behind that way is EMPTY, and `extract_document_paragraphs` skips a `<w:p>`
with no text and no revisions. It does mean an empty logical paragraph in
the projected document is invisible to proof 2, which compares the
document's TEXT content; proof 1 keeps the strict block-by-block form.

## Fail-closed

Every proof failure -- including an exception raised while computing a
projection -- is a structured entry in the returned `failures` list, never
a raised exception and never a silent pass. `verify_projections` returns
`{"status": "verified"|"failed", "failures": [...]}`; a caller that gets
anything other than `"verified"` must deliver no document.

Usage:
    import redline_projections

    report = redline_projections.verify_projections(
        input_docx_bytes, output_docx_bytes, proven,
        revision_ids={rid for ids in by_issue.values() for rid in ids},
        applied_edits=applied_edit_specs,   # None == "every edit applied"
    )
    if report["status"] != "verified":
        ...  # deliver nothing
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import docx_parts  # noqa: E402
import ooxml_util  # noqa: E402

WORD_NS = ooxml_util.WORD_NS
DOCUMENT_PART = ooxml_util.DOCUMENT_PART
_w = ooxml_util.w

# The transcript vocabulary, bound to its single definition
# (`scripts/block_transcript.py`) rather than re-spelled here.
OP_DELETE_BLOCK = block_transcript.OP_DELETE_BLOCK
OP_INSERT_BLOCK_AFTER = block_transcript.OP_INSERT_BLOCK_AFTER
ANCHOR_START = block_transcript.ANCHOR_START

#: The three proofs, named in every failure entry so a diagnostic report can
#: say WHICH projection rejected the document rather than only that one did.
PROOF_REJECT_ALL = "reject_all"
PROOF_ACCEPT_ALL = "accept_all"
PROOF_PART_ALLOWLIST = "part_allowlist"

PROOFS = (PROOF_REJECT_ALL, PROOF_ACCEPT_ALL, PROOF_PART_ALLOWLIST)

# ---------------------------------------------------------------------------
# The part allowlist (proof 3)
#
# The first six are the parts issue #623 names -- the ones a redline WRITES:
# `word/document.xml` carries the tracked changes; `word/footnotes.xml`,
# `word/_rels/document.xml.rels` and `[Content_Types].xml` carry the
# footnoted rationales (`redline_block_apply.inject_issue_footnotes`);
# `word/header1.xml` / `word/footer1.xml` carry the export marker
# (`redline_generate.inject_export_marker_and_footnotes`).
#
# `word/people.xml` joins them as a MEASURED artifact of the pinned
# `docx-editor` package that pass 1 of the compiler drives: opening and
# saving a document through it CREATES that part -- the `w15:people`
# authorship part Word writes alongside tracked changes (its relationship
# and content-type entries are already allowlisted above). It did not exist
# in the input, it carries no document TEXT, and it is not optional: it is
# what happens when the compiler's own writer saves.
#
# `word/settings.xml` is deliberately NOT in this list, even though the same
# save touches it too. The delta there, measured on a real compilation, is
# exactly one appended `<w:rsid w:val="..."/>` under `<w:rsids>` -- the
# revision-save id any editor stamps -- and nothing else. Allowlisting the
# whole part to excuse that one element would also excuse
# `<w:documentProtection w:edit="readOnly">` (a redline delivered
# edit-locked) and a dropped `<w:trackChanges/>` (revision tracking silently
# switched off), which live in this same part; the proof whose stated job is
# to say no part changed that a redline may not touch would pass both.
# `_verify_settings_rsids_only` permits the measured delta and nothing else.
#
# `word/styles.xml` is likewise NOT in this list, and for the same reason
# (issue #647). A footnote number is superscript because its run carries
# `<w:rStyle w:val="FootnoteReference"/>` and the package DEFINES that style;
# a document that has never carried a footnote defines neither it nor
# `FootnoteText`, so `redline_block_apply._footnote_styles_part` appends the
# missing definitions -- and creates the part outright for a package that has
# none. That is the ONLY delta permitted here. Allowlisting the whole part to
# excuse it would also excuse a rewritten `Normal`, a deleted `Heading1`, or a
# `<w:docDefaults>` swapped for another font -- a wholesale reformat of the
# counterparty's paper, waved through by the proof whose stated job is to say
# no part changed that a redline may not touch.
# `_verify_styles_footnote_additions_only` permits the two named additions and
# nothing else, comparing them against the SAME
# `docx_parts.FOOTNOTE_STYLE_XML` the writer emits from, so the proof
# and the writer cannot drift into agreeing about a definition neither states.
#
# Everything else in the package -- numbering, theme, media, custom XML,
# docProps -- must come back unchanged.
# ---------------------------------------------------------------------------
DECLARED_REDLINE_PARTS = (
    "word/document.xml",
    "word/footnotes.xml",
    "word/_rels/document.xml.rels",
    "[Content_Types].xml",
    "word/header1.xml",
    "word/footer1.xml",
)
DOCX_EDITOR_ARTIFACT_PARTS = ("word/people.xml",)
ALLOWED_CHANGED_PARTS = frozenset(DECLARED_REDLINE_PARTS + DOCX_EDITOR_ARTIFACT_PARTS)

#: The part with the narrower, element-level rule above. Kept OUT of
#: `ALLOWED_CHANGED_PARTS` so a `word/settings.xml` that appears in, or
#: disappears from, the package is a violation like any other part's would
#: be: only a settings part present on BOTH sides gets the rsid comparison.
SETTINGS_PART = "word/settings.xml"
_RSIDS_TAG = _w("rsids")
_RSID_TAG = _w("rsid")
_VAL_ATTR = _w("val")

#: The other narrow-rule part (issue #647). Also kept OUT of
#: `ALLOWED_CHANGED_PARTS`: a redline may CREATE this part for a package that
#: has none, but may never drop one, and a part present on both sides gets
#: `_verify_styles_footnote_additions_only` rather than a blanket pass.
STYLES_PART = docx_parts.STYLES_PART
_STYLE_TAG = _w("style")
_STYLE_ID_ATTR = _w("styleId")

#: How many per-block mismatches a `reject_all` failure spells out before it
#: truncates. A whole-document divergence would otherwise paste the entire
#: contract into a diagnostic report.
_MAX_REPORTED_MISMATCHES = 5

#: How much context a text-stream mismatch carries around the first
#: divergence, from each side.
_CONTEXT_CHARS = 80


# ---------------------------------------------------------------------------
# Reject-all materialization (the mirror of
# `extraction_normalization_stage.materialize_accept_all`)
# ---------------------------------------------------------------------------


#: `<w:delText>`/`<w:delInstrText>` are how OOXML spells text that lives
#: inside a `<w:del>`. Rejecting the deletion puts the text back into the
#: document proper, so the tags go back too -- the exact inverse of
#: `redline_block_apply._DELETED_TEXT_TAGS`.
_UNDELETED_TEXT_TAGS = {
    _w("delText"): _w("t"),
    _w("delInstrText"): _w("instrText"),
}


def _is_owned(el: ET.Element, owned_ids: Optional[set]) -> bool:
    """Whether this `<w:ins>`/`<w:del>` is one the projection should reject.

    `owned_ids is None` means "reject every revision" (the correct reading
    for a materialized input, which carries none of its own). Otherwise only
    the `w:id`s the caller names are ours; a revision with no id, or an id
    that does not parse, is somebody else's and is left exactly as it is --
    fail-closed in the direction that never silently discards a
    counterparty's pending change.
    """
    if owned_ids is None:
        return True
    raw = el.get(_w("id"))
    if raw is None:
        return False
    try:
        return int(raw) in owned_ids
    except ValueError:
        return False


def _undelete_text_tags(el: ET.Element) -> None:
    """Retag every `<w:delText>`/`<w:delInstrText>` under `el` back to
    `<w:t>`/`<w:instrText>`, WITHOUT descending into a `<w:del>` that
    survived (somebody else's pending deletion, whose text is still deleted
    text and must keep its tags)."""
    for child in list(el):
        if child.tag == _w("del"):
            continue
        _undelete_text_tags(child)
    replacement = _UNDELETED_TEXT_TAGS.get(el.tag)
    if replacement is not None:
        el.tag = replacement


def _splice_reject_all(el: ET.Element, owned_ids: Optional[set]) -> None:
    """Mutate `el`'s children in place, REJECTING every owned tracked change
    under them: an owned `<w:ins>` is removed entirely (including its
    subtree -- text that was only ever proposed never existed), and an owned
    `<w:del>` is unwrapped, its children spliced back in at its position with
    their `<w:delText>` retagged `<w:t>`.

    Deliberately the mirror image of
    `extraction_normalization_stage._splice_accept_all`, down to the
    process-children-FIRST recursion: an owned `<w:del>` nested inside a
    counterparty's `<w:ins>` (the shape `delete_block` writes when it strikes
    a clause the other side had proposed) is unwrapped where it sits, leaving
    the counterparty's insertion intact.

    A `<w:ins>`/`<w:del>` living in `<w:pPr><w:rPr>` is a PARAGRAPH MARK
    marker, not run content; it is disposed of like any other by this same
    recursion (an owned inserted mark is dropped, an owned deleted mark is
    unwrapped to nothing), which is exactly the "un-delete the paragraph
    mark" half of the projection. See the module docstring for why not
    merging the `<w:p>` siblings is sound here.
    """
    original_children = list(el)
    for child in original_children:
        el.remove(child)
    for child in original_children:
        if child.tag == _w("ins") and _is_owned(child, owned_ids):
            continue  # rejected entirely, including all descendants
        if child.tag == _w("del") and _is_owned(child, owned_ids):
            _splice_reject_all(child, owned_ids)  # reject nested content FIRST
            _undelete_text_tags(child)
            for grandchild in list(child):
                el.append(grandchild)
            continue
        _splice_reject_all(child, owned_ids)
        el.append(child)


def materialize_reject_all(
    docx_bytes: bytes, *, revision_ids: Optional[Iterable[int]] = None
) -> bytes:
    """Physically REJECT the tracked changes named by `revision_ids` (or every
    one of them, when `revision_ids is None`) in `word/document.xml`.

    The mirror of `extraction_normalization_stage.materialize_accept_all`:
    every other zip entry is copied through byte for byte, and no tag or
    attribute outside the disposed revisions is ever touched. Uses the same
    guarded `ooxml_util` namespace-preservation dance every writer in this
    repo uses, so a document declaring a prefix ElementTree would rename (or
    one declaring a prefix on a non-root element) survives the round trip.

    Unlike `materialize_accept_all` this does NOT round-trip-verify its
    output: the projection is an internal analysis artifact that is never
    delivered to anybody, and `extract_document_paragraphs` parses it
    immediately.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}

    if DOCUMENT_PART not in originals:
        raise ValueError(f"Not a valid WordprocessingML .docx: {DOCUMENT_PART} is missing.")

    document_xml_text = originals[DOCUMENT_PART].decode("utf-8")
    original_root_open_tag = ooxml_util.root_open_tag(document_xml_text)
    ooxml_util.register_declared_namespaces(
        ooxml_util.declared_namespaces_anywhere(document_xml_text)
    )

    root = ET.fromstring(originals[DOCUMENT_PART])
    owned_ids = None if revision_ids is None else {int(rid) for rid in revision_ids}
    _splice_reject_all(root, owned_ids)

    serialized = ET.tostring(root, encoding="unicode")
    auto_root_open_tag = ooxml_util.root_open_tag(serialized)
    body_and_close = serialized[len(auto_root_open_tag) :]
    root_open_tag = ooxml_util.merge_hoisted_namespaces(
        original_root_open_tag, auto_root_open_tag
    )
    new_document_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + root_open_tag.encode("utf-8")
        + body_and_close.encode("utf-8")
    )

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for info in infos:
            data = (
                new_document_xml
                if info.filename == DOCUMENT_PART
                else originals[info.filename]
            )
            zf_out.writestr(info, data)
    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _failure(proof: str, detail: str, **extra: Any) -> dict[str, Any]:
    entry = {"proof": proof, "detail": detail}
    entry.update(extra)
    return entry


def _block_texts(norm: dict[str, Any]) -> list[tuple[str, str]]:
    """`(block_id, text)` for every logical paragraph, in document order.

    BODY text only. `extraction_normalization_stage` splits a clause into a
    `heading` field and a `text` field (see `extract_document_paragraphs`,
    which lifts the boundary `<w:p>` into `heading` and appends only the rest
    to `physical_paragraphs`), so this accessor is deliberately blind to the
    heading -- callers that need to prove the WHOLE block did not move must
    use `_block_headings_and_texts` instead.
    """
    return [
        (paragraph["block_id"], paragraph.get("text", "") or "")
        for paragraph in norm["paragraphs"]
    ]


def _block_headings_and_texts(norm: dict[str, Any]) -> list[tuple[str, str, str]]:
    """`(block_id, heading, text)` for every logical paragraph, in order.

    The reject-all proof compares this, not `_block_texts`: a clause's heading
    (`"Section 2. Fees"`) lives in its own field, never in `text`, so a
    comparison over `text` alone would let a renumbered or re-titled section
    through the gate untouched. Every field the extractor produces for a block
    has to be on both sides of the proof for "nothing outside a tracked change
    moved" to mean what it says.

    Kept SEPARATE from `_block_texts` because the accept-all proof's
    `_text_stream` comparison is deliberately grouping-independent: an
    inserted or deleted paragraph legitimately moves the heading boundaries
    around, so folding headings into that stream turns a correct document red.
    """
    return [
        (
            paragraph["block_id"],
            paragraph.get("heading", "") or "",
            paragraph.get("text", "") or "",
        )
        for paragraph in norm["paragraphs"]
    ]


def _text_stream(texts: Iterable[str]) -> str:
    """The document's TEXT content as one comparable string.

    Physical paragraphs are joined into a logical block with `"\\n"`
    (`normalize_paragraphs`) and each physical paragraph's clean text is
    `.strip()`ped (`normalize_input._normalize_paragraph`), so a stream built
    by splitting on `"\\n"`, stripping each piece and dropping the empties is
    GROUPING-INDEPENDENT: it reads the same whether the extractor grouped an
    inserted paragraph into its anchor's block or started a new block at it,
    and the same whether a wholly-deleted block left an empty logical
    paragraph behind or none at all.

    Applying it to BOTH sides is also what keeps the comparison honest about
    whitespace: an edit that deletes `"The Term"` and leaves the paragraph
    reading `" is X."` extracts as `"is X."`, so the expected side must be
    stripped on the same boundaries or the proof would fail on a document
    nothing is wrong with. Nothing else is normalized -- interior spacing,
    casing and punctuation all still have to match exactly.
    """
    pieces = []
    for text in texts:
        for piece in text.split("\n"):
            stripped = piece.strip()
            if stripped:
                pieces.append(stripped)
    return "\n".join(pieces)


def _first_divergence(expected: str, actual: str) -> str:
    """A short, human-readable description of where two text streams first
    differ -- the offset plus `_CONTEXT_CHARS` from each side."""
    limit = min(len(expected), len(actual))
    offset = limit
    for index in range(limit):
        if expected[index] != actual[index]:
            offset = index
            break
    start = max(0, offset - _CONTEXT_CHARS // 2)
    return (
        f"first divergence at character {offset}: "
        f"expected ...{expected[start:offset + _CONTEXT_CHARS]!r}... "
        f"but the projection reads ...{actual[start:offset + _CONTEXT_CHARS]!r}..."
    )


# ---------------------------------------------------------------------------
# Proof 1: reject-all == source
# ---------------------------------------------------------------------------


def _verify_reject_all(
    source_norm: dict[str, Any],
    output_docx_bytes: bytes,
    revision_ids: Optional[Iterable[int]],
) -> list[dict[str, Any]]:
    try:
        projected_bytes = materialize_reject_all(
            output_docx_bytes, revision_ids=revision_ids
        )
    except Exception as exc:  # noqa: BLE001 - any failure here is fail-closed
        return [
            _failure(
                PROOF_REJECT_ALL,
                f"the reject-all projection could not be built: "
                f"{type(exc).__name__}: {exc}",
            )
        ]

    projected_norm = extraction_normalization_stage.extract_and_normalize(projected_bytes)
    if projected_norm.get("status") != "normalized":
        return [
            _failure(
                PROOF_REJECT_ALL,
                "the reject-all projection does not normalize, so it cannot be "
                f"compared to the source (status={projected_norm.get('status')!r})",
            )
        ]

    expected = _block_headings_and_texts(source_norm)
    actual = _block_headings_and_texts(projected_norm)

    failures: list[dict[str, Any]] = []
    if len(expected) != len(actual):
        failures.append(
            _failure(
                PROOF_REJECT_ALL,
                f"rejecting the redline leaves {len(actual)} block(s), but the source "
                f"document has {len(expected)}",
            )
        )

    reported = 0
    for index, (expected_entry, actual_entry) in enumerate(zip(expected, actual)):
        expected_block_id, expected_heading, expected_text = expected_entry
        actual_block_id, actual_heading, actual_text = actual_entry
        if expected_entry == actual_entry:
            continue
        if reported >= _MAX_REPORTED_MISMATCHES:
            failures.append(
                _failure(
                    PROOF_REJECT_ALL,
                    f"... and further mismatches after block {expected_block_id!r} "
                    "(truncated)",
                )
            )
            break
        reported += 1
        failures.append(
            _failure(
                PROOF_REJECT_ALL,
                (
                    f"block {index + 1} of the reject-all projection is "
                    f"{actual_block_id!r} headed {actual_heading!r} reading "
                    f"{actual_text!r}, but the source's is {expected_block_id!r} "
                    f"headed {expected_heading!r} reading {expected_text!r} -- "
                    "rejecting the redline did not reproduce the source document"
                ),
                block_id=expected_block_id,
            )
        )
    return failures


# ---------------------------------------------------------------------------
# Proof 2: accept-all == the model-approved final text
# ---------------------------------------------------------------------------


def _apply_edit_tuples(source_text: str, tuples: list[tuple[int, int, str]]) -> str:
    """`source_text` with every `(start, end, replacement)` applied.

    Walked in ASCENDING order with a cursor, which needs no offset
    translation: the transcript's edits never overlap (`block_transcript`
    proves them against a single sequential walk of the block). Ties are
    broken by `end`, which puts a zero-width INSERTION before a deletion
    beginning at the same offset -- exactly the order the transcript wrote
    them in, since `block_transcript._prove_patch` stamps an `insert` written
    before its `delete` with `at == delete.start` and one written after it
    with `at == delete.end`.
    """
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(tuples, key=lambda t: (t[0], t[1])):
        if start > cursor:
            pieces.append(source_text[cursor:start])
        pieces.append(replacement)
        cursor = max(cursor, end)
    pieces.append(source_text[cursor:])
    return "".join(pieces)


def _placeholder_block_ids_from_transcript(
    source_docx_bytes: bytes, source_norm: dict[str, Any], proven: dict[str, Any]
) -> set:
    """Which of `proven`'s `delete_block`s leave `[Intentionally omitted.]`
    behind, for the `applied_edits is None` mode (issue #646).

    With a compiler record in hand this question is already answered -- the
    placeholder is that edit's `insert_text`. Without one, "every edit in the
    transcript applied" has to be projected forward, and whether a delete
    leaves a placeholder is a property of the SOURCE DOCUMENT's heading
    structure and of the whole batch, not of the op alone. So this asks
    `redline_block_apply`'s own planner rather than re-deriving the rule:
    a second implementation of a structural gate is a second thing to drift.

    That makes this half of proof 2 agree with the compiler by construction
    for the placeholder DECISION -- exactly as it already does for the issue
    #644 boundary trim, and for the same reason. What proof 2 still proves is
    that the compiler WROTE what it decided, in the right block, and nothing
    else; that a placeholder is rejectable is proof 1's job.
    """
    # Local import: `redline_block_apply` imports this module, so the
    # dependency can only run this way round at call time.
    import redline_block_apply  # noqa: PLC0415

    deleted: set = set()
    anchored: set = set()
    block_ids_in_order = [
        record.get("block_id")
        for record in source_norm.get("paragraphs") or []
        if record.get("block_id")
    ]
    for block_op in proven.get("block_ops") or []:
        if block_op["op"] == OP_DELETE_BLOCK:
            deleted.add(block_op["block_id"])
        elif block_op["anchor_block_id"] == ANCHOR_START:
            # `"start"` names no block: the new paragraph lands before the
            # FIRST block's first `<w:p>`, i.e. under that block's heading.
            if block_ids_in_order:
                anchored.add(block_ids_in_order[0])
        else:
            anchored.add(block_op["anchor_block_id"])
    if not deleted:
        return set()

    # Read-only: this parse is never serialized back, so it needs none of
    # `materialize_reject_all`'s namespace-preservation dance.
    with zipfile.ZipFile(io.BytesIO(source_docx_bytes)) as zf:
        root = ET.fromstring(zf.read(DOCUMENT_PART))
    return redline_block_apply._plan_omitted_clause_placeholders(
        root, source_norm.get("paragraphs") or [], deleted, anchored
    )


def _expected_accept_all_texts(
    source_docx_bytes: bytes,
    source_norm: dict[str, Any],
    proven: dict[str, Any],
    applied_edits: Optional[list[dict[str, Any]]],
) -> list[str]:
    """The block texts the accept-all projection must read, in document order.

    With `applied_edits is None` this is the transcript's own promise -- each
    edited block's ops as DELIVERED, every `block_op` in force. Delivered,
    not `final_text`: the compiler drops duplicative boundary spaces from
    insert texts (issue #644), so `block_transcript.delivered_final_text` is
    what the document is entitled to say and `final_text` -- the promise as
    the model authored it -- is a space too long at any such boundary. Both
    sides of this proof call the SAME function, so the trim can never be the
    thing that fails it.

    With a list, it is the promise RESTRICTED to what actually landed:
    `apply_block_transcript` fails closed per edit and never lets one edit's
    failure block its siblings, so a batch where one span edit was refused
    still delivers the rest, and the proof has to be against the delivered
    set or it would fail-closed on a document that is exactly right. Those
    `insert_text`s are already trimmed, having come from
    `_pair_ops_into_edits`.

    `source_docx_bytes` is the document the transcript was proven against.
    It is read in the `applied_edits is None` mode only, to project forward
    which `delete_block`s leave an `[Intentionally omitted.]` placeholder
    (issue #646) -- a question about the source's heading structure that the
    normalized paragraph list alone cannot answer, because the rule guards
    on the LIVE heading paragraph's text.
    """
    block_map = extraction_normalization_stage.build_block_map(source_norm["paragraphs"])

    final_text_by_block: dict[str, str] = {}
    deleted_block_ids: set[str] = set()
    # `block_id -> the text a struck block still shows` (issue #646): the
    # `[Intentionally omitted.]` placeholder where the compiler left one, and
    # nothing at all otherwise.
    struck_block_text: dict[str, str] = {}
    inserts_by_anchor: dict[str, list[str]] = {}

    if applied_edits is None:
        for block_id in _placeholder_block_ids_from_transcript(
            source_docx_bytes, source_norm, proven
        ):
            struck_block_text[block_id] = block_transcript.OMITTED_CLAUSE_PLACEHOLDER
        for block in proven.get("blocks") or []:
            # NOT `block["final_text"]`: that is the model's promise verbatim,
            # and the compiler is entitled to drop a duplicative boundary
            # space from an insert (issue #644). Rebuilding from the ops
            # through the same rule keeps this proof about the writers.
            final_text_by_block[block["block_id"]] = block_transcript.delivered_final_text(
                block["ops"]
            )
        for block_op in proven.get("block_ops") or []:
            if block_op["op"] == OP_DELETE_BLOCK:
                deleted_block_ids.add(block_op["block_id"])
            else:
                inserts_by_anchor.setdefault(block_op["anchor_block_id"], []).append(
                    block_op["new_text"]
                )
    else:
        tuples_by_block: dict[str, list[tuple[int, int, str]]] = {}
        for spec in applied_edits:
            kind = spec.get("kind")
            block_id = spec.get("block_id")
            if kind == OP_DELETE_BLOCK:
                deleted_block_ids.add(block_id)
                # A delete that left a placeholder recorded it as its own
                # `insert_text` (issue #646); an ordinary one recorded "".
                struck_block_text[block_id] = spec.get("insert_text", "") or ""
            elif kind == OP_INSERT_BLOCK_AFTER:
                inserts_by_anchor.setdefault(block_id, []).append(spec.get("insert_text", ""))
            else:
                tuples_by_block.setdefault(block_id, []).append(
                    (spec["start"], spec["end"], spec.get("insert_text", ""))
                )
        for block_id, tuples in tuples_by_block.items():
            live = block_map.get(block_id)
            if live is None:  # pragma: no cover - defensive
                continue
            final_text_by_block[block_id] = _apply_edit_tuples(
                live.get("text", "") or "", tuples
            )

    expected: list[str] = []
    expected.extend(inserts_by_anchor.get(ANCHOR_START, []))
    for block_id, live in block_map.items():
        if block_id in deleted_block_ids:
            # Struck wholesale: contributes nothing, unless the compiler left
            # `[Intentionally omitted.]` standing under an emptied heading.
            expected.append(struck_block_text.get(block_id, ""))
        else:
            expected.append(final_text_by_block.get(block_id, live.get("text", "") or ""))
        expected.extend(inserts_by_anchor.get(block_id, []))
    return expected


def _verify_accept_all(
    source_docx_bytes: bytes,
    source_norm: dict[str, Any],
    output_docx_bytes: bytes,
    proven: dict[str, Any],
    applied_edits: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    try:
        projected_bytes = extraction_normalization_stage.materialize_accept_all(
            output_docx_bytes
        )
    except Exception as exc:  # noqa: BLE001 - any failure here is fail-closed
        return [
            _failure(
                PROOF_ACCEPT_ALL,
                f"the accept-all projection could not be built: "
                f"{type(exc).__name__}: {exc}",
            )
        ]

    projected_norm = extraction_normalization_stage.extract_and_normalize(projected_bytes)
    if projected_norm.get("status") != "normalized":
        return [
            _failure(
                PROOF_ACCEPT_ALL,
                "the accept-all projection does not normalize, so it cannot be "
                f"compared to the final text (status={projected_norm.get('status')!r})",
            )
        ]

    try:
        expected_texts = _expected_accept_all_texts(
            source_docx_bytes, source_norm, proven, applied_edits
        )
    except Exception as exc:  # noqa: BLE001 - any failure here is fail-closed
        return [
            _failure(
                PROOF_ACCEPT_ALL,
                f"the expected final text could not be derived from the transcript: "
                f"{type(exc).__name__}: {exc}",
            )
        ]

    expected_stream = _text_stream(expected_texts)
    actual_stream = _text_stream(text for _, text in _block_texts(projected_norm))
    if expected_stream == actual_stream:
        return []
    return [
        _failure(
            PROOF_ACCEPT_ALL,
            (
                "accepting the redline does not reproduce the model-approved final "
                f"text: {_first_divergence(expected_stream, actual_stream)}"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Proof 3: the part allowlist
# ---------------------------------------------------------------------------


def _canonical_part(data: bytes) -> Any:
    """A comparable form of one package part.

    XML parts are compared CANONICALLY (`ET.canonicalize`, `strip_text=True`)
    rather than byte for byte, because the pinned `docx-editor` re-serializes
    every part it reads: it rewrites the XML declaration's quoting and drops
    the pretty-printing whitespace in parts it never semantically touches
    (measured on `word/styles.xml`, `word/numbering.xml`, `word/theme/
    theme1.xml`, `docProps/*`, `customXml/*`). Byte equality would flag all
    of those as changed and make the proof useless. Canonical equality still
    catches every real difference: one element, attribute or character of
    text content added, removed or altered.

    Anything that does not parse as XML (images, fonts) falls back to byte
    equality, which is the right comparison for a binary part anyway.
    """
    try:
        return ET.canonicalize(xml_data=data.decode("utf-8"), strip_text=True)
    except Exception:  # noqa: BLE001 - not XML (or not decodable): compare bytes
        return data


def _settings_without_rsids(data: bytes) -> tuple[str, list[str]]:
    """`word/settings.xml` split into (everything else, the `<w:rsid>` values).

    Lifts every `<w:rsid>` child of a `<w:rsids>` out of the tree and returns
    the canonical form of what is left alongside the values it removed, so the
    two halves can be held to different rules: the rsid list may GROW, the
    remainder may not change at all. `<w:rsidRoot>` is a different tag and
    stays in the remainder, where altering it is a violation like any other.
    """
    root = ET.fromstring(data.decode("utf-8"))
    values: list[str] = []
    for rsids in root.iter(_RSIDS_TAG):
        for child in list(rsids):
            if child.tag != _RSID_TAG:
                continue
            values.append(child.get(_VAL_ATTR) or "")
            rsids.remove(child)
    remainder = ET.canonicalize(
        xml_data=ET.tostring(root, encoding="unicode"), strip_text=True
    )
    return remainder, values


def _verify_settings_rsids_only(
    source: bytes, output: bytes
) -> list[dict[str, Any]]:
    """Proof 3's narrower rule for `word/settings.xml` (see the allowlist
    comment): the ONLY difference permitted is one or more appended
    `<w:rsid>`; everything else in the part must be canonically identical.

    Both sides go through the same parse-and-canonicalize path, so the
    reformatting `docx-editor` does when it saves is not a difference -- only
    an element, attribute or character of content is.
    """
    try:
        source_rest, source_rsids = _settings_without_rsids(source)
        output_rest, output_rsids = _settings_without_rsids(output)
    except Exception as exc:  # noqa: BLE001 - an uncomparable part is fail-closed
        return [
            _failure(
                PROOF_PART_ALLOWLIST,
                f"package part {SETTINGS_PART!r} could not be compared: "
                f"{type(exc).__name__}: {exc}",
                part=SETTINGS_PART,
            )
        ]

    failures: list[dict[str, Any]] = []
    lost = Counter(source_rsids) - Counter(output_rsids)
    if lost:
        failures.append(
            _failure(
                PROOF_PART_ALLOWLIST,
                f"package part {SETTINGS_PART!r} lost revision-save id(s) "
                f"{sorted(lost.elements())!r}; a redline may only APPEND a "
                "<w:rsid>, never drop or rewrite one",
                part=SETTINGS_PART,
            )
        )
    if source_rest != output_rest:
        failures.append(
            _failure(
                PROOF_PART_ALLOWLIST,
                f"package part {SETTINGS_PART!r} changed outside <w:rsids>, and an "
                "appended <w:rsid> is the only change a redline may make to it -- "
                "this part also carries <w:documentProtection> and <w:trackChanges>",
                part=SETTINGS_PART,
            )
        )
    return failures


def _styles_split(data: Optional[bytes]) -> tuple[str, dict[str, str]]:
    """`word/styles.xml` split into (everything that is not a `<w:style>`,
    `styleId` -> that style's canonical form).

    `None` is a package with no styles part at all, which reads as an empty
    `<w:styles>` root and no styles -- so a CREATED part is held to exactly
    the same rule as an appended-to one rather than escaping the check.

    A style with no `w:styleId`, and a second style repeating one already
    seen, are keyed by POSITION rather than by id -- two entries that collided
    on one key would silently collapse into one and hide a difference from
    every comparison below.
    """
    root = ET.fromstring(data.decode("utf-8")) if data is not None else ET.Element(_w("styles"))
    styles: dict[str, str] = {}
    for index, style in enumerate(list(root.findall(_STYLE_TAG))):
        style_id = style.get(_STYLE_ID_ATTR)
        key = style_id if style_id is not None else f"<unnamed #{index}>"
        while key in styles:
            key = f"{key} #{index}"
        styles[key] = ET.canonicalize(
            xml_data=ET.tostring(style, encoding="unicode"), strip_text=True
        )
        root.remove(style)
    remainder = ET.canonicalize(
        xml_data=ET.tostring(root, encoding="unicode"), strip_text=True
    )
    return remainder, styles


def _canonical_footnote_styles() -> dict[str, str]:
    """`styleId` -> canonical form, for the two definitions
    `docx_parts.FOOTNOTE_STYLE_XML` holds. Parsed from that same
    mapping the writer emits from, so this proof cannot bless a definition
    the writer does not actually produce."""
    return {
        style_id: ET.canonicalize(xml_data=xml, strip_text=True)
        for style_id, xml in docx_parts.FOOTNOTE_STYLE_XML.items()
    }


def _verify_styles_footnote_additions_only(
    source: Optional[bytes], output: bytes
) -> list[dict[str, Any]]:
    """Proof 3's narrower rule for `word/styles.xml` (see the allowlist
    comment): the ONLY difference permitted is the APPEARANCE of the two
    footnote styles issue #647 requires, each canonically identical to the
    definition `docx_parts.FOOTNOTE_STYLE_XML` states.

    Everything else in the part -- `<w:docDefaults>`, `<w:latentStyles>`, and
    every style the document already defined, INCLUDING a `FootnoteReference`
    or `FootnoteText` of its own -- must come back canonically identical. A
    document that styles footnotes its own way keeps that styling; the writer
    only ever fills a gap, and this is what says so about the delivered bytes
    rather than about the writer's intent.
    """
    try:
        source_rest, source_styles = _styles_split(source)
        output_rest, output_styles = _styles_split(output)
    except Exception as exc:  # noqa: BLE001 - an uncomparable part is fail-closed
        return [
            _failure(
                PROOF_PART_ALLOWLIST,
                f"package part {STYLES_PART!r} could not be compared: "
                f"{type(exc).__name__}: {exc}",
                part=STYLES_PART,
            )
        ]

    failures: list[dict[str, Any]] = []
    if source_rest != output_rest:
        failures.append(
            _failure(
                PROOF_PART_ALLOWLIST,
                f"package part {STYLES_PART!r} changed outside its <w:style> "
                "definitions, and appending the two footnote styles is the only "
                "change a redline may make to it -- this part also carries "
                "<w:docDefaults> and <w:latentStyles>",
                part=STYLES_PART,
            )
        )

    for style_id in sorted(set(source_styles) - set(output_styles)):
        failures.append(
            _failure(
                PROOF_PART_ALLOWLIST,
                f"package part {STYLES_PART!r} dropped style {style_id!r}; a redline "
                "may only ADD a style definition, never remove one",
                part=STYLES_PART,
            )
        )
    for style_id in sorted(set(source_styles) & set(output_styles)):
        if source_styles[style_id] != output_styles[style_id]:
            failures.append(
                _failure(
                    PROOF_PART_ALLOWLIST,
                    f"package part {STYLES_PART!r} rewrote style {style_id!r}; the "
                    "document's own style definitions are left exactly as they were",
                    part=STYLES_PART,
                )
            )

    permitted = _canonical_footnote_styles()
    for style_id in sorted(set(output_styles) - set(source_styles)):
        if style_id not in permitted:
            failures.append(
                _failure(
                    PROOF_PART_ALLOWLIST,
                    f"package part {STYLES_PART!r} gained style {style_id!r}; the only "
                    f"styles a redline may add are {sorted(permitted)!r}",
                    part=STYLES_PART,
                )
            )
        elif output_styles[style_id] != permitted[style_id]:
            failures.append(
                _failure(
                    PROOF_PART_ALLOWLIST,
                    f"package part {STYLES_PART!r} gained a style {style_id!r} that is "
                    "not the definition docx_parts.FOOTNOTE_STYLE_XML states",
                    part=STYLES_PART,
                )
            )
    return failures


def _verify_part_allowlist(
    input_docx_bytes: bytes, output_docx_bytes: bytes
) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(io.BytesIO(input_docx_bytes)) as zf:
            source_parts = {name: zf.read(name) for name in zf.namelist()}
        with zipfile.ZipFile(io.BytesIO(output_docx_bytes)) as zf:
            output_parts = {name: zf.read(name) for name in zf.namelist()}
    except Exception as exc:  # noqa: BLE001 - any failure here is fail-closed
        return [
            _failure(
                PROOF_PART_ALLOWLIST,
                f"a package could not be read: {type(exc).__name__}: {exc}",
            )
        ]

    failures: list[dict[str, Any]] = []
    for name in sorted(set(output_parts) - set(source_parts)):
        if name == STYLES_PART:
            # Created for a package that carried none: held to the same
            # content rule as an appended-to one, against an empty source.
            failures.extend(
                _verify_styles_footnote_additions_only(None, output_parts[name])
            )
            continue
        if name not in ALLOWED_CHANGED_PARTS:
            failures.append(
                _failure(
                    PROOF_PART_ALLOWLIST,
                    f"the redline added package part {name!r}, which no redline may "
                    "write",
                    part=name,
                )
            )
    for name in sorted(set(source_parts) - set(output_parts)):
        if name not in ALLOWED_CHANGED_PARTS:
            failures.append(
                _failure(
                    PROOF_PART_ALLOWLIST,
                    f"the redline dropped package part {name!r}, which no redline may "
                    "remove",
                    part=name,
                )
            )
    for name in sorted(set(source_parts) & set(output_parts)):
        if name == SETTINGS_PART:
            failures.extend(
                _verify_settings_rsids_only(source_parts[name], output_parts[name])
            )
            continue
        if name == STYLES_PART:
            failures.extend(
                _verify_styles_footnote_additions_only(
                    source_parts[name], output_parts[name]
                )
            )
            continue
        if name in ALLOWED_CHANGED_PARTS:
            continue
        if source_parts[name] == output_parts[name]:
            continue
        if _canonical_part(source_parts[name]) == _canonical_part(output_parts[name]):
            continue
        failures.append(
            _failure(
                PROOF_PART_ALLOWLIST,
                f"package part {name!r} changed, and it is not a part a redline may "
                "touch",
                part=name,
            )
        )
    return failures


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def verify_projections(
    input_docx_bytes: bytes,
    output_docx_bytes: bytes,
    proven: dict[str, Any],
    *,
    revision_ids: Optional[Iterable[int]] = None,
    applied_edits: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Prove a compiled redline against its input, three ways.

    `input_docx_bytes` is the document `apply_block_transcript` was handed --
    the accept-all materialized bytes in the real pipeline (see the module
    docstring). `output_docx_bytes` is what it returned. `proven` is the
    `block_transcript.validate_block_patches()` success shape the compilation
    consumed.

    `revision_ids` scopes proof 1 to the revisions THIS compilation created
    (`apply_block_transcript`'s `revision_ids_by_issue`); `None` rejects every
    revision in the document, which is the stricter and correct reading when
    the input carries none of its own.

    `applied_edits` is the compiler's own per-edit record of what actually
    landed (each entry carrying `kind`, `block_id` and, for a span edit,
    `start`/`end`/`insert_text`); `None` means "every edit in the transcript
    applied", and rebuilds proof 2's expected text from the transcript's ops
    through the same issue #644 boundary rule the compiler writes with, so
    the two modes agree on a boundary-trimmed transcript.

    Returns `{"status": "verified"|"failed", "failures": [...]}`. Each failure
    names its `proof`. Never raises: a projection that cannot even be built is
    itself a failure, because a proof that could not be run has not passed.
    """
    failures: list[dict[str, Any]] = []
    failures.extend(_verify_part_allowlist(input_docx_bytes, output_docx_bytes))

    source_norm = extraction_normalization_stage.extract_and_normalize(input_docx_bytes)
    if source_norm.get("status") != "normalized":
        failures.append(
            _failure(
                PROOF_REJECT_ALL,
                "the source document does not normalize, so neither text projection "
                f"has anything to be proven against (status={source_norm.get('status')!r})",
            )
        )
        return {"status": "failed", "failures": failures}

    failures.extend(_verify_reject_all(source_norm, output_docx_bytes, revision_ids))
    failures.extend(
        _verify_accept_all(
            input_docx_bytes, source_norm, output_docx_bytes, proven, applied_edits
        )
    )

    return {
        "status": "failed" if failures else "verified",
        "failures": failures,
    }


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke note. The gate test (tests/test_redline_projections.py) is
    the authoritative check."""
    print(
        "redline_projections: run tests/test_redline_projections.py for the "
        "authoritative check (synthetic fixtures only)."
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
