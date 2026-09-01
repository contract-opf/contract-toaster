#!/usr/bin/env python3
"""
Regression test for the SECOND-ORDER defect introduced by the heading-
fidelity fix (`scripts/review_spine.py::document_text_for_review`): a
rendered `"## "` heading marker (or issue #627's `"[pNNNN] "` block-id
marker) leaking into a model's transcript and silently costing the redline.

## Root problem this proves fixed

`document_text_for_review` (the fix for "28 of 30 section headings never
reach the model") renders each normalized paragraph as:

    [pNNNN] ## <heading>\n<body text>

-- so the model now SEES the clause title and an address it can name, which
is the whole point. But neither marker exists in any document: a block's own
`text` carries neither, and `heading` is a separate key on the normalized
record. So a model that transcribes a block starting from the top of what it
was shown -- the visually natural thing to do, and something the prompt did
not originally warn against -- produces `keep`/`delete` segments that cannot
prove against the block.

Under the v3 block-transcript contract that is MORE consequential than it
was under v2, not less: a poisoned segment no longer costs one issue its
redline, it fails the WHOLE transcript as `source_mismatch`
(`block_transcript.validate_block_patches`) and the review delivers no
tracked changes at all. Nothing errors; the redline is just quietly missing.
This is the same failure mode as issue #560, which cost 65% of real
documents their redline.

Issue #628 deleted this file's v2 half along with the quote machinery it
exercised: `validate_model_response` no longer normalizes any quote field,
because no contract defines one. Every property that half proved is proved
below over transcript SEGMENTS, which is where a copied marker now does the
damage.

This test file:

  1. Pins the prompt rules that tell the model not to copy either marker in
     the first place (the normalizer is the backstop, not the fix), and
     cross-checks the duplicated marker literals against `review_spine`'s
     own rendering, per this repo's "each module owning its own copy of
     small shared sentinels" convention.
  2. Proves both normalizers fire, in order, on a `keep` segment that
     carried both markers.
  3. Proves the normalization can never touch model judgment: an `insert`
     segment is left alone (it is AUTHORED, not transcribed), a non-leading
     `"## "` is left alone, and `"##"`-without-a-space is left alone.

Fails on a tree where `primary_review_pass` does not strip the markers
(pre-fix: a marker-carrying segment survived validation intact and went on
to fail `source_mismatch`).

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
import review_spine  # noqa: E402


HEADING = "Confidential Information"
BODY = (
    "Each party shall hold the other party's Confidential Information in "
    "strict confidence and shall not disclose it to any third party without "
    "prior written consent."
)


# ---------------------------------------------------------------------------
# 1. The prompt rules, and the duplicated literals.
# ---------------------------------------------------------------------------


def test_prompt_tells_the_model_not_to_copy_any_marker(failures: list[str]) -> None:
    """The normalizer is the backstop. The actual fix is telling the model
    the markers are not contract text -- otherwise every review keeps paying
    for a round of silent degradation the backstop merely papers over.

    Issue #627 added a SECOND rendered marker (the `"[pNNNN] "` block id) and
    made both consequential in a new way: under the block-transcript contract
    a copied marker no longer costs one issue its redline, it fails the whole
    transcript as `source_mismatch`. So the prompt must disclaim both.
    """
    prompt = primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK

    if "## " not in prompt:
        failures.append(
            "[10a] the binary-decision overlay block never mentions the "
            "'## ' heading marker, so the model is never told the line it "
            "can see at the top of every clause is not part of the contract."
        )
    if "[pNNNN]" not in prompt:
        failures.append(
            "[10b] the overlay never mentions the '[pNNNN]' block-id marker, "
            "so the model is never told the id it can see at the head of "
            "every paragraph is not part of the contract."
        )
    # Both must be disclaimed as PIPELINE ORIENTATION, not merely named.
    if "NOT part of the contract" not in prompt:
        failures.append(
            "[10c] the overlay names the markers but never says they are not "
            "part of the contract -- naming a thing is not disclaiming it."
        )
    if "NEVER copy the \"[pNNNN]\" markers into any output field" not in prompt:
        failures.append(
            "[10d] the overlay does not carry the explicit no-copy rule for "
            "the block-id marker."
        )


def test_marker_literals_match_review_spine(failures: list[str]) -> None:
    """`primary_review_pass` owns its own copy of BOTH marker literals
    (`review_spine` imports `primary_review_pass`, so it cannot import back
    without a cycle) -- this is the cross-check that stops them from
    drifting from what `review_spine` actually renders.
    """
    heading_marker = getattr(primary_review_pass, "RENDERED_HEADING_MARKER", None)
    block_pattern = getattr(primary_review_pass, "RENDERED_BLOCK_MARKER_PATTERN", None)
    if heading_marker is None:
        failures.append("[11a] primary_review_pass.RENDERED_HEADING_MARKER does not exist")
        return
    if block_pattern is None:
        failures.append(
            "[11b] primary_review_pass.RENDERED_BLOCK_MARKER_PATTERN does not exist -- "
            "nothing strips the block-id marker issue #627 renders"
        )
        return

    rendered = review_spine.document_text_for_review(
        [{"heading": HEADING, "text": BODY, "block_id": "p0001"}]
    )
    # Issue #642 moved the heading ABOVE the block marker, so the rendering is
    # now "## Heading\n[pNNNN] body". Strip in that same order -- heading line
    # first, then the block marker introducing the body -- which is the exact
    # order `validate_model_response` runs the two normalizers in. Both
    # patterns are anchored at `^`, so the order is load-bearing, not
    # cosmetic: reversing it leaves the block marker in the value and fails
    # the whole transcript as `source_mismatch`.
    if not rendered.startswith(heading_marker):
        failures.append(
            f"[11c] review_spine renders a heading-bearing paragraph as {rendered[:12]!r}..., "
            f"but primary_review_pass's heading marker {heading_marker!r} does not lead it -- "
            f"the two have drifted, and every marker-carrying segment silently stops being "
            f"cleaned."
        )
    else:
        _heading_line, _newline, after_heading = rendered.partition("\n")
        without_block = block_pattern.sub("", after_heading, count=1)
        if without_block == after_heading:
            failures.append(
                f"[11d] after the heading line, the block marker must introduce the body and "
                f"must match primary_review_pass's block-marker pattern; got "
                f"{after_heading[:12]!r}"
            )
        elif without_block != BODY:
            failures.append(
                f"[11f] stripping the heading line then the block marker must leave EXACTLY "
                f"the block's own text (issue #642 -- this is what a model transcribes and "
                f"what block_transcript proves against); got {without_block[:40]!r}"
            )

    # A paragraph with NO block id renders exactly as it did before #627 --
    # the marker is additive, never a placeholder for a missing id.
    unmarked = review_spine.document_text_for_review([{"heading": HEADING, "text": BODY}])
    if not unmarked.startswith(heading_marker):
        failures.append(
            f"[11e] a paragraph carrying no block_id must render unmarked; got {unmarked[:12]!r}"
        )


# ---------------------------------------------------------------------------
# 2/3. The hazard under the v3 block-transcript contract (issue #627), where
#      a copied marker costs the WHOLE transcript rather than one issue.
# ---------------------------------------------------------------------------


def _v3_transcript_response(first_keep_text: str) -> str:
    """A v3 REQUEST_CHANGE whose first `keep` segment is `first_keep_text`."""
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": "4",
                    "section_title": HEADING,
                    "counterparty_change_summary": "Confidentiality is one-way.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "We require mutual confidentiality.",
                    "playbook_topic_id": "confidentiality",
                    "internal_precedent_citation": None,
                }
            ],
            "block_patches": [
                {
                    "block_id": "p0001",
                    "segments": [
                        {"op": "keep", "text": first_keep_text},
                        {"op": "delete", "text": " ", "issue_key": "I1"},
                        {"op": "insert", "text": "  ", "issue_key": "I1"},
                    ],
                }
            ],
            "block_ops": [],
        }
    )


def _v3_first_segment(raw: str) -> Any:
    ok, parsed = primary_review_pass.validate_model_response(raw)
    if not ok:
        return None
    return parsed["block_patches"][0]["segments"][0]["text"]


def test_block_marker_stripped_from_a_keep_segment(failures: list[str]) -> None:
    poisoned = _v3_transcript_response(f"[p0001] {BODY}")
    got = _v3_first_segment(poisoned)
    if got != BODY:
        failures.append(
            f"[12a] the leading block-id marker was not stripped from a keep segment; "
            f"got {got!r}"
        )


def test_heading_marker_stripped_from_a_keep_segment(failures: list[str]) -> None:
    """The model copied the WHOLE rendered block -- heading line, id and all
    -- into its first keep segment. Both normalizers must fire, in order.

    The order here is the ORDER REVIEW_SPINE ACTUALLY RENDERS (issue #642):
    the heading line first, then the block marker introducing the body. It
    used to be `"[p0001] ## Heading\nbody"`; asserting over that shape now
    would be asserting over a string production can no longer emit, and would
    silently stop covering the real hazard.
    """
    poisoned = _v3_transcript_response(f"## {HEADING}\n[p0001] {BODY}")
    got = _v3_first_segment(poisoned)
    if got != BODY:
        failures.append(
            f"[12b] a keep segment carrying BOTH rendered markers was not reduced to the "
            f"paragraph's own text; got {got!r}"
        )


def test_an_insert_segment_is_never_edited(failures: list[str]) -> None:
    """An `insert` is language the model is AUTHORING, not transcribing. A
    leading `"## "` there is the model's own text, and stripping it would be
    the backstop editing model judgment -- the invariant every normalization
    in `validate_model_response` is bound by."""
    authored = "## Mutual Confidentiality\nEach party shall hold the other's information in confidence."
    raw = json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": "4",
                    "section_title": HEADING,
                    "counterparty_change_summary": "Confidentiality is one-way.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "We require mutual confidentiality.",
                    "playbook_topic_id": "confidentiality",
                    "internal_precedent_citation": None,
                }
            ],
            "block_patches": [
                {
                    "block_id": "p0001",
                    "segments": [
                        {"op": "keep", "text": BODY},
                        {"op": "insert", "text": authored, "issue_key": "I1"},
                    ],
                }
            ],
            "block_ops": [],
        }
    )
    ok, parsed = primary_review_pass.validate_model_response(raw)
    if not ok:
        failures.append(f"[12c] setup: the response did not validate: {parsed}")
        return
    got = parsed["block_patches"][0]["segments"][1]["text"]
    if got != authored:
        failures.append(
            f"[12d] an insert segment was edited by the marker backstop; got {got!r}"
        )


def test_a_non_leading_marker_in_a_keep_segment_is_left_alone(failures: list[str]) -> None:
    """Only a LEADING marker line is rendering scaffolding. A `"## "` deeper
    inside a transcribed segment is genuine document text -- removing it
    would be this normalizer inventing an edit, which the "never patch model
    judgment" invariant forbids, and would then fail the transcript against
    the block it was supposed to protect.
    """
    inner = f"{BODY}\n## Term\nThis Agreement commences on the Effective Date"
    got = _v3_first_segment(_v3_transcript_response(inner))
    if got != inner:
        failures.append(
            f"[13a] a non-leading '## ' must be left alone, got {got!r}"
        )


def test_hash_text_that_is_not_a_marker_is_left_alone(failures: list[str]) -> None:
    """`"##"` with no following space, and a `"#"` heading, are not what
    `document_text_for_review` emits. Neither may be treated as scaffolding.
    """
    for text in (f"##{HEADING} means the following: {BODY}", f"# {HEADING}\n{BODY}"):
        got = _v3_first_segment(_v3_transcript_response(text))
        if got != text:
            failures.append(
                f"[13b] {text[:24]!r}... must pass through unchanged, got {got!r}"
            )


def main() -> int:
    failures: list[str] = []

    test_prompt_tells_the_model_not_to_copy_any_marker(failures)
    test_marker_literals_match_review_spine(failures)
    test_block_marker_stripped_from_a_keep_segment(failures)
    test_heading_marker_stripped_from_a_keep_segment(failures)
    test_an_insert_segment_is_never_edited(failures)
    test_a_non_leading_marker_in_a_keep_segment_is_left_alone(failures)
    test_hash_text_that_is_not_a_marker_is_left_alone(failures)

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
