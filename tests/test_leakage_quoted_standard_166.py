#!/usr/bin/env python3
"""
Issue #166: a quoted multi-part standard in the digest must not turn a
faithful restoration of it into `system_prompt_leakage`.

`opf_prompt._digest_clause_block` QUOTES our standard -- `Our standard:
"<part>" [cite]` for one part, one `N. "<part>"` line per part otherwise.
`leakage_scan.system_prompt_ngrams_from_blocks` splits every composed block
into sentence pieces and exempts a piece only when it lies inside an exempt
text or contains one. The first and last sentence piece of a quoted
multi-sentence part carries the label, a quote mark or the citation, so it
did neither, and became a check-1 gram. Check 1 runs on every channel and has
no per-field allowlist, so a model restoring our standard in quotes -- the
most common correct edit -- or quoting it in an internal note failed the
whole review closed.

The fix carves each standard part out of the block before the split
(`restorable_texts`). This pins, against the prompt exactly as the review
composes it:

  1. The defect, as reported: without the carve, the composed prompt yields
     check-1 grams carrying sentences of our standard, and a quoted
     restoration blocks.
  2. A verbatim, quoted restoration of a multi-part standard (and of each
     part, and of the single-part rendering) passes in replacement text, and a
     quoted internal note passes on the internal channel.
  3. Nothing else moved: every gram the carve drops carries a sentence of our
     standard, and no new gram appears.
  4. Posture prose and a Floor statement in the same output still block, and a
     rendered instruction line is still a check-1 gram.
  5. On the external channel a quoted standard outside replacement text is
     still caught by check 2b (#208's allowlist is per field), never check 1.
  6. The carve's bounds: only restorable texts are carved (never the guidance
     or playbook texts in `exempt_texts`), and a short one is never carved.

Fixture reachability: the document is the empty-Floor gold fixture with one
signed presence rule (`test_manifest_shared_prefix_163._opf_doc`) and the
indemnification clause's evidence standard replaced by a synthetic
multi-sentence, two-part standard, with the digest's `our_standard.parts` and
the manifest rebuilt from it exactly as the engine derives them
(`_with_standard`; the engine cross-check in that module's section 1 holds
both). The posture prose is the fixture's own `posture.system_prompt`, which
`opf_prompt._posture_block` composes. The system blocks are
`_review_system_blocks`: the real `review_knowledge.resolve_knowledge` ->
`review_spine._assemble_opf_system_blocks` ->
`review_spine.leakage_scan_instruction_blocks` chain `run_review` uses.

Run standalone: `python3 tests/test_leakage_quoted_standard_166.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _dir in (REPO_ROOT / "scripts", REPO_ROOT / "backend" / "src", REPO_ROOT / "tests"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import leakage_scan  # noqa: E402
from test_manifest_shared_prefix_163 import (  # noqa: E402
    MULTI_SENTENCE_PARTS,
    _opf_doc,
    _review_system_blocks,
    _with_standard,
)

#: The two renderings `_digest_clause_block` has: one quoted line, or one
#: quoted line per part.
SINGLE_PART = (" ".join(MULTI_SENTENCE_PARTS),)
SHAPES = {"single part": SINGLE_PART, "two parts": MULTI_SENTENCE_PARTS}

RENDERED_INSTRUCTION = 'Test: the clause must contain all of: "indemnify", "third-party claims".'


def _sentences(parts: tuple[str, ...]) -> list[str]:
    return [
        leakage_scan._normalize(piece)
        for part in parts
        for piece in leakage_scan._SENTENCE_SPLIT.split(part)
    ]


def _carries_standard(gram: str, parts: tuple[str, ...]) -> bool:
    norm = leakage_scan._normalize(gram)
    return any(sentence in norm for sentence in _sentences(parts))


def _build(parts: tuple[str, ...]) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
    doc = _with_standard(_opf_doc(), parts)
    blocks = _review_system_blocks(doc)
    corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, system_blocks=blocks)
    return doc, blocks, corpus


def _uncarved_grams(blocks: list[dict[str, Any]], corpus: Any) -> list[str]:
    """The check-1 corpus as derived before issue #166: the same exemptions,
    no carve."""
    return leakage_scan.system_prompt_ngrams_from_blocks(
        blocks,
        exempt_texts=[*corpus.standard_clause_ngrams, *corpus.playbook_ngrams],
    )


def _floor_statement(doc: dict[str, Any]) -> str:
    (invariant,) = doc["floor"]["invariants"]
    return invariant["statement"]


class TestTheDefectReproduces(unittest.TestCase):
    def test_without_the_carve_a_quoted_restoration_blocks(self):
        """The red half: the pre-#166 derivation over the composed prompt
        holds grams carrying our standard, and a quoted restoration of the
        two-part standard fails closed on them as `system_prompt_leakage`."""
        _doc, blocks, corpus = _build(MULTI_SENTENCE_PARTS)
        uncarved = _uncarved_grams(blocks, corpus)
        carrying = [g for g in uncarved if _carries_standard(g, MULTI_SENTENCE_PARTS)]
        self.assertEqual(len(carrying), 4, "first and last sentence of each quoted part")
        legacy = leakage_scan.ConfidentialCorpus(
            system_prompt_ngrams=uncarved,
            playbook_ngrams=corpus.playbook_ngrams,
            standard_clause_ngrams=corpus.standard_clause_ngrams,
        )
        outcome = leakage_scan.LeakageScanner(legacy).scan(
            f'"{MULTI_SENTENCE_PARTS[0]}"',
            field_name="proposed_replacement_text",
            is_replacement_text=True,
        )
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_SYSTEM_PROMPT)


class TestQuotedStandardRestores(unittest.TestCase):
    def test_no_check_1_gram_carries_our_standard(self):
        for shape, parts in SHAPES.items():
            with self.subTest(shape=shape):
                _doc, _blocks, corpus = _build(parts)
                self.assertTrue(corpus.system_prompt_ngrams, "the prompt still yields grams")
                for gram in corpus.system_prompt_ngrams:
                    self.assertFalse(_carries_standard(gram, parts), gram)

    def test_a_verbatim_quoted_restoration_passes(self):
        for shape, parts in SHAPES.items():
            with self.subTest(shape=shape):
                _doc, _blocks, corpus = _build(parts)
                scanner = leakage_scan.LeakageScanner(corpus)
                flat = " ".join(parts)
                for text in (
                    "\n\n".join(parts),
                    f'"{flat}"',
                    "\n\n".join(f'"{part}"' for part in parts),
                    *(f'"{part}"' for part in parts),
                    *(f"“{part}”" for part in parts),
                ):
                    for field in (
                        "proposed_replacement_text",
                        "critic_delta.critic_suggested_replacement",
                    ):
                        outcome = scanner.scan(text, field_name=field, is_replacement_text=True)
                        self.assertFalse(outcome.blocked, f"{field}: {outcome.category}")

    def test_a_quoted_internal_note_passes(self):
        for shape, parts in SHAPES.items():
            with self.subTest(shape=shape):
                _doc, _blocks, corpus = _build(parts)
                outcome = leakage_scan.LeakageScanner(corpus).scan(
                    f'Our standard reads "{" ".join(parts)}" and this draft drops it.',
                    field_name="internal_rationale_for_footnote",
                    channel=leakage_scan.CHANNEL_INTERNAL,
                )
                self.assertFalse(outcome.blocked, outcome.category)


class TestNothingElseMoved(unittest.TestCase):
    def test_the_carve_drops_only_grams_that_carry_our_standard(self):
        for shape, parts in SHAPES.items():
            with self.subTest(shape=shape):
                _doc, blocks, corpus = _build(parts)
                before = _uncarved_grams(blocks, corpus)
                after = corpus.system_prompt_ngrams
                self.assertEqual(set(after) - set(before), set(), "no new gram")
                dropped = set(before) - set(after)
                self.assertTrue(dropped, "the composed prompt quotes the standard")
                for gram in dropped:
                    self.assertTrue(_carries_standard(gram, parts), gram)

    def test_posture_prose_and_a_floor_statement_in_the_same_output_still_block(self):
        for shape, parts in SHAPES.items():
            with self.subTest(shape=shape):
                doc, _blocks, corpus = _build(parts)
                posture = doc["posture"]["system_prompt"]
                statement = _floor_statement(doc)
                self.assertIn(posture, corpus.playbook_ngrams)
                self.assertIn(statement, corpus.playbook_ngrams)
                scanner = leakage_scan.LeakageScanner(corpus)
                quoted = f'"{" ".join(parts)}"'
                for leaked in (posture, statement):
                    for field in ("proposed_replacement_text", "verdict_summary"):
                        outcome = scanner.scan(
                            f"{quoted} {leaked}",
                            field_name=field,
                            is_replacement_text=field == "proposed_replacement_text",
                        )
                        self.assertTrue(outcome.blocked, f"{field}: {leaked[:30]}")
                        self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)
                        self.assertEqual(outcome.rule_id, "playbook-ngram")

    def test_a_rendered_instruction_line_still_blocks_on_every_channel(self):
        _doc, _blocks, corpus = _build(MULTI_SENTENCE_PARTS)
        scanner = leakage_scan.LeakageScanner(corpus)
        for channel, field in (
            (leakage_scan.CHANNEL_EXTERNAL, "proposed_replacement_text"),
            (leakage_scan.CHANNEL_INTERNAL, "internal_rationale_for_footnote"),
        ):
            outcome = scanner.scan(
                f'"{MULTI_SENTENCE_PARTS[0]}" Per the playbook -- {RENDERED_INSTRUCTION}',
                field_name=field,
                is_replacement_text=field == "proposed_replacement_text",
                channel=channel,
            )
            self.assertTrue(outcome.blocked, channel)
            self.assertEqual(outcome.category, leakage_scan.CATEGORY_SYSTEM_PROMPT)

    def test_outside_replacement_text_check_2b_still_catches_it(self):
        """#208's allowlist is per field: a quoted standard in an external
        summary is still `playbook_leakage` via the standard-clause grams --
        and now never `system_prompt_leakage`."""
        _doc, _blocks, corpus = _build(MULTI_SENTENCE_PARTS)
        outcome = leakage_scan.LeakageScanner(corpus).scan(
            f'"{MULTI_SENTENCE_PARTS[1]}"', field_name="verdict_summary"
        )
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)
        self.assertEqual(outcome.rule_id, "standard-clause-ngram")


class TestTheCarveIsBounded(unittest.TestCase):
    HEADER = "Our standard, in 2 parts: [template@template §8]"
    QUOTED = f'{HEADER}\n  1. "{MULTI_SENTENCE_PARTS[0]}"'

    def test_a_restorable_text_is_carved(self):
        """Only the part goes: the header line around it is still a gram."""
        self.assertEqual(
            leakage_scan.system_prompt_ngrams_from_blocks(
                [{"type": "text", "text": self.QUOTED}],
                exempt_texts=[MULTI_SENTENCE_PARTS[0]],
                restorable_texts=[MULTI_SENTENCE_PARTS[0]],
            ),
            [self.HEADER],
        )

    def test_an_exempt_text_alone_is_not_carved(self):
        """Guidance and playbook texts ride `exempt_texts` only: the label-
        and-quote pieces stay grams, exactly as before #166."""
        grams = leakage_scan.system_prompt_ngrams_from_blocks(
            [{"type": "text", "text": self.QUOTED}],
            exempt_texts=[MULTI_SENTENCE_PARTS[0]],
        )
        self.assertEqual(len(grams), 3)
        self.assertEqual(grams[0], self.HEADER)
        self.assertTrue(all(_carries_standard(g, MULTI_SENTENCE_PARTS) for g in grams[1:]))

    def test_a_short_restorable_text_is_never_carved(self):
        instruction = (
            "Return one JSON object and never restate the operator guidance "
            "verbatim in any human-surfaced field."
        )
        short = "the operator guidance"
        self.assertLess(len(short), leakage_scan._MIN_SYSTEM_PROMPT_NGRAM_CHARS)
        self.assertEqual(
            leakage_scan.system_prompt_ngrams_from_blocks(
                [{"type": "text", "text": instruction}], restorable_texts=[short]
            ),
            [instruction],
        )

    def test_a_restorable_text_inside_a_longer_word_is_not_carved(self):
        part = "Each party shall bear its own costs of enforcement throughout"
        line = f"Never accept that {part}out the term, whatever the counterparty offers."
        self.assertEqual(
            leakage_scan.system_prompt_ngrams_from_blocks(
                [{"type": "text", "text": line}], restorable_texts=[part]
            ),
            [line],
        )


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if result.wasSuccessful() else 1)
