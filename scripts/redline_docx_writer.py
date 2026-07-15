#!/usr/bin/env python3
"""
Pure-Python OOXML tracked-changes `.docx` writer.

Issue #198 (audit finding, `re-redline-core`): "The tracked-changes DOCX
writer does not exist; the deployed pipeline serves a canned pre-baked
redline." `scripts/redline_patch.py` deliberately stops at validating
`(anchor, source_text_hash)` pairs and returns `new_text` -- the actual
`<w:ins>`/`<w:del>` edit was previously deferred to "issue #83, out of
scope" and no writer existed anywhere in the repo (no `backend/vendor/`
directory, `grep -r 'w:ins'` found only docstrings/mock/tests, zero `.docx`
files anywhere in the repo).

## What this is

A minimal, dependency-free OOXML writer: a `.docx` is just a ZIP archive
of XML parts, so this module builds one with nothing but the standard
library (`zipfile` + `xml.etree.ElementTree`). No `python-docx`, no
vendored `anthropics/skills` `docx` fork, no `backend/vendor/` (that
directory was never created -- see the corrected claim at
ARCHITECTURE.md's "Redlining" section, landed alongside this module).

## Input contract

`build_tracked_changes_docx()` takes the `applied_patches` list exactly as
produced by `scripts/redline_patch.py::apply_patches()`:

    [{"anchor": "sec-8", "new_text": "Each party's liability is uncapped."}, ...]

`apply_patches()`'s return value is a patch-APPLICATION result, not a
diff, so it deliberately does not carry the pre-patch text at each anchor.
This writer takes that pre-patch text separately, as
`original_paragraphs_by_anchor` -- the SAME `current_paragraphs_by_anchor`
mapping the caller already passed into `apply_patches()` (patch-time
paragraph text, re-read from the live document) -- so it can render both
the struck-through original text (`<w:del>`) and the inserted replacement
(`<w:ins>`) for each applied patch, which is what "tracked changes" means
in Word: a reviewer opening the output sees both sides of the edit, not
just the new text silently swapped in.

## Correctness requirements (docs/evaluation.md gate #4:
"<w:ins>/<w:del> correctness, footnote insertion")

Each revision element (`<w:ins>`, `<w:del>`) carries `w:id`, `w:author`,
and `w:date` attributes, per the OOXML spec -- Word uses these to attribute
and render tracked changes in the Reviewing pane; a malformed or
attribute-incomplete revision element can silently corrupt what Word
displays as the change (this is exactly the risk the issue calls out: a
lawyer must never open a `.docx` whose revision markup is subtly wrong).
Deleted-run text uses `<w:delText>` (not `<w:t>`) inside `<w:del>`, per
spec -- using `<w:t>` there is a common and incorrect shortcut that some
naive writers take, and it renders wrong in Word's Reviewing pane.

Usage:
    from redline_patch import apply_patches
    from redline_docx_writer import build_tracked_changes_docx

    batch = apply_patches(current_paragraphs_by_anchor, patches)
    docx_bytes = build_tracked_changes_docx(
        batch["applied_patches"], current_paragraphs_by_anchor
    )
    with open("out.docx", "wb") as f:
        f.write(docx_bytes)

## Footnotes and the export marker (issue #83)

Two more pieces of the redline are built here, not in a separate module,
because both are OOXML-structure concerns (new document parts,
relationships, content-type overrides) that belong with the rest of the
writer:

- **Footnoted rationales.** When `footnote_text_by_anchor` maps an applied
  patch's anchor to its `external_rationale_for_footnote` text, a
  `<w:footnoteReference>` is appended to that patch's paragraph and the
  rationale is written to `word/footnotes.xml` as a literal text run --
  same literal-runs-only rule as the `<w:ins>`/`<w:del>` text
  (docs/output-contract.md -> "Per-issue output and footnote rules").
  Footnote ids are computed once by `_compute_footnotes()` and reused by
  both `word/document.xml` (the reference) and `word/footnotes.xml` (the
  definition), so the two parts can never disagree on numbering.
- **Export marker.** `include_marker=True` (the default -- "the marker
  remains the default on every generated redline", docs/output-contract.md
  -> "Export marker") adds the internal-only / export-warning marker
  redundantly: a first-page cover note (`word/document.xml`, before the
  tracked-change body, followed by an explicit page break) plus a running
  every-page header and footer (`word/header1.xml`, `word/footer1.xml`,
  wired via `word/_rels/document.xml.rels` + `<w:headerReference>`/
  `<w:footerReference>` on `<w:sectPr>`). Pass `include_marker=False` only
  for the deliberate de-marking / clean-copy path (issue #39); every
  ordinary redline call site leaves it on.
"""

