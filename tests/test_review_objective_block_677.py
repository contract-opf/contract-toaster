"""Issues #675/#677: ONE stated objective, rendered for both prompt paths.

WHY, measured. The old `REVIEW_GUIDANCE_BLOCK` opened every review with
"reviewing a counterparty-modified contract against your organization's
standard-form position ... restore an acceptable position". On third-party paper
that is false, and it was the strongest stated objective in the prompt: four
live runs against the real playbook produced findings whose rationales cited the
form every time ("consistent with the standard position", "the form we
ordinarily sign") and never cited our interest -- including edits that gave away
one-sided terms running IN OUR FAVOUR. The party labels were already correct, so
this was never a misbinding; it was a conformity objective doing what it said.

These are PROMPT PINS: what the assembled prompt says, not what a model does
with it. Model behaviour is measured live on #677, not asserted here.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import opf_prompt  # noqa: E402
import opf_terminology  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

PARTY = "Athletes' Performance Elite, LLC"
CTYPE = "Educational Institution"


class _Knowledge:
    """Minimal stand-in for `review_knowledge.ReviewKnowledge`."""

    def __init__(self, perspective):
        self.opf_doc = {"perspective": perspective} if perspective else {}
        self.overrides = None

    def system_blocks(self):
        return []


class TestObjectiveBlock(unittest.TestCase):
    def test_the_objective_is_our_interest_never_conformity(self) -> None:
        text = pp.render_review_guidance_block(PARTY, CTYPE)
        self.assertIn("never conformity to", text)
        self.assertIn("do not report it as an issue", text)
        # the old objective must be gone from the shipped block
        self.assertNotIn("standard-form position", text)

    def test_counterparty_accepted_position_favourable_terms_pinned(self) -> None:
        """Issue #687: the uploaded doc is the counterparty's accepted position."""
        text = pp.render_review_guidance_block(PARTY, CTYPE)
        self.assertIn("reflects terms they accept", text)
        self.assertIn("leave it exactly as written", text)
        self.assertIn("Do not narrow it, balance it, make it mutual", text)
        self.assertIn("whatever the drafting argument", text)
        self.assertIn("because the other side has already accepted it", text)

    def test_identity_lines_render_when_a_perspective_exists(self) -> None:
        text = pp.render_review_guidance_block(PARTY, CTYPE)
        self.assertIn(PARTY, text)
        self.assertIn(CTYPE, text)

    def test_no_perspective_renders_the_same_objective_without_identity(self) -> None:
        """Every v1 playbook, and an OPF artifact carrying no perspective."""
        text = pp.render_review_guidance_block()
        self.assertNotIn("We are", text)
        self.assertIn("never conformity to", text)

    def test_the_opf_path_carries_the_party_name(self) -> None:
        blocks = review_spine._assemble_opf_system_blocks(
            _Knowledge({"party": PARTY, "counterparty_type": CTYPE}), ""
        )
        self.assertIn(PARTY, blocks[0]["text"])

    def test_the_opf_path_without_a_perspective_omits_identity(self) -> None:
        blocks = review_spine._assemble_opf_system_blocks(_Knowledge(None), "")
        self.assertNotIn("We are", blocks[0]["text"])
        self.assertIn("never conformity to", blocks[0]["text"])


class TestCriticDuties(unittest.TestCase):
    def test_duty_one_is_worse_for_us_not_departs_from_the_playbook(self) -> None:
        duties = pp._CRITIC_TASKING_DUTIES_1_TO_3
        self.assertIn("WORSE FOR US", duties)
        self.assertNotIn("departs from the playbook position and was not flagged", duties)

    def test_the_critic_must_contest_giving_a_favourable_term_away(self) -> None:
        self.assertIn("gives away a term", pp._CRITIC_TASKING_DUTIES_1_TO_3)

    def test_the_critic_must_contest_narrowing_or_mutualising_favourable_terms(self) -> None:
        """Issue #687: critic duty 2 specifically bars narrowing or mutualising."""
        duties = pp._CRITIC_TASKING_DUTIES_1_TO_3
        self.assertIn("narrowing, balancing, or mutualising a term that runs in our favour", duties)
        self.assertIn("the counterparty has already agreed to it, whatever the drafting argument", duties)


class TestBindingAndRefusedAsks(unittest.TestCase):
    def test_binding_intro_says_floor_rules_are_directional(self) -> None:
        self.assertIn("DIRECTIONAL", opf_prompt.BINDING_INTRO)
        self.assertIn("never us", opf_prompt.BINDING_INTRO)

    def test_refused_asks_are_not_labelled_as_usable_wording(self) -> None:
        # The HEADER mirrors the playbook-engine's canonical vocabulary and is
        # pinned cross-repo by tests/test_opf_digest_prompt.py -- the corrective
        # framing belongs in the toaster-authored help text, not in it.
        self.assertEqual(
            opf_terminology.UNACCEPTABLE.header,
            "Unacceptable variations — rejected/reversed asks",
        )
        self.assertIn("NEVER be used as replacement", opf_terminology.UNACCEPTABLE.help)
        self.assertIn("NEVER our position", opf_terminology.UNACCEPTABLE.help)

    def test_a_refused_ask_renders_without_a_risk_tag(self) -> None:
        """`{risk: neutral/none}` beside wording we turned down reads as
        endorsement. The risk delta describes the ask's effect, not our
        appetite for it."""
        entry = {
            "text_summary": "SYNTHETIC refused wording",
            "risk_delta": {"direction": "neutral", "magnitude": "none"},
        }
        refused = opf_prompt._LIST_FORMATTERS["unacceptable"](entry, "test")
        kept = opf_prompt._LIST_FORMATTERS["concessions"](entry, "test")
        self.assertNotIn("risk", refused)
        self.assertIn("risk", kept)  # the sibling sections still carry it


if __name__ == "__main__":
    unittest.main()
