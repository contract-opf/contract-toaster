#!/usr/bin/env python3
"""
Block-transcript compiler (issues #621, #622): a DETERMINISTIC compiler from
a PROVEN block transcript (`scripts/block_transcript.py`, issue #620) to
OOXML tracked changes -- sub-block SPAN edits (#621) and whole-block ops
(#622) alike.

This is the write half of Candidate E. `block_transcript.validate_block_
patches()` proves a model-authored transcript against the document's own
bytes and hands back, per block, an ordered list of `keep`/`delete`/`insert`
ops with TRUE character offsets into that block's real text. This module
turns those offsets into `<w:ins>`/`<w:del>` markup, and does the three
things the retired quote path (issue #379, deleted by issue #628) could
not:

1. **Multiple surgical edits inside one paragraph.** The quote path
   re-located each patch document-wide and refused anything that matched
   twice; two edits landing in one paragraph then produced two `<w:del>`
   siblings that its footnote pass could not tell apart. Here the edits are
   already addressed by offset, so a paragraph carrying three edits is
   ordinary: they are applied in DESCENDING offset order, which is what
   keeps every earlier offset valid as the paragraph mutates underneath.
2. **Pure insertions.** The quote path could only `replace()`, so inserting
   a clause meant deleting a span and re-inserting it with the addition --
   a spurious `<w:del>` a reviewer has to read past. A transcript's `insert`
   with no adjacent `delete` is compiled here as a `<w:ins>` alone (see
   `_apply_pure_insertion`).
3. **Footnotes keyed by issue, not by paragraph position.**
   `redline_generate._find_patched_paragraph` locates a footnote's home with
   `p.find(w:del)` -- the FIRST `<w:del>` in the paragraph -- which
   misattributes every rationale after the first whenever one paragraph
   carries two edits -- the quote patcher's own docstring documented that
   bug. Here each issue's footnote reference is placed inside an
   `<w:ins>` located BY REVISION ID, so attribution cannot drift no matter
   how many issues share a paragraph.

## The four passes, and why they are separate

**Pass 1 -- `docx_editor`.** Every REPLACEMENT (a `delete` span, with or
without an adjacent `insert`) goes through the pinned `docx-editor` package's
own `Document.replace()`/`Document.delete()`, addressed by
`paragraph=`/`occurrence=` exactly as the quote patcher did.
The occurrence index is COMPUTED from the proven offset -- the count of
matches of the source substring strictly before it -- rather than requiring
the substring to be document-unique, which is what makes "the transcript
edits the SECOND of two identical spans" expressible at all.

**Pass 2 -- owned XML.** Pure insertions are ours: `docx-editor` exposes only
anchor-text-addressed `insert_after`/`insert_before`, which would reintroduce
the very quote-uniqueness burden block addressing exists to remove (and
cannot express an insertion at offset 0 at all). `_apply_pure_insertion`
walks the paragraph's runs accumulating ACCEPTED-view text length, splits the
run containing the offset, and writes a `<w:ins>` holding a new run that
carries the split run's own `<w:rPr>` so the inserted text keeps the
surrounding formatting. This pass also rewrites `w:date` on exactly pass 1's
new revisions (the quote patcher did the same) --
`docx-editor` always stamps "now" and exposes no way to inject a
caller-supplied timestamp.

Pass 2 runs AFTER pass 1, so a pure insertion's offset must be translated
across whatever pass 1 changed in that paragraph. That translation is exact
and needs no re-matching: each replacement in the paragraph shifts every
later offset by `len(new_text) - len(source_text)` in the accepted view (a
tracked deletion leaves `<w:delText>`, which the accepted view excludes; a
tracked insertion adds `<w:t>`, which it includes). A pure insertion's offset
is always a segment BOUNDARY -- never inside a delete span -- so it can never
land in the middle of something pass 1 rewrote.

**Pass 3 -- whole-block ops (issue #622).** Also ours, and in the SAME
owned-XML tree pass 2 opened, because both write markup `docx-editor` has no
API for. It runs after pass 2 for one structural reason: every earlier pass
addresses paragraphs by their preorder `<w:p>` INDEX, and
`insert_block_after` adds a `<w:p>`, which renumbers every paragraph after
it. Inside the pass that renumbering is harmless, because every op's target
was resolved to an ELEMENT before the first insertion and is held by
reference from then on -- a new sibling `<w:p>` changes no reference. Several
insertions naming one anchor chain off the previous one rather than off the
anchor again, which is what keeps them in transcript order. See "Whole-block
ops" below.

**Pass 4 -- footnotes.** `inject_issue_footnotes` appends each issue's
`<w:footnoteReference>` run inside the first `<w:ins>` IN DOCUMENT ORDER
whose `w:id` this call recorded for that issue. Both runs carrying the
footnote's NUMBER name the `FootnoteReference` character style and the body
paragraph names `FootnoteText`, and `_footnote_styles_part` appends whichever
of those two definitions `word/styles.xml` is missing -- without them the
number renders as ordinary inline text (issue #647), which is what the owner
read in the first production redline.

## Footnote bodies are themselves tracked (issue #615)

Open issue #615 reports that the rationale footnote BODY is written as plain
runs, so reject-all strips the reference and leaves an orphaned note while
accept-all silently promotes machine-authored commentary to permanent
document text. This module does not reproduce that shape: the footnote body's
text run is wrapped in its own `<w:ins>` carrying `w:id`/`w:author`/`w:date`,
matching the reference run that points at it, so accept-all yields a footnote
that is unambiguously attributable and reject-all leaves no text behind.
(The empty `<w:footnote>` shell itself remains -- OOXML has no tracked-change
representation for "this footnote element should not exist" -- but it renders
nothing and holds no content.)

## Fail-closed, per edit

Nothing here guesses. Every condition that would require a guess is a
structured per-edit failure the caller routes into the analysis report, and
it never blocks the other edits in the batch:

- `unknown_block_id` / `block_text_changed`: the proven transcript does not
  describe THIS document's bytes. A transcript is only proof of the document
  it was proven against.
- `spans_physical_paragraph`: a `delete` span crosses a physical `<w:p>`
  boundary inside one logical block (blocks join sibling `<w:p>`s with
  `"\\n"`; `physical_spans` records where). `docx_editor` edits PHYSICAL
  paragraphs, so there is nowhere to write one tracked change across the
  join -- and this module never attempts a cross-paragraph write.
- `paragraph_not_resolved`: the block's physical paragraph could not be
  resolved to the `<w:p>` it was extracted from -- either the normalized
  paragraphs carry no identity (`physical_p_indexes`) or that `<w:p>`'s
  accepted-view text is no longer the clean text the transcript was proven
  against. Resolution is by carried-through identity, NEVER by scanning for
  a `<w:p>` whose text happens to match (see
  `_resolve_physical_paragraphs`).
- `edit_not_applied`: `docx-editor` itself refused the edit.
- `insert_anchor_unsplittable`: the run holding a pure insertion's offset is
  not a plain `<w:p>`-level run this module can safely split (e.g. it sits
  inside somebody else's pending `<w:ins>`, or carries markup beyond a single
  `<w:t>`).
- `block_op_unsupported_in_table`: a WHOLE-BLOCK op (`delete_block` /
  `insert_block_after`) whose block lives in a table cell -- v1 writes block
  ops for body and list paragraphs only. Span edits inside the cell are not
  affected.
- `round_trip_verification_failed`: the assembled package did not re-open
  cleanly, so no bytes are returned at all.
- `projection_verification_failed` (issue #623): the assembled package
  re-opened cleanly but failed one of the three PROJECTION PROOFS
  (`scripts/redline_projections.py`) -- see "The projection gate" below.
  BATCH-level, and the one failure here that does block every edit in the
  batch: the proofs are statements about the whole document.

## Whole-block ops (issue #622)

`proven["block_ops"]` -- the operations that name no sub-block span -- are
compiled here too, as a THIRD writer pass over the same owned XML tree, so
the pipeline can finally propose a new clause or strike an entire one
instead of degrading those issues to flag-only:

- **`delete_block`.** Every run of every physical `<w:p>` of the block is
  wrapped in a `<w:del>`, its `<w:t>` retagged `<w:delText>` (and
  `<w:instrText>` retagged `<w:delInstrText>`) -- `<w:t>` inside a `<w:del>`
  is the common, incorrect shortcut that renders wrong in Word's Reviewing
  pane (`_DELETED_TEXT_TAGS` below carries the rule and its rationale). The
  paragraph MARK is marked deleted too
  (`<w:pPr><w:rPr><w:del/></w:rPr></w:pPr>`), which is what makes the
  deletion a WHOLE-paragraph one rather than "a paragraph emptied of text":
  without it, accepting the change leaves the pilcrow and therefore a blank
  paragraph behind. (`scripts/extraction_normalization_stage.py`'s
  accept-all materializer documents the mirror-image limitation in its
  `_splice_accept_all` KNOWN LIMITATION note: it strips such a marker
  without applying its merge semantics.)
  A clause's HEADING is a separate `<w:p>` from its body, so a delete that
  covers only the block's own paragraphs leaves a numbered heading with no
  clause under it in the ACCEPTED document (issue #645, seen in the first
  live-model redline). The OWNER's ruling on that artifact (issue #646) is
  that the heading STAYS and the emptied clause reads
  `block_transcript.OMITTED_CLAUSE_PLACEHOLDER` -- `[Intentionally
  omitted.]` -- so the clean copy shows a deliberate striking rather than
  what looks like a drafting mistake:

      3. Indemnification
      [Intentionally omitted.]

  `_plan_omitted_clause_placeholders` owns the rule and its guards: the
  placeholder goes in only when the batch leaves NOTHING under that heading
  (issue #646 kept issue #645's condition verbatim and swapped its action),
  and it is written into the block's LAST struck `<w:p>` -- whose paragraph
  MARK is therefore left undeleted, or accept-all would merge that paragraph
  away and the placeholder would have nowhere to live. A block the rule
  declines simply loses its body, which is the pre-#645 behaviour and never
  a wrong edit.
  Two readers reconstruct a delete's outcome and both follow: the DERIVED
  `proposed_replacement_text`
  (`redline_generate.derived_replacement_text_by_issue`) is the placeholder
  for a whole-block strike that proposes no language of its own, and proof 2
  of the projection gate (`scripts/redline_projections.py`) expects the
  placeholder as that block's accepted text instead of the empty string.
  Proof 1 is unaffected: rejecting our revisions drops the `<w:ins>` and
  unwraps the `<w:del>`s, so the original clause comes back byte for byte.
- **`insert_block_after`.** A new `<w:p>` holding a single `<w:ins>`-wrapped
  run, placed after the anchor block's LAST physical `<w:p>`
  (`anchor_block_id == "start"` places it BEFORE the document's first
  block's first `<w:p>`). Its paragraph mark is marked INSERTED, the mirror
  of the delete case and for the same reason: a rejected insertion must not
  leave a stray pilcrow. The anchor's own `<w:pPr>` is inherited (minus
  `<w:rPr>`/`<w:sectPr>`/`<w:pPrChange>`) so a clause inserted into a
  numbered list is numbered like its neighbours, and the run carries the
  anchor's first `<w:rPr>` so it is formatted like them.

Both ops feed `revision_ids_by_issue`, so pass 4's per-`issue_key` footnote
anchoring covers them unchanged -- an inserted clause carries its issue's
rationale exactly as an in-place insertion does. (A paragraph-mark
`<w:ins>`/`<w:del>` lives inside `<w:pPr><w:rPr>`, which holds no run
content, so `inject_issue_footnotes` never anchors a reference on one.)

V1 scope is BODY and LIST paragraphs. A block op whose block lives in a
table cell is a structured `block_op_unsupported_in_table` failure naming
the op and the block: deleting a cell's only paragraph, or adding one, is a
table-structure edit (row/column semantics, `<w:cellDel>`/`<w:cellIns>`)
this module does not attempt. Within-cell SPAN edits are unaffected and
remain supported.

## Boundary whitespace (issue #644)

The one place this module touches model-authored text, and it is limited to
the ASCII space: when a kept span and an inserted span BOTH supply the space
at the boundary between them, accept-all reads a doubled space in the text a
lawyer sends out. `block_transcript.collapse_boundary_spaces` drops the
duplicate from the INSERT side only (the kept side is the document's own
bytes), leaving whitespace anywhere else -- interior doubles included --
exactly as authored. See that function for the full rule, what it
deliberately does not cover, and why it lives in `block_transcript` rather
than here: two other readers reconstruct the delivered text from the same
ops and have to apply the same rule to stay in agreement with it.

## The projection gate (issue #623)

`redline_generate.verify_docx_round_trip` proves the assembled package
OPENS. It says nothing about whether the tracked changes say what the
transcript said, so before any bytes are returned they must also pass
`redline_projections.verify_projections`: rejecting this call's revisions
reproduces the input document's block map exactly, accepting them reproduces
the final text of everything that actually applied, and no package part
changed outside the allowlist that module documents. A failure is
`projection_verification_failed` and NO document is delivered.

The proof is built from what ACTUALLY APPLIED, not from the transcript as
written -- this module fails closed per edit and never lets one edit's
failure block its siblings, so a batch that legitimately dropped one span
edit must still be provable.

## Out of scope (this issue)

Table row/column structural ops. Spine wiring, prompts and schema are
separate tickets too, and nothing here reads or writes S3, DynamoDB, or the
reconciled result shape.

Usage:
    from redline_block_apply import apply_block_transcript

    result = apply_block_transcript(
        docx_bytes,
        proven,                       # block_transcript.validate_block_patches(...)
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
        rationale_by_issue={"TERM-1": "Term shortened per position X."},
    )
    # result["docx_bytes"], result["applied"], result["failures"],
    # result["revision_ids_by_issue"]
    #
    # `proven["block_ops"]` (delete_block / insert_block_after) is compiled
    # by the same call -- each op reports one `applied`/`failures` entry
    # whose `kind` is the op name.
"""

