#!/usr/bin/env python3
"""
RED test — issue #291 (In-place redline 2/2): wiring the slice-1 patcher
(`scripts/redline_inplace.py`, issue #290) into `generate_redline` so the
delivered `.docx` IS the uploaded document redlined in place -- export
marker, footnoted rationales, output-OOXML scan, and round-trip
verification all running over the FINAL in-place bytes.

Covers the issue's five acceptance criteria:

  1. Gold fixture: a 5-paragraph synthetic draft, two modified sections ->
     delivered `.docx` is the UPLOAD with in-situ tracked changes at those
     sections; every OTHER paragraph's `word/document.xml` XML is
     unchanged; every zip entry the patch batch doesn't touch (styles.xml,
     settings.xml, theme1.xml, ...) is byte-identical to the upload.
  2. Export marker present on the delivered doc (header1.xml/footer1.xml);
     footnote reference runs live INSIDE each patched paragraph's <w:ins>,
     and footnotes.xml carries the matching body text with correct shared
     (sequential) numbering across both footnoted patches.
  3. Output OOXML scan and round-trip verification run over the in-place
     bytes, not some earlier-stage artifact: a doctored patch that plants a
     <w:fldChar> field code into the in-place patcher's own output is
     caught by generate_redline's output-scan gate.
  4. A patch the in-place patcher cannot safely locate (its text drifted /
     was never in the draft) produces the internal-analysis-report,
     partial-delivery path -- never a silent omission -- while the OTHER
     clean patches in the same batch still land.
  5. No 'Exos'/'EXOS' anywhere in the emitted content (marker, footnotes,
     patched text).

This test FAILS on the unmodified tree because `generate_redline` builds a
STANDALONE synthetic-body docx (`redline_docx_writer.build_tracked_changes_docx`)
instead of patching the uploaded package in place, and does not accept a
`normalized_docx_bytes` parameter at all.

Run standalone: `python tests/redline/test_inplace_tracked_changes.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import hashlib
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


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _import_modules():
    missing = []
    rg = None
    redline_inplace_mod = None
    try:
        import redline_generate as _rg  # type: ignore

        rg = _rg
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_generate.py does not import ({exc}).\n"
            f"  FIX: implement issue #291 -- generate_redline() must accept "
            f"a normalized_docx_bytes parameter and wire "
            f"redline_inplace.apply_tracked_changes_inplace into its "
            f"REQUEST_CHANGE path."
        )
    try:
        import redline_inplace as _ri  # type: ignore

        redline_inplace_mod = _ri
    except ImportError as exc:
        missing.append(f"MISSING: scripts/redline_inplace.py does not import ({exc}).")
    return rg, redline_inplace_mod, missing


# ---------------------------------------------------------------------------
# Fixture: a 5-paragraph synthetic draft (python-docx allowed in tests only,
# per issue #290's convention).
# ---------------------------------------------------------------------------

_PARAGRAPHS = [
    "This is the preamble paragraph, unrelated to any patch.",
    "The Vendor shall keep Client information confidential.",
    "This is an untouched filler paragraph two.",
    "The Vendor's liability shall not exceed $150,000.",
    "This Agreement shall be governed by the laws of Delaware.",
]

_SEC1_NEW_TEXT = (
    "The Vendor shall keep Client information strictly confidential and "
    "indemnify for any breach."
)
_SEC1_RATIONALE = "Strengthens the standard confidentiality obligation."
_SEC3_NEW_TEXT = "The Vendor's liability is uncapped."
_SEC3_RATIONALE = "Restores the standard liability cap."
_SEC5_NEW_TEXT = "This text is never delivered -- sec-5 cannot be located."
_SEC5_RATIONALE = "sec-5's drifted text cannot be safely patched."


def _make_draft_docx() -> bytes:
    import docx  # local import: python-docx is a test-only dependency

    document = docx.Document()
    for text in _PARAGRAPHS:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _hunks_and_current_paragraphs():
    """3 anchors: sec-1 and sec-3 anchor real paragraphs in the draft (both
    will be cleanly located and patched in place); sec-5's hunk `text` is
    deliberately text that does NOT appear anywhere in the draft body,
    simulating a target the in-place patcher cannot locate (AC4) even
    though the anchor/hash gate upstream (`redline_patch.apply_patches`)
    lets it through -- the standard-form `current_paragraphs_by_anchor`
    value hashes to exactly what each hunk's `source_text_hash` expects, by
    construction."""
    std_text_sec1 = "STANDARD FORM: Vendor confidentiality obligation text."
    std_text_sec3 = "STANDARD FORM: Vendor liability cap text."
    std_text_sec5 = "STANDARD FORM: some other clause text."

    hunks = [
        {
            "anchor": "sec-1",
            "kind": "modified_new",
            "text": _PARAGRAPHS[1],
            "source_text_hash": _sha256_text(std_text_sec1),
        },
        {
            "anchor": "sec-3",
            "kind": "modified_new",
            "text": _PARAGRAPHS[3],
            "source_text_hash": _sha256_text(std_text_sec3),
        },
        {
            "anchor": "sec-5",
            "kind": "modified_new",
            "text": "This text does not appear anywhere in the draft body.",
            "source_text_hash": _sha256_text(std_text_sec5),
        },
    ]
    current_paragraphs_by_anchor = {
        "sec-1": std_text_sec1,
        "sec-3": std_text_sec3,
        "sec-5": std_text_sec5,
    }
    return hunks, current_paragraphs_by_anchor


def _make_issue(section_ref: str, *, replacement: str, rationale: str) -> dict:
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


def _run_generate_redline(rg):
    """The full 3-anchor scenario used by AC1/AC2/AC4/AC5: sec-1 and sec-3
    apply cleanly in place, sec-5 fails at the in-place-locate layer."""
    hunks, current_paragraphs_by_anchor = _hunks_and_current_paragraphs()
    issues = [
        _make_issue("sec-1", replacement=_SEC1_NEW_TEXT, rationale=_SEC1_RATIONALE),
        _make_issue("sec-3", replacement=_SEC3_NEW_TEXT, rationale=_SEC3_RATIONALE),
        _make_issue("sec-5", replacement=_SEC5_NEW_TEXT, rationale=_SEC5_RATIONALE),
    ]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()
    draft_bytes = _make_draft_docx()

    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
    )
    return draft_bytes, result


def _plant_fldchar(docx_bytes: bytes) -> bytes:
    """Doctor an already-produced in-place package by planting a
    `<w:fldChar>` field-code element into `word/document.xml` -- simulating
    a hostile/buggy patch that got past the in-place patcher, used by AC3
    to prove `generate_redline`'s output-OOXML scan actually runs over the
    FINAL in-place bytes (not some earlier-stage artifact)."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        infos = zf.infolist()
        originals = {info.filename: zf.read(info.filename) for info in infos}
    doc_xml = originals[DOCUMENT_PART]
    doctored = doc_xml.replace(
        b"<w:body>",
        b'<w:body><w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p>',
        1,
    )
    if doctored == doc_xml:
        raise AssertionError("test fixture bug: <w:body> not found to doctor")
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for info in infos:
            data = doctored if info.filename == DOCUMENT_PART else originals[info.filename]
            zf_out.writestr(info, data)
    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# AC1 -- in-place, gold fixture: patched sections in situ, everything else
