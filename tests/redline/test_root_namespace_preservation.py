#!/usr/bin/env python3
"""
Regression gate: the in-place patcher's root-namespace handling must survive
the shapes real Word documents actually ship.

## Problem this guards against

`redline_inplace` re-serializes `word/document.xml` and then splices the
ORIGINAL root start tag back over the serializer's own, because ElementTree
drops any `xmlns` declaration it cannot see being used (e.g. one referenced
only inside an attribute value, `mc:Ignorable="w14 wp14"`). That splice was
built for declarations ElementTree LOSES. It broke on the opposite case --
declarations ElementTree ADDS:

1. A prefix declared on a NON-root element (`a` / `a14`, inside a
   `<w:drawing>`) is HOISTED to the root by the serializer. Splicing the
   original tag over that output dropped the hoisted binding while the body
   still used the prefix, so `word/document.xml` came back with an unbound
   prefix. Measured on the real corpus: 12 of 83 documents.
2. A root prefix matching ElementTree's reserved `ns<digits>` format made
   `ET.register_namespace` raise `ValueError: Prefix format reserved for
   internal use`. That propagated out of `apply_tracked_changes_inplace`
   uncaught -- a wedged, non-terminal review, the exact failure mode
   `test_roundtrip_failure_fails_closed.py` exists to prevent. Measured on
   the real corpus: 17 of 83 documents.

Together: 29 of 83 real documents (35%) could not pass through the in-place
path at all. Neither shape appears in any other fixture, so nothing was red.

## Why the fixtures are synthetic

The real documents that exposed this are counterparty contracts under
`docs/planning/`, which is excluded from the public cut
(`public-cut-exclude.txt`). A committed test must not depend on them, so each
shape below is reproduced synthetically with invented parties. The
corpus-wide sweep stays a local measurement.

## What is asserted

Anchors on the OUTPUT document, never on byte-identity:
  1. the drawing document's output parses (no unbound prefix)
  2. the hoisted `a` binding survives to the root
  3. the reserved `ns0` prefix does not raise, and its output parses
  4. every original root declaration survives the splice verbatim
  5. a genuine prefix/URI collision raises rather than silently rebinding
  6. the patch still applies (the fix does not cost the actual edit)

Run: python3 tests/redline/test_root_namespace_preservation.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import redline_inplace  # noqa: E402

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

PARA_TEXT = "Acme University shall provide notice to FixtureCorp within thirty (30) days."
NEW_TEXT = "Acme University shall provide notice to FixtureCorp within sixty (60) days."

# A root tag in Word's real style: many prefixes, and an mc:Ignorable whose
# VALUE names prefixes ElementTree cannot see being used.
_ROOT_OPEN = (
    '<w:document '
    f'xmlns:w="{W_NS}" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
    'mc:Ignorable="w14 wp14">'
)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _drawing_paragraph_declaring_a_off_root() -> str:
    """A paragraph whose drawing declares `xmlns:a` on a NON-root element --
    the shape ElementTree hoists to the root. This is what 12 real documents
    do."""
    return (
        "<w:p><w:r><w:drawing>"
        f'<a:graphic xmlns:a="{A_NS}"><a:graphicData uri="urn:example:fixture">'
        '<a:blip r:embed="rId1"/>'
        "</a:graphicData></a:graphic>"
        "</w:drawing></w:r></w:p>"
    )


def _docx(document_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _document(body: str, root_open: str = _ROOT_OPEN) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + root_open
        + "<w:body>"
        + body
        + "</w:body></w:document>"
    )
    return _docx(xml)


def _apply(docx_bytes: bytes):
    return redline_inplace.apply_tracked_changes_inplace(
        docx_bytes,
        [{"anchor": "sec-1", "source_text": PARA_TEXT, "new_text": NEW_TEXT}],
        author="Reviewer",
        timestamp_iso="2026-07-17T00:00:00Z",
    )


def _output_document_xml(result) -> bytes:
    with zipfile.ZipFile(io.BytesIO(result.docx_bytes)) as zf:
        return zf.read("word/document.xml")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_1_and_2_hoisted_namespace_survives(failures: list) -> None:
    """The drawing declares `a` off-root; the output must still bind it."""
    docx = _document(_paragraph(PARA_TEXT) + _drawing_paragraph_declaring_a_off_root())
    try:
        result = _apply(docx)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"[1] patching a document with an off-root xmlns raised: {exc!r}")
        return

    out = _output_document_xml(result)
    try:
        ET.fromstring(out)
    except ET.ParseError as exc:
        failures.append(
            f"[1] output document.xml is NOT well-formed: {exc}. The `a` prefix was "
            f"declared on a non-root element, hoisted to the root by the serializer, "
            f"and then dropped by the root-tag splice -- the body still uses it."
        )
        return

    root_tag = redline_inplace._root_open_tag(out.decode("utf-8"))
    bindings = dict(redline_inplace._declared_namespaces(root_tag))
    if bindings.get("a") != A_NS:
        failures.append(
            f"[2] the hoisted `a` binding did not survive to the output root tag "
            f"(got {bindings.get('a')!r}, expected {A_NS!r})"
        )


def check_3_reserved_prefix_does_not_raise(failures: list) -> None:
    """A root prefix of ElementTree's reserved `ns<digits>` form must not be
    fatal -- 17 real documents carry one."""
    root_open = _ROOT_OPEN[:-1] + ' xmlns:ns0="urn:example:legacy-tool">'
    docx = _document(_paragraph(PARA_TEXT), root_open=root_open)
    try:
        result = _apply(docx)
    except ValueError as exc:
        failures.append(
            f"[3] a root prefix matching ElementTree's reserved ns<digits> format "
            f"raised instead of being tolerated: {exc}. This propagates out of "
            f"apply_tracked_changes_inplace uncaught -- a wedged review."
        )
        return
    except Exception as exc:  # noqa: BLE001
        failures.append(f"[3] unexpected error on a reserved-format prefix: {exc!r}")
        return

    try:
        ET.fromstring(_output_document_xml(result))
    except ET.ParseError as exc:
        failures.append(f"[3] output with a reserved-format prefix is not well-formed: {exc}")


def check_4_original_declarations_survive(failures: list) -> None:
    """The splice's original purpose must not regress: every declaration on the
    inbound root, including ones only named inside mc:Ignorable's value,
    survives verbatim."""
    docx = _document(_paragraph(PARA_TEXT) + _drawing_paragraph_declaring_a_off_root())
    result = _apply(docx)
    out = _output_document_xml(result).decode("utf-8")
    root_tag = redline_inplace._root_open_tag(out)
    bindings = dict(redline_inplace._declared_namespaces(root_tag))

    for prefix, uri in redline_inplace._declared_namespaces(_ROOT_OPEN):
        if bindings.get(prefix) != uri:
            failures.append(
                f"[4] original root declaration xmlns:{prefix}={uri!r} did not survive "
                f"(got {bindings.get(prefix)!r})"
            )
    if 'mc:Ignorable="w14 wp14"' not in root_tag:
        failures.append("[4] mc:Ignorable was not preserved verbatim on the root tag")


def check_5_conflicting_binding_raises(failures: list) -> None:
    """A prefix bound to two different URIs cannot be merged -- keeping the
    original would silently rebind every use in the body. It must raise."""
    original = '<w:document xmlns:w="%s" xmlns:x="urn:example:one">' % W_NS
    auto = '<w:document xmlns:w="%s" xmlns:x="urn:example:TWO">' % W_NS
    try:
        redline_inplace._merge_hoisted_namespaces(original, auto)
    except ValueError:
        return
    failures.append(
        "[5] a prefix bound to different URIs on the original and serialized root "
        "tags did NOT raise -- the splice would silently rebind it in the body"
    )


def check_6_patch_still_applies(failures: list) -> None:
    """The fix must not cost the actual edit."""
    docx = _document(_paragraph(PARA_TEXT) + _drawing_paragraph_declaring_a_off_root())
    result = _apply(docx)
    if result.applied != ["sec-1"]:
        failures.append(f"[6] patch did not apply: applied={result.applied} failed={result.failed}")
        return
    out = _output_document_xml(result).decode("utf-8")
    if "<w:del " not in out or "<w:ins " not in out:
        failures.append("[6] output carries no tracked change for an applied patch")
    if NEW_TEXT not in out:
        failures.append("[6] the new text is absent from the output")


def main() -> int:
    failures: list[str] = []
    check_1_and_2_hoisted_namespace_survives(failures)
    check_3_reserved_prefix_does_not_raise(failures)
    check_4_original_declarations_survive(failures)
    check_5_conflicting_binding_raises(failures)
    check_6_patch_still_applies(failures)

    if failures:
        print("ROOT NAMESPACE PRESERVATION: FAIL")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("ROOT NAMESPACE PRESERVATION: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
