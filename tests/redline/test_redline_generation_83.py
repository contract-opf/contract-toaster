#!/usr/bin/env python3
"""
Slice test — redline generation end-to-end (issue #83).

"Redline generation: tracked-changes docx, fail-closed patching, output
scan, export marker." Wires the reconciled issue list (#82) into the
tracked-changes docx writer (#198) end-to-end via
`scripts/redline_generate.py::generate_redline`.

Covers the issue's Required-verification acceptance checks:

  1. A known issue list -> the expected tracked-changes `.docx`: correct
     `w:ins`/`w:del` structure, anchored + hash-validated patches only,
     footnoted rationales, and the redundant export marker on the cover and
     every page header/footer.
  2. An anchor/hash mismatch -> no edit is applied + the #38 analysis-report
     is emitted (fail-closed per #65).
  3. Hostile replacement text (field syntax, hyperlink, XML metachars) is
     inserted as inert literal runs only, and the output-side OOXML
     external-relationship/field/embedded-object scan passes.
  4. A planted leakage string (#26/#73) blocks generation and gates the
     ACCEPT-path `verdict_summary` prose too.
  5. A Word round-trip check -- the docx writer opens its own output
     cleanly.

This test FAILS on a tree with no `scripts/redline_generate.py` (no
end-to-end wiring from a reconciled issue list to a tracked-changes docx)
and PASSES once that module exists.

Run standalone: `python tests/redline/test_redline_generation_83.py`
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


def _import_modules():
    missing = []
    redline_generate = None
    leakage_scan = None
    try:
        import redline_generate as _redline_generate  # type: ignore

        redline_generate = _redline_generate
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_generate.py does not exist or fails "
            f"to import ({exc}).\n"
            f"  FIX: implement the issue #83 orchestrator wiring the "
            f"reconciled issue list (#82) into redline_patch.py (#65/#205) "
            f"and redline_docx_writer.py (#198), gated by leakage_scan.py "
            f"(#73/#26) and an output-side OOXML scan "
            f"(docs/threat-model.md -> 'Generated redline output hygiene')."
        )
    try:
        import leakage_scan as _leakage_scan  # type: ignore

        leakage_scan = _leakage_scan
    except ImportError as exc:
        missing.append(f"MISSING: scripts/leakage_scan.py does not import ({exc}).")
    return redline_generate, leakage_scan, missing


def _base_hunks_and_paragraphs():
    """A tiny two-section standard-form diff: sec-8 (liability) modified,
    sec-9 (governing law) unchanged in the current draft."""
    sec8_text = (
        "Each party's aggregate liability under this Agreement shall not "
        "exceed $150,000."
    )
    sec9_text = "This Agreement shall be governed by the laws of Delaware."
    hunks = [
        {
            "anchor": "sec-8",
            "kind": "modified_new",
            "text": sec8_text,
            "source_text_hash": _sha256_text(sec8_text),
        },
        {
            "anchor": "sec-9",
            "kind": "unchanged",
            "text": sec9_text,
            "source_text_hash": _sha256_text(sec9_text),
        },
    ]
    current_paragraphs_by_anchor = {"sec-8": sec8_text, "sec-9": sec9_text}
    return hunks, current_paragraphs_by_anchor


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
    locates each patch's target paragraph in. ElementTree-escapes nothing
    itself, so callers keep paragraph text free of raw `<`/`&`; hostile-text
    coverage (part 3 below) goes through the writer's own literal-run
    escaping, not this fixture builder."""
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


def _base_draft_docx_bytes() -> bytes:
    hunks, _ = _base_hunks_and_paragraphs()
    return _build_docx_bytes([h["text"] for h in hunks])


def _make_issue(
    section_ref: str,
    *,
    replacement: str,
    rationale: str,
    topic_id: str = "limitation-of-liability",
) -> dict:
    return {
        "section_ref": section_ref,
        "section_title": "Section",
        "counterparty_change_summary": "Deletes the standard position.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": rationale,
        "proposed_replacement_text": replacement,
        "playbook_topic_id": topic_id,
        "internal_precedent_citation": None,
        "provenance": "model",
    }


def _reconciled(issues: list, *, decision: str = "REQUEST_CHANGE", verdict_summary=None) -> dict:
    return {
        "schema_version": "output-schema-v1",
        "decision": decision,
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": issues,
        "critic_delta": None,
        "verdict_summary": verdict_summary,
    }