# byte/XML-identical to the upload.
# ---------------------------------------------------------------------------


def _check_ac1(rg, failures: list) -> None:
    draft_bytes, result = _run_generate_redline(rg)

    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[AC1-a] Expected status=MANUAL_REVIEW_REQUIRED (sec-5 is unlocatable), "
            f"got {result}"
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
        injected_parts = {
            DOCUMENT_PART,
            "word/header1.xml",
            "word/footer1.xml",
            "word/footnotes.xml",
            "word/_rels/document.xml.rels",
            "[Content_Types].xml",
        }
        if not in_names.issubset(out_names):
            failures.append(
                f"[AC1-c] Delivered package dropped upload zip entries: "
                f"{in_names - out_names}"
            )
        for name in in_names & out_names:
            if name in injected_parts:
                continue
            if zf_out.read(name) != zf_in.read(name):
                failures.append(
                    f"[AC1-d] Zip entry '{name}' is not byte-identical to the upload."
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

        for idx in (0, 2, 4):  # untouched paragraphs
            expected = ET.tostring(in_paras[idx], encoding="unicode")
            actual = ET.tostring(out_paras[idx], encoding="unicode")
            if expected != actual:
                failures.append(f"[AC1-f] Untouched paragraph {idx} differs from upload.")

        for idx, new_text in ((1, _SEC1_NEW_TEXT), (3, _SEC3_NEW_TEXT)):
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
            if del_text != _PARAGRAPHS[idx]:
                failures.append(
                    f"[AC1-h] Paragraph {idx}'s <w:del> should equal the original "
                    f"draft text {_PARAGRAPHS[idx]!r}, got {del_text!r}."
                )
            ins_text = "".join(e.text or "" for e in ins_els[0].findall(f".//{_qn('t')}"))
            if new_text not in ins_text:
                failures.append(
                    f"[AC1-i] Paragraph {idx}'s <w:ins> should contain {new_text!r}, "
                    f"got {ins_text!r}"
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
        if b"attorney approval required" not in header_bytes:
            failures.append("[AC2-c] Marker text not found in word/header1.xml.")
        if b"attorney approval required" not in footer_bytes:
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
                    f"<w:footnoteReference> (issue #291 scope item 3), got {len(refs)}."
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
# AC3 -- output-OOXML scan and round-trip run over the FINAL in-place bytes.
# ---------------------------------------------------------------------------


def _check_ac3(rg, redline_inplace_mod, failures: list) -> None:
    hunks, current_paragraphs_by_anchor = _hunks_and_current_paragraphs()
    only_sec3_hunks = [h for h in hunks if h["anchor"] == "sec-3"]
    only_sec3_current = {"sec-3": current_paragraphs_by_anchor["sec-3"]}
    issues = [_make_issue("sec-3", replacement=_SEC3_NEW_TEXT, rationale=_SEC3_RATIONALE)]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()
    draft_bytes = _make_draft_docx()

    original_apply = redline_inplace_mod.apply_tracked_changes_inplace

    def _doctoring_apply(*args, **kwargs):
        real_result = original_apply(*args, **kwargs)
        return redline_inplace_mod.InplaceResult(
            docx_bytes=_plant_fldchar(real_result.docx_bytes),
            applied=real_result.applied,
            failed=real_result.failed,
        )

    with mock.patch.object(
        redline_inplace_mod, "apply_tracked_changes_inplace", side_effect=_doctoring_apply
    ):
        doctored_result = rg.generate_redline(
            reconciled_result=reconciled,
            hunks=only_sec3_hunks,
            current_paragraphs_by_anchor=only_sec3_current,
            corpus=corpus,
            normalized_docx_bytes=draft_bytes,
        )

    if doctored_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[AC3-a] Expected ERROR_MANUAL_REVIEW_REQUIRED when the in-place bytes "
            f"carry a planted <w:fldChar>, got {doctored_result}"
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
        hunks=only_sec3_hunks,
        current_paragraphs_by_anchor=only_sec3_current,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
    )
    if clean_result.get("status") != "OK":
        failures.append(
            f"[AC3-d] Undoctored in-place redline should pass with status=OK, got "
            f"{clean_result}"
        )
    else:
        docx_bytes = clean_result.get("docx_bytes")
        try:
            rg.run_output_ooxml_scan(docx_bytes)
        except rg.OutputScanError as exc:
            failures.append(
                f"[AC3-e] Output OOXML scan unexpectedly failed on the clean "
                f"in-place redline: {exc}"
            )
        try:
            rg.verify_docx_round_trip(docx_bytes)
        except ValueError as exc:
            failures.append(
                f"[AC3-f] Round-trip verification unexpectedly failed on the clean "
                f"in-place redline: {exc}"
            )


# ---------------------------------------------------------------------------
# AC4 -- a patch the in-place patcher cannot locate joins the
# internal-analysis-report, partial-delivery path.
# ---------------------------------------------------------------------------


def _check_ac4(rg, failures: list) -> None:
    _draft_bytes, result = _run_generate_redline(rg)
    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[AC4-a] Expected MANUAL_REVIEW_REQUIRED (sec-5 is unlocatable), got {result}")
        return
    # sec-5 fails at the IN-PLACE-LOCATE layer (its text never appears in
    # the draft body) even though the earlier anchor/hash join already
    # passed for that anchor -- the correct reason is the distinct
    # `inplace_locate_failed`, not a hash-mismatch label (issue #291 review
    # finding 3: reporting this as `hash_mismatch_at_patch` would mislabel
    # the real cause).
    if result.get("reason") != "inplace_locate_failed":
        failures.append(f"[AC4-b] Expected reason=inplace_locate_failed, got {result.get('reason')}")

    analysis_report = result.get("analysis_report")
    if not analysis_report or analysis_report.get("report_type") != "analysis_report":
        failures.append(f"[AC4-c] Expected an analysis_report artifact, got {analysis_report}")
        return

    # Review finding: the machine-readable `reason` alone is not enough --
    # the attorney-facing `fail_closed_path` string must also describe the
    # REAL trigger (anchor/hash join passed, in-place locate failed), never
    # fall through to the un-normalizable-input wording ("the normalization
    # pass could not produce a clean, unambiguous document body"), which
    # would tell the attorney normalization failed when it did not. Wording
    # must match docs/output-contract.md's "In-place locate failure at
    # patch time" row.
    fail_closed_path = analysis_report.get("fail_closed_path", "")
    if "could not safely locate the target paragraph" not in fail_closed_path:
        failures.append(
            f"[AC4-k] fail_closed_path must describe the in-place-locate "
            f"failure (per output-contract.md's in-place-locate-failure row), "
            f"got {fail_closed_path!r}"
        )
    if "normalization pass could not produce" in fail_closed_path:
        failures.append(
            f"[AC4-l] fail_closed_path must NOT use the un-normalizable-input "
            f"wording for an inplace_locate_failed reason -- normalization "
            f"succeeded here; only the in-place locate failed. Got "
            f"{fail_closed_path!r}"
        )

    not_applied_anchors = {c.get("anchor") for c in analysis_report.get("changes_not_applied", [])}
    if "sec-5" not in not_applied_anchors:
        failures.append(f"[AC4-d] Expected sec-5 in changes_not_applied, got {not_applied_anchors}")
    if "sec-1" in not_applied_anchors or "sec-3" in not_applied_anchors:
        failures.append(
            f"[AC4-e] sec-1/sec-3 were cleanly located and applied -- they must not "
            f"appear in changes_not_applied. Got {not_applied_anchors}"
        )
    if analysis_report.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append("[AC4-f] analysis_report must never carry a legal decision.")
    if "decision" in analysis_report:
        failures.append("[AC4-g] analysis_report must never carry an ACCEPT/REQUEST_CHANGE decision field.")

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
# AC5 -- no 'Exos'/'EXOS' anywhere in the emitted content.
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
                failures.append(f"[AC5-b] '{name}' contains a de-branding violation ('Exos'/'EXOS').")

    analysis_report = result.get("analysis_report")
    if analysis_report is not None:
        report_text = str(analysis_report)
        if "Exos" in report_text or "EXOS" in report_text:
            failures.append("[AC5-c] analysis_report contains a de-branding violation ('Exos'/'EXOS').")


# ---------------------------------------------------------------------------
# AC6 -- regression (review of #291): a hunk's `text` is normalized/stripped
# draft text (what `diff_standard_form.py` actually carries, per
# `extraction_normalization_stage.normalize_paragraphs`), not the raw
# `<w:t>` concatenation `redline_inplace._paragraph_text` reads off the
# uploaded package. A paragraph whose OWN runs carry leading/trailing
# whitespace must still be located and patched -- not silently dropped as
# "not_found" -- and the `<w:del>` must faithfully carry the paragraph's
# ACTUAL raw text (including that edge whitespace), never a lossy delete of
# the normalized proxy used only to locate it.
# ---------------------------------------------------------------------------

_AC6_RAW_TARGET_TEXT = "  The Vendor shall pay within 30 days.  "
_AC6_NEW_TEXT = "The Vendor shall pay within 10 days."
_AC6_RATIONALE = "Tightens the standard payment term."


def _check_ac6_normalized_hunk_text_vs_raw_paragraph(rg, failures: list) -> None:
    import docx  # local import: test-only dependency

    # Real normalization pipeline output (issue #291 review finding 1/2) --
    # NOT a hand-rolled stand-in -- so this test exercises the actual
    # stripped shape a hunk's `text` field carries in the real pipeline.
    import extraction_normalization_stage as ens

    normalized = ens.normalize_paragraphs(
        [
            {
                "heading": "<untitled>",
                "physical_paragraphs": [{"text": _AC6_RAW_TARGET_TEXT, "revisions": []}],
            }
        ]
    )
    hunk_text = normalized["paragraphs"][0]["text"]
    if hunk_text == _AC6_RAW_TARGET_TEXT:
        failures.append(
            "[AC6-fixture] Test fixture bug: normalization did not strip the "
            "target paragraph's edge whitespace, so this test would not "
            "exercise the raw-vs-normalized mismatch."
        )
        return

    document = docx.Document()
    document.add_paragraph("Preamble paragraph, unrelated to any patch.")
    document.add_paragraph(_AC6_RAW_TARGET_TEXT)
    buf = io.BytesIO()
    document.save(buf)
    draft_bytes = buf.getvalue()

    std_text = "STANDARD FORM: Vendor payment term text."
    hunks = [
        {
            "anchor": "sec-pay",
            "kind": "modified_new",
            "text": hunk_text,
            "source_text_hash": _sha256_text(std_text),
        }
    ]
    current_paragraphs_by_anchor = {"sec-pay": std_text}
    issues = [_make_issue("sec-pay", replacement=_AC6_NEW_TEXT, rationale=_AC6_RATIONALE)]
    reconciled = _reconciled(issues)
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline(
        reconciled_result=reconciled,
        hunks=hunks,
        current_paragraphs_by_anchor=current_paragraphs_by_anchor,
        corpus=corpus,
        normalized_docx_bytes=draft_bytes,
    )

    if result.get("status") != "OK":
        failures.append(
            f"[AC6-a] Expected status=OK (the only patch should apply cleanly "
            f"despite the hunk carrying normalized/stripped text), got {result}"
        )
        return
    if result.get("analysis_report") is not None:
        failures.append(
            f"[AC6-b] Expected no analysis_report (no silent omission of the "
            f"REQUEST_CHANGE edit), got {result.get('analysis_report')}"
        )

    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append("[AC6-c] Expected a delivered redline docx.")
        return

    doc_root = ET.fromstring(zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))).read(DOCUMENT_PART))
    paras = [c for c in doc_root.find(_qn("body")) if c.tag == _qn("p")]
    if len(paras) != 2:
        failures.append(f"[AC6-d] Expected 2 body paragraphs, got {len(paras)}.")
        return

    target = paras[1]
    del_els = target.findall(_qn("del"))
    ins_els = target.findall(_qn("ins"))
    if len(del_els) != 1 or len(ins_els) != 1:
        failures.append(
            f"[AC6-e] Target paragraph should carry exactly one <w:del> and one "
            f"<w:ins> (the patch must have been located and applied), got "
            f"{len(del_els)}/{len(ins_els)}."
        )
        return

    del_text = "".join(e.text or "" for e in del_els[0].findall(f".//{_qn('delText')}"))
    if del_text != _AC6_RAW_TARGET_TEXT:
        failures.append(
            f"[AC6-f] <w:del> should carry the paragraph's ACTUAL raw text "
            f"(with its own leading/trailing whitespace) {_AC6_RAW_TARGET_TEXT!r}, "
            f"got {del_text!r} -- a lossy delete of the normalized proxy used "
            f"only to locate the paragraph."
        )

    ins_text = "".join(e.text or "" for e in ins_els[0].findall(f".//{_qn('t')}"))
    if _AC6_NEW_TEXT not in ins_text:
        failures.append(f"[AC6-g] <w:ins> should contain {_AC6_NEW_TEXT!r}, got {ins_text!r}")

    # Footnote-reference injection (issue #291 review, second pass): AC2
    # only ever exercises whitespace-free paragraphs, so it never caught
    # `_find_patched_paragraph` comparing a RAW <w:del> delText (edge
    # whitespace included) against a STRIPPED source_text unstripped -- a
    # mismatch that silently drops the <w:footnoteReference> injection (and
    # orphans the footnote body in footnotes.xml) for exactly this
    # edge-whitespace paragraph class, even though the patch itself applies
    # cleanly. Assert the reference actually landed inside this <w:ins>.
    refs = ins_els[0].findall(f".//{_qn('footnoteReference')}")
    if len(refs) != 1:
        failures.append(
            f"[AC6-h] Edge-whitespace paragraph's <w:ins> should carry exactly "
            f"one <w:footnoteReference> (issue #291 scope item 3), got "
            f"{len(refs)} -- a raw-vs-stripped delText mismatch in "
            f"_find_patched_paragraph silently drops this injection."
        )
        return

    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        if "word/footnotes.xml" not in zf.namelist():
            failures.append("[AC6-i] Expected word/footnotes.xml for the footnoted rationale.")
            return
        footnotes_root = ET.fromstring(zf.read("word/footnotes.xml"))

    ref_id = refs[0].get(_qn("id"))
    footnote = None
    for fn in footnotes_root.findall(_qn("footnote")):
        if fn.get(_qn("id")) == ref_id:
            footnote = fn
            break
    if footnote is None:
        failures.append(f"[AC6-j] No <w:footnote w:id={ref_id!r}> found in footnotes.xml.")
        return
    footnote_text = "".join(t.text or "" for t in footnote.findall(f".//{_qn('t')}"))
    if _AC6_RATIONALE not in footnote_text:
        failures.append(
            f"[AC6-k] Footnote {ref_id} body should contain {_AC6_RATIONALE!r}, "
            f"got {footnote_text!r}"
        )


def main() -> int:
    failures: list = []

    rg, redline_inplace_mod, missing = _import_modules()
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
    _check_ac3(rg, redline_inplace_mod, failures)
    _check_ac4(rg, failures)
    _check_ac5(rg, failures)
    _check_ac6_normalized_hunk_text_vs_raw_paragraph(rg, failures)

    if failures:
        print("FAIL: in-place redline wiring gate (issue #291).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: in-place redline wiring gate (issue #291).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
