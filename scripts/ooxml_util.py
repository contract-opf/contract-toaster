#!/usr/bin/env python3
"""
Shared OOXML primitives: namespaces, part names, and the
root-namespace-preservation dance (issue #621, scope item 1).

Every module in this repo that rewrites `word/document.xml` does the same
dance: read the ORIGINAL root start tag out of the raw bytes, register the
namespace prefixes so `xml.etree.ElementTree` does not rename them, serialize
the mutated tree, then splice the original start tag back in (merged with any
namespace the serializer hoisted). That dance lived in the retired in-place
patcher and was reached into from four other modules (`redline_generate`,
`redline_block_apply`, `extraction_normalization_stage`, `tools/churn_docx.py`)
through its private, underscore-prefixed names.

Issue #621 added a fifth writer (`scripts/redline_block_apply.py`), so the
helpers moved here and every writer imports them from one place. The function
bodies are the originals, byte for byte, with only the exception-message
prefix renamed to `ooxml_util:` so a raised error names the module it actually
came from.

Issue #631 retired that in-place patcher itself (the anchor/hash patcher the
2026-07-22 LLM-native decision D3 replaced with the block-transcript
compiler). The four names its callers still reached for
that were NOT namespace helpers -- the `w:`/`xml:` namespace URIs, the
`word/document.xml` part name, the `_w()` qname helper and the
`_max_existing_id()` revision-id sweep -- moved here with it, unchanged, so
the surviving writers keep one shared definition of each instead of
re-declaring the same string constants five times.

## Why the dance is necessary at all

`xml.etree.ElementTree` discards `xmlns:*` bindings from `Element.attrib` at
parse time and, at serialize time, only re-declares a namespace it decides is
actually "used" by some tag or attribute it walks -- so a straight
`ET.fromstring` -> mutate -> `ET.tostring` round trip silently drops any root
xmlns declaration that isn't referenced by a qname ElementTree can see (e.g.
one referenced only inside an attribute VALUE such as
`mc:Ignorable="w14 wp14"`). The functions below read the root element's start
tag directly out of the original bytes -- never through ElementTree -- so it
can be spliced back in verbatim after serialization.

Usage:
    import ooxml_util

    original_open_tag = ooxml_util.root_open_tag(doc_xml_text)
    ooxml_util.register_declared_namespaces(
        ooxml_util.declared_namespaces_anywhere(doc_xml_text)
    )
    root = ET.fromstring(doc_xml_bytes)
    ...  # mutate
    serialized = ET.tostring(root, encoding="unicode")
    auto_open_tag = ooxml_util.root_open_tag(serialized)
    body_and_close = serialized[len(auto_open_tag):]
    open_tag = ooxml_util.merge_hoisted_namespaces(original_open_tag, auto_open_tag)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

_ATTR_RE = re.compile(r"([^\s=/>]+)\s*=\s*(\"[^\"]*\"|'[^']*')")


def scan_tag_end(text: str, start: int) -> int:
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
    raise ValueError("ooxml_util: malformed root start tag (no closing '>' found)")


def root_open_tag(xml_text: str) -> str:
    """Return the root element's start tag exactly as it appears in
    `xml_text` -- attribute order, quoting, and every `xmlns` declaration
    preserved verbatim -- skipping a leading XML declaration if present."""
    idx = 0
    if xml_text.startswith("<?"):
        idx = xml_text.index("?>") + 2
    start = xml_text.index("<", idx)
    end = scan_tag_end(xml_text, start)
    open_tag = xml_text[start : end + 1]
    if open_tag.endswith("/>"):
        raise ValueError(
            "ooxml_util: word/document.xml root element must not be self-closing"
        )
    return open_tag


def declared_namespaces(open_tag: str) -> list:
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


def register_declared_namespaces(namespace_pairs) -> None:
    """`ET.register_namespace` every `(prefix, uri)` pair that can be
    registered, skipping the ones that cannot.

    TWO prefixes must be skipped, for different reasons.

    The DEFAULT prefix (`""`, from `xmlns="uri"`): registering it would make
    that URI the default for every element that has no prefix, which is not
    what any caller here wants.

    ElementTree's RESERVED `ns<digits>` format: it reserves that pattern for
    its own auto-generated bindings and raises `ValueError` rather than
    registering one. Real uploaded documents -- especially any that have been
    round-tripped through another tool -- do carry such prefixes, so this must
    not be fatal. And it needn't be: registering only stops the serializer
    RENAMING a prefix on elements it writes. An unregistered prefix means the
    serializer picks its own for that URI and declares it on the root, which
    `merge_hoisted_namespaces` carries across; a genuine prefix/URI collision
    raises there rather than corrupting anything.

    This exists as one shared function rather than N copies of the same
    try/except because it was already TWO copies and one omission (issue
    #560): `redline_generate.inject_export_marker_and_footnotes` looped over
    the same pairs without the guard, and that single unguarded call raised on
    65% of a real 31-agreement corpus -- turning a document that had located
    every one of its patches into a review that produced nothing.
    """
    for prefix, uri in namespace_pairs:
        if not prefix:
            continue
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            continue


def declared_namespaces_anywhere(xml_text: str) -> list:
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


def merge_hoisted_namespaces(original_open_tag: str, auto_open_tag: str) -> str:
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
    original = declared_namespaces(original_open_tag)
    original_uri_by_prefix = {prefix: uri for prefix, uri in original}

    missing = []
    for prefix, uri in declared_namespaces(auto_open_tag):
        if prefix not in original_uri_by_prefix:
            missing.append((prefix, uri))
        elif original_uri_by_prefix[prefix] != uri:
            raise ValueError(
                f"ooxml_util: cannot preserve the original root tag -- "
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


# ---------------------------------------------------------------------------
# Namespaces, part names, qname helpers (issue #631: moved from the retired
# retired in-place patcher / standalone writer, unchanged)
# ---------------------------------------------------------------------------

#: The WordprocessingML main namespace -- the `w:` prefix on every element
#: this repo reads or writes inside `word/document.xml`.
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
#: The XML namespace, for `xml:space="preserve"` on text runs.
XML_NS = "http://www.w3.org/XML/1998/namespace"
#: The officeDocument relationships namespace, for `r:id` and the
#: `.../relationships/<kind>` relationship-type URIs.
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

ET.register_namespace("w", WORD_NS)
ET.register_namespace("r", REL_NS)

#: The one part every writer in this repo rewrites.
DOCUMENT_PART = "word/document.xml"


def w(tag: str) -> str:
    """`tag` as a WordprocessingML qname, e.g. ``w("p")``."""
    return f"{{{WORD_NS}}}{tag}"


def r(tag: str) -> str:
    """`tag` as an officeDocument-relationships qname, e.g. ``r("id")``."""
    return f"{{{REL_NS}}}{tag}"


def max_existing_id(root: ET.Element) -> int:
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

    It is NOT a comment-id allocator, and any code that authors its own
    `<w:comment>` elements must not reuse it as one: it reads only the parsed
    `word/document.xml`, so a comment id that exists in `word/comments.xml`
    would be invisible to it. Allocating an authored comment id from this max
    would be reading the wrong part.
    """
    max_id = 0
    for el in root.iter():
        val = el.get(w("id"))
        if val is None:
            continue
        try:
            max_id = max(max_id, int(val))
        except ValueError:
            continue
    return max_id