from __future__ import annotations

import io
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_transcript  # noqa: E402
import docx_editor  # noqa: E402
import docx_parts  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import ooxml_util  # noqa: E402
import redline_generate  # noqa: E402
import redline_projections  # noqa: E402

WORD_NS = ooxml_util.WORD_NS
XML_NS = ooxml_util.XML_NS
DOCUMENT_PART = ooxml_util.DOCUMENT_PART
FOOTNOTES_PART = "word/footnotes.xml"
STYLES_PART = docx_parts.STYLES_PART
RELS_PART = "word/_rels/document.xml.rels"
CONTENT_TYPES_PART = "[Content_Types].xml"

_w = ooxml_util.w
_pkg = redline_generate._pkg
_ct = redline_generate._ct

# `w16du:dateUtc` -- the OOXML "extensible date" `docx-editor` stamps
# alongside `w:date` on every revision it creates. Only rewritten when
# already present, never added: this module never invents markup
# `docx-editor` itself did not write.
_W16DU_DATE_ATTR = "{http://schemas.microsoft.com/office/word/2023/wordml/word16du}dateUtc"

# Per-edit failure reasons. Every one is fail-closed -- none has a
# "best effort" branch (see module docstring, "Fail-closed, per edit").
REASON_UNKNOWN_BLOCK_ID = "unknown_block_id"
REASON_BLOCK_TEXT_CHANGED = "block_text_changed"
REASON_SPANS_PHYSICAL_PARAGRAPH = "spans_physical_paragraph"
REASON_PARAGRAPH_NOT_RESOLVED = "paragraph_not_resolved"
REASON_EDIT_NOT_APPLIED = "edit_not_applied"
REASON_INSERT_ANCHOR_UNSPLITTABLE = "insert_anchor_unsplittable"
REASON_DOCUMENT_NOT_NORMALIZABLE = "document_not_normalizable"
REASON_ROUND_TRIP_FAILED = "round_trip_verification_failed"
# Issue #622: a whole-block op whose block lives in a table cell. Deleting a
# cell's paragraph or adding one is a TABLE-STRUCTURE edit (row/column
# semantics, `<w:cellDel>`/`<w:cellIns>`), not a body-paragraph edit, and v1
# does not attempt it. Within-cell SPAN edits are untouched by this rule.
REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE = "block_op_unsupported_in_table"
# Issue #623: the assembled package failed one of the three PROJECTION
# PROOFS (`scripts/redline_projections.py`) -- rejecting this redline did not
# reproduce the source, accepting it did not reproduce the model-approved
# final text, or a package part changed that no redline may touch. Unlike
# every reason above it is BATCH-level, not per-edit: the proofs are
# statements about the whole document, so a caller cannot be told which one
# edit to drop, only that no document is deliverable.
REASON_PROJECTION_VERIFICATION_FAILED = "projection_verification_failed"

FAILURE_REASONS = (
    REASON_UNKNOWN_BLOCK_ID,
    REASON_BLOCK_TEXT_CHANGED,
    REASON_SPANS_PHYSICAL_PARAGRAPH,
    REASON_PARAGRAPH_NOT_RESOLVED,
    REASON_EDIT_NOT_APPLIED,
    REASON_INSERT_ANCHOR_UNSPLITTABLE,
    REASON_DOCUMENT_NOT_NORMALIZABLE,
    REASON_ROUND_TRIP_FAILED,
    REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE,
    REASON_PROJECTION_VERIFICATION_FAILED,
)

# The whole-block ops this module compiles, bound to the single definition
# in `block_transcript` (the module that owns the transcript vocabulary and
# normalizes an incoming op to exactly these names).
OP_DELETE_BLOCK = block_transcript.OP_DELETE_BLOCK
OP_INSERT_BLOCK_AFTER = block_transcript.OP_INSERT_BLOCK_AFTER


# ---------------------------------------------------------------------------
# Accepted-view text, computed the same way `docx_editor` computes it
# ---------------------------------------------------------------------------


def _accepted_text_runs(p: ET.Element) -> list[dict[str, Any]]:
    """Every `<w:t>` a reader SEES in paragraph `p`, in document order.

    Mirrors `docx_editor.xml_editor.build_text_map(view="accepted")` exactly:
    every `<w:t>` except those inside a `<w:del>` (deleted text lives in
    `<w:delText>` and is excluded from the accepted view), including text
    inside a pending `<w:ins>`. Matching that rule character for character is
    what lets an offset computed here be handed to `docx_editor` as an
    `occurrence=`, and what makes the run walk in `_apply_pure_insertion`
    agree with the offsets pass 1 used.

    Each entry is `{"t", "run", "container", "text"}`: the `<w:t>` element,
    its parent `<w:r>`, the run's own parent (the `<w:p>` itself for a plain
    run, or a `<w:ins>`/`<w:moveTo>` wrapper for one inside a pending
    revision), and the text.
    """
    out: list[dict[str, Any]] = []

    def walk(node: ET.Element) -> None:
        for child in list(node):
            if child.tag in (_w("del"), _w("moveFrom")):
                continue
            if child.tag == _w("r"):
                for t_el in child.iter(_w("t")):
                    out.append(
                        {"t": t_el, "run": child, "container": node, "text": t_el.text or ""}
                    )
                continue
            walk(child)

    walk(p)
    return out


def _accepted_text(p: ET.Element) -> str:
    return "".join(entry["text"] for entry in _accepted_text_runs(p))


def _body_paragraph_elements(root: ET.Element) -> list[ET.Element]:
    """Every `<w:p>` in the part, in the SAME document order
    `docx_editor` numbers its `P{n}#{hash}` refs by (minidom's
    `getElementsByTagName("w:p")`, i.e. preorder over the whole tree,
    table-cell and text-box paragraphs included). `ET.iter()` is preorder
    too, so index N here is `docx_editor`'s paragraph N+1."""
    return list(root.iter(_w("p")))


def _occurrence_before(haystack: str, needle: str, offset: int) -> int:
    """0-based occurrence index, WITHIN `haystack`, of the match of `needle`
    that starts at `offset` -- i.e. how many matches begin strictly before it.

    Counts with `docx_editor`'s own OVERLAPPING advance
    (`xml_editor.count_in_text_map` / `find_in_text_map` step the search
    start by ONE character after each hit, not by `len(needle)`), so the
    number returned is exactly the `occurrence=` value that addresses this
    match and not some other one. Counting non-overlapping matches here
    would silently target the wrong span for any self-overlapping source
    text (`"aa"` inside `"aaaa"`).
    """
    return sum(1 for start in range(0, offset) if haystack.startswith(needle, start))


# ---------------------------------------------------------------------------
# Turning proven ops into applicable edits
# ---------------------------------------------------------------------------


