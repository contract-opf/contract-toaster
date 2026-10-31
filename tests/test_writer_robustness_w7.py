#!/usr/bin/env python3
"""
QA remediation W7 item 6 (QA report 2026-10-31, area 4): two writer defects.

## XML-illegal characters in inserted text failed the whole review

A model-authored insert carrying a character XML 1.0 cannot hold (BEL
U+0007, a vertical tab, U+FFFE, ...) proved -- the transcript validator
compares text, not encodability -- and then raised an uncaught `ExpatError`
the moment the edited part was re-parsed, taking every other proven edit in
the review down with it (review ERROR). `redline_block_apply` now refuses
that ONE edit before anything is written, as `insert_text_not_xml_safe`,
and its siblings still land.

## The export marker assumed the header was `header1.xml`

`redline_generate.inject_export_marker_and_footnotes` looked only for a
part NAMED `word/header1.xml`. A package whose real default header was
`header2.xml` got a NEW `header1.xml` wired as a SECOND `default`
`headerReference` on its final `w:sectPr` (two in one section, which Word
resolves arbitrarily), and every earlier section got no marker at all. The
marker now follows the sections' own references through the document's
relationships: every referenced header/footer part gets the marker, and only
the sections that show no default header/footer at all are wired to a new
marker part -- never one that already has, or inherits, its own.

Both runners: no-argument `test_*` functions plus the `TESTS` list below.
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
for _dir in (SCRIPTS_DIR, TESTS_DIR, REPO_ROOT / "backend" / "src"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript as bt  # noqa: E402
import extraction_normalization_stage as stage  # noqa: E402
import redline_block_apply as rba  # noqa: E402
import redline_generate as rg  # noqa: E402
from w7_docx_helpers import CONTENT_TYPES, PACKAGE_RELS, docx, heading, p, r  # noqa: E402

AUTHOR = "Synthetic Reviewer"
WHEN = "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# XML-illegal insert text
# ---------------------------------------------------------------------------


def _two_edit_transcript(bad_insert: str) -> tuple[bytes, dict]:
    doc = docx(heading("1. Fees") + p(r("Pay in 30 days.")) + heading("2. Term") + p(r("Lasts 12 months.")))
    block_map = stage.build_block_map(stage.extract_and_normalize(doc)["paragraphs"])
    patches = [
        {
            "block_id": "p0001",
            "segments": [
                {"op": "keep", "text": "Pay in "},
                {"op": "delete", "text": "30", "issue_key": "bad"},
                {"op": "insert", "text": bad_insert, "issue_key": "bad"},
                {"op": "keep", "text": " days."},
            ],
        },
        {
            "block_id": "p0002",
            "segments": [
                {"op": "keep", "text": "Lasts "},
                {"op": "delete", "text": "12", "issue_key": "good"},
                {"op": "insert", "text": "24", "issue_key": "good"},
                {"op": "keep", "text": " months."},
            ],
        },
    ]
    proven = bt.validate_block_patches(patches, [], block_map)
    assert proven["status"] == "proven", proven["failures"]
    return doc, proven


def test_an_xml_illegal_insert_fails_only_its_own_edit() -> None:
    for bad in ("45\x07", "45\x0b", "45\ufffe", "4\x005"):
        doc, proven = _two_edit_transcript(bad)
        result = rba.apply_block_transcript(doc, proven, author=AUTHOR, timestamp_iso=WHEN)
        assert result["docx_bytes"] is not None, (repr(bad), result["failures"])
        assert [a.get("issue_key") for a in result["applied"]] == ["good"], (repr(bad), result["applied"])
        assert [(f["issue_key"], f["reason"]) for f in result["failures"]] == [
            ("bad", rba.REASON_INSERT_TEXT_NOT_XML_SAFE)
        ], (repr(bad), result["failures"])
        assert bad not in result["failures"][0]["detail"], "the detail must not echo the text"
        rg.verify_docx_round_trip(result["docx_bytes"])


def test_xml_legal_but_awkward_text_still_lands() -> None:
    for fine in ("45 <b> & co", "45 ]]>", "45\ttabbed", "45 \u00e9\u20ac\U0001f600"):
        doc, proven = _two_edit_transcript(fine)
        result = rba.apply_block_transcript(doc, proven, author=AUTHOR, timestamp_iso=WHEN)
        assert result["docx_bytes"] is not None and not result["failures"], (repr(fine), result["failures"])


def test_an_xml_illegal_block_insert_fails_only_itself() -> None:
    doc = docx(heading("1. Fees") + p(r("Pay in 30 days.")))
    block_map = stage.build_block_map(stage.extract_and_normalize(doc)["paragraphs"])
    ops = [
        {"op": "insert_block_after", "anchor_block_id": "p0001", "new_text": "New\x01 clause.", "issue_key": "bad"},
        {"op": "insert_block_after", "anchor_block_id": "p0001", "new_text": "A clean clause.", "issue_key": "good"},
    ]
    proven = bt.validate_block_patches([], ops, block_map)
    assert proven["status"] == "proven", proven["failures"]
    result = rba.apply_block_transcript(doc, proven, author=AUTHOR, timestamp_iso=WHEN)
    assert result["docx_bytes"] is not None, result["failures"]
    assert [(f["issue_key"], f["reason"]) for f in result["failures"]] == [
        ("bad", rba.REASON_INSERT_TEXT_NOT_XML_SAFE)
    ], result["failures"]


# ---------------------------------------------------------------------------
# The export marker
# ---------------------------------------------------------------------------

W_R = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HDR_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"


def _package(body: str, parts: dict[str, tuple[str, str, str]]) -> bytes:
    """`parts`: rId -> (part file under word/, relationship kind, text)."""
    rels = "".join(
        f'<Relationship Id="{rid}" Type="{REL}/{kind}" Target="{name}"/>' for rid, (name, kind, _t) in parts.items()
    )
    overrides = "".join(
        f'<Override PartName="/word/{name}" ContentType="{HDR_CT.replace("header", kind)}"/>'
        for name, kind, _t in parts.values()
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES.replace("</Types>", overrides + "</Types>"))
        zf.writestr("_rels/.rels", PACKAGE_RELS)
        zf.writestr(
            "word/_rels/document.xml.rels",
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>',
        )
        for name, kind, text in parts.values():
            tag = "hdr" if kind == "header" else "ftr"
            zf.writestr(f"word/{name}", f"<?xml version=\"1.0\"?><w:{tag} {W_R}><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:{tag}>")
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W_R}><w:body>{body}</w:body></w:document>',
        )
    return buf.getvalue()


def _marked(docx_bytes: bytes) -> tuple[list[str], dict[str, bytes]]:
    out = rg.inject_export_marker_and_footnotes(docx_bytes, [], {}, include_marker=True)
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        parts = {name: zf.read(name) for name in zf.namelist()}
    sections = re.findall(r"<w:sectPr>.*?</w:sectPr>", parts["word/document.xml"].decode())
    rg.verify_docx_round_trip(out)
    rg.run_output_ooxml_scan(out, docx_bytes)
    return sections, parts


def _default_refs(section: str, kind: str) -> list[str]:
    return re.findall(rf'<w:{kind}Reference w:type="default" r:id="(\w+)"', section)


def test_the_qa_repro_wires_no_second_default_header() -> None:
    """Two sections, both on `header2.xml`: the marker goes INTO header2.xml,
    no section gains a second default header, and both sections (which had
    no footer) get the one new marker footer."""
    body = (
        '<w:p><w:pPr><w:sectPr><w:headerReference w:type="default" r:id="rId9"/></w:sectPr></w:pPr>'
        "<w:r><w:t>Section one text.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Section two text.</w:t></w:r></w:p>"
        '<w:sectPr><w:headerReference w:type="default" r:id="rId9"/><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
    )
    sections, parts = _marked(_package(body, {"rId9": ("header2.xml", "header", "SYNTHETIC HEADER")}))
    assert len(sections) == 2, sections
    for section in sections:
        assert _default_refs(section, "header") == ["rId9"], section
        assert len(_default_refs(section, "footer")) == 1, section
    assert rg.MARKER_TEXT.encode() in parts["word/header2.xml"]
    assert b"SYNTHETIC HEADER" in parts["word/header2.xml"]
    assert "word/header1.xml" not in parts, sorted(parts)
    assert rg.MARKER_TEXT.encode() in parts["word/footer1.xml"]


def test_only_sections_that_show_no_header_are_wired() -> None:
    """Section 1 has no header; section 2 has its own; section 3 has none
    and INHERITS section 2's. Only section 1 is wired to the new marker
    part -- wiring section 3 would replace the header it shows."""
    body = (
        "<w:p><w:pPr><w:sectPr/></w:pPr><w:r><w:t>One.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:sectPr><w:headerReference w:type="default" r:id="rId5"/></w:sectPr></w:pPr>'
        "<w:r><w:t>Two.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Three.</w:t></w:r></w:p><w:sectPr/>"
    )
    sections, parts = _marked(_package(body, {"rId5": ("header7.xml", "header", "SECTION TWO HEADER")}))
    assert len(sections) == 3, sections
    new_rids = _default_refs(sections[0], "header")
    assert len(new_rids) == 1 and new_rids != ["rId5"], sections[0]
    assert _default_refs(sections[1], "header") == ["rId5"], sections[1]
    assert _default_refs(sections[2], "header") == [], sections[2]
    assert rg.MARKER_TEXT.encode() in parts["word/header7.xml"]
    assert rg.MARKER_TEXT.encode() in parts["word/header1.xml"]


def test_every_referenced_header_type_gets_the_marker_and_names_are_never_reused() -> None:
    """`header1.xml` is the FIRST-page header and `header2.xml` the default:
    both carry the marker, nothing new is created, and `header1.xml` is not
    mistaken for the default."""
    body = (
        '<w:sectPr><w:headerReference w:type="first" r:id="rId1"/>'
        '<w:headerReference w:type="default" r:id="rId2"/>'
        '<w:footerReference w:type="default" r:id="rId3"/><w:titlePg/></w:sectPr>'
    )
    sections, parts = _marked(
        _package(
            "<w:p><w:r><w:t>Body.</w:t></w:r></w:p>" + body,
            {
                "rId1": ("header1.xml", "header", "FIRST PAGE"),
                "rId2": ("header2.xml", "header", "EVERY PAGE"),
                "rId3": ("footer4.xml", "footer", "FOOTER"),
            },
        )
    )
    assert _default_refs(sections[0], "header") == ["rId2"], sections[0]
    assert _default_refs(sections[0], "footer") == ["rId3"], sections[0]
    for name in ("word/header1.xml", "word/header2.xml", "word/footer4.xml"):
        assert parts[name].count(rg.MARKER_TEXT.encode()) == 1, name
    assert "word/footer1.xml" not in parts, sorted(parts)


def test_a_package_with_no_header_still_gets_one() -> None:
    sections, parts = _marked(docx(p(r("Body."))))
    assert len(_default_refs(sections[0], "header")) == 1
    assert len(_default_refs(sections[0], "footer")) == 1
    assert rg.MARKER_TEXT.encode() in parts["word/header1.xml"]
    assert rg.MARKER_TEXT.encode() in parts["word/footer1.xml"]


TESTS = [
    test_an_xml_illegal_insert_fails_only_its_own_edit,
    test_xml_legal_but_awkward_text_still_lands,
    test_an_xml_illegal_block_insert_fails_only_itself,
    test_the_qa_repro_wires_no_second_default_header,
    test_only_sections_that_show_no_header_are_wired,
    test_every_referenced_header_type_gets_the_marker_and_names_are_never_reused,
    test_a_package_with_no_header_still_gets_one,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {fn.__name__}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"  [ERROR] {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {fn.__name__}")
    print("\n" + "=" * 60)
    print("ALL GREEN" if failures == 0 else f"FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
