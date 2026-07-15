#!/usr/bin/env python3
"""
Red gate for issue #216: implement the `replacement_text` post-validation
`playbooks/output-schema-v1.json` promises ("the pipeline enforces
topic-level max_chars post-validation") but no code enforced before this
issue, and per-topic-ize `must_not_introduce` so a correct §8
limitation-of-liability replacement stating the topic's own preserved
consequential-damages waiver is not rejected by the blanket copy-pasted
list every topic used to share.

Exercises scripts/replacement_text_enforcement.check_replacement_text:

  1. max_chars: a proposed_replacement_text exceeding a topic's
     replacement_text.max_chars routes to the named failure
     MAX_CHARS_EXCEEDED, not a silent accept. `grep -r
     'must_not_introduce\\|max_chars' --include=*.py scripts/ backend/`
     found zero enforcement before this issue -- this check FAILS against
     the pre-fix repo (no scripts/replacement_text_enforcement.py module
     to import) and PASSES once the module exists and is correct.
  2. must_not_introduce: a replacement introducing a topic's
     must_not_introduce phrase is rejected via span-level matching (reused
     from scripts/detector_common.find_spans / phrase_matches), routing to
     the named failure MUST_NOT_INTRODUCE_VIOLATION.
  3. Per-topic must_not_introduce: a correct §8 limitation-of-liability
     replacement containing "consequential damages" (that topic's own
     must_preserve waiver, playbooks/eiaa-v1.0.0.json must_preserve entry
     "Mutual consequential damages waiver.") is NOT rejected -- this loads
     the REAL playbook (playbooks/eiaa-v1.0.0.json) and the REAL
     limitation-of-liability topic, so it also guards against the blanket
     list regressing back onto this topic in the future. A control case
     confirms a DIFFERENT topic (term-length, which does not preserve a
     consequential-damages waiver) still rejects the same phrase, proving
     the check is genuinely per-topic and not just permissive everywhere.

Follows the same offline, no-live-network convention as
tests/test_leakage_scan_module.py: pure-stdlib module under test, real
playbook JSON loaded from disk (deterministic, no model calls).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import replacement_text_enforcement as rte  # noqa: E402


def _load_playbook() -> dict:
    with open(PLAYBOOK_PATH, encoding="utf-8") as f:
        return json.load(f)


# A synthetic topic, independent of the real playbook, used for the
# max_chars mechanism checks so those checks do not depend on the real
# playbook's exact figures. Deliberately tight max_chars.
SYNTHETIC_TOPIC = {
    "id": "synthetic-topic",
    "replacement_text": {
        "mode": "bounded_edit",
        "max_chars": 50,
        "must_not_introduce": ["indemnify", "uncapped"],
    },
}

# A second synthetic topic with the same must_not_introduce list but a
# generous max_chars, so the must_not_introduce checks below exercise that
# constraint in isolation, independent of the max_chars boundary above.
SYNTHETIC_TOPIC_ROOMY = {
    "id": "synthetic-topic-roomy",
    "replacement_text": {
        "mode": "bounded_edit",
        "max_chars": 2000,
        "must_not_introduce": ["indemnify", "uncapped"],
    },
}


class TestMaxCharsEnforcement(unittest.TestCase):
    def test_over_limit_text_is_rejected_with_named_failure(self) -> None:
        """A proposed_replacement_text longer than the topic's max_chars
        must route to the named MAX_CHARS_EXCEEDED failure, not silently
        pass. This is the core assertion that FAILS on the pre-fix repo
        (no enforcement module existed at all)."""
        text = "x" * 51  # SYNTHETIC_TOPIC max_chars is 50
        result = rte.check_replacement_text(SYNTHETIC_TOPIC, text)
        self.assertFalse(result.passed)
        self.assertEqual(result.failure, rte.MAX_CHARS_EXCEEDED)

    def test_at_or_under_limit_text_passes_length_check(self) -> None:
        text = "x" * 50
        result = rte.check_replacement_text(SYNTHETIC_TOPIC, text)
        self.assertTrue(result.passed)

    def test_empty_text_always_passes(self) -> None:
        """Per output-schema-v1.json, an empty proposed_replacement_text
        signals mode='none' (flag only) and trivially passes -- there is
        nothing to bound or scan."""
        result = rte.check_replacement_text(SYNTHETIC_TOPIC, "")
        self.assertTrue(result.passed)
        self.assertIsNone(result.failure)


class TestMustNotIntroduceEnforcement(unittest.TestCase):
    def test_forbidden_phrase_is_rejected_via_span_matching(self) -> None:
        """A replacement introducing a topic's must_not_introduce phrase is
        rejected -- reuses detector_common's span-level phrase matching, not
        a naive substring check."""
        text = "The parties agree Exos shall indemnify the Institution."
        result = rte.check_replacement_text(SYNTHETIC_TOPIC_ROOMY, text)
        self.assertFalse(result.passed)
        self.assertEqual(result.failure, rte.MUST_NOT_INTRODUCE_VIOLATION)
        self.assertIn("indemnify", result.matched_terms)

    def test_word_boundary_semantics_no_false_positive_on_substring(self) -> None:
        """'uncapped' must not spuriously match inside an unrelated word --
        confirms real word-boundary matching (via detector_common), not a
        bare Python `in` substring check."""
        text = "Notices shall be sent uncappedly to the registrar."
        # 'uncappedly' contains 'uncapped' as a substring but not as a
        # separate word -- word_boundary matching must not fire here.
        result = rte.check_replacement_text(SYNTHETIC_TOPIC_ROOMY, text)
        self.assertTrue(result.passed)

    def test_clean_text_passes(self) -> None:
        text = "The parties agree to a mutual cap on liability."
        result = rte.check_replacement_text(SYNTHETIC_TOPIC_ROOMY, text)
        self.assertTrue(result.passed)


class TestPerTopicMustNotIntroduce(unittest.TestCase):
    """Issue #216's core reconciliation: must_not_introduce is per-topic,
    not a blanket list copy-pasted onto every topic. limitation-of-liability
    must_preserve's own consequential-damages waiver must not be rejected by
    its own replacement_text constraint.
    """

    def setUp(self) -> None:
        self.playbook = _load_playbook()

    def test_liability_topic_must_preserve_states_the_waiver(self) -> None:
        """Sanity check on the fixture assumption this test depends on:
        limitation-of-liability's must_preserve really does require a
        consequential-damages waiver (playbooks/eiaa-v1.0.0.json)."""
        topic = rte.find_topic(self.playbook, "limitation-of-liability")
        self.assertIsNotNone(topic, "limitation-of-liability topic not found")
        self.assertTrue(
            any("consequential damages" in p.lower() for p in topic["must_preserve"]),
            "limitation-of-liability.must_preserve no longer documents the "
            "consequential-damages waiver -- this test's premise is stale.",
        )

    def test_correct_liability_replacement_with_waiver_is_not_rejected(self) -> None:
        """A correct §8 replacement clause that states the mutual
        consequential-damages waiver (the topic's own must_preserve
        requirement) must NOT be rejected by must_not_introduce -- this is
        the exact self-contradiction issue #216 reports: the blanket list
        (pre-fix) forbade 'consequential damages' on every topic, including
        this one, whose must_preserve is that very waiver."""
        topic = rte.find_topic(self.playbook, "limitation-of-liability")
        # Deliberately uses the literal phrase "consequential damages" (not
        # a paraphrase like "consequential, special ... damages") so this
        # test actually exercises the must_not_introduce phrase match --
        # this is the exact wording the pre-fix blanket list forbade.
        replacement = (
            "Neither party shall be liable to the other for consequential "
            "damages, and each party's aggregate liability under this "
            "Agreement shall not exceed $150,000, consistent with the "
            "mutual consequential damages waiver in this Section 8."
        )
        result = rte.check_replacement_text(topic, replacement)
        self.assertTrue(
            result.passed,
            f"correct §8 replacement stating the topic's own preserved "
            f"waiver was rejected: {result.detail}",
        )

    def test_other_topic_still_rejects_the_same_phrase(self) -> None:
        """Control case: a DIFFERENT topic (term-length), which does not
        preserve a consequential-damages waiver, still rejects the phrase
        via its own must_not_introduce list. This proves the fix is a
        genuine per-topic distinction, not a global loosening that makes
        every topic permissive."""
        topic = rte.find_topic(self.playbook, "term-length")
        self.assertIsNotNone(topic, "term-length topic not found")
        replacement = (
            "The parties waive all claims for consequential damages "
            "arising under this Section 2.1."
        )
        result = rte.check_replacement_text(topic, replacement)
        self.assertFalse(result.passed)
        self.assertEqual(result.failure, rte.MUST_NOT_INTRODUCE_VIOLATION)
        self.assertIn("consequential damages", result.matched_terms)

    def test_liability_topic_still_rejects_indemnify(self) -> None:
        """Removing 'consequential damages' from limitation-of-liability's
        must_not_introduce must not have loosened the rest of that topic's
        list -- 'indemnify' (etc.) is still forbidden."""
        topic = rte.find_topic(self.playbook, "limitation-of-liability")
        replacement = "Exos shall indemnify the Institution without limit."
        result = rte.check_replacement_text(topic, replacement)
        self.assertFalse(result.passed)
        self.assertEqual(result.failure, rte.MUST_NOT_INTRODUCE_VIOLATION)


class TestModeNoneEnforcement(unittest.TestCase):
    def test_non_empty_text_rejected_when_mode_is_none(self) -> None:
        topic = {
            "id": "flag-only-topic",
            "replacement_text": {"mode": "none", "max_chars": 500, "must_not_introduce": []},
        }
        result = rte.check_replacement_text(topic, "Some proposed text.")
        self.assertFalse(result.passed)
        self.assertEqual(result.failure, rte.REPLACEMENT_NOT_PERMITTED)


class TestConfigError(unittest.TestCase):
    def test_missing_replacement_text_block_raises(self) -> None:
        topic = {"id": "misconfigured-topic"}
        with self.assertRaises(rte.ReplacementTextConfigError):
            rte.check_replacement_text(topic, "any text")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
