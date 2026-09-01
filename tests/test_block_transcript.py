#!/usr/bin/env python3
"""
Slice test (TDD) for issue #620: the shared typographic-fold module
(`scripts/text_fold.py`) and the block-transcript validator
(`scripts/block_transcript.py`).

## What this proves

Candidate E hands the model the edit boundaries: it transcribes a paragraph
as an ordered `keep`/`delete`/`insert` segment list, and CODE proves that
transcript back against the document's own bytes. This file is the gate on
that proof:

  1. A byte-exact transcript validates, and every returned offset indexes
     the block's REAL text (slice it back out and compare).
  2. A transcript the model straightened -- curly apostrophes and an em dash
     turned into ASCII, a whitespace run collapsed -- still realigns, and the
     proven offsets point at the DOCUMENT's own curly characters. Asserted
     against the actual code points, not just against a length.
  3. A transcript that silently altered ONE comma inside a `keep` is
     rejected with `source_mismatch`. This is the unmarked-rewrite guard: the
     fold tolerates how a character was ENCODED, never what it says. The
     paired assertion runs the same transcript with the comma restored and
     requires it to PROVE, so a validator that rejected everything could not
     pass this file.
  4. Two issues editing different spans of one paragraph live in ONE patch
     entry; a `delete` immediately followed by an `insert` round-trips as a
     replacement in `final_text`; whole-block ops validate against the map.
  5. Every structured rejection is exercised: `unknown_block_id`,
     `duplicate_block_id`, `malformed_segments`, `no_edit_segments`,
     `alignment_ambiguous`, and the block-op codes.
  6. The extracted fold's own invariants hold: a 1:1 fold table (so the
     index map back onto real offsets is sound) and an exact index map over
     a mixed whitespace/typographic input.

## Fixtures

Synthetic only. Most cases are hand-built `block_map` dicts (the module is
pure logic -- no OOXML, no I/O). One end-to-end case manufactures a document
in-process from `tools/churn_docx.py`'s synthetic base contracts and its
`curly_punctuation` transform, normalizes it, and builds a real block map
with `extraction_normalization_stage.build_block_map()` -- so the curly-
punctuation claim is proven against text a real extractor produced, not
against a string this file typed. Nothing is written to disk; no real party
names, no counterparty paper.

Run with: python3 tests/test_block_transcript.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOOLS_DIR = REPO_ROOT / "tools"

for _dir in (SCRIPTS_DIR, TOOLS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript as bt  # noqa: E402
import churn_docx as cd  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import text_fold  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

# Straight-quoted plain text: the simple case, where a byte-exact transcript
# is byte-exact all the way down.
PLAIN_BLOCK = "The Term shall be sixty (60) days from the Effective Date."

# The same clause as real paper carries it: Word's curly apostrophe, curly
# double quotes, and an em dash. This is the text a model reliably
# ASCII-folds when it copies it back out.
CURLY_BLOCK = (
    "Each party’s obligations — including the “Confidentiality” "
    "covenant — survive for sixty (60) days."
)


def _block_map(**blocks: str) -> dict[str, dict[str, Any]]:
    """A block map with the same field shape `build_block_map()` produces."""
    return {
        block_id: {
            "text": text,
            "heading": "Section 1",
            "physical_spans": [[0, len(text)]],
            "index": index,
        }
        for index, (block_id, text) in enumerate(blocks.items())
    }


def _keep(text: str) -> dict[str, Any]:
    return {"op": "keep", "text": text}


def _delete(text: str, issue_key: str) -> dict[str, Any]:
    return {"op": "delete", "text": text, "issue_key": issue_key}


def _insert(text: str, issue_key: str) -> dict[str, Any]:
    return {"op": "insert", "text": text, "issue_key": issue_key}


def _patch(block_id: str, *segments: dict[str, Any]) -> dict[str, Any]:
    return {"block_id": block_id, "segments": list(segments)}


def _reasons(result: dict[str, Any]) -> list[str]:
    return [failure["reason"] for failure in result["failures"]]


def _expect_rejected(
    failures: list[str], label: str, result: dict[str, Any], reason: str
) -> dict[str, Any] | None:
    if result["status"] != "rejected":
        failures.append(f"[{label}] expected status 'rejected', got {result['status']!r}")
        return None
    if reason not in _reasons(result):
        failures.append(f"[{label}] expected reason {reason!r}, got {_reasons(result)!r}")
        return None
    if result["blocks"] or result["block_ops"] or result["by_issue"]:
        failures.append(
            f"[{label}] a rejected transcript must carry no proven output, got "
            f"{len(result['blocks'])} block(s), {len(result['block_ops'])} block op(s)"
        )
    return next(f for f in result["failures"] if f["reason"] == reason)


def _expect_proven(
    failures: list[str], label: str, result: dict[str, Any]
) -> dict[str, Any] | None:
    if result["status"] != "proven":
        failures.append(
            f"[{label}] expected status 'proven', got {result['status']!r} "
            f"({result['failures']!r})"
        )
        return None
    return result


def _check_offsets_index_real_text(
    failures: list[str], label: str, result: dict[str, Any], block_map: dict[str, Any]
) -> None:
    """Every keep/delete op's `text` must be exactly the slice its offsets
    name in the block's REAL text, and the ops must tile that text."""
    for block in result["blocks"]:
        source = block_map[block["block_id"]]["text"]
        if block["source_text"] != source:
            failures.append(f"[{label}] source_text is not the block's own text")
        cursor = 0
        for op in block["ops"]:
            if op["op"] == "insert":
                if not (0 <= op["at"] <= len(source)):
                    failures.append(f"[{label}] insert anchor {op['at']} is out of range")
                continue
            if op["start"] != cursor:
                failures.append(
                    f"[{label}] {op['op']} starts at {op['start']}, expected {cursor} "
                    "(ops must tile the block with no gap and no overlap)"
                )
            if source[op["start"] : op["end"]] != op["text"]:
                failures.append(
                    f"[{label}] {op['op']} offsets {op['start']}..{op['end']} name "
                    f"{source[op['start']:op['end']]!r}, but the op carries {op['text']!r}"
                )
            cursor = op["end"]
        if cursor != len(source):
            failures.append(
                f"[{label}] ops stop at {cursor} but the block is {len(source)} chars long"
            )


