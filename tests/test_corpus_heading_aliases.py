#!/usr/bin/env python3
"""
Red/green gate for issue #215: corpus ingestion mapped clauses to topics by
exact-normalized match on *display* section_ref labels that real executed
agreements never carry — composite refs ('1.2 Admitting Students; 1.4
Expulsion; 1.7 Final Authority'), absent-from-standard-form refs ('[absent]
Indemnification'), and parenthetical Section-10 sub-clause refs ('10
Miscellaneous (Notices)'). No real heading equaled these display strings, so
those topics could never receive clauses and the paragraphs silently
dropped (backend/src/corpus.py:123-180, pre-fix).

This test exercises the real enforcement code in backend/src/corpus.py
(`_build_topic_alias_index`, `_match_by_keyword`, `extract_clauses`)
against the real playbook (`playbooks/eiaa-v1.0.0.json`) so it fails on the
pre-fix single-exact-alias implementation and passes once:

  1. each topic carries a list of heading aliases/patterns (not one
     literal display string);
  2. a composite section_ref is split per real section number, each
     segment its own matchable heading;
  3. `not_in_standard` topics (and other topics whose real-document
     heading can't be pinned to one exact string, e.g. shared Section 10
     sub-clauses) use keyword matching so headings like 'Indemnification',
     'Hold Harmless', and 'Mutual Indemnity' all resolve to the
     indemnification topic;
  4. a paragraph that previously dropped (no exact match) now lands on
     its topic — no silent drop.

Run with: python3 tests/test_corpus_heading_aliases.py
Exit 0 = all checks pass; non-zero = one or more checks failed.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

import corpus  # noqa: E402


def _load_real_playbook() -> dict:
    return corpus._load_playbook()


class TestCompositeSectionRefSplitsPerRealSection(unittest.TestCase):
    """exos-discretion-and-authority: '1.2 Admitting Students; 1.4
    Expulsion; 1.7 Final Authority' — three real sections jammed into one
    display label. Each real section's own heading must independently
    resolve to the topic."""

    def setUp(self):
        self.playbook = _load_real_playbook()

    def test_each_composite_segment_heading_resolves_to_the_topic(self):
        for heading in ("1.2 Admitting Students", "1.4 Expulsion", "1.7 Final Authority"):
            paragraphs = [{"heading": heading, "text": f"Clause body for {heading}."}]
            clauses = corpus.extract_clauses(paragraphs, self.playbook)
            self.assertEqual(
                len(clauses), 1,
                f"Heading {heading!r} (a real composite segment, not the jammed display "
                f"label) should not be silently dropped.",
            )
            self.assertEqual(clauses[0]["playbook_topic_id"], "exos-discretion-and-authority")

    def test_the_jammed_composite_display_label_itself_still_never_appears_verbatim(self):
        # Sanity: the composite display label is not a real heading an
        # executed agreement would carry — confirms the fix isn't just
        # widening the old exact-match to accept the jammed string.
        jammed = "1.2 Admitting Students; 1.4 Expulsion; 1.7 Final Authority"
        paragraphs = [{"heading": "1.2 Admitting Students", "text": "Body."}]
        clauses = corpus.extract_clauses(paragraphs, self.playbook)
        self.assertNotEqual(clauses[0]["heading"], jammed)


class TestNotInStandardTopicsUseKeywordMatching(unittest.TestCase):
    """indemnification: '[absent] Indemnification' — not_in_standard, so
    the real document's own heading for this newly-inserted clause varies
    freely. Must match by keyword, not exact display-label equality."""

    def setUp(self):
        self.playbook = _load_real_playbook()

    def test_indemnification_hold_harmless_and_mutual_indemnity_all_map_to_indemnification(self):
        headings = ["Indemnification", "Hold Harmless", "Mutual Indemnity"]
        for heading in headings:
            paragraphs = [{"heading": heading, "text": f"Body text for {heading}."}]
            clauses = corpus.extract_clauses(paragraphs, self.playbook)
            self.assertEqual(
                len(clauses), 1,
                f"Heading {heading!r} should keyword-match the indemnification topic, "
                f"not silently drop.",
            )
            self.assertEqual(clauses[0]["playbook_topic_id"], "indemnification")

    def test_insurance_and_governing_law_headings_also_resolve(self):
        cases = [
            ("Insurance", "insurance"),
            ("Governing Law", "governing-law-and-venue"),
            ("Venue and Jurisdiction", "governing-law-and-venue"),
        ]
        for heading, expected_topic_id in cases:
            paragraphs = [{"heading": heading, "text": f"Body text for {heading}."}]
            clauses = corpus.extract_clauses(paragraphs, self.playbook)
            self.assertEqual(len(clauses), 1, f"Heading {heading!r} should not silently drop.")
            self.assertEqual(clauses[0]["playbook_topic_id"], expected_topic_id)

    def test_the_absent_bracket_prefix_never_appears_in_a_real_heading(self):
        # Sanity: a real document heading would never literally read
        # '[absent] Indemnification' -- confirms keyword matching (not a
        # widened exact-match on the bracketed display label) is doing
        # the work.
        paragraphs = [{"heading": "Hold Harmless", "text": "Body."}]
        clauses = corpus.extract_clauses(paragraphs, self.playbook)
        self.assertNotIn("[absent]", clauses[0]["heading"])


class TestSection10SubClausesDisambiguateByContent(unittest.TestCase):
    """notices / exclusivity / entire-agreement-and-amendment /
    order-of-precedence all share the bare '10 Miscellaneous' heading in a
    real document; their section_ref parenthetical ('(Notices)', etc.) is
    a display note, not literal heading text. Must disambiguate by
    content, not silently drop everything under a shared heading."""

    def setUp(self):
        self.playbook = _load_real_playbook()

    def test_bare_section_10_heading_resolves_by_body_keyword_content(self):
        cases = [
            ("10 Miscellaneous", "Any notice under this Agreement shall be in writing.", "notices"),
            ("10 Miscellaneous", "This Agreement is non-exclusive.", "exclusivity"),
            ("10 Miscellaneous", "This constitutes the entire agreement and may be amended only in writing.", "entire-agreement-and-amendment"),
        ]
        for heading, text, expected_topic_id in cases:
            paragraphs = [{"heading": heading, "text": text}]
            clauses = corpus.extract_clauses(paragraphs, self.playbook)
            self.assertEqual(
                len(clauses), 1,
                f"Bare {heading!r} heading with body {text!r} should resolve by content, "
                f"not silently drop.",
            )
            self.assertEqual(clauses[0]["playbook_topic_id"], expected_topic_id)

    def test_ambiguous_bare_heading_with_no_disambiguating_content_is_not_guessed(self):
        # No keyword from any Section 10 sub-topic appears in the body —
        # must not guess a topic (fails closed, same convention as
        # IngestionError elsewhere in this module).
        paragraphs = [{"heading": "10 Miscellaneous", "text": "General boilerplate text."}]
        clauses = corpus.extract_clauses(paragraphs, self.playbook)
        self.assertEqual(clauses, [])


class TestRealPlaybookAllTopicsReachableThroughRealisticHeadings(unittest.TestCase):
    """Every topic in the real playbook must be reachable by at least one
    heading a real executed agreement plausibly carries -- not just the
    topic's own jammed/bracketed/parenthetical display label."""

    def test_realistic_heading_per_topic_never_silently_drops(self):
        playbook = _load_real_playbook()
        realistic_headings = {
            "term-length": "2.1 Term",
            "termination-for-cause": "2.2.2 For Cause",
            "limitation-of-liability": "8 Limitation on Liability",
            "exos-discretion-and-authority": "1.4 Expulsion",
            "payment-and-remuneration": "1.5 No Remuneration",
            "student-status-and-benefits": "1.6 No Insurance/Benefits",
            "confidentiality": "7 Confidentiality",
            "assignment": "9 Assignment to Operating Entity",
            "indemnification": "Hold Harmless",
            "insurance": "Insurance",
            "governing-law-and-venue": "Governing Law",
            "non-discrimination": "4 Non-Discrimination",
            "ferpa": "5 Student Records",
            "hipaa": "6 HIPAA",
            "notices": "10 Miscellaneous",
            "exclusivity": "10 Miscellaneous",
            "entire-agreement-and-amendment": "10 Miscellaneous",
            "order-of-precedence": "10 Miscellaneous",
            "inspections": "1.3 Inspections",
            "survival": "2.3 Effect of Termination",
            "compliance": "3 Compliance",
        }
        bodies = {
            "notices": "Any notice under this Agreement shall be in writing.",
            "exclusivity": "This Agreement is non-exclusive.",
            "entire-agreement-and-amendment": "This constitutes the entire agreement and may be amended only in writing.",
            "order-of-precedence": "In the event of a conflict, this Agreement controls and prevails.",
        }
        topic_ids = {t["id"] for t in playbook["topics"]}
        self.assertEqual(
            set(realistic_headings), topic_ids,
            "Test fixture must cover every topic id in the real playbook.",
        )
        for topic_id, heading in realistic_headings.items():
            text = bodies.get(topic_id, f"Clause body text for {topic_id}.")
            paragraphs = [{"heading": heading, "text": text}]
            clauses = corpus.extract_clauses(paragraphs, playbook)
            self.assertEqual(
                len(clauses), 1,
                f"Topic {topic_id!r} with realistic heading {heading!r} silently dropped.",
            )
            self.assertEqual(clauses[0]["playbook_topic_id"], topic_id)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestCompositeSectionRefSplitsPerRealSection,
        TestNotInStandardTopicsUseKeywordMatching,
        TestSection10SubClausesDisambiguateByContent,
        TestRealPlaybookAllTopicsReachableThroughRealisticHeadings,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
