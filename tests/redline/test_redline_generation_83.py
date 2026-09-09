#!/usr/bin/env python3
"""
Slice test — redline generation end-to-end (issue #83, rewritten onto the
v3 block-transcript path by issue #628).

"Redline generation: tracked-changes docx, fail-closed patching, output
scan, export marker." Wires the reconciled issue list (#82) into the block
compiler (`scripts/block_transcript.py` proves, `scripts/redline_block_
apply.py` writes) end-to-end via `scripts/redline_generate.py::
generate_redline_from_blocks`.

## Block-path coverage

  1. A known issue list carrying a proven transcript -> the expected
     tracked-changes `.docx` (`w:ins`/`w:del` around the edited SPAN,
     footnotes, export marker).
  2. `{applied, failures}` -> result mapping: zero applied ->
     `MANUAL_REVIEW_REQUIRED` (`block_edits_not_applied`); at least one
     applied -> `OK` with partial delivery (docx_bytes present, an
     `analysis_report` names whichever edit(s) the writer refused); no
     edits attempted at all (every issue flag-only) -> `OK` with
     `docx_bytes=None` via `generate_redline`, which is the entry point the
     spine routes a transcript-less result to.
  3. Hostile replacement text (field syntax, hyperlink, XML metachars) is
     inserted as inert literal runs only, and the output-side OOXML
     external-relationship/field/embedded-object scan passes.
  4. A planted leakage string (#26/#73) blocks generation and gates the
     ACCEPT-path `verdict_summary` prose too.
  5. A Word round-trip check -- the docx writer opens its own output
     cleanly.

## Retired path coverage (issues #380/#628)

This file used to also cover an anchor/hash mismatch -> fail-closed
analysis report (issue #65's anchor/hash-patcher guarantee), and then the
quote path that replaced it (issue #379's `not_found` / `ambiguous` locate
outcomes). Both are gone: #380 retired the anchor/hash-joined patch path,
#628 deleted the quote locator and patcher. The analog guarantee under
block addressing is round-trip verification plus the per-edit compile
failures covered by part 2 below -- an edit whose span crosses a physical
`<w:p>` join inside one logical block is refused as
`spans_physical_paragraph` rather than half-written.

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
            f"  FIX: implement the issue #626 wiring of "
            f"scripts/redline_block_apply.py::apply_block_transcript into "
            f"generate_redline_from_blocks."
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
# A THIRD physical paragraph, so the single logical block below carries two
# physical `<w:p>` joins. That is what lets part 2 express a compilable edit
# and a writer-refused one (a delete crossing a join) in the SAME block
# without the two overlapping -- the shape a real multi-`<w:p>` clause has.
_SEC10_TEXT = "Any notice under this Agreement shall be given in writing."

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
    normalized upload the block compiler edits in place. With no heading
    styles these sibling `<w:p>` runs normalize into ONE logical block whose
    `physical_spans` keeps them distinct. ElementTree-escapes nothing
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
    return _build_docx_bytes([_SEC8_TEXT, _SEC9_TEXT, _SEC10_TEXT])


def _block_id_and_text(rg) -> tuple:
    """The addressed block id and its own text for `_base_draft_docx_bytes()`,
    resolved through the SAME `build_block_map` a real review addresses
    through -- never a hand-written id, which could name a block the
    extractor never stamped."""
    ens = rg.extraction_normalization_stage
    normalized = ens.extract_and_normalize(_base_draft_docx_bytes())
    block_id, block = next(iter(ens.build_block_map(normalized["paragraphs"]).items()))
    return block_id, block["text"]


def _patches(rg, edits: list) -> list:
    """One `block_patch` expressing `edits` -- a list of
    `(needle, replacement, issue_key)` in DOCUMENT ORDER, each `needle` a
    verbatim span of the block's own text.

    The `keep` segments between them are filled in from the block's real
    text, so the transcript reproduces the paragraph end to end exactly as
    the prompt requires and as `block_transcript.validate_block_patches`
    proves. A `replacement` of `""` is a pure deletion.
    """
    block_id, text = _block_id_and_text(rg)
    segments: list = []
    cursor = 0
    for needle, replacement, issue_key in edits:
        index = text.index(needle, cursor)
        if index > cursor:
            segments.append({"op": "keep", "text": text[cursor:index]})
        segments.append({"op": "delete", "text": needle, "issue_key": issue_key})
        if replacement:
            segments.append({"op": "insert", "text": replacement, "issue_key": issue_key})
        cursor = index + len(needle)
    if cursor < len(text):
        segments.append({"op": "keep", "text": text[cursor:]})
    return [{"block_id": block_id, "segments": segments}]


#: A span that crosses a physical `<w:p>` join inside the one logical block.
#: The transcript PROVES (it describes the block correctly) and the WRITER
#: refuses it -- `redline_block_apply.REASON_SPANS_PHYSICAL_PARAGRAPH`.
_SPANNING_NEEDLE = "the laws of Delaware.\nAny notice under this Agreement"


def _make_issue(
    section_ref: str,
    *,
    rationale: str,
    issue_key: str = "I1",
    replacement: str | None = None,
    topic_id: str = "limitation-of-liability",
    replacement_text_outcome: str | None = None,
) -> dict:
    issue = {
        "issue_key": issue_key,
        "section_ref": section_ref,
        "section_title": "Section",
        "counterparty_change_summary": "Deletes the standard position.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": rationale,
        "playbook_topic_id": topic_id,
        "internal_precedent_citation": None,
        "provenance": "model",
    }
    # Under v3 the model does not author this field -- the pipeline DERIVES
    # it from the proven transcript. It is set here only for the flag-only
    # case, where there is no transcript to derive from.
    if replacement is not None:
        issue["proposed_replacement_text"] = replacement
    # Issue #585: mirror the real reconciled shape, where an empty-text
    # issue already carries the enforcement-set label by the time it
    # reaches redline_generate.
    if replacement_text_outcome is not None:
        issue["replacement_text_outcome"] = replacement_text_outcome
    return issue


def _reconciled(
    issues: list,
    *,
    decision: str = "REQUEST_CHANGE",
    verdict_summary=None,
    block_patches: list | None = None,
) -> dict:
    return {
        "schema_version": "output-schema-v3",
        "decision": decision,
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": issues,
        "block_patches": block_patches or [],
        "block_ops": [],
        "critic_delta": None,
        "verdict_summary": verdict_summary,
    }


def _part_1_known_issue_list(rg, failures: list) -> None:
    """AC1: a known issue list carrying a proven transcript -> correct
    `w:ins`/`w:del` around the quoted span, footnoted rationale, redundant
    export marker on every page header/footer. Marker presence is
    conditional on notes mode (issue #513); this part passes
    `notes_mode="both"` explicitly to exercise the marker-present case
    -- see `_part_6_marker_conditional_on_notes_mode` for the
    marker-absent-by-default case.

    `"both"`, not `"internal"`: since issue #522 the FOOTNOTES are
    audience-aware too, and this issue carries only an
    `external_rationale_for_footnote` -- under `"internal"` it would
    produce no footnote at all, which is that ticket's contract, not a
    regression in this one. `"both"` is the mode that carries the marker
    AND the external rationale this part asserts on. The four modes' own
    footnote outcomes are gated by
    `tests/redline/test_footnote_audience_modes_522.py`."""
    issues = [_make_issue("sec-8", rationale="Restores the standard liability cap.")]
    reconciled = _reconciled(
        issues,
        block_patches=_patches(rg, [("shall not exceed $150,000", "is uncapped", "I1")]),
    )
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline_from_blocks(
        reconciled_result=reconciled,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
        notes_mode="both",
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
        # Everything OUTSIDE the edited span survives untouched (span-level
        # edit, not a whole-paragraph replace).
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
            marker_bytes = "contains internal notes".encode("utf-8")
            if marker_bytes not in header_text:
                failures.append("[1k] Marker text not found in word/header1.xml.")
            if marker_bytes not in footer_text:
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
    """AC2: `{applied, failures}` -> result mapping. Three sub-cases: zero
    applied -> MANUAL_REVIEW_REQUIRED; partial delivery (some applied, some
    refused) -> OK with an analysis_report; no edits attempted at all (every
    issue flag-only) -> OK with docx_bytes=None, but STILL a labelled
    analysis_report (issue #585 review round 1, finding 2 -- an
    all-flag-only batch is no longer silent)."""
    corpus = rg.leakage_scan.ConfidentialCorpus()

    # --- 2a: zero applied -- the one edit crosses a physical paragraph join,
    #     so it proves against the block and the WRITER refuses it ----------
    spanning_issue = _make_issue("sec-8", rationale="Restores the standard liability cap.")
    result = rg.generate_redline_from_blocks(
        reconciled_result=_reconciled(
            [spanning_issue],
            block_patches=_patches(rg, [(_SPANNING_NEEDLE, "is uncapped", "I1")]),
        ),
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(f"[2a] Expected status=MANUAL_REVIEW_REQUIRED (zero applied), got {result}")
    if result.get("reason") != rg.REASON_BLOCK_EDITS_NOT_APPLIED:
        failures.append(f"[2b] Expected reason=block_edits_not_applied, got {result.get('reason')!r}")
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
        elif changes[0].get("reason") != "spans_physical_paragraph":
            failures.append(
                f"[2h] Expected reason=spans_physical_paragraph on the entry, got "
                f"{changes[0].get('reason')!r}"
            )

    # --- 2b: partial delivery -- one compilable edit, one the writer refuses
    #     Both live in the SAME addressed block, in document order, and do
    #     not overlap: I1 edits inside the FIRST physical paragraph, I2's
    #     delete crosses the second join.
    mixed_result = rg.generate_redline_from_blocks(
        reconciled_result=_reconciled(
            [
                _make_issue("sec-8", rationale="Restores the standard liability cap.", issue_key="I1"),
                _make_issue("sec-9", rationale="Governing law should be mutual.", issue_key="I2"),
            ],
            block_patches=_patches(
                rg,
                [
                    ("shall not exceed $150,000", "is uncapped", "I1"),
                    (_SPANNING_NEEDLE, "is uncapped", "I2"),
                ],
            ),
        ),
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
            failures.append("[2l] The compilable edit should still have applied in the partial delivery.")
    mixed_report = mixed_result.get("analysis_report")
    if not mixed_report:
        failures.append(f"[2m] Expected an analysis_report naming the refused edit, got {mixed_report}")
    elif len(mixed_report.get("changes_not_applied", [])) != 1:
        failures.append(f"[2n] Expected exactly 1 changes_not_applied entry, got {mixed_report.get('changes_not_applied')}")
    elif mixed_report["changes_not_applied"][0].get("section_ref") != "sec-9":
        failures.append(
            f"[2n2] The refused edit's OWN issue must be the one reported, got "
            f"{mixed_report['changes_not_applied'][0]!r}"
        )

    # --- 2c: no edits attempted at all -- every issue flag-only ------------
    # A transcript-less result is what `review_spine` routes to
    # `generate_redline` (`uses_block_mode` is False), so this sub-case
    # deliberately goes through THAT entry point.
    flag_only_issue = _make_issue(
        "sec-9",
        replacement="",  # mode='none' -- flag only, no replacement proposed
        rationale="Flagging for attorney review; no specific replacement.",
        topic_id="generic-topic",
        replacement_text_outcome="flag_only_mode_none",
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
    # Issue #585 review round 1, finding 2: pre-#585 this asserted NO
    # analysis_report for an all-flag-only batch -- that was exactly the
    # silent-empty-string outcome the ticket rejects. It must now carry a
    # labelled analysis_report naming WHY (mode='none' here), even though
    # nothing was attempted and no docx_bytes is produced.
    flag_only_report = flag_only_result.get("analysis_report")
    if not flag_only_report:
        failures.append(
            f"[2q] An all-flag-only batch must carry a labelled analysis_report "
            f"(issue #585); got {flag_only_report}"
        )
    else:
        if flag_only_report.get("report_type") != "flag_only_report":
            failures.append(
                f"[2q2] An all-flag-only batch's report must be a flag_only_report, "
                f"never an apply-failure report; got {flag_only_report.get('report_type')!r}"
            )
        flag_changes = flag_only_report.get("changes_not_applied", [])
        if len(flag_changes) != 1 or flag_changes[0].get("section_ref") != "sec-9":
            failures.append(f"[2r] Expected sec-9 in changes_not_applied, got {flag_changes}")
        elif flag_changes[0].get("reason") != "flag_only_mode_none":
            failures.append(
                f"[2s] Expected reason=flag_only_mode_none (never confused with a "
                f"'could not apply' failure), got {flag_changes[0].get('reason')!r}"
            )


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
            rationale='Rationale with metachars: <tag> & "quoted"',
        )
    ]
    reconciled = _reconciled(
        issues,
        block_patches=_patches(rg, [("shall not exceed $150,000", hostile_text, "I1")]),
    )
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline_from_blocks(
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
    independently of, the block compiler -- unaffected by issue #628's
    rewrite of the REQUEST_CHANGE branch. The ACCEPT sub-cases go through
    `generate_redline`, which is the entry point `review_spine` routes a
    transcript-less result to."""
    system_prompt_secret = "You are the confidential internal review assistant codenamed FALCON."
    corpus = rg.leakage_scan.ConfidentialCorpus(
        system_prompt_ngrams=[system_prompt_secret]
    )

    # --- REQUEST_CHANGE path: leakage planted in a rationale field --------
    issues = [_make_issue("sec-8", rationale=f"Internal note: {system_prompt_secret}")]
    reconciled = _reconciled(
        issues,
        block_patches=_patches(rg, [("shall not exceed $150,000", "is uncapped", "I1")]),
    )
    result = rg.generate_redline_from_blocks(
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

    # --- Clean REQUEST_CHANGE: the proven transcript compiles -------------
    clean_request_change = _reconciled(
        [_make_issue("sec-8", rationale="Restores the standard liability cap.")],
        block_patches=_patches(rg, [("shall not exceed $150,000", "is uncapped", "I1")]),
    )
    clean_rc_result = rg.generate_redline_from_blocks(
        reconciled_result=clean_request_change,
        corpus=corpus,
        normalized_docx_bytes=_base_draft_docx_bytes(),
    )
    if clean_rc_result["status"] != "OK" or clean_rc_result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[4g] Expected a clean REQUEST_CHANGE status=OK, got {clean_rc_result}")
    if not clean_rc_result.get("docx_bytes"):
        failures.append(
            f"[4h] Expected non-empty docx_bytes on a clean, compilable REQUEST_CHANGE, "
            f"got {clean_rc_result.get('docx_bytes')!r}"
        )
    if clean_rc_result.get("analysis_report") is not None:
        failures.append(
            f"[4i] Expected no analysis_report (nothing flag-only), got "
            f"{clean_rc_result['analysis_report']}"
        )


def _part_5_word_round_trip(rg, failures: list) -> None:
    """AC5: a Word round-trip check -- the docx writer opens its own output
    cleanly."""
    issues = [_make_issue("sec-8", rationale="Restores the standard liability cap.")]
    reconciled = _reconciled(
        issues,
        block_patches=_patches(rg, [("shall not exceed $150,000", "is uncapped", "I1")]),
    )
    corpus = rg.leakage_scan.ConfidentialCorpus()

    result = rg.generate_redline_from_blocks(
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


def _part_6_marker_conditional_on_notes_mode(rg, failures: list) -> None:
    """Issue #513 AC: the export marker is conditional on this review's
    notes mode -- present iff internal notes are included, absent
    otherwise. `notes_mode="external"` (today's default, and the value
    `_part_1` deliberately overrides to `"both"` to test the
    marker-present case above) and `notes_mode="none"` must both produce a
    `.docx` with NO marker in any part: no `word/header1.xml`, no
    `word/footer1.xml`, no `<w:headerReference>`/`<w:footerReference>` on
    `<w:sectPr>` -- nothing to de-mark afterward. The OMITTED-notes_mode
    call (no kwarg at all) must behave identically to the explicit
    `"external"` default, since that IS the parameter's default value."""
    issues = [_make_issue("sec-8", rationale="Restores the standard liability cap.")]
    reconciled = _reconciled(
        issues,
        block_patches=_patches(rg, [("shall not exceed $150,000", "is uncapped", "I1")]),
    )
    corpus = rg.leakage_scan.ConfidentialCorpus()

    def _assert_no_marker(case: str, docx_bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
            names = set(zf.namelist())
            if "word/header1.xml" in names or "word/footer1.xml" in names:
                failures.append(
                    f"[6-{case}] Expected NO header1.xml/footer1.xml with no "
                    f"internal notes in scope. Got parts: {sorted(names)}"
                )
            doc_root = ET.fromstring(zf.read("word/document.xml"))
            if doc_root.findall(f".//{_qn('headerReference')}") or doc_root.findall(
                f".//{_qn('footerReference')}"
            ):
                failures.append(
                    f"[6-{case}] <w:sectPr> should carry no header/footer "
                    f"reference when no marker was injected."
                )
            all_text = "".join(t.text or "" for t in doc_root.findall(f".//{_qn('t')}"))
            if "internal notes" in all_text or "attorney approval" in all_text:
                failures.append(f"[6-{case}] Marker text leaked into document.xml body.")

    for case, kwargs in (
        ("default-omitted", {}),
        ("external-explicit", {"notes_mode": "external"}),
        ("none", {"notes_mode": "none"}),
    ):
        result = rg.generate_redline_from_blocks(
            reconciled_result=reconciled,
            corpus=corpus,
            normalized_docx_bytes=_base_draft_docx_bytes(),
            **kwargs,
        )
        docx_bytes = result.get("docx_bytes")
        if not docx_bytes:
            failures.append(f"[6-{case}] Expected non-empty docx bytes, got {docx_bytes!r}")
            continue
        _assert_no_marker(case, docx_bytes)


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
    _part_6_marker_conditional_on_notes_mode(redline_generate, failures)

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
