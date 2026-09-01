#!/usr/bin/env python3
"""
Slice test for issue #642: "every heading-bearing block fails source_mismatch,
because the rendering puts the heading inside the block marker but the block's
provable text excludes it".

## The defect this proves fixed

`review_spine.document_text_for_review` used to render a heading-bearing
paragraph as ONE marked block:

    [p0005] ## Term and Termination
    The Receiving Party's obligations under this Section 4 survive...

while `extraction_normalization_stage.build_block_map` keys `p0005` to
`paragraph["text"]` ALONE -- the heading lives in a separate
`paragraph["heading"]` field and is part of no block's provable text. So a
model transcribing what it was SHOWN under `[p0005]` transcribed the heading
too, and `block_transcript.validate_block_patches` rejected the WHOLE
transcript with `source_mismatch`.

This is not hypothetical and it is not a fixture artefact. It is how the FIRST
live-model run of the v3 block-transcript path died, on a 9-block synthetic
NDA:

    status=ERROR_MANUAL_REVIEW_REQUIRED  primary_attempts=2  validity_rate=0.0
    block_transcript_rejected:
      - [source_mismatch] block p0005: segment 0 diverges from the block's own
        text at folded offset 1
          the document has: "he Receiving Party's obligations under t"
          you transcribed:  "erm and Termination The Receiving Party'"

Note WHICH model behaviour triggers it: the prompt forbids copying the
rendered markers, so a COMPLIANT model drops the `"## "` and keeps the heading
WORDS. That is precisely the form
`primary_review_pass._strip_rendered_heading_markers` cannot repair, because
that backstop only fires on a surviving literal `"## "`. Obeying the prompt
produced the unrepairable case; the backstop only ever caught the disobedient
model.

## Why the rest of the suite could not catch this

Every v3 fixture builds its segments by slicing `build_block_map`'s own text,
so the fixtures transcribe perfectly by construction and can never reproduce
the divergence. #627's Notes said the offline gate "uses schema-perfect fakes
and cannot prove real-model behavior" -- this file closes exactly that gap for
this defect, by transcribing WHAT THE RENDERING SHOWS rather than what the
block map holds. Assertion [1a] is the one that fails on the pre-fix tree.

Run with: python3 tests/test_heading_block_rendering_642.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "document-shapes" / "baseline-mutual-nda.SYNTHETIC.docx"

for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import review_spine  # noqa: E402

BLOCK_MARKER_PREFIX = "[p"


def _paragraphs() -> list[dict[str, Any]]:
    return ens.extract_and_normalize(FIXTURE.read_bytes())["paragraphs"]


def _rendered_blocks(doc_text: str) -> list[str]:
    return doc_text.split("\n\n")


def _block_containing(doc_text: str, block_id: str) -> str | None:
    for block in _rendered_blocks(doc_text):
        if f"[{block_id}] " in block or block.rstrip().endswith(f"[{block_id}]"):
            return block
    return None


def _as_the_model_transcribes_it(rendered_block: str, block_id: str) -> str:
    """What a COMPLIANT model transcribes for `block_id`.

    The prompt forbids copying the rendered `[pNNNN]` and `"## "` markers, so
    the model reproduces everything the marker introduces, with the markers
    themselves removed. That is the whole point: this is the model doing what
    it was told, not a model misbehaving.
    """
    marker = f"[{block_id}] "
    _before, sep, after = rendered_block.partition(marker)
    if not sep:
        return ""
    return after


def _deletable_span(own_text: str) -> str:
    """A contiguous span of the block's OWN text to delete.

    Derived from the block rather than hard-coded, so this test works on any
    heading-bearing block the fixture happens to yield first -- a hard-coded
    anchor silently picks the wrong block and fails for an unrelated reason.
    """
    words = own_text.split()
    if len(words) < 6:
        raise AssertionError(f"block text too short to build an edit from: {own_text!r}")
    span = " ".join(words[2:5])
    if own_text.count(span) != 1:
        raise AssertionError(f"span {span!r} is not unique in {own_text!r}")
    return span


def _edit_segments(source: str, delete_text: str, issue_key: str = "I1") -> list[dict[str, Any]]:
    index = source.find(delete_text)
    if index < 0:
        raise AssertionError(f"anchor {delete_text!r} not present in {source!r}")
    return [
        {"op": "keep", "text": source[:index]},
        {"op": "delete", "text": delete_text, "issue_key": issue_key},
        {"op": "insert", "text": "five (5) years", "issue_key": issue_key},
        {"op": "keep", "text": source[index + len(delete_text) :]},
    ]


# ---------------------------------------------------------------------------
# 1. A compliant transcription of a heading-bearing block PROVES.
# ---------------------------------------------------------------------------


def test_a_compliant_transcription_of_a_heading_bearing_block_proves(
    failures: list[str],
) -> None:
    paragraphs = _paragraphs()
    block_map = ens.build_block_map(paragraphs)
    doc_text = review_spine.document_text_for_review(paragraphs)

    heading_block_id = None
    for paragraph in paragraphs:
        heading = (paragraph.get("heading") or "").strip()
        if heading and heading != review_spine._UNTITLED_HEADING and paragraph.get("text"):
            heading_block_id = paragraph["block_id"]
            break
    if heading_block_id is None:
        failures.append(
            "[0a] Fixture assumption broken: baseline-mutual-nda.SYNTHETIC.docx must carry at "
            "least one paragraph with BOTH a heading and body text for this test to mean anything."
        )
        return

    rendered = _block_containing(doc_text, heading_block_id)
    if rendered is None:
        failures.append(f"[0b] {heading_block_id} does not appear in the rendered document.")
        return

    transcribed = _as_the_model_transcribes_it(rendered, heading_block_id)
    own_text = block_map[heading_block_id]["text"]


    # [1a] THE REGRESSION. Pre-fix the rendering was
    # "[pNNNN] ## Heading\nbody", so `transcribed` carried the heading and
    # this proof failed with source_mismatch at folded offset 1.
    result = block_transcript.validate_block_patches(
        [
            {
                "block_id": heading_block_id,
                "segments": _edit_segments(transcribed, _deletable_span(own_text)),
            }
        ],
        [],
        block_map,
    )
    if result.get("status") != "proven":
        detail = "; ".join(
            f"{f.get('reason')}: {f.get('detail')}" for f in result.get("failures", [])
        )
        failures.append(
            f"[1a] A COMPLIANT transcription of what the rendering shows under "
            f"[{heading_block_id}] must PROVE against that block's own text; got "
            f"{result.get('status')!r} -- {detail}"
        )

    # [1b] The stronger statement: what follows the marker IS the block's own
    # text, exactly. This is the invariant that makes [1a] true by
    # construction rather than by luck.
    if transcribed != own_text:
        failures.append(
            f"[1b] Everything after a block marker must be EXACTLY that block's provable text.\n"
            f"       shown after [{heading_block_id}]: {transcribed[:60]!r}\n"
            f"       block_map text:                   {own_text[:60]!r}"
        )


# ---------------------------------------------------------------------------
# 2. The invariant holds for EVERY rendered block, not just the sampled one.
# ---------------------------------------------------------------------------


def test_every_marked_block_is_followed_by_exactly_its_own_text(failures: list[str]) -> None:
    paragraphs = _paragraphs()
    block_map = ens.build_block_map(paragraphs)
    doc_text = review_spine.document_text_for_review(paragraphs)

    checked = 0
    for block_id, entry in block_map.items():
        rendered = _block_containing(doc_text, block_id)
        if rendered is None:
            # A block with neither heading nor text renders nothing at all.
            if entry.get("text"):
                failures.append(f"[2a] {block_id} has text but does not appear in the rendering.")
            continue
        transcribed = _as_the_model_transcribes_it(rendered, block_id)
        if transcribed != entry.get("text", ""):
            failures.append(
                f"[2b] {block_id}: text after the marker != the block's provable text.\n"
                f"       after marker: {transcribed[:60]!r}\n"
                f"       block_map:    {entry.get('text','')[:60]!r}"
            )
        checked += 1

    if checked == 0:
        failures.append("[2c] Vacuous: no rendered blocks were checked.")


# ---------------------------------------------------------------------------
# 3. Headings are still SHOWN -- the fix must not hide document structure.
# ---------------------------------------------------------------------------


def test_headings_are_still_rendered_above_their_block(failures: list[str]) -> None:
    paragraphs = _paragraphs()
    doc_text = review_spine.document_text_for_review(paragraphs)

    headings = [
        (p["block_id"], (p.get("heading") or "").strip())
        for p in paragraphs
        if (p.get("heading") or "").strip()
        and (p.get("heading") or "").strip() != review_spine._UNTITLED_HEADING
    ]
    if not headings:
        failures.append("[3a] Fixture assumption broken: no headings found to assert over.")
        return

    for block_id, heading in headings:
        if f"## {heading}" not in doc_text:
            failures.append(f"[3b] Heading {heading!r} is no longer shown to the model at all.")
            continue
        rendered = _block_containing(doc_text, block_id)
        if rendered is None:
            continue
        # The heading must precede the marker, never follow it.
        heading_at = rendered.find(f"## {heading}")
        marker_at = rendered.find(f"[{block_id}]")
        if heading_at < 0 or marker_at < 0:
            continue
        if heading_at > marker_at:
            failures.append(
                f"[3c] {block_id}: the heading must render ABOVE the block marker, not after it "
                f"-- inside the marker is what made a compliant transcription unprovable (#642)."
            )


TESTS = [
    test_a_compliant_transcription_of_a_heading_bearing_block_proves,
    test_every_marked_block_is_followed_by_exactly_its_own_text,
    test_headings_are_still_rendered_above_their_block,
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
    if failures:
        print("\nFAIL: heading/block rendering gate (issue #642).")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: all heading/block rendering (issue #642) assertions satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
