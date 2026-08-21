#!/usr/bin/env python3
"""
Gate for issue #560: a reserved `ns<digits>` namespace prefix on an uploaded
document crashed the redline.

## What was wrong

`ET.register_namespace` refuses any prefix matching `ns\\d+` -- that pattern is
reserved for ElementTree's own auto-generated bindings -- and raises
`ValueError`. Three places in the redline path register every prefix a document
declares. Two guarded the call. `redline_generate.
inject_export_marker_and_footnotes` did not.

Real Word documents carry such prefixes, particularly any that have been
round-tripped through another tool. So a document that had located every one of
its patches, and was one step from a finished redline, raised instead --
producing nothing.

## Measured, not estimated

On a real 31-agreement corpus, 5 patches each:

    before   8 of 26 documents produced a redline   (30.8%)
             36 of 130 patches applied              (27.7%)
             17 documents RAISED

    after   25 of 26 documents produced a redline   (96.2%)
            121 of 130 patches applied              (93.1%)
             0 documents raised

The residual 9 are a different, known bug (#529: the quote spans a multi-`w:p`
logical-paragraph join), not this one.

## Why this test is shaped the way it is

The corpus is the client's and cannot be committed, so the fixture is a
synthetic document that reproduces the ONE structural property that triggered
it: a root that declares `xmlns:ns0`. That is the whole trigger -- nothing about
the corpus documents' content mattered.

The load-bearing assertion is that a redline is PRODUCED. Asserting merely that
no exception escaped would pass against a version that swallowed the error and
returned nothing, which is the same outcome for the attorney as the crash.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import redline_inplace  # noqa: E402
import redline_quote_apply as rqa  # noqa: E402

CLAUSE = (
    "The Recipient shall indemnify the Discloser against all claims arising "
    "from the Recipient's breach of this Agreement."
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _document(extra_root_ns: str) -> bytes:
    body = (
        "<w:p><w:r><w:t>Mutual Non-Disclosure Agreement</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>{CLAUSE}</w:t></w:r></w:p>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        f"{extra_root_ns}>"
        f"<w:body>{body}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def _apply(docx_bytes: bytes) -> dict:
    return rqa.apply_quote_patches(
        docx_bytes,
        [{
            "source_quote": CLAUSE,
            "new_text": CLAUSE + " This obligation survives termination.",
            "rationale": "regression fixture",
        }],
        author="Test",
        timestamp_iso="2026-08-06T00:00:00Z",
    )


# ---------------------------------------------------------------------------


def test_a_reserved_prefix_still_produces_a_redline(failures: list) -> None:
    """THE REGRESSION. A document declaring `xmlns:ns0` must still yield a
    finished redline.

    Asserting that the call did not raise is NOT enough: a version that caught
    the error and returned no document would pass that, and the attorney gets
    the same nothing either way. So this asserts a document came back and the
    patch is recorded as applied.
    """
    reserved = ' xmlns:ns0="http://schemas.example.com/round-tripped"'
    try:
        result = _apply(_document(reserved))
    except ValueError as exc:
        failures.append(f"a reserved ns<digits> prefix still raises: {type(exc).__name__}")
        return
    if result["docx_bytes"] is None:
        failures.append("no redline was produced for a document with a reserved prefix")
    if len(result["applied"]) != 1:
        failures.append(
            f"expected the patch to apply; applied={len(result['applied'])} "
            f"flag_only={[f.get('reason') for f in result['flag_only']]}"
        )


def test_the_ordinary_case_is_unchanged(failures: list) -> None:
    """The control. If this fails, the fix broke the path it was meant to
    unblock rather than widening it."""
    result = _apply(_document(""))
    if result["docx_bytes"] is None or len(result["applied"]) != 1:
        failures.append("a document with no unusual prefixes no longer redlines")


def test_several_reserved_prefixes_are_all_survived(failures: list) -> None:
    """Real round-tripped documents carry more than one. Skipping the first
    and dying on the second would be a fix that passed the test above."""
    many = "".join(
        f' xmlns:ns{i}="http://schemas.example.com/rt{i}"' for i in range(4)
    )
    try:
        result = _apply(_document(many))
    except ValueError:
        failures.append("multiple reserved prefixes still raise")
        return
    if result["docx_bytes"] is None:
        failures.append("no redline produced with several reserved prefixes")


def test_a_legitimate_prefix_is_still_registered(failures: list) -> None:
    """The guard must skip ONLY what ElementTree refuses. A fix that skipped
    every prefix would pass every test above while quietly letting the
    serializer rename bindings this module exists to preserve."""
    before = dict(getattr(__import__("xml.etree.ElementTree", fromlist=["_namespace_map"]),
                          "_namespace_map"))
    redline_inplace.register_declared_namespaces(
        [("ns0", "http://example.com/reserved"), ("mc", "http://example.com/legit")]
    )
    after = getattr(__import__("xml.etree.ElementTree", fromlist=["_namespace_map"]),
                    "_namespace_map")
    if after.get("http://example.com/legit") != "mc":
        failures.append("a legitimate prefix was not registered")
    if after.get("http://example.com/reserved") == "ns0":
        failures.append("a reserved prefix was registered; ElementTree forbids it")
    # Leave the global map as we found it -- it is process-wide state.
    after.clear()
    after.update(before)


def test_the_default_prefix_is_still_skipped(failures: list) -> None:
    """Pre-existing behaviour, folded into the shared helper: registering the
    empty prefix would make that URI the default for every unprefixed element.
    """
    ET = __import__("xml.etree.ElementTree", fromlist=["_namespace_map"])
    before = dict(ET._namespace_map)
    redline_inplace.register_declared_namespaces([("", "http://example.com/default")])
    if "http://example.com/default" in ET._namespace_map:
        failures.append("the default (empty) prefix was registered")
    ET._namespace_map.clear()
    ET._namespace_map.update(before)


TESTS = [
    test_a_reserved_prefix_still_produces_a_redline,
    test_the_ordinary_case_is_unchanged,
    test_several_reserved_prefixes_are_all_survived,
    test_a_legitimate_prefix_is_still_registered,
    test_the_default_prefix_is_still_skipped,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        print(("PASS: " if len(failures) == before else "FAIL: ") + test.__name__)

    if failures:
        print()
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print("\nPASS: a reserved namespace prefix no longer costs the redline (issue #560).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
