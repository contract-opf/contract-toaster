#!/usr/bin/env python3
"""
Block-transcript validator (issue #620): turn a MODEL-AUTHORED block
transcript into a PROVEN transcript anchored to the document's own bytes.

This is the heart of Candidate E. The model chooses the edit boundaries --
it transcribes each paragraph it wants to touch as an ordered list of
`keep` / `delete` / `insert` segments -- and this module proves, in pure
logic with zero I/O, that the `keep`+`delete` half of that transcript maps
back onto the block's REAL text, character for character. Code never guesses
what the model meant and never repairs a transcript: every divergence it
cannot account for as transcription noise is a structured, fail-closed
rejection.

## Why a transcript instead of a quote

The retired quote locator (issue #375) anchored an edit by requiring the
model to reproduce a document-wide-UNIQUE quote. That is a hard thing to ask
of a model and a brittle thing to verify: a quote that appears twice is
`ambiguous`, a quote whose punctuation drifted is `not_found`, and either
way the edit is dropped. Block addressing (issue #619) removes the
uniqueness burden -- the model names a code-assigned `block_id` -- and this
module removes the "find it" burden: the segments are already in order and
already cover the block, so alignment is a single sequential walk with no
search and therefore no ambiguity to resolve by guessing.

What survives from the quote path is the one property that actually matters:
the applied edit is expressed in the DOCUMENT's characters, never the
model's transcription of them. Every offset returned here indexes the real
block text, so a `delete` covering a curly apostrophe deletes the curly
apostrophe.

## The noise this tolerates, and the guard it must not weaken

Transcription noise is real and measured: 15 of the 16 normalizable
documents in the real EIAA corpus carry curly punctuation, and models
straighten it when copying text back out. Runs of whitespace get collapsed
the same way. Both sides are therefore compared in `scripts/text_fold.py`'s
COMPARISON-ONLY folded form, which collapses whitespace runs and folds
typographic punctuation to ASCII -- and nothing else.

The `keep` segments are what make this safe. A `keep` asserts "this text is
unchanged", so if the model silently rewrote one comma inside a `keep`, the
fold does NOT absorb it: casing, word content and every non-typographic
punctuation mark must still match exactly, so the patch is rejected with
`source_mismatch`. That is the unmarked-rewrite guard, and it is the reason
the fold is a narrow encoding-only tolerance rather than a fuzzy matcher.

## Inputs (plain dicts -- deliberately no schema dependency)

`block_patches`: sub-block edits, EXACTLY ONE entry per `block_id`.

    {"block_id": "b0007",
     "segments": [{"op": "keep",   "text": "The Term shall be "},
                  {"op": "delete", "text": "sixty (60)", "issue_key": "TERM-1"},
                  {"op": "insert", "text": "thirty (30)", "issue_key": "TERM-1"},
                  {"op": "keep",   "text": " days."}]}

Two issues editing different spans of the same paragraph are TWO segments
carrying different `issue_key`s inside that ONE entry -- not two entries.
Two entries for one `block_id` are rejected (`duplicate_block_id`): a block
has exactly one true text, and two independent transcripts of it cannot both
be proven against it without merging them, which would be a repair.

`block_ops`: whole-block operations, which need no transcript because they
name no sub-block span.

    {"op": "delete_block", "block_id": "b0011", "issue_key": "IP-2"}
    {"op": "insert_block_after", "anchor_block_id": "b0011" | "start",
     "new_text": "...", "issue_key": "IP-2"}

`block_map`: `extraction_normalization_stage.build_block_map()`'s output --
`block_id -> {"text", "heading", "physical_spans", "index"}`. This module
only ever READS `text` and `index` (plus `heading`, passed through); it
never re-derives a block's text and never touches OOXML.

## Output

On success, a ProvenTranscript: `{"status": "proven", "blocks": [...],
"block_ops": [...], "by_issue": {...}, "failures": []}`. Every `keep`/
`delete` op carries `(start, end)` offsets into the block's REAL text, every
`insert` carries a proven `at` offset, each block carries its `final_text`
(the accept-all projection), and `by_issue` groups every edit -- segment
edits AND block ops -- under the `issue_key` that authored it.

On failure, `{"status": "rejected", "blocks": [], "block_ops": [],
"by_issue": {}, "failures": [ ... ]}`, with one structured entry per
rejected patch or block op. `source_mismatch` entries carry the first
divergence offset and ~40 characters of folded context from EACH side, which
is what an informed retry shows the model. This module never raises on bad
input and never partially applies: a rejected transcript yields no blocks.

See: `scripts/text_fold.py`, `scripts/extraction_normalization_stage.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import text_fold  # noqa: E402

# ---------------------------------------------------------------------------
# Rejection reasons. The first five are this issue's named per-patch failures;
# the last three cover conditions the issue requires rejecting (duplicate
# entries, malformed or contradictory whole-block ops) that are structural
# rather than segment-level. Every one of them is fail-closed: none of them
# has a "best effort" branch.
# ---------------------------------------------------------------------------
REASON_UNKNOWN_BLOCK_ID = "unknown_block_id"
REASON_SOURCE_MISMATCH = "source_mismatch"
REASON_ALIGNMENT_AMBIGUOUS = "alignment_ambiguous"
REASON_NO_EDIT_SEGMENTS = "no_edit_segments"
REASON_MALFORMED_SEGMENTS = "malformed_segments"
REASON_DUPLICATE_BLOCK_ID = "duplicate_block_id"
REASON_MALFORMED_BLOCK_OP = "malformed_block_op"
REASON_CONFLICTING_BLOCK_OP = "conflicting_block_op"

REJECTION_REASONS = (
    REASON_UNKNOWN_BLOCK_ID,
    REASON_SOURCE_MISMATCH,
    REASON_ALIGNMENT_AMBIGUOUS,
    REASON_NO_EDIT_SEGMENTS,
    REASON_MALFORMED_SEGMENTS,
    REASON_DUPLICATE_BLOCK_ID,
    REASON_MALFORMED_BLOCK_OP,
    REASON_CONFLICTING_BLOCK_OP,
)

SEGMENT_OPS = ("keep", "delete", "insert")
SOURCE_OPS = ("keep", "delete")

# The WHOLE-BLOCK op names, defined here because this module owns the
# transcript vocabulary: `_validate_block_op` normalizes an incoming op to
# exactly these strings, and every reader downstream
# (`scripts/redline_block_apply.py`, which compiles them, and
# `scripts/redline_projections.py`, which proves the compiled result) binds
# its own constant to these rather than re-spelling the literals.
OP_DELETE_BLOCK = "delete_block"
OP_INSERT_BLOCK_AFTER = "insert_block_after"

_SEGMENT_KEYS = {"op", "text", "issue_key"}
_PATCH_KEYS = {"block_id", "segments"}
_DELETE_BLOCK_KEYS = {"op", "block_id", "issue_key"}
_INSERT_BLOCK_KEYS = {"op", "anchor_block_id", "new_text", "issue_key"}

# `insert_block_after` may name this instead of a `block_id` to insert a new
# paragraph BEFORE the document's first block, which no existing block can
# anchor. It is a literal sentinel, never a real id (`build_block_map` stamps
# `b0001`-style ids), so it can never collide with one.
ANCHOR_START = "start"

# How much folded context a `source_mismatch` carries from each side. Enough
# to see the divergence in its sentence without pasting the whole block back
# into a retry prompt.
_CONTEXT_CHARS = 40


def _failure(reason: str, detail: str, **extra: Any) -> dict[str, Any]:
    failure: dict[str, Any] = {"reason": reason, "detail": detail}
    failure.update(extra)
    return failure


# ---------------------------------------------------------------------------
# Segment-list shape
# ---------------------------------------------------------------------------


def _segment_shape_error(segments: Any) -> str | None:
    """The first structural problem with a patch's `segments` list, or `None`.

    Shape only -- nothing here looks at the block's text. Unknown keys are
    rejected rather than ignored: a typo'd `issueKey` on a `delete` would
    otherwise silently become an unattributed edit.

    The adjacency rules are the issue's "overlapping/misordered ops". Two
    consecutive `keep`s are always a redundant split of one span (a `keep`
    carries no `issue_key`, so nothing distinguishes them), and two
    consecutive `delete`s or `insert`s from the SAME issue are one edit
    written as two. Consecutive same-op segments from DIFFERENT issues are
    legitimate -- two issues really can delete adjacent spans -- so those are
    left alone.
    """
    if not isinstance(segments, list) or not segments:
        return "segments must be a non-empty list"
    previous: dict[str, Any] | None = None
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            return f"segment {index} is not an object"
        unknown = sorted(set(segment) - _SEGMENT_KEYS)
        if unknown:
            return f"segment {index} carries unknown key(s) {unknown}"
        op = segment.get("op")
        if op not in SEGMENT_OPS:
            return f"segment {index} has op {op!r}, expected one of {list(SEGMENT_OPS)}"
        text = segment.get("text")
        if not isinstance(text, str) or text == "":
            return f"segment {index} ({op}) has empty or non-string text"
        issue_key = segment.get("issue_key")
        if op == "keep":
            if issue_key is not None:
                return f"segment {index} is a keep but carries issue_key {issue_key!r}"
        elif not isinstance(issue_key, str) or not issue_key.strip():
            return f"segment {index} ({op}) is missing a non-empty issue_key"
        if previous is not None and previous.get("op") == op:
            if op == "keep":
                return (
                    f"segments {index - 1} and {index} are consecutive keeps; "
                    "one unchanged span must be one segment"
                )
            if previous.get("issue_key") == issue_key:
                return (
                    f"segments {index - 1} and {index} are consecutive {op}s for "
                    f"issue_key {issue_key!r}; one edit must be one segment"
                )
        previous = segment
    return None


# ---------------------------------------------------------------------------
# Alignment: fold both sides, walk the block once, project back
# ---------------------------------------------------------------------------


def _common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _mismatch(
    folded_block: str, block_offset: int, folded_segment: str, segment_offset: int, index: int
) -> dict[str, Any]:
    """A `source_mismatch` carrying the first divergence and the folded
    context an informed retry needs from BOTH sides."""
    return _failure(
        REASON_SOURCE_MISMATCH,
        (
            f"segment {index} diverges from the block's own text at folded "
            f"offset {block_offset}"
        ),
        segment_index=index,
        divergence_offset=block_offset,
        block_context=folded_block[block_offset : block_offset + _CONTEXT_CHARS],
        transcript_context=folded_segment[segment_offset : segment_offset + _CONTEXT_CHARS],
    )


def _place_source_segments(
    folded_block: str, source_segments: list[dict[str, Any]]
) -> tuple[list[list[int]], list[tuple[int, int]], dict[str, Any] | None]:
    """Walks `source_segments` (the `keep`/`delete` half of a transcript, in
    order) across `folded_block` exactly once, left to right.

    There is no searching: each segment must match AT the cursor, so the walk
    is deterministic and cannot pick between candidate placements. Returns
    `(placements, gaps, failure)` where `placements[i]` is the folded
    `[start, end)` of segment `i` and `gaps[i]` is the folded run of block
    whitespace no segment claimed immediately before it (usually empty).

    Two transcription tolerances, both whitespace-only and both narrow:

      * a segment whose leading space RE-TRANSCRIBES the whitespace run the
        previous segment already consumed (the model split the run and kept a
        space on each side) contributes that space to neither side, rather
        than demanding a second space the block does not have -- but the
        tolerance stops short of absorbing the SEGMENT: a segment left with
        nothing to align to is rejected, never placed zero-width;
      * block whitespace the transcript skipped entirely is recorded as a
        `gap` for the caller to attribute -- see `_resolve_gaps`.
    """
    placements: list[list[int]] = []
    gaps: list[tuple[int, int]] = []
    nb = len(folded_block)
    pos = 0
    for index, segment in enumerate(source_segments):
        folded_segment, _, _ = text_fold.fold_text_with_map(segment["text"])
        matched_at: int | None = None
        consumed = folded_segment
        if folded_block.startswith(folded_segment, pos):
            matched_at = pos
        elif (
            folded_segment.startswith(" ")
            and pos > 0
            and folded_block[pos - 1] == " "
            and folded_block.startswith(folded_segment[1:], pos)
        ):
            if len(folded_segment) == 1:
                # The segment IS the space the previous one already consumed,
                # so absorbing it would align it to zero characters -- a
                # zero-width op emitted under `status: "proven"` while the
                # model's edit is silently discarded. That is a repair, not a
                # transcription tolerance: fail closed.
                return (
                    placements,
                    gaps,
                    _failure(
                        REASON_ALIGNMENT_AMBIGUOUS,
                        (
                            f"segment {index} ({segment['op']}) is whitespace the "
                            f"previous segment already consumed at folded offset "
                            f"{pos}; it would align to no characters at all, so "
                            "what it covers cannot be decided without guessing"
                        ),
                        segment_index=index,
                        divergence_offset=pos,
                    ),
                )
            # The model split one whitespace run across the boundary and kept
            # a space on both sides; the previous segment already consumed it.
            matched_at = pos
            consumed = folded_segment[1:]
        else:
            skipped = pos
            while skipped < nb and folded_block[skipped] == " ":
                skipped += 1
            if skipped > pos and folded_block.startswith(folded_segment, skipped):
                matched_at = skipped

        if matched_at is None:
            # Report the divergence from whichever candidate start got
            # furthest, so the context shown is the real disagreement rather
            # than an artifact of where the walk happened to try first.
            best_block_offset = pos
            best_prefix = _common_prefix_len(folded_block[pos:], folded_segment)
            skipped = pos
            while skipped < nb and folded_block[skipped] == " ":
                skipped += 1
            if skipped > pos:
                alternative = _common_prefix_len(folded_block[skipped:], folded_segment)
                if alternative > best_prefix:
                    best_block_offset, best_prefix = skipped, alternative
            return (
                placements,
                gaps,
                _mismatch(
                    folded_block,
                    best_block_offset + best_prefix,
                    folded_segment,
                    best_prefix,
                    index,
                ),
            )

        gaps.append((pos, matched_at))
        placements.append([matched_at, matched_at + len(consumed)])
        pos = matched_at + len(consumed)

    trailing = folded_block[pos:]
    if trailing.strip():
        return (
            placements,
            gaps,
            _mismatch(folded_block, pos, "", 0, len(source_segments)),
        )
    gaps.append((pos, nb))
    return placements, gaps, None


def _resolve_gaps(
    source_segments: list[dict[str, Any]],
    placements: list[list[int]],
    gaps: list[tuple[int, int]],
) -> dict[str, Any] | None:
    """Attributes each unclaimed run of block whitespace to an adjacent
    segment, in place, or fails closed.

    Every character of the block must belong to exactly one segment, so an
    unclaimed run is a boundary that has to land on one side or the other.
    When both neighbours have the SAME disposition the choice has no
    consequence (whitespace kept between two keeps is kept either way;
    whitespace deleted between two deletes is deleted either way), so it goes
    to the preceding segment. When the neighbours DISAGREE -- a keep on one
    side, a delete on the other -- placing it decides whether that whitespace
    survives the redline, and this module does not decide that on the model's
    behalf: `alignment_ambiguous`, fail closed.

    A gap at the very start or the very end of the block has only one
    possible claimant, so there is nothing to choose and no ambiguity.
    """
    if not placements:
        return None
    for index, (gap_start, gap_end) in enumerate(gaps):
        if gap_start == gap_end:
            continue
        if index == 0:
            placements[0][0] = gap_start
        elif index == len(placements):
            placements[-1][1] = gap_end
        elif source_segments[index - 1]["op"] == source_segments[index]["op"]:
            placements[index - 1][1] = gap_end
        else:
            return _failure(
                REASON_ALIGNMENT_AMBIGUOUS,
                (
                    "the transcript omits block whitespace at folded offset "
                    f"{gap_start} between a {source_segments[index - 1]['op']} and a "
                    f"{source_segments[index]['op']}; whether it is kept or deleted "
                    "cannot be decided without guessing"
                ),
                segment_index=index,
                divergence_offset=gap_start,
            )
    return None


def _prove_patch(patch: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    """Proves one patch against one block's REAL text.

    Returns `{"ops": [...], "final_text": ...}` on success, or
    `{"failure": {...}}`. Offsets in `ops` index `block["text"]` itself, not
    the folded comparison form.
    """
    shape_error = _segment_shape_error(patch.get("segments"))
    if shape_error is not None:
        return {"failure": _failure(REASON_MALFORMED_SEGMENTS, shape_error)}

    segments: list[dict[str, Any]] = patch["segments"]
    if all(segment["op"] == "keep" for segment in segments):
        return {
            "failure": _failure(
                REASON_NO_EDIT_SEGMENTS,
                "every segment is a keep; a patch that changes nothing is not an edit",
            )
        }

    block_text: str = block.get("text", "") or ""
    folded_block, starts, ends = text_fold.fold_text_with_map(block_text)
    source_segments = [segment for segment in segments if segment["op"] in SOURCE_OPS]

    placements, gaps, failure = _place_source_segments(folded_block, source_segments)
    if failure is not None:
        return {"failure": failure}
    failure = _resolve_gaps(source_segments, placements, gaps)
    if failure is not None:
        return {"failure": failure}

    def _true_span(fold_start: int, fold_end: int) -> tuple[int, int]:
        if fold_end > fold_start:
            return starts[fold_start], ends[fold_end - 1]
        at = starts[fold_start] if fold_start < len(starts) else len(block_text)
        return at, at

    true_spans = [_true_span(start, end) for start, end in placements]

    # Independent proof that the placements really do tile the block: the
    # transcript's source segments, read back out of the block's OWN text in
    # order, must reconstruct that text exactly. If this ever disagrees with
    # the walk above, the walk is wrong -- reject rather than emit offsets
    # nothing verified.
    rebuilt = "".join(block_text[start:end] for start, end in true_spans)
    if rebuilt != block_text:
        return {
            "failure": _failure(
                REASON_SOURCE_MISMATCH,
                "the proven spans do not reconstruct the block's text",
                divergence_offset=_common_prefix_len(rebuilt, block_text),
                block_context=block_text[:_CONTEXT_CHARS],
                transcript_context=rebuilt[:_CONTEXT_CHARS],
            )
        }

    ops: list[dict[str, Any]] = []
    cursor = 0
    source_index = 0
    for segment in segments:
        if segment["op"] == "insert":
            ops.append(
                {
                    "op": "insert",
                    "at": cursor,
                    "text": segment["text"],
                    "issue_key": segment["issue_key"],
                }
            )
            continue
        start, end = true_spans[source_index]
        source_index += 1
        cursor = end
        op: dict[str, Any] = {
            "op": segment["op"],
            "start": start,
            "end": end,
            "text": block_text[start:end],
        }
        if segment["op"] == "delete":
            op["issue_key"] = segment["issue_key"]
        ops.append(op)

    final_parts: list[str] = []
    for op in ops:
        if op["op"] == "keep":
            final_parts.append(op["text"])
        elif op["op"] == "insert":
            final_parts.append(op["text"])
    return {"ops": ops, "final_text": "".join(final_parts)}


# ---------------------------------------------------------------------------
# Whole-block operations
# ---------------------------------------------------------------------------


def _validate_block_op(
    block_op: Any, block_map: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """`(normalized_op, failure)` for one whole-block operation."""
    if not isinstance(block_op, dict):
        return None, _failure(REASON_MALFORMED_BLOCK_OP, "block op is not an object")
    op = block_op.get("op")
    issue_key = block_op.get("issue_key")

    if op == OP_DELETE_BLOCK:
        unknown = sorted(set(block_op) - _DELETE_BLOCK_KEYS)
        if unknown:
            return None, _failure(
                REASON_MALFORMED_BLOCK_OP, f"delete_block carries unknown key(s) {unknown}"
            )
        if not isinstance(issue_key, str) or not issue_key.strip():
            return None, _failure(
                REASON_MALFORMED_BLOCK_OP, "delete_block is missing a non-empty issue_key"
            )
        block_id = block_op.get("block_id")
        if not isinstance(block_id, str) or block_id not in block_map:
            return None, _failure(
                REASON_UNKNOWN_BLOCK_ID,
                f"delete_block names block_id {block_id!r}, which is not in the block map",
                block_id=block_id,
            )
        return (
            {
                "op": OP_DELETE_BLOCK,
                "block_id": block_id,
                "issue_key": issue_key,
                "index": block_map[block_id]["index"],
                "text": block_map[block_id].get("text", ""),
            },
            None,
        )

    if op == OP_INSERT_BLOCK_AFTER:
        unknown = sorted(set(block_op) - _INSERT_BLOCK_KEYS)
        if unknown:
            return None, _failure(
                REASON_MALFORMED_BLOCK_OP, f"insert_block_after carries unknown key(s) {unknown}"
            )
        if not isinstance(issue_key, str) or not issue_key.strip():
            return None, _failure(
                REASON_MALFORMED_BLOCK_OP, "insert_block_after is missing a non-empty issue_key"
            )
        new_text = block_op.get("new_text")
        if not isinstance(new_text, str) or new_text == "":
            return None, _failure(
                REASON_MALFORMED_BLOCK_OP, "insert_block_after has empty or non-string new_text"
            )
        anchor = block_op.get("anchor_block_id")
        if anchor != ANCHOR_START and (not isinstance(anchor, str) or anchor not in block_map):
            return None, _failure(
                REASON_UNKNOWN_BLOCK_ID,
                (
                    f"insert_block_after names anchor_block_id {anchor!r}, which is "
                    f"neither {ANCHOR_START!r} nor in the block map"
                ),
                block_id=anchor,
            )
        return (
            {
                "op": OP_INSERT_BLOCK_AFTER,
                "anchor_block_id": anchor,
                "new_text": new_text,
                "issue_key": issue_key,
                "anchor_index": -1 if anchor == ANCHOR_START else block_map[anchor]["index"],
            },
            None,
        )

    return None, _failure(REASON_MALFORMED_BLOCK_OP, f"unknown block op {op!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_block_patches(
    block_patches: Any, block_ops: Any, block_map: Any
) -> dict[str, Any]:
    """Proves a model-authored block transcript against `block_map`'s text.

    See the module docstring for the input shapes. Returns a ProvenTranscript
    (`status="proven"`) or a structured rejection (`status="rejected"`) --
    never raises on malformed input, and never returns a partial transcript:
    one rejected patch rejects the whole transcript, because a redline
    assembled from the half that happened to prove is a redline nobody
    authored.
    """
    failures: list[dict[str, Any]] = []

    if not isinstance(block_map, dict):
        return _rejected([_failure(REASON_MALFORMED_SEGMENTS, "block_map must be a mapping")])
    if block_patches is None:
        block_patches = []
    if block_ops is None:
        block_ops = []
    if not isinstance(block_patches, list):
        return _rejected([_failure(REASON_MALFORMED_SEGMENTS, "block_patches must be a list")])
    if not isinstance(block_ops, list):
        return _rejected([_failure(REASON_MALFORMED_BLOCK_OP, "block_ops must be a list")])

    proven_blocks: list[dict[str, Any]] = []
    seen_block_ids: set[str] = set()

    for patch in block_patches:
        if not isinstance(patch, dict):
            failures.append(_failure(REASON_MALFORMED_SEGMENTS, "patch is not an object"))
            continue
        unknown = sorted(set(patch) - _PATCH_KEYS)
        if unknown:
            failures.append(
                _failure(
                    REASON_MALFORMED_SEGMENTS,
                    f"patch carries unknown key(s) {unknown}",
                    block_id=patch.get("block_id"),
                )
            )
            continue
        block_id = patch.get("block_id")
        if not isinstance(block_id, str) or block_id not in block_map:
            failures.append(
                _failure(
                    REASON_UNKNOWN_BLOCK_ID,
                    f"block_id {block_id!r} is not in the block map",
                    block_id=block_id,
                )
            )
            continue
        if block_id in seen_block_ids:
            failures.append(
                _failure(
                    REASON_DUPLICATE_BLOCK_ID,
                    (
                        f"block_id {block_id!r} appears in more than one patch; every "
                        "edit to one block belongs in that block's single entry"
                    ),
                    block_id=block_id,
                )
            )
            continue
        seen_block_ids.add(block_id)

        block = block_map[block_id]
        proved = _prove_patch(patch, block)
        if "failure" in proved:
            failure = dict(proved["failure"])
            failure["block_id"] = block_id
            failures.append(failure)
            continue
        proven_blocks.append(
            {
                "block_id": block_id,
                "index": block.get("index"),
                "heading": block.get("heading", "<untitled>"),
                "source_text": block.get("text", "") or "",
                "ops": proved["ops"],
                "final_text": proved["final_text"],
            }
        )

    normalized_block_ops: list[dict[str, Any]] = []
    deleted_block_ids: set[str] = set()
    for block_op in block_ops:
        normalized, failure = _validate_block_op(block_op, block_map)
        if failure is not None:
            failures.append(failure)
            continue
        if normalized is None:  # unreachable: a None op always carries a failure
            continue
        if normalized["op"] == OP_DELETE_BLOCK:
            target = normalized["block_id"]
            if target in deleted_block_ids:
                failures.append(
                    _failure(
                        REASON_CONFLICTING_BLOCK_OP,
                        f"block_id {target!r} is deleted more than once",
                        block_id=target,
                    )
                )
                continue
            if target in seen_block_ids:
                failures.append(
                    _failure(
                        REASON_CONFLICTING_BLOCK_OP,
                        (
                            f"block_id {target!r} is deleted wholesale AND carries a "
                            "segment patch; the two cannot both be applied"
                        ),
                        block_id=target,
                    )
                )
                continue
            deleted_block_ids.add(target)
        normalized_block_ops.append(normalized)

    if failures:
        return _rejected(failures)

    proven_blocks.sort(key=lambda entry: (entry["index"] is None, entry["index"]))
    return {
        "status": "proven",
        "blocks": proven_blocks,
        "block_ops": normalized_block_ops,
        "by_issue": _group_by_issue(proven_blocks, normalized_block_ops),
        "failures": [],
    }


def _rejected(failures: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "rejected",
        "blocks": [],
        "block_ops": [],
        "by_issue": {},
        "failures": failures,
    }


def _group_by_issue(
    proven_blocks: list[dict[str, Any]], normalized_block_ops: list[dict[str, Any]]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Every proven edit, grouped under the `issue_key` that authored it --
    segment edits and whole-block ops alike, so a caller can render or drop
    one issue's whole contribution without re-deriving which ops were its."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def bucket(issue_key: str) -> dict[str, list[dict[str, Any]]]:
        return grouped.setdefault(issue_key, {"segment_edits": [], "block_ops": []})

    for block in proven_blocks:
        for op in block["ops"]:
            issue_key = op.get("issue_key")
            if issue_key is None:  # a keep is not an edit and has no author
                continue
            entry = dict(op)
            entry["block_id"] = block["block_id"]
            bucket(issue_key)["segment_edits"].append(entry)
    for block_op in normalized_block_ops:
        bucket(block_op["issue_key"])["block_ops"].append(dict(block_op))
    return grouped
