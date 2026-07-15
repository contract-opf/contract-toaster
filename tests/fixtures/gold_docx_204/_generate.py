#!/usr/bin/env python3
"""
One-shot generator for the issue #204 document-level gold-case fixture.

NOT part of the test suite (no test_*.py name, not collected by check.sh) --
this is a build-time tool, run once to produce the committed
`reject-no-exclusivity-doc-level.docx` + `.json` pair under this directory,
the same "generate once, commit the output" convention
`scripts/build_anchor_map.py` uses for `standard-forms/*.anchor-map.json`.

Re-run this script (`python3 tests/fixtures/gold_docx_204/_generate.py`) only
if the planted document's content or the canonical standard form changes --
otherwise the committed `.docx`/`.json` pair IS the fixture, exactly like
every other `tests/gold-fixtures/*.json` entry.

Docx-builder helpers (`_build_docx_bytes` / `_heading_p` / `_body_p`) mirror
`tests/test_extraction_normalization_stage_80.py`'s dependency-free
zipfile+ElementTree convention (no python-docx needed to WRITE a minimal
valid OOXML body) -- this repo already uses that pattern for hand-built
.docx fixtures.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import diff_standard_form as dsf  # noqa: E402

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)

_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body_paragraphs_xml}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


TARGET_ANCHOR = "sec-10-non-exclusive"


def build_draft_body_xml(standard: list, planted_anchor: str, planted_text: str) -> str:
    """Build a FULL draft document body: every non-`absent_from_form`
    standard-form anchor is carried over VERBATIM (heading + text) except
    `planted_anchor`, which gets `planted_text` instead. This is what makes
    the resulting diff realistic -- only the ONE planted anchor comes back
    as a real hunk (`modified_new`); every other anchor matches its
    standard-form counterpart exactly and diffs as `unchanged`, so
    on_remove_or_alter rules protecting THOSE anchors correctly see their
    required tokens still present (no spurious `deleted`-hunk false
    positives from an intentionally-partial draft)."""
    parts = []
    for std_para in standard:
        if std_para.get("absent_from_form", False):
            continue
        text = planted_text if std_para["anchor"] == planted_anchor else std_para["text"]
        parts.append(_heading_p(std_para["heading"]))
        parts.append(_body_p(text))
    return "".join(parts)


def main() -> None:
    standard = dsf.load_standard_form_paragraphs(docx_path=None)
    std_by_anchor = {p["anchor"]: p for p in standard}

    excl_text = std_by_anchor[TARGET_ANCHOR]["text"]
    # Planting recipe (docs/evaluation.md "Planting recipe"): start from the
    # verbatim standard-form clause and INSERT a clause containing a
    # hard_rejection trigger term ("sole provider", rule "no-exclusivity")
    # into the in-scope section.
    planted_excl_text = (
        excl_text
        + " Institution shall be the sole provider of clinical placements for this program."
    )

    body_xml = build_draft_body_xml(standard, TARGET_ANCHOR, planted_excl_text)
    docx_bytes = _build_docx_bytes(body_xml)

    docx_path = FIXTURE_DIR / "reject-no-exclusivity-doc-level.docx"
    docx_path.write_bytes(docx_bytes)
    docx_sha256 = "sha256:" + hashlib.sha256(docx_bytes).hexdigest()

    # The hunk's own source_text_hash (hash of the OLD standard-side text at
    # sec-10-non-exclusive) -- computed identically to
    # diff_standard_form.py's _sha256_text(), i.e. redline_checks[] pins the
    # SAME hash the diff/patch pipeline itself computes, not an
    # independently-derived one.
    redline_hash = "sha256:" + hashlib.sha256(excl_text.encode("utf-8")).hexdigest()

    fixture = {
        "case_id": "reject-no-exclusivity-doc-level",
        "description": (
            "Document-level gold case (issue #204): a SYNTHETIC counterparty "
            "draft .docx built from the canonical standard form (synthetic "
            "mode) with one planted hard-rejection insertion ('sole "
            "provider', rule no-exclusivity) in the "
            "'Miscellaneous: Non-Exclusive' section, run through the actual "
            "extract -> normalize -> diff -> detector chain "
            "(scripts/extraction_normalization_stage.py -> "
            "scripts/diff_standard_form.py -> scripts/detector_common.py) "
            "-- NOT a per-fixture text snippet like the pre-#204 gold set."
        ),
        "input_docx": "tests/fixtures/gold_docx_204/reject-no-exclusivity-doc-level.docx",
        "input_docx_sha256": docx_sha256,
        "playbook_version": "1.0.0",
        "expected_decision": "REQUEST_CHANGE",
        "expected_issues": [
            {
                "playbook_topic_id": "exclusivity",
                "section_ref": "10 Miscellaneous ('non-exclusive')",
                "is_hard_rejection": True,
                "rule_id": "no-exclusivity",
            }
        ],
        "must_not_flag": [],
        "fp_tolerance": 0,
        "redline_checks": [
            {"anchor": "sec-10-non-exclusive", "source_text_hash": redline_hash}
        ],
        "case_type": "document_level",
        "synthetic": True,
        "gc_signoff": "pending",
        "provenance": "synthetic",
        "authorship_note": (
            "AFK-drafted per issue #204 (owner-approved mocked-model scope, "
            "2026-07-10): fully synthetic, standard-form-derived draft "
            "docx, no real-world contract text, no de-identification "
            "required. Marked synthetic and pending GC sign-off; not "
            "production-qualified until Legal/GC reviews and approves."
        ),
    }

    json_path = FIXTURE_DIR / "reject-no-exclusivity-doc-level.json"
    json_path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {docx_path} ({len(docx_bytes)} bytes, {docx_sha256})")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