import datetime
import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from typing import Optional

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

ET.register_namespace("w", WORD_NS)
ET.register_namespace("r", REL_NS)

DEFAULT_AUTHOR = "contract-toaster"

# Internal-only / export-warning marker text (docs/output-contract.md ->
# "Export marker", ARCHITECTURE.md -> "Export / misuse marker"). Matches the
# SPA result-view watermark verbatim -- this is the baked-into-the-document
# half of that same misuse-prevention control.
MARKER_TEXT = (
    "tool recommendation only — attorney approval required; do not "
    "send externally before attorney approval"
)

FOOTNOTES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)
HEADER_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
)
FOOTER_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
)
FOOTNOTES_REL_TYPE = f"{REL_NS}/footnotes"
HEADER_REL_TYPE = f"{REL_NS}/header"
FOOTER_REL_TYPE = f"{REL_NS}/footer"

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _r(tag: str) -> str:
    return f"{{{REL_NS}}}{tag}"


def _iso_date(dt: Optional[datetime.datetime]) -> str:
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_run(
    parent: ET.Element,
    revision_tag: str,
    text_tag: str,
    text: str,
    author: str,
    date_str: str,
    rev_id: int,
) -> None:
    """Append one <w:ins> or <w:del> revision element (with a single run
    inside it) carrying w:id/w:author/w:date, per the OOXML tracked-changes
    schema."""
    revision = ET.SubElement(parent, _w(revision_tag))
    revision.set(_w("id"), str(rev_id))
    revision.set(_w("author"), author)
    revision.set(_w("date"), date_str)
    run = ET.SubElement(revision, _w("r"))
    text_el = ET.SubElement(run, _w(text_tag))
    # xml:space="preserve" so leading/trailing whitespace in clause text
    # survives Word's default whitespace-collapsing.
    text_el.set(f"{{{XML_NS}}}space", "preserve")
    text_el.text = text


def _append_footnote_reference(parent: ET.Element, footnote_id: int) -> None:
    """Append a plain run carrying `<w:footnoteReference w:id="..."/>`,
    linking a paragraph to its footnote definition in `footnotes.xml`."""
    run = ET.SubElement(parent, _w("r"))
    ref = ET.SubElement(run, _w("footnoteReference"))
    ref.set(_w("id"), str(footnote_id))


def _compute_footnotes(
    applied_patches: list[dict], footnote_text_by_anchor: Optional[dict]
) -> list[dict]:
    """Deterministic anchor -> footnote-id/text assignment, in
    `applied_patches` order, skipping any anchor with no footnote text.
    Computed by this single pure function and reused by both
    `build_document_xml` (footnote references) and `build_footnotes_xml`
    (footnote definitions) so the two parts can never disagree on
    numbering."""
    footnote_text_by_anchor = footnote_text_by_anchor or {}
    footnotes = []
    next_id = 1
    for patch in applied_patches:
        text = footnote_text_by_anchor.get(patch["anchor"])
        if text:
            footnotes.append({"id": next_id, "anchor": patch["anchor"], "text": text})
            next_id += 1
    return footnotes


