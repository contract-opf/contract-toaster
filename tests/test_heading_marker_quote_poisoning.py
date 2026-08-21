#!/usr/bin/env python3
"""
Regression test for the SECOND-ORDER defect introduced by the heading-
fidelity fix (`scripts/review_spine.py::document_text_for_review`): a
rendered `"## "` heading marker leaking into a model's `source_quote` and
silently costing that issue its redline.

## Root problem this proves fixed

`document_text_for_review` (the fix for "28 of 30 section headings never
reach the model") renders each normalized paragraph as:

    ## <heading>\n<body text>

-- so the model now SEES the clause title, which is the whole point. But
the locator that later turns a model `source_quote` back into a document
span, `scripts/quote_locate.py::locate_quote_in_paragraphs`, searches
`paragraph["text"]` ONLY (`quote_locate.py:331-335`). `heading` is a
separate key on the normalized record and appears nowhere in that search
basis, and the `"## "` marker is pure rendering -- it exists in no
document, in no paragraph, anywhere.

So a model that copies its quote starting from the top of the block it was
shown -- the visually natural thing to do, and something the prompt did not
warn against -- produces a `source_quote` that CANNOT locate. It returns
`not_found`, the issue degrades to flag-only, and the attorney gets an
observation with no tracked change against it. Nothing errors; the redline
is just quietly missing. This is the same failure mode as issue #560, which
cost 65% of real documents their redline.

This test file:

  1. Proves the hazard is real at the locator level (test 1): the exact
     string a model would copy off the rendered block locates cleanly once
     the marker line is removed, and does not locate at all while it is
     still attached.
  2. Unit-tests the normalizer that removes it, over the Issue-shaped
     objects `validate_model_response` already normalizes -- top-level
     `issues` AND `critic_delta.added_issues` -- including the
     heading-ONLY quote, which must degrade to ABSENT rather than to an
     empty string the schema's `minLength: 1` would reject.
  3. Proves a quote carrying no marker is passed through byte-identically,
     so this normalization can never touch model judgment.
  4. Pins the prompt rule that tells the model not to copy the marker in
     the first place (the normalizer is the backstop, not the fix), and
     cross-checks the duplicated marker literal against
     `review_spine`'s own, per this repo's "each module owning its own
     copy of small shared sentinels" convention -- the same cross-check
     `tests/test_full_doc_threshold.py` does for
     `INPUT_MODE_SECTION_OUTLINE`.

Fails on a tree where `primary_review_pass` does not strip the marker
(pre-fix: a heading-carrying `source_quote` survived validation intact and
went on to fail quote-locate).

Run standalone: `python3 tests/test_heading_marker_quote_poisoning.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import re
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

import primary_review_pass  # noqa: E402
import quote_locate  # noqa: E402
import review_spine  # noqa: E402


HEADING = "Confidential Information"
BODY = (
    "Each party shall hold the other party's Confidential Information in "
    "strict confidence and shall not disclose it to any third party without "
    "prior written consent."
)


def _paragraphs() -> list[dict[str, Any]]:
    """A normalized paragraph list in the exact shape
    `extraction_normalization_stage.normalize_paragraphs` produces -- the
    shape BOTH `document_text_for_review` (what the model reads) and
    `locate_quote_in_paragraphs` (what turns its quote back into a span)
    consume. One paragraph carrying a real heading, plus a second so a
    successful locate is proving uniqueness, not a one-element list.
    """
    return [
        {"heading": HEADING, "text": BODY, "physical_spans": [[0, len(BODY)]]},
        {
            "heading": "Term",
            "text": "This Agreement commences on the Effective Date and continues for two years.",
            "physical_spans": [[0, 74]],
        },
    ]


def _response(source_quote: Any, *, in_critic_delta: bool = False) -> str:
    """A minimal schema-valid REQUEST_CHANGE response carrying `source_quote`
    on either the top-level issue or a critic-added one. `source_quote` of
    `None` means "omit the key entirely" (the absent case).
    """
    issue: dict[str, Any] = {
        "section_ref": "4",
        "section_title": HEADING,
        "counterparty_change_summary": "Confidentiality is one-way.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "We require mutual confidentiality.",
        "proposed_replacement_text": "Each party shall hold the other party's Confidential Information in strict confidence.",
        "playbook_topic_id": "confidentiality",
        "internal_precedent_citation": "",
        "provenance": "model",
    }
    if source_quote is not None:
        issue["source_quote"] = source_quote
    body: dict[str, Any] = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "verdict_summary": "One-way confidentiality.",
        "issues": [] if in_critic_delta else [issue],
    }
    if in_critic_delta:
        body["critic_delta"] = {"added_issues": [issue]}
    return json.dumps(body)


def _validated_issue(raw: str, *, in_critic_delta: bool = False) -> tuple[bool, Any]:
    ok, parsed = primary_review_pass.validate_model_response(raw)
    if not ok:
        return False, parsed
    if in_critic_delta:
        return True, parsed["critic_delta"]["added_issues"][0]
    return True, parsed["issues"][0]


# ---------------------------------------------------------------------------
# 1. The hazard itself, at the locator.
# ---------------------------------------------------------------------------


def test_marker_carrying_quote_cannot_locate(failures: list[str]) -> None:
    """The defect, stated as behavior: the marker line is not in the search
    basis, so a quote carrying it is unlocatable -- while the SAME quote
    without it locates uniquely and cleanly.
    """
    paragraphs = _paragraphs()
    rendered = review_spine.document_text_for_review(paragraphs)
    marker_line = f"## {HEADING}"
    if marker_line not in rendered:
        failures.append(
            f"[1a] setup failure -- {marker_line!r} is not how "
            f"document_text_for_review renders a heading any more; this "
            f"test's premise needs revisiting."
        )
        return

    # Exactly what a model copying from the top of the rendered block emits.
    poisoned = f"{marker_line}\n{BODY}"
    poisoned_result = quote_locate.locate_quote_in_paragraphs(paragraphs, poisoned)
    if poisoned_result["status"] == "found":
        failures.append(
            "[1b] setup failure -- a marker-carrying quote located "
            "successfully, so the marker must now be in the locator's "
            "search basis; this test's premise needs revisiting."
        )
    clean_result = quote_locate.locate_quote_in_paragraphs(paragraphs, BODY)
    if clean_result["status"] != "found":
        failures.append(
            f"[1c] setup failure -- the body text alone should locate "
            f"cleanly, got {clean_result['status']!r}."
        )


def test_stripped_quote_locates_end_to_end(failures: list[str]) -> None:
    """The fix, stated as behavior: run the poisoned quote through the real
    validation path and the surviving `source_quote` locates in the real
    document. This is the assertion that actually protects the redline.
    """
    paragraphs = _paragraphs()
    poisoned = f"## {HEADING}\n{BODY}"
    ok, issue = _validated_issue(_response(poisoned))
    if not ok:
        failures.append(f"[2a] response failed validation outright: {issue}")
        return
    quote = issue.get("source_quote")
    if quote is None:
        failures.append(
            "[2b] the whole quote was dropped -- only the marker LINE should "
            "be removed when real body text follows it."
        )
        return
    result = quote_locate.locate_quote_in_paragraphs(paragraphs, quote)
    if result["status"] != "found":
        failures.append(
            f"[2c] a marker-carrying source_quote still does not locate after "
            f"validation (status={result['status']!r}, quote={quote!r}) -- the "
            f"issue degrades to flag-only and its redline is silently lost."
        )


# ---------------------------------------------------------------------------
# 2. The normalizer, over every Issue-shaped object.
# ---------------------------------------------------------------------------


def test_marker_line_stripped_from_top_level_issue(failures: list[str]) -> None:
    ok, issue = _validated_issue(_response(f"## {HEADING}\n{BODY}"))
    if not ok:
        failures.append(f"[3a] response failed validation outright: {issue}")
        return
    if issue.get("source_quote") != BODY:
        failures.append(
            f"[3b] expected the marker line removed and the body preserved "
            f"exactly, got {issue.get('source_quote')!r}"
        )


def test_marker_line_stripped_from_critic_added_issue(failures: list[str]) -> None:
    """`critic_delta.added_issues` carry `source_quote` into exactly the same
    redline path, and `_denullify_unrepresentable_issue_fields` already
    normalizes both lists -- this one must not be the exception.
    """
    ok, issue = _validated_issue(_response(f"## {HEADING}\n{BODY}", in_critic_delta=True), in_critic_delta=True)
    if not ok:
        failures.append(f"[4a] response failed validation outright: {issue}")
        return
    if issue.get("source_quote") != BODY:
        failures.append(
            f"[4b] a critic-added issue's source_quote was not normalized, "
            f"got {issue.get('source_quote')!r}"
        )


def test_heading_only_quote_degrades_to_absent(failures: list[str]) -> None:
    """A quote that is ONLY the marker line names no contract text at all.
    It must degrade to ABSENT -- the shape the schema and the redline path
    already treat as "no quote to locate" (flag-only, honestly) -- never to
    `""`, which `output-schema-v2.json`'s `minLength: 1` would reject and
    which would fail the whole response closed over a technicality.
    """
    ok, issue = _validated_issue(_response(f"## {HEADING}"))
    if not ok:
        failures.append(
            f"[5a] a heading-only source_quote failed the whole response "
            f"closed instead of degrading to absent: {issue}"
        )
        return
    if "source_quote" in issue:
        failures.append(
            f"[5b] a heading-only source_quote must be ABSENT after "
            f"normalization, got {issue.get('source_quote')!r}"
        )


def test_heading_only_quote_with_trailing_newline_degrades_to_absent(failures: list[str]) -> None:
    ok, issue = _validated_issue(_response(f"## {HEADING}\n"))
    if not ok:
        failures.append(f"[6a] failed closed instead of degrading to absent: {issue}")
        return
    if "source_quote" in issue:
        failures.append(
            f"[6b] expected absent, got {issue.get('source_quote')!r}"
        )


# ---------------------------------------------------------------------------
# 3. Never touch model judgment.
# ---------------------------------------------------------------------------


def test_quote_without_marker_is_byte_identical(failures: list[str]) -> None:
    ok, issue = _validated_issue(_response(BODY))
    if not ok:
        failures.append(f"[7a] response failed validation outright: {issue}")
        return
    if issue.get("source_quote") != BODY:
        failures.append(
            f"[7b] a quote carrying no marker must pass through unchanged, "
            f"got {issue.get('source_quote')!r}"
        )


def test_marker_not_at_start_is_left_alone(failures: list[str]) -> None:
    """Only a LEADING marker line is rendering scaffolding. A `"## "` deeper
    inside a quote means the quote crossed a paragraph boundary (already
    forbidden by the prompt, already `not_found` at the locator) or is
    genuine document text -- either way, removing it would be this
    normalizer inventing an edit, which the "never patch model judgment"
    invariant forbids.
    """
    spanning = f"{BODY}\n\n## Term\nThis Agreement commences on the Effective Date"
    ok, issue = _validated_issue(_response(spanning))
    if not ok:
        failures.append(f"[8a] response failed validation outright: {issue}")
        return
    if issue.get("source_quote") != spanning:
        failures.append(
            f"[8b] a non-leading marker must be left alone, got "
            f"{issue.get('source_quote')!r}"
        )


def test_hash_text_that_is_not_a_marker_is_left_alone(failures: list[str]) -> None:
    """`"##"` with no following space, and a `"#"` heading, are not what
    `document_text_for_review` emits. Neither may be treated as scaffolding.
    """
    for quote in (f"##{HEADING} means the following: {BODY}", f"# {HEADING}\n{BODY}"):
        ok, issue = _validated_issue(_response(quote))
        if not ok:
            failures.append(f"[9a] response failed validation outright: {issue}")
            continue
        if issue.get("source_quote") != quote:
            failures.append(
                f"[9b] {quote[:24]!r}... must pass through unchanged, got "
                f"{issue.get('source_quote')!r}"
            )


# ---------------------------------------------------------------------------
# 4. The prompt rule, and the duplicated literal.
# ---------------------------------------------------------------------------


def test_prompt_tells_the_model_not_to_copy_the_marker(failures: list[str]) -> None:
    """The normalizer is the backstop. The actual fix is telling the model
    the marker is not contract text -- otherwise every review keeps paying
    for a round of silent degradation the backstop merely papers over.
    """
    # The source_quote rule lives in the binary-decision overlay block --
    # the same block that already carries the "MUST NOT cross a paragraph
    # boundary" constraint this one sits beside.
    prompt = primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK
    occurrences = [m.start() for m in re.finditer(re.escape("## "), prompt)]
    if not occurrences:
        failures.append(
            "[10a] the binary-decision overlay block never mentions the "
            "'## ' heading marker, so the model is never told the line it "
            "can see at the top of every clause is not part of the contract."
        )
        return
    if not any(
        "source_quote" in prompt[max(0, i - 500) : i + 500] for i in occurrences
    ):
        failures.append(
            "[10b] the '## ' marker is mentioned in the prompt but not in "
            "connection with source_quote -- the rule that matters is that "
            "it must never be copied into one."
        )


def test_marker_literal_matches_review_spine(failures: list[str]) -> None:
    """`primary_review_pass` owns its own copy of the marker literal
    (`review_spine` imports `primary_review_pass`, so it cannot import back
    without a cycle) -- this is the cross-check that stops the two from
    drifting, exactly as `tests/test_full_doc_threshold.py` does for
    `INPUT_MODE_SECTION_OUTLINE`.
    """
    marker = getattr(primary_review_pass, "RENDERED_HEADING_MARKER", None)
    if marker is None:
        failures.append(
            "[11a] primary_review_pass.RENDERED_HEADING_MARKER does not exist"
        )
        return
    rendered = review_spine.document_text_for_review(
        [{"heading": HEADING, "text": BODY}]
    )
    if not rendered.startswith(marker):
        failures.append(
            f"[11b] review_spine renders a heading as {rendered[:8]!r}..., but "
            f"primary_review_pass strips {marker!r} -- the two have drifted, "
            f"and every marker-carrying quote silently stops being cleaned."
        )


def main() -> int:
    failures: list[str] = []

    test_marker_carrying_quote_cannot_locate(failures)
    test_stripped_quote_locates_end_to_end(failures)
    test_marker_line_stripped_from_top_level_issue(failures)
    test_marker_line_stripped_from_critic_added_issue(failures)
    test_heading_only_quote_degrades_to_absent(failures)
    test_heading_only_quote_with_trailing_newline_degrades_to_absent(failures)
    test_quote_without_marker_is_byte_identical(failures)
    test_marker_not_at_start_is_left_alone(failures)
    test_hash_text_that_is_not_a_marker_is_left_alone(failures)
    test_prompt_tells_the_model_not_to_copy_the_marker(failures)
    test_marker_literal_matches_review_spine(failures)

    if failures:
        print("FAIL: heading-marker quote-poisoning gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: heading-marker quote-poisoning gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
