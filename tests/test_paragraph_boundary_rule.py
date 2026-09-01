#!/usr/bin/env python3
"""
Regression guard: the paragraph-boundary rule in
`primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK` is LOAD-BEARING and must
not be "corrected" away.

## Why this file exists

Under v2 the rule was addressed to a verbatim quote: "a newline in the
document text you were shown marks a paragraph boundary; the quote MUST NOT
cross one." Once `review_spine.document_text_for_review` began rendering
headings, that sentence LOOKED wrong -- a heading and its body sit in one
block separated by a single "\n", and `normalize_paragraphs` joins sibling
physical <w:p> runs inside one logical paragraph with "\n" too (issue #564),
while blocks are separated by "\n\n". The obvious-looking fix was to relax
the rule to "a BLANK line separates clauses, crossing a single line break is
fine."

That fix is WRONG, and silently destructive, because the WRITER edits
PHYSICAL paragraphs: an edit span that crosses a single "\n" inside one
logical block cannot be written as a tracked change. Measured on the real
target affiliation agreement: 11 of its 30 logical paragraphs contain an
internal "\n". Relaxing the rule would have cost a redline on every issue
raised against any of them.

Issue #627 moved the CONSTRAINT off the quote sentence and onto the
block-transcript rules that carry it (see `TestRuleStillStated`'s own
docstring), and issue #628 deleted the locator whose behaviour used to be
this file's ground truth. The writer-level hazard is unchanged and is
covered where it now lives -- a `delete` span crossing a physical `<w:p>`
join inside one logical block compiles to `spans_physical_paragraph`
(`tests/test_document_shapes.py`'s split-paragraphs case and
`tests/test_block_mode_e2e.py`'s change-set atomicity test). What remains
here is the PROMPT half: the sentences that stop a model from writing a
transcript the writer cannot honour.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import primary_review_pass  # noqa: E402

class TestRuleStillStated(unittest.TestCase):
    """Issue #627 replaced the SENTENCE, not the constraint.

    Under v2 the rule was addressed to a verbatim quote: do not let one cross
    a paragraph boundary, because the locator could not anchor it. Under the
    block-transcript contract the model writes no quote -- so the same
    constraint is now carried by two other statements, and those are what
    must not be "corrected" away:

      * ONE block_patch entry per block_id, so an edit cannot straddle two
        paragraphs by construction; and
      * a block's segments TRANSCRIBE THE WHOLE PARAGRAPH, which is what
        stops a model from writing one entry that runs on past the block's
        end.

    The writer-level hazard is unchanged and still enforced -- a `delete`
    span crossing a physical `<w:p>` join inside one logical block compiles
    to `spans_physical_paragraph` (see tests/test_document_shapes.py's
    split-paragraphs case and tests/test_block_mode_e2e.py's change-set
    atomicity test).
    """

    RULE = primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK

    def test_the_one_entry_per_block_rule_is_present(self) -> None:
        flat = " ".join(self.RULE.split())
        self.assertIn("EXACTLY ONE entry per block_id", flat)

    def test_the_whole_paragraph_transcription_rule_is_present(self) -> None:
        flat = " ".join(self.RULE.split())
        self.assertIn("TRANSCRIBE THE WHOLE PARAGRAPH", flat)
        self.assertIn("start to finish", flat)

    def test_the_rule_was_not_relaxed(self) -> None:
        """Guards the wrong edit in its v3 form: telling the model it may
        write two entries for one block, or transcribe only the part it is
        changing. Either would produce transcripts that cannot prove."""
        flat = " ".join(self.RULE.split()).lower()
        self.assertNotIn("more than one entry per block", flat)
        self.assertNotIn("transcribe only the part", flat)

    def test_the_marker_rules_still_stand_beside_it(self) -> None:
        flat = " ".join(self.RULE.split())
        self.assertIn('NEVER copy the "[pNNNN]" markers into any output field', flat)
        self.assertIn('"## " marker', flat)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