def _part_1_known_issue_list(rg, failures: list) -> None:
    """AC1: a known issue list -> correct w:ins/w:del, anchored + hash-
    validated patches only, footnoted rationales, redundant export marker
    on the cover and every page header/footer."""
    hunks, current_paragraphs_by_anchor = _base_hunks_and_paragraphs()
    issues = [
        _make_issue(
            "sec-8",
            replacement="Each party's liability is uncapped.",
            rationale="Restores the standard liability cap.",
        )
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )

    if result["status"] != "OK":
        failures.append(f"[1a] Expected status=OK for a clean issue list, got {result}")
        return
    docx_bytes = result.get("docx_bytes")
    if not isinstance(docx_bytes, (bytes, bytearray)) or not docx_bytes:
        failures.append(f"[1b] Expected non-empty docx bytes, got {docx_bytes!r}")
        return

    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        names = set(zf.namelist())

        # --- anchored, hash-validated patch landed as w:ins/w:del --------
        doc_root = ET.fromstring(zf.read("word/document.xml"))
        ins_elements = doc_root.findall(f".//{_qn('ins')}")
        del_elements = doc_root.findall(f".//{_qn('del')}")
        if not ins_elements or not del_elements:
            failures.append("[1c] Expected at least one <w:ins> and <w:del> pair.")
        all_text = "".join(
            (t.text or "") for t in doc_root.findall(f".//{_qn('t')}")
        ) + "".join((t.text or "") for t in doc_root.findall(f".//{_qn('delText')}"))
        if "liability is uncapped" not in all_text:
            failures.append("[1d] Inserted replacement text missing from document.")
        if "$150,000" not in all_text:
            failures.append("[1e] Deleted original text missing from document.")

        # --- footnoted rationale -------------------------------------------
        if "word/footnotes.xml" not in names:
            failures.append("[1f] No word/footnotes.xml part -- footnoted rationale missing.")
        else:
            footnote_ref = doc_root.findall(f".//{_qn('footnoteReference')}")
            if not footnote_ref:
                failures.append("[1g] No <w:footnoteReference> in document.xml.")
            footnotes_root = ET.fromstring(zf.read("word/footnotes.xml"))
            footnote_text = "".join(
                (t.text or "") for t in footnotes_root.findall(f".//{_qn('t')}")
            )
            if "Restores the standard liability cap" not in footnote_text:
                failures.append(
                    f"[1h] Footnote rationale text not found in footnotes.xml: "
                    f"{footnote_text!r}"
                )

        # --- redundant export marker: every-page header/footer (issue #291:
        # the in-place redline no longer prepends a synthetic cover-note
        # paragraph to the uploaded document's own body -- the header/footer
        # marker is the export marker for the in-place path).
        if "word/header1.xml" not in names or "word/footer1.xml" not in names:
            failures.append(
                f"[1j] Expected word/header1.xml and word/footer1.xml for the "
                f"every-page marker. Got parts: {sorted(names)}"
            )
        else:
            header_text = zf.read("word/header1.xml")
            footer_text = zf.read("word/footer1.xml")
            if b"attorney approval required" not in header_text:
                failures.append("[1k] Marker text not found in word/header1.xml.")
            if b"attorney approval required" not in footer_text:
                failures.append("[1l] Marker text not found in word/footer1.xml.")

        # --- sectPr wires the header/footer relationship ------------------
        header_refs = doc_root.findall(f".//{_qn('headerReference')}")
        footer_refs = doc_root.findall(f".//{_qn('footerReference')}")
        if not header_refs or not footer_refs:
            failures.append(
                "[1m] <w:sectPr> is missing <w:headerReference>/<w:footerReference> "
                "-- the header/footer marker is not actually wired into the section."
            )


