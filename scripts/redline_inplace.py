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

The paragraph's inbound COMMENT ANCHORS are the one thing carried across the
rewrite: `<w:commentRangeStart>` re-emitted before the `<w:del>`,
`<w:commentRangeEnd>` and the `<w:commentReference>`'s run after the
`<w:ins>`, so each comment still spans the text it was written about. They
are collected at every nesting depth (a comment on a clause the counterparty
also edited sits inside their `<w:ins>`/`<w:del>`, not at the top level of the
`<w:p>`). The anchors are half of a comment -- the body in
`word/comments.xml` is the other half, and preserving that part alone leaves
an ORPHANED comment that vanishes from Word's margin. See `_rewrite_paragraph`
and `InplaceResult.orphaned_comments`.

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
from collections import Counter
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

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


_XMLNS_RE = re.compile(r"\sxmlns(?::([A-Za-z_][\w.-]*))?\s*=\s*(\"[^\"]*\"|'[^']*')")


def _declared_namespaces_anywhere(xml_text: str) -> list:
    """`(prefix, uri)` for every `xmlns[:prefix]` declaration in `xml_text`,
    wherever it appears -- root or not. Order-preserving and de-duplicated on
    the pair, so a prefix legitimately rebound on different subtrees still
    yields both bindings (and `register_namespace`, last-write-wins, keeps the
    final one -- the serializer's own behaviour anyway)."""
    out = []
    seen = set()
    for match in _XMLNS_RE.finditer(xml_text):
        prefix = match.group(1) or ""
        uri = match.group(2)[1:-1]
        if (prefix, uri) not in seen:
            seen.add((prefix, uri))
            out.append((prefix, uri))
    return out


def _merge_hoisted_namespaces(original_open_tag: str, auto_open_tag: str) -> str:
    """Return `original_open_tag` plus any `xmlns` declaration that appears on
    `auto_open_tag` but not on the original.

    The splice above keeps the ORIGINAL root start tag because ElementTree
    drops declarations it cannot see being used. The reverse case exists too:
    a real Word document may declare a prefix on a NON-root element (`a` /
    `a14`, on a `<w:drawing>` subtree), and ElementTree HOISTS those bindings
    to the root when it serializes. Splicing the original tag over that output
    drops the hoisted declaration while the body still uses the prefix, so
    `word/document.xml` comes back with an unbound prefix -- not well-formed.
    Merging the two keeps both properties: every original declaration byte-for-
    byte in its original order, and every binding the serialized body relies on.

    A prefix bound to different URIs by the two tags is unmergeable: keeping
    the original silently rebinds every use of that prefix in the body. That is
    a corrupt document with a plausible shape, so it raises instead.
    """
    original = _declared_namespaces(original_open_tag)
    original_uri_by_prefix = {prefix: uri for prefix, uri in original}

    missing = []
    for prefix, uri in _declared_namespaces(auto_open_tag):
        if prefix not in original_uri_by_prefix:
            missing.append((prefix, uri))
        elif original_uri_by_prefix[prefix] != uri:
            raise ValueError(
                f"redline_inplace: cannot preserve the original root tag -- "
                f"prefix {prefix!r} is bound to {original_uri_by_prefix[prefix]!r} "
                f"on the original root but to {uri!r} on the serialized output. "
                f"Splicing the original would silently rebind every use of "
                f"{prefix!r} in the document body."
            )

    if not missing:
        return original_open_tag

    additions = "".join(
        f" xmlns={quoteattr(uri)}" if prefix == "" else f" xmlns:{prefix}={quoteattr(uri)}"
        for prefix, uri in missing
    )
    return original_open_tag[:-1].rstrip() + additions + ">"


