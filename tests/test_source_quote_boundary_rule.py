#!/usr/bin/env python3
"""
Regression guard: the `source_quote` newline rule in
`primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK` is LOAD-BEARING and must
not be "corrected" away.

## Why this file exists

The rule tells the model: "A newline in the document text you were shown
marks a paragraph boundary; source_quote MUST NOT cross one." Once
`review_spine.document_text_for_review` began rendering headings, that
sentence LOOKS wrong -- a heading and its body sit in one block separated by
a single "\n", and `normalize_paragraphs` joins sibling physical <w:p> runs
inside one logical paragraph with "\n" too (issue #564), while blocks are
separated by "\n\n". The obvious-looking fix is to relax the rule to "a
BLANK line separates clauses, quoting across a single line break is fine."

That fix is WRONG, and silently destructive. `quote_locate` anchors a span
only when it fits inside ONE PHYSICAL <w:p>; a quote crossing a single "\n"
returns `spans_paragraph_break`, which `redline_quote_apply` treats exactly
like `not_found` -- the finding survives, the tracked change does not. The
re-quote repair pass lists it in ELIGIBLE_REASONS but ships env-flagged OFF,
so there is no backstop.

Measured on the real target affiliation agreement: 11 of its 30 logical
paragraphs contain an internal "\n". Relaxing the rule would have cost a
redline on every issue raised against any of them.

So these tests pin the rule to the LOCATOR'S ACTUAL BEHAVIOUR rather than to
the renderer's cosmetics: the prose stays because the locator says it must.

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
import quote_locate  # noqa: E402
import review_spine  # noqa: E402

# One logical paragraph whose own text runs across a single line break --
# the shape 11 of the real document's 30 paragraphs have.
PARAGRAPHS = [
    {
        "heading": "Indemnification",
        "text": "First line of the clause.\nSecond line of the same clause.",
        "physical_spans": [[0, 25], [26, 57]],
    },
]


class TestLocatorGroundTruth(unittest.TestCase):
    """The facts the prompt rule exists to respect."""

    def test_a_quote_inside_one_line_is_locatable(self) -> None:
        result = quote_locate.locate_quote_in_paragraphs(PARAGRAPHS, "First line of the clause.")
        self.assertEqual(result["status"], "found")

    def test_a_quote_crossing_a_single_newline_is_NOT_locatable(self) -> None:
        """The whole justification for the rule. If this ever starts
        returning `found`, the prompt rule may be relaxed -- and not before."""
        result = quote_locate.locate_quote_in_paragraphs(
            PARAGRAPHS, "the clause.\nSecond line of the same"
        )
        self.assertEqual(result["status"], quote_locate.REASON_SPANS_PARAGRAPH_BREAK)

    def test_that_status_costs_the_redline(self) -> None:
        """`spans_paragraph_break` is not a soft outcome: redline_quote_apply
        groups it with not_found, so the issue degrades to flag-only."""
        import redline_quote_apply

        self.assertEqual(
            redline_quote_apply.REASON_SPANS_PARAGRAPH_BREAK,
            quote_locate.REASON_SPANS_PARAGRAPH_BREAK,
        )


class TestRuleStillStated(unittest.TestCase):
    RULE = primary_review_pass.BINARY_DECISION_OVERLAY_BLOCK

    def test_the_newline_rule_is_present(self) -> None:
        flat = " ".join(self.RULE.split())
        self.assertIn("A newline in the document text you were shown marks a paragraph boundary", flat)
        self.assertIn('"source_quote" MUST NOT cross one', flat)

    def test_the_rule_was_not_relaxed_to_a_blank_line(self) -> None:
        """Guards the specific wrong edit: 'a BLANK line separates clauses'."""
        flat = " ".join(self.RULE.split()).lower()
        self.assertNotIn("must not cross a blank line", flat)
        self.assertNotIn("quoting across those single line breaks is expected", flat)

    def test_the_heading_marker_rule_still_stands_beside_it(self) -> None:
        flat = " ".join(self.RULE.split())
        self.assertIn('NEVER copy a "## " line', flat)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
