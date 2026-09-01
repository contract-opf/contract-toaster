#!/usr/bin/env python3
"""
End-to-end slice test for issue #381: "quote-based redline produces a real
tracked-changes .docx on a reformatted contract".

## Root problem this proves fixed

Before the LLM-native quote-based path (issues #379/#380/#398) landed, the
review pipeline located every REQUEST_CHANGE edit by diffing the draft
against the standard form and joining issues to the resulting anchors/
hunks. A counterparty draft reformatted from scratch -- different section
numbering, different headings, merged/reordered clauses, paraphrased prose
-- diffs almost entirely as "no matching anchor", so that scenario
fail-closed to `MANUAL_REVIEW_REQUIRED` with NO document produced, no
matter how cleanly the model itself understood the draft. This test drives
the REAL, composed `scripts/review_spine.py::run_review` end to end, over a
genuinely reformatted fixture contract (`tests/fixtures/quote_redline_e2e/
reformatted-contract.SYNTHETIC.docx` -- see that directory's `_generate.py`
for why it is NOT built from the standard form), with a `FakeBedrockClient`
(offline, no network), and proves the pipeline produces a real
tracked-changes `.docx` instead.

Issue #627 moved the ADDRESSING this rests on from verbatim `source_quote`s
to block transcripts proven against the document's own bytes. The property
under test is unchanged and, if anything, more strongly held: a reformatted
draft that no diff can anchor is exactly the case block addressing was
designed for, since the id is assigned by the extractor rather than
recovered from the paper.

## What this test asserts (mirrors the issue's Scope / Acceptance criteria)

  1. `run_review()` returns `status="OK"`, `decision="REQUEST_CHANGE"`, and
     non-empty `redline_bytes` -- the scenario that used to fail closed.
  2. The delivered `.docx` round-trips (`redline_generate
     .verify_docx_round_trip`, plus an independent zipfile/ElementTree
     re-open of every part) and contains a `<w:del>`/`<w:ins>` pair at the
     LOCATABLE issue's exact quoted span (Article IV's uncapped-liability/
     one-way-indemnification clause), with the text immediately outside
     that span preserved untouched, plus a `<w:footnoteReference>` and a
     `word/footnotes.xml` entry carrying that issue's rationale.
  3. A second issue (Article II's shortened non-renewal notice) that the
     model could not express as an edit at all is reported FLAG-ONLY: it is
     named in `analysis_report.changes_not_applied` with its own reason,
     nothing of it is silently inserted into the delivered document, and it
     still reaches the attorney via the ordinary `findings` list (flag-only
     means "not auto-applied", never "hidden").

     Under v2 this issue's `source_quote` PARAPHRASED the clause instead of
     quoting it -- a realistic imperfect-copy failure that cost the tracked
     change. Issue #627 retired that failure mode outright: the model names
     a code-assigned `block_id`, not a quote it must reproduce, so there is
     no address for it to get subtly wrong. What survives is the OUTCOME
     this part exists to pin -- an issue with nothing appliable is still
     delivered as an observation and never half-applied -- and that is what
     it now asserts.

Run standalone: `python3 tests/test_quote_redline_e2e.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "quote_redline_e2e"
    / "reformatted-contract.SYNTHETIC.docx"
)

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _import_modules():
    missing = []
    review_spine = None
    redline_generate = None
    model_client_module = None
    try:
        import review_spine as _review_spine  # type: ignore

        review_spine = _review_spine
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/review_spine.py does not import ({exc})."
        )
    try:
        import redline_generate as _redline_generate  # type: ignore

        redline_generate = _redline_generate
    except ImportError as exc:
        missing.append(
            f"MISSING: scripts/redline_generate.py does not import ({exc})."
        )
    try:
        import model_client as _model_client  # type: ignore

        model_client_module = _model_client
    except ImportError as exc:
        missing.append(f"MISSING: backend/src/model_client.py does not import ({exc}).")
    return review_spine, redline_generate, model_client_module, missing


# ---------------------------------------------------------------------------
# Fixture fake-model content. These text constants are independently
# verified (not imported) against the committed fixture -- see
# tests/fixtures/quote_redline_e2e/_generate.py for the fixture's own source
# of truth. Keeping them separately declared here matches this repo's
# existing convention (e.g. tests/redline/test_redline_generation_83.py's
# _SEC8_TEXT / tests/test_review_spine.py's _SEC8_DRAFT_TEXT are not
# imported from a shared module either).
# ---------------------------------------------------------------------------

ARTICLE_IV_SECTION_REF = "Article IV -- Risk Allocation and Indemnification"
ARTICLE_IV_SECTION_TITLE = "Risk Allocation and Indemnification"

# Exact, unique, verbatim substring of the fixture's Article IV paragraph --
# the LOCATABLE issue's source_quote (issue #379's quote-based patcher must
# locate and apply this).
LOCATABLE_SOURCE_QUOTE = (
    "Your Organization, LLC's liability under this Agreement shall be unlimited"
)
LOCATABLE_REPLACEMENT_TEXT = (
    "Your Organization, LLC's aggregate liability under this Agreement shall "
    "not exceed $150,000, and neither party shall be liable to the other for "
    "consequential, special, punitive, incidental, or indirect damages"
)
LOCATABLE_RATIONALE = (
    "Restores the standard mutual liability cap and removes the uncapped "
    "one-way indemnification obligation, consistent with your "
    "organization's negotiating position."
)

ARTICLE_II_SECTION_REF = "Article II -- Duration and Renewal"
ARTICLE_II_SECTION_TITLE = "Duration and Renewal"

# Paraphrases (does not verbatim-match) the fixture's Article II sentence --
# the NOT-LOCATABLE issue's source_quote. Realistic imperfect-copy failure
# mode: different wording ("may terminate ... without cause") than the
# actual clause ("delivers written notice of non-renewal"), not merely a
# whitespace variant, so the retired locator's whitespace-tolerant (but
# not fuzzy/semantic) matcher legitimately fails to locate it.
NOT_LOCATABLE_SOURCE_QUOTE = (
    "either party may terminate this Agreement without cause upon ten (10) "
    "days' written notice"
)
NOT_LOCATABLE_REPLACEMENT_TEXT = (
    "either party delivers written notice of non-renewal at least sixty "
    "(60) days before the end of the then-current term"
)
NOT_LOCATABLE_RATIONALE = (
    "Restores the standard sixty-day non-renewal notice period; ten days "
    "does not give adequate transition time for in-progress placements."
)


def _article_iv_transcript(docx_bytes: bytes) -> dict[str, Any]:
    """Article IV's block, transcribed as keep / delete / insert / keep
    (issue #627).

    Both the block id and every source character are DERIVED from the
    fixture's own bytes by the production extractor: the block is found by
    the span it must contain, and the surrounding `keep` text is sliced out
    of that block's real text. Nothing here is retyped, which is the only
    way a transcript fixture can be trusted -- a hand-copied keep is a
    `source_mismatch` waiting to happen.
    """
    import extraction_normalization_stage as ens

    normalized = ens.extract_and_normalize(docx_bytes)
    assert normalized["status"] == "normalized", normalized
    for block_id, block in ens.build_block_map(normalized["paragraphs"]).items():
        text = block["text"]
        if LOCATABLE_SOURCE_QUOTE in text:
            at = text.index(LOCATABLE_SOURCE_QUOTE)
            return {
                "block_id": block_id,
                "segments": [
                    {"op": "keep", "text": text[:at]},
                    {"op": "delete", "text": LOCATABLE_SOURCE_QUOTE, "issue_key": "I1"},
                    {"op": "insert", "text": LOCATABLE_REPLACEMENT_TEXT, "issue_key": "I1"},
                    {"op": "keep", "text": text[at + len(LOCATABLE_SOURCE_QUOTE):]},
                ],
            }
    raise AssertionError("the reformatted fixture no longer carries Article IV's clause")


def _primary_request_change_response(docx_bytes: bytes) -> str:
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": ARTICLE_IV_SECTION_REF,
                    "section_title": ARTICLE_IV_SECTION_TITLE,
                    "counterparty_change_summary": (
                        "Counterparty removed the mutual liability cap and "
                        "imposed unlimited, one-way indemnification "
                        "obligations on Your Organization, LLC."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": LOCATABLE_RATIONALE,
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                },
                {
                    # The second issue carries NO EDIT (issue #627). Under v2
                    # this was the "model paraphrased its own quote" failure:
                    # a real, common, imperfect-copy mode that cost the issue
                    # its tracked change. Block addressing makes that
                    # particular mistake impossible -- the model names a code
                    # -assigned id, not a quote it has to reproduce -- and the
                    # surviving shape of the same OUTCOME is an issue the
                    # model could not express as an edit at all. It must
                    # still reach the attorney, and its replacement language
                    # must still never appear in the delivered document.
                    "issue_key": "I2",
                    "section_ref": ARTICLE_II_SECTION_REF,
                    "section_title": ARTICLE_II_SECTION_TITLE,
                    "counterparty_change_summary": (
                        "Counterparty shortened the non-renewal notice "
                        "period from the standard sixty days to ten days."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": NOT_LOCATABLE_RATIONALE,
                    "playbook_topic_id": "term-length",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                },
            ],
            "block_patches": [_article_iv_transcript(docx_bytes)],
            "block_ops": [],
            "critic_delta": None,
            "verdict_summary": (
                "Two issues identified: Article IV imposes unlimited, "
                "one-way liability and indemnification, and Article II "
                "shortens the non-renewal notice period, both deviating "
                "from your organization's standard positions."
            ),
        }
    )


def _critic_no_delta_response() -> str:
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def _load_bundle() -> dict[str, Any]:
    with open(
        REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json",
        encoding="utf-8",
    ) as fh:
        return json.load(fh)


def _run_pipeline(rs, model_client_module):
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"MISSING FIXTURE: {FIXTURE_PATH} does not exist.\n"
            f"  FIX: run `python3 tests/fixtures/quote_redline_e2e/_generate.py` "
            f"to (re)generate it."
        )
    docx_bytes = FIXTURE_PATH.read_bytes()
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    fake_client = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response(docx_bytes)],
            critic_id: [_critic_no_delta_response()],
        }
    )
    result = rs.run_review(docx_bytes, bundle, fake_client, review_id="quote-redline-e2e-381")
    return result


# ---------------------------------------------------------------------------
# Part 1: run_review() itself -- the scenario that used to fail closed now
# returns OK/REQUEST_CHANGE with a real document (issue #381 Goal/AC1).
# ---------------------------------------------------------------------------


def _part_1_review_result(result: dict[str, Any], failures: list[str]) -> None:
    if result["status"] != "OK":
        failures.append(f"[1a] Expected status=OK, got {result}")
        return
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[1b] Expected decision=REQUEST_CHANGE, got {result['decision']!r}")

    redline_bytes = result.get("redline_bytes")
    if not redline_bytes:
        failures.append(
            f"[1c] Expected non-empty redline_bytes -- a reformatted contract "
            f"used to fail closed to MANUAL_REVIEW_REQUIRED with no document "
            f"here (issue #381's whole point). Got {redline_bytes!r}."
        )

    findings = result.get("findings") or []
    model_findings = [f for f in findings if f.get("provenance") == "model"]
    if len(model_findings) != 2:
        failures.append(
            f"[1d] Expected exactly 2 provenance='model' findings (both "
            f"planted issues reach the attorney via findings regardless of "
            f"which one auto-applied), got {len(model_findings)}: {findings}"
        )
        return
    section_refs = {f.get("section_ref") for f in model_findings}
    if section_refs != {ARTICLE_IV_SECTION_REF, ARTICLE_II_SECTION_REF}:
        failures.append(
            f"[1e] Expected findings anchored at Article IV and Article II, "
            f"got section_refs={section_refs}"
        )

    # The not-locatable issue must still reach the attorney via findings --
    # flag-only (not auto-applied) is never hidden.
    article_ii_finding = next(
        (f for f in model_findings if f.get("section_ref") == ARTICLE_II_SECTION_REF), None
    )
    if article_ii_finding is None:
        failures.append("[1f] Article II's not-locatable issue is missing from findings entirely.")
    elif article_ii_finding.get("proposed_replacement_text"):
        # Issue #627: the model proposed no edit for this issue, so there is
        # no proven transcript to derive a replacement from -- the field must
        # stay empty rather than being invented.
        failures.append(
            "[1g] Article II finding carries a replacement text the model never "
            "proposed and the pipeline could not have derived; got "
            f"{article_ii_finding.get('proposed_replacement_text')!r}. Was mutated "
            "by the pipeline -- reconciliation must pass primary issues "
            "through unchanged."
        )


# ---------------------------------------------------------------------------
# Part 2: the delivered .docx round-trips and contains the expected tracked
# changes + footnote for the LOCATABLE issue (issue #381 AC2/Scope item 3).
# ---------------------------------------------------------------------------


def _part_2_docx_round_trip_and_tracked_changes(
    rg, result: dict[str, Any], failures: list[str]
) -> None:
    redline_bytes = result.get("redline_bytes")
    if not redline_bytes:
        failures.append("[2a] No redline_bytes to inspect -- part 1 already failed.")
        return

    # -- Round-trip verification: the same gate generate_redline itself
    #    already ran, re-asserted here as this test's own explicit check
    #    (issue #381's Required verification), plus an independent
    #    zipfile/ElementTree re-open of every part (belt-and-suspenders,
    #    same convention as tests/redline/test_redline_generation_83.py
    #    part 5) -- exactly what a caller/attorney's Word client effectively
    #    does when it opens the file.
    try:
        rg.verify_docx_round_trip(redline_bytes)
    except ValueError as exc:
        failures.append(f"[2b] verify_docx_round_trip raised on the delivered docx: {exc}")

    buf = io.BytesIO(bytes(redline_bytes))
    if not zipfile.is_zipfile(buf):
        failures.append("[2c] Delivered redline_bytes are not a valid ZIP archive.")
        return
    with zipfile.ZipFile(buf) as zf:
        bad = zf.testzip()
        if bad is not None:
            failures.append(f"[2d] Corrupt member in delivered ZIP: {bad}")
        names = set(zf.namelist())
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                try:
                    ET.fromstring(zf.read(name))
                except ET.ParseError as exc:
                    failures.append(f"[2e] {name} failed to re-parse: {exc}")

        doc_root = ET.fromstring(zf.read("word/document.xml"))
        ins_elements = doc_root.findall(f".//{_qn('ins')}")
        del_elements = doc_root.findall(f".//{_qn('del')}")
        if not ins_elements or not del_elements:
            failures.append("[2f] Expected at least one <w:ins> and <w:del> pair in the delivered docx.")

        ins_text = "".join(
            (t.text or "") for ins in ins_elements for t in ins.findall(f".//{_qn('t')}")
        )
        del_text = "".join(
            (t.text or "") for d in del_elements for t in d.findall(f".//{_qn('delText')}")
        )
        if LOCATABLE_REPLACEMENT_TEXT not in ins_text:
            failures.append(
                f"[2g] Expected the Article IV replacement text inserted as "
                f"<w:ins>, not found in {ins_text!r}"
            )
        if LOCATABLE_SOURCE_QUOTE not in del_text:
            failures.append(
                f"[2h] Expected the Article IV quoted span deleted as "
                f"<w:del>, not found in {del_text!r}"
            )

        # Span-level edit, not a whole-paragraph replace: the text
        # immediately surrounding the quoted span survives untouched.
        all_text = "".join((t.text or "") for t in doc_root.findall(f".//{_qn('t')}")) + del_text
        surrounding = "Notwithstanding anything to the contrary elsewhere in this Agreement"
        if surrounding not in all_text:
            failures.append("[2i] Text outside the quoted span (Article IV's lead-in) was not preserved.")
        trailing = "regardless of the degree of fault of any party."
        if trailing not in all_text:
            failures.append("[2i2] Text outside the quoted span (Article IV's trailing clause) was not preserved.")

        # -- Rationale footnote -----------------------------------------
        if "word/footnotes.xml" not in names:
            failures.append("[2j] No word/footnotes.xml part -- footnoted rationale missing.")
        else:
            footnote_refs = doc_root.findall(f".//{_qn('footnoteReference')}")
            if not footnote_refs:
                failures.append("[2k] No <w:footnoteReference> in document.xml.")
            footnotes_root = ET.fromstring(zf.read("word/footnotes.xml"))
            footnote_text = "".join(
                (t.text or "") for t in footnotes_root.findall(f".//{_qn('t')}")
            )
            if LOCATABLE_RATIONALE not in footnote_text:
                failures.append(
                    f"[2l] Footnote rationale text not found in footnotes.xml: {footnote_text!r}"
                )

        # -- The NOT-LOCATABLE issue's replacement text must never have
        #    been silently inserted anywhere (a locate failure must route
        #    to flag-only, never a mis-applied edit).
        if NOT_LOCATABLE_REPLACEMENT_TEXT in ins_text:
            failures.append(
                "[2m] Article II's not-locatable replacement text was "
                "inserted into the document -- a locate failure must never "
                "silently apply."
            )


# ---------------------------------------------------------------------------
# Part 3: the NOT-LOCATABLE issue is reported flag-only via analysis_report
# (issue #381 Scope item 3 / AC2).
# ---------------------------------------------------------------------------


def _part_3_not_locatable_is_flag_only(result: dict[str, Any], failures: list[str]) -> None:
    analysis_report = result.get("analysis_report")
    if not analysis_report:
        failures.append(
            f"[3a] Expected a non-None analysis_report naming Article II's "
            f"not-locatable quote, got {analysis_report!r}"
        )
        return
    if analysis_report.get("report_type") not in ("analysis_report", "flag_only_report"):
        failures.append(f"[3b] Unexpected analysis_report shape: {analysis_report}")
    if "decision" in analysis_report:
        failures.append("[3c] analysis_report must never carry a decision field (system artifact, not a legal one).")

    changes_not_applied = analysis_report.get("changes_not_applied") or []
    if len(changes_not_applied) != 1:
        failures.append(
            f"[3d] Expected exactly 1 changes_not_applied entry (Article IV's "
            f"transcript proved and WAS applied), got {len(changes_not_applied)}: "
            f"{changes_not_applied}"
        )
        return
    entry = changes_not_applied[0]
    if entry.get("section_ref") != ARTICLE_II_SECTION_REF:
        failures.append(f"[3e] Expected the flag-only entry anchored at Article II, got {entry.get('section_ref')!r}")
    # Issue #627: the entry is reported by the issue #585 flag-only
    # labelling rather than by the quote patcher's `not_found`, because
    # under the block-transcript contract this issue proposes no edit at all
    # -- there is no address that failed to resolve, there is nothing the
    # model asked to change.
    if analysis_report.get("report_type") != "flag_only_report":
        failures.append(
            f"[3f] An issue carrying no edit must be reported as a flag_only_report, got "
            f"{analysis_report.get('report_type')!r}"
        )
    if not entry.get("reason"):
        failures.append(f"[3g] The flag-only entry must carry its own reason; got {entry!r}")


def main() -> int:
    failures: list[str] = []

    rs, rg, model_client_module, missing = _import_modules()
    if missing:
        print("FAIL: quote-redline e2e gate cannot run.\n")
        for m in missing:
            print(f"[G0] {m}")
        return 1

    try:
        result = _run_pipeline(rs, model_client_module)
    except FileNotFoundError as exc:
        print("FAIL: quote-redline e2e gate cannot run.\n")
        print(f"[G0] {exc}")
        return 1

    _part_1_review_result(result, failures)
    _part_2_docx_round_trip_and_tracked_changes(rg, result, failures)
    _part_3_not_locatable_is_flag_only(result, failures)

    if failures:
        print("FAIL: quote-redline e2e gate (issue #381).\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: quote-redline e2e gate (issue #381).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
