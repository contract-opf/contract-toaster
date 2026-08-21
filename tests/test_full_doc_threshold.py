#!/usr/bin/env python3
"""
Slice test (TDD) for issue #419: "Raise the full-document threshold +
surface outline degrade".

## Root problem this proves fixed

Before this issue, `DEFAULT_FULL_DOC_TOKEN_THRESHOLD` was 15_000 -- any
document over ~60KB of text (~a 30-page MSA) silently degraded to a
headings+word-count outline (`scripts/primary_review_pass.py`'s
`render_section_outline`) with NO signal anywhere in the result: the model
reviewed a table of contents and returned a confident-looking decision, and
neither `run_primary_pass`'s own result nor `run_review`'s `ReviewResult`
said which mode was actually used.

This test FAILS on a tree where:
  - `primary_review_pass.DEFAULT_FULL_DOC_TOKEN_THRESHOLD` is still 15_000
    (or anything other than 60_000), OR
  - `primary_review_pass.run_primary_pass` / `resolve_input_mode` do not
    exist or do not report `input_mode`, OR
  - `reconciliation.reconcile()` does not degrade `confidence_state` and
    append a fixed notice to `verdict_summary` for `input_mode=
    "section_outline"`, OR
  - `scripts/review_spine.py::run_review`'s result dict does not carry
    `input_mode`.

## What this test asserts (mirrors the issue's acceptance criteria)

  1. `DEFAULT_FULL_DOC_TOKEN_THRESHOLD == 60_000`.
  2. A doc estimating just under 60_000 tokens (at the REAL default, no
     override) resolves to `input_mode="full_document"`; just over resolves
     to `"section_outline"` -- both via `resolve_input_mode` and via
     `assemble_user_prompt_primary`'s actual block choice.
  3. `run_primary_pass` threads `input_mode` onto its returned result (using
     the parameterized `full_doc_token_threshold` kwarg for a fast,
     small-threshold unit test) -- and the model genuinely never receives
     the raw document text in section-outline mode.
  4. `reconciliation.reconcile()`: `input_mode="full_document"` (or omitted,
     the default) leaves `confidence_state`/`verdict_summary` unchanged;
     `input_mode="section_outline"` degrades `confidence_state` one level,
     stacks with the pre-existing critic-delta degrade, and appends a
     FIXED, substance-free sentence to `verdict_summary` (never replacing
     the model's own summary, and identical regardless of document/model
     content).
  5. `reconciliation.INPUT_MODE_SECTION_OUTLINE` (a deliberately duplicated
     literal, per this repo's small-shared-sentinel convention) matches
     `primary_review_pass.INPUT_MODE_SECTION_OUTLINE`.
  6. End to end (`scripts/review_spine.py::run_review`, driven by
     `FakeBedrockClient`, no `full_doc_token_threshold` override -- the real
     production wiring): a small doc's `ReviewResult` carries
     `input_mode="full_document"` and no notice in `summary`; a doc
     genuinely over the REAL 60_000-token default carries
     `input_mode="section_outline"` and the fixed notice in `summary`, and
     the raw document text never reaches either model.

Run with: python3 tests/test_full_doc_threshold.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import reconciliation as recon  # noqa: E402
import review_spine as rs  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _load_fixture_text(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _load_bundle() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_FILLER_UNIT = "The quick brown fox jumps over the lazy dog. "


def _text_of_length(target_chars: int) -> str:
    """Deterministic filler text of EXACTLY `target_chars` characters -- used
    to hit precise token-estimate boundaries (`pp.estimate_tokens` is a
    simple 4-chars/token ceiling division, so exact char counts translate to
    exact token counts)."""
    reps = (target_chars // len(_FILLER_UNIT)) + 2
    return (_FILLER_UNIT * reps)[:target_chars]


# Minimal, dependency-free OOXML .docx builder (same convention as
# tests/test_review_spine.py / tests/fixtures/gold_docx_204/_generate.py).
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

_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body_paragraphs_xml}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _single_paragraph_docx(heading: str, text: str) -> bytes:
    return _build_docx_bytes(_heading_p(heading) + _body_p(text))


# ---------------------------------------------------------------------------
# 1. Default threshold constant
# ---------------------------------------------------------------------------


def test_default_threshold_is_60000(failures: list[str]) -> None:
    if pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD != 60_000:
        failures.append(
            f"[1a] DEFAULT_FULL_DOC_TOKEN_THRESHOLD must be 60_000 (issue #419), "
            f"got {pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD!r}"
        )
    if pp.MAX_INPUT_TOKENS != 80_000:
        failures.append(
            f"[1b] MAX_INPUT_TOKENS must stay 80_000 -- issue #419 explicitly keeps this "
            f"unchanged (headroom math), got {pp.MAX_INPUT_TOKENS!r}"
        )
    if pp.CHARS_PER_TOKEN_ESTIMATE != 4:
        failures.append(
            f"[1c] CHARS_PER_TOKEN_ESTIMATE must stay 4 -- issue #419 keeps the offline "
            f"estimate heuristic unchanged (no tokenizer dependency permitted), "
            f"got {pp.CHARS_PER_TOKEN_ESTIMATE!r}"
        )


# ---------------------------------------------------------------------------
# 2. Boundary behavior at the REAL default threshold
# ---------------------------------------------------------------------------


def test_resolve_input_mode_boundary_at_default_threshold(failures: list[str]) -> None:
    just_under = _text_of_length(pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD * 4)
    just_over = _text_of_length(pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD * 4 + 4)

    under_tokens = pp.estimate_tokens(just_under)
    over_tokens = pp.estimate_tokens(just_over)
    if under_tokens > pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD:
        failures.append(
            f"[2a] test fixture bug: 'just under' doc estimates to {under_tokens} tokens, "
            f"expected <= {pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD}"
        )
    if over_tokens <= pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD:
        failures.append(
            f"[2b] test fixture bug: 'just over' doc estimates to {over_tokens} tokens, "
            f"expected > {pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD}"
        )

    # No full_doc_token_threshold override below -- exercises the REAL
    # DEFAULT_FULL_DOC_TOKEN_THRESHOLD, not a parameterized stand-in.
    mode_under = pp.resolve_input_mode(just_under)
    mode_over = pp.resolve_input_mode(just_over)

    if mode_under != pp.INPUT_MODE_FULL_DOCUMENT:
        failures.append(
            f"[2c] A doc estimating {under_tokens} tokens (<= the 60_000 default) must "
            f"resolve to full_document, got {mode_under!r}"
        )
    if mode_over != pp.INPUT_MODE_SECTION_OUTLINE:
        failures.append(
            f"[2d] A doc estimating {over_tokens} tokens (> the 60_000 default) must "
            f"resolve to section_outline, got {mode_over!r}"
        )


def test_assemble_user_prompt_primary_default_threshold_boundary(failures: list[str]) -> None:
    just_under = _text_of_length(pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD * 4)
    just_over = _text_of_length(pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD * 4 + 4)

    prompt_under = pp.assemble_user_prompt_primary(
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        doc_text=just_under,
    )
    if "<COUNTERPARTY_DOCUMENT>" not in prompt_under:
        failures.append("[3a] Just-under-the-default-threshold doc must use the full-doc block.")
    if "<SECTION_OUTLINE>" in prompt_under:
        failures.append("[3b] Just-under-the-default-threshold doc must not fall back to a section outline.")

    prompt_over = pp.assemble_user_prompt_primary(
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        doc_text=just_over,
        doc_paragraphs=[{"heading": "Section 1", "text": just_over}],
    )
    if "<SECTION_OUTLINE>" not in prompt_over:
        failures.append("[3c] Just-over-the-default-threshold doc must degrade to a section outline.")
    if "<COUNTERPARTY_DOCUMENT>" in prompt_over:
        failures.append("[3d] Just-over-the-default-threshold doc must not carry the full-doc block.")


# ---------------------------------------------------------------------------
# 3. run_primary_pass threads input_mode (fast, parameterized threshold)
# ---------------------------------------------------------------------------


def test_run_primary_pass_input_mode_full_document(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    client = model_client.FakeBedrockClient(
        {primary_id: [_load_fixture_text("primary_accept_valid.json")]}
    )
    ledger: list[Any] = []

    result = pp.run_primary_pass(
        review_id="threshold-test-1",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=bundle,
        model_client=client,
        model_id=primary_id,
        ledger_write=ledger.append,
        doc_text="Short document text under any reasonable threshold.",
        full_doc_token_threshold=50,
    )
    if result["status"] != "OK":
        failures.append(f"[4a] Expected status=OK, got {result}")
        return
    if result.get("input_mode") != pp.INPUT_MODE_FULL_DOCUMENT:
        failures.append(
            f"[4b] Expected input_mode={pp.INPUT_MODE_FULL_DOCUMENT!r}, got {result.get('input_mode')!r}"
        )


def test_run_primary_pass_input_mode_section_outline(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    client = model_client.FakeBedrockClient(
        {primary_id: [_load_fixture_text("primary_accept_valid.json")]}
    )
    ledger: list[Any] = []
    long_doc = _text_of_length(400)  # well above a forced threshold of 10 tokens (40 chars)

    result = pp.run_primary_pass(
        review_id="threshold-test-2",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=bundle,
        model_client=client,
        model_id=primary_id,
        ledger_write=ledger.append,
        doc_text=long_doc,
        doc_paragraphs=[{"heading": "Section 1", "text": long_doc}],
        full_doc_token_threshold=10,
    )
    if result["status"] != "OK":
        failures.append(f"[5a] Expected status=OK, got {result}")
        return
    if result.get("input_mode") != pp.INPUT_MODE_SECTION_OUTLINE:
        failures.append(
            f"[5b] Expected input_mode={pp.INPUT_MODE_SECTION_OUTLINE!r}, got {result.get('input_mode')!r}"
        )

    # The FakeBedrockClient records every call it actually received -- prove
    # the model genuinely never saw the raw document text, not just that
    # input_mode claims so.
    if not client.calls:
        failures.append("[5c] Expected exactly one recorded model call.")
        return
    sent_user_prompt = client.calls[0]["user_prompt"]
    if "<COUNTERPARTY_DOCUMENT>" in sent_user_prompt:
        failures.append("[5d] Above-threshold doc must not have sent the raw COUNTERPARTY_DOCUMENT block.")
    if long_doc[:100] in sent_user_prompt:
        failures.append("[5e] Above-threshold doc's raw text must not appear anywhere in the sent prompt.")


# ---------------------------------------------------------------------------
# 4. reconciliation.reconcile(): confidence degrade + fixed notice
# ---------------------------------------------------------------------------


def test_reconcile_full_document_no_degrade_no_notice(failures: list[str]) -> None:
    primary = {
        "confidence_state": "OK",
        "decision": "ACCEPT",
        "issues": [],
        "verdict_summary": "No changes identified relative to your standard positions.",
    }
    result = recon.reconcile(
        primary_result=primary, critic_result=None, detector_fires=[], input_mode="full_document"
    )

    if result["confidence_state"] != "OK":
        failures.append(f"[6a] full_document input_mode must not degrade confidence_state, got {result['confidence_state']!r}")
    if result["confidence_band"] is not None:
        failures.append(f"[6b] full_document input_mode must not set a confidence_band, got {result['confidence_band']!r}")
    if result["verdict_summary"] != primary["verdict_summary"]:
        failures.append(f"[6c] full_document input_mode must leave verdict_summary unchanged, got {result['verdict_summary']!r}")
    if recon.OUTLINE_MODE_SUMMARY_NOTICE in (result["verdict_summary"] or ""):
        failures.append("[6d] full_document result must not carry the outline-mode notice.")


def test_reconcile_default_input_mode_reproduces_prior_behavior(failures: list[str]) -> None:
    primary = {"confidence_state": "OK", "decision": "ACCEPT", "issues": [], "verdict_summary": "Fine."}
    result = recon.reconcile(primary_result=primary, critic_result=None, detector_fires=[])
    if result["confidence_state"] != "OK" or result["verdict_summary"] != "Fine.":
        failures.append(f"[7a] Omitting input_mode must reproduce pre-#419 behavior exactly, got {result}")


def test_reconcile_section_outline_degrades_confidence_and_appends_notice(failures: list[str]) -> None:
    primary = {
        "confidence_state": "OK",
        "decision": "ACCEPT",
        "issues": [],
        "verdict_summary": "No changes identified relative to your standard positions.",
    }
    result = recon.reconcile(
        primary_result=primary, critic_result=None, detector_fires=[], input_mode="section_outline"
    )

    if result["confidence_state"] != "LOW_CONFIDENCE":
        failures.append(f"[8a] section_outline must degrade OK -> LOW_CONFIDENCE (one level), got {result['confidence_state']!r}")
    if result["confidence_band"] != "LOW_CONFIDENCE":
        failures.append(f"[8b] confidence_band must mirror the degraded confidence_state, got {result['confidence_band']!r}")
    summary = result["verdict_summary"] or ""
    if primary["verdict_summary"] not in summary:
        failures.append("[8c] The model's own verdict_summary must be preserved, not replaced.")
    if recon.OUTLINE_MODE_SUMMARY_NOTICE not in summary:
        failures.append("[8d] The fixed outline-mode notice must be appended to verdict_summary.")


def test_reconcile_section_outline_stacks_with_critic_degrade(failures: list[str]) -> None:
    primary = {
        "confidence_state": "OK",
        "decision": "REQUEST_CHANGE",
        "issues": [],
        "verdict_summary": "One issue identified.",
    }
    critic = {
        "decision": "REQUEST_CHANGE",
        "critic_delta": {
            "added_issues": [
                {
                    "section_ref": "sec-99",
                    "playbook_topic_id": "some-topic",
                    "provenance": "critic-added",
                }
            ],
            "contested_replacements": [],
            "rationale_objections": [],
        },
    }
    result = recon.reconcile(
        primary_result=primary, critic_result=critic, detector_fires=[], input_mode="section_outline"
    )

    # OK -> LOW_CONFIDENCE (critic-added issue, issue #265) -> MANUAL_REVIEW_REQUIRED
    # (outline-only input, issue #419) -- the two degrades stack.
    if result["confidence_state"] != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[9a] Critic-degrade and outline-degrade must stack "
            f"(OK -> LOW_CONFIDENCE -> MANUAL_REVIEW_REQUIRED), got {result['confidence_state']!r}"
        )


def test_reconcile_section_outline_null_verdict_summary(failures: list[str]) -> None:
    primary = {"confidence_state": "OK", "decision": "ACCEPT", "issues": [], "verdict_summary": None}
    result = recon.reconcile(
        primary_result=primary, critic_result=None, detector_fires=[], input_mode="section_outline"
    )
    if result["verdict_summary"] != recon.OUTLINE_MODE_SUMMARY_NOTICE:
        failures.append(
            f"[10a] A null primary verdict_summary in outline mode must become exactly the "
            f"fixed notice, got {result['verdict_summary']!r}"
        )


def test_outline_notice_is_fixed_and_substance_free(failures: list[str]) -> None:
    unique_marker = "ZQXJ7-DOCUMENT-CONTENT-MARKER"
    primary_a = {
        "confidence_state": "OK",
        "decision": "ACCEPT",
        "issues": [],
        "verdict_summary": f"Summary mentioning {unique_marker}.",
    }
    primary_b = {
        "confidence_state": "OK",
        "decision": "ACCEPT",
        "issues": [],
        "verdict_summary": "A completely different summary.",
    }

    result_a = recon.reconcile(primary_result=primary_a, critic_result=None, detector_fires=[], input_mode="section_outline")
    result_b = recon.reconcile(primary_result=primary_b, critic_result=None, detector_fires=[], input_mode="section_outline")

    notice_a = (result_a["verdict_summary"] or "").split("\n\n")[-1]
    notice_b = (result_b["verdict_summary"] or "").split("\n\n")[-1]
    if notice_a != notice_b:
        failures.append(
            f"[11a] The appended notice must be identical regardless of document/model "
            f"content, got {notice_a!r} vs {notice_b!r}"
        )
    if unique_marker in notice_a:
        failures.append("[11b] The appended notice must never carry document-derived content.")
    if notice_a != recon.OUTLINE_MODE_SUMMARY_NOTICE:
        failures.append(f"[11c] The appended notice must equal the fixed OUTLINE_MODE_SUMMARY_NOTICE constant, got {notice_a!r}")


def test_reconcile_section_outline_bounds_verdict_summary_length(failures: list[str]) -> None:
    # A schema-VALID (maxLength: 2000, per playbooks/output-schema-v1.json /
    # -v2.json) primary verdict_summary, at the boundary -- appending
    # OUTLINE_MODE_SUMMARY_NOTICE naively would push the merged string past
    # 2000 chars, making reconcile()'s self-declared output-schema-v1 result
    # schema-invalid on every section_outline review with a long summary.
    long_summary = _text_of_length(2000)
    primary = {
        "confidence_state": "OK",
        "decision": "ACCEPT",
        "issues": [],
        "verdict_summary": long_summary,
    }
    result = recon.reconcile(
        primary_result=primary, critic_result=None, detector_fires=[], input_mode="section_outline"
    )
    summary = result["verdict_summary"] or ""
    if len(summary) > 2000:
        failures.append(
            f"[15a] Merged verdict_summary must stay within the schema's 2000-char maximum "
            f"even when the primary's own summary is already at the boundary, got {len(summary)} chars"
        )
    if recon.OUTLINE_MODE_SUMMARY_NOTICE not in summary:
        failures.append(
            "[15b] The fixed outline-mode notice must survive intact (never itself truncated) "
            "even when the primary's summary must be elided to make room for it."
        )


def test_reconcile_output_validates_against_output_schema_v2(failures: list[str]) -> None:
    # Finding #1/#2 regression guard: reconcile() self-declares
    # schema_version="output-schema-v1" (SCHEMA_VERSION) on every result, so
    # its return dict must actually validate against the schema it stamps
    # itself with -- for both input_mode values, since section_outline mode
    # is the one that mutates the dict's shape (verdict_summary length) and
    # previously carried a dead `input_mode` key the schema does not allow
    # (`additionalProperties: false`).
    primary = {
        "confidence_state": "OK",
        "decision": "ACCEPT",
        "issues": [],
        "verdict_summary": "No changes identified relative to your standard positions.",
    }
    schema = pp.load_output_schema()
    for mode in ("full_document", "section_outline"):
        result = recon.reconcile(
            primary_result=primary, critic_result=None, detector_fires=[], input_mode=mode
        )
        try:
            pp.jsonschema.validate(instance=result, schema=schema)
        except pp.jsonschema.ValidationError as exc:
            failures.append(
                f"[16a] reconcile()'s output for input_mode={mode!r} must validate against "
                f"playbooks/output-schema-v2.json, got: {exc.message}"
            )


def test_input_mode_literal_matches_between_modules(failures: list[str]) -> None:
    if recon.INPUT_MODE_SECTION_OUTLINE != pp.INPUT_MODE_SECTION_OUTLINE:
        failures.append(
            f"[12a] reconciliation.INPUT_MODE_SECTION_OUTLINE={recon.INPUT_MODE_SECTION_OUTLINE!r} must "
            f"match primary_review_pass.INPUT_MODE_SECTION_OUTLINE={pp.INPUT_MODE_SECTION_OUTLINE!r} -- "
            f"these are deliberately-duplicated literals (see reconciliation.py's own comment) and must "
            f"not silently drift."
        )


# ---------------------------------------------------------------------------
# 5. End to end: scripts/review_spine.py::run_review
# ---------------------------------------------------------------------------


def test_run_review_end_to_end_full_document_mode(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    docx_bytes = _single_paragraph_docx("Section 1", "The parties agree to standard confidentiality terms.")
    client = model_client.FakeBedrockClient(
        {
            primary_id: [_load_fixture_text("primary_accept_valid.json")],
            critic_id: [_load_fixture_text("critic_no_delta_accept_valid.json")],
        }
    )

    result = rs.run_review(docx_bytes, bundle, client, review_id="threshold-e2e-full")

    if result["status"] != "OK":
        failures.append(f"[13a] Expected status=OK, got {result}")
        return
    if result.get("input_mode") != pp.INPUT_MODE_FULL_DOCUMENT:
        failures.append(
            f"[13b] run_review's result dict must carry input_mode=full_document for a small "
            f"doc, got {result.get('input_mode')!r}"
        )
    if recon.OUTLINE_MODE_SUMMARY_NOTICE in (result.get("summary") or ""):
        failures.append("[13c] A full-document review must not carry the outline-mode notice in its summary.")


def test_run_review_end_to_end_section_outline_mode(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    # Genuinely over the REAL production default (60_000 estimated tokens) --
    # review_spine.run_review does not expose full_doc_token_threshold as a
    # param, so this exercises the actual wiring, not a parameterized
    # stand-in threshold.
    huge_text = _text_of_length(pp.DEFAULT_FULL_DOC_TOKEN_THRESHOLD * 4 + 400)
    docx_bytes = _single_paragraph_docx("Section 1", huge_text)
    client = model_client.FakeBedrockClient(
        {
            primary_id: [_load_fixture_text("primary_accept_valid.json")],
            critic_id: [_load_fixture_text("critic_no_delta_accept_valid.json")],
        }
    )

    result = rs.run_review(docx_bytes, bundle, client, review_id="threshold-e2e-outline")

    if result["status"] != "OK":
        failures.append(f"[14a] Expected status=OK, got {result}")
        return
    if result.get("input_mode") != pp.INPUT_MODE_SECTION_OUTLINE:
        failures.append(
            f"[14b] run_review's result dict must carry input_mode=section_outline for a "
            f"document over the default 60_000-token threshold, got {result.get('input_mode')!r}"
        )
    summary = result.get("summary") or ""
    if recon.OUTLINE_MODE_SUMMARY_NOTICE not in summary:
        failures.append(f"[14c] An outline-mode review's summary must carry the fixed notice, got {summary!r}")

    # Prove the raw document text was genuinely never sent to either model --
    # not just that input_mode says so.
    sent_prompts = " ".join(call["user_prompt"] for call in client.calls)
    if huge_text[:200] in sent_prompts:
        failures.append("[14d] The raw counterparty document text must not have reached either model in section-outline mode.")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_default_threshold_is_60000,
    test_resolve_input_mode_boundary_at_default_threshold,
    test_assemble_user_prompt_primary_default_threshold_boundary,
    test_run_primary_pass_input_mode_full_document,
    test_run_primary_pass_input_mode_section_outline,
    test_reconcile_full_document_no_degrade_no_notice,
    test_reconcile_default_input_mode_reproduces_prior_behavior,
    test_reconcile_section_outline_degrades_confidence_and_appends_notice,
    test_reconcile_section_outline_stacks_with_critic_degrade,
    test_reconcile_section_outline_null_verdict_summary,
    test_outline_notice_is_fixed_and_substance_free,
    test_reconcile_section_outline_bounds_verdict_summary_length,
    test_reconcile_output_validates_against_output_schema_v2,
    test_input_mode_literal_matches_between_modules,
    test_run_review_end_to_end_full_document_mode,
    test_run_review_end_to_end_section_outline_mode,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all full-doc-threshold (issue #419) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