# ---------------------------------------------------------------------------
# Criterion 1: a byte-exact transcript validates against the true block text
# ---------------------------------------------------------------------------


def test_byte_exact_transcript_proves_with_true_offsets(failures: list[str]) -> None:
    block_map = _block_map(b0001=PLAIN_BLOCK)
    result = bt.validate_block_patches(
        [
            _patch(
                "b0001",
                _keep("The Term shall be "),
                _delete("sixty (60)", "TERM-1"),
                _insert("thirty (30)", "TERM-1"),
                _keep(" days from the Effective Date."),
            )
        ],
        [],
        block_map,
    )
    if _expect_proven(failures, "byte_exact", result) is None:
        return
    _check_offsets_index_real_text(failures, "byte_exact", result, block_map)

    block = result["blocks"][0]
    deletion = next(op for op in block["ops"] if op["op"] == "delete")
    if PLAIN_BLOCK[deletion["start"] : deletion["end"]] != "sixty (60)":
        failures.append(
            f"[byte_exact] the delete span names "
            f"{PLAIN_BLOCK[deletion['start']:deletion['end']]!r}, not 'sixty (60)'"
        )
    expected_final = "The Term shall be thirty (30) days from the Effective Date."
    if block["final_text"] != expected_final:
        failures.append(f"[byte_exact] final_text is {block['final_text']!r}")
    if block["index"] != 0 or block["block_id"] != "b0001":
        failures.append("[byte_exact] the proven block lost its map identity")


def test_a_keep_only_transcript_is_not_an_edit(failures: list[str]) -> None:
    result = bt.validate_block_patches(
        [_patch("b0001", _keep(PLAIN_BLOCK))], [], _block_map(b0001=PLAIN_BLOCK)
    )
    _expect_rejected(failures, "all_keep", result, bt.REASON_NO_EDIT_SEGMENTS)


# ---------------------------------------------------------------------------
# Criterion 2: transcription noise realigns; the offsets stay on the
# document's own characters
# ---------------------------------------------------------------------------


