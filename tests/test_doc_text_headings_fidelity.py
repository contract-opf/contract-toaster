#!/usr/bin/env python3
"""
Regression test for the "headings never reach the model" defect in
`scripts/review_spine.py::run_review`.

## Root problem this proves fixed

`scripts/extraction_normalization_stage.py::normalize_paragraphs` returns
paragraph records shaped `{"heading": ..., "text": ..., "physical_spans":
...}` -- the heading is a SEPARATE key from the body text. Before this fix,
`scripts/review_spine.py::run_review` built the document text the model
reads with:

    doc_text = "\\n\\n".join(p.get("text", "") for p in draft_paragraphs)

-- `text` only. On a real 30-paragraph target document (all 30 carrying a
heading), 29 of 30 headings never appeared anywhere in `doc_text`, so the
model reviewed the agreement's body with every clause title stripped, even
though `INPUT_MODE_FULL_DOCUMENT` (the common case) sends `doc_text`
straight into the `COUNTERPARTY_DOCUMENT` prompt block with no other
heading channel at all (`primary_review_pass.py::assemble_user_prompt_
primary` only reads `doc_paragraphs` -- and therefore headings -- in the
degraded `INPUT_MODE_SECTION_OUTLINE` branch).

This test file:

  1. Unit-tests `review_spine.document_text_for_review` (the extracted,
     pure join function) directly, covering heading placement/order,
     visual distinguishability, and the "no stray blank lines" edge cases
     for an empty heading or an empty body.
  2. Drives the real `review_spine.run_review()` end to end (reusing
     `tests/test_review_spine.py`'s already-proven docx builder and
     playbook fixture -- the established cross-file-reuse convention, see
     `tests/test_llm_native_overlay.py`'s own header) and asserts a real
     standard-form heading ("Admitting Students", whose body text does
     NOT itself contain the heading string) reaches the ACTUAL
     `user_prompt` the injected `FakeBedrockClient` records, in document
     order, ahead of its own body text.

Fails on a tree where `review_spine.document_text_for_review` does not
exist (pre-fix: `doc_text` was assembled inline from `text` alone, with no
extracted, independently-testable join function) and passes once one
exists and is wired into `run_review`.

Run standalone: `python3 tests/test_doc_text_headings_fidelity.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import review_spine  # noqa: E402

# Cross-file reuse of the already-proven docx builder / playbook loader /
# canned model responses (same convention tests/test_llm_native_overlay.py
# uses to import from this same module).
from test_review_spine import (  # noqa: E402
    _build_draft_docx,
    _critic_accept_response,
    _load_bundle,
    _primary_accept_response,
)


# ---------------------------------------------------------------------------
# Part 1: unit tests on the extracted pure join function.
# ---------------------------------------------------------------------------


def test_heading_and_text_both_present(failures: list[str]) -> None:
    paragraphs = [
        {"heading": "Confidentiality", "text": "Each party shall keep the other's information secret."},
    ]
    doc_text = review_spine.document_text_for_review(paragraphs)
    if "Confidentiality" not in doc_text:
        failures.append(f"[1a] heading missing from doc_text entirely: {doc_text!r}")
    if "Each party shall keep" not in doc_text:
        failures.append(f"[1b] body text missing from doc_text: {doc_text!r}")
    if "Confidentiality" in doc_text and "Each party shall keep" in doc_text:
        if doc_text.index("Confidentiality") > doc_text.index("Each party shall keep"):
            failures.append(f"[1c] heading must precede its own body text: {doc_text!r}")


def test_heading_visually_distinguished_from_body(failures: list[str]) -> None:
    """The heading must not read as a bare sentence of the contract -- it
    needs a marker distinguishing it from body prose, on its own line."""
    paragraphs = [{"heading": "Term", "text": "This Agreement lasts one year."}]
    doc_text = review_spine.document_text_for_review(paragraphs)
    lines = doc_text.split("\n")
    heading_lines = [ln for ln in lines if "Term" in ln]
    if not heading_lines:
        failures.append(f"[2a] heading line not found: {doc_text!r}")
    elif heading_lines[0].strip() == "Term":
        failures.append(
            f"[2b] heading rendered with no distinguishing marker at all -- "
            f"indistinguishable from a bare one-word sentence: {doc_text!r}"
        )
    # The body text must not be on the SAME line as the heading (that would
    # read as one run-on sentence to the model).
    for ln in lines:
        if "Term" in ln and "This Agreement lasts" in ln:
            failures.append(f"[2c] heading and body text collapsed onto one line: {ln!r}")


def test_multiple_paragraphs_preserve_document_order(failures: list[str]) -> None:
    paragraphs = [
        {"heading": "Section One", "text": "First body."},
        {"heading": "Section Two", "text": "Second body."},
        {"heading": "Section Three", "text": "Third body."},
    ]
    doc_text = review_spine.document_text_for_review(paragraphs)
    positions = [doc_text.index(p["heading"]) for p in paragraphs]
    if positions != sorted(positions):
        failures.append(f"[3a] headings out of document order: {doc_text!r}")
    body_positions = [doc_text.index(p["text"]) for p in paragraphs]
    if body_positions != sorted(body_positions):
        failures.append(f"[3b] body texts out of document order: {doc_text!r}")


def test_empty_heading_produces_no_stray_separator(failures: list[str]) -> None:
    paragraphs = [
        {"heading": "Before", "text": "Before body."},
        {"heading": "", "text": "Untitled body."},
        {"heading": "After", "text": "After body."},
    ]
    doc_text = review_spine.document_text_for_review(paragraphs)
    if "\n\n\n" in doc_text:
        failures.append(
            f"[4a] an empty heading produced a stray blank-line run: {doc_text!r}"
        )
    if "Untitled body." not in doc_text:
        failures.append(f"[4b] paragraph with empty heading lost its body text: {doc_text!r}")


def test_empty_text_produces_no_stray_separator(failures: list[str]) -> None:
    paragraphs = [
        {"heading": "Before", "text": "Before body."},
        {"heading": "Empty Section", "text": ""},
        {"heading": "After", "text": "After body."},
    ]
    doc_text = review_spine.document_text_for_review(paragraphs)
    if "\n\n\n" in doc_text:
        failures.append(f"[5a] an empty body produced a stray blank-line run: {doc_text!r}")
    if "Empty Section" not in doc_text:
        failures.append(f"[5b] the heading of an empty-text paragraph must still be shown: {doc_text!r}")


def test_both_empty_contributes_nothing(failures: list[str]) -> None:
    paragraphs = [
        {"heading": "Before", "text": "Before body."},
        {"heading": "", "text": ""},
        {"heading": "After", "text": "After body."},
    ]
    doc_text = review_spine.document_text_for_review(paragraphs)
    if "\n\n\n" in doc_text:
        failures.append(f"[6a] a fully-empty paragraph produced a stray separator: {doc_text!r}")
    expected = review_spine.document_text_for_review(
        [{"heading": "Before", "text": "Before body."}, {"heading": "After", "text": "After body."}]
    )
    if doc_text != expected:
        failures.append(
            f"[6b] a fully-empty paragraph must join identically to it being absent. "
            f"Got {doc_text!r}, expected {expected!r}"
        )


def test_untitled_sentinel_treated_as_no_heading(failures: list[str]) -> None:
    """`extraction_normalization_stage.py` defaults a paragraph with no real
    heading to the literal sentinel "<untitled>" (see
    `normalize_paragraphs`/`extract_document_paragraphs`). Rendering that
    placeholder verbatim as a document heading on every untitled paragraph
    would be noise, not fidelity -- it should behave like no heading at
    all, exactly like `primary_review_pass.render_section_outline`'s own
    `"(untitled)"` fallback treats it as absent-of-a-real-title."""
    paragraphs = [{"heading": "<untitled>", "text": "Recital text with no heading."}]
    doc_text = review_spine.document_text_for_review(paragraphs)
    if "<untitled>" in doc_text:
        failures.append(
            f"[7a] the '<untitled>' sentinel must not be rendered as a literal heading: {doc_text!r}"
        )
    if "Recital text with no heading." not in doc_text:
        failures.append(f"[7b] body text lost for an untitled paragraph: {doc_text!r}")


def test_no_headings_matches_pre_fix_join(failures: list[str]) -> None:
    """When no paragraph carries a real heading, the result must be
    byte-identical to the pre-fix `"\\n\\n".join(text for ...)` formula --
    this fix must not change behavior for a document with no headings at
    all."""
    paragraphs = [
        {"heading": "<untitled>", "text": "First."},
        {"heading": "<untitled>", "text": "Second."},
    ]
    doc_text = review_spine.document_text_for_review(paragraphs)
    expected = "\n\n".join(p["text"] for p in paragraphs)
    if doc_text != expected:
        failures.append(
            f"[8a] a headingless document must join exactly like before this fix. "
            f"Got {doc_text!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# Part 2: end-to-end -- a real standard-form heading reaches the ACTUAL
# user_prompt sent to the injected FakeBedrockClient.
# ---------------------------------------------------------------------------


def test_run_review_sends_heading_to_model(failures: list[str]) -> None:
    import diff_standard_form as dsf_module
    import model_client as model_client_module

    bundle = _load_bundle()
    # Unmodified draft (identical to the standard form) -- ACCEPT path,
    # the minimal-setup scenario. This test cares only about what reaches
    # the model's user_prompt, not the decision.
    docx_bytes = _build_draft_docx(dsf_module, {})
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    fake_client = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_accept_response()],
            critic_id: [_critic_accept_response()],
        }
    )

    result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="heading-fidelity-test")

    if result["status"] != "OK":
        failures.append(f"[9a] setup failure -- expected status=OK, got {result}")
        return

    if not fake_client.calls:
        failures.append("[9b] setup failure -- no model calls recorded at all")
        return

    primary_calls = [c for c in fake_client.calls if c["model_id"] == primary_id]
    if not primary_calls:
        failures.append(f"[9c] setup failure -- no call recorded for primary_id={primary_id!r}")
        return
    user_prompt = primary_calls[0]["user_prompt"]
    if not isinstance(user_prompt, str):
        # Prompt-caching content-block shape -- flatten to text for the
        # substring checks below.
        user_prompt = "\n".join(
            block.get("text", "") for block in user_prompt if isinstance(block, dict)
        )

    heading = "Admitting Students"
    # This heading's own body text does NOT itself contain the heading
    # string, so finding the heading string proves it was rendered
    # SEPARATELY, not incidentally via the body.
    body_snippet = "The Company has sole discretion"

    if heading not in user_prompt:
        failures.append(
            f"[9d] heading {heading!r} never reached the model's user_prompt -- "
            f"this is the measured defect: a paragraph's heading, present in "
            f"the normalized document, does not reach doc_text/the prompt."
        )
    if body_snippet not in user_prompt:
        failures.append(f"[9e] setup failure -- expected body text not found in user_prompt at all")
    if heading in user_prompt and body_snippet in user_prompt:
        if user_prompt.index(heading) > user_prompt.index(body_snippet):
            failures.append(
                f"[9f] heading {heading!r} must appear in the prompt BEFORE its own body text"
            )


def main() -> int:
    failures: list[str] = []

    test_heading_and_text_both_present(failures)
    test_heading_visually_distinguished_from_body(failures)
    test_multiple_paragraphs_preserve_document_order(failures)
    test_empty_heading_produces_no_stray_separator(failures)
    test_empty_text_produces_no_stray_separator(failures)
    test_both_empty_contributes_nothing(failures)
    test_untitled_sentinel_treated_as_no_heading(failures)
    test_no_headings_matches_pre_fix_join(failures)
    test_run_review_sends_heading_to_model(failures)

    if failures:
        print("FAIL: doc-text heading fidelity gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: doc-text heading fidelity gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
