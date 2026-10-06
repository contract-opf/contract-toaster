#!/usr/bin/env python3
"""Issue #153: the output OOXML scan refuses field codes and hyperlinks the
REDLINE ADDED, never the ones the counterparty's own document already held.

Before the fix every ordinary contract with a page-number field, a
cross-reference or a hyperlink failed closed as `output_ooxml_scan_failed`
after both model passes were paid for. Synthetic fixtures only."""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "scripts", ROOT / "backend", ROOT / "backend" / "src"):
    sys.path.insert(0, str(p))

import redline_generate as rg  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

PAGE_FIELD = (
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
)
REF_FIELD = '<w:p><w:fldSimple w:instr=" REF _Ref1 "><w:r><w:t>Section 4</w:t></w:r></w:fldSimple></w:p>'
LINK = '<w:p><w:hyperlink w:anchor="_Toc1"><w:r><w:t>Definitions</w:t></w:r></w:hyperlink></w:p>'
BODY = '<w:p><w:r><w:t xml:space="preserve">Supplier shall pay within 30 days.</w:t></w:r></w:p>'
TEXT_EDIT = (
    '<w:p><w:r><w:t xml:space="preserve">Supplier shall pay within </w:t></w:r>'
    '<w:del w:id="91" w:author="contract-toaster" w:date="2026-10-06T00:00:00Z">'
    '<w:r><w:delText>30</w:delText></w:r></w:del>'
    '<w:ins w:id="92" w:author="contract-toaster" w:date="2026-10-06T00:00:00Z">'
    '<w:r><w:t>45</w:t></w:r></w:ins><w:r><w:t xml:space="preserve"> days.</w:t></w:r></w:p>'
)


def _docx(body: str) -> bytes:
    doc = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


SOURCE = _docx(PAGE_FIELD + REF_FIELD + LINK + BODY)


def _refused(out: bytes, baseline: bytes | None) -> bool:
    try:
        rg.run_output_ooxml_scan(out, baseline)
    except rg.OutputScanError as exc:
        assert exc.reason_code == "field_code_in_output", exc.reason_code
        return True
    return False


def test_a_plain_text_edit_keeps_the_sources_own_fields_and_links() -> None:
    out = _docx(PAGE_FIELD + REF_FIELD + LINK + TEXT_EDIT)
    assert not _refused(out, SOURCE)


def test_without_a_baseline_the_old_strict_rule_still_holds() -> None:
    assert _refused(_docx(PAGE_FIELD + BODY), None)


def test_a_hyperlink_the_redline_inserts_is_refused() -> None:
    inserted_link = (
        '<w:p><w:ins w:id="93" w:author="contract-toaster" w:date="2026-10-06T00:00:00Z">'
        '<w:hyperlink w:anchor="x"><w:r><w:t>click</w:t></w:r></w:hyperlink></w:ins></w:p>'
    )
    assert _refused(_docx(PAGE_FIELD + REF_FIELD + LINK + BODY + inserted_link), SOURCE)


def test_an_inserted_field_cannot_hide_behind_a_deleted_source_field() -> None:
    # The source's REF is removed and a new fldSimple appears inside an
    # insertion: total fldSimple count is unchanged, but the inserted one
    # is counted on its own and refused.
    swapped = (
        '<w:p><w:ins w:id="94" w:author="contract-toaster" w:date="2026-10-06T00:00:00Z">'
        '<w:fldSimple w:instr=" HYPERLINK &quot;https://attacker.example&quot; ">'
        '<w:r><w:t>here</w:t></w:r></w:fldSimple></w:ins></w:p>'
    )
    assert _refused(_docx(PAGE_FIELD + swapped + LINK + BODY), SOURCE)


def test_more_field_elements_than_the_source_held_is_refused() -> None:
    assert _refused(_docx(PAGE_FIELD + PAGE_FIELD + REF_FIELD + LINK + BODY), SOURCE)


def test_a_field_in_a_part_the_source_did_not_have_is_refused() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_docx(PAGE_FIELD + REF_FIELD + LINK + BODY))) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr(
            "word/footnotes.xml",
            f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1"><w:p>'
            '<w:hyperlink w:anchor="x"><w:r><w:t>x</w:t></w:r></w:hyperlink></w:p></w:footnote></w:footnotes>',
        )
    assert _refused(out.getvalue(), SOURCE)


TESTS = [
    test_a_plain_text_edit_keeps_the_sources_own_fields_and_links,
    test_without_a_baseline_the_old_strict_rule_still_holds,
    test_a_hyperlink_the_redline_inserts_is_refused,
    test_an_inserted_field_cannot_hide_behind_a_deleted_source_field,
    test_more_field_elements_than_the_source_held_is_refused,
    test_a_field_in_a_part_the_source_did_not_have_is_refused,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