def test_straightened_punctuation_and_collapsed_whitespace_realign(
    failures: list[str],
) -> None:
    """The model straightens Word's curly punctuation and collapses a
    whitespace run when it copies text back out. Both must realign -- and the
    proven offsets must still land on the DOCUMENT's curly characters, which
    is what makes the eventual redline quote the paper rather than the
    model's transcription of it."""
    # The document's own text carries a tab and a newline (a multi-`<w:p>`
    # logical-paragraph join) on top of the curly punctuation.
    block_text = CURLY_BLOCK.replace("obligations", "obligations\t", 1).replace(
        "covenant", "covenant\n", 1
    )
    block_map = _block_map(b0002=block_text)

    # Everything the model wrote back is ASCII, and every whitespace run is a
    # single space.
    result = bt.validate_block_patches(
        [
            _patch(
                "b0002",
                _keep("Each party's obligations -- including the \"Confidentiality\" covenant"),
                _delete(" -- survive for sixty (60) days.", "CONF-1"),
                _insert(" survive indefinitely.", "CONF-1"),
            )
        ],
        [],
        block_map,
    )
    # An em dash folds to ONE hyphen, so the model's "--" is a real
    # divergence, not noise: prove the guard notices before fixing it.
    mismatch = _expect_rejected(
        failures, "double_hyphen", result, bt.REASON_SOURCE_MISMATCH
    )
    if mismatch is not None and "block_context" not in mismatch:
        failures.append("[double_hyphen] source_mismatch carries no block_context")

    result = bt.validate_block_patches(
        [
            _patch(
                "b0002",
                _keep("Each party's obligations - including the \"Confidentiality\" covenant"),
                _delete(" - survive for sixty (60) days.", "CONF-1"),
                _insert(" survive indefinitely.", "CONF-1"),
            )
        ],
        [],
        block_map,
    )
    if _expect_proven(failures, "straightened", result) is None:
        return
    _check_offsets_index_real_text(failures, "straightened", result, block_map)

    block = result["blocks"][0]
    kept = next(op for op in block["ops"] if op["op"] == "keep")
    kept_slice = block_text[kept["start"] : kept["end"]]
    for label, codepoint in (
        ("RIGHT SINGLE QUOTATION MARK", "’"),
        ("LEFT DOUBLE QUOTATION MARK", "“"),
        ("RIGHT DOUBLE QUOTATION MARK", "”"),
        ("EM DASH", "—"),
        ("TAB", "\t"),
    ):
        if codepoint not in kept_slice:
            failures.append(
                f"[straightened] the proven keep span dropped the document's own "
                f"{label} (U+{ord(codepoint):04X}); it reads {kept_slice!r}"
            )
    if kept_slice != kept["text"]:
        failures.append("[straightened] the keep op's text is not the document's own slice")

    deletion = next(op for op in block["ops"] if op["op"] == "delete")
    deleted_slice = block_text[deletion["start"] : deletion["end"]]
    # The `"\n"` that joins two sibling `<w:p>`s is elastic whitespace, so the
    # model's single space stands in for the whole `"\n "` run -- and the
    # proven span still covers that run's real extent, not just its first
    # character.
    for label, codepoint in (("EM DASH", "—"), ("NEWLINE", "\n")):
        if codepoint not in deleted_slice:
            failures.append(
                "[straightened] the proven delete span does not cover the document's "
                f"own {label} (U+{ord(codepoint):04X}); it reads {deleted_slice!r}"
            )
    if block["final_text"] != kept_slice + " survive indefinitely.":
        failures.append(f"[straightened] final_text is {block['final_text']!r}")


def test_a_whitespace_run_split_across_a_segment_boundary_realigns(
    failures: list[str],
) -> None:
    """The model split one whitespace run across a boundary and kept a space
    on BOTH sides. That is one run in the document, so the boundary is still
    unambiguous and the block's characters must still tile exactly once."""
    block_text = "Payment is due  within thirty days."  # two spaces after "due"
    block_map = _block_map(b0003=block_text)
    result = bt.validate_block_patches(
        [
            _patch(
                "b0003",
                _keep("Payment is due "),
                _delete(" within thirty days.", "PAY-1"),
                _insert(" upon receipt.", "PAY-1"),
            )
        ],
        [],
        block_map,
    )
    if _expect_proven(failures, "split_run", result) is None:
        return
    _check_offsets_index_real_text(failures, "split_run", result, block_map)


def test_a_whitespace_only_delete_never_aligns_to_zero_characters(
    failures: list[str],
) -> None:
    """The split-run tolerance above must never absorb a whole segment.

    If a `delete` folds to exactly one space and the preceding segment
    already consumed that whitespace run, treating it as a re-transcription
    would align it to ZERO characters: a zero-width `delete` op reported as
    `proven` while the model's deletion is silently discarded. That is a
    repair, so it fails closed instead.

    The control below is the shape this must NOT break: the same block, with
    the deletion claiming the whole run, still proves and still deletes both
    of the document's real spaces.
    """
    block_text = "Alpha  Beta."  # two spaces between the words
    block_map = _block_map(b0016=block_text)

    absorbed = bt.validate_block_patches(
        [_patch("b0016", _keep("Alpha "), _delete(" ", "WS-1"), _keep("Beta."))],
        [],
        block_map,
    )
    _expect_rejected(failures, "absorbed_space", absorbed, bt.REASON_ALIGNMENT_AMBIGUOUS)

    genuine = bt.validate_block_patches(
        [_patch("b0016", _keep("Alpha"), _delete("  ", "WS-1"), _keep("Beta."))],
        [],
        block_map,
    )
    if _expect_proven(failures, "genuine_space", genuine) is None:
        return
    _check_offsets_index_real_text(failures, "genuine_space", genuine, block_map)
    block = genuine["blocks"][0]
    deletion = next(op for op in block["ops"] if op["op"] == "delete")
    if (deletion["start"], deletion["end"], deletion["text"]) != (5, 7, "  "):
        failures.append(
            f"[genuine_space] the delete spans {deletion['start']}..{deletion['end']} "
            f"({deletion['text']!r}), not both of the document's real spaces"
        )
    if block["final_text"] != "AlphaBeta.":
        failures.append(f"[genuine_space] final_text is {block['final_text']!r}")


