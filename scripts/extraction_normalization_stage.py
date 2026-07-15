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
     `w:tr`/`w:tc`, `w:r`, `w:ins`, `w:del`, `w:fldSimple`) -- it is not a
     generic "recurse into every child" walk, so an unrecognized wrapper tag
     (`w:drawing`, `w:sdt`, `mc:AlternateContent`, ...) is simply never
     descended into. Image alt text (`wp:docPr/@descr`, `@title`) is an XML
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
     `scripts/diff_standard_form.py`'s `diff_draft_against_standard()`
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
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import clause_boundaries  # noqa: E402
import normalize_input  # noqa: E402

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
        # a new one. This makes "more than one cluster on a paragraph"
        # exactly normalize_input.py's rule-1 ambiguity ("more than one
        # pending tracked_change on the same paragraph"), whether the
        # clusters share an author or not; two different authors editing
        # back-to-back with no intervening plain text (rule 2, multi-author)
        # must still surface as two distinct clusters, not silently merge
        # into the first author seen.
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
    `w:pict`, bookmarks, proofing marks, ...) is skipped WITHOUT recursion --
    this is what keeps textbox bodies, content-control placeholder bodies,
    and any other non-allowlisted nested content out of the output by
    construction rather than by an after-the-fact filter."""
    for el in elements:
        tag = el.tag
        if tag == _w("r"):
            _process_run(el, builder, mode, author, inside_field_code)
        elif tag == _w("ins"):
            ins_author = el.get(_w("author")) or author
            _walk_content(list(el), builder, mode="ins", author=ins_author, inside_field_code=inside_field_code)
        elif tag == _w("del"):
            del_author = el.get(_w("author")) or author
            _walk_content(list(el), builder, mode="del", author=del_author, inside_field_code=inside_field_code)
        elif tag == _w("fldSimple"):
            _process_fld_simple(el, builder)
        else:
            continue


def _build_paragraph_record(p_el: ET.Element) -> dict[str, Any]:
    """Extracts one raw `<w:p>` into `{"text", "revisions"}` (no `heading`
    key -- heading-vs-body grouping happens one level up, in
    `extract_document_paragraphs`, the same convention
    `scripts/diff_standard_form.py`'s real-docx loader uses for the
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

    return {"text": original_text, "revisions": revisions}


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
    same rule `scripts/diff_standard_form.py`'s real-docx loader uses for
    the canonical standard form; when no Heading style is present (real
    counterparty drafts routinely lose named heading styles), a
    document-signals fallback (numbered/lettered lead-ins, outline level,
    bold single-line paragraphs, ALL-CAPS short lines) starts one instead --
    this is the DRAFT side, which `clause_boundaries`'s module docstring
    documents as allowed to relax the style-only rule; the canonical
    standard-form loaders keep requiring proper Heading styles, unchanged.
    Subsequent non-boundary `w:p`s are siblings of the most recent boundary
    until the next one.

      [{"heading": "...", "physical_paragraphs": [{"text": "...",
        "revisions": [...]}, ...]}, ...]

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

    logical: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def _flush() -> None:
        if current is not None:
            logical.append(
                {
                    "heading": current["heading"],
                    "physical_paragraphs": current["physical_paragraphs"],
                }
            )

    for p_el in _iter_body_paragraphs(body):
        record = _build_paragraph_record(p_el)
        if not record["text"] and not record["revisions"]:
            continue  # empty paragraph (spacer) -- nothing to extract

        if clause_boundaries.is_boundary_paragraph_ooxml(p_el, record["text"]):
            _flush()
            heading_text = clause_boundaries.clean_heading_text(record["text"])
            current = {
                "heading": heading_text or "<untitled>",
                "physical_paragraphs": [],
            }
        else:
            if current is None:
                current = {"heading": "<untitled>", "physical_paragraphs": []}
            # Kept as its own physical-paragraph record -- see this
            # function's docstring for why siblings must not be merged
            # before normalization.
            current["physical_paragraphs"].append(
                {"text": record["text"], "revisions": record["revisions"]}
            )

    _flush()
    return logical


# ---------------------------------------------------------------------------
# Normalization (delegates the documented rule to normalize_input.py)
# ---------------------------------------------------------------------------


def normalize_paragraphs(raw_paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Applies `scripts/normalize_input.py`'s documented per-paragraph
    accept/reject rule (`_normalize_paragraph` -- the exact function that
    module's docstring names this stage as the caller of) to each raw
    extracted paragraph.

    Unlike `normalize_input.normalize()`, this does NOT flatten the result
    into one joined `clean_body` string -- it returns a structured
    paragraph list, `[{"heading": ..., "text": ...}, ...]`, matching the
    `scripts/diff_standard_form.py` / `backend/src/corpus.py` draft-input
    contract (see module docstring), so each paragraph stays independently
    anchorable downstream.

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
    clean_paragraphs: list[dict[str, str]] = []

    for paragraph in raw_paragraphs:
        heading = paragraph.get("heading", "<untitled>")
        physical_paragraphs = paragraph.get("physical_paragraphs", [])

        clean_texts: list[str] = []
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
            if result.get("note"):
                accept_notes.append(result["note"])

        if paragraph_failed:
            continue

        clean_paragraphs.append({"heading": heading, "text": " ".join(clean_texts).strip()})

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
