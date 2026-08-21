#!/usr/bin/env python3
"""
Slice test — quote-based in-place redline wiring (issue #379).

Issue #291 originally wired the anchor/hash-joined in-place patcher
(`scripts/redline_inplace.py`) into `generate_redline`; issue #380 retired
that whole path (and this file, wholesale) along with the deterministic
detector engine. Issue #379 replaces it with the quote-based patcher
(`scripts/redline_quote_apply.py::apply_quote_patches`, issue #377) --
this file reintroduces equivalent coverage against the quote-based patch
shape (`{source_quote, new_text, rationale}`).

Covers:

  AC1. Gold fixture: a 5-paragraph draft, two quote-located patches ->
       delivered `.docx` is the UPLOAD with in-situ tracked changes around
       just the quoted SPAN at those two paragraphs (span-level, not
       whole-paragraph -- unlike the retired anchor path); every OTHER
       paragraph is unchanged; every upload zip entry is still present in
       the output. `docx-editor` repackages EVERY XML part on save
       (content-identical, not byte-identical -- see
       scripts/redline_quote_apply.py's own docstring, "Limitations"
       section), so this test asserts PARSED XML equality for untouched
       paragraphs, not raw byte equality.
  AC2. Export marker present (header1.xml/footer1.xml, wired into
       <w:sectPr>); a <w:footnoteReference> run lives inside each patched
       paragraph's <w:ins>; footnotes.xml carries the matching rationale
       text with correct sequential numbering across both footnoted
       patches.
  AC3. The output OOXML scan runs over the FINAL bytes `generate_redline`
       itself receives, not some earlier-stage artifact: a doctored
       `apply_quote_patches` result that plants a `<w:fldChar>` field code
       is caught by `generate_redline`'s own output-scan gate. A positive
       control (the same flow, undoctored) passes the scan and round-trip
       cleanly, proving this isn't just "the mock always fails."
  AC4. A patch whose quote does not exist anywhere in the draft becomes
       flag-only (`reason="not_found"`) and joins the analysis-report,
       partial-delivery path -- `status="OK"` (>=1 OTHER patch in the same
       batch applied, issue #379's result-mapping contract: never
       `MANUAL_REVIEW_REQUIRED` while anything applied), never a silent
       omission, while the other clean patches in the same batch still
       land.
  AC5. No tenant-brand strings anywhere in the emitted content (marker,
       footnotes, patched text, analysis_report).

Run standalone: `python tests/redline/test_inplace_tracked_changes.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCUMENT_PART = "word/document.xml"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _import_modules():
    missing = []
    rg = None
    redline_quote_apply_mod = None
    try:
        import redline_generate as _rg  # type: ignore

        rg = _rg
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_generate.py does not import ({exc}).\n"
            f"  FIX: implement issue #379 -- generate_redline() must call "
            f"redline_quote_apply.apply_quote_patches on the REQUEST_CHANGE "
            f"path."
        )
    try:
        import redline_quote_apply as _rqa  # type: ignore

        redline_quote_apply_mod = _rqa
    except ImportError as exc:
        missing.append(f"MISSING: scripts/redline_quote_apply.py does not import ({exc}).")
    return rg, redline_quote_apply_mod, missing


# ---------------------------------------------------------------------------
# Fixture: a 5-paragraph synthetic draft (python-docx allowed in tests only,
# per issue #290's convention -- also matches tests/test_redline_quote_apply
# .py's own fixture-building convention).
# ---------------------------------------------------------------------------

_PARAGRAPHS = [
    "This is the preamble paragraph, unrelated to any patch.",
    "The Vendor shall keep Client information confidential.",
    "This is an untouched filler paragraph two.",
    "The Vendor's liability shall not exceed $150,000.",
    "This Agreement shall be governed by the laws of Delaware.",
]

_SEC1_QUOTE = "keep Client information confidential"
_SEC1_NEW_TEXT = "keep Client information strictly confidential and indemnify for any breach"
_SEC1_RATIONALE = "Strengthens the standard confidentiality obligation."
_SEC3_QUOTE = "shall not exceed $150,000"
_SEC3_NEW_TEXT = "is uncapped"
_SEC3_RATIONALE = "Restores the standard liability cap."
_SEC5_QUOTE = "this precise phrase does not appear anywhere in the draft"
_SEC5_NEW_TEXT = "never delivered -- sec-5's quote cannot be located"
_SEC5_RATIONALE = "sec-5's quote cannot be safely located."


def _make_draft_docx() -> bytes:
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for text in _PARAGRAPHS:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _make_issue(section_ref: str, *, replacement: str, rationale: str, source_quote: str) -> dict:
    return {
        "section_ref": section_ref,
        "section_title": "Section",
        "counterparty_change_summary": "Deviates from the standard position.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": rationale,
        "proposed_replacement_text": replacement,
        "playbook_topic_id": "generic-topic",
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


def _three_issue_scenario() -> list:
    """sec-1 and sec-3's quotes locate cleanly in the draft (both applied);
    sec-5's quote is deliberately absent from the draft entirely, simulating
    a target the patcher cannot locate (AC4)."""
    return [
        _make_issue("sec-1", replacement=_SEC1_NEW_TEXT, rationale=_SEC1_RATIONALE, source_quote=_SEC1_QUOTE),
        _make_issue("sec-3", replacement=_SEC3_NEW_TEXT, rationale=_SEC3_RATIONALE, source_quote=_SEC3_QUOTE),
        _make_issue("sec-5", replacement=_SEC5_NEW_TEXT, rationale=_SEC5_RATIONALE, source_quote=_SEC5_QUOTE),
    ]


def _run_generate_redline(rg, notes_mode="internal"):
    """The full 3-issue scenario used by AC1/AC2/AC4/AC5: sec-1 and sec-3
    apply cleanly in place, sec-5 fails to locate.

    `notes_mode` defaults to `"internal"` (issue #513: the export marker is
    conditional on notes mode -- present iff internal notes are included)
    so AC2's marker-presence assertions keep exercising the marker-present
    case; AC1/AC4/AC5 don't inspect header/footer at all so the override is
    harmless there. See `_check_marker_absent_without_internal_notes` for
    the marker-absent-by-default case."""
    issues = _three_issue_scenario()
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()
    draft_bytes = _make_draft_docx()

    result = rg.generate_redline(
        reconciled_result=reconciled,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
        notes_mode=notes_mode,
    )
    return draft_bytes, result


def _plant_fldchar(docx_bytes: bytes) -> bytes:
    """Doctor an already-produced quote-patched package by planting a
    `<w:fldChar>` field-code element into `word/document.xml` -- simulating
    a hostile/buggy patch that got past the quote-based patcher, used by
    AC3 to prove `generate_redline`'s output-OOXML scan actually runs over
    the FINAL bytes (not some earlier-stage artifact)."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}
    doc_xml = originals[DOCUMENT_PART]
    marker = doc_xml.find(b"<w:body>")
    if marker == -1:
        raise AssertionError("test fixture bug: <w:body> not found to doctor")
    insert_at = marker + len(b"<w:body>")
    doctored = (
        doc_xml[:insert_at]
        + b'<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p>'
        + doc_xml[insert_at:]
    )
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for info in infos:
            data = doctored if info.filename == DOCUMENT_PART else originals[info.filename]
            zf_out.writestr(info, data)
    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# AC1 -- in-place, gold fixture: patched spans in situ, everything else