def _pair_ops_into_edits(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group one block's proven ops into the edits this module can write.

    A `delete` with an ADJACENT `insert` carrying the SAME `issue_key` (in
    either order -- a transcript may write the replacement text before or
    after the span it replaces) is ONE replacement: a single
    `<w:del>`/`<w:ins>` pair, which is how Word renders a substitution. A
    lone `delete` is a pure deletion; a lone `insert` is a pure insertion
    (and, unlike the quote path, gets no spurious `<w:del>` to go with it).

    `keep` ops are not edits and carry no author, so they are skipped.

    Insert texts arrive here through
    `block_transcript.collapse_boundary_spaces` (issue #644), so the writers
    and the `applied` report see the trimmed text. The other two readers that
    reconstruct what the document says apply the SAME function rather than
    reading the transcript raw -- the issue #623 accept-all proof
    (`redline_projections._expected_accept_all_texts`, in BOTH its
    `applied_edits` modes) and `redline_generate.
    derived_replacement_text_by_issue` -- so the document cannot disagree
    with the proof, and the field the pen rules judge is the language that
    shipped.
    """
    ops = block_transcript.collapse_boundary_spaces(ops)
    edits: list[dict[str, Any]] = []
    consumed: set[int] = set()

    for index, op in enumerate(ops):
        if index in consumed or op["op"] != "delete":
            continue
        partner_index: Optional[int] = None
        for candidate in (index + 1, index - 1):
            if candidate < 0 or candidate >= len(ops) or candidate in consumed:
                continue
            other = ops[candidate]
            if other["op"] == "insert" and other.get("issue_key") == op["issue_key"]:
                partner_index = candidate
                break
        consumed.add(index)
        insert_text = ""
        if partner_index is not None:
            consumed.add(partner_index)
            insert_text = ops[partner_index]["text"]
        edits.append(
            {
                "kind": "replace" if insert_text else "delete",
                "issue_key": op["issue_key"],
                "start": op["start"],
                "end": op["end"],
                "source_text": op["text"],
                "insert_text": insert_text,
            }
        )

    for index, op in enumerate(ops):
        if index in consumed or op["op"] != "insert":
            continue
        edits.append(
            {
                "kind": "insert",
                "issue_key": op["issue_key"],
                "start": op["at"],
                "end": op["at"],
                "source_text": "",
                "insert_text": op["text"],
            }
        )
    return edits


def _physical_span_for(spans: list, start: int, end: int) -> Optional[int]:
    """Index of the physical `<w:p>` span that wholly contains `[start, end]`,
    or `None` when the range crosses a join (or falls outside every span).

    `physical_spans` (issue #564) are `[start, end)` ranges into the block's
    joined text with a single `"\\n"` between consecutive spans, so a
    zero-width point at `end_k` belongs to span k (the end of that physical
    paragraph) and a point at `end_k + 1` belongs to span k+1 (its start).
    """
    for index, span in enumerate(spans):
        span_start, span_end = span[0], span[1]
        if span_start <= start and end <= span_end:
            return index
    return None


# ---------------------------------------------------------------------------
# Resolving a block's physical paragraphs onto the live document's <w:p>s
# ---------------------------------------------------------------------------


def _resolve_physical_paragraphs(
    root: ET.Element, normalized_paragraphs: list[dict[str, Any]]
) -> dict[tuple[str, int], dict[str, Any]]:
    """Map `(block_id, physical_span_index)` -> `{"index", "text", "lead"}`
    for the live `<w:p>` that span was extracted from.

    Resolution is by IDENTITY, never by text search: the extractor records
    each physical paragraph's own position in the part's preorder `w:p`
    numbering (`extraction_normalization_stage`, `physical_p_indexes`,
    parallel to `physical_spans`), and this function simply indexes the live
    `<w:p>` list with it. `index` is that position + 1, because `docx_editor`
    paragraph refs are 1-based.

    Why identity and not "the first `<w:p>` whose text matches" (issue #621
    fix round 1): the normalizer's clean text is `.strip()`ped and drops
    hidden text, so an ordinary paragraph carrying leading/trailing
    whitespace does not match itself -- and a forward scan then does not
    stop, it SLIDES onto a later, unrelated paragraph with the same text and
    writes the tracked change into the wrong clause while reporting success.
    Cross-clause contamination reported as `applied` is the worst outcome
    this module can produce, so nothing here is resolved by equal text.

    The text equality that remains is a GUARD, not a search: the identified
    `<w:p>`'s accepted-view text, compared on the SAME derivation the
    normalizer used (`.strip()`ed), must equal the span's clean text. If it
    does not -- hidden text, a field-code result, anything that makes the
    live accepted view differ from the text the transcript was proven
    against -- the offsets could not be trusted anyway, so the span is left
    OUT of the mapping and the caller turns that into a
    `paragraph_not_resolved` failure for whichever edits needed it, and only
    those.

    `text` is the live paragraph's accepted-view text UNSTRIPPED, and `lead`
    is how many characters `.strip()` would remove from its front -- block
    offsets are relative to the stripped text, `docx_editor`'s occurrence
    counting and `_apply_pure_insertion`'s run walk are both relative to the
    unstripped text, and `lead` is exactly the translation between them.
    """
    paragraphs = _body_paragraph_elements(root)

    resolved: dict[tuple[str, int], dict[str, Any]] = {}
    for record in normalized_paragraphs:
        block_id = record.get("block_id")
        if not block_id:
            continue
        block_text = record.get("text", "") or ""
        spans = record.get("physical_spans") or []
        p_indexes = record.get("physical_p_indexes") or []
        if len(p_indexes) != len(spans):
            # No carried-through identity (a caller hand-built the
            # paragraphs). Fail closed for the whole block rather than fall
            # back to a text scan.
            continue
        for span_index, (span, p_index) in enumerate(zip(spans, p_indexes)):
            if not isinstance(p_index, int) or isinstance(p_index, bool):
                continue
            if not 0 <= p_index < len(paragraphs):
                continue
            live_text = _accepted_text(paragraphs[p_index])
            if live_text.strip() != block_text[span[0] : span[1]]:
                continue
            resolved[(block_id, span_index)] = {
                "index": p_index + 1,  # docx_editor refs are 1-based
                "text": live_text,
                "lead": len(live_text) - len(live_text.lstrip()),
            }
    return resolved


def _plan_omitted_clause_placeholders(
    root: ET.Element,
    normalized_paragraphs: list[dict[str, Any]],
    deleted_block_ids: set,
    anchored_block_ids: set,
) -> set:
    """The `block_id`s whose `delete_block` must leave
    `block_transcript.OMITTED_CLAUSE_PLACEHOLDER` behind, because this batch
    empties the clause under a heading completely (issues #645, #646).

    ## The rule, and why it is structural rather than a judgment call

    A clause's heading is a SEPARATE `<w:p>` from its body
    (`extraction_normalization_stage.extract_document_paragraphs` lifts the
    boundary paragraph into `heading` and appends only the rest to
    `physical_paragraphs`), so a `delete_block` -- which strikes the block's
    own physical paragraphs -- leaves the heading standing. In the REDLINE
    view that reads fine; in the ACCEPTED document it leaves a numbered
    heading with no clause under it, which is not a document an attorney
    would send.

    Issue #645 answered that by deleting the heading too. The OWNER reversed
    it (issue #646) after reading the first production redline: the heading
    stays, and the emptied clause reads `[Intentionally omitted.]`, so the
    clean copy says the striking was deliberate. The CONDITION is unchanged
    -- it was already exactly right -- and only the action moved, which is
    why this function still asks the #645 question.

    A placeholder goes in only when the batch leaves NOTHING under that
    heading: every block it heads is being deleted (`deleted_block_ids`) and
    none of them is the anchor of an `insert_block_after`
    (`anchored_block_ids`), whose new `<w:p>` lands under that same heading.
    That is a fact about the document's structure, not a reading of what the
    model meant: a heading with a surviving body needs no placeholder,
    always.

    The anchored half is not hypothetical -- it is how a clause is REPLACED
    in place. `scripts/third_party_output_integration.py` emits
    `delete_block` + `insert_block_after` on the same block for one
    `issue_key`, and the governed replacement text belongs under the struck
    clause's own heading; a placeholder there would announce an omission
    directly above the replacement clause.

    Today `extract_document_paragraphs` starts exactly one logical block at
    each boundary paragraph, so the set of blocks under one heading has a
    single member -- but the gate is written over the set the paragraph list
    actually describes, so a grouping change can never silently turn this
    into "placeholder whenever any block under a heading goes". For the same
    reason a heading is claimed by AT MOST ONE block: two `delete_block`s
    under one heading yield ONE placeholder, not two.

    Nothing is guessed and nothing is searched for. The heading is read by
    the SAME carried identity a body paragraph is (`heading_p_index`, the
    boundary `<w:p>`'s own position in the part's preorder `w:p` numbering),
    and the same text-equality GUARD applies: the live paragraph's
    accepted-view text must still be the text that heading was extracted
    from. A block with no heading paragraph at all (`heading_p_index is
    None` -- the document's preamble, or a hand-built caller), an index that
    no longer exists, or a paragraph whose text has moved is simply left OUT
    of the returned set: the body deletion still lands whole, with no
    placeholder, which is the pre-#645 behaviour and never a wrong edit.
    """
    paragraphs = _body_paragraph_elements(root)

    blocks_by_heading: dict[int, list[str]] = {}
    heading_of_block: dict[str, tuple[int, str]] = {}
    for record in normalized_paragraphs:
        block_id = record.get("block_id")
        heading_p_index = record.get("heading_p_index")
        if not block_id:
            continue
        if not isinstance(heading_p_index, int) or isinstance(heading_p_index, bool):
            continue
        blocks_by_heading.setdefault(heading_p_index, []).append(block_id)
        heading_of_block[block_id] = (
            heading_p_index,
            record.get("heading_source_text", "") or "",
        )

    placeholders: set = set()
    claimed: set = set()
    for record in normalized_paragraphs:
        block_id = record.get("block_id")
        if block_id not in deleted_block_ids or block_id not in heading_of_block:
            continue
        heading_p_index, source_text = heading_of_block[block_id]
        if heading_p_index in claimed:
            continue
        siblings = blocks_by_heading.get(heading_p_index, [])
        if any(
            sibling not in deleted_block_ids or sibling in anchored_block_ids
            for sibling in siblings
        ):
            # A body under this heading survives the batch -- either it was
            # never struck, or a replacement clause is being inserted under
            # it. The heading is still doing its job, so nothing is omitted.
            continue
        if not 0 <= heading_p_index < len(paragraphs):  # pragma: no cover - defensive
            continue
        if _accepted_text(paragraphs[heading_p_index]).strip() != source_text.strip():
            # The live paragraph is not the heading this record was extracted
            # from any more, so this record is not evidence about what the
            # accepted document will show under it. Leave the clause to be
            # struck the plain way rather than announce an omission on a
            # guess.
            continue
        claimed.add(heading_p_index)
        placeholders.add(block_id)
    return placeholders


# ---------------------------------------------------------------------------
# Pass 2: owned pure-insertion writer
# ---------------------------------------------------------------------------


def _copy_rpr(run: Optional[ET.Element]) -> Optional[ET.Element]:
    """A deep copy of `run`'s `<w:rPr>`, or None when it has none -- so the
    inserted run continues the surrounding formatting instead of resetting
    to the paragraph default."""
    if run is None:
        return None
    rpr = run.find(_w("rPr"))
    if rpr is None:
        return None
    return _deep_copy(rpr)


def _deep_copy(el: ET.Element) -> ET.Element:
    clone = ET.Element(el.tag, dict(el.attrib))
    clone.text = el.text
    clone.tail = el.tail
    for child in el:
        clone.append(_deep_copy(child))
    return clone


def _make_text_run(rpr: Optional[ET.Element], text: str) -> ET.Element:
    run = ET.Element(_w("r"))
    if rpr is not None:
        run.append(_deep_copy(rpr))
    t_el = ET.SubElement(run, _w("t"))
    t_el.set(f"{{{XML_NS}}}space", "preserve")
    t_el.text = text
    return run


def _clone_run_with_text(run: ET.Element, text: str) -> ET.Element:
    """A copy of `run` -- its `<w:rPr>` AND its own attributes (`w:rsidR`
    and friends) intact -- carrying `text` instead of its original.

    Splitting a run must not quietly strip the revision-save ids Word keeps
    on it; the two halves are the SAME authored run, so everything about
    them except the text stays byte-identical."""
    clone = _deep_copy(run)
    clone_t = clone.find(_w("t"))
    if clone_t is None:  # pragma: no cover - guarded by _is_simple_text_run
        raise ValueError("redline_block_apply: cannot clone a run with no <w:t>")
    clone_t.set(f"{{{XML_NS}}}space", "preserve")
    clone_t.text = text
    return clone


def _is_simple_text_run(run: ET.Element) -> bool:
    """True when `run` is a plain `<w:r>` carrying at most an `<w:rPr>` and
    exactly one `<w:t>` -- the only shape this module will SPLIT. A run
    holding breaks, tabs, drawings or several `<w:t>` children is left
    alone (fail closed) rather than reassembled by guesswork."""
    children = list(run)
    text_children = [c for c in children if c.tag == _w("t")]
    other = [c for c in children if c.tag not in (_w("t"), _w("rPr"))]
    return len(text_children) == 1 and not other


def _apply_pure_insertion(
    p: ET.Element, offset: int, text: str, revision_id: int, author: str, timestamp_iso: str
) -> Optional[str]:
    """Write `text` into paragraph `p` at ACCEPTED-view character `offset` as
    a `<w:ins>` -- no `<w:del>`, no anchor text, no re-matching.

    Walks the paragraph's runs accumulating accepted-view text length to find
    the run that owns the offset. When the offset falls strictly INSIDE a
    run, that run is split and the `<w:ins>` goes between the halves, with
    the run's own `<w:rPr>` copied onto both halves and onto the inserted run
    so formatting continues across the insertion. When it falls on a run
    BOUNDARY nothing needs splitting: the `<w:ins>` is placed after the run
    on its left (whose formatting the inserted text then continues), or
    before the run on its right if the left neighbour is not a plain
    paragraph-level run.

    Returns `None` on success, or a failure reason string. Fails closed
    rather than writing into a shape it cannot reason about: an offset owned
    only by a run inside somebody else's pending revision, or by a run
    carrying markup beyond a single `<w:t>`, is refused
    (`insert_anchor_unsplittable`) instead of being rebuilt by guesswork.
    """
    entries = [entry for entry in _accepted_text_runs(p) if entry["text"]]

    if not entries:
        if offset != 0:
            return REASON_INSERT_ANCHOR_UNSPLITTABLE
        p.append(_build_ins(revision_id, author, timestamp_iso, None, text))
        return None

    inside: Optional[tuple] = None
    left: Optional[dict[str, Any]] = None
    right: Optional[dict[str, Any]] = None
    cumulative = 0
    for entry in entries:
        length = len(entry["text"])
        if cumulative < offset < cumulative + length:
            inside = (entry, offset - cumulative)
            break
        if offset == cumulative and right is None:
            right = entry
        if offset == cumulative + length:
            left = entry
        cumulative += length

    if inside is not None:
        entry, offset_in_run = inside
        run = entry["run"]
        if entry["container"] is not p:
            # The offset lands inside a pending revision somebody else
            # authored. Splicing our own insertion into it would merge two
            # authors' work into one revision.
            return REASON_INSERT_ANCHOR_UNSPLITTABLE
        if not _is_simple_text_run(run):
            return REASON_INSERT_ANCHOR_UNSPLITTABLE
        rpr = _copy_rpr(run)
        run_index = list(p).index(run)
        p.remove(run)
        p.insert(run_index, _clone_run_with_text(run, entry["text"][:offset_in_run]))
        p.insert(run_index + 1, _build_ins(revision_id, author, timestamp_iso, rpr, text))
        p.insert(run_index + 2, _clone_run_with_text(run, entry["text"][offset_in_run:]))
        return None

    for entry, after in ((left, True), (right, False)):
        if entry is None or entry["container"] is not p:
            continue
        run = entry["run"]
        run_index = list(p).index(run)
        ins = _build_ins(revision_id, author, timestamp_iso, _copy_rpr(run), text)
        p.insert(run_index + 1 if after else run_index, ins)
        return None

    return REASON_INSERT_ANCHOR_UNSPLITTABLE


def _stamp_revision(el: ET.Element, revision_id: int, author: str, timestamp_iso: str) -> None:
    """`w:id`/`w:author`/`w:date` on one `<w:ins>`/`<w:del>`, per the OOXML
    tracked-changes schema -- the three attributes every revision this
    module creates carries."""
    el.set(_w("id"), str(revision_id))
    el.set(_w("author"), author)
    el.set(_w("date"), timestamp_iso)


def _build_ins(
    revision_id: int,
    author: str,
    timestamp_iso: str,
    rpr: Optional[ET.Element],
    text: str,
) -> ET.Element:
    ins = ET.Element(_w("ins"))
    _stamp_revision(ins, revision_id, author, timestamp_iso)
    ins.append(_make_text_run(rpr, text))
    return ins


# ---------------------------------------------------------------------------
# Pass 3: whole-block ops (issue #622)
# ---------------------------------------------------------------------------

# `<w:pPr>` (CT_PPr) ends with `<w:rPr>`, then `<w:sectPr>`, then
# `<w:pPrChange>`. A paragraph-mark revision lives in that `<w:rPr>`, so a
# newly created one is inserted BEFORE these two and appended otherwise --
# element order in `<w:pPr>` is a schema sequence, not a set.
_PPR_TAIL_TAGS = (_w("sectPr"), _w("pPrChange"))

# The only children a whole-paragraph deletion does NOT descend into:
# `<w:del>`/`<w:moveFrom>` already hold deleted text (re-wrapping would nest
# one author's deletion inside another's) and `<w:pPr>` holds properties, not
# run content. Everything ELSE is recursed into -- deliberately a skip list
# and not an allowlist of container tags, because it has to agree with
# `_accepted_text_runs`, whose walk skips exactly these three. An allowlist
# that missed one container (`<w:fldSimple>`, `<w:hyperlink>`,
# `<w:sdtContent>`, a pending `<w:ins>`) would leave that run's text
# undeleted in a paragraph whose MARK had been deleted, and report the op
# applied -- text surviving a "deleted" clause is the worst outcome here.
_UNDELETED_CONTAINERS = (_w("del"), _w("moveFrom"), _w("pPr"))

# `<w:t>` inside a `<w:del>` is the common, incorrect shortcut that renders
# wrong in Word's Reviewing pane; deleted text is `<w:delText>` and a deleted
# field instruction is `<w:delInstrText>`. Word silently renders a `<w:t>`
# inside a `<w:del>` as ordinary body text, so the reviewer sees the deleted
# words as if they were still part of the clause.
_DELETED_TEXT_TAGS = {_w("t"): _w("delText"), _w("instrText"): _w("delInstrText")}


def _parent_map(root: ET.Element) -> dict[int, ET.Element]:
    """`id(child) -> parent` over the whole tree. ElementTree elements carry
    no parent pointer, and a new `<w:p>` has to be inserted into the SAME
    parent its anchor lives in (a `<w:body>`, or somebody's `<w:txbxContent>`)
    rather than assumed to be a body-level sibling."""
    parents: dict[int, ET.Element] = {}
    for element in root.iter():
        for child in element:
            parents[id(child)] = element
    return parents


def _table_cell_paragraph_indexes(root: ET.Element) -> set[int]:
    """The 0-based preorder `<w:p>` positions that live inside a `<w:tc>`.

    Indexed the same way `_body_paragraph_elements` numbers paragraphs, so a
    resolved location's `index - 1` looks up directly. Ancestry is walked
    explicitly (`_parent_map`) rather than inferred from the extractor's
    grouping: a logical block can mix body and table-cell physical
    paragraphs, and only the ANCESTRY of the actual `<w:p>` says which is
    which.
    """
    parents = _parent_map(root)
    in_table: set[int] = set()
    for index, paragraph in enumerate(_body_paragraph_elements(root)):
        node = parents.get(id(paragraph))
        while node is not None:
            if node.tag == _w("tc"):
                in_table.add(index)
                break
            node = parents.get(id(node))
    return in_table


def _paragraph_properties(p: ET.Element) -> ET.Element:
    """`p`'s `<w:pPr>`, created as its FIRST child when absent (the schema
    requires it there)."""
    pPr = p.find(_w("pPr"))
    if pPr is None:
        pPr = ET.Element(_w("pPr"))
        p.insert(0, pPr)
    return pPr


def _mark_paragraph_mark(
    p: ET.Element, tag: str, revision_id: int, author: str, timestamp_iso: str
) -> None:
    """Record `p`'s paragraph MARK (the pilcrow) as inserted or deleted.

    `tag` is `"ins"` or `"del"`. This is the difference between a whole
    paragraph op and "a paragraph emptied of text": with no marker, accepting
    a `delete_block` leaves the break -- and therefore a blank paragraph --
    behind, and rejecting an `insert_block_after` leaves a stray one.

    `<w:rPr>` sits at the END of `<w:pPr>`'s schema sequence and the revision
    marker at the START of `<w:rPr>`'s (CT_ParaRPr orders `ins`, `del`,
    `moveFrom`, `moveTo` before every formatting child), so both are placed,
    not appended blindly. An existing marker is re-stamped rather than
    duplicated.
    """
    pPr = _paragraph_properties(p)
    rpr = pPr.find(_w("rPr"))
    if rpr is None:
        rpr = ET.Element(_w("rPr"))
        position = len(list(pPr))
        for index, child in enumerate(list(pPr)):
            if child.tag in _PPR_TAIL_TAGS:
                position = index
                break
        pPr.insert(position, rpr)
    marker = rpr.find(_w(tag))
    if marker is None:
        marker = ET.Element(_w(tag))
        # `<w:del>` follows `<w:ins>` when a mark is both (inserted by one
        # author, then deleted by another); otherwise it leads.
        rpr.insert(1 if tag == "del" and rpr.find(_w("ins")) is not None else 0, marker)
    _stamp_revision(marker, revision_id, author, timestamp_iso)


def _retag_run_as_deleted(run: ET.Element) -> None:
    """Retag `run`'s text nodes for life inside a `<w:del>`."""
    for element in run.iter():
        replacement = _DELETED_TEXT_TAGS.get(element.tag)
        if replacement is not None:
            element.tag = replacement


def _delete_runs_in(
    container: ET.Element, allocate, author: str, timestamp_iso: str
) -> list[int]:
    """Wrap every run under `container` in a `<w:del>`, deepest wrappers
    included, and return the revision ids created.

    Adjacent runs share ONE `<w:del>` -- that is how Word writes a deleted
    span, and it keeps the revision count proportional to the deletion rather
    than to the paragraph's run splits. A run already inside a `<w:del>` or a
    `<w:moveFrom>` is left alone: it is deleted text already, and re-wrapping
    it would nest one author's deletion inside another's. A run inside a
    pending `<w:ins>` IS deleted, nested as `<w:ins><w:del>` -- Word's own
    representation of "inserted, then deleted before either was accepted".

    The recursion is over everything except `_UNDELETED_CONTAINERS`, which
    is what makes "every run the accepted view SHOWS is deleted" true by
    construction rather than by an allowlist that can fall behind.
    """
    revision_ids: list[int] = []
    index = 0
    while index < len(container):
        child = container[index]
        if child.tag == _w("r"):
            group_end = index
            while group_end < len(container) and container[group_end].tag == _w("r"):
                group_end += 1
            runs = list(container)[index:group_end]
            del_el = ET.Element(_w("del"))
            revision_id = allocate()
            _stamp_revision(del_el, revision_id, author, timestamp_iso)
            revision_ids.append(revision_id)
            for run in runs:
                container.remove(run)
                _retag_run_as_deleted(run)
                del_el.append(run)
            container.insert(index, del_el)
            index += 1
            continue
        if child.tag not in _UNDELETED_CONTAINERS:
            revision_ids.extend(_delete_runs_in(child, allocate, author, timestamp_iso))
        index += 1
    return revision_ids


def _delete_paragraph(
    p: ET.Element,
    allocate,
    author: str,
    timestamp_iso: str,
    placeholder: Optional[str] = None,
) -> list[int]:
    """One physical `<w:p>` of a `delete_block`: every run tracked-deleted,
    then the paragraph mark itself.

    `placeholder` (issue #646) makes this the paragraph that CARRIES
    `block_transcript.OMITTED_CLAUSE_PLACEHOLDER` -- the last struck `<w:p>`
    of a clause whose heading would otherwise be left with nothing under it.
    Two things change, and they stand or fall together:

    - The paragraph MARK is left alone. Deleting it is what makes an ordinary
      whole-paragraph deletion whole (see `_mark_paragraph_mark`), but
      accept-all would then merge this paragraph into its successor and the
      placeholder would have nowhere to live -- it would land inside the NEXT
      clause. A paragraph that survives on purpose keeps its pilcrow.
    - An `<w:ins>` run carrying `placeholder` is appended AFTER the
      `<w:del>`s, so the redline reads "struck text, then the replacement"
      exactly as an in-place substitution does, and rejecting the change
      drops the `<w:ins>` and unwraps the `<w:del>`s back to the original
      paragraph.

    The run inherits the `<w:rPr>` of the paragraph's first run as it was
    BEFORE the deletion, so the placeholder is formatted like the clause it
    replaces rather than like the document's defaults.
    """
    rpr = None
    if placeholder is not None:
        accepted_runs = _accepted_text_runs(p)
        rpr = _copy_rpr(accepted_runs[0]["run"]) if accepted_runs else None

    revision_ids = _delete_runs_in(p, allocate, author, timestamp_iso)
    if placeholder is None:
        mark_id = allocate()
        _mark_paragraph_mark(p, "del", mark_id, author, timestamp_iso)
        revision_ids.append(mark_id)
        return revision_ids

    insert_id = allocate()
    p.append(_build_ins(insert_id, author, timestamp_iso, rpr, placeholder))
    revision_ids.append(insert_id)
    return revision_ids


def _inherited_paragraph_properties(anchor: ET.Element) -> ET.Element:
    """A `<w:pPr>` for a NEW paragraph, copied from `anchor`'s.

    Style and numbering are inherited so a clause inserted into a numbered
    list is numbered like its neighbours (v1 scope is body AND list
    paragraphs). Three children are deliberately dropped: `<w:rPr>` (the
    anchor's own paragraph-mark properties, replaced by this paragraph's own
    insertion marker), `<w:sectPr>` (a section break belongs to exactly one
    paragraph -- copying it would duplicate the break) and `<w:pPrChange>`
    (somebody else's pending formatting revision, which this new paragraph
    never had).
    """
    source = anchor.find(_w("pPr"))
    if source is None:
        return ET.Element(_w("pPr"))
    pPr = _deep_copy(source)
    for child in list(pPr):
        if child.tag in (_w("rPr"),) + _PPR_TAIL_TAGS:
            pPr.remove(child)
    return pPr


def _build_inserted_paragraph(
    anchor: ET.Element,
    text: str,
    body_revision_id: int,
    mark_revision_id: int,
    author: str,
    timestamp_iso: str,
) -> ET.Element:
    """A new `<w:p>` holding `text` as a single tracked insertion, formatted
    like `anchor` and with its own paragraph mark marked inserted."""
    new_p = ET.Element(_w("p"))
    new_p.append(_inherited_paragraph_properties(anchor))
    _mark_paragraph_mark(new_p, "ins", mark_revision_id, author, timestamp_iso)
    anchor_runs = _accepted_text_runs(anchor)
    rpr = _copy_rpr(anchor_runs[0]["run"]) if anchor_runs else None
    new_p.append(_build_ins(body_revision_id, author, timestamp_iso, rpr, text))
    return new_p


# ---------------------------------------------------------------------------
# Package rewriting
# ---------------------------------------------------------------------------


def _read_package(docx_bytes: bytes) -> tuple[list, dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}
    return infos, originals


def _write_package(infos: list, originals: dict[str, bytes], new_parts: dict[str, bytes]) -> bytes:
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        written = set()
        for info in infos:
            zf_out.writestr(info, new_parts.get(info.filename, originals[info.filename]))
            written.add(info.filename)
        for name, data in new_parts.items():
            if name not in written:
                zf_out.writestr(name, data)
    return out_buf.getvalue()


def _serialize_document(root: ET.Element, original_root_open_tag: str) -> bytes:
    """Serialize a mutated `word/document.xml` tree, splicing the ORIGINAL
    root start tag back in verbatim and merging any namespace the serializer
    hoisted -- the shared `scripts/ooxml_util.py` dance (issue #621), not a
    fourth copy of it.

    Nothing here is specific to `word/document.xml`; issue #647's
    `_footnote_styles_part` reuses it for an existing `word/styles.xml`,
    whose root start tag carries just as many namespace declarations that
    ElementTree would otherwise drop.
    """
    serialized = ET.tostring(root, encoding="unicode")
    auto_root_open_tag = ooxml_util.root_open_tag(serialized)
    body_and_close = serialized[len(auto_root_open_tag) :]
    root_open_tag = ooxml_util.merge_hoisted_namespaces(original_root_open_tag, auto_root_open_tag)
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + root_open_tag.encode("utf-8")
        + body_and_close.encode("utf-8")
    )


# ---------------------------------------------------------------------------
# Pass 3: footnotes keyed by issue
# ---------------------------------------------------------------------------


def _footnote_style_rpr() -> ET.Element:
    """`<w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>` -- the run
    properties Word puts on BOTH runs that carry a footnote's number (the
    in-body `<w:footnoteReference>` and the `<w:footnoteRef/>` that opens the
    note itself). Without it the number renders as ordinary inline text
    (issue #647)."""
    rpr = ET.Element(_w("rPr"))
    rstyle = ET.SubElement(rpr, _w("rStyle"))
    rstyle.set(_w("val"), docx_parts.FOOTNOTE_REFERENCE_STYLE_ID)
    return rpr


def _footnote_styles_part(styles_xml: Optional[bytes]) -> tuple[Optional[bytes], bool]:
    """`(new "word/styles.xml" bytes, part_was_created)` for a package whose
    footnotes must be able to RESOLVE `FootnoteReference` / `FootnoteText`,
    or `(None, False)` when the package's own part already defines both.

    A style the document ALREADY defines is left completely alone (issue
    #647 scope item 3): a counterparty may style footnotes their own way,
    and rewriting their definition would be an unrequested formatting edit
    to a part a redline otherwise never touches. Only a MISSING definition
    is appended, and only the two ids `docx_parts.FOOTNOTE_STYLE_XML`
    names -- which is also exactly what `redline_projections`' proof 3
    permits this part to gain, checked against that same mapping rather than
    against a second copy of it here.

    `styles_xml is None` means the uploaded package carries no styles part at
    all. That is reachable: a `.docx` from a non-Word producer can omit it,
    and pass 1's `docx-editor` save neither adds one nor fails on its
    absence. The part is then created carrying only these two styles, and the
    caller adds the relationship and content-type override for it.
    """
    created = styles_xml is None
    original_open_tag: Optional[str] = None
    if created:
        root = ET.Element(_w("styles"))
    else:
        styles_text = styles_xml.decode("utf-8")
        original_open_tag = ooxml_util.root_open_tag(styles_text)
        ooxml_util.register_declared_namespaces(
            ooxml_util.declared_namespaces_anywhere(styles_text)
        )
        root = ET.fromstring(styles_xml)

    already_defined = {style.get(_w("styleId")) for style in root.findall(_w("style"))}
    missing = [
        style
        for style in docx_parts.footnote_style_elements()
        if style.get(_w("styleId")) not in already_defined
    ]
    if not missing:
        return None, False
    root.extend(missing)

    if original_open_tag is not None:
        return _serialize_document(root, original_open_tag), created
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(root, encoding="unicode").encode("utf-8")
    ), created


def inject_issue_footnotes(
    docx_bytes: bytes,
    footnote_specs: list[dict[str, Any]],
    *,
    author: str,
    timestamp_iso: str,
) -> bytes:
    """Attach one footnoted rationale per ISSUE, anchored BY REVISION ID.

    `footnote_specs` is an ordered list of `{"issue_key", "revision_ids",
    "text"}`. For each spec the `<w:footnoteReference>` run is appended
    inside the FIRST `<w:ins>` IN DOCUMENT ORDER whose `w:id` appears in
    that issue's `revision_ids` -- document order, not the order
    `revision_ids` records them in, which for several edits in one paragraph
    is descending-offset order. That is the whole point of this function:
    `redline_generate._find_patched_paragraph` anchors on the first
    `<w:del>` in a paragraph, so two issues editing one paragraph both
    footnote the first one's deletion -- the failure the retired quote
    patcher's own docstring recorded. A revision id is unique document-wide,
    so it cannot
    misattribute.

    The footnote BODY is written inside its own `<w:ins>` (issue #615): the
    reference is tracked, so the body must be too, or reject-all leaves an
    orphaned note and accept-all promotes machine-authored commentary to
    permanent document text with nothing marking it as tool-generated.

    Both runs that carry the footnote's NUMBER (the in-body
    `<w:footnoteReference>` and the `<w:footnoteRef/>` opening the note) get
    `<w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>`, and the footnote
    body paragraph gets `<w:pStyle w:val="FootnoteText"/>` -- Word's own
    markup, superscript carried by the STYLE rather than by direct run
    formatting. `_footnote_styles_part` then makes sure `word/styles.xml`
    actually DEFINES those two ids, because a document that has never
    carried a footnote does not, and an unresolvable `<w:rStyle>` renders as
    plain inline text (issue #647). A definition the document already
    carries is left exactly as it is.

    `[Content_Types].xml` and `word/_rels/document.xml.rels` are only ever
    APPENDED to, never replaced, and new footnote ids are offset past
    whatever an already-footnoted upload carries -- the same id-offset
    machinery `redline_generate.inject_export_marker_and_footnotes` uses
    (`_max_rel_id` / `_max_footnote_id`, reused here rather than copied).
    """
    specs = [spec for spec in footnote_specs if spec.get("text") and spec.get("revision_ids")]
    if not specs:
        return docx_bytes

    infos, originals = _read_package(docx_bytes)
    names = set(originals)

    doc_xml_text = originals[DOCUMENT_PART].decode("utf-8")
    original_root_open_tag = ooxml_util.root_open_tag(doc_xml_text)
    ooxml_util.register_declared_namespaces(ooxml_util.declared_namespaces_anywhere(doc_xml_text))
    doc_root = ET.fromstring(originals[DOCUMENT_PART])

    have_footnotes = FOOTNOTES_PART in names
    if have_footnotes:
        footnotes_root = ET.fromstring(originals[FOOTNOTES_PART])
        next_footnote_id = redline_generate._max_footnote_id(footnotes_root) + 1
    else:
        footnotes_root = None
        next_footnote_id = 1

    # One sweep for every <w:ins> in the document, keyed by revision id, so
    # N issues cost one walk rather than N. `position` records DOCUMENT
    # ORDER, because that -- not the order the caller happened to record the
    # ids in -- is what picks an issue's anchor below.
    # Paragraph-MARK insertions (issue #622) are excluded: a
    # `<w:pPr><w:rPr><w:ins/></w:rPr></w:pPr>` records that a pilcrow was
    # inserted and holds no run content, so appending a
    # `<w:footnoteReference>` run to it would write a run into `<w:rPr>`.
    # They ARE recorded in `revision_ids_by_issue` (a caller accepting one
    # issue's whole contribution needs them), which is exactly why the
    # exclusion belongs here and not in the caller's id bookkeeping.
    paragraph_mark_revisions = {
        id(el) for rpr in doc_root.iter(_w("rPr")) for el in rpr.iter(_w("ins"))
    }

    ins_by_id: dict[int, ET.Element] = {}
    ins_position: dict[int, int] = {}
    for position, el in enumerate(doc_root.iter(_w("ins"))):
        if id(el) in paragraph_mark_revisions:
            continue
        raw_id = el.get(_w("id"))
        if raw_id is None:
            continue
        try:
            numeric_id = int(raw_id)
        except ValueError:
            continue
        if numeric_id in ins_by_id:
            continue
        ins_by_id[numeric_id] = el
        ins_position[numeric_id] = position

    next_revision_id = ooxml_util.max_existing_id(doc_root) + 1

    entries: list[dict[str, Any]] = []
    for spec in specs:
        # The FIRST candidate in DOCUMENT ORDER, not the first the caller
        # recorded: an issue with several edits in one paragraph records its
        # revision ids in descending-offset order (the order pass 1 must
        # apply them in), so picking `revision_ids[0]` would hang the
        # reference on the LAST insertion a reader reaches.
        owned = [
            int(revision_id)
            for revision_id in spec["revision_ids"]
            if int(revision_id) in ins_by_id
        ]
        target = (
            ins_by_id[min(owned, key=lambda rid: ins_position[rid])] if owned else None
        )
        if target is None:
            # No insertion of this issue's survived into the document (a
            # delete-only edit). Nothing to hang a reference on; skip rather
            # than attach it to somebody else's revision.
            continue
        footnote_id = next_footnote_id
        next_footnote_id += 1
        ref_run = ET.SubElement(target, _w("r"))
        # `<w:rPr>` FIRST: OOXML fixes the order of a run's children, and
        # run properties are the only thing allowed before the content.
        ref_run.append(_footnote_style_rpr())
        ref = ET.SubElement(ref_run, _w("footnoteReference"))
        ref.set(_w("id"), str(footnote_id))
        entries.append(
            {
                "id": footnote_id,
                "issue_key": spec["issue_key"],
                "text": spec["text"],
                "revision_id": next_revision_id,
            }
        )
        next_revision_id += 1

    if not entries:
        return docx_bytes

    new_parts: dict[str, bytes] = {
        DOCUMENT_PART: _serialize_document(doc_root, original_root_open_tag)
    }

    if footnotes_root is None:
        footnotes_root = ET.Element(_w("footnotes"))
        for special_id, kind in ((-1, "separator"), (0, "continuationSeparator")):
            fn = ET.SubElement(footnotes_root, _w("footnote"))
            fn.set(_w("type"), kind)
            fn.set(_w("id"), str(special_id))
            p = ET.SubElement(fn, _w("p"))
            r = ET.SubElement(p, _w("r"))
            ET.SubElement(r, _w(kind))

    for entry in entries:
        fn = ET.SubElement(footnotes_root, _w("footnote"))
        fn.set(_w("id"), str(entry["id"]))
        p = ET.SubElement(fn, _w("p"))
        # `<w:pPr>` FIRST (OOXML child order), carrying the paragraph style
        # Word gives a footnote body (issue #647).
        ppr = ET.SubElement(p, _w("pPr"))
        pstyle = ET.SubElement(ppr, _w("pStyle"))
        pstyle.set(_w("val"), docx_parts.FOOTNOTE_TEXT_STYLE_ID)
        ref_run = ET.SubElement(p, _w("r"))
        ref_run.append(_footnote_style_rpr())
        ET.SubElement(ref_run, _w("footnoteRef"))
        # Issue #615: the BODY is tracked too, matching the tracked
        # reference that points at it.
        ins = ET.SubElement(p, _w("ins"))
        ins.set(_w("id"), str(entry["revision_id"]))
        ins.set(_w("author"), author)
        ins.set(_w("date"), timestamp_iso)
        ins.append(_make_text_run(None, " " + entry["text"]))

    new_parts[FOOTNOTES_PART] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(footnotes_root, encoding="unicode").encode("utf-8")
    )

    # Issue #647: the two runs above now carry `<w:rStyle
    # w:val="FootnoteReference"/>` and the body paragraph `<w:pStyle
    # w:val="FootnoteText"/>`. A style reference resolves to nothing unless
    # the package DEFINES the style, and a document that has never carried a
    # footnote does not -- which is why the number rendered as plain inline
    # text. Definitions the document already has are never touched.
    styles_bytes, styles_created = _footnote_styles_part(originals.get(STYLES_PART))
    if styles_bytes is not None:
        new_parts[STYLES_PART] = styles_bytes

    # Parts this call ADDED to the package, each needing a relationship and a
    # content-type override: `(part name, target, rel type, content type)`.
    added_parts: list[tuple[str, str, str, str]] = []
    if not have_footnotes:
        added_parts.append(
            (
                FOOTNOTES_PART,
                "footnotes.xml",
                docx_parts.FOOTNOTES_REL_TYPE,
                docx_parts.FOOTNOTES_CONTENT_TYPE,
            )
        )
    if styles_created:
        added_parts.append(
            (
                STYLES_PART,
                "styles.xml",
                docx_parts.STYLES_REL_TYPE,
                docx_parts.STYLES_CONTENT_TYPE,
            )
        )

    if added_parts:
        rels_root = (
            ET.fromstring(originals[RELS_PART])
            if RELS_PART in names
            else ET.Element(_pkg("Relationships"))
        )
        ct_root = ET.fromstring(originals[CONTENT_TYPES_PART])
        next_rel_id = redline_generate._max_rel_id(rels_root) + 1
        for part_name, target, rel_type, content_type in added_parts:
            rel = ET.SubElement(rels_root, _pkg("Relationship"))
            rel.set("Id", f"rId{next_rel_id}")
            next_rel_id += 1
            rel.set("Type", rel_type)
            rel.set("Target", target)

            override = ET.SubElement(ct_root, _ct("Override"))
            override.set("PartName", "/" + part_name)
            override.set("ContentType", content_type)

        new_parts[RELS_PART] = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(rels_root, encoding="unicode").encode("utf-8")
        )
        new_parts[CONTENT_TYPES_PART] = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(ct_root, encoding="unicode").encode("utf-8")
        )

    return _write_package(infos, originals, new_parts)


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------


