#!/usr/bin/env python3
"""
Issue #616, layer 3: the leakage gate must stop blocking a review for NAMING
the clause it is reviewing.

## The measured failure

Every review of a real affiliation agreement against the real
`EDUCATIONAL-AFFILIATION` playbook died at the leakage gate. Same signature on
every attempt, in prod (2026-08-24) and reproduced offline against the bound
playbook:

    leakage_category:   playbook_leakage
    leakage_rule_id:    playbook-ngram
    leakage_field_name: verdict_summary

Instrumenting the scanner named the matching gram: the single word
`indemnification`, 15 characters. The model output that tripped it was an
ordinary executive summary that named the clause it was discussing -- nothing
proprietary, nothing a counterparty could not read. **You cannot review a
contract without naming its clauses**, so this is a FALSE POSITIVE.

## How a public label became blocked content

`leakage_scan.ConfidentialCorpus.from_opf_document` derived `playbook_ngrams`
from each (then digest 2) clause's summary entries, which carried no
`minLength`, so a summary that summarised nothing -- the bare clause name
repeated -- was schema-valid, and became a blocked gram byte-identical to the
SAME document's public `taxonomy.entries[].label`. Since issue #128 the corpus
reads the digest's precedent texts (signed variants, refused asks, openings not
signed as proposed) and the per-deal precedent record, and
`playbooks/opf/playbook.schema-0.6.json` puts no `minLength` on those either,
so the hazard -- and this guard -- carry over unchanged. The owner measured 18 fields in
the real bound playbook whose entire content is `indemnification`, including
that taxonomy label. The toaster consumes a playbook authored elsewhere, so
it cannot assume those fields are well-formed; the guard belongs here.

## What the fix is, and what it deliberately is NOT

The gate is NOT loosened. `_without_public_vocabulary` narrows the CORPUS by
two whole-gram rules -- a gram that is exactly this document's own taxonomy
`label`/`id` or a clause `title`/`taxonomy_id`, and a gram of fewer than two
words. Nothing about how a retained gram is matched changes, and a long
confidential statement that merely CONTAINS a clause name is untouched.

`TestTheGateStillFires` is the class that matters. Without it this change
would be indistinguishable from switching the check off.

## Fixture reachability

The document under test is the shipped gold OPF 0.6 fixture with two precedent
texts (a refused ask and an opening not signed as proposed, in the digest AND in
the precedent record they summarise) rewritten to the bare-clause-name shape
the real playbook was measured to contain. `TestProductionAcceptsThisPlaybook` re-derives
`identity.section_digests`/`content_hash` and pushes the result through
`opf_load.load_opf_document(require_identity=True)` -- the real production
loader, schema validation, hash verification and injection scan included -- so
the shape asserted over here is a shape production actually accepts, not one
invented for a test.

Run standalone: `python3 tests/test_leakage_public_clause_vocabulary_616.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university-0.6.opf.json"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import leakage_scan  # noqa: E402
import opf_canonicalize  # noqa: E402
import opf_load  # noqa: E402

#: The taxonomy id / clause taxonomy_id of the first digest clause in the gold
#: fixture, and its human label -- the exact pairing the real playbook has for
#: the clause that fired the gate.
CLAUSE_TAXONOMY_ID = "indemnification"
CLAUSE_LABEL = "Indemnification"

#: A second clause whose public label is MULTI-WORD, so the vocabulary rule is
#: proven on more than the single-token case (the single-token rule alone
#: would already cover `indemnification`, leaving the label rule untested).
MULTIWORD_CLAUSE_INDEX = 2
MULTIWORD_CLAUSE_LABEL = "Term and Termination"

#: A summary of exactly the kind the live review produced: it names clauses,
#: and says nothing else the playbook told it.
CLAUSE_NAMING_SUMMARY = (
    "Executive summary: three clauses must change before signature and two "
    "others are worth a look. 1) Indemnification (Section 8) -- as drafted "
    "only one party indemnifies the other, for every claim connected to the "
    "program regardless of who caused it. 2) Term and Termination (Section "
    "11) -- the notice period is one-sided."
)


def _gold_doc() -> dict[str, Any]:
    return json.loads(OPF_FIXTURE_PATH.read_text(encoding="utf-8"))


def _records(doc: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The precedent records a digest entry summarises (its precedent_ids)."""
    ids = set(entry["precedent_ids"])
    return [r for r in doc["evidence"]["precedent"] if r["id"] in ids]