# ---------------------------------------------------------------------------
# Criterion 3: the unmarked-rewrite guard
# ---------------------------------------------------------------------------


def test_one_altered_comma_inside_a_keep_is_rejected(failures: list[str]) -> None:
    """A `keep` asserts "this text is unchanged". If the model quietly
    rewrote it, that is an edit nobody marked, nobody attributed to an issue,
    and nobody would see in a redline. The fold must NOT absorb it.

    The honest half of this test is the control below: the same transcript
    with the punctuation restored must PROVE, so a validator that simply
    rejected everything cannot pass.
    """
    block_text = "If, and only if, the Fee is paid, the license shall vest."
    block_map = _block_map(b0004=block_text)

    control = bt.validate_block_patches(
        [
            _patch(
                "b0004",
                _keep("If, and only if, the Fee is paid, "),
                _delete("the license shall vest.", "LIC-1"),
                _insert("the license vests immediately.", "LIC-1"),
            )
        ],
        [],
        block_map,
    )
    if _expect_proven(failures, "comma_control", control) is None:
        return

    tampered = bt.validate_block_patches(
        [
            _patch(
                "b0004",
                # One comma removed after "only if" -- a semantic rewrite of a
                # span the model swore was untouched.
                _keep("If, and only if the Fee is paid, "),
                _delete("the license shall vest.", "LIC-1"),
                _insert("the license vests immediately.", "LIC-1"),
            )
        ],
        [],
        block_map,
    )
    failure = _expect_rejected(failures, "comma_tampered", tampered, bt.REASON_SOURCE_MISMATCH)
    if failure is None:
        return
    for key in ("divergence_offset", "block_context", "transcript_context"):
        if key not in failure:
            failures.append(f"[comma_tampered] source_mismatch is missing {key!r}")
    if failure.get("block_id") != "b0004":
        failures.append("[comma_tampered] the failure does not name the block it came from")
    if not failure.get("block_context", "").startswith(","):
        failures.append(
            "[comma_tampered] block_context should start at the divergence (the "
            f"comma), got {failure.get('block_context')!r}"
        )


# ---------------------------------------------------------------------------
# Criterion 4: multi-issue patches, replacement adjacency, whole-block ops
# ---------------------------------------------------------------------------


def test_two_issues_edit_one_block_in_one_patch_entry(failures: list[str]) -> None:
    block_text = "The Fee is sixty (60) dollars and the Term is twelve (12) months."
    block_map = _block_map(b0005=block_text)
    result = bt.validate_block_patches(
        [
            _patch(
                "b0005",
                _keep("The Fee is "),
                _delete("sixty (60)", "FEE-1"),
                _insert("forty (40)", "FEE-1"),
                _keep(" dollars and the Term is "),
                _delete("twelve (12)", "TERM-2"),
                _insert("six (6)", "TERM-2"),
                _keep(" months."),
            )
        ],
        [],
        block_map,
    )
    if _expect_proven(failures, "two_issues", result) is None:
        return
    _check_offsets_index_real_text(failures, "two_issues", result, block_map)

    block = result["blocks"][0]
    expected = "The Fee is forty (40) dollars and the Term is six (6) months."
    if block["final_text"] != expected:
        failures.append(f"[two_issues] final_text is {block['final_text']!r}")

    by_issue = result["by_issue"]
    if sorted(by_issue) != ["FEE-1", "TERM-2"]:
        failures.append(f"[two_issues] by_issue keys are {sorted(by_issue)!r}")
        return
    for issue_key, deleted in (("FEE-1", "sixty (60)"), ("TERM-2", "twelve (12)")):
        edits = by_issue[issue_key]["segment_edits"]
        if len(edits) != 2:
            failures.append(f"[two_issues] {issue_key} has {len(edits)} edit(s), expected 2")
            continue
        if edits[0]["op"] != "delete" or edits[0]["text"] != deleted:
            failures.append(f"[two_issues] {issue_key}'s delete is {edits[0]!r}")
        if edits[1]["op"] != "insert":
            failures.append(f"[two_issues] {issue_key}'s second edit is not an insert")
        if any(edit.get("block_id") != "b0005" for edit in edits):
            failures.append(f"[two_issues] {issue_key}'s edits do not name their block")