def _part_2_hash_mismatch_fail_closed(rg, failures: list) -> None:
    """AC2: anchor/hash mismatch -> no edit applied for that section +
    analysis_report emitted (fail closed, issue #65), while a clean patch
    elsewhere in the same batch still lands (issue #203, partial
    delivery)."""
    hunks, current_paragraphs_by_anchor = _base_hunks_and_paragraphs()
    issues = [
        _make_issue(
            "sec-8",
            replacement="Each party's liability is uncapped.",
            rationale="Restores the standard liability cap.",
        ),
        _make_issue(
            "sec-9",
            replacement="This Agreement shall be governed by the laws of New York.",
            rationale="Restores Delaware governing law.",
        ),
    ]
    reconciled = _reconciled(issues)

    # Drift sec-9's current text after the diff was computed -- the hash
    # this batch validates against no longer matches.
    current_paragraphs_by_anchor["sec-9"] = "This clause has drifted since the diff ran."

    corpus = rg.leakage_scan.ConfidentialCorpus()
    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )

    if result["status"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[2a] Expected status=MANUAL_REVIEW_REQUIRED on a hash mismatch, "
            f"got {result.get('status')}: {result}"
        )
        return
    if result.get("reason") != "hash_mismatch_at_patch":
        failures.append(f"[2b] Expected reason=hash_mismatch_at_patch, got {result.get('reason')}")

    analysis_report = result.get("analysis_report")
    if not analysis_report or analysis_report.get("report_type") != "analysis_report":
        failures.append(f"[2c] Expected an analysis_report artifact, got {analysis_report}")
    else:
        not_applied_anchors = {c.get("anchor") for c in analysis_report.get("changes_not_applied", [])}
        if "sec-9" not in not_applied_anchors:
            failures.append(
                f"[2d] Expected sec-9 in changes_not_applied, got {not_applied_anchors}"
            )
        if analysis_report.get("status") != "MANUAL_REVIEW_REQUIRED":
            failures.append("[2e] analysis_report must never carry a legal decision.")
        if "decision" in analysis_report:
            failures.append("[2f] analysis_report must never carry an ACCEPT/REQUEST_CHANGE decision field.")

    # Partial delivery: the sec-8 clean patch still lands in the docx.
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append("[2g] Expected a partial redline docx alongside the analysis report (issue #203).")
    else:
        doc_root = ET.fromstring(zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))).read("word/document.xml"))
        all_text = "".join((t.text or "") for t in doc_root.findall(f".//{_qn('t')}"))
        if "liability is uncapped" not in all_text:
            failures.append("[2h] sec-8's clean patch should still be present in the partial redline.")
        if "New York" in all_text:
            failures.append("[2i] sec-9's mismatched patch must NOT have been applied (no approximate match).")


def _part_3_hostile_text_inert_literal_runs(rg, failures: list) -> None:
    """AC3: hostile replacement text (field syntax, hyperlink, XML
    metachars) lands as inert literal runs only, and the output-side OOXML
    scan passes."""
    hunks, current_paragraphs_by_anchor = _base_hunks_and_paragraphs()
    hostile_text = (
        '{ HYPERLINK "https://attacker.example/exfiltrate" } '
        "<w:fldChar w:fldCharType=\"begin\"/> & < > \" ' "
        "{ REF bookmark \\* MERGEFORMAT }"
    )
    issues = [
        _make_issue(
            "sec-8",
            replacement=hostile_text,
            rationale='Rationale with metachars: <tag> & "quoted"',
        )
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )

    if result["status"] != "OK":
        failures.append(f"[3a] Expected status=OK (hostile TEXT is not a leak/scan hit), got {result}")
        return
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append("[3b] Expected a docx to be produced.")
        return

    # The output OOXML scan itself must pass on this document.
    try:
        rg.run_output_ooxml_scan(docx_bytes)
    except rg.OutputScanError as exc:
        failures.append(f"[3c] Output OOXML scan unexpectedly failed: {exc}")

    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        doc_root = ET.fromstring(zf.read("word/document.xml"))

        # No field-code / hyperlink structure was created from the hostile text.
        for tag in ("fldChar", "instrText", "fldSimple", "hyperlink"):
            if doc_root.findall(f".//{_qn(tag)}"):
                failures.append(
                    f"[3d] Hostile text was serialized as document structure "
                    f"(<w:{tag}> present) instead of a literal text run."
                )

        # The hostile string survives as literal, inert TEXT content.
        ins_text = "".join(
            (el.text or "")
            for ins in doc_root.findall(f".//{_qn('ins')}")
            for el in ins.findall(f".//{_qn('t')}")
        )
        if "attacker.example" not in ins_text:
            failures.append(
                f"[3e] Hostile replacement text was not preserved as literal "
                f"run text (expected it verbatim, inert). Got: {ins_text!r}"
            )

        # No external relationship or embedded object exists anywhere.
        if "word/_rels/document.xml.rels" in zf.namelist():
            rels_root = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
            for rel in rels_root:
                if rel.get("TargetMode", "").lower() == "external":
                    failures.append("[3f] An external relationship was created from hostile text.")