# preserved.
# ---------------------------------------------------------------------------


def _check_ac1(rg, failures: list) -> None:
    draft_bytes, result = _run_generate_redline(rg)

    if result.get("status") != "OK":
        failures.append(
            f"[AC1-a] Expected status=OK (sec-1/sec-3 applied even though sec-5 "
            f"is unlocatable -- issue #379 result-mapping contract), got {result}"
        )
        return
    docx_bytes = result.get("docx_bytes")
    if not isinstance(docx_bytes, (bytes, bytearray)) or not docx_bytes:
        failures.append(f"[AC1-b] Expected a partial redline docx, got {docx_bytes!r}")
        return

    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf_out, zipfile.ZipFile(
        io.BytesIO(draft_bytes)
    ) as zf_in:
        out_names = set(zf_out.namelist())
        in_names = set(zf_in.namelist())
        if not in_names.issubset(out_names):
            failures.append(
                f"[AC1-c] Delivered package dropped upload zip entries: "
                f"{in_names - out_names}"
            )

        in_doc_root = ET.fromstring(zf_in.read(DOCUMENT_PART))
        out_doc_root = ET.fromstring(zf_out.read(DOCUMENT_PART))
        in_paras = [c for c in in_doc_root.find(_qn("body")) if c.tag == _qn("p")]
        out_paras = [c for c in out_doc_root.find(_qn("body")) if c.tag == _qn("p")]
        if len(out_paras) != len(in_paras):
            failures.append(
                f"[AC1-e] Expected {len(in_paras)} body paragraphs (same as upload), "
                f"got {len(out_paras)}"
            )
            return

        # Untouched paragraphs: PARSED XML equality, not raw byte equality
        # -- docx-editor re-serializes every XML part on save (content-
        # identical, not byte-identical; see scripts/redline_quote_apply
        # .py's own docstring, "Limitations" section).
        for idx in (0, 2, 4):
            expected = ET.tostring(in_paras[idx], encoding="unicode")
            actual = ET.tostring(out_paras[idx], encoding="unicode")
            if expected != actual:
                failures.append(f"[AC1-f] Untouched paragraph {idx} differs from upload.")

        for idx, quote, new_text in ((1, _SEC1_QUOTE, _SEC1_NEW_TEXT), (3, _SEC3_QUOTE, _SEC3_NEW_TEXT)):
            p = out_paras[idx]
            del_els = p.findall(_qn("del"))
            ins_els = p.findall(_qn("ins"))
            if len(del_els) != 1 or len(ins_els) != 1:
                failures.append(
                    f"[AC1-g] Patched paragraph {idx} should carry exactly one "
                    f"<w:del> and one <w:ins>, got {len(del_els)}/{len(ins_els)}."
                )
                continue
            del_text = "".join(
                e.text or "" for e in del_els[0].findall(f".//{_qn('delText')}")
            )
            if del_text != quote:
                failures.append(
                    f"[AC1-h] Paragraph {idx}'s <w:del> should equal the quoted "
                    f"span {quote!r}, got {del_text!r}."
                )
            ins_text = "".join(e.text or "" for e in ins_els[0].findall(f".//{_qn('t')}"))
            if new_text not in ins_text:
                failures.append(
                    f"[AC1-i] Paragraph {idx}'s <w:ins> should contain {new_text!r}, "
                    f"got {ins_text!r}"
                )
            # Span-level, not whole-paragraph: text OUTSIDE the quoted span
            # survives as ordinary runs alongside the del/ins.
            full_text = "".join(t.text or "" for t in p.findall(f".//{_qn('t')}")) + "".join(
                t.text or "" for t in p.findall(f".//{_qn('delText')}")
            )
            if _PARAGRAPHS[idx].split()[0] not in full_text:
                failures.append(
                    f"[AC1-j] Paragraph {idx} should retain its text OUTSIDE the "
                    f"quoted span (span-level edit, not whole-paragraph replace)."
                )


