#!/usr/bin/env python3
"""
Slice test — redline generation end-to-end (issue #83, rewritten to the
quote-based patch shape by issue #379).

"Redline generation: tracked-changes docx, fail-closed patching, output
scan, export marker." Wires the reconciled issue list (#82) into the
quote-based patcher (`scripts/redline_quote_apply.py::apply_quote_patches`,
issue #377) end-to-end via `scripts/redline_generate.py::generate_redline`.

## Quote-path coverage (issue #379)

  1. A known issue list carrying a `source_quote` -> the expected
     tracked-changes `.docx` (`w:ins`/`w:del` around the quoted SPAN,
     footnotes, export marker).
  2. `{applied, flag_only}` -> result mapping (issue #379 Scope item 2):
     zero applied -> `MANUAL_REVIEW_REQUIRED`; at least one applied ->
     `OK` with partial delivery (docx_bytes present, an `analysis_report`
     names whichever quote(s) could not be located); no patches attempted
     at all (every issue flag-only) -> `OK` with `docx_bytes=None`.
  3. Hostile replacement text (field syntax, hyperlink, XML metachars) is
     inserted as inert literal runs only, and the output-side OOXML
     external-relationship/field/embedded-object scan passes.
  4. A planted leakage string (#26/#73) blocks generation and gates the
     ACCEPT-path `verdict_summary` prose too.
  5. A Word round-trip check -- the docx writer opens its own output
     cleanly.

## Retired anchor-path coverage (issue #380)

This file used to also cover an anchor/hash mismatch -> fail-closed
analysis report (issue #65's `redline_patch.py` guarantee). Issue #380
retired the anchor/hash-joined patch path entirely (`hunks` /
`current_paragraphs_by_anchor` params, `redline_patch
.join_patches_from_diff`/`apply_patches`) along with the deterministic
detector engine and the standard-form diff that fed it -- there is no
`source_text_hash` under the quote path, so `hash_mismatch_at_patch` has no
analog here; the analog guarantee is round-trip verification plus the
`not_found`/`ambiguous` locate outcomes covered by part 2 below (issue
#379 Notes).

Run standalone: `python tests/redline/test_redline_generation_83.py`
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
            f"  FIX: implement the issue #379 wiring of "
            f"scripts/redline_quote_apply.py::apply_quote_patches into "
            f"generate_redline's REQUEST_CHANGE branch."
        )
    try:
        import leakage_scan as _leakage_scan  # type: ignore

        leakage_scan = _leakage_scan
    except ImportError as exc:
        missing.append(f"MISSING: scripts/leakage_scan.py does not import ({exc}).")
    return redline_generate, leakage_scan, missing


_SEC8_TEXT = (
    "Each party's aggregate liability under this Agreement shall not "
    "exceed $150,000."
)
_SEC9_TEXT = "This Agreement shall be governed by the laws of Delaware."

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
    normalized upload `generate_redline`'s quote-based patcher (issue #379)
    locates each patch's `source_quote` inside. ElementTree-escapes nothing
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
    return _build_docx_bytes([_SEC8_TEXT, _SEC9_TEXT])


def _make_issue(
    section_ref: str,
    *,
    replacement: str,
    rationale: str,
    source_quote: str = "",
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
        "source_quote": source_quote,
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
    """AC1: a known issue list carrying a `source_quote` -> correct
    `w:ins`/`w:del` around the quoted span, footnoted rationale, redundant
    export marker on every page header/footer."""
    issues = [
        _make_issue(
            "sec-8",
            replacement="is uncapped",
            rationale="Restores the standard liability cap.",
            source_quote="shall not exceed $150,000",
        )
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline(
        reconciled_result=reconciled,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )

    if result["status"] != "OK":
        failures.append(f"[1a] Expected status=OK for a clean issue list, got {result}")
        return
    if result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[1a2] Expected decision=REQUEST_CHANGE, got {result.get('decision')!r}")
    if result.get("analysis_report") is not None:
        failures.append(f"[1a3] Expected no analysis_report (nothing flag-only), got {result['analysis_report']}")
    docx_bytes = result.get("docx_bytes")
    if not isinstance(docx_bytes, (bytes, bytearray)) or not docx_bytes:
        failures.append(f"[1b] Expected non-empty docx bytes, got {docx_bytes!r}")
        return

    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        names = set(zf.namelist())

        # --- quote-located patch landed as w:ins/w:del ----------------------
        doc_root = ET.fromstring(zf.read("word/document.xml"))
        ins_elements = doc_root.findall(f".//{_qn('ins')}")
        del_elements = doc_root.findall(f".//{_qn('del')}")
        if not ins_elements or not del_elements:
            failures.append("[1c] Expected at least one <w:ins> and <w:del> pair.")
        all_text = "".join(
            (t.text or "") for t in doc_root.findall(f".//{_qn('t')}")
        ) + "".join((t.text or "") for t in doc_root.findall(f".//{_qn('delText')}"))
        if "is uncapped" not in all_text:
            failures.append("[1d] Inserted replacement text missing from document.")
        if "shall not exceed $150,000" not in all_text:
            failures.append("[1e] Deleted original quoted text missing from document.")
        # Everything OUTSIDE the quoted span survives untouched (span-level
        # edit, not a whole-paragraph replace -- unlike the retired anchor
        # path).
        if "Each party's aggregate liability under this Agreement" not in all_text:
            failures.append("[1e2] Text outside the quoted span was not preserved.")

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

        # --- redundant export marker: every-page header/footer -------------
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

        # --- sectPr wires the header/footer relationship --------------------
        header_refs = doc_root.findall(f".//{_qn('headerReference')}")
        footer_refs = doc_root.findall(f".//{_qn('footerReference')}")
        if not header_refs or not footer_refs:
            failures.append(
                "[1m] <w:sectPr> is missing <w:headerReference>/<w:footerReference> "
                "-- the header/footer marker is not actually wired into the section."
            )


def _part_2_result_mapping(rg, failures: list) -> None:
    """AC2 (issue #379 Scope item 2): `{applied, flag_only}` -> result
    mapping. Three sub-cases: zero applied -> MANUAL_REVIEW_REQUIRED;
    partial delivery (some applied, some not_found) -> OK with an
    analysis_report; no patches attempted at all (every issue flag-only)
    -> OK with docx_bytes=None and no analysis_report."""
    corpus = rg.leakage_scan.ConfidentialCorpus()

    # --- 2a: zero applied -- the quote does not exist anywhere -------------
    unlocatable_issue = _make_issue(
        "sec-8",
        replacement="is uncapped",
        rationale="Restores the standard liability cap.",
        source_quote="this text was never in the draft at all",
    )
    result = rg.generate_redline(
        reconciled_result=_reconciled([unlocatable_issue]),
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[2a] Expected status=MANUAL_REVIEW_REQUIRED (zero applied), got {result}")
    if result.get("reason") != "quote_patches_not_applied":
        failures.append(f"[2b] Expected reason=quote_patches_not_applied, got {result.get('reason')!r}")
    if result.get("docx_bytes") is not None:
        failures.append("[2c] Zero applied must never deliver docx_bytes.")
    if "decision" in result:
        failures.append("[2d] MANUAL_REVIEW_REQUIRED is a SYSTEM status, never a legal decision.")
    analysis_report = result.get("analysis_report")
    if not analysis_report or analysis_report.get("report_type") != "analysis_report":
        failures.append(f"[2e] Expected an analysis_report artifact, got {analysis_report}")
    else:
        if "decision" in analysis_report:
            failures.append("[2f] analysis_report must never carry a decision field.")
        changes = analysis_report.get("changes_not_applied", [])
        if len(changes) != 1 or changes[0].get("section_ref") != "sec-8":
            failures.append(f"[2g] Expected sec-8 in changes_not_applied, got {changes}")
        elif changes[0].get("reason") != "not_found":
            failures.append(f"[2h] Expected reason=not_found on the entry, got {changes[0].get('reason')!r}")

    # --- 2b: partial delivery -- one locatable quote, one that is not ------
    locatable_issue = _make_issue(
        "sec-8",
        replacement="is uncapped",
        rationale="Restores the standard liability cap.",
        source_quote="shall not exceed $150,000",
        topic_id="limitation-of-liability",
    )
    mixed_result = rg.generate_redline(
        reconciled_result=_reconciled([locatable_issue, unlocatable_issue]),
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if mixed_result.get("status") != "OK":
        failures.append(f"[2i] Expected status=OK (partial delivery, >=1 applied), got {mixed_result}")
    if mixed_result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[2j] Expected decision=REQUEST_CHANGE, got {mixed_result.get('decision')!r}")
    mixed_docx = mixed_result.get("docx_bytes")
    if not mixed_docx:
        failures.append("[2k] Expected partial redline docx_bytes alongside the analysis report (issue #203).")
    else:
        doc_root = ET.fromstring(zipfile.ZipFile(io.BytesIO(bytes(mixed_docx))).read("word/document.xml"))
        all_text = "".join((t.text or "") for t in doc_root.findall(f".//{_qn('t')}"))
        if "is uncapped" not in all_text:
            failures.append("[2l] The locatable patch should still have applied in the partial delivery.")
    mixed_report = mixed_result.get("analysis_report")
    if not mixed_report:
        failures.append(f"[2m] Expected an analysis_report naming the unlocatable quote, got {mixed_report}")
    elif len(mixed_report.get("changes_not_applied", [])) != 1:
        failures.append(f"[2n] Expected exactly 1 changes_not_applied entry, got {mixed_report.get('changes_not_applied')}")

    # --- 2c: no patches attempted at all -- every issue flag-only ----------
    flag_only_issue = _make_issue(
        "sec-9",
        replacement="",  # mode='none' -- flag only, no replacement proposed
        rationale="Flagging for attorney review; no specific replacement.",
        source_quote="",
        topic_id="generic-topic",
    )
    flag_only_result = rg.generate_redline(
        reconciled_result=_reconciled([flag_only_issue]),
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if flag_only_result.get("status") != "OK" or flag_only_result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[2o] Expected a clean OK/REQUEST_CHANGE (nothing to attempt), got {flag_only_result}")
    if flag_only_result.get("docx_bytes") is not None:
        failures.append("[2p] An all-flag-only issue list must never produce docx_bytes.")
    if flag_only_result.get("analysis_report") is not None:
        failures.append(f"[2q] An all-flag-only issue list must never carry an analysis_report, got {flag_only_result['analysis_report']}")


def _part_3_hostile_text_inert_literal_runs(rg, failures: list) -> None:
    """AC3: hostile replacement text (field syntax, hyperlink, XML
    metachars) lands as inert literal runs only, and the output-side OOXML
    scan passes."""
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
            source_quote="shall not exceed $150,000",
        )
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline(
        reconciled_result=reconciled,
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
    ACCEPT-path verdict_summary too. Leakage gating runs before, and
    independently of, quote-based patch application -- unaffected by issue
    #379's rewrite of the REQUEST_CHANGE branch."""
    system_prompt_secret = "You are the confidential internal review assistant codenamed FALCON."
    corpus = rg.leakage_scan.ConfidentialCorpus(
        system_prompt_ngrams=[system_prompt_secret]
    )

    # --- REQUEST_CHANGE path: leakage planted in a rationale field --------
    issues = [
        _make_issue(
            "sec-8",
            replacement="is uncapped",
            rationale=f"Internal note: {system_prompt_secret}",
            source_quote="shall not exceed $150,000",
        )
    ]
    reconciled = _reconciled(issues)
    result = rg.generate_redline(
        reconciled_result=reconciled,
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
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if clean_result["status"] != "OK" or clean_result.get("decision") != "ACCEPT":
        failures.append(f"[4e] Expected a clean ACCEPT status=OK, got {clean_result}")
    if clean_result.get("docx_bytes") is not None:
        failures.append("[4f] ACCEPT path must never produce a document.")

    # --- Clean REQUEST_CHANGE: quote-based patch applies (issue #379) -----
    clean_request_change = _reconciled(
        [
            _make_issue(
                "sec-8",
                replacement="is uncapped",
                rationale="Restores the standard liability cap.",
                source_quote="shall not exceed $150,000",
            )
        ]
    )
    clean_rc_result = rg.generate_redline(
        reconciled_result=clean_request_change,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if clean_rc_result["status"] != "OK" or clean_rc_result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[4g] Expected a clean REQUEST_CHANGE status=OK, got {clean_rc_result}")
    if not clean_rc_result.get("docx_bytes"):
        failures.append(
            f"[4h] Expected non-empty docx_bytes on a clean, locatable REQUEST_CHANGE "
            f"(issue #379), got {clean_rc_result.get('docx_bytes')!r}"
        )
    if clean_rc_result.get("analysis_report") is not None:
        failures.append(
            f"[4i] Expected no analysis_report (nothing flag-only), got "
            f"{clean_rc_result['analysis_report']}"
        )


def _part_5_word_round_trip(rg, failures: list) -> None:
    """AC5: a Word round-trip check -- the docx writer opens its own output
    cleanly."""
    issues = [
        _make_issue(
            "sec-8",
            replacement="is uncapped",
            rationale="Restores the standard liability cap.",
            source_quote="shall not exceed $150,000",
        )
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline(
        reconciled_result=reconciled,
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
    _part_2_result_mapping(redline_generate, failures)
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