def _rewrite_refused_ask(doc: dict[str, Any], clause_index: int, text: str) -> None:
    """Rewrite a clause's first refused ask in the digest AND in the record it
    summarises (a refused ask's text is not part of the precedent id)."""
    entry = doc["digest"]["clauses"][clause_index]["refused_asks"][0]
    old = entry["text"]
    entry["text"] = text
    for record in _records(doc, entry):
        for ask in record["refused_asks"]:
            if ask["text"] == old:
                ask["text"] = text


def _rewrite_opening(doc: dict[str, Any], clause_index: int, text: str) -> None:
    """Rewrite a clause's first opening-not-signed-as-proposed in the digest
    AND in the record's `opening_text` (not part of the precedent id)."""
    entry = doc["digest"]["clauses"][clause_index]["changed_openings"][0]
    entry["text"] = text
    for record in _records(doc, entry):
        record["opening_text"]["text"] = text


def _bare_clause_name_doc() -> dict[str, Any]:
    """The gold fixture with two precedent texts rewritten to the bare clause
    name -- the shape measured in the real bound playbook.

    Both rewritten fields keep every other key, so this is the same document
    the pipeline already reads, differing only in the one string that caused
    the incident.
    """
    doc = _gold_doc()
    clauses = doc["digest"]["clauses"]

    first = clauses[0]
    assert first["taxonomy_id"] == CLAUSE_TAXONOMY_ID, first["taxonomy_id"]
    assert first["title"] == CLAUSE_LABEL, first["title"]
    assert first["refused_asks"], "fixture must carry a refused ask"
    # Lower-cased, exactly like the taxonomy `id` and unlike the `label`, so
    # the match is proven to survive normalization rather than depending on
    # the label's own casing.
    _rewrite_refused_ask(doc, 0, CLAUSE_TAXONOMY_ID)

    multiword = clauses[MULTIWORD_CLAUSE_INDEX]
    assert multiword["title"] == MULTIWORD_CLAUSE_LABEL, multiword["title"]
    assert multiword["changed_openings"], "fixture must carry a changed opening"
    _rewrite_opening(doc, MULTIWORD_CLAUSE_INDEX, MULTIWORD_CLAUSE_LABEL)

    return doc


def _digest_texts(doc: dict[str, Any]) -> list[str]:
    """Every precedent text the digest carries (not our standard)."""
    texts: list[str] = []
    for clause in doc["digest"]["clauses"]:
        for entry in clause["signed_variants"]:
            texts.extend([entry["text"]] if "text" in entry else [e["text"] for e in entry["exemplars"]])
        texts.extend(e["text"] for e in clause["refused_asks"])
        texts.extend(e["text"] for e in clause["changed_openings"])
    return texts


def _corpus(doc: dict[str, Any], **kwargs: Any) -> leakage_scan.ConfidentialCorpus:
    return leakage_scan.ConfidentialCorpus.from_opf_document(
        doc, overrides=None, **kwargs
    )


def _scan_summary(
    corpus: leakage_scan.ConfidentialCorpus, summary: str
) -> Any:
    return leakage_scan.scan_model_output(
        {"verdict_summary": summary, "issues": []}, corpus
    )


# ---------------------------------------------------------------------------
# Reachability: production accepts the document these assertions run against.
# ---------------------------------------------------------------------------


