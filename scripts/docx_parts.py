#!/usr/bin/env python3
"""
The non-`document.xml` OOXML parts a redline export writes, and the
constants that name them (issue #631).

## Why this module exists

These builders and constants lived in the standalone whole-document
tracked-changes writer written for the retired anchor/hash patch path.
Issue #631 deleted that writer -- the 2026-07-22
LLM-native decision (D3) replaced whole-document authoring with the
block-transcript compiler, which edits the UPLOADED package in place. But the
three live writers still need the same footnote / header / footer / styles
parts, and the same relationship and content-type identifiers, to attach them
to that package:

  - `scripts/redline_generate.py`    (export marker + footnote injection)
  - `scripts/redline_block_apply.py` (footnote references + style injection)
  - `scripts/redline_projections.py` (proves a `word/styles.xml` delta is
                                      only the footnote styles)

So the parts move here, unchanged. Following `scripts/ooxml_util.py`'s
precedent (issue #621), `_iso_date` is spelled without the underscore now
that it is deliberate shared API rather than a private reach into another
module. The BODIES are the originals, byte for byte.

What did NOT move: the deleted writer's `_append_run` and
`_compute_footnotes`. Each live writer already does that work itself
(`redline_generate._compute_new_footnote_entries` computes footnote
entries against the uploaded package's existing ids;
`redline_block_apply._stamp_revision` stamps revisions inline), so
carrying them forward under new public names would have re-spelled the
retired subsystem instead of removing it -- a module with no caller is not
shared API.

The export marker's own TEXT is not here: it belongs to the injection seam
that decides whether a document carries one at all, so it lives with
`redline_generate.inject_export_marker_and_footnotes`. `build_header_xml` /
`build_footer_xml` therefore take the marker text as a required argument --
there is no default that could quietly stamp a marker nobody asked for.

Namespaces, part names and the `w:`/`r:` qname helpers come from
`scripts/ooxml_util.py`, the one place they are declared.
"""

from __future__ import annotations

import datetime
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ooxml_util import REL_NS, WORD_NS, XML_NS, w  # noqa: E402

DEFAULT_AUTHOR = "contract-toaster"

FOOTNOTES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)
HEADER_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
)
FOOTER_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
)
STYLES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
)
FOOTNOTES_REL_TYPE = f"{REL_NS}/footnotes"
HEADER_REL_TYPE = f"{REL_NS}/header"
FOOTER_REL_TYPE = f"{REL_NS}/footer"
STYLES_REL_TYPE = f"{REL_NS}/styles"

STYLES_PART = "word/styles.xml"

# ---------------------------------------------------------------------------
# The two styles a footnote resolves against (issue #647)
#
# Word's own markup for a footnote reference is
# `<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>
# <w:footnoteReference w:id="N"/></w:r>` -- the superscript lives in the
# STYLE, not in direct run formatting. Emitting the `<w:rStyle>` alone is not
# enough: a document that has never carried a footnote does not define
# `FootnoteReference` at all (measured on
# `tests/fixtures/document-shapes/baseline-mutual-nda.SYNTHETIC.docx`, and on
# python-docx's default template), so the reference resolves to nothing and
# renders as ordinary inline text -- the bug issue #647 reports, seen by the
# owner in the first production redline.
#
# Direct formatting (`<w:vertAlign w:val="superscript"/>` on the run) would
# also render, and is rejected deliberately: it reads to a human as a manual
# override, it does not follow a document's own footnote styling, and it is
# not what Word writes. Injecting the styles is.
#
# `w:basedOn` is deliberately ABSENT from both definitions. Word writes
# `basedOn="DefaultParagraphFont"` / `basedOn="Normal"`, but those style ids
# are not guaranteed to exist in an arbitrary uploaded package, and a
# `w:basedOn` pointing at an undefined style is a dangling reference. Without
# it each style inherits from the document's own `<w:docDefaults>`, which is
# what a reader expects anyway.
# ---------------------------------------------------------------------------
FOOTNOTE_REFERENCE_STYLE_ID = "FootnoteReference"
FOOTNOTE_TEXT_STYLE_ID = "FootnoteText"