def _failure(edit: dict[str, Any], block_id: str, reason: str, detail: str) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "issue_key": edit.get("issue_key"),
        "kind": edit.get("kind"),
        "reason": reason,
        "detail": detail,
    }


def _block_op_block_id(block_op: dict[str, Any]) -> str:
    """The block a whole-block op is ADDRESSED to -- the block it deletes, or
    the block a new paragraph is anchored to. Reported as the `block_id` of
    every `applied`/`failures` entry the op produces, so an op is as
    locatable in a report as a span edit is."""
    if block_op["op"] == OP_DELETE_BLOCK:
        return block_op["block_id"]
    return block_op["anchor_block_id"]


def _block_op_edit(block_op: dict[str, Any]) -> dict[str, Any]:
    """A whole-block op in the same shape `_pair_ops_into_edits` produces, so
    `_failure` and `record` treat it exactly like a span edit. `kind` is the
    OP NAME (`delete_block`/`insert_block_after`), which is what tells the two
    apart in a report."""
    if block_op["op"] == OP_DELETE_BLOCK:
        source_text, insert_text = block_op.get("text", ""), ""
    else:
        source_text, insert_text = "", block_op["new_text"]
    return {
        "kind": block_op["op"],
        "issue_key": block_op["issue_key"],
        "block_id": _block_op_block_id(block_op),
        "source_text": source_text,
        "insert_text": insert_text,
    }