def test_delete_then_insert_round_trips_as_a_replacement(failures: list[str]) -> None:
    """A `delete` immediately followed by an `insert` IS the replacement
    primitive: the insert must anchor exactly where the delete ended, so a
    tracked-change compiler can emit one `w:del`+`w:ins` pair."""
    block_map = _block_map(b0006=PLAIN_BLOCK)
    result = bt.validate_block_patches(
        [
            _patch(
                "b0006",
                _keep("The Term shall be "),
                _delete("sixty (60)", "TERM-1"),
                _insert("thirty (30)", "TERM-1"),
                _keep(" days from the Effective Date."),
            )
        ],
        [],
        block_map,
    )
    if _expect_proven(failures, "replacement", result) is None:
        return
    ops = result["blocks"][0]["ops"]
    deletion = next(op for op in ops if op["op"] == "delete")
    insertion = next(op for op in ops if op["op"] == "insert")
    if insertion["at"] != deletion["end"]:
        failures.append(
            f"[replacement] insert anchors at {insertion['at']} but the delete ends "
            f"at {deletion['end']}"
        )
    if ops.index(insertion) != ops.index(deletion) + 1:
        failures.append("[replacement] the insert is not adjacent to the delete")


def test_whole_block_ops_validate_against_the_map(failures: list[str]) -> None:
    block_map = _block_map(b0007=PLAIN_BLOCK, b0008=CURLY_BLOCK)
    result = bt.validate_block_patches(
        [],
        [
            {"op": "delete_block", "block_id": "b0008", "issue_key": "IP-2"},
            {
                "op": "insert_block_after",
                "anchor_block_id": "b0007",
                "new_text": "Neither party may assign this Agreement.",
                "issue_key": "IP-2",
            },
            {
                "op": "insert_block_after",
                "anchor_block_id": bt.ANCHOR_START,
                "new_text": "PREAMBLE",
                "issue_key": "PRE-1",
            },
        ],
        block_map,
    )
    if _expect_proven(failures, "block_ops", result) is None:
        return
    ops = result["block_ops"]
    if len(ops) != 3:
        failures.append(f"[block_ops] {len(ops)} op(s) survived, expected 3")
        return
    if ops[0]["index"] != block_map["b0008"]["index"] or ops[0]["text"] != CURLY_BLOCK:
        failures.append("[block_ops] delete_block did not resolve against the map")
    if ops[1]["anchor_index"] != block_map["b0007"]["index"]:
        failures.append("[block_ops] insert_block_after did not resolve its anchor")
    if ops[2]["anchor_index"] != -1:
        failures.append(
            f"[block_ops] the {bt.ANCHOR_START!r} anchor resolved to "
            f"{ops[2]['anchor_index']!r}, expected -1 (before the first block)"
        )
    if sorted(result["by_issue"]) != ["IP-2", "PRE-1"]:
        failures.append(f"[block_ops] by_issue keys are {sorted(result['by_issue'])!r}")
        return
    if len(result["by_issue"]["IP-2"]["block_ops"]) != 2:
        failures.append("[block_ops] by_issue did not group both of IP-2's block ops")

    unknown = bt.validate_block_patches(
        [], [{"op": "delete_block", "block_id": "b9999", "issue_key": "IP-2"}], block_map
    )
    _expect_rejected(failures, "block_ops_unknown", unknown, bt.REASON_UNKNOWN_BLOCK_ID)

    contradiction = bt.validate_block_patches(
        [
            _patch(
                "b0007",
                _keep("The Term shall be "),
                _delete("sixty (60) days from the Effective Date.", "TERM-1"),
            )
        ],
        [{"op": "delete_block", "block_id": "b0007", "issue_key": "IP-2"}],
        block_map,
    )
    _expect_rejected(
        failures, "block_ops_conflict", contradiction, bt.REASON_CONFLICTING_BLOCK_OP
    )

    malformed = bt.validate_block_patches(
        [],
        [
            {
                "op": "insert_block_after",
                "anchor_block_id": "b0007",
                "new_text": "",
                "issue_key": "X",
            }
        ],
        block_map,
    )
    _expect_rejected(failures, "block_ops_malformed", malformed, bt.REASON_MALFORMED_BLOCK_OP)


