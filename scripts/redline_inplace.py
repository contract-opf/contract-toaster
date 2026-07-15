#!/usr/bin/env python3
"""
Stdlib in-place OOXML patcher.

Issue #290 (In-place redline 1/2, implements #261 -- "Redline output must be
the uploaded document with in-situ tracked changes, not a standalone clause
list"). `scripts/redline_docx_writer.py` (issue #198) builds a STANDALONE
tracked-changes document: one synthetic `<w:p>` per applied patch, none of
the uploaded document's own paragraphs, styling, or untouched clauses. #261
requires the delivered redline to be the SAME document the attorney
uploaded, with edits applied in place as `<w:ins>`/`<w:del>` markup and
every other part of the docx preserved byte-for-byte -- this module is the
core patcher that does that. Wiring this into `generate_redline`, the export
marker, footnotes, leakage/output scans, and the round-trip gate are all
slice 2 of #261 (a separate issue); this module is patcher-only.

## Input contract

`patches` is a list of dicts, each `{anchor, source_text, new_text,
footnote_text (optional, ignored in this slice)}` -- the anchor plus the
paragraph text a patch targets and the text it replaces it with. `new_text`
MUST be non-empty for every patch: issue #260 filters empty-`new_text`
("flag only", no replacement) patches out upstream, so a patch reaching this
module with an empty `new_text` is a caller bug, not a valid input --
`apply_tracked_changes_inplace` raises `ValueError` naming every offending
anchor rather than silently producing a paragraph with no insertion.

## Locate (same content invariant as `redline_patch.py`, edge-whitespace-tolerant)

A patch's target is the paragraph (direct child `<w:p>` of `word/document.xml`'s
`<w:body>` -- table-cell paragraphs are out of scope for this slice, see
"Limitations" below) whose concatenated `<w:t>` text equals `source_text`
EXACTLY once both sides are stripped of leading/trailing whitespace -- the
same "no fuzzy match, ever" exact-match rule `scripts/redline_patch.py` uses
at the hash-validation layer (its module docstring: "'Apply the closest
match' is explicitly prohibited"), extended only to tolerate the edge
whitespace `extraction_normalization_stage.normalize_paragraphs` strips
from `source_text` in the real pipeline (issue #291 review finding 1) --
the caller's `source_text` is that NORMALIZED draft text, not this
paragraph's own raw runs, so a stripping-insensitive comparison is required
for `source_text` to ever match at all. The text between the edges is still
compared character-for-character; nothing fuzzy about interior content. The
`<w:del>` this module then writes still carries the paragraph's ACTUAL raw
text (edge whitespace included), never the normalized proxy used only to
locate it. Zero matches or two-or-more matches both mean the patch cannot
be safely targeted, and it is NOT applied -- reported in
`InplaceResult.failed` as `{"anchor": ..., "reason": "not_found" |
"ambiguous"}`. This mirrors `redline_patch.apply_patches`'s per-patch, fail-closed, partial-delivery
semantics: one patch's failure to locate does not block any other patch in
the same call from applying.

## Rewrite

The matched paragraph's run children are replaced with exactly two
elements -- a `<w:del>` of `source_text` (using `<w:delText>`, per the
OOXML tracked-changes schema -- `<w:t>` inside `<w:del>` is a common,
incorrect shortcut that renders wrong in Word's Reviewing pane, same
correctness requirement `redline_docx_writer.py` documents) followed by a
`<w:ins>` of `new_text` -- while the paragraph's own `<w:pPr>` (styling,
numbering, etc.) is left completely untouched. All text goes through
ElementTree text nodes (escaped automatically); no field codes, no `<w:rPr>`
carried over from the model's proposed text -- literal runs only, same
convention as `redline_docx_writer.py`.

`w:id` values assigned to new `<w:del>`/`<w:ins>` elements are unique
across the WHOLE document, not just the touched paragraph: this module
scans every element in the parsed tree for an existing `w:id` attribute
before assigning anything, so a document that already carries tracked
changes (opened, edited, and re-saved by a human before upload) never gets
a colliding id.

## Preserve

Every zip entry except `word/document.xml` is copied byte-for-byte (same
`ZipInfo` object, same raw bytes) -- only `word/document.xml` is
re-serialized.

Registering the `w` namespace with `ET.register_namespace` stops
ElementTree from renaming THAT one prefix on elements it serializes, but it
does nothing for the other 15+ namespaces (`mc`, `r`, `w14`, `wp14`, ...) a
real Word-authored `word/document.xml` root declares, and ElementTree's
serializer only re-declares a namespace at all if it thinks some tag or
attribute in the tree still "uses" it -- a namespace referenced only inside
an attribute VALUE (e.g. `mc:Ignorable="w14 wp14"`, where `w14`/`wp14` are
themselves just tokens in a string, invisible to ElementTree's namespace
scan) is silently dropped, and `mc:Ignorable` itself gets rewritten to an
auto-generated prefix (`ns1:Ignorable`) if `mc` was never registered --
malformed Markup Compatibility (ISO/IEC 29500-3) markup that risks Word's
"unreadable content" repair dialog on open, defeating the whole point of
this module. So instead: every namespace prefix the root element declares
in the ORIGINAL `word/document.xml` is registered with
`ET.register_namespace` (not just `w`) so ElementTree picks matching
prefixes for anything it does serialize, AND the root element's start tag
is spliced back in VERBATIM from the original bytes after ElementTree
serializes the (mutated) tree -- so every xmlns declaration on the root
survives untouched regardless of whether ElementTree's usage-scan would
have kept it.

## Limitations (out of scope this slice)

Only document-body `<w:p>` elements are ever located or rewritten --
table-cell paragraphs (inside `<w:tbl>`) are not visited. A patch whose
`source_text` only exists inside a table cell reports `"not_found"`, same
as if the text were absent altogether. Extending locate/rewrite into table
cells is left to a follow-up slice.

Usage:
    from redline_inplace import apply_tracked_changes_inplace

    result = apply_tracked_changes_inplace(
        docx_bytes,
        [{"anchor": "sec-8", "source_text": "...", "new_text": "..."}],
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    # result.docx_bytes, result.applied, result.failed
"""