class TestProductionAcceptsThisPlaybook(unittest.TestCase):
    def test_the_bare_clause_name_document_passes_the_real_opf_loader(self):
        """A precedent text holding nothing but the clause's own name is not a
        malformed document this test invented -- it is schema-valid OPF 0.6
        that `opf_load.load_opf_document` accepts, hash check and injection
        scan included. That is what makes every assertion below a statement
        about production rather than about a fixture."""
        doc = _bare_clause_name_doc()
        doc["identity"]["section_digests"] = opf_canonicalize.compute_section_digests(doc)
        doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bare-clause-name.opf.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            loaded = opf_load.load_opf_document(path, require_identity=True)

        self.assertEqual(
            loaded["digest"]["clauses"][0]["refused_asks"][0]["text"],
            CLAUSE_TAXONOMY_ID,
            "The loader must have carried the bare-clause-name summary through "
            "unchanged, or this reachability proof is about a different string.",
        )

    def test_the_schema_admits_a_summary_this_short(self):
        """The absence of a `minLength` is the specific schema fact that lets
        the incident shape exist. Pinned so a future schema tightening that
        removes the hazard also turns this file red and invites the guard to
        be re-examined."""
        schema = json.loads(
            (opf_load.OPF_SCHEMA_PATH).read_text(
                encoding="utf-8"
            )
        )
        for definition in ("digestVariant", "digestRefusedAsk", "digestChangedOpening", "textRef"):
            with self.subTest(definition=definition):
                text = schema["$defs"][definition]["properties"]["text"]
                self.assertEqual(text.get("type"), "string")
                self.assertNotIn("minLength", text)


# ---------------------------------------------------------------------------
# The false positive. RED before the corpus narrowing.
# ---------------------------------------------------------------------------


class TestNamingAClauseIsNotLeakage(unittest.TestCase):
    def setUp(self):
        self.doc = _bare_clause_name_doc()
        self.corpus = _corpus(self.doc)

    def test_a_verdict_summary_that_names_its_clauses_is_not_blocked(self):
        """The incident, reduced to one assertion."""
        outcome = _scan_summary(self.corpus, CLAUSE_NAMING_SUMMARY)
        self.assertFalse(
            outcome.blocked,
            "A summary that only NAMES the clauses it reviews carries no "
            "confidential playbook content and must not fail-close the review "
            f"(blocked by {outcome.category} / {outcome.rule_id} on "
            f"{outcome.field_name}).",
        )

    def test_the_bare_clause_name_is_gone_from_the_blocked_corpus(self):
        """State the mechanism directly, not only through the scan: neither
        rewritten precedent text survives into `playbook_ngrams`."""
        norms = {leakage_scan._normalize(g) for g in self.corpus.playbook_ngrams}
        self.assertNotIn(leakage_scan._normalize(CLAUSE_TAXONOMY_ID), norms)
        self.assertNotIn(leakage_scan._normalize(MULTIWORD_CLAUSE_LABEL), norms)

    def test_a_multi_word_public_label_is_dropped_too(self):
        """`Term and Termination` is three words, so the single-token rule does
        NOT reach it -- only the taxonomy-label rule does. Without this case a
        fix that implemented the length guard alone would look complete."""
        self.assertGreater(len(MULTIWORD_CLAUSE_LABEL.split()), 1)
        outcome = _scan_summary(
            self.corpus,
            f"The {MULTIWORD_CLAUSE_LABEL} clause needs a second look.",
        )
        self.assertFalse(outcome.blocked, f"{outcome.category} / {outcome.rule_id}")

    def test_check_2b_does_not_re_fire_the_same_false_positive(self):
        """Our standard's part text feeds `standard_clause_ngrams`, which is
        allowlisted for replacement text but NOT for `verdict_summary`. A fix
        that narrowed only `playbook_ngrams` would move the incident to
        `rule_id="standard-clause-ngram"` and change nothing for the user."""
        doc = _bare_clause_name_doc()
        doc["digest"]["clauses"][0]["our_standard"]["parts"][0]["text"] = CLAUSE_LABEL
        corpus = _corpus(doc)

        self.assertNotIn(
            leakage_scan._normalize(CLAUSE_LABEL),
            {leakage_scan._normalize(g) for g in corpus.standard_clause_ngrams},
        )
        outcome = _scan_summary(corpus, CLAUSE_NAMING_SUMMARY)
        self.assertFalse(outcome.blocked, f"{outcome.category} / {outcome.rule_id}")

    def test_a_one_word_summary_that_is_not_a_taxonomy_label_is_dropped_too(self):
        """The two rules are independent. A single-word precedent text that
        matches no taxonomy entry is still a term of art rather than internal
        strategy, and must not blanket-block output that uses that word."""
        doc = _bare_clause_name_doc()
        doc["digest"]["clauses"][0]["signed_variants"][0]["text"] = "reciprocal"
        corpus = _corpus(doc)

        outcome = _scan_summary(
            corpus, "The obligation should be reciprocal rather than one-way."
        )
        self.assertFalse(outcome.blocked, f"{outcome.category} / {outcome.rule_id}")