# ---------------------------------------------------------------------------
# Criterion 5: every structured rejection, and no partial output
# ---------------------------------------------------------------------------


def test_unknown_and_duplicate_block_ids_are_rejected(failures: list[str]) -> None:
    block_map = _block_map(b0009=PLAIN_BLOCK)
    unknown = bt.validate_block_patches(
        [_patch("b4242", _keep("The Term shall be "), _delete("sixty (60)", "T-1"))],
        [],
        block_map,
    )
    failure = _expect_rejected(failures, "unknown_id", unknown, bt.REASON_UNKNOWN_BLOCK_ID)
    if failure is not None and failure.get("block_id") != "b4242":
        failures.append("[unknown_id] the failure does not name the offending id")

    duplicate = bt.validate_block_patches(
        [
            _patch(
                "b0009",
                _keep("The Term shall be "),
                _delete("sixty (60)", "T-1"),
                _keep(" days from the Effective Date."),
            ),
            _patch(
                "b0009",
                _keep("The Term shall be sixty (60) days from the "),
                _delete("Effective Date.", "T-2"),
            ),
        ],
        [],
        block_map,
    )
    _expect_rejected(failures, "duplicate_id", duplicate, bt.REASON_DUPLICATE_BLOCK_ID)


def test_malformed_segments_are_rejected(failures: list[str]) -> None:
    block_map = _block_map(b0010=PLAIN_BLOCK)
    cases: dict[str, list[dict[str, Any]]] = {
        "empty_list": [],
        "empty_text": [{"op": "delete", "text": "", "issue_key": "T-1"}],
        "unknown_op": [{"op": "replace", "text": "x", "issue_key": "T-1"}],
        "keep_with_issue_key": [
            {"op": "keep", "text": "The Term shall be ", "issue_key": "T-1"},
            _delete("sixty (60)", "T-1"),
        ],
        "delete_without_issue_key": [
            _keep("The Term shall be "),
            {"op": "delete", "text": "sixty (60)"},
        ],
        "insert_without_issue_key": [
            _keep("The Term shall be "),
            {"op": "insert", "text": "thirty (30)"},
        ],
        "misspelled_key": [
            _keep("The Term shall be "),
            {"op": "delete", "text": "sixty (60)", "issueKey": "T-1"},
        ],
        "consecutive_keeps": [
            _keep("The Term "),
            _keep("shall be "),
            _delete("sixty (60)", "T-1"),
        ],
        "one_edit_split_in_two": [
            _keep("The Term shall be "),
            _delete("sixty ", "T-1"),
            _delete("(60)", "T-1"),
        ],
    }
    for label, segments in cases.items():
        result = bt.validate_block_patches(
            [{"block_id": "b0010", "segments": segments}], [], block_map
        )
        _expect_rejected(failures, f"malformed/{label}", result, bt.REASON_MALFORMED_SEGMENTS)

    # Two DIFFERENT issues deleting adjacent spans is legitimate, not a
    # malformed split -- the adjacency rule must not swallow it.
    legitimate = bt.validate_block_patches(
        [
            _patch(
                "b0010",
                _keep("The Term shall be "),
                _delete("sixty ", "T-1"),
                _delete("(60)", "T-2"),
                _keep(" days from the Effective Date."),
            )
        ],
        [],
        block_map,
    )
    _expect_proven(failures, "adjacent_deletes_two_issues", legitimate)


def test_omitted_boundary_whitespace_fails_closed(failures: list[str]) -> None:
    """Whitespace the transcript never claimed, sitting between a keep and a
    delete, decides whether that space survives the redline. The validator
    must not decide it."""
    block_map = _block_map(b0011=PLAIN_BLOCK)
    result = bt.validate_block_patches(
        [
            _patch(
                "b0011",
                # The space between "be" and "sixty" belongs to neither segment.
                _keep("The Term shall be"),
                _delete("sixty (60)", "T-1"),
                _keep(" days from the Effective Date."),
            )
        ],
        [],
        block_map,
    )
    _expect_rejected(failures, "omitted_space", result, bt.REASON_ALIGNMENT_AMBIGUOUS)

    # An omission between two segments that AGREE on the disposition is not
    # ambiguous -- whitespace between two deletes is deleted either way -- so
    # it proves. The run lands in the PRECEDING segment, which is the one
    # place this module picks a side on the model's behalf; the pick is
    # invisible in `final_text` but decides `by_issue` attribution, so it is
    # pinned here. The gap between "sixty" and "(60)" is the ONLY unclaimed
    # whitespace in this transcript.
    agreeing_map = _block_map(b0012=PLAIN_BLOCK)
    agreeing = bt.validate_block_patches(
        [
            _patch(
                "b0012",
                _keep("The Term shall be "),
                _delete("sixty", "T-1"),
                _delete("(60)", "T-2"),
                _keep(" days from the Effective Date."),
            )
        ],
        [],
        agreeing_map,
    )
    if _expect_proven(failures, "omitted_space_agreeing", agreeing) is None:
        return
    _check_offsets_index_real_text(failures, "omitted_space_agreeing", agreeing, agreeing_map)
    attributed = [
        (op["issue_key"], op["start"], op["end"], op["text"])
        for op in agreeing["blocks"][0]["ops"]
        if op["op"] == "delete"
    ]
    if attributed != [("T-1", 18, 24, "sixty "), ("T-2", 24, 28, "(60)")]:
        failures.append(
            f"[omitted_space_agreeing] the unclaimed space did not land in the "
            f"preceding delete; the proven deletes are {attributed!r}"
        )


