#!/usr/bin/env python3
"""
Regression test for issue #616: the primary reviewer must be TOLD, in the
assembled prompt, to write `verdict_summary` in its own words rather than
quoting playbook text back verbatim.

## The bug this pins the fix for

Live prod review `9d0a5718-4061-4e41-b98d-c88a78d57a21` (2026-08-24) against
the real `EDUCATIONAL-AFFILIATION` playbook died at `run_review` with
`leakage_detected`. The Diagnostics row named the detector exactly:

    Detector: playbook_leakage - playbook-ngram - verdict_summary

The model reproduced blocked playbook content VERBATIM in its own verdict
summary. On the OPF 0.3 path `leakage_scan.ConfidentialCorpus
.from_opf_document` puts Floor invariant `statement`/`rationale`,
`posture.system_prompt`, and the digest's `concessions`/`unacceptable`/
`exemplar_forms` summaries into `playbook_ngrams` -- all normative contract
prose, i.e. exactly the wording a model reaches for when it states back which
position it applied. The whole review was blocked and the user got nothing to
download.

## Why the fix is upstream and not a gate change

`scripts/leakage_scan.py` is CORRECT and is deliberately untouched by the
change this test guards. The scanner matches verbatim substrings modulo
case/whitespace and its own module docstring records paraphrase as an
accepted, documented residual ("Residual risk -- paraphrase (documented, not
a silent miss)"). A summary written in the model's own words is therefore
already inside what the design tolerates. The fix is to bring the model's
behavior in line with the contract that already exists -- not to move the
line.

## What this test proves, and what it does NOT

PROVES: the own-words instruction is present in the system prompt that is
actually SENT to the model -- on the v1 playbook path, on the OPF digest-mode
path, and on the critic pass -- and that it keeps its two verbatim
exemptions. A future prompt refactor cannot silently drop it and re-open the
bug without turning this file red.

DOES NOT PROVE: that any model obeys the instruction. Prompt compliance is
only observable in a live paid review. This file is a prompt-assembly
regression guard, nothing more; the behavioural confirmation is the owner's
live re-run against prod.

Run with: python3 tests/test_verdict_summary_own_words_616.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
V1_PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass  # noqa: E402
import leakage_scan  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_knowledge  # noqa: E402
import review_spine  # noqa: E402

PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"

#: The two fields `LeakageScanner.scan` allowlists against
#: `standard_clause_ngrams` (issue #208) -- read off the scanner rather than
#: restated, so this test cannot drift from the real allowlist.
_REPLACEMENT_TEXT_ALLOWLIST = leakage_scan._REPLACEMENT_TEXT_FIELDS


def _load_fixture_text(name: str) -> str:
    with open(MODEL_RESPONSES_DIR / name, "r", encoding="utf-8") as fh:
        return fh.read()


def _v1_playbook() -> dict[str, Any]:
    with open(V1_PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _opf_doc() -> dict[str, Any]:
    with open(OPF_FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _diff_hunks() -> list[dict[str, Any]]:
    return [
        {
            "kind": "modified_new",
            "anchor": "sec-8",
            "text": "Each party's aggregate liability shall not exceed $75,000.",
        }
    ]


def _anchored_clauses() -> list[dict[str, Any]]:
    return [
        {
            "anchor": "sec-8",
            "standard_text": "Each party's aggregate liability shall not exceed $150,000.",
            "counterparty_text": "Each party's aggregate liability shall not exceed $75,000.",
            "delta": "$150,000 -> $75,000",
        }
    ]


def _precedent() -> list[dict[str, Any]]:
    return [
        {
            "clause_id": "clause-1",
            "polarity": "positive",
            "text": "Aggregate liability capped at $150,000.",
        }
    ]


def _sent_primary_system_prompt(
    system_blocks_override: list[dict[str, Any]] | None = None,
    playbook: dict[str, Any] | None = None,
) -> str:
    """The system prompt `run_primary_pass` actually hands to `invoke()`.

    Asserting on `FakeBedrockClient.calls[0]["system_prompt"]` rather than on
    the constant is the whole point: it is the ASSEMBLED prompt, so a
    refactor that keeps the constant but stops composing it into the blocks
    still turns this red.
    """
    client = model_client.FakeBedrockClient(
        {PRIMARY_MODEL_ID: [_load_fixture_text("primary_request_change_valid.json")]}
    )
    ledger: list[model_client.ModelInvocationRecord] = []
    pp.run_primary_pass(
        review_id="review-616-primary",
        retrieved_precedent=_precedent(),
        playbook=playbook if playbook is not None else _v1_playbook(),
        model_client=client,
        model_id=PRIMARY_MODEL_ID,
        ledger_write=ledger.append,
        doc_text="Section 8 text.",
        system_blocks_override=system_blocks_override,
        playbook_hash_override="sha256:616" if system_blocks_override is not None else None,
    )
    if not client.calls:
        raise AssertionError(
            "run_primary_pass made no model call -- the prompt this test "
            "inspects was never assembled."
        )
    return client.calls[0]["system_prompt"]


def _opf_system_blocks() -> list[dict[str, Any]]:
    """The OPF digest-mode blocks, composed exactly the way
    `review_spine.run_review` composes them for a real OPF-governed review."""
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2={"opf": _opf_doc(), "overrides": None},
        policy=None,
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        accept_empty_posture=True,
    )
    return review_spine._assemble_opf_system_blocks(knowledge, "")


# ---------------------------------------------------------------------------
# The instruction's substance.
# ---------------------------------------------------------------------------


class TestOwnWordsRuleSubstance(unittest.TestCase):
    def test_rule_binds_verdict_summary_by_name(self):
        rule = pp.OWN_WORDS_SUMMARY_RULE
        self.assertIn(
            '"verdict_summary"',
            rule,
            "The own-words rule must name verdict_summary explicitly -- that is "
            "the field the live leakage trip fired on (issue #616).",
        )
        lowered = rule.lower()
        self.assertIn(
            "your own words",
            lowered,
            "The rule must tell the model to use its OWN WORDS; that phrasing "
            "is the whole instruction.",
        )
        self.assertIn(
            "word-for-word",
            lowered,
            "The rule must forbid copying playbook wording word-for-word -- "
            "verbatim reproduction is exactly what leakage_scan.py matches.",
        )
        self.assertIn(
            "playbook",
            lowered,
            "The rule must name the playbook as the source not to be quoted.",
        )

    def test_rule_forbids_vagueness_as_a_substitute_for_paraphrase(self):
        """Paraphrase must not degrade into 'be vague' -- the attorney still
        has to learn which position was applied and how the clause fell
        short."""
        lowered = pp.OWN_WORDS_SUMMARY_RULE.lower()
        self.assertIn(
            "does not mean vague",
            lowered,
            "The rule must say in as many words that own-words does NOT mean "
            "vague, or a model will trade specificity for safety.",
        )
        for required in ("which position was applied", "falls short"):
            self.assertIn(
                required,
                lowered,
                f"The rule must still demand that the summary state {required!r} "
                "-- rewording is a change of wording, never a loss of substance.",
            )

    def test_rule_is_scoped_not_a_blanket_never_quote_the_playbook(self):
        """A blanket 'never quote the playbook' would be a REGRESSION.

        Issue #627 moved the exempt fields, not the exemption. Under the
        block-transcript contract the model no longer writes a
        `proposed_replacement_text` or a `source_quote` at all -- it writes
        SEGMENTS. A `keep`/`delete` segment must stay
        character-for-character or the transcript cannot prove (the #378
        concern, now enforced mechanically); an `insert` segment (and an
        `insert_block_after`'s `new_text`) is where the tenant's
        `our_standard` / digest text is reproduced word-for-word so it can
        land in the document, which is exactly what `LeakageScanner.scan`'s
        `is_replacement_text` allowlist (issue #208) exists to keep from
        self-blocking. Those are the carve-outs that must survive any future
        edit of this rule.
        """
        rule = pp.OWN_WORDS_SUMMARY_RULE
        for exempt_field in ('"keep"', '"delete"', '"insert"', '"new_text"'):
            self.assertIn(
                exempt_field,
                rule,
                f"{exempt_field} must be named as EXEMPT from the own-words "
                "rule. Without the carve-out the instruction breaks faithful "
                "redlines and makes every transcript unprovable -- see issue "
                "#208 / #378 / #627.",
            )
        self.assertIn(
            "exempt",
            rule.lower(),
            "The exemption must be stated as an exemption, not merely mentioned.",
        )
        self.assertIn(
            "verbatim",
            rule.lower(),
            "The exempt fields must be told to stay verbatim.",
        )

    def test_rule_also_binds_the_other_scanned_narrative_fields(self):
        """`counterparty_change_summary` and
        `external_rationale_for_footnote` are scanned against the SAME
        `playbook_ngrams` with no allowlist (leakage_scan._ISSUE_SCANNED_FIELDS
        + `_REPLACEMENT_TEXT_FIELDS`), so they carry the identical exposure and
        get the identical instruction."""
        rule = pp.OWN_WORDS_SUMMARY_RULE
        for field_name in ("counterparty_change_summary", "external_rationale_for_footnote"):
            self.assertIn(
                f'"{field_name}"',
                rule,
                f"{field_name} is scanned against playbook_ngrams with no "
                "allowlist, exactly like verdict_summary -- the own-words rule "
                "must cover it.",
            )
            self.assertNotIn(
                field_name,
                _REPLACEMENT_TEXT_ALLOWLIST,
                f"{field_name} is not replacement-text-allowlisted; if that "
                "ever changes, revisit this instruction.",
            )
        self.assertIn(
            "objection",
            rule.lower(),
            "The critic's own narrative prose (critic_delta contested "
            "replacements / rationale objections) is scanned the same way and "
            "reads this same overlay -- the rule must reach it.",
        )


# ---------------------------------------------------------------------------
# The instruction's presence in the ASSEMBLED prompt (the anti-refactor pin).
# ---------------------------------------------------------------------------


class TestOwnWordsRuleReachesTheModel(unittest.TestCase):
    def test_v1_primary_prompt_carries_the_rule(self):
        system_prompt = _sent_primary_system_prompt()
        self.assertIn(
            pp.OWN_WORDS_SUMMARY_RULE,
            system_prompt,
            "The own-words rule is missing from the system prompt "
            "run_primary_pass actually sent on the v1 playbook path -- issue "
            "#616 is re-opened.",
        )

    def test_opf_digest_mode_primary_prompt_carries_the_rule(self):
        """The path the live failure was on. `_assemble_opf_system_blocks`
        composes its OWN block list and hands it to `run_primary_pass` as
        `system_blocks_override`, so a rule added only to the v1 assembler
        would never reach an OPF review."""
        blocks = _opf_system_blocks()
        system_prompt = _sent_primary_system_prompt(system_blocks_override=blocks)
        self.assertIn(
            pp.OWN_WORDS_SUMMARY_RULE,
            system_prompt,
            "The own-words rule is missing from the OPF digest-mode system "
            "prompt -- that is the exact path review "
            "9d0a5718-4061-4e41-b98d-c88a78d57a21 failed on.",
        )

    def test_critic_prompt_carries_the_rule(self):
        """The critic writes `critic_delta` objection prose, which is scanned
        against the same corpus. It reads the same assembled system blocks, so
        the rule must arrive there too."""
        client = model_client.FakeBedrockClient(
            {CRITIC_MODEL_ID: [_load_fixture_text("critic_no_delta_accept_valid.json")]}
        )
        ledger: list[model_client.ModelInvocationRecord] = []
        critic_review_pass.run_critic_pass(
            review_id="review-616-critic",
            primary_output=json.loads(_load_fixture_text("primary_request_change_valid.json")),
            playbook=_v1_playbook(),
            model_client=client,
            model_id=CRITIC_MODEL_ID,
            ledger_write=ledger.append,
        )
        self.assertTrue(client.calls, "run_critic_pass made no model call.")
        self.assertIn(
            pp.OWN_WORDS_SUMMARY_RULE,
            client.calls[0]["system_prompt"],
            "The own-words rule is missing from the critic pass's assembled "
            "system prompt.",
        )

    def test_rule_survives_the_shared_overlay_seam(self):
        """`BINARY_DECISION_OVERLAY_BLOCK` is the ONE block both knowledge
        paths and both passes send. Pinning the rule INTO it is what makes the
        three assertions above hold at once -- if a refactor moves the rule to
        a path-specific block, this is the test that says why that is not
        equivalent."""
        self.assertIn(
            pp.OWN_WORDS_SUMMARY_RULE,
            pp.BINARY_DECISION_OVERLAY_BLOCK,
            "The own-words rule must live in the shared output-contract "
            "overlay, not in a per-path block.",
        )


# ---------------------------------------------------------------------------
# The premise the fix rests on, stated as an executable fact.
# ---------------------------------------------------------------------------


class TestParaphraseIsAnAcceptedOutcome(unittest.TestCase):
    """Not a mutation-sensitive test of the prompt change -- a guard on the
    PREMISE that makes the prompt change the right lever instead of a gate
    change: `leakage_scan` blocks the verbatim Floor statement and passes a
    reworded one. If this ever stops holding, telling the model to paraphrase
    stops being sufficient and issue #616 needs re-deciding.
    """

    def test_verbatim_floor_statement_blocks_but_a_reworded_one_passes(self):
        doc = _opf_doc()
        corpus = leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)
        self.assertTrue(
            corpus.playbook_ngrams,
            "The OPF fixture must contribute blocked playbook n-grams, or this "
            "test proves nothing.",
        )
        blocked_gram = max(corpus.playbook_ngrams, key=len)

        verbatim = leakage_scan.scan_model_output(
            {"verdict_summary": f"Overall: {blocked_gram}", "issues": []}, corpus
        )
        self.assertTrue(verbatim.blocked, "A verbatim playbook n-gram must still block.")
        self.assertEqual(verbatim.field_name, "verdict_summary")
        self.assertEqual(verbatim.category, leakage_scan.CATEGORY_PLAYBOOK)

        reworded = " ".join(reversed(blocked_gram.split()))
        self.assertNotEqual(reworded, blocked_gram)
        paraphrased = leakage_scan.scan_model_output(
            {"verdict_summary": f"Overall: {reworded}", "issues": []}, corpus
        )
        self.assertFalse(
            paraphrased.blocked,
            "Re-ordered (non-verbatim) wording must NOT trip the scan -- that "
            "documented residual is what makes the own-words instruction a "
            "sufficient upstream fix rather than a gate weakening.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
