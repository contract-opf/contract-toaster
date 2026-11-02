#!/usr/bin/env python3
"""
QA remediation W7 item 5 (QA report 2026-10-31, area 4): two upload-gauntlet
bypasses, closed on the input side and on the generated-redline side alike.

## Duplicate zip entry names

A ZIP central directory may list the same name twice. `zipfile.read(name)`
returns the LAST entry, while every other ZIP reader is free to take the
FIRST -- so a package whose first `word/_rels/document.xml.rels` carried an
external image (or whose first `word/document.xml` carried a DOCTYPE) and
whose second copy was clean passed every check here, which inspected only
the clean copy. OPC part names are also case-insensitive (ECMA-376 Part 2,
9.1.1.1), so `word/document.xml` and `WORD/Document.xml` name the same
part. Either shape is now refused outright (`duplicate_zip_entry`).

## Case-sensitive part-name comparisons

`_iter_relationship_parts` and the entity-safety walk matched `.rels` /
`.xml` case-SENSITIVELY, so `word/_rels/document.xml.RELS` carrying an
external relationship sailed through. Both compare case-insensitively now.

## Both directions, one set of helpers

`redline_generate.run_output_ooxml_scan` reuses the same
`upload_validation` helpers (`check_part_names`, `_check_relationships`), so
a generated redline cannot carry either shape either.

Both runners: no-argument `test_*` functions plus the `TESTS` list below.
"""

from __future__ import annotations

import io
import sys
import warnings
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _dir in (REPO_ROOT / "backend" / "src", REPO_ROOT / "backend", REPO_ROOT / "scripts"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import redline_generate  # noqa: E402
import upload_validation as uv  # noqa: E402

BASE = REPO_ROOT / "tests" / "fixtures" / "document-shapes" / "baseline-mutual-nda.SYNTHETIC.docx"
DOC_RELS = "word/_rels/document.xml.rels"
EXTERNAL_IMAGE = (
    b'<Relationship Id="rIdX" '
    b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
    b'Target="http://attacker.example/x.png" TargetMode="External"/>'
)
DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _CleanAv:
    def scan(self, _bytes: bytes) -> str:
        return uv.AV_VERDICT_CLEAN


def _rebuild(transform) -> bytes:
    """Rewrite the synthetic baseline package entry by entry through
    `transform(zip_out, name, data)`."""
    out = io.BytesIO()
    with zipfile.ZipFile(BASE) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # zipfile warns on a duplicate name
                transform(zout, info.filename, zin.read(info.filename))
    return out.getvalue()


def _with_external(data: bytes) -> bytes:
    return data.replace(b"</Relationships>", EXTERNAL_IMAGE + b"</Relationships>")


def _gauntlet_reason(data: bytes) -> str | None:
    try:
        uv.run_upload_gauntlet(data, filename="x.docx", declared_content_type=DOCX_CT, av_client=_CleanAv())
    except uv.HostileFileError as exc:
        return exc.reason_code
    return None


def _uppercase_rels(zout, name, data) -> None:
    """The QA repro: the document relationships part RENAMED to `.RELS`
    (one entry, no duplicate), carrying an external image."""
    if name == DOC_RELS:
        zout.writestr(name[: -len(".rels")] + ".RELS", _with_external(data))
        return
    zout.writestr(name, data)


def _duplicate_rels_hostile_first(zout, name, data) -> None:
    if name == DOC_RELS:
        zout.writestr(name, _with_external(data))
    zout.writestr(name, data)


def _duplicate_document_doctype_first(zout, name, data) -> None:
    if name == "word/document.xml":
        hostile = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "http://attacker.example/">]>'
        zout.writestr(name, hostile + data.split(b"?>", 1)[1])
    zout.writestr(name, data)


def _case_folded_duplicate(zout, name, data) -> None:
    zout.writestr(name, data)
    if name == "word/document.xml":
        zout.writestr("WORD/Document.xml", data)


def _uppercase_xml_extension_with_doctype(zout, name, data) -> None:
    zout.writestr(name, data)
    if name == "word/document.xml":
        zout.writestr(
            "word/extra.XML",
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]><x>&e;</x>',
        )


def test_the_control_is_still_rejected() -> None:
    assert _gauntlet_reason(_rebuild(lambda z, n, d: z.writestr(n, _with_external(d) if n == DOC_RELS else d))) == "external_relationship"


def test_the_clean_baseline_is_still_accepted() -> None:
    assert _gauntlet_reason(BASE.read_bytes()) is None


def test_an_uppercase_rels_part_is_scanned() -> None:
    assert _gauntlet_reason(_rebuild(_uppercase_rels)) == "external_relationship"


def test_an_uppercase_xml_part_is_entity_checked() -> None:
    assert _gauntlet_reason(_rebuild(_uppercase_xml_extension_with_doctype)) == "xml_entity_rejected"


def test_a_duplicate_rels_entry_with_the_hostile_copy_first_is_rejected() -> None:
    assert _gauntlet_reason(_rebuild(_duplicate_rels_hostile_first)) == "duplicate_zip_entry"


def test_a_duplicate_document_entry_with_a_doctype_first_is_rejected() -> None:
    assert _gauntlet_reason(_rebuild(_duplicate_document_doctype_first)) == "duplicate_zip_entry"


def test_part_names_that_differ_only_in_case_are_duplicates() -> None:
    assert _gauntlet_reason(_rebuild(_case_folded_duplicate)) == "duplicate_zip_entry"


def _output_scan_reason(data: bytes) -> str | None:
    try:
        redline_generate.run_output_ooxml_scan(data, BASE.read_bytes())
    except redline_generate.OutputScanError as exc:
        return exc.reason_code
    return None


def test_the_output_scan_shares_both_rules() -> None:
    assert _output_scan_reason(BASE.read_bytes()) is None
    assert _output_scan_reason(_rebuild(_uppercase_rels)) == "external_relationship"
    assert _output_scan_reason(_rebuild(_duplicate_rels_hostile_first)) == "duplicate_zip_entry"
    assert _output_scan_reason(_rebuild(_case_folded_duplicate)) == "duplicate_zip_entry"


TESTS = [
    test_the_control_is_still_rejected,
    test_the_clean_baseline_is_still_accepted,
    test_an_uppercase_rels_part_is_scanned,
    test_an_uppercase_xml_part_is_entity_checked,
    test_a_duplicate_rels_entry_with_the_hostile_copy_first_is_rejected,
    test_a_duplicate_document_entry_with_a_doctype_first_is_rejected,
    test_part_names_that_differ_only_in_case_are_duplicates,
    test_the_output_scan_shares_both_rules,
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