# ---------------------------------------------------------------------------
# AC2 -- export marker + footnoted rationales, correct shared numbering.
# ---------------------------------------------------------------------------


def _check_ac2(rg, failures: list) -> None:
    _draft_bytes, result = _run_generate_redline(rg)
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append("[AC2-a] Expected a partial redline docx to check the export marker on.")
        return

    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        names = set(zf.namelist())
        if "word/header1.xml" not in names or "word/footer1.xml" not in names:
            failures.append(
                f"[AC2-b] Expected word/header1.xml and word/footer1.xml. "
                f"Got: {sorted(names)}"
            )
            return
        header_bytes = zf.read("word/header1.xml")
        footer_bytes = zf.read("word/footer1.xml")
        marker_bytes = "contains internal notes".encode("utf-8")
        if marker_bytes not in header_bytes:
            failures.append("[AC2-c] Marker text not found in word/header1.xml.")
        if marker_bytes not in footer_bytes:
            failures.append("[AC2-d] Marker text not found in word/footer1.xml.")

        doc_root = ET.fromstring(zf.read(DOCUMENT_PART))
        header_refs = doc_root.findall(f".//{_qn('headerReference')}")
        footer_refs = doc_root.findall(f".//{_qn('footerReference')}")
        if not header_refs or not footer_refs:
            failures.append(
                "[AC2-e] <w:sectPr> is missing <w:headerReference>/<w:footerReference> "
                "-- the header/footer marker is not wired into the section."
            )

        if "word/footnotes.xml" not in names:
            failures.append("[AC2-f] Expected word/footnotes.xml for the footnoted rationales.")
            return
        footnotes_root = ET.fromstring(zf.read("word/footnotes.xml"))

        body = doc_root.find(_qn("body"))
        paras = [c for c in body if c.tag == _qn("p")]
        ref_ids_in_order = []
        for idx in (1, 3):  # sec-1, then sec-3, in document order
            ins_el = paras[idx].find(_qn("ins"))
            if ins_el is None:
                failures.append(f"[AC2-g] Paragraph {idx} missing <w:ins>.")
                continue
            refs = ins_el.findall(f".//{_qn('footnoteReference')}")
            if len(refs) != 1:
                failures.append(
                    f"[AC2-h] Paragraph {idx}'s <w:ins> should carry exactly one "
                    f"<w:footnoteReference>, got {len(refs)}."
                )
                continue
            ref_ids_in_order.append(refs[0].get(_qn("id")))

        if None in ref_ids_in_order or len(set(ref_ids_in_order)) != len(ref_ids_in_order):
            failures.append(f"[AC2-i] Footnote reference ids are not unique: {ref_ids_in_order!r}")

        footnotes_by_id = {
            fn.get(_qn("id")): fn for fn in footnotes_root.findall(_qn("footnote"))
        }
        for fid, rationale in zip(ref_ids_in_order, (_SEC1_RATIONALE, _SEC3_RATIONALE)):
            fn = footnotes_by_id.get(fid)
            if fn is None:
                failures.append(f"[AC2-j] No <w:footnote w:id={fid!r}> found in footnotes.xml.")
                continue
            text = "".join(t.text or "" for t in fn.findall(f".//{_qn('t')}"))
            if rationale not in text:
                failures.append(
                    f"[AC2-k] Footnote {fid} body should contain {rationale!r}, got {text!r}"
                )