def _part_4_leakage_gates_generation_and_accept(rg, failures: list) -> None:
    """AC4: a planted leakage string blocks generation, and gates the
    ACCEPT-path verdict_summary too."""
    system_prompt_secret = "You are the confidential internal review assistant codenamed FALCON."
    corpus = rg.leakage_scan.ConfidentialCorpus(
        system_prompt_ngrams=[system_prompt_secret]
    )
    hunks, current_paragraphs_by_anchor = _base_hunks_and_paragraphs()

    # --- REQUEST_CHANGE path: leakage planted in a rationale field --------
    issues = [
        _make_issue(
            "sec-8",
            replacement="Each party's liability is uncapped.",
            rationale=f"Internal note: {system_prompt_secret}",
        )
    ]
    reconciled = _reconciled(issues)
    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if result["status"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[4a] Expected ERROR_MANUAL_REVIEW_REQUIRED on a planted leak, got {result}"
        )
    if result.get("docx_bytes") is not None:
        failures.append("[4b] A leakage-blocked review must not produce a docx.")
    if "decision" in result:
        failures.append("[4c] A leakage block is a SYSTEM status, never a legal decision.")

    # --- ACCEPT path is NOT a bypass: verdict_summary is scanned too ------
    accept_reconciled = _reconciled(
        [], decision="ACCEPT", verdict_summary=f"Everything looked fine. {system_prompt_secret}"
    )
    accept_result = rg.generate_redline(
        reconciled_result=accept_reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if accept_result["status"] != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[4d] Expected the ACCEPT path's verdict_summary to be gated by the "
            f"leakage scan too, got {accept_result}"
        )

    # --- Clean ACCEPT still produces no document -------------------------
    clean_accept = _reconciled([], decision="ACCEPT", verdict_summary="Nothing notable changed.")
    clean_result = rg.generate_redline(
        reconciled_result=clean_accept,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if clean_result["status"] != "OK" or clean_result.get("decision") != "ACCEPT":
        failures.append(f"[4e] Expected a clean ACCEPT status=OK, got {clean_result}")
    if clean_result.get("docx_bytes") is not None:
        failures.append("[4f] ACCEPT path must never produce a document.")


def _part_5_word_round_trip(rg, failures: list) -> None:
    """AC5: a Word round-trip check -- the docx writer opens its own output
    cleanly."""
    hunks, current_paragraphs_by_anchor = _base_hunks_and_paragraphs()
    issues = [
        _make_issue(
            "sec-8",
            replacement="Each party's liability is uncapped.",
            rationale="Restores the standard liability cap.",
        )
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append("[5a] Expected a docx to round-trip check.")
        return

    try:
        rg.verify_docx_round_trip(docx_bytes)
    except ValueError as exc:
        failures.append(f"[5b] verify_docx_round_trip raised on the writer's own output: {exc}")

    # Belt-and-suspenders: independently re-open every part with zipfile +
    # ElementTree, exactly as a caller/attorney's Word client effectively
    # does when it opens the file.
    buf = io.BytesIO(bytes(docx_bytes))
    if not zipfile.is_zipfile(buf):
        failures.append("[5c] Produced bytes are not a valid ZIP archive.")
        return
    with zipfile.ZipFile(buf) as zf:
        bad = zf.testzip()
        if bad is not None:
            failures.append(f"[5d] Corrupt member in produced ZIP: {bad}")
        for name in zf.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                try:
                    ET.fromstring(zf.read(name))
                except ET.ParseError as exc:
                    failures.append(f"[5e] {name} failed to re-parse: {exc}")


def main() -> None:
    failures: list = []

    redline_generate, leakage_scan_mod, missing = _import_modules()
    if missing:
        print("FAIL: redline generation gate cannot run.\n")
        for m in missing:
            print(f"[G0] {m}")
            print()
        sys.exit(1)

    # Expose leakage_scan on the module under test for convenience in the
    # part functions above (redline_generate.py itself imports it).
    assert redline_generate.leakage_scan is leakage_scan_mod

    _part_1_known_issue_list(redline_generate, failures)
    _part_2_hash_mismatch_fail_closed(redline_generate, failures)
    _part_3_hostile_text_inert_literal_runs(redline_generate, failures)
    _part_4_leakage_gates_generation_and_accept(redline_generate, failures)
    _part_5_word_round_trip(redline_generate, failures)

    if failures:
        print("FAIL: redline generation gate (issue #83).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print("PASS: redline generation gate (issue #83).")
        sys.exit(0)


if __name__ == "__main__":
    main()
