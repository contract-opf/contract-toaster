#!/usr/bin/env python3
"""
Redline generation — issue #83: wires the reconciled issue list (#82) into
the tracked-changes docx writer end-to-end. Issue #379 wires the
QUOTE-BASED patcher (`scripts/redline_quote_apply.py::apply_quote_patches`,
issue #377) into the REQUEST_CHANGE path, replacing the anchor/hash-joined
patch path issue #380 retired.

Implements ARCHITECTURE.md -> "Redlining" and docs/output-contract.md's
fail-closed / marker / leakage-scan / output-OOXML-scan rules as a single
pure orchestration function, `generate_redline()`. This module owns no I/O
of its own (no S3, no DynamoDB) -- it takes the reconciled review result
and the current draft's normalized docx bytes, and returns a result dict a
caller persists. That keeps the same pure-logic/I/O separation as every
other module in this pipeline (`redline_quote_apply.py`, `leakage_scan.py`,
`reconciliation.py`).

## Pipeline (in order -- every gate is fail-closed, never a sanitized
## partial result)

1. **Leakage scan gates generation AND the ACCEPT summary** (issue #26).
   `leakage_scan.run_leakage_gate()` runs over the FULL reconciled result
   (`verdict_summary`, every issue field, `critic_delta`) before anything
   else -- a positive detection routes straight to
   `ERROR_MANUAL_REVIEW_REQUIRED` with no document produced, on *either*
   the ACCEPT or the REQUEST_CHANGE path (docs/output-contract.md ->
   "Leakage scan scope").
2. **ACCEPT path produces no document.** Per docs/output-contract.md ->
   "ACCEPT summary shape", the ACCEPT result is `verdict_summary` prose
   only (already leakage-scanned in step 1) -- there is nothing to redline.
3. **REQUEST_CHANGE path -- quote-based patching (issue #379).**
   `_issues_to_quote_patches()` builds a `{source_quote, new_text,
   rationale}` patch (`redline_quote_apply.apply_quote_patches`'s own input
   shape) for every issue that carries a `proposed_replacement_text`. A
   TRUE flag-only issue (`proposed_replacement_text == ""` -- the model
   proposed no replacement at all, issue #260) is excluded here, before
   `apply_quote_patches` is ever called, so it is never touched in the
   delivered document (never a bare `<w:del>` with no `<w:ins>`). An issue
   whose `source_quote` is missing/empty (the model legitimately omits it
   when no single contiguous span exists to name --
   `playbooks/output-schema-v2.json`) is still handed through as
   `source_quote=""`; `quote_locate.locate_quote_in_paragraphs()` fails
   that lookup safe to `"not_found"` rather than matching everything, so it
   lands in the ordinary flag-only bucket alongside any other unlocatable
   quote -- no detector-style special case (issue #379's title: "uniform,
   no detector exception").
4. **Quote-based patch application** (issue #377):
   `redline_quote_apply.apply_quote_patches()` locates each patch's
   `source_quote` in the uploaded package (issue #375's whitespace-
   tolerant, uniqueness-checked locator) and applies it as a genuine Word
   tracked change (`<w:ins>`/`<w:del>` around just the quoted SPAN, not the
   whole paragraph -- unlike the retired anchor path) via `docx-editor`. It
   already reuses THIS module's `inject_export_marker_and_footnotes()`
   (export marker plus a footnote per applied patch) and
   `verify_docx_round_trip()` internally -- see that module's own
   docstring. A patch it cannot safely locate or apply (`not_found` /
   `ambiguous` / an internal round-trip failure) never blocks any OTHER
   patch in the same batch -- a per-patch fail-safe, never a
   whole-document failure.
5. **Mapping `{applied, flag_only}` to a result** (issue #379 Scope item
   2): `status="OK"` with the delivered `docx_bytes` whenever at least one
   patch applied -- a batch with some flag-only patches alongside is still
   `OK`, PARTIAL delivery, never fail-closed-whole-doc; the flag-only
   entries are surfaced in `analysis_report.changes_not_applied` for the
   attorney to apply by hand, alongside (never instead of) the partial
   redline. `status="MANUAL_REVIEW_REQUIRED"` only when EVERY attempted
   patch failed to locate/apply (zero applied) -- distinct from "no
   patches were even attempted" (every issue flag-only), which is a clean
   `OK` with `docx_bytes=None` (nothing to redline, not a failure --
   mirrors `third_party_output_integration
   .generate_third_party_review_output`'s identical "no patches to apply"
   branch). See `generate_redline()`'s own docstring for the full result
   shape.
6. **Output OOXML scan** (docs/threat-model.md -> "Generated redline
   output hygiene"): the assembled `.docx` is subjected to the SAME
   external-relationship / embedded-object / macro-template scan as an
   uploaded input document (`backend/src/upload_validation.py` stage 7,
   reused directly here rather than re-implemented, so the two directions
   can never drift), plus a field-code/hyperlink structural check specific
   to output hygiene. A positive detection routes to
   `ERROR_MANUAL_REVIEW_REQUIRED` and the document is NOT written anywhere.
7. **Word round-trip check**: re-verified on the FINAL assembled bytes
   (defense-in-depth alongside `apply_quote_patches`'s own internal check,
   reusing the SAME `verify_docx_round_trip` function so the two calls can
   never drift) before ever being handed to a caller -- a document that
   fails to open is never delivered. A failure here routes to
   `ERROR_MANUAL_REVIEW_REQUIRED` (`reason="round_trip_verification_failed"`),
   fail-closed like every other gate above -- never an uncaught exception
   (issue #263).

MOCKED-MODEL slice (owner-approved, issues #81/#82/#83): this module has no
model-invocation dependency of its own -- it consumes `reconciled_result`,
the already-reconciled `output-schema-v1`-shaped dict `reconciliation.py`
produces. No live Bedrock, no network.
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import leakage_scan  # noqa: E402
import redline_docx_writer  # noqa: E402
import redline_inplace  # noqa: E402
import redline_quote_apply  # noqa: E402
import upload_validation  # noqa: E402

ERROR_MANUAL_REVIEW_REQUIRED = "ERROR_MANUAL_REVIEW_REQUIRED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

# Issue #379: the single umbrella `reason` for the REQUEST_CHANGE path's
# MANUAL_REVIEW_REQUIRED outcome (every attempted quote patch failed to
# locate/apply -- zero applied). Deliberately ONE stable string rather than
# echoing whichever of `redline_quote_apply`'s per-patch reasons
# (`not_found` / `ambiguous` / `round_trip_verification_failed`) happened to
# be involved -- a single batch can legitimately mix reasons across its
# failed patches, and a caller/UI switching on the top-level `reason` needs
# a small, stable enum; the per-patch detail lives in
# `analysis_report.changes_not_applied[].reason` (see
# `_build_quote_analysis_report`).
REASON_QUOTE_PATCHES_NOT_APPLIED = "quote_patches_not_applied"

# NOTE: this module never imports `redline_patch`. The anchor/hash-joined
# patch path that used to live here (redline_patch.join_patches_from_diff/
# apply_patches) was retired by issue #380 alongside the deterministic
# detector engine and the standard-form diff that fed it.
# scripts/redline_patch.py itself is untouched and still used by
# scripts/third_party_output_integration.py and scripts/eval_harness.py --
# only THIS module's use of it is gone, replaced by the quote-based
# `redline_quote_apply.apply_quote_patches` (issue #379, imported above).

WORD_NS = redline_docx_writer.WORD_NS
REL_NS = redline_docx_writer.REL_NS
XML_NS = redline_docx_writer.XML_NS
PKG_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

# OOXML constructs that would mean model text was serialized as document
# STRUCTURE rather than DATA (docs/output-contract.md -> "Literal-runs-only
# insertion"). This module's own writer never emits these -- it only ever
# creates <w:t>/<w:delText> text nodes -- so this check exists as the
# structural proof of that guarantee (and the regression catch if a future
# writer change ever stops being literal-runs-only).
_FIELD_CODE_TAGS = ("fldChar", "instrText", "fldSimple", "hyperlink")


class OutputScanError(Exception):
    """Raised by `run_output_ooxml_scan` on any positive detection.

    Carries a stable `reason_code` (mirrors
    `backend.src.upload_validation.HostileFileError`'s convention) so
    callers/tests can assert on the failure class without pattern-matching
    prose.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


def _check_no_field_codes(zf: zipfile.ZipFile) -> None:
    """Defense-in-depth structural check: no `<w:fldChar>`, `<w:instrText>`,
    `<w:fldSimple>`, or `<w:hyperlink>` element exists anywhere in a `word/*.xml`
    part of the generated document. Model-generated text (`proposed_replacement_text`,
    `external_rationale_for_footnote`) reaches this document only as literal
    `<w:t>`/`<w:delText>` runs (`redline_docx_writer.py`'s only text-insertion
    path), so hostile replacement text containing field syntax (e.g.
    `{ HYPERLINK "https://attacker.example" }`) lands as inert literal
    characters, never as parsed document structure -- this check verifies
    that guarantee held for the specific bytes just assembled."""
    for name in zf.namelist():
        if not (name.startswith("word/") and name.endswith(".xml")):
            continue
        xml_bytes = zf.read(name)
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise OutputScanError(
                "malformed_output_part", f"{name} did not parse: {exc}"
            ) from exc
        for tag in _FIELD_CODE_TAGS:
            if root.findall(f".//{{{WORD_NS}}}{tag}"):
                raise OutputScanError(
                    "field_code_in_output",
                    f"{name} contains a <w:{tag}> element -- model text "
                    "must be inserted as literal runs only.",
                )


def run_output_ooxml_scan(docx_bytes: bytes) -> None:
    """Subject a just-assembled redline `.docx` to the same
    external-relationship / embedded-object / macro-template scan as an
    uploaded input document (docs/threat-model.md -> "Generated redline
    output hygiene (output OOXML scan)"). Reuses
    `backend.src.upload_validation._check_no_macro_enabled_parts` /
    `_check_relationships` directly -- the same pipeline step adapted for
    the output direction, per the spec, not a re-implementation that could
    drift from the input-side gauntlet -- plus `_check_no_field_codes`
    (this module) for the field-code/hyperlink structural check.

    Raises `OutputScanError` on any positive detection; never returns a
    sanitized document. Runs AFTER the leakage scan, never instead of it
    (docs/output-contract.md -> "Literal-runs-only insertion and output
    OOXML scan").
    """
    buf = io.BytesIO(docx_bytes)
    with zipfile.ZipFile(buf) as zf:
        try:
            upload_validation._check_no_macro_enabled_parts(zf)
            upload_validation._check_relationships(zf)
        except upload_validation.HostileFileError as exc:
            raise OutputScanError(exc.reason_code, exc.detail) from exc
        _check_no_field_codes(zf)


def verify_docx_round_trip(docx_bytes: bytes) -> None:
    """The writer's own output must open cleanly before it is ever
    delivered: a valid ZIP whose `word/document.xml` (and every other
    `.xml`/`.rels` part actually present) parses as well-formed XML.
    Raises ValueError on any failure -- required verification item 5
    ("a Word round-trip check -- the docx writer opens its own output
    cleanly")."""
    buf = io.BytesIO(docx_bytes)
    if not zipfile.is_zipfile(buf):
        raise ValueError("generated docx is not a valid ZIP archive")
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        if "word/document.xml" not in names:
            raise ValueError("generated docx is missing word/document.xml")
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                try:
                    ET.fromstring(zf.read(name))
                except ET.ParseError as exc:
                    raise ValueError(f"{name} did not parse: {exc}") from exc


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _r(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _pkg(tag: str) -> str:
    return f"{{{PKG_RELS_NS}}}{tag}"


def _ct(tag: str) -> str:
    return f"{{{CT_NS}}}{tag}"


def _max_rel_id(rels_root: ET.Element) -> int:
    """Highest numeric suffix among a `.rels` part's existing `Id="rIdN"`
    attributes (0 if none) -- new relationship ids for the marker/footnote
    parts this function may add start one past this, so they never collide
    with a relationship an uploaded document already carries (e.g. to its
    own `styles.xml`, `theme1.xml`, ...)."""
    max_id = 0
    for rel in rels_root.findall(_pkg("Relationship")):
        rid = rel.get("Id", "")
        if rid.startswith("rId"):
            try:
                max_id = max(max_id, int(rid[3:]))
            except ValueError:
                continue
    return max_id


def _max_footnote_id(footnotes_root: ET.Element) -> int:
    """Highest `w:id` among an existing `word/footnotes.xml`'s `<w:footnote>`
    elements (0 if none, or only the special -1/0 separator footnotes) --
    reused by `_compute_new_footnote_entries` to offset new ids past
    whatever an uploaded, already-footnoted document carries (issue #291
    scope item 3: 'extend with ids offset past the existing max')."""
    max_id = 0
    for fn in footnotes_root.findall(_w("footnote")):
        try:
            max_id = max(max_id, int(fn.get(_w("id"), "0")))
        except ValueError:
            continue
    return max_id


def _find_patched_paragraph(body: ET.Element, source_text: str) -> Optional[ET.Element]:
    """Locate the paragraph `redline_inplace.apply_tracked_changes_inplace`
    just rewrote for one applied patch, by its now-unique `<w:del>` delText.

    Compared STRIPPED on both sides (issue #291 review, second pass), for
    the same reason `redline_inplace.apply_tracked_changes_inplace` itself
    locates the target paragraph by stripped comparison (issue #291 review
    finding 1): `source_text` here is the caller's NORMALIZED/stripped hunk
    text, while the `<w:delText>` the patcher wrote is the paragraph's
    ACTUAL raw text (edge whitespace included -- see `redline_inplace.py`'s
    `apply_tracked_changes_inplace`, `actual_source_text`). Comparing raw
    delText to stripped source_text unstripped would never match for any
    paragraph whose runs carry leading/trailing whitespace, silently
    dropping the `<w:footnoteReference>` injection for that class of
    paragraph even though the patch itself applied cleanly. Safe by
    construction: the in-place patcher only ever applies a patch whose
    STRIPPED `source_text` matched EXACTLY ONE body paragraph's STRIPPED
    text (two-or-more matches fail closed as 'ambiguous' and are never
    rewritten), so this stripped delText can never collide with a
    different paragraph either."""
    normalized_source = (source_text or "").strip()
    for p in body:
        if p.tag != _w("p"):
            continue
        del_el = p.find(_w("del"))
        if del_el is None:
            continue
        del_text = "".join(t.text or "" for t in del_el.iter(_w("delText")))
        if del_text.strip() == normalized_source:
            return p
    return None


def _find_sect_pr(body: ET.Element) -> ET.Element:
    """The `<w:sectPr>` a new header/footer reference should be wired into:
    a direct child of `<w:body>` (the common, single-section case) or, for a
    multi-section document, nested inside the LAST paragraph's `<w:pPr>`. If
    neither exists (unusual), one is created so the marker still has
    somewhere to attach."""
    direct = body.find(_w("sectPr"))
    if direct is not None:
        return direct
    paragraphs = [c for c in body if c.tag == _w("p")]
    if paragraphs:
        ppr = paragraphs[-1].find(_w("pPr"))
        if ppr is not None:
            nested = ppr.find(_w("sectPr"))
            if nested is not None:
                return nested
    return ET.SubElement(body, _w("sectPr"))


def _append_marker_paragraph(part_bytes: bytes, marker_text: str) -> bytes:
    """Append one marker paragraph to an EXISTING `word/header1.xml` or
    `word/footer1.xml` part (issue #291 scope item 2: 'if headers exist,
    append the marker paragraph to them') -- the part's own existing content
    is otherwise untouched."""
    root = ET.fromstring(part_bytes)
    p = ET.SubElement(root, _w("p"))
    run = ET.SubElement(p, _w("r"))
    text_el = ET.SubElement(run, _w("t"))
    text_el.set(f"{{{XML_NS}}}space", "preserve")
    text_el.text = marker_text
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(root, encoding="unicode").encode("utf-8")
    )


def _compute_new_footnote_entries(
    inplace_applied_patches: list[dict[str, Any]],
    footnote_text_by_anchor: dict[str, Any],
    next_footnote_id: int,
) -> list[dict[str, Any]]:
    """Deterministic anchor -> footnote-id/text assignment, in
    `inplace_applied_patches` order, skipping any anchor with no footnote
    text -- the in-place-package analogue of
    `redline_docx_writer._compute_footnotes`, starting numbering at
    `next_footnote_id` (1 for a package with no pre-existing footnotes, or
    one past the existing max for a package that already has some)."""
    entries = []
    for patch in inplace_applied_patches:
        text = footnote_text_by_anchor.get(patch["anchor"])
        if text:
            entries.append(
                {"id": next_footnote_id, "anchor": patch["anchor"], "text": text}
            )
            next_footnote_id += 1
    return entries


def inject_export_marker_and_footnotes(
    docx_bytes: bytes,
    inplace_applied_patches: list[dict[str, Any]],
    footnote_text_by_anchor: dict[str, Any],
    *,
    marker_text: str = redline_docx_writer.MARKER_TEXT,
) -> bytes:
    """Issue #291 scope items 2-3: inject the export marker (header/footer)
    and footnoted rationales into an ALREADY in-place-patched package (the
    output of `redline_inplace.apply_tracked_changes_inplace`).

    Every zip entry the in-place patcher didn't touch is preserved as-is.
    This function only ever ADDS to `[Content_Types].xml` and
    `word/_rels/document.xml.rels` -- it never replaces either wholesale, so
    an uploaded document's own existing declarations (`styles.xml`,
    `theme1.xml`, ...) survive untouched. `word/header1.xml`,
    `word/footer1.xml`, and `word/footnotes.xml` are created fresh (reusing
    `redline_docx_writer`'s part builders verbatim) only when the uploaded
    package doesn't already carry them; when it does, the marker paragraph
    or footnote entries are appended to the EXISTING part instead.

    `inplace_applied_patches` is the `{"anchor", "source_text", "new_text"}`
    list, filtered to just the anchors `InplaceResult.applied` reports --
    used both to locate each patched paragraph (by its now-unique `<w:del>`
    delText) and to assign footnote ids in deterministic order, exactly like
    `redline_docx_writer._compute_footnotes`.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}
    names = set(originals.keys())

    # ---- word/document.xml: parse with the same root-namespace-preservation
    # technique redline_inplace.py uses (see that module's docstring,
    # "Preserve") -- this is a SECOND rewrite pass over document.xml, after
    # the in-place patcher's own pass, so the same care applies.
    doc_xml_text = originals[redline_inplace.DOCUMENT_PART].decode("utf-8")
    original_root_open_tag = redline_inplace._root_open_tag(doc_xml_text)
    for prefix, uri in redline_inplace._declared_namespaces(original_root_open_tag):
        if prefix:
            ET.register_namespace(prefix, uri)
    doc_root = ET.fromstring(originals[redline_inplace.DOCUMENT_PART])
    body = doc_root.find(_w("body"))

    # ---- Footnotes: compute the id assignment before touching any XML, so
    # a patch batch with nothing to footnote is a true no-op.
    have_footnotes = "word/footnotes.xml" in names
    if have_footnotes:
        footnotes_root = ET.fromstring(originals["word/footnotes.xml"])
        next_footnote_id = _max_footnote_id(footnotes_root) + 1
    else:
        footnotes_root = None
        next_footnote_id = 1

    footnote_entries = _compute_new_footnote_entries(
        inplace_applied_patches, footnote_text_by_anchor, next_footnote_id
    )
    source_text_by_anchor = {p["anchor"]: p["source_text"] for p in inplace_applied_patches}

    # ---- Wire a <w:footnoteReference> run into each footnoted patch's
    # <w:ins> (issue #291 scope item 3: "append a footnote reference run
    # inside that patch's <w:ins>").
    for entry in footnote_entries:
        target_p = _find_patched_paragraph(body, source_text_by_anchor[entry["anchor"]])
        if target_p is None:  # pragma: no cover - defensive; can't happen given the caller contract
            continue
        ins_el = target_p.find(_w("ins"))
        if ins_el is None:  # pragma: no cover - defensive
            continue
        ref_run = ET.SubElement(ins_el, _w("r"))
        ref = ET.SubElement(ref_run, _w("footnoteReference"))
        ref.set(_w("id"), str(entry["id"]))

    # ---- word/_rels/document.xml.rels: merge in, never replace.
    if "word/_rels/document.xml.rels" in names:
        rels_root = ET.fromstring(originals["word/_rels/document.xml.rels"])
    else:
        rels_root = ET.Element(_pkg("Relationships"))
    next_rid = _max_rel_id(rels_root) + 1

    have_header = "word/header1.xml" in names
    have_footer = "word/footer1.xml" in names

    new_header_rid = None
    new_footer_rid = None
    new_footnotes_rid = None

    if not have_header:
        new_header_rid = f"rId{next_rid}"
        next_rid += 1
        rel = ET.SubElement(rels_root, _pkg("Relationship"))
        rel.set("Id", new_header_rid)
        rel.set("Type", redline_docx_writer.HEADER_REL_TYPE)
        rel.set("Target", "header1.xml")
    if not have_footer:
        new_footer_rid = f"rId{next_rid}"
        next_rid += 1
        rel = ET.SubElement(rels_root, _pkg("Relationship"))
        rel.set("Id", new_footer_rid)
        rel.set("Type", redline_docx_writer.FOOTER_REL_TYPE)
        rel.set("Target", "footer1.xml")
    if footnote_entries and not have_footnotes:
        new_footnotes_rid = f"rId{next_rid}"
        next_rid += 1
        rel = ET.SubElement(rels_root, _pkg("Relationship"))
        rel.set("Id", new_footnotes_rid)
        rel.set("Type", redline_docx_writer.FOOTNOTES_REL_TYPE)
        rel.set("Target", "footnotes.xml")

    # ---- <w:sectPr>: wire header/footer references only for NEWLY created
    # parts -- a package that already carried a marker keeps whatever
    # wiring it already has.
    if new_header_rid or new_footer_rid:
        sect_pr = _find_sect_pr(body)
        # headerReference/footerReference must precede sectPr's other
        # children (pgSz, pgMar, ...) per the CT_SectPr schema order.
        if new_footer_rid:
            fref = ET.Element(_w("footerReference"))
            fref.set(_w("type"), "default")
            fref.set(_r("id"), new_footer_rid)
            sect_pr.insert(0, fref)
        if new_header_rid:
            href = ET.Element(_w("headerReference"))
            href.set(_w("type"), "default")
            href.set(_r("id"), new_header_rid)
            sect_pr.insert(0, href)

    # A <w:headerReference>/<w:footerReference> we just added carries an
    # `r:id` attribute -- if the ORIGINAL root open tag (spliced back in
    # verbatim below, see redline_inplace.py's "Preserve") never declared
    # the relationships namespace at all (a document with no existing
    # r:-prefixed attribute never needed to), splicing it back unmodified
    # would leave `r:id` an unbound-prefix parse error. Add the declaration
    # only in that case -- never touch a root tag that already has it.
    if new_header_rid or new_footer_rid:
        already_declared = any(
            uri == REL_NS
            for _prefix, uri in redline_inplace._declared_namespaces(original_root_open_tag)
        )
        if not already_declared:
            original_root_open_tag = (
                original_root_open_tag[:-1] + f' xmlns:r="{REL_NS}">'
            )

    # ---- Re-serialize document.xml, splicing the ORIGINAL root open tag
    # back in verbatim (same technique as redline_inplace.py).
    serialized = ET.tostring(doc_root, encoding="unicode")
    auto_root_open_tag = redline_inplace._root_open_tag(serialized)
    body_and_close = serialized[len(auto_root_open_tag):]
    new_document_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + original_root_open_tag.encode("utf-8")
        + body_and_close.encode("utf-8")
    )

    new_parts: dict[str, bytes] = {redline_inplace.DOCUMENT_PART: new_document_xml}

    # ---- word/header1.xml / word/footer1.xml
    if new_header_rid:
        new_parts["word/header1.xml"] = redline_docx_writer.build_header_xml(marker_text)
    elif have_header:
        new_parts["word/header1.xml"] = _append_marker_paragraph(
            originals["word/header1.xml"], marker_text
        )
    if new_footer_rid:
        new_parts["word/footer1.xml"] = redline_docx_writer.build_footer_xml(marker_text)
    elif have_footer:
        new_parts["word/footer1.xml"] = _append_marker_paragraph(
            originals["word/footer1.xml"], marker_text
        )

    # ---- word/footnotes.xml
    if footnote_entries:
        if have_footnotes:
            for entry in footnote_entries:
                fn = ET.SubElement(footnotes_root, _w("footnote"))
                fn.set(_w("id"), str(entry["id"]))
                p = ET.SubElement(fn, _w("p"))
                ref_run = ET.SubElement(p, _w("r"))
                ET.SubElement(ref_run, _w("footnoteRef"))
                text_run = ET.SubElement(p, _w("r"))
                text_el = ET.SubElement(text_run, _w("t"))
                text_el.set(f"{{{XML_NS}}}space", "preserve")
                text_el.text = " " + entry["text"]
            new_parts["word/footnotes.xml"] = (
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                + ET.tostring(footnotes_root, encoding="unicode").encode("utf-8")
            )
        else:
            new_parts["word/footnotes.xml"] = redline_docx_writer.build_footnotes_xml(
                footnote_entries
            )

    # ---- word/_rels/document.xml.rels (only rewritten if it actually changed)
    if new_header_rid or new_footer_rid or new_footnotes_rid:
        new_parts["word/_rels/document.xml.rels"] = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(rels_root, encoding="unicode").encode("utf-8")
        )

    # ---- [Content_Types].xml: append Overrides for whatever parts are new
    # this pass -- never replace the original wholesale (a real upload's
    # content-types already declares styles.xml, settings.xml, etc.).
    new_ct_parts = []
    if new_header_rid:
        new_ct_parts.append(("/word/header1.xml", redline_docx_writer.HEADER_CONTENT_TYPE))
    if new_footer_rid:
        new_ct_parts.append(("/word/footer1.xml", redline_docx_writer.FOOTER_CONTENT_TYPE))
    if new_footnotes_rid:
        new_ct_parts.append(("/word/footnotes.xml", redline_docx_writer.FOOTNOTES_CONTENT_TYPE))
    if new_ct_parts:
        ct_root = ET.fromstring(originals["[Content_Types].xml"])
        for part_name, content_type in new_ct_parts:
            override = ET.SubElement(ct_root, _ct("Override"))
            override.set("PartName", part_name)
            override.set("ContentType", content_type)
        new_parts["[Content_Types].xml"] = (
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(ct_root, encoding="unicode").encode("utf-8")
        )

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        written = set()
        for info in infos:
            data = new_parts.get(info.filename, originals[info.filename])
            zf_out.writestr(info, data)
            written.add(info.filename)
        # Parts that did not exist in the original package at all (header/
        # footer/footnotes on an upload that had none) need a fresh ZipInfo.
        for name, data in new_parts.items():
            if name not in written:
                zf_out.writestr(name, data)

    return out_buf.getvalue()


def _issues_to_quote_patches(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Builds `redline_quote_apply.apply_quote_patches`'s patch shape
    (`{source_quote, new_text, rationale}`) from a reconciled REQUEST_CHANGE
    result's `issues` list (issue #379 Scope item 1).

    A TRUE flag-only issue (`proposed_replacement_text == ""` -- the model
    proposed no replacement at all, per docs/output-contract.md's
    `mode='none'` convention, issue #260) is excluded here, before
    `apply_quote_patches` is ever called: that function raises `ValueError`
    on an empty `new_text`, and this module's contract (matching
    `redline_inplace.apply_tracked_changes_inplace`'s identical convention)
    is that a flag-only issue is never touched in the delivered document.

    An issue's `source_quote` is passed through even when missing/empty
    (`issue.get("source_quote") or ""`, never a raised `KeyError`) -- the
    model legitimately omits it when no single contiguous span exists to
    name (`playbooks/output-schema-v2.json`); `apply_quote_patches`'s own
    locate step fails an empty quote safe to `"not_found"` rather than
    matching everything, so this issue lands in the ordinary flag-only
    bucket alongside any other unlocatable quote (issue #379's title:
    "uniform, no detector exception" -- there is no special case here).

    Each patch also carries a private `_source_issue` key (the ORIGINAL
    issue dict, by reference) so `_build_quote_analysis_report` can recover
    `section_ref` / `section_title` / `counterparty_change_summary` for
    whichever patches come back flag-only. `apply_quote_patches` reads only
    `source_quote` / `new_text` / `rationale` by name and passes every other
    key straight through its own `dict(patch, ...)` copies untouched
    (confirmed by reading that module's source), so this rides along inertly
    and is read back out (never copied wholesale) by
    `_build_quote_analysis_report` -- it never reaches the docx or any
    public return value.
    """
    patches = []
    for issue in issues:
        new_text = issue.get("proposed_replacement_text") or ""
        if not new_text:
            continue
        patches.append(
            {
                "source_quote": issue.get("source_quote") or "",
                "new_text": new_text,
                "rationale": issue.get("external_rationale_for_footnote"),
                "_source_issue": issue,
            }
        )
    return patches


def _build_quote_analysis_report(flag_only: list[dict[str, Any]]) -> dict[str, Any]:
    """Builds the `analysis_report` artifact (docs/output-contract.md ->
    "Fail-closed internal analysis report" -> "Format") for the patches
    `redline_quote_apply.apply_quote_patches` could not locate/apply --
    issue #379 Scope item 2's quote-path analog of
    `redline_patch.build_analysis_report` (not reused directly: that
    function's `fail_closed_path` derivation is hardcoded to the retired
    anchor path's own three reasons and does not generalize to this path's
    `not_found` / `ambiguous` / `round_trip_verification_failed`).

    `flag_only` is `apply_quote_patches`'s own return list -- each entry the
    original `{source_quote, new_text, rationale}` patch plus `reason`, plus
    this module's private `_source_issue` correlation key (see
    `_issues_to_quote_patches`), read here for the attorney-readable fields
    and never copied into the returned shape verbatim.

    Never carries a `decision` field (ACCEPT/REQUEST_CHANGE): this artifact
    describes a system-level "could not auto-apply," never a legal
    decision, regardless of whether the CALLER's overall status is `OK`
    (partial delivery, some other patch in the batch applied) or
    `MANUAL_REVIEW_REQUIRED` (zero applied) -- see `generate_redline`'s own
    docstring.
    """
    changes_not_applied = []
    for entry in flag_only:
        issue = entry.get("_source_issue") or {}
        changes_not_applied.append(
            {
                "section_ref": issue.get("section_ref"),
                "section_title": issue.get("section_title"),
                "counterparty_change_summary": issue.get("counterparty_change_summary"),
                "source_quote": entry.get("source_quote"),
                "proposed_replacement_text": entry.get("new_text"),
                "external_rationale_for_footnote": entry.get("rationale"),
                "reason": entry.get("reason"),
            }
        )
    return {
        "report_type": "analysis_report",
        "reason": REASON_QUOTE_PATCHES_NOT_APPLIED,
        "fail_closed_path": (
            "One or more proposed edits could not be safely located or "
            "applied to the uploaded document -- the exact quoted text was "
            "not found, matched more than once, or the writer's own "
            "round-trip check failed after applying it. Each is listed "
            "below for the attorney to apply by hand."
        ),
        "changes_not_applied": changes_not_applied,
    }


def generate_redline(
    *,
    reconciled_result: dict[str, Any],
    corpus: "leakage_scan.ConfidentialCorpus",
    normalized_docx_bytes: bytes,
    review_id: Optional[str] = None,
    audit_write: Optional[Callable[..., None]] = None,
    current_counterparty_name: Optional[str] = None,
    author: str = redline_docx_writer.DEFAULT_AUTHOR,
    date: Any = None,
) -> dict[str, Any]:
    """Produce the final review deliverable from a reconciled review result.

    `reconciled_result` is `reconciliation.reconcile()`'s
    `output-schema-v1`-shaped output. `normalized_docx_bytes` is the same
    normalized upload the pipeline reviewed (issue #291) -- the package
    `redline_quote_apply.apply_quote_patches` patches in place on the
    REQUEST_CHANGE path (issue #379); unused on the ACCEPT / leakage-blocked
    paths, but still a required parameter so every caller threads it through
    regardless of which internal path a given review takes.

    ## Delivered document: in place, not a standalone clause list (issue #261)

    The delivered `.docx` is the UPLOADED document with `<w:ins>`/`<w:del>`
    tracked changes applied in place around each applied patch's quoted
    SPAN (not the whole paragraph -- issue #379's quote-based patcher is
    span-level, unlike the retired anchor path's whole-paragraph replace),
    with the export marker and footnoted rationales already injected by
    `redline_quote_apply.apply_quote_patches` itself (it reuses THIS
    module's `inject_export_marker_and_footnotes()` internally -- see that
    module's own docstring). Every paragraph, style, and part the patch
    batch didn't touch survives unchanged. `redline_docx_writer
    .build_tracked_changes_docx` (the standalone, synthetic-body writer) is
    not used by this function; it remains for other callers
    (`scripts/third_party_output_integration.py`,
    `scripts/gen_mock_eiaa_redline_fixture.py`) that were never in scope
    here.

    A TRUE flag-only issue (`proposed_replacement_text == ""`) is excluded
    before this function ever calls `apply_quote_patches` -- it is never
    touched in the delivered document (docs/output-contract.md, issue #260:
    never a bare `<w:del>` with no `<w:ins>`). See `_issues_to_quote_patches`
    for the full exclusion/pass-through contract, including the
    missing-`source_quote` case.

    Returns one of:

      ACCEPT, clean leakage scan:
        {"status": "OK", "decision": "ACCEPT", "docx_bytes": None,
         "verdict_summary": ..., "analysis_report": None}

      REQUEST_CHANGE, no patches to attempt (every issue flag-only):
        {"status": "OK", "decision": "REQUEST_CHANGE", "docx_bytes": None,
         "analysis_report": None, "verdict_summary": ...}

      REQUEST_CHANGE, at least one patch applied (issue #203 -- partial
      delivery, never "instead of": `analysis_report` is populated iff
      `apply_quote_patches` also reported one or more flag-only patches
      alongside the applied ones, else `None`):
        {"status": "OK", "decision": "REQUEST_CHANGE", "docx_bytes": <bytes>,
         "analysis_report": {...} | None, "verdict_summary": ...}

      REQUEST_CHANGE, every attempted patch failed to locate/apply (zero
      applied -- an ordinary counterparty-document condition, e.g. the
      model's quotes didn't match this document; never raised as an error):
        {"status": "MANUAL_REVIEW_REQUIRED",
         "reason": "quote_patches_not_applied", "docx_bytes": None,
         "analysis_report": {...}}

      Leakage scan positive detection (either path):
        {"status": "ERROR_MANUAL_REVIEW_REQUIRED", "reason": "leakage_detected",
         "field_name": ..., "category": ..., "rule_id": ...,
         "docx_bytes": None, "analysis_report": None}

      Output OOXML scan positive detection:
        {"status": "ERROR_MANUAL_REVIEW_REQUIRED",
         "reason": "output_ooxml_scan_failed", "detail": ...,
         "docx_bytes": None, "analysis_report": None}

      Word round-trip verification failure (issue #263 -- a writer bug, not
      a counterparty-document condition, but still reported the SAME
      fail-closed way as every other gate, never an uncaught exception):
        {"status": "ERROR_MANUAL_REVIEW_REQUIRED",
         "reason": "round_trip_verification_failed", "detail": ...,
         "docx_bytes": None, "analysis_report": None}

    Never raises for ANY of the fail-closed conditions above -- every gate
    is reported back as a status dict so a caller
    (`scripts/review_spine.py::run_review`, in turn
    `backend/src/pipeline_runner.py::run_real_pipeline`, the Docker Compose
    in-process runner's real-pipeline body) can persist a terminal,
    stage-attributed review state without relying on catching an
    unexpected exception.
    """
    try:
        leakage_scan.run_leakage_gate(
            reconciled_result,
            corpus,
            review_id=review_id,
            audit_write=audit_write,
            current_counterparty_name=current_counterparty_name,
        )
    except leakage_scan.LeakageDetectedError as exc:
        return {
            "status": ERROR_MANUAL_REVIEW_REQUIRED,
            "reason": "leakage_detected",
            "field_name": exc.field_name,
            "category": exc.category,
            "rule_id": exc.rule_id,
            "docx_bytes": None,
            "analysis_report": None,
        }

    if reconciled_result.get("decision") == "ACCEPT":
        # No document -- there is nothing to redline on the ACCEPT path
        # (docs/output-contract.md -> "ACCEPT summary shape").
        # verdict_summary already passed the leakage gate above.
        return {
            "status": "OK",
            "decision": "ACCEPT",
            "docx_bytes": None,
            "verdict_summary": reconciled_result.get("verdict_summary"),
            "analysis_report": None,
        }

    # REQUEST_CHANGE: quote-based patching (issue #379).
    issues = reconciled_result.get("issues") or []
    patches = _issues_to_quote_patches(issues)

    if not patches:
        # Every issue is TRUE flag-only (no proposed_replacement_text) --
        # nothing to attempt, not a failure (mirrors
        # third_party_output_integration.generate_third_party_review_output's
        # identical "no patches to apply" branch). Every issue still reaches
        # the attorney via the ordinary `findings` list the caller
        # (scripts/review_spine.py::run_review) surfaces.
        return {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "docx_bytes": None,
            "analysis_report": None,
            "verdict_summary": reconciled_result.get("verdict_summary"),
        }

    quote_result = redline_quote_apply.apply_quote_patches(
        normalized_docx_bytes,
        patches,
        author=author,
        timestamp_iso=redline_docx_writer._iso_date(date),
    )
    docx_bytes = quote_result["docx_bytes"]

    if docx_bytes is not None:
        try:
            run_output_ooxml_scan(docx_bytes)
        except OutputScanError as exc:
            return {
                "status": ERROR_MANUAL_REVIEW_REQUIRED,
                "reason": "output_ooxml_scan_failed",
                "detail": exc.detail,
                "docx_bytes": None,
                "analysis_report": None,
            }

        try:
            verify_docx_round_trip(docx_bytes)
        except ValueError as exc:
            # Defense-in-depth: apply_quote_patches already verified this
            # SAME round trip internally before ever returning non-None
            # docx_bytes (see that module's docstring, "Round-trip
            # verification") -- re-checking here can only ever pass, but
            # keeps this module's own gate list identical regardless of
            # which REQUEST_CHANGE patcher produced the bytes (issue #263).
            return {
                "status": ERROR_MANUAL_REVIEW_REQUIRED,
                "reason": "round_trip_verification_failed",
                "detail": str(exc),
                "docx_bytes": None,
                "analysis_report": None,
            }

    analysis_report = (
        _build_quote_analysis_report(quote_result["flag_only"])
        if quote_result["flag_only"]
        else None
    )

    if quote_result["applied"]:
        # Partial delivery (issue #203): docx_bytes present, some patches
        # may still be flag-only -- OK, never fail-closed-whole-doc (issue
        # #379 Scope item 2).
        return {
            "status": "OK",
            "decision": "REQUEST_CHANGE",
            "docx_bytes": docx_bytes,
            "analysis_report": analysis_report,
            "verdict_summary": reconciled_result.get("verdict_summary"),
        }

    # Zero applied: every attempted patch failed to locate/apply. Unlike the
    # "no patches to attempt" branch above, SOMETHING was tried and none of
    # it landed -- a human needs to see why (issue #379 Scope item 2:
    # "MANUAL_REVIEW_REQUIRED only if zero applied"). No "decision" key --
    # this is a SYSTEM status, never a legal decision (same convention as
    # the leakage-blocked result above).
    return {
        "status": MANUAL_REVIEW_REQUIRED,
        "reason": REASON_QUOTE_PATCHES_NOT_APPLIED,
        "docx_bytes": None,
        "analysis_report": analysis_report,
    }


_SMOKE_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_SMOKE_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _smoke_docx_bytes(paragraph_text: str) -> bytes:
    """Build a minimal, stdlib-only (no python-docx) one-paragraph `.docx`
    for the CLI smoke entry point below -- this module has no python-docx
    dependency of its own (that stays test-only, per issue #290)."""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{paragraph_text}</w:t></w:r></w:p><w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _SMOKE_CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _SMOKE_RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: run a trivial REQUEST_CHANGE reconciled result
    (carrying a real `source_quote`) through the full pipeline and report
    the outcome. The gate test (tests/redline/test_redline_generation_83.py)
    is the authoritative check. `docx_bytes` is expected to be non-empty --
    "shall not exceed $150,000" locates uniquely in the smoke fixture and
    applies cleanly (issue #379's quote-based patcher)."""
    sec8_text = "Each party's liability shall not exceed $150,000."
    reconciled_result = {
        "schema_version": "output-schema-v1",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [
            {
                "section_ref": "sec-8",
                "section_title": "Limitation on Liability",
                "counterparty_change_summary": "Deletes the liability cap.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "Restores the standard liability cap.",
                "proposed_replacement_text": "is uncapped",
                "playbook_topic_id": "limitation-of-liability",
                "internal_precedent_citation": None,
                "provenance": "model",
                "source_quote": "shall not exceed $150,000",
            }
        ],
        "critic_delta": None,
        "verdict_summary": None,
    }
    corpus = leakage_scan.ConfidentialCorpus()
    result = generate_redline(
        reconciled_result=reconciled_result,
        corpus=corpus,
        normalized_docx_bytes=_smoke_docx_bytes(sec8_text),
    )
    print(f"status={result['status']} docx_bytes={len(result['docx_bytes'] or b'')} bytes")


if __name__ == "__main__":
    main()
    sys.exit(0)
