#!/usr/bin/env python3
"""Gate: pre-existing inbound margin comments survive the block-mode redline.

The counterparty's comments are THEIR work product. Losing one is silent data
loss in a legal document: nobody notices a comment that is not there.

This file used to gate the retired in-place patcher (`redline_inplace`), and
went with it. The review path now delivers through `redline_generate.
generate_redline_from_blocks` (the v3 block-transcript contract), and until
this file nothing asserted the same property for it. `scripts/opf_acceptance.py`
stage 8 (PRESERVE) imports this module, so it reports a real result again
instead of a `FileNotFoundError`.

## Why "word/comments.xml is unchanged" is NOT the test

A Word comment is TWO things: the body in `word/comments.xml`, and its ANCHORS
in `word/document.xml` (`<w:commentRangeStart>`, `<w:commentRangeEnd>` and a run
carrying `<w:commentReference>`). The redline rewrites `document.xml`. A comment
whose body survives but whose anchors are gone is ORPHANED: Word has nothing to
attach it to and it vanishes from the margin. So this gate asserts the ANCHORS
in the OUTPUT document, id by id, in document order, and the bodies by id and
text. (The comments part is re-serialised, so byte identity is neither expected
nor meaningful.)

## Fixtures: synthetic, and Word's real run shapes

Invented text only (the fixture the block-mode e2e test builds). The run shapes
are the ones Word emits:

  * WORD_STANDARD  -- `rPr` + `commentReference` in its own run, on a paragraph
                      the redline edits.
  * ANNOTATION_REF -- `rPr` + `annotationRef` + `commentReference` in one run.
  * NESTED_RANGES  -- two comments on the same run: one range enclosing the other.
  * UNTOUCHED      -- a comment on a paragraph the redline does not patch.
  * MIXED_RUN      -- text and `commentReference` in ONE run, on an edited
                      paragraph: the anchor must survive WITHOUT duplicating
                      the text.
  * STRUCK_SPAN    -- the comment is anchored on exactly the span the redline
                      strikes ("sixty (60)"), split into its own run.
  * SPANS_PARAGRAPHS -- one comment whose range starts in one edited paragraph
                      and ends in the next.

Not covered: a whole-block strike of a commented block (the real output wraps
the reference run in `<w:del>`), tab / line-break content, threaded replies
(`commentsExtended`), and a comment anchored inside the counterparty's own
`<w:ins>` / `<w:del>`. An inbound document with pending tracked changes goes through
normalisation first (`tests/test_normalize_pending_tracked_changes.py`), which
is a separate property.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: Fewer shapes than this means the gate was gutted, not that it passed.
MIN_SHAPES = 7

#: shape name -> (one-line description, builder name, True when the redline
#: patches the paragraph the comment starts in). The acceptance harness reads
#: the keys for its PASS line.
SHAPES: dict[str, tuple[str, str, bool]] = {
    "WORD_STANDARD": ("rPr + reference run on an edited paragraph", "_build_word_standard", True),
    "ANNOTATION_REF": ("rPr + annotationRef + reference in one run", "_build_annotation_ref", True),
    "NESTED_RANGES": ("two comments on one run, one range inside the other", "_build_nested", True),
    "UNTOUCHED": ("a comment on a paragraph the redline does not patch", "_build_untouched", False),
    "MIXED_RUN": ("text and reference in one run on an edited paragraph", "_build_mixed_run", True),
    "STRUCK_SPAN": ("a comment on exactly the span the redline strikes", "_build_struck_span", True),
    "SPANS_PARAGRAPHS": ("one range from an edited paragraph into the next", "_build_spans_paragraphs", True),
}


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _harness():
    """The block-mode e2e test's synthetic fixture and runner, imported as a
    library (its top level is import-safe; nothing runs on import)."""
    import test_block_mode_e2e as harness

    if harness.IMPORT_ERRORS:
        raise RuntimeError("; ".join(harness.IMPORT_ERRORS))
    return harness


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _document(comment_on: dict[str, list[str]]):
    """The e2e fixture's three body paragraphs, with a comment (or several) on
    the named ones. `comment_on` maps "p1" / "p2" / "notice" to comment texts."""
    import docx

    h = _harness()
    document = docx.Document()
    runs = {}
    document.add_paragraph("Section 1. Term and Fee", style="Heading 1")
    runs["p1"] = document.add_paragraph().add_run(h.TERM_P1)
    runs["p2"] = document.add_paragraph().add_run(h.TERM_P2)
    document.add_paragraph("Section 2. Notices", style="Heading 1")
    runs["notice"] = document.add_paragraph().add_run(h.NOTICE_P1)
    for key, texts in comment_on.items():
        for text in texts:
            document.add_comment(runs=[runs[key]], text=text, author="Other Side", initials="OS")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _rewrite_document_xml(docx_bytes: bytes, transform) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as src, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/document.xml":
                data = transform(data.decode("utf-8")).encode("utf-8")
            dst.writestr(item, data)
    return out.getvalue()


def _build_word_standard() -> bytes:
    return _document({"p1": ["Margin note on the term"]})


def _build_annotation_ref() -> bytes:
    def add_annotation_ref(xml: str) -> str:
        pattern = r'(<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>)(<w:commentReference )'
        new, count = re.subn(pattern, r"\1<w:annotationRef/>\2", xml)
        assert count == 1, f"expected one reference run to rewrite, found {count}"
        return new

    return _rewrite_document_xml(_document({"p1": ["Margin note on the term"]}), add_annotation_ref)


def _build_nested() -> bytes:
    return _document({"p1": ["Outer margin note", "Inner margin note"]})


def _build_untouched() -> bytes:
    return _document({"notice": ["Margin note on notices"]})


def _build_mixed_run() -> bytes:
    def merge_reference_into_text_run(xml: str) -> str:
        # start, <w:r>TEXT</w:r>, end, <w:r>rPr + reference</w:r>  becomes
        # start, <w:r>TEXT + reference</w:r>, end: text and reference in ONE run.
        pattern = (
            r'(<w:commentRangeStart w:id="(\d+)"/>)<w:r><w:t>([^<]*)</w:t></w:r>'
            r'(<w:commentRangeEnd w:id="\2"/>)<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
            r'(<w:commentReference w:id="\2"/>)</w:r>'
        )
        new, count = re.subn(pattern, r"\1<w:r><w:t>\3</w:t>\5</w:r>\4", xml)
        assert count == 1, f"expected one paragraph to rewrite, found {count}"
        return new

    return _rewrite_document_xml(_document({"p2": ["Margin note on the fee"]}), merge_reference_into_text_run)


def _build_struck_span() -> bytes:
    import docx

    h = _harness()
    prefix, struck = "The Term shall be ", "sixty (60)"
    assert h.TERM_P1.startswith(prefix + struck), "fixture drifted from the e2e document"
    document = docx.Document()
    document.add_paragraph("Section 1. Term and Fee", style="Heading 1")
    paragraph = document.add_paragraph()
    paragraph.add_run(prefix)
    struck_run = paragraph.add_run(struck)
    paragraph.add_run(h.TERM_P1[len(prefix) + len(struck) :])
    document.add_paragraph().add_run(h.TERM_P2)
    document.add_paragraph("Section 2. Notices", style="Heading 1")
    document.add_paragraph().add_run(h.NOTICE_P1)
    document.add_comment(runs=[struck_run], text="Margin note on the struck span", author="Other Side", initials="OS")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _build_spans_paragraphs() -> bytes:
    import docx

    h = _harness()
    document = docx.Document()
    document.add_paragraph("Section 1. Term and Fee", style="Heading 1")
    first = document.add_paragraph().add_run(h.TERM_P1)
    last = document.add_paragraph().add_run(h.TERM_P2)
    document.add_paragraph("Section 2. Notices", style="Heading 1")
    document.add_paragraph().add_run(h.NOTICE_P1)
    document.add_comment(runs=[first, last], text="Margin note across both", author="Other Side", initials="OS")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Reading a document
# ---------------------------------------------------------------------------


def _zip_part(docx_bytes: bytes, name: str) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return zf.read(name) if name in zf.namelist() else None


def _document_root(docx_bytes: bytes) -> ET.Element:
    return ET.fromstring(_zip_part(docx_bytes, "word/document.xml"))  # noqa: S314


_ANCHOR_KINDS = {
    _qn("commentRangeStart"): "start",
    _qn("commentRangeEnd"): "end",
    _qn("commentReference"): "reference",
}


def _rejected_text(node: ET.Element) -> str:
    """The text under `node` with the redline rejected: `<w:ins>` dropped,
    `<w:delText>` kept. Equal to the inbound text iff nothing was duplicated or
    lost by splitting a run."""
    parts: list[str] = []

    def walk(current: ET.Element) -> None:
        for child in current:
            if child.tag == _qn("ins"):
                continue
            if child.tag in (_qn("t"), _qn("delText")):
                parts.append(child.text or "")
            else:
                walk(child)

    walk(node)
    return "".join(parts)


class _Anchors:
    """One pass over `document.xml` in document order."""

    def __init__(self, docx_bytes: bytes) -> None:
        root = _document_root(docx_bytes)
        parents = {child: parent for parent in root.iter() for child in parent}
        paragraph_text: dict[ET.Element, str] = {}
        #: (kind, id, rejected text of the paragraph the anchor sits in)
        self.events: list[tuple[str, str, str]] = []
        #: id -> the text its range covers, redline rejected
        self.covered: dict[str, str] = {}
        #: anchors that sit inside a tracked insertion
        self.inside_insertion: list[tuple[str, str]] = []
        #: id -> True when the paragraph its range starts in carries an edit
        self.start_paragraph_edited: dict[str, bool] = {}
        self.has_insertion = False
        self.has_deletion = False
        open_ids: list[str] = []
        inside_ins_depth = 0

        def inside_insertion(node: ET.Element) -> bool:
            while node in parents:
                node = parents[node]
                if node.tag == _qn("ins"):
                    return True
            return False

        def containing_paragraph(node: ET.Element) -> ET.Element | None:
            while node in parents:
                node = parents[node]
                if node.tag == _qn("p"):
                    return node
            return None

        for node in root.iter():
            if node.tag == _qn("ins"):
                self.has_insertion = True
            elif node.tag == _qn("del"):
                self.has_deletion = True
            kind = _ANCHOR_KINDS.get(node.tag)
            if kind is not None:
                comment_id = node.get(_qn("id"), "?")
                paragraph = containing_paragraph(node)
                if paragraph is not None and paragraph not in paragraph_text:
                    paragraph_text[paragraph] = _rejected_text(paragraph)
                self.events.append((kind, comment_id, paragraph_text.get(paragraph, "")))
                if inside_insertion(node):
                    self.inside_insertion.append((kind, comment_id))
                if kind == "start":
                    open_ids.append(comment_id)
                    self.covered.setdefault(comment_id, "")
                    self.start_paragraph_edited[comment_id] = paragraph is not None and any(
                        n.tag in (_qn("ins"), _qn("del")) for n in paragraph.iter()
                    )
                elif kind == "end" and comment_id in open_ids:
                    open_ids.remove(comment_id)
            elif node.tag in (_qn("t"), _qn("delText")) and open_ids and not inside_insertion(node):
                for comment_id in open_ids:
                    self.covered[comment_id] += node.text or ""
        del inside_ins_depth


def _comments(docx_bytes: bytes) -> dict[str, tuple[str, str, str]]:
    """Comment id -> (text, author, initials)."""
    part = _zip_part(docx_bytes, "word/comments.xml")
    if part is None:
        return {}
    root = ET.fromstring(part)  # noqa: S314
    return {
        comment.get(_qn("id"), "?"): (
            "".join(t.text or "" for t in comment.iter(_qn("t"))),
            comment.get(_qn("author"), ""),
            comment.get(_qn("initials"), ""),
        )
        for comment in root.iter(_qn("comment"))
    }


def _comments_wiring(docx_bytes: bytes) -> tuple[bool, bool]:
    """(the document's relationships name a comments part, the content types
    declare it): the two things that make Word load the comments part at all."""
    rels = (_zip_part(docx_bytes, "word/_rels/document.xml.rels") or b"").decode("utf-8", "replace")
    types = (_zip_part(docx_bytes, "[Content_Types].xml") or b"").decode("utf-8", "replace")
    return ("/comments" in rels, "comments+xml" in types)


def _reject_all_text(docx_bytes: bytes) -> list[str]:
    """Each paragraph's text with the redline rejected. A paragraph the redline
    INSERTED is empty once its `<w:ins>` is rejected, so empties are dropped and
    only the inbound paragraphs are compared (a deleted inbound paragraph keeps
    its `<w:delText>`, so it is not mistaken for an inserted one)."""
    return [t for t in (_rejected_text(p) for p in _document_root(docx_bytes).iter(_qn("p"))) if t]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

#: shape name -> (source bytes, delivered bytes, result). The redline is the
#: slow part (about 3 s), so each shape is delivered once and the mutation
#: checks reuse it.
_DELIVERED: dict[str, tuple[bytes, dict]] = {}


def _deliver(name: str) -> tuple[bytes, dict]:
    if name not in _DELIVERED:
        h = _harness()
        builder_name = SHAPES[name][1]
        source = globals()[builder_name]()
        result = h._run_block_mode(h._reconciled(h._multi_edit_response(h._block_ids(source))), source)
        _DELIVERED[name] = (source, result)
    return _DELIVERED[name]


def check_shape(name: str, mutate=None) -> list[str]:
    """Failures for one run shape: empty means every inbound comment survived.
    `mutate(delivered, source) -> bytes` corrupts the delivered document first;
    `mutation_failures` uses it to prove each category can fail."""
    edited = SHAPES[name][2]
    source, result = _deliver(name)
    failures: list[str] = []
    want = _Anchors(source)
    want_comments = _comments(source)
    if not want.events or not want_comments:
        return [f"[{name}] fixture precondition broken: the inbound document carries no comment"]
    if result["status"] != "OK" or not result.get("docx_bytes"):
        return [
            f"[{name}] [edit] the redline did not deliver (status={result['status']!r}, "
            f"reason={result.get('reason')!r}): comment preservation cannot be checked"
        ]
    delivered = result["docx_bytes"]
    if mutate is not None:
        delivered = mutate(delivered, source)

    got = _Anchors(delivered)

    # [edit] the redline must have done something to the document.
    report = result.get("analysis_report")
    if report and "issue_changeset_failed" in repr(report):
        failures.append(f"[{name}] [edit] the redline reported a failed issue changeset: {report!r}")
    if not (got.has_insertion and got.has_deletion):
        failures.append(f"[{name}] [edit] the delivered document carries no tracked insertion and deletion")
    if edited:
        for comment_id, was_edited in got.start_paragraph_edited.items():
            if not was_edited:
                failures.append(
                    f"[{name}] [edit] comment {comment_id}: the paragraph its range starts in was "
                    "not edited, so preservation across an edit is unproven"
                )

    # [anchors] sequence + paragraph context, covered text, nothing under <w:ins>.
    if got.events != want.events:
        failures.append(
            f"[{name}] [anchors] the anchor sequence changed (orphaned, duplicated, cross-wired or "
            f"moved to another paragraph): input {[(k, i) for k, i, _ in want.events]}, "
            f"output {[(k, i) for k, i, _ in got.events]}"
        )
    for comment_id, text in want.covered.items():
        if got.covered.get(comment_id) != text:
            failures.append(
                f"[{name}] [anchors] comment {comment_id}: its range covers {got.covered.get(comment_id)!r} "
                f"in the output, {text!r} in the input"
            )
    if got.inside_insertion:
        failures.append(
            f"[{name}] [anchors] anchors inside a tracked insertion (rejecting the redline would delete "
            f"the comment): {got.inside_insertion}"
        )

    # [bodies] text, author, initials, and the part wiring.
    got_comments = _comments(delivered)
    if got_comments != want_comments:
        failures.append(
            f"[{name}] [bodies] comments changed: input {sorted(want_comments.items())}, "
            f"output {sorted(got_comments.items())}"
        )
    if _comments_wiring(delivered) != _comments_wiring(source):
        failures.append(f"[{name}] [bodies] the comments relationship or content type was lost")

    # [reject-all]
    if _reject_all_text(delivered) != _reject_all_text(source):
        failures.append(
            f"[{name}] [reject-all] rejecting every tracked change does not give back the inbound text "
            "(a split run duplicated or dropped text)"
        )
    return failures


def run_all() -> list[str]:
    """Every shape through the real redline, then proof the gate can fail."""
    if len(SHAPES) < MIN_SHAPES:
        return [f"gate gutted: {len(SHAPES)} run shapes configured, at least {MIN_SHAPES} required"]
    failures: list[str] = []
    for name in SHAPES:
        failures.extend(check_shape(name))
    return failures + mutation_failures()


# ---------------------------------------------------------------------------
# Proof that the gate can fail
# ---------------------------------------------------------------------------


def _rewrite_part(docx_bytes: bytes, part: str, transform) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == part:
                data = transform(data.decode("utf-8")).encode("utf-8")
            dst.writestr(item, data)
    return out.getvalue()


def _doc_xml(transform):
    return lambda delivered, _source: _rewrite_part(delivered, "word/document.xml", transform)


# The serialiser writes `<w:commentRangeStart w:id="0" />` (a space before `/>`),
# so every pattern below tolerates it. `_changed` makes a mutation that did not
# apply (a pattern that matched nothing) fail loudly instead of "passing".
def _changed(before: str, after: str, what: str) -> str:
    if before == after:
        raise AssertionError(f"mutation did not apply: {what}")
    return after


def _swap_start_ids(xml: str) -> str:
    """Cross-wire two comments: a body ends up pointing at the other's range."""
    start = r'<w:commentRangeStart w:id="(\d)"\s*/>'
    swapped = re.sub(start, lambda m: f'<w:commentRangeStart w:id="{1 - int(m.group(1))}"/>', xml)
    return _changed(xml, swapped, "swap the two range-start ids")