def apply_block_transcript(
    docx_bytes: bytes,
    proven: dict[str, Any],
    *,
    author: str,
    timestamp_iso: str,
    rationale_by_issue: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Compile a PROVEN block transcript into OOXML tracked changes.

    `proven` is `block_transcript.validate_block_patches()`'s success shape.
    A rejected transcript is a caller-contract violation (`ValueError`) --
    the validator's whole job is to make sure nothing unproven reaches a
    writer, so this module never sees `status="rejected"` in a correct
    pipeline and must not quietly write half of one.

    Returns `{"docx_bytes", "applied", "failures",
    "revision_ids_by_issue"}`. `docx_bytes` is `None` unless at least one edit
    applied AND the assembled package passed BOTH
    `redline_generate.verify_docx_round_trip` and the issue #623 projection
    proofs (`redline_projections.verify_projections`) -- never
    partially-corrupt, unverified, or unproven bytes. `applied` and
    `failures` carry one entry per EDIT (not per block), so a paragraph with
    three edits reports three outcomes, and one edit's failure never blocks
    its siblings.
    `revision_ids_by_issue` maps each `issue_key` to the `w:id`s of the
    revisions this call created for it -- the keys the footnote anchors were
    resolved by, and the handle a caller needs to accept/reject one issue's
    whole contribution.
    """
    if proven.get("status") != "proven":
        raise ValueError(
            "apply_block_transcript requires a PROVEN transcript "
            f"(block_transcript.validate_block_patches status='proven'); got "
            f"{proven.get('status')!r}. A rejected transcript must never reach a writer."
        )

    rationales = dict(rationale_by_issue or {})
    blocks = proven.get("blocks") or []
    block_ops = list(proven.get("block_ops") or [])
    empty: dict[str, Any] = {
        "docx_bytes": None,
        "applied": [],
        "failures": [],
        "revision_ids_by_issue": {},
    }
    if not blocks and not block_ops:
        return empty

    norm = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    if norm.get("status") != "normalized":
        failures = [
            _failure(
                edit,
                block["block_id"],
                REASON_DOCUMENT_NOT_NORMALIZABLE,
                "the document does not normalize, so no block addresses resolve",
            )
            for block in blocks
            for edit in _pair_ops_into_edits(block["ops"])
        ]
        failures.extend(
            _failure(
                _block_op_edit(block_op),
                _block_op_block_id(block_op),
                REASON_DOCUMENT_NOT_NORMALIZABLE,
                "the document does not normalize, so no block addresses resolve",
            )
            for block_op in block_ops
        )
        return dict(empty, failures=failures)

    block_map = extraction_normalization_stage.build_block_map(norm["paragraphs"])
    doc_root = ET.fromstring(_read_package(docx_bytes)[1][DOCUMENT_PART])
    resolved = _resolve_physical_paragraphs(doc_root, norm["paragraphs"])

    # ---- Plan every edit against the LIVE document before touching bytes.
    failures: list[dict[str, Any]] = []
    planned: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        block_id = block["block_id"]
        edits = _pair_ops_into_edits(block["ops"])
        live = block_map.get(block_id)
        if live is None:
            for edit in edits:
                failures.append(
                    _failure(
                        edit,
                        block_id,
                        REASON_UNKNOWN_BLOCK_ID,
                        f"block_id {block_id!r} is not in this document's block map",
                    )
                )
            continue
        if live.get("text", "") != block.get("source_text", ""):
            for edit in edits:
                failures.append(
                    _failure(
                        edit,
                        block_id,
                        REASON_BLOCK_TEXT_CHANGED,
                        (
                            f"block {block_id!r} no longer holds the text the transcript "
                            "was proven against"
                        ),
                    )
                )
            continue

        spans = live.get("physical_spans") or []
        for edit in edits:
            span_index = _physical_span_for(spans, edit["start"], edit["end"])
            if span_index is None:
                failures.append(
                    _failure(
                        edit,
                        block_id,
                        REASON_SPANS_PHYSICAL_PARAGRAPH,
                        (
                            f"offsets [{edit['start']}, {edit['end']}) cross a physical "
                            "<w:p> boundary inside this block; docx_editor edits physical "
                            "paragraphs, so there is nowhere to write one tracked change"
                        ),
                    )
                )
                continue
            location = resolved.get((block_id, span_index))
            if location is None:
                failures.append(
                    _failure(
                        edit,
                        block_id,
                        REASON_PARAGRAPH_NOT_RESOLVED,
                        (
                            f"physical paragraph {span_index} of block {block_id!r} could "
                            "not be resolved to the <w:p> it was extracted from"
                        ),
                    )
                )
                continue
            paragraph_index = location["index"]
            # Block offsets are relative to the STRIPPED clean text; both
            # writers work on the live paragraph's own accepted-view text.
            # `lead` is the whole difference between the two (see
            # `_resolve_physical_paragraphs`).
            span_start = spans[span_index][0] - location["lead"]
            planned.setdefault(paragraph_index, []).append(
                dict(
                    edit,
                    block_id=block_id,
                    paragraph_index=paragraph_index,
                    local_start=edit["start"] - span_start,
                    local_end=edit["end"] - span_start,
                    paragraph_text=location["text"],
                )
            )

    # ---- Plan the whole-block ops (issue #622), same guards, same
    # fail-closed-per-op discipline: a transcript is only proof of the
    # document it was proven against, and an op this module cannot write
    # safely never blocks the ops that it can.
    table_paragraph_indexes = _table_cell_paragraph_indexes(doc_root)
    planned_block_ops: list[dict[str, Any]] = []
    for block_op in block_ops:
        edit = _block_op_edit(block_op)
        target_block_id = _block_op_block_id(block_op)
        from_start = (
            block_op["op"] == OP_INSERT_BLOCK_AFTER
            and block_op["anchor_block_id"] == block_transcript.ANCHOR_START
        )
        if from_start:
            # "start" names no block: the new paragraph goes BEFORE the
            # document's first block's first physical `<w:p>`.
            first = norm["paragraphs"][0] if norm["paragraphs"] else None
            if first is None:
                failures.append(
                    _failure(
                        edit,
                        target_block_id,
                        REASON_UNKNOWN_BLOCK_ID,
                        (
                            f"{block_op['op']} anchors at "
                            f"{block_transcript.ANCHOR_START!r} but this document "
                            "normalizes to no blocks at all"
                        ),
                    )
                )
                continue
            live_block_id = first["block_id"]
            wanted_spans = [0]
        else:
            live = block_map.get(target_block_id)
            if live is None:
                failures.append(
                    _failure(
                        edit,
                        target_block_id,
                        REASON_UNKNOWN_BLOCK_ID,
                        (
                            f"{block_op['op']} names block_id {target_block_id!r}, which "
                            "is not in this document's block map"
                        ),
                    )
                )
                continue
            if block_op["op"] == OP_DELETE_BLOCK and live.get("text", "") != block_op.get(
                "text", ""
            ):
                failures.append(
                    _failure(
                        edit,
                        target_block_id,
                        REASON_BLOCK_TEXT_CHANGED,
                        (
                            f"block {target_block_id!r} no longer holds the text the "
                            f"{block_op['op']} was proven against"
                        ),
                    )
                )
                continue
            span_count = len(live.get("physical_spans") or [])
            if span_count == 0:
                failures.append(
                    _failure(
                        edit,
                        target_block_id,
                        REASON_PARAGRAPH_NOT_RESOLVED,
                        (
                            f"block {target_block_id!r} has no physical paragraphs to "
                            f"{block_op['op']} against"
                        ),
                    )
                )
                continue
            live_block_id = target_block_id
            # A deletion touches EVERY physical `<w:p>` of the block; an
            # insertion anchors on its LAST one.
            wanted_spans = (
                list(range(span_count))
                if block_op["op"] == OP_DELETE_BLOCK
                else [span_count - 1]
            )

        locations = [resolved.get((live_block_id, span)) for span in wanted_spans]
        if any(location is None for location in locations):
            # Fail closed for the WHOLE op: half a deleted clause, or a new
            # clause anchored to a paragraph that is not the one the
            # transcript meant, is worse than no edit at all.
            failures.append(
                _failure(
                    edit,
                    target_block_id,
                    REASON_PARAGRAPH_NOT_RESOLVED,
                    (
                        f"{block_op['op']} on block {live_block_id!r}: a physical "
                        "paragraph could not be resolved to the <w:p> it was extracted "
                        "from"
                    ),
                )
            )
            continue

        paragraph_indexes = [location["index"] for location in locations]
        if any(index - 1 in table_paragraph_indexes for index in paragraph_indexes):
            failures.append(
                _failure(
                    edit,
                    target_block_id,
                    REASON_BLOCK_OP_UNSUPPORTED_IN_TABLE,
                    (
                        f"{block_op['op']} targets block {target_block_id!r}, whose "
                        "paragraph lives in a table cell; whole-block ops in tables are "
                        "table-structure edits (row/column semantics) this compiler does "
                        "not attempt. Span edits inside the cell remain supported."
                    ),
                )
            )
            continue

        planned_block_ops.append(
            {
                "op": block_op["op"],
                "edit": edit,
                "anchor_key": target_block_id,
                "insert_before": from_start,
                "paragraph_indexes": paragraph_indexes,
                "new_text": block_op.get("new_text", ""),
            }
        )

    # ---- Leave `[Intentionally omitted.]` under a heading this batch empties
    # (issue #646, reversing issue #645's heading removal). Decided over the
    # WHOLE batch, after every op is planned, because "is any body under this
    # heading surviving?" is a question about the batch and not about one op
    # -- a `delete_block` paired with an `insert_block_after` on the same
    # block is a clause REPLACED in place, and the replacement, not a
    # placeholder, is what goes under its heading. See
    # `_plan_omitted_clause_placeholders` for the rule and its guards.
    anchored_block_ids: set = set()
    for op in planned_block_ops:
        if op["op"] != OP_INSERT_BLOCK_AFTER:
            continue
        if op["insert_before"]:
            # `"start"` names no block: the new paragraph goes before the
            # FIRST block's first `<w:p>`, i.e. under that block's heading.
            if norm["paragraphs"]:
                anchored_block_ids.add(norm["paragraphs"][0]["block_id"])
        else:
            anchored_block_ids.add(op["anchor_key"])
    placeholder_block_ids = _plan_omitted_clause_placeholders(
        doc_root,
        norm["paragraphs"],
        {op["anchor_key"] for op in planned_block_ops if op["op"] == OP_DELETE_BLOCK},
        anchored_block_ids,
    )
    for op in planned_block_ops:
        if op["op"] != OP_DELETE_BLOCK:
            continue
        if op["anchor_key"] not in placeholder_block_ids:
            continue
        op["placeholder"] = block_transcript.OMITTED_CLAUSE_PLACEHOLDER
        # The placeholder IS this edit's insertion, so the `applied` record
        # -- which is what proof 2 rebuilds the expected accepted text from
        # (`redline_projections._expected_accept_all_texts`) and what the
        # caller reports -- has to say so. `_block_op_edit` leaves
        # `insert_text` empty for a delete because most deletes insert
        # nothing; this one does.
        op["edit"]["insert_text"] = block_transcript.OMITTED_CLAUSE_PLACEHOLDER

    applied: list[dict[str, Any]] = []
    # The EDIT dicts behind `applied`, kept whole (offsets included) for the
    # issue #623 projection gate below: proving "accept-all == the final
    # text" against the transcript as WRITTEN would fail-closed on a batch
    # where one edit legitimately failed closed and its siblings landed, so
    # the proof is built from what actually applied. `applied` itself is the
    # caller-facing report shape and deliberately carries no offsets.
    applied_edit_specs: list[dict[str, Any]] = []
    revision_ids_by_issue: dict[str, list[int]] = {}
    phase_one_ids: set[int] = set()

    def record(edit: dict[str, Any], revision_ids) -> None:
        applied_edit_specs.append(dict(edit))
        bucket = revision_ids_by_issue.setdefault(edit["issue_key"], [])
        for revision_id in revision_ids:
            if revision_id not in bucket:
                bucket.append(int(revision_id))
        applied.append(
            {
                "block_id": edit["block_id"],
                "issue_key": edit["issue_key"],
                "kind": edit["kind"],
                "source_text": edit["source_text"],
                "insert_text": edit["insert_text"],
                "revision_ids": [int(rid) for rid in revision_ids],
            }
        )

    # ---- Pass 1: replacements and pure deletions, via docx_editor.
    replacement_work = {
        index: [e for e in edits if e["kind"] in ("replace", "delete")]
        for index, edits in planned.items()
    }
    replacement_work = {index: edits for index, edits in replacement_work.items() if edits}

    working_bytes = docx_bytes
    if replacement_work:
        with tempfile.TemporaryDirectory(prefix="redline-block-apply-") as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "input.docx"
            input_path.write_bytes(docx_bytes)
            doc = docx_editor.Document.open(
                input_path, author=author, workspace_dir=str(tmp_path / "workspace")
            )
            try:
                for paragraph_index in sorted(replacement_work):
                    ref = doc.get_paragraph(paragraph_index).ref
                    # DESCENDING offset order: an edit never shifts the text
                    # before it, so every remaining (lower) offset in this
                    # paragraph stays valid as the paragraph mutates.
                    for edit in sorted(
                        replacement_work[paragraph_index],
                        key=lambda e: e["local_start"],
                        reverse=True,
                    ):
                        occurrence = _occurrence_before(
                            edit["paragraph_text"], edit["source_text"], edit["local_start"]
                        )
                        try:
                            if edit["kind"] == "replace":
                                result = doc.replace(
                                    edit["source_text"],
                                    edit["insert_text"],
                                    paragraph=ref,
                                    occurrence=occurrence,
                                )
                            else:
                                result = doc.delete(
                                    edit["source_text"], paragraph=ref, occurrence=occurrence
                                )
                        except (
                            docx_editor.TextNotFoundError,
                            docx_editor.AmbiguousTextError,
                            docx_editor.HashMismatchError,
                        ) as exc:
                            failures.append(
                                _failure(
                                    edit,
                                    edit["block_id"],
                                    REASON_EDIT_NOT_APPLIED,
                                    f"docx_editor refused the edit: {type(exc).__name__}",
                                )
                            )
                            continue
                        ref = str(result)
                        edit["_applied"] = True
                        phase_one_ids.update(int(rid) for rid in result.revision_ids)
                        record(edit, result.revision_ids)
                doc.save()
            finally:
                doc.close()
            working_bytes = input_path.read_bytes()

    # ---- Pass 2: pure insertions (owned) + this call's revision dates.
    insertion_work = {
        index: [e for e in edits if e["kind"] == "insert"] for index, edits in planned.items()
    }
    insertion_work = {index: edits for index, edits in insertion_work.items() if edits}

    if insertion_work or phase_one_ids or planned_block_ops:
        infos, originals = _read_package(working_bytes)
        doc_xml_text = originals[DOCUMENT_PART].decode("utf-8")
        original_root_open_tag = ooxml_util.root_open_tag(doc_xml_text)
        ooxml_util.register_declared_namespaces(
            ooxml_util.declared_namespaces_anywhere(doc_xml_text)
        )
        root = ET.fromstring(originals[DOCUMENT_PART])

        # Document-wide `w:id` uniqueness, the same sweep
        # `ooxml_util.max_existing_id` documents: never collide with an
        # id a human-edited upload (or pass 1) already carries.
        next_revision_id = ooxml_util.max_existing_id(root) + 1
        paragraphs = _body_paragraph_elements(root)

        for paragraph_index in sorted(insertion_work):
            if paragraph_index > len(paragraphs):  # pragma: no cover - defensive
                for edit in insertion_work[paragraph_index]:
                    failures.append(
                        _failure(
                            edit,
                            edit["block_id"],
                            REASON_PARAGRAPH_NOT_RESOLVED,
                            "the paragraph index no longer exists after pass 1",
                        )
                    )
                continue
            p = paragraphs[paragraph_index - 1]
            # Only the replacements that ACTUALLY applied moved anything;
            # one that failed closed left the paragraph exactly as it was.
            done = [
                edit
                for edit in replacement_work.get(paragraph_index, [])
                if edit.get("_applied")
            ]
            for edit in sorted(
                insertion_work[paragraph_index], key=lambda e: e["local_start"], reverse=True
            ):
                # Translate the offset across whatever pass 1 already wrote
                # into this paragraph: each APPLIED replacement entirely
                # before this point moved the accepted view by
                # len(new) - len(old). See the module docstring, "The three
                # passes".
                shift = sum(
                    len(other["insert_text"]) - len(other["source_text"])
                    for other in done
                    if other["local_end"] <= edit["local_start"]
                )
                reason = _apply_pure_insertion(
                    p,
                    edit["local_start"] + shift,
                    edit["insert_text"],
                    next_revision_id,
                    author,
                    timestamp_iso,
                )
                if reason is not None:
                    failures.append(
                        _failure(
                            edit,
                            edit["block_id"],
                            reason,
                            (
                                "the run holding this insertion point is not a plain "
                                "paragraph-level <w:t> run this module can split"
                            ),
                        )
                    )
                    continue
                record(edit, [next_revision_id])
                next_revision_id += 1

        # ---- Pass 3: whole-block ops (issue #622), in the same tree.
        #
        # After pass 2 on purpose: `insert_block_after` adds a `<w:p>`, which
        # renumbers every paragraph after it, and every earlier pass
        # addresses paragraphs BY that number. Inside this pass nothing is
        # addressed by number any more: `paragraphs` is a snapshot taken
        # before the first insertion, so each op holds its target ELEMENT and
        # a new sibling cannot move it out from under a later op.
        def allocate() -> int:
            nonlocal next_revision_id
            revision_id = next_revision_id
            next_revision_id += 1
            return revision_id

        for op in planned_block_ops:
            if op["op"] != OP_DELETE_BLOCK:
                continue
            revision_ids: list[int] = []
            target_indexes = op["paragraph_indexes"]
            # `[Intentionally omitted.]` (issue #646) goes in the LAST struck
            # `<w:p>` of the block, which therefore keeps its paragraph mark
            # -- see `_delete_paragraph`. Every earlier paragraph of a
            # multi-paragraph clause is deleted whole, mark included, so
            # accept-all collapses them into the one that survives.
            for position, paragraph_index in enumerate(target_indexes):
                is_last = position == len(target_indexes) - 1
                revision_ids.extend(
                    _delete_paragraph(
                        paragraphs[paragraph_index - 1],
                        allocate,
                        author,
                        timestamp_iso,
                        placeholder=op.get("placeholder") if is_last else None,
                    )
                )
            record(op["edit"], revision_ids)

        parents = _parent_map(root)
        # Where the NEXT insertion for a given anchor goes: several
        # `insert_block_after`s naming one anchor must land in transcript
        # order, so each one anchors on the paragraph the previous one
        # created rather than on the original anchor again (which would
        # reverse them).
        anchor_cursor: dict[str, tuple[ET.Element, ET.Element]] = {}
        for op in planned_block_ops:
            if op["op"] != OP_INSERT_BLOCK_AFTER:
                continue
            anchor_paragraph = paragraphs[op["paragraph_indexes"][-1] - 1]
            cursor = anchor_cursor.get(op["anchor_key"])
            if cursor is None:
                parent = parents.get(id(anchor_paragraph))
                if parent is None:  # pragma: no cover - defensive
                    failures.append(
                        _failure(
                            op["edit"],
                            op["edit"]["block_id"],
                            REASON_PARAGRAPH_NOT_RESOLVED,
                            "the anchor paragraph has no parent element to insert into",
                        )
                    )
                    continue
                # `"start"` inserts BEFORE the first block's first `<w:p>`;
                # every other anchor inserts after its LAST one.
                position = list(parent).index(anchor_paragraph) + (
                    0 if op["insert_before"] else 1
                )
            else:
                parent, previous = cursor
                position = list(parent).index(previous) + 1
            body_id, mark_id = allocate(), allocate()
            new_paragraph = _build_inserted_paragraph(
                anchor_paragraph, op["new_text"], body_id, mark_id, author, timestamp_iso
            )
            parent.insert(position, new_paragraph)
            anchor_cursor[op["anchor_key"]] = (parent, new_paragraph)
            record(op["edit"], [body_id, mark_id])

        # `docx-editor`
        # always stamps `datetime.now()` and exposes no way to inject a
        # caller-supplied timestamp, so rewrite the date on EXACTLY the
        # revisions pass 1 created -- never a document-wide sweep that could
        # restamp a human editor's own pending tracked changes.
        if phase_one_ids:
            for el in root.iter():
                if el.tag not in (_w("ins"), _w("del")):
                    continue
                raw_id = el.get(_w("id"))
                if raw_id is None:
                    continue
                try:
                    numeric_id = int(raw_id)
                except ValueError:
                    continue
                if numeric_id not in phase_one_ids:
                    continue
                el.set(_w("date"), timestamp_iso)
                if el.get(_W16DU_DATE_ATTR) is not None:
                    el.set(_W16DU_DATE_ATTR, timestamp_iso)

        working_bytes = _write_package(
            infos, originals, {DOCUMENT_PART: _serialize_document(root, original_root_open_tag)}
        )

    if not applied:
        return dict(empty, failures=failures)

    # ---- Pass 4: one footnoted rationale per issue, anchored by revision id.
    footnote_specs = [
        {
            "issue_key": issue_key,
            "revision_ids": revision_ids,
            "text": rationales.get(issue_key),
        }
        for issue_key, revision_ids in revision_ids_by_issue.items()
    ]
    working_bytes = inject_issue_footnotes(
        working_bytes, footnote_specs, author=author, timestamp_iso=timestamp_iso
    )

    try:
        redline_generate.verify_docx_round_trip(working_bytes)
    except ValueError as exc:
        # Fail closed exactly like `generate_redline`'s own round-trip gate
        # (issue #263): a writer bug, not a counterparty-document condition,
        # but still never delivered as corrupt bytes.
        return {
            "docx_bytes": None,
            "applied": [],
            "failures": failures
            + [
                _failure(entry, entry["block_id"], REASON_ROUND_TRIP_FAILED, str(exc))
                for entry in applied
            ],
            "revision_ids_by_issue": {},
        }

    # ---- The projection gate (issue #623). MANDATORY and fail-closed: the
    # round-trip check above only proves the package opens. These three
    # proofs are what say the redline is SURGICAL -- rejecting it reproduces
    # the document we were handed, accepting it reproduces the text the model
    # approved, and no package part changed that a redline may not touch.
    # Scoped to the revisions THIS call created, so a counterparty's own
    # pending tracked changes are not rejected out from under the source they
    # are part of (see `redline_projections`' module docstring).
    projection_report = redline_projections.verify_projections(
        docx_bytes,
        working_bytes,
        proven,
        revision_ids={
            revision_id
            for ids in revision_ids_by_issue.values()
            for revision_id in ids
        },
        applied_edits=applied_edit_specs,
    )
    if projection_report["status"] != "verified":
        batch_failure = _failure(
            {},
            None,
            REASON_PROJECTION_VERIFICATION_FAILED,
            "; ".join(
                f"[{entry['proof']}] {entry['detail']}"
                for entry in projection_report["failures"]
            ),
        )
        # Say WHICH proof rejected the document, structurally, not only in
        # the prose detail -- a gate that can only report "something failed"
        # costs an operator the whole diagnosis.
        batch_failure["proof_failures"] = projection_report["failures"]
        return {
            "docx_bytes": None,
            "applied": [],
            "failures": failures + [batch_failure],
            "revision_ids_by_issue": {},
        }

    return {
        "docx_bytes": working_bytes,
        "applied": applied,
        "failures": failures,
        "revision_ids_by_issue": revision_ids_by_issue,
    }


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke note. The gate test (tests/test_redline_block_apply.py) is
    the authoritative check."""
    print(
        "redline_block_apply: run tests/test_redline_block_apply.py for the "
        "authoritative check (synthetic fixtures only)."
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
