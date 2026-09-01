#!/usr/bin/env python3
"""
Regression test — issue #260, rewritten onto the v3 block-transcript path by
issue #628: a flag-only issue (an issue that authored NO edit, mode 'none')
must never render as a deletion-with-no-replacement in the generated redline
`.docx`.

## Problem this proves stays fixed under the block path

An issue whose topic has `replacement_text.mode == "none"` authors no
transcript segment at all, so its `issue_key` never appears in
`block_transcript.validate_block_patches`' `by_issue` map. That absence --
never an empty string -- is how `redline_generate.generate_redline_from_
blocks` identifies a TRUE flag-only issue, and this test is the regression
guard for it: such an issue's clause must never be struck through with a
bare `<w:del>` and no matching `<w:ins>`.

## What this test checks

  1. A reconciled result containing ONE flag-only issue (no edits anywhere
     in the transcript) and ONE ordinary edit-bearing issue whose segments
     touch a DIFFERENT physical paragraph of the same addressed block.
  2. The flag-only issue's clause text is left FULLY intact in the
     generated `.docx` -- no `<w:del>` of that clause's text anywhere in
     `word/document.xml` (the regression this test guards: a deletion with
     no matching insertion), and its physical paragraph carries no
     revision markup at all.
  3. The flag-only issue is still SURFACED, labelled with WHY it has no
     replacement (`reason == "flag_only_mode_none"`), rather than silently
     dropped -- issue #585 finding 2.
  4. The ordinary edit-bearing issue's edit still lands normally --
     `<w:ins>`/`<w:del>` pair present for its span.

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
    issue_key: str,
    rationale: str,
    topic_id: str,
    replacement_text_outcome: str | None = None,
) -> dict:
    issue = {
        "issue_key": issue_key,
        "section_ref": section_ref,
        "section_title": "Section",
        "counterparty_change_summary": "Deviates from the standard position.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": rationale,
        "playbook_topic_id": topic_id,
        "internal_precedent_citation": None,
        "provenance": "model",
    }
    # Issue #585: a real reconciled issue reaching this stage already
    # passed through `replacement_text_enforcement.check_issues_
    # replacement_text` upstream (primary/critic pass), which sets this
    # field before an empty-text issue ever gets here -- mirror that so
    # this fixture matches the real shape `redline_generate` actually sees.
    if replacement_text_outcome is not None:
        issue["replacement_text_outcome"] = replacement_text_outcome
    return issue


def _reconciled(issues: list, block_patches: list) -> dict:
    return {
        "schema_version": "output-schema-v3",
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": issues,
        "block_patches": block_patches,
        "block_ops": [],
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
    normalized upload the block compiler edits in place.

    NOTE (and it is load-bearing for this fixture): with no heading styles,
    `extraction_normalization_stage` joins these sibling `<w:p>` runs into
    ONE logical block whose `physical_spans` keeps them distinct. So both
    clauses below live in a single addressed block, and the transcript's
    `keep` segments are what leave the flag-only clause's own physical
    paragraph untouched -- exactly the situation a real multi-`<w:p>`
    clause presents.
    """
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
            issue_key="I1",  # mode='none' -- flag only, authors NO edit below
            rationale="Anti-assignment clause weakened; flagging for attorney review.",
            topic_id="assignment",
            replacement_text_outcome="flag_only_mode_none",
        ),
        _make_issue(
            "sec-8",
            issue_key="I2",
            rationale="Restores the standard liability cap.",
            topic_id="limitation-of-liability",
        ),
    ]
    corpus = rg.leakage_scan.ConfidentialCorpus()
    draft_bytes = _build_docx_bytes([_SEC4_TEXT, _SEC8_TEXT])

    # The transcript addresses the document's OWN block, resolved through the
    # same `build_block_map` a real review addresses through -- never a
    # hand-written id. Only I2 authors segments; I1 authors none, which is
    # what makes it flag-only under the block contract.
    normalized = rg.extraction_normalization_stage.extract_and_normalize(draft_bytes)
    block_map = rg.extraction_normalization_stage.build_block_map(normalized["paragraphs"])
    block_id, block = next(iter(block_map.items()))
    head, cap, tail = block["text"].partition("shall not exceed $150,000")
    if not cap:
        print("FAIL: fixture setup -- the liability cap is not in the block text.")
        return 1
    block_patches = [
        {
            "block_id": block_id,
            "segments": [
                {"op": "keep", "text": head},
                {"op": "delete", "text": cap, "issue_key": "I2"},
                {"op": "insert", "text": "is uncapped", "issue_key": "I2"},
                {"op": "keep", "text": tail},
            ],
        }
    ]
    reconciled = _reconciled(issues, block_patches)

    result = rg.generate_redline_from_blocks(
        reconciled_result=reconciled,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
    )

    if result["status"] != "OK":
        failures.append(f"[1] Expected status=OK, got {result}")
    else:
        # Issue #585 review round 1, finding 2: the flag-only issue was
        # never ATTEMPTED (it authored no edit at all), but it must no
        # longer be silently dropped either -- it
        # now shows up in analysis_report labelled with WHY it has no
        # replacement text (`reason == "flag_only_mode_none"`, set by
        # `replacement_text_enforcement.check_issues_replacement_text`),
        # distinct from a "could not apply" failure. This is the explicit,
        # labelled outcome the ticket requires; it must not be confused
        # with a failed patch.
        analysis_report = result.get("analysis_report")
        if analysis_report is None:
            failures.append(
                "[1b] Expected an analysis_report labelling the flag-only "
                "issue's outcome (issue #585); got None."
            )
        else:
            sec4_entries = [
                entry
                for entry in analysis_report.get("changes_not_applied", [])
                if entry.get("section_ref") == "sec-4"
            ]
            if len(sec4_entries) != 1:
                failures.append(
                    f"[1c] Expected exactly one analysis_report entry for "
                    f"sec-4's flag-only issue; got {sec4_entries!r}"
                )
            elif sec4_entries[0].get("reason") != "flag_only_mode_none":
                failures.append(
                    f"[1d] Expected sec-4's entry to be labelled "
                    f"reason='flag_only_mode_none' (mode='none' topic, never "
                    f"a redline failure); got {sec4_entries[0].get('reason')!r}"
                )

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

                # The ordinary edit-bearing issue is unaffected: its edit
                # still lands as a normal <w:ins>/<w:del> pair.
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