#: `styleId` -> the `<w:style>` XML this module injects for it. Ordered
#: paragraph-style-first, the order Word writes them in. Held as TEXT rather
#: than as `ET.Element`s so there is exactly one literal definition of each
#: style in the repo: `footnote_style_elements()` parses it for a writer, and
#: `redline_projections` parses it to decide whether a `word/styles.xml`
#: delta is the one a redline is allowed to make.
FOOTNOTE_STYLE_XML = {
    FOOTNOTE_TEXT_STYLE_ID: (
        f'<w:style xmlns:w="{WORD_NS}" w:type="paragraph" w:styleId="FootnoteText">'
        '<w:name w:val="footnote text"/>'
        '<w:uiPriority w:val="99"/>'
        "<w:semiHidden/>"
        "<w:unhideWhenUsed/>"
        '<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        '<w:rPr><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr>'
        "</w:style>"
    ),
    FOOTNOTE_REFERENCE_STYLE_ID: (
        f'<w:style xmlns:w="{WORD_NS}" w:type="character" w:styleId="FootnoteReference">'
        '<w:name w:val="footnote reference"/>'
        '<w:uiPriority w:val="99"/>'
        "<w:semiHidden/>"
        "<w:unhideWhenUsed/>"
        '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
        "</w:style>"
    ),
}


def footnote_style_elements() -> "list[ET.Element]":
    """Freshly parsed `<w:style>` elements for `FOOTNOTE_STYLE_XML`, in that
    mapping's order.

    Fresh on every call: the caller appends them into a document's own
    `<w:styles>` tree, and handing back a shared element would alias one
    package's styles into the next one's.
    """
    return [ET.fromstring(xml) for xml in FOOTNOTE_STYLE_XML.values()]


def iso_date(dt: Optional[datetime.datetime]) -> str:
    """The `w:date` stamp OOXML tracked changes carry, UTC, second precision."""
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_footnotes_xml(footnotes: list[dict]) -> bytes:
    """Build `word/footnotes.xml` bytes: the two mandatory separator
    footnotes Word expects (ids -1 and 0) plus one `<w:footnote>` per entry
    in `footnotes` (as produced by
    `redline_generate._compute_new_footnote_entries`), each carrying the
    rationale text as a literal `<w:t>` run -- never a field code or
    hyperlink (docs/output-contract.md -> 'Literal-runs-only insertion')."""
    root = ET.Element(w("footnotes"))

    for special_id, kind in ((-1, "separator"), (0, "continuationSeparator")):
        fn = ET.SubElement(root, w("footnote"))
        fn.set(w("type"), kind)
        fn.set(w("id"), str(special_id))
        p = ET.SubElement(fn, w("p"))
        run = ET.SubElement(p, w("r"))
        ET.SubElement(run, w(kind))

    for entry in footnotes:
        fn = ET.SubElement(root, w("footnote"))
        fn.set(w("id"), str(entry["id"]))
        p = ET.SubElement(fn, w("p"))
        ref_run = ET.SubElement(p, w("r"))
        ET.SubElement(ref_run, w("footnoteRef"))
        text_run = ET.SubElement(p, w("r"))
        text_el = ET.SubElement(text_run, w("t"))
        text_el.set(f"{{{XML_NS}}}space", "preserve")
        text_el.text = " " + entry["text"]

    xml_bytes = ET.tostring(root, encoding="unicode").encode("utf-8")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml_bytes


def append_marker_text_run(parent: ET.Element, marker_text: str) -> None:
    """Append a plain literal-text run carrying the export marker."""
    run = ET.SubElement(parent, w("r"))
    text_el = ET.SubElement(run, w("t"))
    text_el.set(f"{{{XML_NS}}}space", "preserve")
    text_el.text = marker_text


def _build_header_or_footer_xml(root_tag: str, marker_text: str) -> bytes:
    root = ET.Element(w(root_tag))
    p = ET.SubElement(root, w("p"))
    append_marker_text_run(p, marker_text)
    xml_bytes = ET.tostring(root, encoding="unicode").encode("utf-8")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml_bytes


def build_header_xml(marker_text: str) -> bytes:
    """Build `word/header1.xml` bytes: a single paragraph carrying the
    export marker, rendered on every page (docs/output-contract.md ->
    'Export marker')."""
    return _build_header_or_footer_xml("hdr", marker_text)


def build_footer_xml(marker_text: str) -> bytes:
    """Build `word/footer1.xml` bytes -- the footer half of the redundant
    every-page marker placement."""
    return _build_header_or_footer_xml("ftr", marker_text)
