#!/usr/bin/env python3
"""Gate: pre-existing inbound margin comments survive the redline untouched.

Work item 6(b) of the OPF 0.3 launch. The counterparty's comments are THEIR
work product. Losing one is silent data loss in a legal document -- nobody
notices a comment that isn't there.

## Why "word/comments.xml is byte-identical" is NOT the test

The obvious check -- the in-place patcher never rewrites `word/comments.xml` --
is true and insufficient. A Word comment is TWO things:

  1. the comment body, in `word/comments.xml` (a part the patcher byte-copies,
     because it only ever mutates `word/document.xml`); and
  2. its ANCHORS, inside `word/document.xml`: `<w:commentRangeStart>`,
     `<w:commentRangeEnd>`, and a run carrying `<w:commentReference>`.

`document.xml` IS rewritten. A comment whose body survives but whose anchors are
gone is ORPHANED: Word has nothing to attach it to and it disappears from the
margin. So a test that only diffs `comments.xml` passes while the comment
vanishes -- and it did: byte-identity reported True for a comment that was
already orphaned. This gate asserts the ANCHORS, in the output document. Nothing
here is proved by byte-identity.

## The fixtures are SYNTHETIC, and they reproduce Word's REAL run shapes

Invented parties only (Acme University / FixtureCorp). But the run SHAPES are
the ones measured off real counterparty documents, because the shape is exactly
what the first attempt at this got wrong. It preserved only a run whose sole
child is a `<w:commentReference>`; Word overwhelmingly writes

    <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
         <w:commentReference w:id="2"/></w:r>

-- two children -- so the real-world preservation rate was 3 in 85. Each shape
below is a case that rate was made of:

  * WORD_STANDARD  -- `rPr` + reference. 82 of 85 real anchors.
  * ANNOTATION_REF -- `rPr` + `annotationRef` + reference.
  * NESTED_IN_INS  -- anchor inside the counterparty's own `<w:ins>`.
  * NESTED_IN_DEL  -- anchor inside their `<w:del>`.
  * NESTED_IN_SDT  -- anchor inside a content control.
  * MIXED_RUN      -- text and reference in ONE run; the anchor must survive
                      WITHOUT duplicating the text.
  * TWO_COMMENTS   -- two comments on one paragraph; the second must not
                      collapse to a zero-width range.
  * NESTED_RANGES  -- one comment's range enclosing another's.
  * BARE_REFERENCE -- reference alone in its run. The 3 of 85.
  * UNTOUCHED      -- a comment on a paragraph we do not patch.

Tracked-change-PLUS-comment (NESTED_IN_INS / NESTED_IN_DEL) is not exotic: a
counterparty comments on exactly the clause they edited, which is exactly the
clause we redline. It is the NORMAL case.

The corpus-wide sweep that produced the 3/85 and 85/85 numbers is run against
the real documents under `docs/planning/`, which is excluded from the public
cut -- so it is a local verification, not a test, and nothing here depends on
it.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import redline_inplace  # noqa: E402

DOCUMENT_PART = "word/document.xml"
COMMENTS_PART = "word/comments.xml"
RELS_PART = "word/_rels/document.xml.rels"
CONTENT_TYPES_PART = "[Content_Types].xml"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


#: Comment id -> (shape name, paragraph text). One paragraph per shape; every
#: one but UNTOUCHED is patched, so every one but UNTOUCHED has its anchors
#: rewritten. Ids are the `w:id` on all three anchor elements AND on the
#: `<w:comment>` body, exactly as Word ties them together.
SHAPES = {
    0: ("WORD_STANDARD", "Acme University shall maintain commercial general liability coverage."),
    1: ("ANNOTATION_REF", "FixtureCorp shall provide a qualified on-site supervisor."),
    2: ("NESTED_IN_INS", "Each party shall carry professional liability insurance of $1,000,000."),
    3: ("NESTED_IN_DEL", "Notice under this Agreement shall be sent by certified mail. "),
    4: ("MIXED_RUN", "This Agreement may be terminated on thirty (30) days written notice."),
    5: ("TWO_COMMENTS", None),  # shares a paragraph with id 6
    6: ("TWO_COMMENTS", None),
    7: ("NESTED_RANGES", None),  # shares a paragraph with id 8
    8: ("NESTED_RANGES", None),
    9: ("BARE_REFERENCE", "Acme University shall not discriminate on any protected basis."),
    10: ("NESTED_IN_SDT", "The term of this Agreement is three (3) years from the Effective Date."),
    11: ("UNTOUCHED", "This Agreement is governed by the laws of the State of Fixtureland."),
}

#: Text of the two multi-comment paragraphs, split across the runs each comment
#: range covers.
TWO_COMMENTS_HALVES = ("Students placed by Acme University ", "shall remain enrolled throughout.")
NESTED_RANGES_PARTS = ("FixtureCorp shall indemnify ", "Acme University ", "for any third-party claim.")

#: The paragraph carrying MIXED_RUN puts this tail in the SAME run as the
#: reference. If that run were re-emitted whole beside the del/ins, this text
#: would appear twice in the redlined paragraph.
MIXED_RUN_TAIL = " No penalty shall apply."

PATCH_SUFFIX = " As amended by FixtureCorp."


def _reference_run(comment_id: int, shape: str) -> str:
    """The `<w:r>` carrying a `<w:commentReference>`, in the shape named."""
    if shape == "BARE_REFERENCE":
        return f'<w:r><w:commentReference w:id="{comment_id}"/></w:r>'
    if shape == "ANNOTATION_REF":
        return (
            '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
            f'<w:annotationRef/><w:commentReference w:id="{comment_id}"/></w:r>'
        )
    # Word's overwhelmingly common shape: rPr + reference.
    return (
        '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
        f'<w:commentReference w:id="{comment_id}"/></w:r>'
    )


def _text_run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _simple_commented_paragraph(comment_id: int, shape: str, text: str) -> str:
    """commentRangeStart, text, commentRangeEnd, reference run — as Word writes
    a comment on a whole paragraph."""
    return (
        "<w:p>"
        f'<w:commentRangeStart w:id="{comment_id}"/>'
        + _text_run(text)
        + f'<w:commentRangeEnd w:id="{comment_id}"/>'
        + _reference_run(comment_id, shape)
        + "</w:p>"
    )


def _paragraph_xml(comment_id: int) -> str:
    """The fixture paragraph for one shape."""
    shape, text = SHAPES[comment_id]

    if shape == "NESTED_IN_INS":
        # The counterparty inserted this clause AND commented on it: the whole
        # comment -- range end and reference included -- sits INSIDE their
        # <w:ins>, not at the top level of the <w:p>.
        return (
            "<w:p>"
            f'<w:commentRangeStart w:id="{comment_id}"/>'
            '<w:ins w:id="900" w:author="Counterparty Counsel" w:date="2026-01-01T00:00:00Z">'
            + _text_run(text)
            + f'<w:commentRangeEnd w:id="{comment_id}"/>'
            + _reference_run(comment_id, shape)
            + "</w:ins></w:p>"
        )

    if shape == "NESTED_IN_DEL":
        # They struck a sentence and commented on the strike. Note the struck
        # text is <w:delText>, so it is NOT part of the paragraph's <w:t> text
        # and the patch locates on the kept text alone.
        return (
            "<w:p>"
            f'<w:commentRangeStart w:id="{comment_id}"/>'
            + _text_run(text)
            + '<w:del w:id="901" w:author="Counterparty Counsel" w:date="2026-01-01T00:00:00Z">'
            '<w:r><w:delText xml:space="preserve">Struck by the counterparty.</w:delText></w:r>'
            + f'<w:commentRangeEnd w:id="{comment_id}"/>'
            + _reference_run(comment_id, shape)
            + "</w:del></w:p>"
        )

    if shape == "NESTED_IN_SDT":
        return (
            "<w:p>"
            f'<w:commentRangeStart w:id="{comment_id}"/>'
            "<w:sdt><w:sdtContent>"
            + _text_run(text)
            + f'<w:commentRangeEnd w:id="{comment_id}"/>'
            + _reference_run(comment_id, shape)
            + "</w:sdtContent></w:sdt></w:p>"
        )

    if shape == "MIXED_RUN":
        # Text and reference in ONE run. Re-emitting the run whole would
        # duplicate MIXED_RUN_TAIL into the redlined paragraph; dropping it
        # would orphan the comment. Neither is acceptable.
        return (
            "<w:p>"
            f'<w:commentRangeStart w:id="{comment_id}"/>'
            + _text_run(text)
            + f'<w:commentRangeEnd w:id="{comment_id}"/>'
            + f'<w:r><w:t xml:space="preserve">{MIXED_RUN_TAIL}</w:t>'
            f'<w:commentReference w:id="{comment_id}"/></w:r>'
            "</w:p>"
        )

    return _simple_commented_paragraph(comment_id, shape, text)


def _two_comments_paragraph() -> str:
    """Comments 5 and 6, side by side on one paragraph.

    The second range is the one that collapsed: with its start left in place
    among the runs and its end pushed past the insertion, nothing sat between
    start and end and Word dropped the zero-width range.
    """
    first, second = TWO_COMMENTS_HALVES
    return (
        "<w:p>"
        '<w:commentRangeStart w:id="5"/>'
        + _text_run(first)
        + '<w:commentRangeEnd w:id="5"/>'
        + _reference_run(5, "WORD_STANDARD")
        + '<w:commentRangeStart w:id="6"/>'
        + _text_run(second)
        + '<w:commentRangeEnd w:id="6"/>'
        + _reference_run(6, "WORD_STANDARD")
        + "</w:p>"
    )


def _nested_ranges_paragraph() -> str:
    """Comment 7's range ENCLOSES comment 8's — a comment on a clause plus a
    comment on one phrase inside it."""
    head, middle, tail = NESTED_RANGES_PARTS
    return (
        "<w:p>"
        '<w:commentRangeStart w:id="7"/>'
        + _text_run(head)
        + '<w:commentRangeStart w:id="8"/>'
        + _text_run(middle)
        + '<w:commentRangeEnd w:id="8"/>'
        + _reference_run(8, "WORD_STANDARD")
        + _text_run(tail)
        + '<w:commentRangeEnd w:id="7"/>'
        + _reference_run(7, "WORD_STANDARD")
        + "</w:p>"
    )


def _paragraph_text_for(comment_id: int) -> str:
    """The concatenated `<w:t>` text of a fixture paragraph — what a patch's
    `source_text` must equal to locate it."""
    shape, text = SHAPES[comment_id]
    if shape == "TWO_COMMENTS":
        return "".join(TWO_COMMENTS_HALVES)
    if shape == "NESTED_RANGES":
        return "".join(NESTED_RANGES_PARTS)
    if shape == "MIXED_RUN":
        return text + MIXED_RUN_TAIL
    return text


#: One <w:comment> body per id, all authored by the counterparty.
_COMMENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<w:comments xmlns:w="{W_NS}">'
    + "".join(
        f'<w:comment w:id="{cid}" w:author="Counterparty Counsel" '
        f'w:date="2026-01-01T00:00:00Z" w:initials="CC">'
        f'<w:p><w:r><w:t>Counterparty note on the {SHAPES[cid][0]} clause.</w:t></w:r></w:p>'
        "</w:comment>"
        for cid in SHAPES
    )
    + "</w:comments>"
)


def _document_xml() -> str:
    # TWO_COMMENTS and NESTED_RANGES each build ONE paragraph carrying two
    # comments, so they are emitted by their own builders rather than per-id.
    body = (
        "".join(_paragraph_xml(cid) for cid in (0, 1, 2, 3, 4))
        + _two_comments_paragraph()
        + _nested_ranges_paragraph()
        + "".join(_paragraph_xml(cid) for cid in (9, 10, 11))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        + body
        + "<w:sectPr/></w:body></w:document>"
    )


def _build_commented_docx() -> bytes:
    """A minimal, dependency-free docx carrying every real-world comment shape.

    Hand-built OOXML rather than python-docx: python-docx cannot author
    comments, which is precisely why the existing byte-identity test
    (tests/redline/test_inplace_tracked_changes.py AC1) never exercised a
    comment part.
    """
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdComments" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(CONTENT_TYPES_PART, content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr(DOCUMENT_PART, _document_xml())
        zf.writestr(RELS_PART, doc_rels)
        zf.writestr(COMMENTS_PART, _COMMENTS_XML)
    return buf.getvalue()


#: Every commented paragraph EXCEPT the UNTOUCHED one is patched — the real
#: case, where the clause the counterparty commented on is the clause we
#: redline.
PATCHED_IDS = [0, 1, 2, 3, 4, 5, 7, 9, 10]
UNTOUCHED_ID = 11


def _patches() -> list:
    return [
        {
            "anchor": f"sec-{cid}",
            "source_text": _paragraph_text_for(cid),
            "new_text": _paragraph_text_for(cid).strip() + PATCH_SUFFIX,
        }
        for cid in PATCHED_IDS
    ]


def _apply():
    inbound = _build_commented_docx()
    result = redline_inplace.apply_tracked_changes_inplace(
        inbound,
        _patches(),
        author="Contract Toaster",
        timestamp_iso="2026-01-01T00:00:00Z",
    )
    docx_bytes = getattr(result, "docx_bytes", None)
    if not docx_bytes:
        raise AssertionError(f"in-place patch produced no docx: {result!r}")
    return inbound, bytes(docx_bytes), result


def _out_document(out: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(out)) as zout:
        return ET.fromstring(zout.read(DOCUMENT_PART))


def _canonical(el: ET.Element) -> bytes:
    return ET.tostring(el, encoding="utf-8")


def check_1_every_inbound_comment_body_survives() -> list:
    """Every inbound `<w:comment>` element survives UNMODIFIED.

    Deliberately a subset-of-elements check, not byte-identity of
    `word/comments.xml`. Byte-identity is the assertion that reported True while
    the comment it described was already orphaned -- it proves nothing about the
    comment surviving. It is also about to become false for a good reason: PR G2
    authors OUR OWN comments into this part, and appending a `<w:comment>` must
    not trip a guard whose real job is "you did not touch THEIRS".
    """
    inbound, out, _ = _apply()
    failures: list = []
    with zipfile.ZipFile(io.BytesIO(inbound)) as zin, zipfile.ZipFile(io.BytesIO(out)) as zout:
        if COMMENTS_PART not in zout.namelist():
            return ["  word/comments.xml was DROPPED from the delivered package"]
        inbound_comments = {
            c.get(_w("id")): _canonical(c)
            for c in ET.fromstring(zin.read(COMMENTS_PART)).findall(_w("comment"))
        }
        out_comments = {
            c.get(_w("id")): _canonical(c)
            for c in ET.fromstring(zout.read(COMMENTS_PART)).findall(_w("comment"))
        }
    for cid, xml in inbound_comments.items():
        if cid not in out_comments:
            failures.append(f"  inbound <w:comment w:id={cid}> is GONE from word/comments.xml")
        elif out_comments[cid] != xml:
            failures.append(f"  inbound <w:comment w:id={cid}> was MODIFIED")
    return failures


def check_2_comment_anchors_survive() -> list:
    """THE test. A comment body with no anchor is orphaned and vanishes.

    Asserted against the anchors present in the OUTPUT DOCUMENT — the thing
    Word actually reads to decide whether to draw a bubble.
    """
    _, out, _ = _apply()
    doc = _out_document(out)
    present = {
        (el.tag, el.get(_w("id")))
        for el in doc.iter()
        if el.tag in redline_inplace.COMMENT_ANCHOR_TAGS
    }
    failures: list = []
    for cid, (shape, _) in SHAPES.items():
        where = "an untouched paragraph" if cid == UNTOUCHED_ID else "a PATCHED paragraph"
        for tag in ("commentRangeStart", "commentRangeEnd", "commentReference"):
            if (_w(tag), str(cid)) not in present:
                failures.append(
                    f"  comment id={cid} ({shape}) on {where}: <w:{tag}> anchor is GONE from "
                    f"document.xml. word/comments.xml still holds the body, so the comment is "
                    f"ORPHANED — Word has nothing to attach it to and it disappears from the "
                    f"margin. The counterparty's note is silently lost."
                )
    return failures


def check_3_redline_still_applied() -> list:
    """Preserving comments must not cost us the tracked change."""
    _, out, result = _apply()
    with zipfile.ZipFile(io.BytesIO(out)) as zout:
        doc = zout.read(DOCUMENT_PART).decode("utf-8")
    failures: list = []
    if sorted(result.applied) != sorted(p["anchor"] for p in _patches()):
        failures.append(
            f"  not every patch applied: applied={result.applied} failed={result.failed}"
        )
    for cid in PATCHED_IDS:
        if _paragraph_text_for(cid).strip() + PATCH_SUFFIX not in doc:
            failures.append(f"  the tracked insertion for comment id={cid} is missing")
    if "<w:del " not in doc or "<w:ins " not in doc:
        failures.append("  the tracked change markup is missing entirely")
    return failures


def check_4_no_text_duplicated_by_a_mixed_run() -> list:
    """A run mixing text with a reference must not re-emit its text.

    This is the case the narrow rule was built to avoid, by dropping the anchor.
    Both are avoidable: the text belongs in the `<w:del>` exactly once, and the
    reference is re-emitted alone.
    """
    _, out, _ = _apply()
    doc = _out_document(out)
    failures: list = []
    for p in doc.iter(_w("p")):
        ids = {el.get(_w("id")) for el in p.iter() if el.tag == _w("commentReference")}
        if str(4) not in ids:  # the MIXED_RUN paragraph
            continue
        live_text = "".join(t.text or "" for t in p.iter(_w("t")))
        deleted_text = "".join(t.text or "" for t in p.iter(_w("delText")))
        expected_live = _paragraph_text_for(4).strip() + PATCH_SUFFIX
        if live_text != expected_live:
            failures.append(
                f"  MIXED_RUN paragraph's live text is {live_text!r}, expected {expected_live!r} "
                f"— the reference run's text was duplicated back into the paragraph"
            )
        if MIXED_RUN_TAIL not in deleted_text:
            failures.append(
                f"  MIXED_RUN paragraph's <w:del> does not carry {MIXED_RUN_TAIL!r} — "
                f"the redline no longer deletes the text it replaces"
            )
    return failures


def check_5_no_comment_range_collapsed() -> list:
    """Every range that has both ends still SPANS content.

    A `<w:commentRangeStart>` immediately followed by its `<w:commentRangeEnd>`
    is zero-width; Word drops it and the bubble goes with it. This is what a
    second comment on one paragraph used to do.
    """
    _, out, _ = _apply()
    doc = _out_document(out)
    failures: list = []
    for p in doc.iter(_w("p")):
        kids = list(p)
        positions: dict = {}
        for idx, el in enumerate(kids):
            if el.tag in (_w("commentRangeStart"), _w("commentRangeEnd")):
                positions.setdefault(el.get(_w("id")), {})[el.tag] = idx
        for cid, ends in positions.items():
            start = ends.get(_w("commentRangeStart"))
            end = ends.get(_w("commentRangeEnd"))
            if start is None or end is None:
                continue
            if end <= start:
                failures.append(f"  comment id={cid}: range END precedes its START")
                continue
            spanned = [k for k in kids[start + 1 : end] if k.tag in (_w("del"), _w("ins"), _w("r"))]
            if not spanned:
                failures.append(
                    f"  comment id={cid} ({SHAPES[int(cid)][0]}): range is ZERO-WIDTH — start "
                    f"and end are adjacent, so Word drops the range and the comment with it"
                )
    return failures


def check_6_no_orphans_reported() -> list:
    """Any anchor the patcher cannot carry across must be LOUD, and there must
    be none of them for these shapes."""
    _, _, result = _apply()
    orphans = getattr(result, "orphaned_comments", None)
    if orphans is None:
        return ["  InplaceResult has no orphaned_comments field — a lost anchor would be silent"]
    if orphans:
        return [f"  the patcher orphaned {len(orphans)} inbound comment anchor(s): {orphans!r}"]
    return []


def check_7_other_parts_untouched() -> list:
    """The patcher mutates document.xml and nothing else.

    `word/comments.xml` is exempt here and covered by check 1's element-level
    rule instead: PR G2 authors our own comments into that part, and this check
    is about parts we have no business rewriting at all.
    """
    inbound, out, _ = _apply()
    failures: list = []
    with zipfile.ZipFile(io.BytesIO(inbound)) as zin, zipfile.ZipFile(io.BytesIO(out)) as zout:
        missing = set(zin.namelist()) - set(zout.namelist())
        if missing:
            failures.append(f"  delivered package dropped inbound parts: {sorted(missing)}")
        for name in set(zin.namelist()) & set(zout.namelist()):
            if name in (DOCUMENT_PART, COMMENTS_PART):
                continue
            if zin.read(name) != zout.read(name):
                failures.append(f"  part '{name}' is not byte-identical to the inbound document")
    return failures


def main() -> int:
    checks = [
        ("1", "every inbound <w:comment> body survives unmodified", check_1_every_inbound_comment_body_survives),
        ("2", "comment ANCHORS survive in document.xml, every real Word shape", check_2_comment_anchors_survive),
        ("3", "the tracked change is still applied", check_3_redline_still_applied),
        ("4", "a mixed text+reference run duplicates no text", check_4_no_text_duplicated_by_a_mixed_run),
        ("5", "no comment range collapsed to zero-width", check_5_no_comment_range_collapsed),
        ("6", "no anchor silently orphaned", check_6_no_orphans_reported),
        ("7", "every part except document.xml/comments.xml is byte-identical", check_7_other_parts_untouched),
    ]
    ok = True
    for code, name, fn in checks:
        try:
            failures = fn()
        except Exception as exc:  # noqa: BLE001
            failures = [f"  UNEXPECTED {type(exc).__name__}: {exc}"]
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            ok = False
    print()
    if ok:
        print("All inbound-comment preservation checks passed.")
        return 0
    print("One or more inbound-comment preservation checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
