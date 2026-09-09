#!/usr/bin/env python3
"""
Extraction and normalization stage (issue #80, BLOCKING GATE head of the
review-pipeline "brain" chain -- #81/#194 depend on this).

## Problem this solves

ARCHITECTURE.md's data-flow step 11 ("Extract text (owned docx library); run
the input-normalization pass") and `scripts/normalize_input.py`'s own
docstring both name this module's job explicitly: normalize_input.py is the
DECISION layer over an already-parsed `{"paragraphs": [{"heading", "text",
"revisions"}, ...]}` document; the actual OOXML `<w:ins>`/`<w:del>`/
`<w:commentReference>` EXTRACTION from a real `.docx` -- reading only the
allowlisted parts (ARCHITECTURE.md -> "Input normalization (before review)" ->
"OOXML part allowlist") -- is this module's job.

This module therefore has two halves:

  1. `extract_document_paragraphs()` -- allowlisted OOXML extraction. Opens
     the `.docx` ZIP and reads ONLY `word/document.xml` (the main document
     body, including tables and table-nested tables -- both explicitly
     "Allowed" per the ARCHITECTURE.md table). Every other part --
     `docProps/core.xml` / `docProps/app.xml` / `docProps/custom.xml`
     (document properties), `word/header*.xml` / `word/footer*.xml`
     (headers/footers), `word/comments.xml` (comment TEXT -- only the
     structural fact "a comment exists here" is used, never its content,
     matching normalize_input.py's "Comments never gate ... REGARDLESS of
     ... content"), any drawing/chart/diagram part -- is never opened at
     all, so payload text planted there cannot structurally reach this
     function's output. Within `word/document.xml` itself, textbox body
     text (`w:txbxContent`) and content-control placeholder bodies
     (`w:sdt`/`w:sdtContent`) are excluded by construction too: the walker
     below only recurses into a fixed, explicit set of tags (`w:p`, `w:tbl`/
     `w:tr`/`w:tc`, `w:r`, `w:ins`, `w:del`, `w:fldSimple`, `w:hyperlink`)
     -- it is not a generic "recurse into every child" walk, so an
     unrecognized wrapper tag (`w:drawing`, `w:sdt`,
     `mc:AlternateContent`, ...) is simply never descended into.
     `w:hyperlink` joined that set in issue #663: it is a transparent
     wrapper around ordinary runs whose text a reader sees inline (a
     cross-reference, a defined-term link, a URL), so excluding it was not
     a security boundary but silent content loss -- the model reviewed a
     clause with words missing from the middle of it.

     Image alt text (`wp:docPr/@descr`, `@title`) is an XML
     ATTRIBUTE, not run text, and this module never reads attributes other
     than the few named ones it explicitly looks up (`w:author`, `w:val`,
     `w:instr`), so alt text cannot reach the output either.

     Footnotes/endnotes (`word/footnotes.xml`, `word/endnotes.xml`) are
     "Allowed only when deliberately surfaced" per ARCHITECTURE.md; this
     slice does not implement footnote surfacing, so -- consistent with the
     allowlist's narrow-by-design, default-deny stance -- they are treated
     as un-surfaced and excluded (the part is simply never opened), not a
     silent gap: a document whose reviewable substance lives only in a
     footnote is out of scope for this slice, same as any other
     not-yet-implemented surfacing rule.

  2. `normalize_paragraphs()` / `extract_and_normalize()` -- calls
     `scripts/normalize_input.py`'s per-paragraph decision function
     (`_normalize_paragraph`, the exact function normalize_input.py's
     docstring designates this stage as the caller of) over each extracted
     paragraph, and returns a STRUCTURED paragraph list -- `[{"heading":
     ..., "text": ...}, ...]` -- not `normalize_input.normalize()`'s single
     lossy joined `clean_body` string. This is the SAME shape
     the retired standard-form diff
     draft parameter and `backend/src/corpus.py`'s `extract_clauses()`
     already consume (see corpus.py's module docstring: "issue #80's output
     shape"), so each paragraph stays independently anchorable by the
     downstream diff stage instead of being flattened into one string that
     discards paragraph boundaries.

     A document normalizes iff every paragraph normalizes (same
     all-or-nothing rule as `normalize_input.normalize()`). If any paragraph
     fails closed, the stage fails closed to the issue #38 internal analysis
     report (`normalize_input.build_unnormalizable_report()`), never a
     partial/guessed body.

## Pointer-only pipeline-stage entry point

`run_stage()` is the Step Functions task-shaped entry point, matching the
POINTER-ONLY PAYLOAD RULE (issue #19) documented in
`infra/lambda/mock_review/handler.py`: its input event and returned dict
carry `review_id` / `owner_sub` / S3 keys / status / reason only -- never
document text. Document bytes are read and normalized-output JSON is written
via two injected callables (`fetch_docx_bytes`, `store_json`) so this stage
is fully testable offline (no live AWS/Bedrock/network -- moto/fakes only,
per this issue's Required verification) without this module taking on a
hard boto3 dependency or any infra wiring of its own; a caller (a future
Lambda handler, out of scope for this pure-Python slice) supplies real S3
reads/writes.

See: ARCHITECTURE.md -> "Input normalization (before review)",
`scripts/normalize_input.py`, `docs/output-contract.md` -> "Fail-closed
internal analysis report".
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import clause_boundaries  # noqa: E402
import normalize_input  # noqa: E402
import ooxml_util  # noqa: E402
import redline_generate  # noqa: E402

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# The ONE OOXML part this module ever opens -- the allowlist is enforced by
# construction (every other part is simply never read), not by filtering
# already-extracted content. See module docstring.
ALLOWED_DOCUMENT_PART = "word/document.xml"


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Low-level OOXML paragraph walking
# ---------------------------------------------------------------------------


def _run_is_hidden(run_el: ET.Element) -> bool:
    """True if this run carries `<w:rPr><w:vanish/></w:rPr>` (hidden text)."""
    rpr = run_el.find(_w("rPr"))
    if rpr is None:
        return False
    vanish = rpr.find(_w("vanish"))
    if vanish is None:
        return False
    val = vanish.get(_w("val"))
    if val is None:
        return True
    return val.lower() not in ("0", "false", "off")


class _ParaBuilder:
    """Accumulates a single logical paragraph's dual text streams (pre-edit
    "original" vs. accept-all "resulting"), pending-tracked-change clusters,
    and comment/hidden-text/field signals, while `_walk_content` walks its
    OOXML children in document order."""

    def __init__(self) -> None:
        self.original_parts: list[str] = []
        self.resulting_parts: list[str] = []
        self.clusters: list[dict[str, Any]] = []
        self._current_cluster: dict[str, Any] | None = None
        self.has_hidden_text = False
        self.has_comment = False
        self.fields: list[dict[str, str]] = []

    def _close_cluster(self) -> None:
        if self._current_cluster is not None:
            self.clusters.append(self._current_cluster)
            self._current_cluster = None

    def _ensure_cluster(self, author: str | None, inside_field_code: bool) -> None:
        # A cluster is a maximal run of CONTIGUOUS w:ins/w:del elements from
        # ONE author -- any intervening plain run (add_plain), hidden run
        # (add_hidden), or a DIFFERENT author's revision closes it and opens
        # a new one. Two different authors editing back-to-back with no
        # intervening plain text must still surface as two distinct
        # clusters, not silently merge into the first author seen -- this is
        # what lets `normalize_input.py`'s disclosure note report an
        # accurate cluster/author count for the multi-cluster/multi-author
        # accept-all case (issue #563), even though neither more than one
        # cluster nor a cluster inside a field code (issue #530) gates
        # normalization on its own any more.
        if (
            self._current_cluster is not None
            and author is not None
            and self._current_cluster.get("author") is not None
            and self._current_cluster.get("author") != author
        ):
            self._close_cluster()
        if self._current_cluster is None:
            self._current_cluster = {
                "author": author,
                "inside_field_code": inside_field_code,
            }
        else:
            if author and not self._current_cluster.get("author"):
                self._current_cluster["author"] = author
            if inside_field_code:
                self._current_cluster["inside_field_code"] = True

    def add_plain(self, text: str) -> None:
        self._close_cluster()
        self.original_parts.append(text)
        self.resulting_parts.append(text)

    def add_ins(self, text: str, author: str | None, inside_field_code: bool) -> None:
        self._ensure_cluster(author, inside_field_code)
        self.resulting_parts.append(text)

    def add_del(self, text: str, author: str | None, inside_field_code: bool) -> None:
        self._ensure_cluster(author, inside_field_code)
        self.original_parts.append(text)

    def add_hidden(self) -> None:
        self._close_cluster()
        self.has_hidden_text = True

    def finish(self) -> None:
        self._close_cluster()


def _process_run(
    run_el: ET.Element,
    builder: _ParaBuilder,
    mode: str,
    author: str | None,
    inside_field_code: bool,
) -> None:
    if _run_is_hidden(run_el):
        # STRIP: hidden text never reaches either stream, regardless of
        # content (ARCHITECTURE.md -> "Input normalization (before
        # review)"). Direct children only (findall with a bare tag name),
        # never `.//`, so a run's own nested drawing/textbox content (if any
        # were hostilely nested inside a run) cannot leak in via this path.
        builder.add_hidden()
        return

    if run_el.find(_w("commentReference")) is not None:
        # Structural signal only -- comment TEXT lives in word/comments.xml,
        # a part this module never opens. See module docstring.
        builder.has_comment = True

    if mode == "del":
        text = "".join(t.text or "" for t in run_el.findall(_w("delText")))
    else:
        text = "".join(t.text or "" for t in run_el.findall(_w("t")))
    text += "".join("\t" for _ in run_el.findall(_w("tab")))

    if not text:
        return

    if mode == "plain":
        builder.add_plain(text)
    elif mode == "ins":
        builder.add_ins(text, author, inside_field_code)
    elif mode == "del":
        builder.add_del(text, author, inside_field_code)


def _process_fld_simple(fld_el: ET.Element, builder: _ParaBuilder) -> None:
    """`<w:fldSimple w:instr="...">` -- a field whose cached result is the
    element's own child content. If that content is plain (static) text,
    RESOLVE it into the visible stream plus a `field` revision note. If a
    pending tracked change lives INSIDE the result region instead (the
    counterparty is live-editing the field's displayed text), that is the
    documented `inside_field_code` ambiguity -- no `field` revision is
    emitted (there is no static result to resolve); the pending change
    bubbles up as an ordinary cluster with `inside_field_code=True`."""
    instr = (fld_el.get(_w("instr")) or "").strip()

    field_builder = _ParaBuilder()
    _walk_content(list(fld_el), field_builder, mode="plain", author=None, inside_field_code=True)
    field_builder.finish()

    builder.has_hidden_text = builder.has_hidden_text or field_builder.has_hidden_text
    builder.has_comment = builder.has_comment or field_builder.has_comment

    if field_builder.clusters:
        builder.clusters.extend(field_builder.clusters)
        builder.original_parts.extend(field_builder.original_parts)
        builder.resulting_parts.extend(field_builder.resulting_parts)
        return

    result_text = "".join(field_builder.original_parts)
    if result_text:
        builder.add_plain(result_text)
    if instr:
        builder.fields.append({"field_code": f"{{ {instr} }}", "field_result": result_text})


def _walk_content(
    elements: list[ET.Element],
    builder: _ParaBuilder,
    *,
    mode: str,
    author: str | None,
    inside_field_code: bool,
) -> None:
    """Walks a fixed, explicit set of OOXML content tags. Any tag not
    explicitly handled (`w:drawing`, `w:sdt`, `mc:AlternateContent`,
    `w:pict`, `w:smartTag`, bookmarks, proofing marks, ...) is skipped
    WITHOUT recursion -- this is what keeps textbox bodies, content-control
    placeholder bodies, and any other non-allowlisted nested content out of
    the output by construction rather than by an after-the-fact filter.

    `w:hyperlink` IS on that allowlist (issue #663). It is a transparent
    wrapper around ordinary `w:r`/`w:ins`/`w:del` children that a reader
    sees as part of the sentence -- Word emits it for cross-references,
    defined-term links, and plain URLs -- so skipping it dropped visible
    words out of the middle of a clause, silently. Descending is also what
    keeps this walk in agreement with the writer side:
    `redline_block_apply._accepted_text_runs` recurses into every container
    except `w:del`/`w:moveFrom`, so hyperlink text was already addressable
    by the block-apply path while being invisible to extraction. Recursion
    carries `mode`/`author` through unchanged, so a `w:hyperlink` nested in
    a pending `w:ins`/`w:del` -- and an `ins`/`del` nested in a hyperlink --
    both land in the right stream via the existing revision handlers.

    Neither of the hyperlink's own attributes is read: `r:id` (external,
    into `word/_rels/document.xml.rels`) and `w:anchor` (internal
    cross-reference) are both ignored, and the rels part is never opened,
    so a hyperlink's TARGET can no more reach the model than image alt text
    can. Only the text a reader sees does.

    `w:moveTo` is on the allowlist for the SAME reason (issue #686), and as
    the same kind of transparent wrapper: it is a tracked MOVE's new
    location, which Word renders as ordinary visible text and
    `redline_block_apply._accepted_text_runs` reads as ordinary visible
    text. Skipping it left the block map built from the uploaded bytes --
    the one `review_spine` hands the model and proves every transcript
    against -- missing a sentence the delivered document contains. Its
    counterpart `w:moveFrom` (the move's OLD location) stays excluded from
    BOTH streams, matching the writer's walk, which skips exactly `w:del`
    and `w:moveFrom`. The four range markers (`w:moveFromRangeStart` and
    friends) are empty and fall through to the generic skip.

    `w:moveFrom` is deliberately NOT routed into `del` mode, which is the
    obvious reading of "moveFrom is removed text" and is a trap (issue
    #686): `del` mode puts text in the pre-edit stream only, which
    manufactures a pending tracked-change record whose `resulting_text` is
    empty for any paragraph moved away WHOLE -- the shape Word writes when a
    clause is dragged elsewhere -- and `normalize_input._normalize_paragraph`
    refuses that record as malformed, failing the entire upload closed. A
    move is not a proposal about what a clause should SAY: the text is
    present in the document either way, at its new location, so both move
    halves are resolved here rather than surfaced as a pending revision.
    The disposition is not silent -- `accepted_revision_disclosure` names
    the `moveFrom`/`moveTo` counts the materializer accepted (issue #685).
    An ordinary `w:del` covering a whole paragraph is a different shape and
    keeps failing closed (tests/test_extraction_normalization_stage_80.py,
    [G3d]): deleted text leaves the document, moved text does not.
    """
    for el in elements:
        tag = el.tag
        if tag == _w("r"):
            _process_run(el, builder, mode, author, inside_field_code)
        elif tag in (_w("hyperlink"), _w("moveTo")):
            _walk_content(list(el), builder, mode=mode, author=author, inside_field_code=inside_field_code)
        elif tag == _w("ins"):
            ins_author = el.get(_w("author")) or author
            _walk_content(list(el), builder, mode="ins", author=ins_author, inside_field_code=inside_field_code)
        elif tag == _w("del"):
            del_author = el.get(_w("author")) or author
            _walk_content(list(el), builder, mode="del", author=del_author, inside_field_code=inside_field_code)
        elif tag == _w("moveFrom"):
            # EXPLICIT (issue #686), not left to the generic skip below: the
            # move's old location is excluded from BOTH streams, exactly as
            # `redline_block_apply._accepted_text_runs` excludes it. See this
            # function's docstring for why `del` mode would be wrong here.
            continue
        elif tag == _w("fldSimple"):
            _process_fld_simple(el, builder)
        else:
            continue


def _build_paragraph_record(p_el: ET.Element) -> dict[str, Any]:
    """Extracts one raw `<w:p>` into `{"text", "revisions"}` (no `heading`
    key -- heading-vs-body grouping happens one level up, in
    `extract_document_paragraphs`, the same convention
    the retired standard-form docx loader used for the
    canonical standard form)."""
    builder = _ParaBuilder()
    _walk_content(list(p_el), builder, mode="plain", author=None, inside_field_code=False)
    builder.finish()

    original_text = "".join(builder.original_parts).strip()
    resulting_text = "".join(builder.resulting_parts).strip()

    revisions: list[dict[str, Any]] = []
    for cluster in builder.clusters:
        entry: dict[str, Any] = {
            "type": "tracked_change",
            # Any w:ins/w:del still present in a real .docx is, by
            # definition, PENDING -- accepting a change strips the markup
            # entirely (see scripts/normalize_input.py's module docstring,
            # issue #199). This extractor therefore never emits
            # status="accepted"; that status exists in normalize_input.py's
            # schema for completeness / other callers only.
            "status": "unresolved",
            "author": cluster.get("author") or "unknown",
            "original_text": original_text,
            "resulting_text": resulting_text,
        }
        if cluster.get("inside_field_code"):
            entry["inside_field_code"] = True
        revisions.append(entry)

    if builder.has_comment:
        revisions.append({"type": "comment", "status": "open"})
    if builder.has_hidden_text:
        revisions.append({"type": "hidden_text", "status": "n/a"})
    for field in builder.fields:
        revisions.append({"type": "field", "status": "n/a", **field})

    return {
        "text": original_text,
        "resulting_text": resulting_text,
        "revisions": revisions,
    }


def _iter_table_paragraphs(tbl_el: ET.Element):
    for tr in tbl_el.findall(_w("tr")):
        for tc in tr.findall(_w("tc")):
            yield from _iter_body_paragraphs(tc)


def _iter_body_paragraphs(container: ET.Element):
    """Yields `<w:p>` elements in document order, descending into tables
    (and tables nested within tables) -- both explicitly "Allowed" per the
    ARCHITECTURE.md OOXML part-allowlist table. Any other container tag
    (`w:sdt`, `w:drawing`, `mc:AlternateContent`, `w:sectPr`, bookmarks, ...)
    is skipped without recursion -- see `_walk_content`'s docstring for the
    same construction-not-filter allowlist rationale."""
    for child in container:
        if child.tag == _w("p"):
            yield child
        elif child.tag == _w("tbl"):
            yield from _iter_table_paragraphs(child)
        else:
            continue


# ---------------------------------------------------------------------------
# Document-level extraction (heading/body grouping)
# ---------------------------------------------------------------------------


def extract_document_paragraphs(docx_bytes: bytes) -> list[dict[str, Any]]:
    """
    Allowlisted OOXML extraction. Opens the `.docx` ZIP and reads ONLY
    `word/document.xml` -- every other part is never opened (see module
    docstring for the full allowlist rationale).

    Returns raw (pre-normalization) logical paragraphs, grouped by
    `scripts/clause_boundaries.py`'s shared clause-boundary detector
    (issue #277): a Heading-style `w:p` starts a new logical paragraph, the
    same rule the retired standard-form docx loader used for
    the canonical standard form; when no Heading style is present (real
    counterparty drafts routinely lose named heading styles), a
    document-signals fallback (numbered/lettered lead-ins, outline level,
    bold single-line paragraphs, ALL-CAPS short lines) starts one instead --
    this is the DRAFT side, which `clause_boundaries`'s module docstring
    documents as allowed to relax the style-only rule; the canonical
    standard-form loaders keep requiring proper Heading styles, unchanged.
    Subsequent non-boundary `w:p`s are siblings of the most recent boundary
    until the next one.

      [{"heading": "...", "heading_p_index": 3, "heading_source_text": "...",
        "physical_paragraphs": [{"text": "...", "revisions": [...]}, ...]},
       ...]

    `heading_p_index` / `heading_source_text` (issue #645) are the boundary
    `<w:p>`'s own IDENTITY -- its position in the part's preorder `w:p`
    numbering, the same numbering `p_index` uses -- and the text that
    paragraph carries before `clean_heading_text` strips its lead-in. They
    exist so a writer can identify the heading ELEMENT: `delete_block`
    (`scripts/redline_block_apply.py`) strikes a clause's body and, when no
    body is left under it, leaves `[Intentionally omitted.]` in its place,
    because the ACCEPTED document must not show a numbered heading with no
    clause under it (issue #645, action set by issue #646). Both are
    None/`""` for the implicit leading group -- text before the document's
    first boundary paragraph -- which has no heading `<w:p>` at all; a writer
    must read that as "there is no heading here to be left empty" rather than
    fall back to guessing an index.

    Each sibling under a heading is kept as its own PHYSICAL paragraph
    record here -- NOT flattened into one combined text/revisions list --
    because `normalize_input._normalize_paragraph()`'s accept-all
    disposition replaces a paragraph's `clean_text` wholesale with that
    paragraph's own `resulting_text`. If multiple physical `<w:p>`s were
    merged into a single logical paragraph before normalization, a lone
    pending tracked change on ONE sibling would accept-all over the
    WHOLE merged text, silently discarding every other sibling's clause
    text (issue #80 fix round 1 / #200's heading-with-multiple-body-
    paragraphs scenario). `normalize_paragraphs()` below normalizes each
    physical paragraph independently and only then joins their clean
    texts into the logical paragraph's final `text`, so accept-all can
    never reach across a sibling boundary.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        names = set(zf.namelist())
        if ALLOWED_DOCUMENT_PART not in names:
            raise ValueError(
                f"Not a valid WordprocessingML .docx: {ALLOWED_DOCUMENT_PART} "
                f"is missing. (The hostile-file / magic-number gauntlet -- "
                f"backend/src/upload_validation.py -- owns rejecting "
                f"non-OOXML input before this stage ever runs; this is a "
                f"defensive check, not this stage's job.)"
            )
        document_xml = zf.read(ALLOWED_DOCUMENT_PART)
        # Every other part in `names` (docProps/*, header*.xml, footer*.xml,
        # word/comments.xml, word/drawings/*, word/charts/*, ...) is
        # deliberately never read -- the allowlist is enforced by never
        # calling zf.read() on anything but ALLOWED_DOCUMENT_PART.

    root = ET.fromstring(document_xml)
    body = root.find(_w("body"))
    if body is None:
        return []

    # Physical-paragraph IDENTITY, carried alongside the text (issue #621
    # fix round 1). `docx_editor` numbers paragraphs by minidom's
    # `getElementsByTagName("w:p")` -- preorder over the WHOLE part -- so
    # `p_index` here is that numbering minus one, and a writer holding a
    # normalized paragraph can address the exact `<w:p>` it came from
    # instead of scanning the live document for the first `<w:p>` whose
    # text happens to be equal (which slides onto a LATER duplicate the
    # moment a paragraph fails to match itself).
    p_index_by_element = {id(el): index for index, el in enumerate(root.iter(_w("p")))}

    logical: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def _flush() -> None:
        if current is not None:
            logical.append(
                {
                    "heading": current["heading"],
                    # IDENTITY of the boundary `<w:p>` the heading was lifted
                    # from, and that paragraph's own text (issue #645). Both
                    # None/"" for the implicit leading group, which has no
                    # heading paragraph at all. See this function's docstring.
                    "heading_p_index": current["heading_p_index"],
                    "heading_source_text": current["heading_source_text"],
                    "physical_paragraphs": current["physical_paragraphs"],
                }
            )

    for p_el in _iter_body_paragraphs(body):
        record = _build_paragraph_record(p_el)
        operative_text = record.get("resulting_text") or record["text"]
        if not operative_text and not record["revisions"]:
            continue  # empty paragraph (spacer) -- nothing to extract

        if clause_boundaries.is_boundary_paragraph_ooxml(p_el, operative_text):
            _flush()
            heading_text = clause_boundaries.clean_heading_text(operative_text)
            current = {
                "heading": heading_text or "<untitled>",
                "heading_p_index": p_index_by_element[id(p_el)],
                "heading_source_text": record["text"],
                "physical_paragraphs": [],
            }
        else:
            if current is None:
                current = {
                    "heading": "<untitled>",
                    # No boundary paragraph was ever seen: this group is the
                    # document's preamble, and there is no heading `<w:p>` at
                    # all. A writer must treat that as "no heading here can be
                    # left empty", never as an index it may guess at.
                    "heading_p_index": None,
                    "heading_source_text": "",
                    "physical_paragraphs": [],
                }
            # Kept as its own physical-paragraph record -- see this
            # function's docstring for why siblings must not be merged
            # before normalization.
            current["physical_paragraphs"].append(
                {
                    "text": record["text"],
                    "revisions": record["revisions"],
                    "p_index": p_index_by_element[id(p_el)],
                }
            )

    _flush()
    return logical


# ---------------------------------------------------------------------------
# Accept-all materialization (issue #563; formatting revisions and tracked
# moves added by issue #685)
#
# `normalize_paragraphs` (below) accepts a paragraph's pending tracked
# changes in TEXT SPACE ONLY -- the `resulting_text` the model reads and a
# block transcript is proven against. The uploaded `.docx` bytes themselves
# still carry the raw `<w:ins>`/`<w:del>` markup at that point.
# `materialize_accept_all`
# closes that gap: it applies the SAME accept-all disposition directly to
# `word/document.xml`, so a caller (`scripts/review_spine.py`) can thread ONE
# canonical, already-accepted document through block-mapping, patch-apply, and
# the delivered redline, rather than text-space and byte-space ever
# disagreeing about what "the document" says.
#
# Issue #685 widened the disposition from `<w:ins>`/`<w:del>` to the
# counterparty's pending FORMATTING revisions and tracked MOVES, which used
# to ride through untouched and surface in the delivered redline as their
# own "Formatted: ..." revisions. Because a document can carry those with
# no pending TEXT revision at all -- and therefore no per-paragraph accept
# note -- the caller now runs this unconditionally and reads the report it
# returns, instead of inferring from text space whether byte space has
# anything to do.
# ---------------------------------------------------------------------------


#: Revision-markup elements that record a FORMATTING / PROPERTY change
#: (issue #685). Each one holds the PREVIOUS properties; the element it
#: sits inside (`w:rPr`, `w:pPr`, `w:tblPr`, `w:sectPr`, `w:tblGrid`, ...)
#: already holds the CURRENT ones. So ACCEPTING such a revision means
#: deleting the change record and keeping what is there -- never touching
#: the surrounding properties. Left in place (the pre-#685 behavior) they
#: survive into the delivered redline, where Word renders them as
#: "Formatted: ..." revisions attributed to the counterparty's author,
#: indistinguishable to a reader from the edits we are actually asking for.
PROPERTY_CHANGE_TAGS = frozenset(
    {
        "rPrChange",
        "pPrChange",
        "sectPrChange",
        "tblPrChange",
        "tblPrExChange",
        "trPrChange",
        "tcPrChange",
        "tblGridChange",
        "numberingChange",
    }
)

#: Tracked-MOVE markup removed with its content (issue #685): `w:moveFrom`
#: is the text at the move's OLD location, which an accepted move deletes
#: (exactly like `w:del`, and its runs even carry `w:delText`), plus the
#: four empty range markers that bracket a move at either end and mean
#: nothing once the move is applied.
MOVE_REMOVE_TAGS = frozenset(
    {
        "moveFrom",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
    }
)

#: Tracked-MOVE markup UNWRAPPED (issue #685): `w:moveTo` is the text at the
#: move's NEW location, which an accepted move keeps -- exactly like
#: `w:ins`, and exactly how the writer side already reads it
#: (`redline_block_apply._accepted_text_runs` skips `w:del`/`w:moveFrom` and
#: descends into everything else), so unwrapping it changes no offset any
#: reader or writer computes; it only drops the revision wrapper.
MOVE_UNWRAP_TAGS = frozenset({"moveTo"})

#: Every child tag `_splice_accept_all` removes OUTRIGHT, content included.
_ACCEPT_BY_REMOVAL = frozenset(
    {_w("del")} | {_w(name) for name in MOVE_REMOVE_TAGS | PROPERTY_CHANGE_TAGS}
)

#: Every child tag `_splice_accept_all` UNWRAPS (children spliced up into
#: the parent, wrapper discarded).
_ACCEPT_BY_UNWRAP = frozenset({_w("ins")} | {_w(name) for name in MOVE_UNWRAP_TAGS})

#: Revision-tracking tags that are neither `*Change` nor `move*` and so are
#: not caught by the shape rule in `_revision_markup_local_name` below.
#: `w:cellIns`/`w:cellDel`/`w:cellMerge` record a tracked table-cell
#: insertion/deletion/merge; accepting one is a STRUCTURAL edit to the table
#: (dropping or merging cells), not a marker removal, so this module does
#: not apply them -- it REPORTS them instead (issue #685: a revision kind
#: the splice does not recognise must be disclosed, never silently passed
#: through).
_OTHER_REVISION_TAGS = frozenset({"ins", "del", "cellIns", "cellDel", "cellMerge"})

_WORD_TAG_PREFIX = f"{{{WORD_NS}}}"


def _revision_markup_local_name(tag: str) -> str | None:
    """The WordprocessingML local name of `tag` when it is revision-tracking
    markup, else None.

    Deliberately a SHAPE rule (`*Change`, `move*`) rather than a closed
    list: a revision kind this module has never heard of -- a newer
    `*PrChange`, a move variant -- still classifies as revision markup and
    is therefore reported by `_unapplied_revision_markup` rather than
    passing through unnoticed. Nothing outside the WordprocessingML
    namespace can match.
    """
    if not tag.startswith(_WORD_TAG_PREFIX):
        return None
    local = tag[len(_WORD_TAG_PREFIX) :]
    if local.endswith("Change") or local.startswith("move") or local in _OTHER_REVISION_TAGS:
        return local
    return None


def _unapplied_revision_markup(root: ET.Element) -> dict[str, int]:
    """Counts, by local tag name, every revision-tracking element STILL
    present under `root`. Run AFTER `_splice_accept_all`, so everything it
    finds is by definition a kind the splice does not apply -- the
    fail-closed disclosure half of issue #685 (`materialize_accept_all`'s
    report carries it; `scripts/review_spine.py` folds it into
    `normalization_notes`, where the attorney sees it, rather than letting
    an unhandled revision kind ride into the delivered redline unmentioned).
    """
    counts: dict[str, int] = {}
    for el in root.iter():
        local = _revision_markup_local_name(el.tag)
        if local is not None:
            counts[local] = counts.get(local, 0) + 1
    return counts


def _splice_accept_all(el: ET.Element, tally: dict[str, int] | None = None) -> None:
    """Mutates `el`'s children in place, accepting every pending tracked
    change anywhere under them: each `<w:del>` child is removed ENTIRELY
    (including its own subtree -- a rejected/superseded span never existed
    once accepted), and each `<w:ins>` child is unwrapped -- its own
    children (recursively processed FIRST) are spliced into `el` at the
    `<w:ins>`'s position, and the now-empty wrapper itself is discarded.
    Every other child is recursed into (so a `<w:ins>`/`<w:del>` nested
    arbitrarily deep -- inside a `<w:tbl>`/`<w:tr>`/`<w:tc>`, or doubly
    nested for an inserted-then-deleted span -- is still found and accepted)
    but otherwise left completely untouched: no other tag, no attribute, is
    ever read or modified (same "touches nothing else" discipline
    `_walk_content` documents for extraction).

    Since issue #685 the same two dispositions cover TWO more revision
    families, which is what stops the counterparty's own pending FORMATTING
    revisions from riding into the delivered redline:

      * REMOVED outright, like `<w:del>`: every `PROPERTY_CHANGE_TAGS`
        element (`w:rPrChange`, `w:pPrChange`, `w:sectPrChange`, ...), whose
        subtree holds the PREVIOUS properties and whose removal therefore
        KEEPS the current ones; and every `MOVE_REMOVE_TAGS` element
        (`w:moveFrom` -- the move's old location -- and the four empty
        move range markers).
      * UNWRAPPED, like `<w:ins>`: `w:moveTo`, the move's new location,
        whose text an accepted move keeps.

    Neither disposition moves a single visible character: `w:moveTo`'s runs
    are already in the accepted view on both sides (`_walk_content` here,
    `redline_block_apply._accepted_text_runs` on the writer side), and a
    `*PrChange` subtree carries no `w:t` at all. Accepting them changes the
    MARKUP only, which is exactly what "accept a formatting revision" means.

    `tally` (optional) counts, by local tag name, every revision element
    accepted -- the input to the `normalization_notes` disclosure
    `materialize_accept_all_with_report` returns.

    Nested markup (`<w:ins>` wrapping `<w:del>` -- text inserted, then
    deleted, before ever being accepted) is handled correctly BY
    CONSTRUCTION, with no special case: unwrapping the `<w:ins>` first
    recurses into its own children, where the nested `<w:del>` is removed
    outright (including its content) before anything is spliced back up --
    so an inserted-then-deleted span contributes nothing to the materialized
    document, mirroring `_walk_content`'s own mode-switching semantics for
    the TEXT streams (`add_del` never contributes to `resulting_parts`,
    regardless of what mode enclosed it) at the XML level instead.

    KNOWN LIMITATION (issue #563 follow-up): a `<w:ins>`/`<w:del>` living
    inside `<w:pPr><w:rPr>` does not wrap ordinary run content -- it is
    Word's record of an inserted or deleted PARAGRAPH MARK (the pilcrow),
    proposing to keep or remove the break between this `<w:p>` and the
    next one. This function strips those markers exactly like any other
    `<w:ins>`/`<w:del>` (by construction, since it recurses into `<w:pPr>`/
    `<w:rPr>` like any other element), but does NOT apply their semantics:
    accepting a deleted paragraph mark should MERGE this paragraph with the
    next `<w:p>`, and this function never merges `<w:p>` siblings. The
    materialized bytes therefore still show the paragraph split as two
    `<w:p>` elements (with a vestigial empty `<w:pPr><w:rPr/></w:pPr>` where
    the marker lived) even though `normalize_paragraphs`'s TEXT-space
    reading already folds the two into one logical paragraph -- see
    `materialize_accept_all_with_report`'s own docstring for how this can
    surface as a spurious paragraph split in the delivered redline, and
    `tests/test_accept_all_materializer.py` for the fixture pinning this as
    current behavior.
    """
    original_children = list(el)
    for child in original_children:
        el.remove(child)
    for child in original_children:
        if child.tag in _ACCEPT_BY_REMOVAL:
            # Accepted-away entirely, including all descendants: a deleted
            # or moved-from span never existed once accepted, and a
            # `*PrChange`'s subtree is the PREVIOUS properties, which
            # accepting discards.
            _count_accepted(tally, child.tag)
            continue
        if child.tag in _ACCEPT_BY_UNWRAP:
            _count_accepted(tally, child.tag)
            _splice_accept_all(child, tally)  # accept nested content FIRST
            for grandchild in list(child):
                el.append(grandchild)
            continue
        _splice_accept_all(child, tally)
        el.append(child)


def _count_accepted(tally: dict[str, int] | None, tag: str) -> None:
    """Records one accepted revision element in `tally`, keyed by local tag
    name (`"rPrChange"`, `"ins"`, ...). A None tally counts nothing -- the
    disposition itself is identical either way."""
    if tally is None:
        return
    local = tag[len(_WORD_TAG_PREFIX) :] if tag.startswith(_WORD_TAG_PREFIX) else tag
    tally[local] = tally.get(local, 0) + 1


def materialize_accept_all(docx_bytes: bytes) -> bytes:
    """The bytes half of `materialize_accept_all_with_report` -- see that
    function for the full contract. Kept as the plain-bytes entry point
    every caller that does not need the disclosure report already uses."""
    return materialize_accept_all_with_report(docx_bytes)[0]


def accepted_revision_disclosure(report: dict[str, Any]) -> str | None:
    """The `normalization_notes` sentence(s) for a
    `materialize_accept_all_with_report` report, or None when there is
    nothing to disclose (issue #685).

    Two independent sentences, either of which may be absent:

      * what was accepted in MARKUP space beyond the `w:ins`/`w:del` text
        revisions `normalize_input`'s own per-paragraph notes already
        disclose -- formatting/property changes and tracked moves, named by
        kind and count; and
      * what was LEFT IN PLACE because this module has no accept rule for
        it, named the same way. Reported, never silent.

    Neither sentence ends with the literal `"accepted-all into the operative
    draft."` tail: `frontend/src/toaster/receipt.ts::acceptedChangesSummary`
    COUNTS that exact tail and drops its whole summary line when it parses
    fewer sentences than it counted, so borrowing the tail here would
    silently suppress the per-edit summary the attorney already gets. These
    sentences are additive disclosure alongside it, not part of its count.
    """
    accepted = {
        name: count
        for name, count in (report.get("accepted") or {}).items()
        if name not in ("ins", "del")
    }
    unapplied = report.get("unapplied") or {}
    sentences: list[str] = []
    if accepted:
        sentences.append(
            "Pending formatting and move revisions ("
            + ", ".join(f"{count} {name}" for name, count in sorted(accepted.items()))
            + ") were accepted into the operative draft before review."
        )
    if unapplied:
        sentences.append(
            "Revision markup with no accept rule was left in place ("
            + ", ".join(f"{count} {name}" for name, count in sorted(unapplied.items()))
            + ")."
        )
    return " ".join(sentences) if sentences else None


def materialize_accept_all_with_report(docx_bytes: bytes) -> tuple[bytes, dict[str, Any]]:
    """
    Physically accept every pending tracked change in `word/document.xml`,
    returning `(materialized_bytes, report)` where `report` is
    `{"accepted": {<local tag name>: count}, "unapplied": {...}}` --
    what was accepted, and what revision markup this module has no rule for
    and therefore left in place (issue #685; `accepted_revision_disclosure`
    turns it into the `normalization_notes` sentence).

    Every `<w:del>` element is removed INCLUDING its content, and every
    `<w:ins>` element is unwrapped (its children spliced into its parent,
    the wrapper dropped) -- everywhere in the part, at any nesting depth.
    Since issue #685 the same two dispositions also accept the
    counterparty's pending FORMATTING revisions (`PROPERTY_CHANGE_TAGS`,
    removed -- the element records the PREVIOUS properties, so dropping it
    keeps the current ones) and tracked MOVES (`w:moveFrom` and the move
    range markers removed, `w:moveTo` unwrapped). Before that, those rode
    through untouched into the delivered redline, where Word showed them as
    the counterparty's own "Formatted: ..." revisions sitting beside our
    edits.

    When there is NOTHING to accept the input `docx_bytes` are returned
    unchanged, byte for byte -- no re-zip, no re-serialization, and no
    round-trip verification of a document this stage never altered. That is
    what lets a caller run this unconditionally instead of guessing from
    text-space signals whether byte-space has anything to do (a document
    whose only pending revisions are formatting ones produces no
    per-paragraph accept note at all, so the pre-#685 "only materialize when
    `normalization_notes` is present" gate never fired for it).
    Touches nothing else: every other zip entry is copied through
    byte-for-byte, and no other tag or attribute in `word/document.xml` is
    ever modified (`_splice_accept_all`'s own contract).

    This is issue #563's physical half of accept-all: `normalize_paragraphs`
    already accepts a paragraph's pending changes in TEXT SPACE (the
    `resulting_text` the model reads and a block transcript is proven
    against); this
    function applies the identical disposition to the BYTES, so a caller
    threading its output through
    `redline_generate.generate_redline_from_blocks`'s
    `normalized_docx_bytes` gets `redline_block_apply.apply_block_transcript`
    operating on a document that already carries no pending markup of its
    own -- only the toaster's own new redline lands on top of it, matching
    this issue's decided product posture ("the redline is delivered ON the
    accepted document, and that fact is disclosed").

    KNOWN LIMITATION (issue #563 follow-up): "operate on one canonical,
    already-accepted document" is accurate for revisions to run CONTENT, but
    not yet for an accepted deleted PARAGRAPH MARK (`<w:pPr><w:rPr><w:del/>
    </w:rPr></w:pPr>`, Word's record of a proposed paragraph merge) --
    `_splice_accept_all` strips that marker like any other `<w:ins>`/
    `<w:del>` but does not merge the two `<w:p>` siblings it joined, so a
    paragraph carrying both a content revision and a deleted paragraph mark
    still materializes as TWO `<w:p>` elements, one logical paragraph short
    of what `normalize_paragraphs`'s TEXT-space reading (and therefore what
    the model reviewed) already treats as one. The delivered redline is
    otherwise on the accepted document, but the attorney's own diff against
    their original will show this one paragraph as a spurious split rather
    than a clean merge. See `_splice_accept_all`'s own docstring for the
    mechanism and `tests/test_accept_all_materializer.py` for the fixture
    pinning this as current behavior.

    Round-trips through `redline_generate.verify_docx_round_trip` before
    returning -- raises `ValueError` (that function's own exception) rather
    than ever handing a caller bytes that do not open. That check covers
    every document this function REWRITES; the nothing-to-accept case above
    returns the caller's own bytes untouched and is deliberately not put
    through it, so running this unconditionally can never turn a document
    the stage did not alter into a refusal.

    Uses the SAME guarded `ET.register_namespace` discipline
    `scripts/ooxml_util.py` owns (issue #560/#561): a real
    uploaded document can declare a namespace prefix ElementTree's own
    serializer would refuse to register (`ns<digits>`, reserved for its own
    auto-generated bindings) or declare a prefix on a non-root element that
    ElementTree hoists to the root on serialization --
    `ooxml_util.register_declared_namespaces` /
    `merge_hoisted_namespaces` handle both; reused here rather than
    reimplemented so the two directions (block-transcript redline write vs.
    accept-all materialization) can never drift apart on this.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}

    if ALLOWED_DOCUMENT_PART not in originals:
        raise ValueError(
            f"Not a valid WordprocessingML .docx: {ALLOWED_DOCUMENT_PART} is missing."
        )

    original_document_xml = originals[ALLOWED_DOCUMENT_PART].decode("utf-8")
    original_root_open_tag = ooxml_util.root_open_tag(original_document_xml)
    # The same call `redline_generate.inject_export_marker_and_footnotes`
    # makes: every prefix the document declares ANYWHERE (not just on the root) is
    # registered so ElementTree picks matching prefixes for anything it
    # re-serializes, guarded against the reserved `ns<digits>` pattern.
    ooxml_util.register_declared_namespaces(
        ooxml_util.declared_namespaces_anywhere(original_document_xml)
    )

    root = ET.fromstring(originals[ALLOWED_DOCUMENT_PART])
    accepted: dict[str, int] = {}
    _splice_accept_all(root, accepted)
    # Whatever revision markup survives the splice is, by definition, a kind
    # it has no rule for -- reported, never silently passed through.
    report: dict[str, Any] = {
        "accepted": accepted,
        "unapplied": _unapplied_revision_markup(root),
    }
    if not accepted:
        # Nothing was accepted, so the materialized document IS the input.
        # Returning it verbatim keeps this a true no-op (byte-identical, and
        # a document this stage never altered is never put through a
        # round-trip that could fail it) while the report still discloses
        # any unapplied markup found.
        return docx_bytes, report

    # Serialize the (mutated) tree, then splice the ORIGINAL root start tag
    # back in verbatim, merged with any namespace the serializer hoisted --
    # same "Preserve" technique ooxml_util.py documents at length.
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
                if info.filename == ALLOWED_DOCUMENT_PART
                else originals[info.filename]
            )
            zf_out.writestr(info, data)

    materialized_bytes = out_buf.getvalue()
    redline_generate.verify_docx_round_trip(materialized_bytes)
    return materialized_bytes, report


# ---------------------------------------------------------------------------
# Normalization (delegates the documented rule to normalize_input.py)
# ---------------------------------------------------------------------------

#: Format of a logical-paragraph block id (issue #619): `p` + the 1-based
#: document-order position, zero-padded to four digits (`p0001`, `p0042`).
#: Four digits is a display width, not a ceiling -- position 10000 simply
#: renders as `p10000` and stays unique and ordered.
BLOCK_ID_FORMAT = "p%04d"


def _block_id(position: int) -> str:
    """The block id for a 1-based logical-paragraph position. Single
    definition so the id format can never drift between the stamping site
    and any reader (issue #619)."""
    return BLOCK_ID_FORMAT % position


def build_block_map(
    normalized_paragraphs: list[dict[str, Any]],
) -> "OrderedDict[str, dict[str, Any]]":
    """
    Addressing view over `normalize_paragraphs`' logical-paragraph records
    (issue #619): an ORDERED mapping `block_id -> {"text", "heading",
    "physical_spans", "index"}`, in document order.

    This is the lookup half of the block-addressing foundation for
    model-authored redline transcripts (Candidate E): the model names a
    `block_id` and a reader resolves it here, instead of having to prove a
    document-wide-unique quote.

    `index` is the 0-based position of the record in
    `normalized_paragraphs` -- i.e. `int(block_id[1:]) - 1` for ids this
    module stamped -- so a caller holding only the map can still index back
    into the paragraph list it was built from. `physical_spans` is the
    record's own span list (issue #564), passed through unchanged: this
    function never re-derives spans, and a block's `text`/`heading` are the
    record's own values, so `build_block_map(paras)[bid]["text"]` is always
    exactly that paragraph's `text`.

    Fails closed rather than inventing addressing: a record with no
    `block_id`, or a duplicate id, raises `ValueError`. A block map is only
    trustworthy if it is 1:1 with the paragraphs it addresses.
    """
    block_map: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for index, paragraph in enumerate(normalized_paragraphs):
        block_id = paragraph.get("block_id")
        if not block_id:
            raise ValueError(
                f"normalized paragraph at index {index} carries no block_id; "
                "block ids are stamped by normalize_paragraphs() and must not "
                "be stripped downstream."
            )
        if block_id in block_map:
            raise ValueError(
                f"duplicate block_id {block_id!r} at index {index}: block ids "
                "must be 1:1 with logical paragraphs."
            )
        block_map[block_id] = {
            "text": paragraph.get("text", ""),
            "heading": paragraph.get("heading", "<untitled>"),
            "physical_spans": paragraph.get("physical_spans", []),
            "index": index,
        }
    return block_map


def normalize_paragraphs(raw_paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Applies `scripts/normalize_input.py`'s documented per-paragraph
    accept/reject rule (`_normalize_paragraph` -- the exact function that
    module's docstring names this stage as the caller of) to each raw
    extracted paragraph.

    Unlike `normalize_input.normalize()`, this does NOT flatten the result
    into one joined `clean_body` string -- it returns a structured
    paragraph list, `[{"heading": ..., "text": ..., "physical_spans": [...],
    "block_id": ...}, ...]`, matching the retired standard-form diff's /
    `backend/src/corpus.py` draft-input contract (see module docstring), so
    each paragraph stays independently anchorable downstream.
    `physical_spans` is ADDITIVE (issue #564) and so are `block_id` (issue
    #619) and `heading_p_index`/`heading_source_text` (issue #645): existing
    readers of `heading`/`text` alone are unaffected.

    `block_id` is an IMMUTABLE, CODE-ASSIGNED address for the logical
    paragraph -- `BLOCK_ID_FORMAT % position`, 1-based over the logical
    paragraphs in document order (`p0001`, `p0002`, ...). It is assigned
    exactly once per review, here, from the materialized bytes, and is
    never reused for a different paragraph and never renumbered downstream:
    everything after this stage treats it as an opaque handle. This is the
    addressing foundation for model-authored redline transcripts, where a
    block is named by id instead of by a quote that must be proven
    document-wide unique. `build_block_map()` (above) is the lookup view.
    Because a document normalizes all-or-nothing (below), the ids are dense
    and gapless whenever any are returned at all.

    A document normalizes iff every paragraph normalizes -- one
    un-normalizable paragraph fails the whole document closed, same
    all-or-nothing rule as `normalize_input.normalize()`.

    Each heading's PHYSICAL paragraphs (`raw_paragraphs[i]
    ["physical_paragraphs"]`, see `extract_document_paragraphs`'s
    docstring) are normalized INDEPENDENTLY -- `_normalize_paragraph` is
    called once per physical paragraph, never once over a combined
    multi-sibling text/revisions blob. This is what keeps an accept-all
    disposition on one sibling's lone pending tracked change from
    overwriting (and silently dropping) another sibling's plain clause
    text under the same heading; the siblings' clean texts are only
    joined together AFTER each has normalized on its own.

    Sibling clean texts are joined with `"\\n"` (issue #564; was `" "`
    before this issue) -- `scripts/text_fold.py`'s whitespace-collapse
    fold treats a newline exactly like a space, so this changes no
    matching outcome, but it makes the physical-paragraph join visible in
    TEXT space, distinct from the `"\\n\\n"` a caller (`review_spine.py`)
    uses to join separate LOGICAL paragraphs together. `physical_spans` is
    the `[start, end)` character range each physical paragraph's own clean
    text occupies in the joined `text`, in order -- concatenating
    `text[s:e]` for each span with `"\\n"` between reconstructs `text`
    exactly. This is what lets `scripts/redline_block_apply.py` (issue
    #564) tell an edit span that stays within one physical `<w:p>` (it can
    be written as a tracked change) from one that spans a join (proven, but
    nowhere for a PHYSICAL-paragraph writer to put the edit --
    `spans_physical_paragraph`).
    A physical paragraph whose own clean text is empty (e.g. one entirely
    inside a `<w:ins>` still pending, or a hidden-text-only paragraph)
    contributes no entry to `physical_spans` and no `"\\n"` join, same as
    it always contributed nothing to the joined text.

    `physical_p_indexes` is the IDENTITY list parallel to `physical_spans`
    (issue #621 fix round 1): entry i is the `<w:p>` position (0-based, in
    the part's preorder `w:p` numbering -- `docx_editor`'s own numbering
    minus one) that span i's clean text came from. A writer resolving a
    block back onto the live document uses this instead of scanning for the
    first `<w:p>` whose text is equal: the normalizer's clean text is
    `.strip()`ped and drops hidden text, so a paragraph can fail to match
    itself, and a forward text scan then slides onto a LATER paragraph with
    the same text and writes the edit into the wrong clause. An entry is
    None when `raw_paragraphs` was hand-built without `p_index`.

    `heading_p_index` / `heading_source_text` are the same kind of carried
    identity for the block's HEADING element (issue #645), passed through
    from `extract_document_paragraphs` unchanged: the boundary `<w:p>`'s own
    position in that preorder numbering, and the text it carries. They are
    what lets `delete_block` tell that striking a clause would leave its
    heading with nothing under it, and leave `[Intentionally omitted.]` there
    instead (issue #646). A record whose `heading_p_index` is None has no
    heading paragraph at all -- the implicit leading group, or a hand-built
    caller -- and a writer must read that as "no heading can be left empty
    here", never as an index to guess at.

    Returns:
      {"status": "normalized", "paragraphs": [...],
       "normalization_notes": "..."}   (notes key present only when one or
                                         more pending tracked changes were
                                         accepted-all -- never silent)
      or
      {"status": "unnormalizable_input",
       "analysis_report": <issue #38 artifact, docs/output-contract.md>}
    """
    fail_notes: list[str] = []
    accept_notes: list[str] = []
    clean_paragraphs: list[dict[str, Any]] = []

    for paragraph in raw_paragraphs:
        heading = paragraph.get("heading", "<untitled>")
        heading_p_index = paragraph.get("heading_p_index")
        heading_source_text = paragraph.get("heading_source_text", "")
        physical_paragraphs = paragraph.get("physical_paragraphs", [])

        clean_texts: list[str] = []
        clean_p_indexes: list[Any] = []
        paragraph_failed = False
        for physical in physical_paragraphs:
            result = normalize_input._normalize_paragraph(
                {
                    "heading": heading,
                    "text": physical.get("text", ""),
                    "revisions": physical.get("revisions", []),
                }
            )
            if not result["normalizable"]:
                fail_notes.append(result["note"])
                paragraph_failed = True
                continue
            if result["clean_text"]:
                clean_texts.append(result["clean_text"])
                clean_p_indexes.append(physical.get("p_index"))
            if result.get("note"):
                accept_notes.append(result["note"])

        if paragraph_failed:
            continue

        text = "\n".join(clean_texts)
        physical_spans: list[list[int]] = []
        pos = 0
        for clean_text in clean_texts:
            physical_spans.append([pos, pos + len(clean_text)])
            pos += len(clean_text) + 1  # +1 for the "\n" join just written

        clean_paragraphs.append(
            {
                "heading": heading,
                # Identity of the boundary `<w:p>` the heading came from, and
                # that paragraph's own pre-clean text, carried through from
                # `extract_document_paragraphs` (issue #645). Additive, like
                # `physical_spans` and `block_id` before it: no existing
                # reader of `heading`/`text` is affected. Both are
                # None/`""` for the implicit leading group and for any caller
                # that hand-built raw paragraphs without them, which a writer
                # must treat as "this block has no heading element" rather
                # than as an index to guess at.
                "heading_p_index": heading_p_index,
                "heading_source_text": heading_source_text,
                "text": text,
                "physical_spans": physical_spans,
                # Identity, parallel to `physical_spans` (issue #621 fix
                # round 1): the `<w:p>`'s own 0-based position in the part's
                # preorder `w:p` numbering, carried through from
                # `extract_document_paragraphs`. An entry is None when the
                # caller hand-built raw paragraphs without `p_index`, which a
                # writer must treat as "no identity" and fail closed on
                # rather than fall back to matching text.
                "physical_p_indexes": clean_p_indexes,
                # Immutable, code-assigned block id (issue #619). 1-based
                # document order over LOGICAL paragraphs, zero-padded to
                # four digits. Assigned exactly once per review, from the
                # materialized bytes; never reused, never renumbered.
                "block_id": _block_id(len(clean_paragraphs) + 1),
            }
        )

    if fail_notes:
        normalize_result = {
            "normalizable": False,
            "normalization_notes": " ".join(fail_notes),
        }
        return {
            "status": "unnormalizable_input",
            "analysis_report": normalize_input.build_unnormalizable_report(normalize_result),
        }

    out: dict[str, Any] = {"status": "normalized", "paragraphs": clean_paragraphs}
    if accept_notes:
        out["normalization_notes"] = " ".join(accept_notes)
    return out


def extract_and_normalize(docx_bytes: bytes) -> dict[str, Any]:
    """Full stage: allowlisted OOXML extraction, then the documented
    normalization rule. See `extract_document_paragraphs` and
    `normalize_paragraphs`."""
    raw_paragraphs = extract_document_paragraphs(docx_bytes)
    return normalize_paragraphs(raw_paragraphs)


# ---------------------------------------------------------------------------
# Pointer-only pipeline-stage entry point (issue #19 convention)
# ---------------------------------------------------------------------------


def run_stage(
    event: dict[str, Any],
    *,
    fetch_docx_bytes: Callable[[str], bytes],
    store_json: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    """
    Step Functions task-shaped entry point. POINTER-ONLY PAYLOAD RULE
    (issue #19, matching infra/lambda/mock_review/handler.py): `event` and
    the returned dict carry `review_id` / `owner_sub` / S3 keys / status /
    reason only -- never document text, so nothing substantive ever passes
    through Step Functions execution history.

    `fetch_docx_bytes(s3_key) -> bytes` and
    `store_json(s3_key, obj) -> None` are injected so this stage runs fully
    offline in tests (moto/fakes) without a hard boto3 dependency here; a
    real S3-backed Lambda handler wiring this in is a follow-up, not part
    of this pure-Python slice.

    Input event shape:
      {"review_id": ..., "owner_sub": ..., "upload_s3_key": "uploads/..."}

    Output (success):
      {"review_id": ..., "status": "EXTRACTED", "normalized_s3_key": "intermediate/..."}

    Output (fail-closed, issue #38):
      {"review_id": ..., "status": "MANUAL_REVIEW_REQUIRED",
       "reason": "unnormalizable_input", "analysis_report_s3_key": "outputs/<review_id>/analysis-report.json"}
    """
    review_id = event["review_id"]
    owner_sub = event.get("owner_sub", "")
    upload_key = event["upload_s3_key"]

    docx_bytes = fetch_docx_bytes(upload_key)
    result = extract_and_normalize(docx_bytes)

    if result["status"] == "unnormalizable_input":
        # Scoped to exactly ``outputs/<review_id>/`` so the report file is
        # downloadable through backend/src/download.py, whose
        # _validate_s3_key_bound_to_review rejects any output key carrying an
        # owner_sub segment (issue #71 AC2). The normalized-intermediate key
        # below is NOT downloaded through that path, so it keeps its
        # owner-partitioned prefix.
        report_key = f"outputs/{review_id}/analysis-report.json"
        store_json(report_key, result["analysis_report"])
        return {
            "review_id": review_id,
            "status": "MANUAL_REVIEW_REQUIRED",
            "reason": "unnormalizable_input",
            "analysis_report_s3_key": report_key,
        }

    normalized_key = f"intermediate/{owner_sub}/{review_id}/normalized.json"
    payload: dict[str, Any] = {"paragraphs": result["paragraphs"]}
    if "normalization_notes" in result:
        payload["normalization_notes"] = result["normalization_notes"]
    store_json(normalized_key, payload)

    return {
        "review_id": review_id,
        "status": "EXTRACTED",
        "normalized_s3_key": normalized_key,
    }


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test over the module's own source-tree fixture, if present."""
    fixture = SCRIPTS_DIR.parent / "tests" / "fixtures" / "extraction_normalization_80" / "clean-standard-form.SYNTHETIC.docx"
    if not fixture.exists():
        print(f"No smoke fixture at {fixture}; nothing to do.")
        return
    result = extract_and_normalize(fixture.read_bytes())
    print(f"status={result['status']}")
    if result["status"] == "normalized":
        print(f"{len(result['paragraphs'])} paragraph(s) extracted")


if __name__ == "__main__":
    main()
    sys.exit(0)