def test_a_transcript_that_does_not_cover_the_block_is_rejected(
    failures: list[str],
) -> None:
    block_map = _block_map(b0013=PLAIN_BLOCK)
    truncated = bt.validate_block_patches(
        [_patch("b0013", _keep("The Term shall be "), _delete("sixty (60)", "T-1"))],
        [],
        block_map,
    )
    _expect_rejected(failures, "truncated", truncated, bt.REASON_SOURCE_MISMATCH)

    overrun = bt.validate_block_patches(
        [
            _patch(
                "b0013",
                _keep("The Term shall be "),
                _delete("sixty (60)", "T-1"),
                _keep(" days from the Effective Date. Renewal is automatic."),
            )
        ],
        [],
        block_map,
    )
    _expect_rejected(failures, "overrun", overrun, bt.REASON_SOURCE_MISMATCH)


def test_one_bad_patch_rejects_the_whole_transcript(failures: list[str]) -> None:
    """No partial application: a redline assembled from the half that
    happened to prove is a redline nobody authored."""
    block_map = _block_map(b0014=PLAIN_BLOCK, b0015=PLAIN_BLOCK)
    result = bt.validate_block_patches(
        [
            _patch(
                "b0014",
                _keep("The Term shall be "),
                _delete("sixty (60)", "T-1"),
                _keep(" days from the Effective Date."),
            ),
            _patch("b9999", _keep("nothing"), _delete("here", "T-2")),
        ],
        [],
        block_map,
    )
    _expect_rejected(failures, "partial", result, bt.REASON_UNKNOWN_BLOCK_ID)


def test_bad_top_level_input_never_raises(failures: list[str]) -> None:
    for label, args in {
        "patches_not_a_list": ({"block_id": "b0001"}, [], _block_map(b0001=PLAIN_BLOCK)),
        "ops_not_a_list": ([], "delete everything", _block_map(b0001=PLAIN_BLOCK)),
        "map_not_a_mapping": ([], [], None),
        "patch_not_an_object": (["b0001"], [], _block_map(b0001=PLAIN_BLOCK)),
        "op_not_an_object": ([], ["delete_block"], _block_map(b0001=PLAIN_BLOCK)),
    }.items():
        try:
            result = bt.validate_block_patches(*args)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{label}] raised {type(exc).__name__}: {exc}")
            continue
        if result["status"] != "rejected" or not result["failures"]:
            failures.append(f"[{label}] expected a structured rejection, got {result!r}")
            continue
        for failure in result["failures"]:
            if failure["reason"] not in bt.REJECTION_REASONS:
                failures.append(f"[{label}] undeclared reason {failure['reason']!r}")

    empty = bt.validate_block_patches([], [], _block_map(b0001=PLAIN_BLOCK))
    if empty["status"] != "proven" or empty["blocks"] or empty["block_ops"]:
        failures.append(f"[empty] an empty transcript should prove trivially, got {empty!r}")


# ---------------------------------------------------------------------------
# Criterion 6: the extracted fold's own invariants
# ---------------------------------------------------------------------------


def test_the_extracted_fold_holds_its_invariants(failures: list[str]) -> None:
    """`text_fold` is the ONE comparison fold both this module and the
    document spine read. Issue #628 deleted its other historical caller, so
    the "the two callers share one object" check went with it -- what
    remains, and is what actually made that check worth having, is the fold
    table's own soundness: a fold that changed a character's WIDTH would
    silently break every offset `validate_block_patches` returns."""
    # U+2026 must stay out of the table: it would fold to three characters and
    # break the 1:1 invariant the index map depends on.
    if ord("…") in text_fold.TYPOGRAPHIC_FOLD:
        failures.append("[fold_extraction] U+2026 is in the fold table; the index map is unsound")
    for source, folded in text_fold.TYPOGRAPHIC_FOLD.items():
        if len(folded) != 1:
            failures.append(
                f"[fold_extraction] U+{source:04X} folds to {folded!r} (not 1 character)"
            )

    folded, starts, ends = text_fold.fold_text_with_map("a \t\n b’c")
    if folded != "a b'c":
        failures.append(f"[fold_extraction] folded form is {folded!r}")
    elif (starts, ends) != ([0, 1, 5, 6, 7], [1, 5, 6, 7, 8]):
        failures.append(f"[fold_extraction] index map is {(starts, ends)!r}")