# ---------------------------------------------------------------------------
# AC3 -- output-OOXML scan runs over the FINAL bytes.
# ---------------------------------------------------------------------------


def _check_ac3(rg, redline_quote_apply_mod, failures: list) -> None:
    issues = [_make_issue("sec-3", replacement=_SEC3_NEW_TEXT, rationale=_SEC3_RATIONALE, source_quote=_SEC3_QUOTE)]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()
    draft_bytes = _make_draft_docx()

    original_apply = redline_quote_apply_mod.apply_quote_patches

    def _doctoring_apply(*args, **kwargs):
        real_result = original_apply(*args, **kwargs)
        if real_result["docx_bytes"] is None:
            return real_result
        return {
            "docx_bytes": _plant_fldchar(real_result["docx_bytes"]),
            "applied": real_result["applied"],
            "flag_only": real_result["flag_only"],
        }

    with mock.patch.object(
        rg.redline_quote_apply, "apply_quote_patches", side_effect=_doctoring_apply
    ):
        doctored_result = rg.generate_redline(
            reconciled_result=reconciled,
            corpus=corpus,
            normalized_docx_bytes=draft_bytes,
        )

    if doctored_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[AC3-a] Expected ERROR_MANUAL_REVIEW_REQUIRED when the assembled "
            f"bytes carry a planted <w:fldChar>, got {doctored_result}"
        )
    if doctored_result.get("reason") != "output_ooxml_scan_failed":
        failures.append(
            f"[AC3-b] Expected reason=output_ooxml_scan_failed, got "
            f"{doctored_result.get('reason')}"
        )
    if doctored_result.get("docx_bytes") is not None:
        failures.append("[AC3-c] A scan-blocked document must never be delivered.")

    # Positive control: the SAME flow, undoctored, passes the scan and
    # round-trip cleanly -- proves AC3 isn't just "the mock always fails".
    clean_result = rg.generate_redline(
        reconciled_result=reconciled,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
    )
    if clean_result.get("status") != "OK":
        failures.append(
            f"[AC3-d] Undoctored quote-based redline should pass with status=OK, got "
            f"{clean_result}"
        )
    else:
        docx_bytes = clean_result.get("docx_bytes")
        try:
            rg.run_output_ooxml_scan(docx_bytes)
        except rg.OutputScanError as exc:
            failures.append(
                f"[AC3-e] Output OOXML scan unexpectedly failed on the clean "
                f"quote-based redline: {exc}"
            )
        try:
            rg.verify_docx_round_trip(docx_bytes)
        except ValueError as exc:
            failures.append(
                f"[AC3-f] Round-trip verification unexpectedly failed on the clean "
                f"quote-based redline: {exc}"
            )