def _collapse_range(xml: str) -> str:
    collapsed = re.sub(
        r'(<w:commentRangeStart w:id="0"\s*/>)(.*?)(<w:commentRangeEnd w:id="0"\s*/>)',
        r"\1\3\2",
        xml,
        count=1,
        flags=re.S,
    )
    return _changed(xml, collapsed, "collapse the range")


def _wrap_reference_in_insertion(xml: str) -> str:
    pattern = r'(<w:r(?: [^>]*)?>(?:(?!</w:r>).)*?<w:commentReference w:id="0"\s*/>.*?</w:r>)'
    wrapped = re.sub(
        pattern, r'<w:ins w:id="9999" w:author="X" w:date="2026-01-01T00:00:00Z">\1</w:ins>', xml, count=1, flags=re.S
    )
    return _changed(xml, wrapped, "wrap the reference run")


def _drop_comments_relationship(xml: str) -> str:
    dropped = re.sub(r'<(?:\w+:)?Relationship\b[^>]*/comments"[^>]*/>', "", xml)
    return _changed(xml, dropped, "drop the comments relationship")


def _drop_comment_body(xml: str) -> str:
    return _changed(xml, re.sub(r"<w:comment\b.*?</w:comment>", "", xml, flags=re.S), "drop a comment body")