@dataclass
class InplaceResult:
    """Result of `apply_tracked_changes_inplace`.

    `docx_bytes`: the rewritten document (every part but `word/document.xml`
    byte-identical to the input). `applied`: anchors whose patch was located
    and rewritten. `failed`: `{"anchor": ..., "reason": "not_found" |
    "ambiguous"}` for every patch that could not be safely targeted.

    `orphaned_comments`: `{"anchor": ..., "comment_id": ..., "tag": ...}` for
    every inbound comment anchor a rewrite could not carry across. Its body
    still sits in `word/comments.xml`, but Word has nothing left to attach it
    to, so it disappears from the margin -- silent loss of the counterparty's
    own work product in a legal document. Empty for every run shape seen so
    far; it exists so that if that ever stops being true, the loss is
    OBSERVABLE to the caller instead of invisible.
    """

    docx_bytes: bytes
    applied: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    orphaned_comments: list = field(default_factory=list)


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
    upload already carries.

    This deliberately does NOT distinguish id SPACES. OOXML gives revisions
    (`w:ins`/`w:del`) and comments (`w:comment` and its anchors) independent
    `w:id` counters, and this sweep takes the max across both: a document whose
    only `w:id` is a comment's 41 pushes the next revision id to 42. That is
    merely conservative for revision ids -- it skips values, never collides --
    and it is why the sweep is safe today.

    It is NOT a comment-id allocator, and PR G2 (which authors our own
    `<w:comment>` elements) must not reuse it as one: it reads only the parsed
    `word/document.xml`, so a comment id that exists in `word/comments.xml`
    would be invisible to it. Allocating an authored comment id from this max
    would be reading the wrong part.
    """
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


# The three elements that, together, attach ONE comment to a span of text.
# `word/comments.xml` holds the body; these hold the attachment. A comment
# needs all three to render as a margin bubble -- a body with no anchors is
# orphaned and vanishes silently.
COMMENT_ANCHOR_TAGS = (
    _w("commentRangeStart"),
    _w("commentRangeEnd"),
    _w("commentReference"),
)

# Children a `<w:commentReference>`'s run may carry while still contributing
# ZERO text to the paragraph: `<w:rPr>` is formatting (Word's own
# "CommentReference" character style) and `<w:annotationRef>` is the mark Word
# draws in the margin. Neither holds a character of document text, so a run
# built only from these plus the reference itself is safe to re-emit verbatim
# beside the `<w:del>`/`<w:ins>` -- it duplicates nothing. This is the SHAPE
# Word actually writes (`<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
# <w:commentReference w:id="2"/></w:r>`); a bare reference-only run is the rare
# case, not the common one.
_TEXTLESS_RUN_CHILDREN = frozenset(
    {_w("rPr"), _w("annotationRef"), _w("commentReference")}
)


def _anchor_inventory(p: ET.Element) -> Counter:
    """`(tag, w:id)` -> count for every comment anchor anywhere under `p`.

    Taken before and after a rewrite and diffed, this is what makes a lost
    anchor LOUD instead of silent (see `_rewrite_paragraph`): it is measured
    off the resulting tree rather than inferred from the emit logic, so it
    cannot drift out of sync with that logic the way a hand-maintained
    "did we handle this case?" branch can.
    """
    return Counter(
        (el.tag, el.get(_w("id"))) for el in p.iter() if el.tag in COMMENT_ANCHOR_TAGS
    )


def _synthesized_reference_run(reference: ET.Element) -> ET.Element:
    """A fresh `<w:r><w:commentReference w:id="N"/></w:r>` carrying only the
    anchor from `reference`.

    For a genuinely MIXED run -- one that holds a `<w:commentReference>`
    alongside real content (a `<w:t>`, a `<w:delText>`, a drawing, a tab) --
    re-emitting the run verbatim beside the `<w:del>`/`<w:ins>` would duplicate
    that content into the redlined paragraph. Dropping the run instead would
    orphan the comment. Neither is necessary: the reference element itself
    carries no text, so it is re-emitted alone in a run of its own. The text
    stays in the `<w:del>` exactly once and the comment survives.
    """
    run = ET.Element(_w("r"))
    new_reference = ET.SubElement(run, _w("commentReference"))
    reference_id = reference.get(_w("id"))
    if reference_id is not None:
        new_reference.set(_w("id"), reference_id)
    return run


def _collect_comment_anchors(p: ET.Element) -> tuple:
    """Every comment anchor under `p`, at ANY nesting depth, split into
    `(starts, ends)` -- the anchors to re-emit BEFORE the rewritten text and
    the ones to re-emit AFTER it.

    Depth matters: `.iter()`, not `list(p)`. A counterparty who comments on a
    clause they also edited leaves the anchor nested inside the `<w:ins>` or
    `<w:del>` of their own tracked change (or inside a `<w:hyperlink>` or
    `<w:sdt>`), not as a direct child of the `<w:p>`. Tracked-change-PLUS-comment
    is the NORMAL case, so a top-level-only scan misses precisely the anchors
    most likely to be there.

    Every `<w:commentRangeStart>` goes before the `<w:del>` and every
    `<w:commentRangeEnd>` after the `<w:ins>`, regardless of where each sat
    among the original runs. The rewrite collapses all of the paragraph's text
    into that one del/ins pair, so this is the placement that keeps each range
    spanning the text it was written about. Bracketing them this way is also
    what stops a SECOND comment in the same paragraph from collapsing: keeping
    a range's start in its original position relative to the runs would leave
    it with no content between start and end, and Word drops a zero-width
    range.
    """
    parents = {child: parent for parent in p.iter() for child in parent}
    starts: list = []
    ends: list = []
    emitted_runs: set = set()

    for el in p.iter():
        if el.tag == _w("commentRangeStart"):
            starts.append(el)
        elif el.tag == _w("commentRangeEnd"):
            ends.append(el)
        elif el.tag == _w("commentReference"):
            run = parents.get(el)
            if (
                run is not None
                and run.tag == _w("r")
                and all(kid.tag in _TEXTLESS_RUN_CHILDREN for kid in run)
            ):
                # Textless: safe to carry the whole run across, rPr and all.
                if id(run) not in emitted_runs:
                    emitted_runs.add(id(run))
                    ends.append(run)
            else:
                ends.append(_synthesized_reference_run(el))

    return starts, ends


def _rewrite_paragraph(
    p: ET.Element,
    source_text: str,
    new_text: str,
    author: str,
    timestamp_iso: str,
    del_id: int,
    ins_id: int,
) -> list:
    """Replace `p`'s run children with exactly one `<w:del>` (delText =
    `source_text`) followed by one `<w:ins>` (t = `new_text`), leaving
    `<w:pPr>` (if present) untouched and re-emitting every pre-existing COMMENT
    ANCHOR around the del/ins pair. Returns a list of anchors that could NOT be
    re-emitted (empty in every shape seen so far) -- see below.

    Comment anchors are preserved deliberately. A counterparty's margin comment
    is two things: its body in `word/comments.xml` (which this module never
    touches, since it only mutates `word/document.xml`) and its anchors HERE --
    `<w:commentRangeStart>`, `<w:commentRangeEnd>`, and a run carrying
    `<w:commentReference>`. Dropping the anchors while the body survives leaves
    an ORPHANED comment: Word has nothing to attach it to and it disappears from
    the margin. So "comments.xml is byte-identical" is necessary and NOT
    sufficient -- the comment is still silently lost.

    This bites on the NORMAL case, not an exotic one: a counterparty comments on
    exactly the clause they edited, which is exactly the clause we redline.

    ANY run carrying a `<w:commentReference>` keeps its anchor. There is no
    trade-off to make against duplicating text: a textless run is re-emitted
    whole, and a run that mixes a reference with real content gets a
    synthesized reference-only run instead (`_synthesized_reference_run`), so
    the content stays in the `<w:del>` exactly once either way.

    Any anchor that still could not be re-emitted is returned, never dropped
    quietly: the caller records it on `InplaceResult.orphaned_comments` so a
    lost comment is observable rather than silent. The check is a before/after
    inventory of the actual tree (`_anchor_inventory`), so it stays honest even
    if a run shape nobody has seen yet slips past the logic above.
    """
    inventory_before = _anchor_inventory(p)
    ppr = p.find(_w("pPr"))
    anchors_before, anchors_after = _collect_comment_anchors(p)

    for child in list(p):
        if child is not ppr:
            p.remove(child)

    for anchor in anchors_before:
        p.append(anchor)

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

    for anchor in anchors_after:
        p.append(anchor)

    missing = inventory_before - _anchor_inventory(p)
    return [
        {"comment_id": comment_id, "tag": tag.split("}", 1)[-1]}
        for (tag, comment_id), count in missing.items()
        for _ in range(count)
    ]


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
    # Register every prefix the document declares ANYWHERE, not just on the
    # root: a prefix declared on a non-root element (`a`, inside a drawing) is
    # unregistered otherwise, so the serializer renames it to an
    # auto-generated one and rewrites that whole subtree for no reason. The
    # output would still be correct -- prefixes carry no meaning in XML, and
    # `_merge_hoisted_namespaces` binds whatever the serializer picked -- but
    # preserving the document as authored is this module's whole intent.
    for prefix, uri in _declared_namespaces_anywhere(original_document_xml):
        if not prefix:
            continue
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            # ElementTree reserves the `ns<digits>` prefix format for its own
            # auto-generated bindings and refuses to register one. Real
            # uploaded documents do carry such prefixes on the root, so this
            # must not be fatal -- and it isn't: registering only stops the
            # serializer RENAMING a prefix on the elements it writes, and the
            # root start tag is spliced back from the original bytes either
            # way. An unregistered prefix means the serializer picks its own
            # for that URI and declares it on the root, which
            # `_merge_hoisted_namespaces` then carries across; a genuine
            # prefix/URI collision raises there rather than corrupting.
            continue

    root = ET.fromstring(originals[DOCUMENT_PART])
    body = root.find(_w("body"))

    next_id = _max_existing_id(root) + 1

    applied = []
    failed = []
    orphaned_comments = []

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
        orphans = _rewrite_paragraph(
            matched_paragraph, actual_source_text, new_text, author, timestamp_iso, del_id, ins_id
        )
        orphaned_comments.extend(dict(orphan, anchor=anchor) for orphan in orphans)
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
    # ...but the original tag alone is not enough: a prefix declared on a
    # non-root element (`a` inside a `<w:drawing>`) is hoisted to the root by
    # the serializer, and the original tag has no such declaration to keep.
    # Merge those in, or the body references a prefix nothing binds.
    root_open_tag = _merge_hoisted_namespaces(original_root_open_tag, auto_root_open_tag)
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

    return InplaceResult(
        docx_bytes=out_buf.getvalue(),
        applied=applied,
        failed=failed,
        orphaned_comments=orphaned_comments,
    )


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