import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("w", WORD_NS)

# The ONE OOXML part this module ever rewrites -- every other zip entry is
# copied through byte-for-byte (same allowlist-by-construction convention as
# scripts/extraction_normalization_stage.py's ALLOWED_DOCUMENT_PART).
DOCUMENT_PART = "word/document.xml"


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Root-namespace preservation (see module docstring, "Preserve")
#
# `xml.etree.ElementTree` discards `xmlns:*` bindings from `Element.attrib`
# at parse time and, at serialize time, only re-declares a namespace it
# decides is actually "used" by some tag or attribute it walks -- so a
# straight `ET.fromstring` -> mutate -> `ET.tostring` round trip silently
# drops any root xmlns declaration that isn't referenced by a qname
# ElementTree can see (e.g. one referenced only inside an attribute VALUE
# such as `mc:Ignorable="w14 wp14"`). The functions below read the root
# element's start tag directly out of the original bytes -- never through
# ElementTree -- so it can be spliced back in verbatim after serialization.
# ---------------------------------------------------------------------------

_ATTR_RE = re.compile(r"([^\s=/>]+)\s*=\s*(\"[^\"]*\"|'[^']*')")


def _scan_tag_end(text: str, start: int) -> int:
    """Return the index of the `>` that closes the start tag beginning at
    `text[start]` (`text[start] == '<'`), skipping over `>` characters that
    appear inside quoted attribute values."""
    i = start + 1
    in_quote = None
    while i < len(text):
        ch = text[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in "'\"":
            in_quote = ch
        elif ch == ">":
            return i
        i += 1
    raise ValueError("redline_inplace: malformed root start tag (no closing '>' found)")


def _root_open_tag(xml_text: str) -> str:
    """Return the root element's start tag exactly as it appears in
    `xml_text` -- attribute order, quoting, and every `xmlns` declaration
    preserved verbatim -- skipping a leading XML declaration if present."""
    idx = 0
    if xml_text.startswith("<?"):
        idx = xml_text.index("?>") + 2
    start = xml_text.index("<", idx)
    end = _scan_tag_end(xml_text, start)
    open_tag = xml_text[start : end + 1]
    if open_tag.endswith("/>"):
        raise ValueError(
            "redline_inplace: word/document.xml root element must not be self-closing"
        )
    return open_tag


def _declared_namespaces(open_tag: str) -> list:
    """`(prefix, uri)` pairs for every `xmlns[:prefix]="uri"` declaration on
    `open_tag` (a default-namespace declaration, `xmlns="uri"`, yields
    prefix `""` and is skipped by the caller -- registering an empty prefix
    with `ET.register_namespace` would make it the default for every URI
    that has none, which is not what we want here)."""
    out = []
    for match in _ATTR_RE.finditer(open_tag):
        name, quoted_value = match.group(1), match.group(2)
        value = quoted_value[1:-1]
        if name == "xmlns":
            out.append(("", value))
        elif name.startswith("xmlns:"):
            out.append((name.split(":", 1)[1], value))
    return out


@dataclass
class InplaceResult:
    """Result of `apply_tracked_changes_inplace`.

    `docx_bytes`: the rewritten document (every part but `word/document.xml`
    byte-identical to the input). `applied`: anchors whose patch was located
    and rewritten. `failed`: `{"anchor": ..., "reason": "not_found" |
    "ambiguous"}` for every patch that could not be safely targeted.
    """

    docx_bytes: bytes
    applied: list = field(default_factory=list)
    failed: list = field(default_factory=list)


def _paragraph_text(p: ET.Element) -> str:
    """Concatenated `<w:t>` text for one paragraph -- the same raw-text
    invariant `redline_patch.py` validates target text against. Uses
    `.iter()` (not direct children) so text inside pre-existing tracked
    changes (`<w:ins>/<w:r>/<w:t>`) on an UNTOUCHED paragraph is still part
    of that paragraph's current text, matching what a reviewer would see as
    the paragraph's content today."""
    return "".join(t.text or "" for t in p.iter(_w("t")))


def _body_paragraphs(body: ET.Element) -> list:
    """Direct-child `<w:p>` elements of `<w:body>` only -- table-cell
    paragraphs (nested inside `<w:tbl>`) are out of scope this slice (see
    module docstring, 'Limitations')."""
    return [child for child in list(body) if child.tag == _w("p")]


def _max_existing_id(root: ET.Element) -> int:
    """Scan every element in the parsed document for an existing `w:id`
    attribute and return the maximum integer value found (0 if none), so
    newly assigned revision ids never collide with ids a human-edited
    upload already carries."""
    max_id = 0
    for el in root.iter():
        val = el.get(_w("id"))
        if val is None:
            continue
        try:
            max_id = max(max_id, int(val))
        except ValueError:
            continue
    return max_id


def _rewrite_paragraph(
    p: ET.Element,
    source_text: str,
    new_text: str,
    author: str,
    timestamp_iso: str,
    del_id: int,
    ins_id: int,
) -> None:
    """Replace `p`'s run children with exactly one `<w:del>` (delText =
    `source_text`) followed by one `<w:ins>` (t = `new_text`), leaving
    `<w:pPr>` (if present) untouched and in place."""
    ppr = p.find(_w("pPr"))
    for child in list(p):
        if child is not ppr:
            p.remove(child)

    del_el = ET.SubElement(p, _w("del"))
    del_el.set(_w("id"), str(del_id))
    del_el.set(_w("author"), author)
    del_el.set(_w("date"), timestamp_iso)
    del_run = ET.SubElement(del_el, _w("r"))
    del_text_el = ET.SubElement(del_run, _w("delText"))
    del_text_el.set(f"{{{XML_NS}}}space", "preserve")
    del_text_el.text = source_text

    ins_el = ET.SubElement(p, _w("ins"))
    ins_el.set(_w("id"), str(ins_id))
    ins_el.set(_w("author"), author)
    ins_el.set(_w("date"), timestamp_iso)
    ins_run = ET.SubElement(ins_el, _w("r"))
    ins_text_el = ET.SubElement(ins_run, _w("t"))
    ins_text_el.set(f"{{{XML_NS}}}space", "preserve")
    ins_text_el.text = new_text


def apply_tracked_changes_inplace(
    docx_bytes: bytes,
    patches: list,
    *,
    author: str,
    timestamp_iso: str,
) -> InplaceResult:
    """
    Apply `patches` to `docx_bytes` in place: every zip entry except
    `word/document.xml` is byte-identical in the output; each patch's
    target paragraph (located by exact `source_text` match, see module
    docstring) is rewritten as a `<w:del>`/`<w:ins>` pair; a patch whose
    target cannot be safely located is skipped and reported in
    `InplaceResult.failed`, never guessed at (fail-closed, partial
    delivery -- one patch's failure does not block any other patch in the
    same call).

    Raises `ValueError` (before any locating/rewriting happens) if any
    patch's `new_text` is empty -- lists every offending anchor. Issue #260
    filters empty-`new_text` ("flag only") patches out upstream, so this is
    a caller-contract violation, not a normal input to fail closed on.
    """
    offending_anchors = [
        patch.get("anchor") for patch in patches if not patch.get("new_text")
    ]
    if offending_anchors:
        raise ValueError(
            "apply_tracked_changes_inplace requires a non-empty new_text "
            "for every patch (issue #260 filters empty-new_text patches "
            f"upstream); offending anchors: {offending_anchors!r}"
        )

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}

    original_document_xml = originals[DOCUMENT_PART].decode("utf-8")
    original_root_open_tag = _root_open_tag(original_document_xml)
    for prefix, uri in _declared_namespaces(original_root_open_tag):
        if prefix:
            ET.register_namespace(prefix, uri)

    root = ET.fromstring(originals[DOCUMENT_PART])
    body = root.find(_w("body"))

    next_id = _max_existing_id(root) + 1

    applied = []
    failed = []

    for patch in patches:
        anchor = patch["anchor"]
        source_text = patch["source_text"]
        new_text = patch["new_text"]

        # Locate is edge-whitespace-tolerant (issue #291 review finding 1):
        # in the real pipeline `source_text` is the NORMALIZED draft text
        # `extraction_normalization_stage.normalize_paragraphs` produces
        # (stripped -- see its final `" ".join(clean_texts).strip()`), while
        # `_paragraph_text` above reads the RAW, unstripped `<w:t>`
        # concatenation straight off the uploaded package. Comparing both
        # sides stripped closes that gap for the common case of a paragraph
        # whose own runs merely carry leading/trailing whitespace, without
        # weakening the "exact content, no fuzzy match" invariant -- the
        # text BETWEEN the edges is still compared character-for-character.
        normalized_source = (source_text or "").strip()
        matches = [
            p for p in _body_paragraphs(body) if _paragraph_text(p).strip() == normalized_source
        ]

        if len(matches) == 0:
            failed.append({"anchor": anchor, "reason": "not_found"})
            continue
        if len(matches) >= 2:
            failed.append({"anchor": anchor, "reason": "ambiguous"})
            continue

        matched_paragraph = matches[0]
        # Delete the paragraph's ACTUAL raw text -- including any edge
        # whitespace the stripped `normalized_source` above discarded for
        # matching purposes only -- so the `<w:delText>` faithfully reflects
        # what is actually being removed from the uploaded document, never
        # a lossy delete of the normalized proxy used to locate it.
        actual_source_text = _paragraph_text(matched_paragraph)

        del_id, ins_id = next_id, next_id + 1
        next_id += 2
        _rewrite_paragraph(
            matched_paragraph, actual_source_text, new_text, author, timestamp_iso, del_id, ins_id
        )
        applied.append(anchor)

    # Serialize the (mutated) tree, then splice the ORIGINAL root start tag
    # back in verbatim -- see module docstring, "Preserve", and the
    # `_declared_namespaces` block above: ElementTree's own serialization of
    # the root start tag would drop any xmlns declaration it considers
    # unused, so it is discarded and replaced with the literal original
    # text, which by construction carries every declaration untouched.
    serialized = ET.tostring(root, encoding="unicode")
    auto_root_open_tag = _root_open_tag(serialized)
    body_and_close = serialized[len(auto_root_open_tag) :]
    new_document_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + original_root_open_tag.encode("utf-8")
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

    return InplaceResult(docx_bytes=out_buf.getvalue(), applied=applied, failed=failed)


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """
    CLI smoke test: build a minimal 1-paragraph docx in memory with
    python-docx if available, else print a usage note. The gate test
    (tests/redline/test_inplace_patcher_core.py) is the authoritative
    check.
    """
    try:
        import docx  # noqa: F401 -- test-only convenience, not a hard dep
    except ImportError:
        print(
            "redline_inplace: no CLI smoke fixture available without "
            "python-docx (test-only dependency); run "
            "tests/redline/test_inplace_patcher_core.py for the "
            "authoritative check."
        )
        return

    document = docx.Document()
    document.add_paragraph("Each party's liability shall not exceed $150,000.")
    buf = io.BytesIO()
    document.save(buf)

    result = apply_tracked_changes_inplace(
        buf.getvalue(),
        [
            {
                "anchor": "sec-1",
                "source_text": "Each party's liability shall not exceed $150,000.",
                "new_text": "Each party's liability is uncapped.",
            }
        ],
        author="contract-toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    print(
        f"Applied: {result.applied}, failed: {result.failed}, "
        f"output size: {len(result.docx_bytes)} bytes"
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