def _compute_relationship_ids(include_footnotes: bool, include_marker: bool) -> dict:
    """Assign `word/_rels/document.xml.rels` relationship ids for whichever
    of footnotes/header/footer parts this document actually carries. Called
    identically by `build_document_xml` (needs header/footer `r:id`s for
    `<w:sectPr>`) and `build_tracked_changes_docx` (needs all three ids to
    write the `.rels`/content-types parts), so the ids used in
    `word/document.xml` always match the relationships that actually
    exist."""
    ids: dict = {}
    next_rid = 1
    if include_footnotes:
        ids["footnotes"] = f"rId{next_rid}"
        next_rid += 1
    if include_marker:
        ids["header"] = f"rId{next_rid}"
        next_rid += 1
        ids["footer"] = f"rId{next_rid}"
        next_rid += 1
    return ids


def _append_marker_text_run(parent: ET.Element, marker_text: str) -> None:
    run = ET.SubElement(parent, _w("r"))
    text_el = ET.SubElement(run, _w("t"))
    text_el.set(f"{{{XML_NS}}}space", "preserve")
    text_el.text = marker_text


def _append_cover_marker(body: ET.Element, marker_text: str) -> None:
    """First-page cover note: a paragraph carrying the marker text,
    followed by an explicit page break so the note reads as its own cover
    page rather than just the first line of the tracked-change body
    (docs/output-contract.md -> 'Export marker': 'first-page cover note
    plus a running every-page header/footer')."""
    cover_p = ET.SubElement(body, _w("p"))
    _append_marker_text_run(cover_p, marker_text)

    break_p = ET.SubElement(body, _w("p"))
    break_run = ET.SubElement(break_p, _w("r"))
    br = ET.SubElement(break_run, _w("br"))
    br.set(_w("type"), "page")


def build_footnotes_xml(footnotes: list[dict]) -> bytes:
    """Build `word/footnotes.xml` bytes: the two mandatory separator
    footnotes Word expects (ids -1 and 0) plus one `<w:footnote>` per entry
    in `footnotes` (as produced by `_compute_footnotes`), each carrying the
    rationale text as a literal `<w:t>` run -- never a field code or
    hyperlink (docs/output-contract.md -> 'Literal-runs-only insertion')."""
    root = ET.Element(_w("footnotes"))

    for special_id, kind in ((-1, "separator"), (0, "continuationSeparator")):
        fn = ET.SubElement(root, _w("footnote"))
        fn.set(_w("type"), kind)
        fn.set(_w("id"), str(special_id))
        p = ET.SubElement(fn, _w("p"))
        r = ET.SubElement(p, _w("r"))
        ET.SubElement(r, _w(kind))

    for entry in footnotes:
        fn = ET.SubElement(root, _w("footnote"))
        fn.set(_w("id"), str(entry["id"]))
        p = ET.SubElement(fn, _w("p"))
        ref_run = ET.SubElement(p, _w("r"))
        ET.SubElement(ref_run, _w("footnoteRef"))
        text_run = ET.SubElement(p, _w("r"))
        text_el = ET.SubElement(text_run, _w("t"))
        text_el.set(f"{{{XML_NS}}}space", "preserve")
        text_el.text = " " + entry["text"]

    xml_bytes = ET.tostring(root, encoding="unicode").encode("utf-8")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml_bytes


def _build_header_or_footer_xml(root_tag: str, marker_text: str) -> bytes:
    root = ET.Element(_w(root_tag))
    p = ET.SubElement(root, _w("p"))
    _append_marker_text_run(p, marker_text)
    xml_bytes = ET.tostring(root, encoding="unicode").encode("utf-8")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml_bytes


def build_header_xml(marker_text: str = MARKER_TEXT) -> bytes:
    """Build `word/header1.xml` bytes: a single paragraph carrying the
    export marker, rendered on every page (docs/output-contract.md ->
    'Export marker')."""
    return _build_header_or_footer_xml("hdr", marker_text)


