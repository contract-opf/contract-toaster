#!/usr/bin/env python3
"""
Gate for issue #560: a reserved `ns<digits>` namespace prefix on an uploaded
document crashed the redline.

## What was wrong

`ET.register_namespace` refuses any prefix matching `ns\\d+` -- that pattern is
reserved for ElementTree's own auto-generated bindings -- and raises
`ValueError`. Several places in the redline path register every prefix a
document declares. Most guarded the call. `redline_generate.
inject_export_marker_and_footnotes` did not.

Real Word documents carry such prefixes, particularly any that have been
round-tripped through another tool. So a document whose every edit had
compiled, one step from a finished redline, raised instead -- producing
nothing.

## Measured, not estimated

On a real 31-agreement corpus, 5 patches each:

    before   8 of 26 documents produced a redline   (30.8%)
             36 of 130 patches applied              (27.7%)
             17 documents RAISED

    after   25 of 26 documents produced a redline   (96.2%)
            121 of 130 patches applied              (93.1%)
             0 documents raised

The residual 9 were a different, known bug (#529/#564: the edit spans a
multi-`w:p` logical-paragraph join), not this one.

## Why this test is shaped the way it is

The corpus is the client's and cannot be committed, so the fixture is a
synthetic document that reproduces the ONE structural property that triggered
it: a root that declares `xmlns:ns0`. That is the whole trigger -- nothing about
the corpus documents' content mattered.

The load-bearing assertion is that a redline is PRODUCED. Asserting merely that
no exception escaped would pass against a version that swallowed the error and
returned nothing, which is the same outcome for the attorney as the crash.

Issue #628 repointed the driver from the deleted quote patcher onto the LIVE
block path (`redline_generate.generate_redline_from_blocks`), run in a notes
mode that carries internal content so the export-marker injection -- the exact
function that raised -- is on the path under test. The structural trigger and
every assertion are unchanged.
"""

from __future__ import annotations

import datetime
import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_normalization_stage as ens  # noqa: E402
import leakage_scan  # noqa: E402
import redline_generate  # noqa: E402
import redline_inplace  # noqa: E402

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


ADDITION = " This obligation survives termination."


def _apply(docx_bytes: bytes) -> dict:
    """Compile ONE whole-clause edit through the live block path.

    The transcript is derived from the document's OWN block map (the same
    `extraction_normalization_stage.build_block_map` a real review addresses
    through), so the fixture cannot address a block the extractor never
    stamped. `notes_mode="internal"` is what puts the export marker -- and
    therefore `redline_generate.inject_export_marker_and_footnotes`, the
    function that raised on a reserved prefix -- on the path.

    Returns `{"docx_bytes", "applied", "flag_only"}` so the assertions below
    read exactly as they did against the retired quote patcher.
    """
    normalized = ens.extract_and_normalize(docx_bytes)
    block_map = ens.build_block_map(normalized["paragraphs"])
    block_id, block = next(
        (bid, blk) for bid, blk in block_map.items() if CLAUSE in blk["text"]
    )
    result = redline_generate.generate_redline_from_blocks(
        reconciled_result={
            "decision": "REQUEST_CHANGE",
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": "2",
                    "section_title": "Indemnification",
                    "counterparty_change_summary": "One-way indemnity.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "regression fixture",
                    "internal_rationale_for_footnote": "regression fixture (internal)",
                    "playbook_topic_id": "indemnification",
                    "provenance": "model",
                }
            ],
            "block_patches": [
                {
                    "block_id": block_id,
                    "segments": [
                        # The WHOLE block, transcribed, then the addition --
                        # the shape the prompt asks a model for.
                        {"op": "keep", "text": block["text"]},
                        {"op": "insert", "text": ADDITION, "issue_key": "I1"},
                    ],
                }
            ],
            "block_ops": [],
            "verdict_summary": None,
        },
        corpus=leakage_scan.ConfidentialCorpus(),
        normalized_docx_bytes=docx_bytes,
        notes_mode="internal",
        author="Test",
        date=datetime.datetime(2026, 8, 6, tzinfo=datetime.timezone.utc),
    )
    return {
        "docx_bytes": result.get("docx_bytes"),
        # One edit in, so "applied" is "a document came back with no
        # flag-only entry for it" -- the same one-element shape the assertions
        # below were written against.
        "applied": [] if result.get("flag_only") or result.get("docx_bytes") is None else [block_id],
        "flag_only": result.get("flag_only") or [],
    }


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
