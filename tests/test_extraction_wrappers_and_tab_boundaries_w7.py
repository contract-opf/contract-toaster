#!/usr/bin/env python3
"""
QA remediation W7 items 1 and 4 (QA report 2026-10-31, area 4, top items #9
and #11): text the extractor dropped, and a boundary decision that depended
on how runs were split.

## W7-1 -- `w:dir`, `w:bdo`, `w:customXml` were skipped without recursion

`extraction_normalization_stage._walk_content` and `_iter_body_paragraphs`
walk an explicit tag allowlist, which is what keeps textbox bodies out by
construction -- and which also dropped, in silence, every text-bearing
wrapper nobody had listed. "Supplier shall <w:dir>not</w:dir> be liable"
reached the model as "Supplier shall  be liable"; a block-level
`w:customXml` paragraph vanished from the review entirely. The three
wrappers are now transparent (inline, block, row and cell level), the writer
can edit inside each one, and an element the walk still does not recognise
but which carries text is DISCLOSED (`extraction_status: "degraded"` plus a
counts-only note) instead of skipped.

## W7-4 -- tab-boundary voiding

`boundary_text` used to append every `w:tab` at the end of its run, so the
same visible `(a)<tab>Customer shall pay in 30 days.` decided differently
depending on where Word -- or the writer, splitting a run to write a tracked
change -- put a run boundary. One edit to such a paragraph moved the tab in
the projected document, turned the paragraph into a heading there, and the
projection proof voided the WHOLE redline after both paid passes. Tabs now
sit where they occur, and the one rule that keeps a lettered sub-clause body
text is stated over the visible text (`_boundary_decision_text`).

Both runners: no-argument `test_*` functions plus the `TESTS` list below.
Offline, synthetic, no model.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
for _dir in (SCRIPTS_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript as bt  # noqa: E402
import extraction_normalization_stage as stage  # noqa: E402
import redline_block_apply as rba  # noqa: E402
from w7_docx_helpers import docx, heading, p, r, replace_in_block  # noqa: E402

LIABILITY = "Supplier shall not be liable for indirect damages."
TERMINATION = "Customer may terminate for convenience on 5 days notice."


def _wrapped(tag: str, inner: str) -> str:
    attrs = ' w:val="rtl"' if tag in ("dir", "bdo") else ' w:element="term"'
    props = "<w:customXmlPr/>" if tag == "customXml" else ""
    return f"<w:{tag}{attrs}>{props}{inner}</w:{tag}>"


def _liability_doc(tag: str) -> bytes:
    body = p(r("Supplier shall ") + _wrapped(tag, r("not")) + r(" be liable for indirect damages."))
    return docx(heading("1. Limitation of Liability") + body)


def _texts(docx_bytes: bytes) -> list[str]:
    norm = stage.extract_and_normalize(docx_bytes)
    assert norm["status"] == "normalized", norm
    return [paragraph["text"] for paragraph in norm["paragraphs"]]


# ---------------------------------------------------------------------------
# W7-1
# ---------------------------------------------------------------------------


def test_text_inside_each_inline_wrapper_reaches_the_model() -> None:
    for tag in ("dir", "bdo", "customXml"):
        texts = _texts(_liability_doc(tag))
        assert texts == [LIABILITY], f"w:{tag}: the negation was dropped: {texts!r}"


def test_a_block_level_custom_xml_paragraph_is_not_dropped() -> None:
    body = _wrapped("customXml", p(r(TERMINATION)))
    texts = _texts(docx(heading("2. Termination") + body))
    assert texts == [TERMINATION], texts


def test_row_and_cell_level_custom_xml_are_transparent() -> None:
    row = f"<w:tr><w:tc>{p(r('Row-wrapped cell text.'))}</w:tc></w:tr>"
    cell = f"<w:tc>{p(r('Cell-wrapped cell text.'))}</w:tc>"
    table = (
        "<w:tbl>"
        + _wrapped("customXml", row)
        + "<w:tr>"
        + _wrapped("customXml", cell)
        + "</w:tr></w:tbl>"
    )
    joined = "\n".join(_texts(docx(heading("3. Schedule") + table)))
    for needle in ("Row-wrapped cell text.", "Cell-wrapped cell text."):
        assert needle in joined, f"{needle!r} missing from {joined!r}"


def test_the_writer_can_edit_inside_each_wrapper() -> None:
    """The redline round trip: an edit to the very word inside the wrapper
    proves, compiles, and accepting it yields the approved text."""
    for tag in ("dir", "bdo", "customXml"):
        out = replace_in_block(stage, bt, rba, _liability_doc(tag), "not", "never")
        assert out["proven"]["status"] == "proven", (tag, out["proven"]["failures"])
        result = out["result"]
        assert result["docx_bytes"] is not None and not result["failures"], (tag, result["failures"])
        assert out["accepted_text"] == out["expected_text"], (tag, out["accepted_text"])
    block_doc = docx(heading("2. Termination") + _wrapped("customXml", p(r(TERMINATION))))
    out = replace_in_block(stage, bt, rba, block_doc, "5 days", "30 days")
    assert out["result"] and out["result"]["docx_bytes"] is not None, out["result"]
    assert out["accepted_text"] == out["expected_text"], out["accepted_text"]


def test_recognised_wrappers_do_not_degrade_extraction() -> None:
    for tag in ("dir", "bdo", "customXml"):
        norm = stage.extract_and_normalize(_liability_doc(tag))
        assert "extraction_status" not in norm, (tag, norm.get("extraction_status"))


def test_an_unrecognised_text_carrier_is_disclosed_not_skipped() -> None:
    """Fail closed for the NEXT wrapper nobody has listed: an unknown
    element carrying text sets `extraction_status: "degraded"` and adds one
    counts-only note. Its text still does not reach the model -- an unknown
    container could be a payload one -- and nothing about it is echoed."""
    secret = "Unlisted wrapper words"
    inline = p(r("Supplier shall ") + f'<w:futureWrap w:val="x">{r(secret)}</w:futureWrap>' + r(" pay."))
    block = f"<w:futureBlock>{p(r(secret + ' block'))}</w:futureBlock>"
    norm = stage.extract_and_normalize(docx(heading("1. Fees") + inline + block))
    assert norm["status"] == "normalized", norm
    assert norm.get("extraction_status") == stage.EXTRACTION_STATUS_DEGRADED, norm
    notes = norm.get("normalization_notes", "")
    assert "Unrecognised markup not read: 2 element(s)" in notes, notes
    assert secret not in notes and "futureWrap" not in notes and "futureBlock" not in notes, notes
    assert all(secret not in paragraph["text"] for paragraph in norm["paragraphs"])
    # Not counted as an accepted edit by the receipt.
    assert "accepted-all into the operative draft." not in notes


def test_a_textbox_payload_is_still_excluded_by_design_and_not_degraded() -> None:
    """`mc:AlternateContent` is excluded BY DESIGN (textbox/drawing text),
    so meeting one is not news: no disclosure, no degraded status."""
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    textbox = f"<mc:AlternateContent {mc}><mc:Fallback>{r('Textbox words.')}</mc:Fallback></mc:AlternateContent>"
    norm = stage.extract_and_normalize(docx(heading("1. Fees") + p(r("Pay on time.") + textbox)))
    assert norm["status"] == "normalized"
    assert "extraction_status" not in norm, norm
    assert [paragraph["text"] for paragraph in norm["paragraphs"]] == ["Pay on time."]


def test_raw_and_materialized_reads_agree_on_wrapped_text() -> None:
    """Stage 1 extracts the raw upload, Stage 5 the accept-all bytes; the
    block maps must be identical (docs/task3_block_map_determinism_walkthrough.md)."""
    for tag in ("dir", "bdo", "customXml"):
        doc = _liability_doc(tag)
        raw = stage.extract_and_normalize(doc)["paragraphs"]
        mat = stage.extract_and_normalize(stage.materialize_accept_all(doc))["paragraphs"]
        assert stage.build_block_map(raw) == stage.build_block_map(mat), tag


# ---------------------------------------------------------------------------
# W7-4
# ---------------------------------------------------------------------------

SUB_CLAUSE = "Customer shall pay in 30 days."


def _sub_clause(split: str) -> str:
    if split == "one-run":
        return '<w:r><w:t>(a)</w:t><w:tab/><w:t xml:space="preserve">' + SUB_CLAUSE + "</w:t></w:r>"
    if split == "marker-and-tab|text":
        return "<w:r><w:t>(a)</w:t><w:tab/></w:r>" + r(SUB_CLAUSE)
    return '<w:r><w:t>(a)</w:t></w:r><w:r><w:tab/><w:t xml:space="preserve">' + SUB_CLAUSE + "</w:t></w:r>"


SPLITS = ("one-run", "marker-and-tab|text", "marker|tab-and-text")


def test_the_same_visible_sub_clause_classifies_identically_across_run_splits() -> None:
    results = {}
    for split in SPLITS:
        norm = stage.extract_and_normalize(docx(heading("2. Payment") + p(_sub_clause(split))))
        results[split] = [(x["block_id"], x["heading"], x["text"]) for x in norm["paragraphs"]]
    assert len(set(map(tuple, results.values()))) == 1, results
    assert results["one-run"] == [("p0001", "Payment", "(a)\t" + SUB_CLAUSE)], results


def test_boundary_text_is_independent_of_run_splits() -> None:
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    seen = set()
    for split in SPLITS:
        root = ET.fromstring(  # noqa: S314 - fixture XML this file builds itself
            f'<w:body xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"{p(_sub_clause(split))}</w:body>"
        )
        seen.add(stage._build_paragraph_record(root[0])["boundary_text"])
    assert seen == {"(a)" + SUB_CLAUSE}, seen


def test_numbered_tab_headings_are_boundaries_whatever_the_split() -> None:
    """The numbered half stays a heading, now regardless of the split (it
    used to need the tab at the end of the marker's run)."""
    body = p(r("Recipient shall hold all Confidential Information in strict confidence."))
    after = p(r("This Agreement lasts two years from the Effective Date unless terminated."))
    for marker_xml in (
        '<w:r><w:t>2.</w:t><w:tab/><w:t>Term</w:t></w:r>',
        "<w:r><w:t>2.</w:t></w:r><w:r><w:tab/><w:t>Term</w:t></w:r>",
        "<w:r><w:t>2.</w:t><w:tab/></w:r><w:r><w:t>Term</w:t></w:r>",
    ):
        norm = stage.extract_and_normalize(docx(heading("1. Confidentiality") + body + p(marker_xml) + after))
        assert [x["heading"] for x in norm["paragraphs"]] == ["Confidentiality", "Term"], norm["paragraphs"]


def test_the_two_edit_repro_applies_both_edits() -> None:
    """The QA repro exactly: two edits, one of them "30" -> "45" inside a
    short one-run `(a)<tab>...` paragraph. Before W7-4 the writer split that
    run after its tab, the projected document read the paragraph as a
    heading, and `projection_verification_failed` voided BOTH edits."""
    doc = docx(
        heading("1. Fees")
        + p(r("Customer shall pay a late fee of 5 percent."))
        + heading("2. Payment")
        + p(_sub_clause("one-run"))
    )
    norm = stage.extract_and_normalize(doc)
    block_map = stage.build_block_map(norm["paragraphs"])
    assert block_map["p0002"]["text"] == "(a)\t" + SUB_CLAUSE, block_map

    def patch(block_id: str, old: str, new: str) -> dict:
        text = block_map[block_id]["text"]
        i = text.index(old)
        segments = []
        if text[:i]:
            segments.append({"op": "keep", "text": text[:i]})
        segments += [
            {"op": "delete", "text": old, "issue_key": "k" + block_id},
            {"op": "insert", "text": new, "issue_key": "k" + block_id},
        ]
        if text[i + len(old) :]:
            segments.append({"op": "keep", "text": text[i + len(old) :]})
        return {"block_id": block_id, "segments": segments}

    proven = bt.validate_block_patches([patch("p0001", "5", "2"), patch("p0002", "30", "45")], [], block_map)
    assert proven["status"] == "proven", proven["failures"]
    result = rba.apply_block_transcript(doc, proven, author="Synthetic Reviewer", timestamp_iso="2026-01-01T00:00:00Z")
    assert result["docx_bytes"] is not None, result["failures"]
    assert len(result["applied"]) == 2 and not result["failures"], result["failures"]


def test_an_edit_in_each_split_sub_clause_lands() -> None:
    for split in SPLITS:
        doc = docx(heading("2. Payment") + p(_sub_clause(split)))
        out = replace_in_block(stage, bt, rba, doc, "30", "45")
        assert out["result"] and out["result"]["docx_bytes"] is not None, (split, out["result"])
        assert out["accepted_text"] == out["expected_text"], (split, out["accepted_text"])


TESTS = [
    test_text_inside_each_inline_wrapper_reaches_the_model,
    test_a_block_level_custom_xml_paragraph_is_not_dropped,
    test_row_and_cell_level_custom_xml_are_transparent,
    test_the_writer_can_edit_inside_each_wrapper,
    test_recognised_wrappers_do_not_degrade_extraction,
    test_an_unrecognised_text_carrier_is_disclosed_not_skipped,
    test_a_textbox_payload_is_still_excluded_by_design_and_not_degraded,
    test_raw_and_materialized_reads_agree_on_wrapped_text,
    test_the_same_visible_sub_clause_classifies_identically_across_run_splits,
    test_boundary_text_is_independent_of_run_splits,
    test_numbered_tab_headings_are_boundaries_whatever_the_split,
    test_the_two_edit_repro_applies_both_edits,
    test_an_edit_in_each_split_sub_clause_lands,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {fn.__name__}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"  [ERROR] {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {fn.__name__}")
    print("\n" + "=" * 60)
    print("ALL GREEN" if failures == 0 else f"FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
