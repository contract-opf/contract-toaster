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

Not covered: a comment anchored inside the counterparty's own `<w:ins>` /
`<w:del>`. An inbound document with pending tracked changes goes through
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

#: shape name -> (one-line description, builder name). The acceptance harness
#: reads the keys for its PASS line.
SHAPES: dict[str, tuple[str, str]] = {
    "WORD_STANDARD": ("rPr + reference run on an edited paragraph", "_build_word_standard"),
    "ANNOTATION_REF": ("rPr + annotationRef + reference in one run", "_build_annotation_ref"),
    "NESTED_RANGES": ("two comments on one run, one range inside the other", "_build_nested"),
    "UNTOUCHED": ("a comment on a paragraph the redline does not patch", "_build_untouched"),
    "MIXED_RUN": ("text and reference in one run on an edited paragraph", "_build_mixed_run"),
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


# ---------------------------------------------------------------------------
# Reading a document
# ---------------------------------------------------------------------------


def _document_root(docx_bytes: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        return ET.fromstring(zf.read("word/document.xml"))  # noqa: S314


def _anchor_order(docx_bytes: bytes) -> dict[str, list[tuple[int, str]]]:
    """Comment id -> its anchors as (document position, kind), in document order."""
    root = _document_root(docx_bytes)
    anchors: dict[str, list[tuple[int, str]]] = {}
    for position, node in enumerate(root.iter()):
        kind = {
            _qn("commentRangeStart"): "start",
            _qn("commentRangeEnd"): "end",
            _qn("commentReference"): "reference",
        }.get(node.tag)
        if kind is not None:
            anchors.setdefault(node.get(_qn("id"), "?"), []).append((position, kind))
    return anchors


def _comment_bodies(docx_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        if "word/comments.xml" not in zf.namelist():
            return {}
        root = ET.fromstring(zf.read("word/comments.xml"))  # noqa: S314
    return {
        comment.get(_qn("id"), "?"): "".join(t.text or "" for t in comment.iter(_qn("t")))
        for comment in root.iter(_qn("comment"))
    }


def _reject_all_text(docx_bytes: bytes) -> list[str]:
    """Each paragraph's text with the redline rejected: `<w:ins>` dropped,
    `<w:delText>` kept. Equal to the inbound text iff nothing was duplicated or
    lost by splitting a run."""
    root = _document_root(docx_bytes)
    paragraphs = []
    for paragraph in root.iter(_qn("p")):
        parts: list[str] = []

        def walk(node: ET.Element, parts: list[str] = parts) -> None:
            for child in node:
                if child.tag == _qn("ins"):
                    continue
                if child.tag in (_qn("t"), _qn("delText")):
                    parts.append(child.text or "")
                else:
                    walk(child)

        walk(paragraph)
        paragraphs.append("".join(parts))
    # A paragraph the redline INSERTED is empty once its `<w:ins>` is rejected;
    # drop empties so only the inbound paragraphs are compared.
    return [text for text in paragraphs if text]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def check_shape(name: str) -> list[str]:
    """Failures for one run shape: empty means every inbound comment survived."""
    h = _harness()
    _, builder_name = SHAPES[name]
    source = globals()[builder_name]()
    failures: list[str] = []
    want_anchors = _anchor_order(source)
    want_bodies = _comment_bodies(source)
    if not want_anchors or not want_bodies:
        return [f"[{name}] fixture precondition broken: the inbound document carries no comment"]

    block_ids = h._block_ids(source)
    result = h._run_block_mode(h._reconciled(h._multi_edit_response(block_ids)), source)
    if result["status"] != "OK" or not result.get("docx_bytes"):
        return [
            f"[{name}] the redline did not deliver (status={result['status']!r}, "
            f"reason={result.get('reason')!r}): comment preservation cannot be checked"
        ]
    delivered = result["docx_bytes"]

    got_anchors = _anchor_order(delivered)
    for comment_id, want in want_anchors.items():
        got = got_anchors.get(comment_id, [])
        if [kind for _, kind in sorted(got)] != [kind for _, kind in sorted(want)]:
            failures.append(
                f"[{name}] comment {comment_id}: anchors {[k for _, k in sorted(got)]} "
                f"in the output, {[k for _, k in sorted(want)]} in the input (orphaned or duplicated)"
            )
            continue
        order = [kind for _, kind in sorted(got)]
        if order.index("start") > order.index("end"):
            failures.append(f"[{name}] comment {comment_id}: its range ends before it starts")
    extra = set(got_anchors) - set(want_anchors)
    if extra:
        failures.append(f"[{name}] the output carries anchors for comments the input never had: {sorted(extra)}")

    got_bodies = _comment_bodies(delivered)
    if got_bodies != want_bodies:
        failures.append(
            f"[{name}] comment bodies changed: input {sorted(want_bodies.items())}, "
            f"output {sorted(got_bodies.items())}"
        )

    if _reject_all_text(delivered) != _reject_all_text(source):
        failures.append(
            f"[{name}] rejecting every tracked change does not give back the inbound text "
            "(a split run duplicated or dropped text)"
        )
    return failures


def run_all() -> list[str]:
    failures: list[str] = []
    for name in SHAPES:
        failures.extend(check_shape(name))
    return failures


def _mutation_is_detected() -> list[str]:
    """The gate must be able to fail: drop one anchor from a delivered document
    and `check_shape`'s comparison must notice. Guards against a gate that
    compares a document with itself."""
    h = _harness()
    source = _build_word_standard()
    result = h._run_block_mode(h._reconciled(h._multi_edit_response(h._block_ids(source))), source)
    delivered = result["docx_bytes"]
    stripped = _rewrite_document_xml(
        delivered, lambda xml: re.sub(r"<w:commentRangeEnd\b[^>]*/>", "", xml)
    )
    want = [kind for _, kind in sorted(_anchor_order(source)["0"])]
    got = [kind for _, kind in sorted(_anchor_order(stripped).get("0", []))]
    if got == want:
        return ["mutation: deleting a commentRangeEnd was NOT detected by the anchor comparison"]
    return []


def main() -> int:
    failures = run_all() + _mutation_is_detected()
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print(f"PASS: inbound comment anchors and bodies survive the block redline across {len(SHAPES)} run shapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