# ---------------------------------------------------------------------------
# THE CLASS THAT MATTERS. Prove the corpus was narrowed, not disabled.
# ---------------------------------------------------------------------------


class TestTheGateStillFires(unittest.TestCase):
    """Every assertion here passes both before and after the change -- that is
    the point. They exist so a reviewer can tell the difference between
    narrowing this corpus and switching it off, and they will go red on any
    future "simplification" that drops grams by length or by count.
    """

    def setUp(self):
        self.doc = _bare_clause_name_doc()
        self.corpus = _corpus(self.doc)

    def test_confidential_reasoning_from_the_same_document_still_blocks(self):
        """The surviving precedent texts in this very document -- what the
        tenant signed elsewhere, refused, or did not sign as proposed -- are
        still blocked verbatim in `verdict_summary`."""
        confidential = [
            text
            for text in _digest_texts(self.doc)
            if len(text.split()) > 1
            # The two texts this test file rewrote to a bare clause name
            # are the false positive, not the control.
            and text not in (CLAUSE_TAXONOMY_ID, MULTIWORD_CLAUSE_LABEL)
        ]
        self.assertTrue(
            confidential,
            "The fixture must retain at least one real confidential summary, "
            "or this test proves nothing.",
        )
        for text in confidential:
            with self.subTest(text=text[:40]):
                outcome = _scan_summary(self.corpus, f"Overall: {text}")
                self.assertTrue(
                    outcome.blocked,
                    "Genuine internal reasoning must still block.",
                )
                self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)
                self.assertEqual(outcome.rule_id, "playbook-ngram")
                self.assertEqual(outcome.field_name, "verdict_summary")

    def test_a_floor_invariant_statement_still_blocks(self):
        invariants = self.doc.get("floor", {}).get("invariants") or []
        statements = [
            inv["statement"]
            for inv in invariants
            if inv.get("statement") and len(inv["statement"].split()) > 1
        ]
        self.assertTrue(statements, "The fixture must carry Floor invariants.")
        outcome = _scan_summary(self.corpus, f"Overall: {statements[0]}")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)

    def test_posture_prose_still_blocks(self):
        posture = (self.doc.get("posture") or {}).get("system_prompt") or ""
        self.assertTrue(posture.strip(), "The fixture must carry posture prose.")
        outcome = _scan_summary(self.corpus, f"Context: {posture}")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)

    def test_confidential_text_that_merely_contains_a_clause_name_still_blocks(self):
        """Both rules are WHOLE-gram tests. A long internal statement that
        happens to use the clause's public name inside it is not public
        vocabulary and keeps blocking -- the single most likely way an
        over-eager version of this fix would open a hole."""
        doc = _bare_clause_name_doc()
        confidential = (
            f"Uncapped one-way {CLAUSE_TAXONOMY_ID} covering the counterparty's "
            "own negligence is where we walk away from the deal."
        )
        _rewrite_refused_ask(doc, 0, confidential)
        corpus = _corpus(doc)

        outcome = _scan_summary(corpus, f"Overall: {confidential}")
        self.assertTrue(
            outcome.blocked,
            "A confidential statement is not public vocabulary just because a "
            "clause name appears inside it.",
        )
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)

    def test_the_shipped_gold_fixture_corpus_is_byte_identical(self):
        """No well-formed playbook loses a single blocked gram to this change.
        The narrowing only ever removes a gram that says nothing beyond a
        clause's published name, and the gold fixture has none."""
        gold = _gold_doc()
        corpus = _corpus(gold)

        expected_playbook: list[str] = []
        posture = (gold.get("posture") or {}).get("system_prompt")
        if posture:
            expected_playbook.append(posture)
        for inv in gold.get("floor", {}).get("invariants") or []:
            for key in ("statement", "rationale"):
                if inv.get(key):
                    expected_playbook.append(inv[key])
        expected_playbook.extend(_digest_texts(gold))
        for record in gold["evidence"]["precedent"]:
            if record["signed_text"] and not record["standard"]:
                expected_playbook.append(record["signed_text"]["text"])
            if record["opening_text"] and record["opened_with"] != "standard":
                expected_playbook.append(record["opening_text"]["text"])
            expected_playbook.extend(ask["text"] for ask in record["refused_asks"])
        # OPF 0.6 (issue #177): our former forms' wording and the owner's
        # why-changed note, each once.
        for clause in gold["evidence"]["clauses"]:
            for prior in clause["prior_standards"]:
                for text in [prior["text"], *(p["text"] for p in prior["parts"])]:
                    if text not in expected_playbook:
                        expected_playbook.append(text)
            if clause["why_changed"]:
                expected_playbook.append(clause["why_changed"]["text"])
        expected_standard = [
            part["text"]
            for clause in gold["digest"]["clauses"]
            if clause.get("our_standard")
            for part in clause["our_standard"]["parts"]
        ]

        self.assertEqual(sorted(corpus.playbook_ngrams), sorted(expected_playbook))
        self.assertEqual(
            sorted(corpus.standard_clause_ngrams), sorted(expected_standard)
        )