# ---------------------------------------------------------------------------
# AC4 -- an unlocatable quote joins the analysis-report, partial-delivery
# path -- status=OK (issue #379: MANUAL_REVIEW_REQUIRED only on ZERO
# applied), never a silent omission.
# ---------------------------------------------------------------------------


def _check_ac4(rg, failures: list) -> None:
    _draft_bytes, result = _run_generate_redline(rg)
    if result.get("status") != "OK":
        failures.append(
            f"[AC4-a] Expected status=OK (sec-1/sec-3 applied; only sec-5 is "
            f"unlocatable -- issue #379: MANUAL_REVIEW_REQUIRED only on zero "
            f"applied), got {result}"
        )
        return
    if result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[AC4-b] Expected decision=REQUEST_CHANGE, got {result.get('decision')!r}")

    analysis_report = result.get("analysis_report")
    if not analysis_report or analysis_report.get("report_type") != "analysis_report":
        failures.append(f"[AC4-c] Expected an analysis_report artifact, got {analysis_report}")
        return
    if "decision" in analysis_report:
        failures.append("[AC4-g] analysis_report must never carry an ACCEPT/REQUEST_CHANGE decision field.")

    changes_not_applied = analysis_report.get("changes_not_applied", [])
    not_applied_refs = {c.get("section_ref") for c in changes_not_applied}
    if "sec-5" not in not_applied_refs:
        failures.append(f"[AC4-d] Expected sec-5 in changes_not_applied, got {not_applied_refs}")
    if "sec-1" in not_applied_refs or "sec-3" in not_applied_refs:
        failures.append(
            f"[AC4-e] sec-1/sec-3 were cleanly located and applied -- they must not "
            f"appear in changes_not_applied. Got {not_applied_refs}"
        )
    sec5_entry = next((c for c in changes_not_applied if c.get("section_ref") == "sec-5"), None)
    if sec5_entry is not None and sec5_entry.get("reason") != "not_found":
        failures.append(f"[AC4-f2] Expected sec-5's entry to carry reason=not_found, got {sec5_entry.get('reason')!r}")

    # Partial delivery: sec-1/sec-3's clean patches still land in the docx --
    # never a silent omission alongside sec-5's failure.
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append("[AC4-h] Expected a partial redline docx alongside the analysis report.")
        return
    doc_root = ET.fromstring(zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))).read(DOCUMENT_PART))
    all_ins_text = "".join(
        (t.text or "")
        for ins in doc_root.findall(f".//{_qn('ins')}")
        for t in ins.findall(f".//{_qn('t')}")
    )
    if _SEC3_NEW_TEXT not in all_ins_text:
        failures.append("[AC4-i] sec-3's clean patch should still be present in the partial redline.")
    if _SEC5_NEW_TEXT in all_ins_text:
        failures.append("[AC4-j] sec-5's unlocatable patch must NOT have been applied (no approximate match).")