def build_footer_xml(marker_text: str = MARKER_TEXT) -> bytes:
    """Build `word/footer1.xml` bytes -- the footer half of the redundant
    every-page marker placement."""
    return _build_header_or_footer_xml("ftr", marker_text)


def _build_document_rels_xml(rel_ids: dict) -> bytes:
    entries = []
    if "footnotes" in rel_ids:
        entries.append(
            f'<Relationship Id="{rel_ids["footnotes"]}" '
            f'Type="{FOOTNOTES_REL_TYPE}" Target="footnotes.xml"/>'
        )
    if "header" in rel_ids:
        entries.append(
            f'<Relationship Id="{rel_ids["header"]}" '
            f'Type="{HEADER_REL_TYPE}" Target="header1.xml"/>'
        )
    if "footer" in rel_ids:
        entries.append(
            f'<Relationship Id="{rel_ids["footer"]}" '
            f'Type="{FOOTER_REL_TYPE}" Target="footer1.xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(entries)
        + "</Relationships>"
    ).encode("utf-8")


def _build_content_types_xml(rel_ids: dict) -> bytes:
    overrides = [
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    ]
    if "footnotes" in rel_ids:
        overrides.append(
            f'<Override PartName="/word/footnotes.xml" ContentType="{FOOTNOTES_CONTENT_TYPE}"/>'
        )
    if "header" in rel_ids:
        overrides.append(
            f'<Override PartName="/word/header1.xml" ContentType="{HEADER_CONTENT_TYPE}"/>'
        )
    if "footer" in rel_ids:
        overrides.append(
            f'<Override PartName="/word/footer1.xml" ContentType="{FOOTER_CONTENT_TYPE}"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(overrides)
        + "</Types>"
    ).encode("utf-8")


def build_document_xml(
    applied_patches: list[dict],
    original_paragraphs_by_anchor: dict,
    author: str = DEFAULT_AUTHOR,
    date: Optional[datetime.datetime] = None,
    footnote_text_by_anchor: Optional[dict] = None,
    include_marker: bool = True,
    marker_text: str = MARKER_TEXT,
) -> bytes:
    """
    Build `word/document.xml` bytes: one `<w:p>` per applied patch, each
    containing a `<w:del>` of the pre-patch text at that anchor (if any
    original text is known) followed by a `<w:ins>` of `new_text`, in the
    order `applied_patches` is given. When `footnote_text_by_anchor` gives
    a patch's anchor a rationale, that paragraph also gets a
    `<w:footnoteReference>` run (issue #83). When `include_marker` is True
    (the default), a first-page cover-note paragraph carrying `marker_text`
    is prepended and `<w:sectPr>` carries `<w:headerReference>`/
    `<w:footerReference>` so the running header/footer marker renders on
    every page (docs/output-contract.md -> 'Export marker').

    Raises ValueError if `applied_patches` is empty -- a writer call with
    nothing to write is a caller bug, not a valid empty document.
    """
    if not applied_patches:
        raise ValueError(
            "build_document_xml requires at least one applied patch; "
            "an empty applied_patches list means the caller should not be "
            "producing a redline document at all"
        )

    date_str = _iso_date(date)
    footnotes = _compute_footnotes(applied_patches, footnote_text_by_anchor)
    footnote_id_by_anchor = {f["anchor"]: f["id"] for f in footnotes}
    rel_ids = _compute_relationship_ids(bool(footnotes), include_marker)

    body = ET.Element(_w("body"))

    if include_marker:
        _append_cover_marker(body, marker_text)

    rev_id = 1
    for patch in applied_patches:
        anchor = patch["anchor"]
        new_text = patch.get("new_text")
        original_text = original_paragraphs_by_anchor.get(anchor)

        p = ET.SubElement(body, _w("p"))

        if original_text:
            _append_run(p, "del", "delText", original_text, author, date_str, rev_id)
            rev_id += 1

        if new_text:
            _append_run(p, "ins", "t", new_text, author, date_str, rev_id)
            rev_id += 1

        footnote_id = footnote_id_by_anchor.get(anchor)
        if footnote_id is not None:
            _append_footnote_reference(p, footnote_id)

    sect_pr = ET.SubElement(body, _w("sectPr"))
    if include_marker:
        header_ref = ET.SubElement(sect_pr, _w("headerReference"))
        header_ref.set(_w("type"), "default")
        header_ref.set(_r("id"), rel_ids["header"])
        footer_ref = ET.SubElement(sect_pr, _w("footerReference"))
        footer_ref.set(_w("type"), "default")
        footer_ref.set(_r("id"), rel_ids["footer"])

    document = ET.Element(_w("document"))
    document.append(body)

    xml_bytes = ET.tostring(document, encoding="unicode").encode("utf-8")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml_bytes