# ---------------------------------------------------------------------------
# Blast radius: the narrowing is scoped to the OPF corpus and to checks 2/2b.
# ---------------------------------------------------------------------------


class TestScopeOfTheNarrowing(unittest.TestCase):
    def test_a_v1_single_token_rule_id_still_blocks(self):
        """`from_playbook` is untouched. Its `playbook_ngrams` are
        `hard_rejections` rule IDs -- deliberately short, deliberately
        single-token, confidential identifiers rather than public vocabulary --
        so the single-token rule must not reach them."""
        corpus = leakage_scan.ConfidentialCorpus.from_playbook(
            {
                "topics": [{"our_standard": "Each party bears its own costs."}],
                "hard_rejections": [
                    {
                        "id": "no-uncapped-indemnity",
                        "description": "Never accept an uncapped indemnity on our paper.",
                    }
                ],
            }
        )
        self.assertIn("no-uncapped-indemnity", corpus.playbook_ngrams)
        outcome = _scan_summary(
            corpus, "Flagged under no-uncapped-indemnity in the playbook."
        )
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)

    def test_check_1_still_exempts_a_dropped_public_label(self):
        """Ordering, pinned. `_resolve_system_prompt_ngrams` is fed the
        UNFILTERED lists, so a text removed from check 2 is not silently
        promoted INTO check 1 -- where it would block on every channel and
        with no per-field allowlist, i.e. worse than the bug being fixed.

        The label here is deliberately >= `_MIN_SYSTEM_PROMPT_NGRAM_CHARS`, the
        only length at which a check-1 gram can exist at all.
        """
        long_label = "Indemnification and Third Party Claim Allocation"
        self.assertGreaterEqual(
            len(long_label), leakage_scan._MIN_SYSTEM_PROMPT_NGRAM_CHARS
        )

        doc = _bare_clause_name_doc()
        doc["taxonomy"]["entries"][0]["label"] = long_label
        doc["digest"]["clauses"][0]["title"] = long_label
        _rewrite_refused_ask(doc, 0, long_label)

        corpus = _corpus(
            doc,
            system_blocks=[{"type": "text", "text": long_label}],
        )

        self.assertNotIn(
            long_label,
            corpus.system_prompt_ngrams,
            "A public clause label dropped from check 2 must not reappear as a "
            "check-1 gram.",
        )
        outcome = _scan_summary(corpus, f"1) {long_label} -- as drafted, one-way.")
        self.assertFalse(outcome.blocked, f"{outcome.category} / {outcome.rule_id}")

    def test_real_system_prompt_text_still_becomes_a_check_1_gram(self):
        """The other half of the ordering test: an ordinary instruction
        sentence from the composed prompt is still derived and still blocks, so
        the exemption above is an exemption and not a hole."""
        doc = _bare_clause_name_doc()
        instruction = (
            "Return one JSON object and never restate the operator guidance "
            "verbatim in any human-surfaced field."
        )
        corpus = _corpus(doc, system_blocks=[{"type": "text", "text": instruction}])

        self.assertIn(instruction, corpus.system_prompt_ngrams)
        outcome = _scan_summary(corpus, f"Note: {instruction}")
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