# ---------------------------------------------------------------------------
# End to end over a real extractor's output
# ---------------------------------------------------------------------------


def test_against_a_real_block_map_from_a_churned_document(failures: list[str]) -> None:
    """The curly-punctuation claim, proven against text a real extractor
    produced. The document is manufactured in-process from `churn_docx.py`'s
    synthetic base contract plus its `curly_punctuation` transform -- the
    same convention `tests/test_block_ir.py` uses -- so nothing here is a
    string this file typed and then asserted about."""
    docx_bytes = cd.apply_transform(
        cd.build_base_document("mutual-nda"), "curly_punctuation", seed=0
    )
    normalized = ens.extract_and_normalize(docx_bytes)
    if normalized.get("status") != "normalized":
        failures.append(f"[real_map] fixture did not normalize: {normalized.get('status')!r}")
        return
    block_map = ens.build_block_map(normalized["paragraphs"])

    curly = [
        (block_id, block["text"])
        for block_id, block in block_map.items()
        if any(ord(ch) in text_fold.TYPOGRAPHIC_FOLD for ch in block["text"])
        and len(block["text"]) > 20
    ]
    if not curly:
        failures.append("[real_map] the churned document carries no curly punctuation")
        return
    block_id, block_text = curly[0]

    # Transcribe the block the way a model does: ASCII-folded, whitespace
    # runs collapsed. Split it so the tail is deleted and replaced.
    straightened = text_fold.fold_text_with_map(block_text)[0]
    cut = straightened.rindex(" ")
    result = bt.validate_block_patches(
        [
            _patch(
                block_id,
                _keep(straightened[:cut]),
                _delete(straightened[cut:], "NDA-1"),
                _insert(" thereafter.", "NDA-1"),
            )
        ],
        [],
        block_map,
    )
    if _expect_proven(failures, "real_map", result) is None:
        return
    _check_offsets_index_real_text(failures, "real_map", result, block_map)

    block = result["blocks"][0]
    kept = next(op for op in block["ops"] if op["op"] == "keep")
    if not any(ord(ch) in text_fold.TYPOGRAPHIC_FOLD for ch in kept["text"]):
        failures.append(
            "[real_map] the proven keep span carries no curly punctuation, so the "
            "offsets did not land on the document's own characters"
        )
    if kept["text"] != block_text[kept["start"] : kept["end"]]:
        failures.append("[real_map] the keep op is not the document's own slice")
    if block["final_text"] != block_text[kept["start"] : kept["end"]] + " thereafter.":
        failures.append(f"[real_map] final_text is {block['final_text']!r}")


TESTS = [
    test_byte_exact_transcript_proves_with_true_offsets,
    test_a_keep_only_transcript_is_not_an_edit,
    test_straightened_punctuation_and_collapsed_whitespace_realign,
    test_a_whitespace_run_split_across_a_segment_boundary_realigns,
    test_a_whitespace_only_delete_never_aligns_to_zero_characters,
    test_one_altered_comma_inside_a_keep_is_rejected,
    test_two_issues_edit_one_block_in_one_patch_entry,
    test_delete_then_insert_round_trips_as_a_replacement,
    test_whole_block_ops_validate_against_the_map,
    test_unknown_and_duplicate_block_ids_are_rejected,
    test_malformed_segments_are_rejected,
    test_omitted_boundary_whitespace_fails_closed,
    test_a_transcript_that_does_not_cover_the_block_is_rejected,
    test_one_bad_patch_rejects_the_whole_transcript,
    test_bad_top_level_input_never_raises,
    test_the_extracted_fold_holds_its_invariants,
    test_against_a_real_block_map_from_a_churned_document,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        print(("PASS: " if len(failures) == before else "FAIL: ") + test.__name__)

    if failures:
        print()
        for failure in failures:
            print(f"  - {failure}")
        print(f"\nFAIL: {len(failures)} issue(s) found.")
        return 1
    print(
        "\nPASS: block transcripts prove against the document's own bytes, and "
        "fail closed otherwise (issue #620)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