def _overwrite_author(xml: str) -> str:
    return _changed(xml, xml.replace('w:author="Other Side"', 'w:author="X"'), "overwrite the author")


#: (label, shape, corruption, the category that must fire).
MUTATIONS = [
    ("strip every anchor", "WORD_STANDARD",
     _doc_xml(lambda x: _changed(x, re.sub(r"<w:commentRange(?:Start|End)\b[^>]*/>|<w:commentReference\b[^>]*/>", "", x), "strip anchors")),
     "[anchors]"),
    ("drop one commentRangeEnd", "WORD_STANDARD",
     _doc_xml(lambda x: _changed(x, re.sub(r"<w:commentRangeEnd\b[^>]*/>", "", x, count=1), "drop a range end")),
     "[anchors]"),
    ("collapse the range to nothing", "WORD_STANDARD", _doc_xml(_collapse_range), "[anchors]"),
    ("cross-wire two comments' ids", "NESTED_RANGES", _doc_xml(_swap_start_ids), "[anchors]"),
    ("wrap the reference run in a tracked insertion", "WORD_STANDARD",
     _doc_xml(_wrap_reference_in_insertion), "[anchors]"),
    ("deliver the inbound bytes (the redline did nothing)", "WORD_STANDARD", lambda _delivered, source: source, "[edit]"),
    ("delete a comment body", "WORD_STANDARD",
     lambda d, _s: _rewrite_part(d, "word/comments.xml", _drop_comment_body), "[bodies]"),
    ("overwrite a comment's author", "WORD_STANDARD",
     lambda d, _s: _rewrite_part(d, "word/comments.xml", _overwrite_author), "[bodies]"),
    ("drop the comments relationship", "WORD_STANDARD",
     lambda d, _s: _rewrite_part(d, "word/_rels/document.xml.rels", _drop_comments_relationship), "[bodies]"),
    ("change the deleted text", "WORD_STANDARD",
     _doc_xml(lambda x: _changed(x, re.sub(r"(<w:delText[^>]*>)[^<]*(</w:delText>)", r"\1ZZZ\2", x), "alter deleted text")),
     "[reject-all]"),
]


def mutation_failures() -> list[str]:
    """Each corruption must make the SAME `check_shape` fail with its category.
    A mutation nothing notices means the gate compares a document with itself."""
    failures: list[str] = []
    for label, shape, corrupt, tag in MUTATIONS:
        fired = check_shape(shape, mutate=corrupt)
        if not any(tag in failure for failure in fired):
            failures.append(f"mutation not detected: {label!r} on {shape} did not raise a {tag} failure")
    return failures


def test_inbound_comments_survive_the_block_redline() -> None:
    assert run_all() == []


def main() -> int:
    failures = run_all()
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print(f"PASS: inbound comment anchors and bodies survive the block redline across {len(SHAPES)} run shapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
