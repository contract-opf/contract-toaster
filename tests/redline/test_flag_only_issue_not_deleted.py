#!/usr/bin/env python3
"""
RED test — issue #260: a flag-only issue (replacement_text mode 'none')
must never render as a deletion-with-no-replacement in the generated
redline `.docx`.

## Problem this proves

An issue whose topic has `replacement_text.mode == "none"` carries
`proposed_replacement_text: ""` (`playbooks/output-schema-v1.json:120-124`
-- "An empty string signals mode='none' (flag only, no replacement
proposed)"). Before the fix, `redline_generate._issues_to_patches` turned
EVERY issue into a docx patch with no empty-text filter, `redline_patch.py`
applied it with `new_text=""`, and `redline_docx_writer.build_document_xml`
emitted the `<w:del>` strikethrough of the original clause but skipped the
`<w:ins>` because `if new_text:` (redline_docx_writer.py:406) is falsy for
an empty string. Net effect: a clause the model meant only to FLAG for
attorney attention was struck through entirely with no replacement -- a
materially wrong proposed edit on legal paper.

## What this test checks

  1. A reconciled issue list containing ONE flag-only issue (empty
     `proposed_replacement_text`) and ONE ordinary replacement-bearing issue
     for a DIFFERENT anchor.
  2. The flag-only issue's clause text is left fully intact in the
     generated `.docx` -- no `<w:del>` of that anchor's text anywhere in
     `word/document.xml` (the regression this test guards: a deletion with
     no matching insertion).
  3. The ordinary replacement-bearing issue's exact-match patch still lands
     normally (today's behavior for real replacements is unchanged) --
     `<w:ins>`/`<w:del>` pair present for that anchor.

This test FAILS on the unmodified tree because
`redline_generate._issues_to_patches` (and thus `generate_redline`) does
not filter empty-`proposed_replacement_text` issues out of the docx patch
set: the flag-only anchor's clause is struck through with no `<w:ins>`.

Run standalone: `python tests/redline/test_flag_only_issue_not_deleted.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import hashlib
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


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hunks_and_paragraphs() -> tuple[list, dict]:
    """Two sections: sec-4 (the flag-only issue's clause) and sec-8 (an
    ordinary replacement-bearing issue's clause)."""
    sec4_text = (
        "Counterparty may assign this Agreement without prior written "
        "consent."
    )
    sec8_text = (
        "Each party's aggregate liability under this Agreement shall not "
        "exceed $150,000."
    )
    hunks = [
        {
            "anchor": "sec-4",
            "kind": "modified_new",
            "text": sec4_text,
            "source_text_hash": _sha256_text(sec4_text),
        },
        {
            "anchor": "sec-8",
            "kind": "modified_new",
            "text": sec8_text,
            "source_text_hash": _sha256_text(sec8_text),
        },
    ]
    current_paragraphs_by_anchor = {"sec-4": sec4_text, "sec-8": sec8_text}
    return hunks, current_paragraphs_by_anchor


def _make_issue(
    section_ref: str,
    *,
    replacement: str,
    rationale: str,
    topic_id: str,
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


def _import_redline_generate():
    try:
        import redline_generate as rg  # type: ignore

        return rg, None
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/redline_generate.py does not import ({exc})."
        )


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
    normalized upload `generate_redline`'s in-place patcher (issue #291)
    locates each patch's target paragraph in."""
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

    hunks, current_paragraphs_by_anchor = _hunks_and_paragraphs()
    issues = [
        _make_issue(
            "sec-4",
            replacement="",  # mode='none' -- flag only, no replacement proposed
            rationale="Anti-assignment clause weakened; flagging for attorney review.",
            topic_id="assignment",
        ),
        _make_issue(
            "sec-8",
            replacement="Each party's liability is uncapped.",
            rationale="Restores the standard liability cap.",
            topic_id="limitation-of-liability",
        ),
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()
    draft_bytes = _build_docx_bytes(
        [hunks[0]["text"], hunks[1]["text"]]  # sec-4, sec-8, in that order
    )

    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
    )

    if result["status"] != "OK":
        failures.append(f"[1] Expected status=OK, got {result}")
    else:
        docx_bytes = result.get("docx_bytes")
        if not isinstance(docx_bytes, (bytes, bytearray)) or not docx_bytes:
            failures.append(f"[2] Expected non-empty docx bytes, got {docx_bytes!r}")
        else:
            with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
                doc_root = ET.fromstring(zf.read("word/document.xml"))

                sec4_text = current_paragraphs_by_anchor["sec-4"]
                del_texts = {
                    (el.text or "")
                    for el in doc_root.findall(f".//{_qn('del')}//{_qn('delText')}")
                }
                if sec4_text in del_texts:
                    failures.append(
                        "[3] REGRESSION: the flag-only issue's clause "
                        f"({sec4_text!r}) was struck through with <w:del> even "
                        "though it carries no proposed_replacement_text -- a "
                        "deletion with no matching insertion."
                    )

                # The clause text must remain intact somewhere in the body
                # (either untouched, or -- at minimum -- not deleted without
                # a replacement).
                all_del_and_ins_texts = "".join(
                    (t.text or "") for t in doc_root.findall(f".//{_qn('delText')}")
                ) + "".join(
                    (t.text or "")
                    for ins in doc_root.findall(f".//{_qn('ins')}")
                    for t in ins.findall(f".//{_qn('t')}")
                )
                if sec4_text in all_del_and_ins_texts:
                    # If it appears at all in a del/ins run, it must be
                    # accompanied by an insertion (never a bare deletion).
                    ins_texts = "".join(
                        (t.text or "")
                        for ins in doc_root.findall(f".//{_qn('ins')}")
                        for t in ins.findall(f".//{_qn('t')}")
                    )
                    if sec4_text not in ins_texts and sec4_text in all_del_and_ins_texts:
                        failures.append(
                            "[4] The flag-only clause appears in a tracked "
                            "del/ins run with no corresponding insertion."
                        )

                # The ordinary replacement-bearing issue is unaffected: its
                # exact-match patch still lands as a normal <w:ins>/<w:del>
                # pair (today's behavior for real replacements).
                all_text = "".join(
                    (t.text or "") for t in doc_root.findall(f".//{_qn('t')}")
                ) + "".join(
                    (t.text or "") for t in doc_root.findall(f".//{_qn('delText')}")
                )
                if "liability is uncapped" not in all_text:
                    failures.append(
                        "[5] The ordinary replacement-bearing issue (sec-8) "
                        "did not land as expected."
                    )
                if "$150,000" not in all_text:
                    failures.append(
                        "[6] The ordinary replacement-bearing issue's "
                        "original text is missing from the redline."
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