# ---------------------------------------------------------------------------
# AC5 -- no tenant-brand strings anywhere in the emitted content.
# ---------------------------------------------------------------------------


def _check_ac5(rg, failures: list) -> None:
    _draft_bytes, result = _run_generate_redline(rg)
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append("[AC5-a] Expected a partial redline docx to scan for de-branding.")
        return
    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        for name in zf.namelist():
            if not name.endswith(".xml"):
                continue
            content = zf.read(name)
            if b"Exos" in content or b"EXOS" in content:
                failures.append(f"[AC5-b] '{name}' contains a de-branding violation (tenant-brand strings).")

    analysis_report = result.get("analysis_report")
    if analysis_report is not None:
        report_text = str(analysis_report)
        if "Exos" in report_text or "EXOS" in report_text:
            failures.append("[AC5-c] analysis_report contains a de-branding violation (tenant-brand strings).")


def _check_marker_absent_without_internal_notes(rg, failures: list) -> None:
    """Issue #513 AC: `notes_mode` values that carry no internal-audience
    content (the default `"external"`, plus `"none"`) must produce a
    `.docx` with NO export marker in any part -- no `word/header1.xml`, no
    `word/footer1.xml`, no `<w:headerReference>`/`<w:footerReference>` --
    unlike `_check_ac2` above, which explicitly opts into `notes_mode=
    "internal"` to exercise the marker-present case."""
    for notes_mode in ("external", "none"):
        _draft_bytes, result = _run_generate_redline(rg, notes_mode=notes_mode)
        docx_bytes = result.get("docx_bytes")
        if not docx_bytes:
            failures.append(
                f"[AC513-a/{notes_mode}] Expected a partial redline docx to check "
                f"marker absence on."
            )
            continue
        with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
            names = set(zf.namelist())
            if "word/header1.xml" in names or "word/footer1.xml" in names:
                failures.append(
                    f"[AC513-b/{notes_mode}] Expected NO header1.xml/footer1.xml "
                    f"with notes_mode={notes_mode!r}. Got: {sorted(names)}"
                )
            doc_root = ET.fromstring(zf.read(DOCUMENT_PART))
            if doc_root.findall(f".//{_qn('headerReference')}") or doc_root.findall(
                f".//{_qn('footerReference')}"
            ):
                failures.append(
                    f"[AC513-c/{notes_mode}] <w:sectPr> should carry no header/footer "
                    f"reference when no marker was injected."
                )


def main() -> int:
    failures: list = []

    rg, redline_quote_apply_mod, missing = _import_modules()
    if missing:
        print("FAIL: in-place redline wiring gate cannot run.\n")
        for m in missing:
            print(f"[G0] {m}")
            print()
        return 1

    try:
        import docx  # noqa: F401
    except ImportError as exc:
        print(f"FAIL: python-docx is required for this test's fixtures (test-only dependency): {exc}")
        return 1

    _check_ac1(rg, failures)
    _check_ac2(rg, failures)
    _check_ac3(rg, redline_quote_apply_mod, failures)
    _check_ac4(rg, failures)
    _check_ac5(rg, failures)
    _check_marker_absent_without_internal_notes(rg, failures)

    if failures:
        print("FAIL: in-place redline wiring gate (issue #379).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: in-place redline wiring gate (issue #379).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
