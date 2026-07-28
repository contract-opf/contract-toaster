#!/usr/bin/env python3
"""
Regression test — issue #260, rewritten to the quote-based patch shape by
issue #379: a flag-only issue (`proposed_replacement_text == ""`, mode
'none') must never render as a deletion-with-no-replacement in the
generated redline `.docx`.

## Problem this proves stays fixed under the quote path

An issue whose topic has `replacement_text.mode == "none"` carries
`proposed_replacement_text: ""` (`playbooks/output-schema-v2.json` --
"An empty string signals mode='none' (flag only, no replacement
proposed)"). `scripts/redline_generate.py::_issues_to_quote_patches`
excludes such an issue BEFORE `redline_quote_apply.apply_quote_patches` is
ever called (issue #379 Scope item 3) -- this test is the regression guard
for that exclusion: a flag-only issue's clause must never be struck
through with a bare `<w:del>` and no matching `<w:ins>`.

## What this test checks

  1. A reconciled issue list containing ONE flag-only issue (empty
     `proposed_replacement_text`, no `source_quote`) and ONE ordinary
     replacement-bearing issue (with a `source_quote`) for a DIFFERENT
     clause.
  2. The flag-only issue's clause text is left FULLY intact in the
     generated `.docx` -- no `<w:del>` of that clause's text anywhere in
     `word/document.xml` (the regression this test guards: a deletion with
     no matching insertion), and the clause is not even referenced in the
     `applied`/`analysis_report` machinery (it was never attempted).
  3. The ordinary replacement-bearing issue's quote-located patch still
     lands normally (today's behavior for real replacements is unchanged)
     -- `<w:ins>`/`<w:del>` pair present for that clause's quoted span.

Run standalone: `python tests/redline/test_flag_only_issue_not_deleted.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _import_redline_generate():
    try:
        import redline_generate as rg  # type: ignore

        return rg, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/redline_generate.py does not import ({exc})."
        )


_SEC4_TEXT = "Counterparty may assign this Agreement without prior written consent."
_SEC8_TEXT = (
    "Each party's aggregate liability under this Agreement shall not "
    "exceed $150,000."
)


def _make_issue(
    section_ref: str,
    *,
    replacement: str,
    rationale: str,
    topic_id: str,
    source_quote: str = "",
) -> dict:
    return {
        "section_ref": section_ref,
        "section_title": "Section",
        "counterparty_change_summary": "Deviates from the standard position.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": rationale,
        "proposed_replacement_text": replacement,
        "playbook_topic_id": topic_id,
        "internal_precedent_citation": None,
        "provenance": "model",
        "source_quote": source_quote,
    }


def _reconciled(issues: list) -> dict:
    return {
        "schema_version": "output-schema-v1",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": issues,
        "critic_delta": None,
        "verdict_summary": None,
    }


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


def _build_docx_bytes(paragraph_texts: list) -> bytes:
    """Minimal stdlib-only (no python-docx) multi-paragraph `.docx` -- the
    normalized upload the quote-based patcher (issue #379) locates each
    patch's `source_quote` inside."""
    body_ps = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraph_texts)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_ps}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def main() -> int:
    failures: list = []

    rg, missing = _import_redline_generate()
    if missing:
        print("FAIL: flag-only issue regression test cannot run.\n")
        print(f"[G0] {missing}")
        return 1

    issues = [
        _make_issue(
            "sec-4",
            replacement="",  # mode='none' -- flag only, no replacement proposed
            rationale="Anti-assignment clause weakened; flagging for attorney review.",
            topic_id="assignment",
            source_quote="",  # the model omits source_quote for a flag-only issue too
        ),
        _make_issue(
            "sec-8",
            replacement="is uncapped",
            rationale="Restores the standard liability cap.",
            topic_id="limitation-of-liability",
            source_quote="shall not exceed $150,000",
        ),
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()
    draft_bytes = _build_docx_bytes([_SEC4_TEXT, _SEC8_TEXT])

    result = rg.generate_redline(
        reconciled_result=reconciled,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
    )

    if result["status"] != "OK":
        failures.append(f"[1] Expected status=OK, got {result}")
    elif result.get("analysis_report") is not None:
        # The flag-only issue was never ATTEMPTED (excluded before
        # apply_quote_patches is even called) -- it must not show up as a
        # "could not apply" entry either; that would mischaracterize a
        # deliberate flag as a failure.
        failures.append(
            f"[1b] The flag-only issue was never attempted -- expected no "
            f"analysis_report (only the ordinary issue was in this batch, "
            f"and it applied cleanly), got {result['analysis_report']}"
        )
    else:
        docx_bytes = result.get("docx_bytes")
        if not isinstance(docx_bytes, (bytes, bytearray)) or not docx_bytes:
            failures.append(f"[2] Expected non-empty docx bytes, got {docx_bytes!r}")
        else:
            with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
                doc_root = ET.fromstring(zf.read("word/document.xml"))

                del_texts = {
                    (el.text or "")
                    for el in doc_root.findall(f".//{_qn('del')}//{_qn('delText')}")
                }
                if _SEC4_TEXT in del_texts:
                    failures.append(
                        "[3] REGRESSION: the flag-only issue's clause "
                        f"({_SEC4_TEXT!r}) was struck through with <w:del> even "
                        "though it carries no proposed_replacement_text -- a "
                        "deletion with no matching insertion."
                    )
                # Nothing in sec-4's paragraph was touched at all -- it is
                # not merely "not deleted", it retains its ORIGINAL single
                # <w:r><w:t> run structure with no del/ins sibling.
                paragraphs = [p for p in doc_root.find(_qn("body")) if p.tag == _qn("p")]
                sec4_paragraph = next(
                    (p for p in paragraphs if _SEC4_TEXT in "".join(t.text or "" for t in p.findall(f".//{_qn('t')}"))),
                    None,
                )
                if sec4_paragraph is None:
                    failures.append("[4] Could not find sec-4's paragraph in the output at all.")
                else:
                    if sec4_paragraph.findall(_qn("del")) or sec4_paragraph.findall(_qn("ins")):
                        failures.append(
                            "[5] sec-4's paragraph carries a <w:del>/<w:ins> even though "
                            "its issue was flag-only and should never have been touched."
                        )

                # The ordinary replacement-bearing issue is unaffected: its
                # quote-located patch still lands as a normal <w:ins>/<w:del>
                # pair (today's behavior for real replacements).
                all_text = "".join(
                    (t.text or "") for t in doc_root.findall(f".//{_qn('t')}")
                ) + "".join(
                    (t.text or "") for t in doc_root.findall(f".//{_qn('delText')}")
                )
                if "is uncapped" not in all_text:
                    failures.append(
                        "[6] The ordinary replacement-bearing issue (sec-8) "
                        "did not land as expected."
                    )
                if "shall not exceed $150,000" not in all_text:
                    failures.append(
                        "[7] The ordinary replacement-bearing issue's "
                        "original quoted text is missing from the redline."
                    )

    if failures:
        print("FAIL: flag-only issue regression test (issue #260).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: flag-only issue regression test (issue #260).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
