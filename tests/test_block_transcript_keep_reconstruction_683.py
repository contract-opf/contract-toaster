"""Issue #683: `keep` segments are reconstructed from source, not proved verbatim.

WHY, measured. A real counterparty-paper affiliation agreement failed 3 live runs
in 5 with:

    block_transcript_rejected: [source_mismatch] block p0004:
    segment 3 diverges from the block's own text at folded offset 4029

Characterised offline: stage-1 normalisation merged SIX physical paragraphs into
one 4,048-character block, and the model's transcription differed in the last 19
characters -- a short trailing structural marker. The fold was 1:1 on that block
(raw 4,048 == folded 4,048), so this was never a folding problem.

A `keep` span is, by definition, text the system already holds unchanged. Making
the model retype 4KB of it verbatim adds a failure mode and buys nothing. Deletes
are the real anchors: they are what the model is asserting about the source.

NO EXISTING FIXTURE CAN REPRODUCE THIS. Every other v3 fixture slices its
segments straight out of `build_block_map`, so it transcribes perfectly by
construction. This file builds a transcript that genuinely diverges.

Synthetic throughout -- invented text, no document content.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import block_transcript as bt  # noqa: E402

from test_block_transcript import _block_map, _delete, _insert, _keep, _patch  # noqa: E402

# A 4KB block ending in a short structural marker -- the p0004 shape.
_SENTENCE = (
    "The Institution shall ensure that each Participant maintains the records "
    "required under this Program and produces them upon reasonable request. "
)
_DELETE_PHRASE = "sixty (60) days"
_PREFIX = _SENTENCE * 28
_SUFFIX = " and the remainder of the term shall continue unaffected."
_MARKER = " [End of Agreement]"          # 19 characters, as in the real failure
BIG_BLOCK = f"{_PREFIX}within {_DELETE_PHRASE}{_SUFFIX}{_MARKER}"


class TestKeepReconstruction(unittest.TestCase):
    def test_the_block_is_the_shape_that_failed(self) -> None:
        """Guard the fixture itself: if it stops being large, or the marker
        stops being 19 chars, this file no longer tests what it claims."""
        self.assertGreater(len(BIG_BLOCK), 4000)
        self.assertEqual(len(_MARKER), 19)
        self.assertEqual(BIG_BLOCK.count(_DELETE_PHRASE), 1)

    def test_a_keep_that_drops_the_trailing_marker_still_proves(self) -> None:
        """THE REGRESSION. The final keep omits the last 19 characters --
        exactly the real divergence. The delete is locatable and in order, so
        the keeps are reconstructed from source and the transcript proves."""
        trailing_keep = _SUFFIX + _MARKER
        result = bt.validate_block_patches(
            [
                _patch(
                    "b0001",
                    _keep(f"{_PREFIX}within "),
                    _delete(_DELETE_PHRASE, "TERM-1"),
                    _insert("thirty (30) days", "TERM-1"),
                    _keep(trailing_keep[: -len(_MARKER)]),   # marker dropped
                )
            ],
            [],
            _block_map(b0001=BIG_BLOCK),
        )
        self.assertEqual(result["status"], "proven", result)

        block = result["blocks"][0]
        deletion = next(op for op in block["ops"] if op["op"] == "delete")
        self.assertEqual(BIG_BLOCK[deletion["start"]:deletion["end"]], _DELETE_PHRASE)
        # every character of the block is still accounted for -- the dropped
        # marker is RESTORED from source, not silently lost from the redline
        self.assertTrue(block["final_text"].endswith(_MARKER))

    def test_an_unlocatable_delete_is_still_rejected(self) -> None:
        """The proof must not become permissive: a delete naming text the
        block does not contain is a fabricated edit and still fails, in the
        same `source_mismatch` shape #670 has to surface."""
        result = bt.validate_block_patches(
            [
                _patch(
                    "b0001",
                    _keep(f"{_PREFIX}within "),
                    _delete("ninety (90) years", "TERM-1"),
                    _insert("thirty (30) days", "TERM-1"),
                    _keep(_SUFFIX + _MARKER),
                )
            ],
            [],
            _block_map(b0001=BIG_BLOCK),
        )
        self.assertEqual(result["status"], "rejected")
        reasons = {f.get("reason") for f in result.get("failures", [])}
        self.assertIn("source_mismatch", reasons)


if __name__ == "__main__":
    unittest.main()