def build_tracked_changes_docx(
    applied_patches: list[dict],
    original_paragraphs_by_anchor: dict,
    author: str = DEFAULT_AUTHOR,
    date: Optional[datetime.datetime] = None,
    footnote_text_by_anchor: Optional[dict] = None,
    include_marker: bool = True,
    marker_text: str = MARKER_TEXT,
) -> bytes:
    """
    Build a complete, valid `.docx` (a ZIP of `[Content_Types].xml`,
    `_rels/.rels`, `word/document.xml`, and -- when applicable --
    `word/footnotes.xml`, `word/header1.xml`, `word/footer1.xml`, and
    `word/_rels/document.xml.rels`) containing one tracked-change
    `<w:del>`/`<w:ins>` pair per applied patch.

    `footnote_text_by_anchor` (anchor -> `external_rationale_for_footnote`
    text) adds a footnoted rationale to each matching patch's paragraph
    (issue #83 AC: "footnoted rationales"). `include_marker=True` (default)
    bakes in the redundant internal-only / export-warning marker -- cover
    note plus every-page header/footer (issue #83 AC: "Marker placement per
    spec"); pass `include_marker=False` only for the deliberate de-marking
    / clean-copy export path (issue #39).

    Returns raw `.docx` bytes -- an actual document, never an S3 pointer or
    a reference to any pre-baked/canned fixture. Callers persist these
    bytes to the outputs bucket themselves; this module has no knowledge
    of S3, review IDs, or the mock pipeline's pointer-only payload
    convention (docs/output-contract.md) -- it is a pure text-in,
    bytes-out writer.
    """
    document_xml = build_document_xml(
        applied_patches,
        original_paragraphs_by_anchor,
        author=author,
        date=date,
        footnote_text_by_anchor=footnote_text_by_anchor,
        include_marker=include_marker,
        marker_text=marker_text,
    )

    footnotes = _compute_footnotes(applied_patches, footnote_text_by_anchor)
    rel_ids = _compute_relationship_ids(bool(footnotes), include_marker)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _build_content_types_xml(rel_ids))
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
        if rel_ids:
            zf.writestr("word/_rels/document.xml.rels", _build_document_rels_xml(rel_ids))
        if footnotes:
            zf.writestr("word/footnotes.xml", build_footnotes_xml(footnotes))
        if include_marker:
            zf.writestr("word/header1.xml", build_header_xml(marker_text))
            zf.writestr("word/footer1.xml", build_footer_xml(marker_text))
    return buf.getvalue()


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """
    CLI smoke test: build a tracked-changes .docx from one inline sample
    patch and report its size. Useful for a quick manual sanity check; the
    gate test (tests/redline/test_docx_tracked_changes_writer.py) is the
    authoritative check.
    """
    original = {
        "sec-8": (
            "Each party's aggregate liability under this Agreement shall "
            "not exceed $150,000."
        )
    }
    applied_patches = [
        {"anchor": "sec-8", "new_text": "Each party's liability is uncapped."}
    ]
    docx_bytes = build_tracked_changes_docx(applied_patches, original)
    print(f"Built tracked-changes .docx: {len(docx_bytes)} bytes")


if __name__ == "__main__":
    main()
    sys.exit(0)
