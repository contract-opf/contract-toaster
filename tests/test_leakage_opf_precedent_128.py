#!/usr/bin/env python3
"""
Issue #128 (item 6): the OPF leakage corpus reads digest 4 and the per-deal
precedent record, so it is never empty on an OPF 0.5 playbook.

`leakage_scan.ConfidentialCorpus.from_opf_document` used to read digest 2's
`concessions` / `unacceptable` / `exemplar_forms` / `preferred_variations` and
OPF 0.3's `evidence.clause_library`. An OPF 0.5 document carries none of them,
so on the real playbook's shape (`posture == {}`, `floor == {}`) the corpus
would have been EMPTY -- the failure mode #104 exists to prevent, arriving by a
format change instead of a lookup failure. This pins the replacement:

  1. On the real playbook's shape, the corpus is not empty: precedent texts
     alone populate `playbook_ngrams`.
  2. Every precedent text the digest carries into the prompt -- a signed
     variant, the collapsed equivalent entry's exemplar, a refused ask, an
     opening not signed as proposed -- blocks verbatim in `verdict_summary`.
  3. The per-deal record is read too: a signed text the capped digest does
     not list still blocks (conformance vector 003, whose digest lists 5 of
     7 signed variants).
  4. Our standard is allowlisted, never blocked: its parts are
     `standard_clause_ngrams`, a faithful restoration in replacement text
     passes, and neither a signed text that IS our standard (`standard: true`)
     nor an opening that opened with it (`opened_with: "standard"`) lands in
     `playbook_ngrams`.
  5. A `vs_standard` reason is not blocked content: it describes a difference
     from our standard in words a faithful review may use.
  6. The derived `manifest` and `dossiers` sections (playbook-engine#228) are
     covered: every text field they carry is already in the corpus (or is
     our standard, allowlisted part by part, or public clause vocabulary),
     except the deliberate exclusions `from_opf_document`'s docstring names.
     Both are also read directly now that both reach a prompt: the manifest
     since issue #163 (`tests/test_manifest_shared_prefix_163.py` pins it),
     the dossiers since issue #40 (`tests/test_dossier_selection_40.py`).

Fixture reachability: every document here is unmutated engine output -- a
committed gold OPF 0.5 fixture generated with the engine's reference
functions (tests/gold-fixtures-opf/generate_opf_fixture.py), the engine's own
public NDA example, or a vendored conformance vector assembled the way the
engine emits it (its `input` plus its `expected` derived sections). The
capped state in (3) is the engine's own: vector 003 has seven distinct signed
variants and the digest's cap lists five.

Run standalone: `python3 tests/test_leakage_opf_precedent_128.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import leakage_scan  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "gold-fixtures-opf"
FULL = FIXTURES / "acme-university-0.5.opf.json"
REAL_SHAPE = FIXTURES / "acme-university-0.5-real-shape.opf.json"
NDA_EXAMPLE = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"
VECTORS = REPO_ROOT / "playbooks" / "opf" / "conformance-0.5" / "vectors"
CAPPED_VECTOR = VECTORS / "003-cap-totals-and-summary.json"
DOSSIER_VECTOR = VECTORS / "008-hard-rules-and-dossiers.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _vector_doc(path: Path) -> dict[str, Any]:
    """A conformance vector as the engine emits it: input + expected derived
    sections (the same assembly tests/test_opf_load.py check 7 validates)."""
    vector = _load(path)
    doc = copy.deepcopy(vector["input"])
    for section in ("digest", "manifest", "dossiers", "provenance_index"):
        doc[section] = vector["expected"][section]
    return doc


def _corpus(doc: dict[str, Any]) -> leakage_scan.ConfidentialCorpus:
    return leakage_scan.ConfidentialCorpus.from_opf_document(doc, overrides=None)


def _scan(corpus: leakage_scan.ConfidentialCorpus, text: str, *, replacement: bool = False):
    return leakage_scan.LeakageScanner(corpus).scan(
        text, field_name="verdict_summary", is_replacement_text=replacement
    )


def _clause(doc: dict[str, Any], clause_id: str) -> dict[str, Any]:
    return next(c for c in doc["digest"]["clauses"] if c["id"] == clause_id)


class TestCorpusIsNeverEmpty(unittest.TestCase):
    def test_the_real_playbook_shape_still_has_a_corpus(self):
        doc = _load(REAL_SHAPE)
        self.assertEqual(doc["posture"], {})
        self.assertEqual(doc["floor"], {})
        corpus = _corpus(doc)
        self.assertTrue(corpus.playbook_ngrams, "precedent texts must populate the corpus")
        self.assertTrue(corpus.standard_clause_ngrams, "our standard must populate the allowlist")

    def test_the_engine_example_has_a_corpus(self):
        corpus = _corpus(_load(NDA_EXAMPLE))
        self.assertGreater(len(corpus.playbook_ngrams), 10)


class TestDigestPrecedentBlocks(unittest.TestCase):
    def setUp(self):
        self.doc = _load(FULL)
        self.corpus = _corpus(self.doc)

    def _assert_blocks(self, text: str) -> None:
        outcome = _scan(self.corpus, f"Overall: {text}")
        self.assertTrue(outcome.blocked, text[:60])
        self.assertEqual(outcome.category, leakage_scan.CATEGORY_PLAYBOOK)
        self.assertEqual(outcome.rule_id, "playbook-ngram")

    def test_a_signed_variant_blocks(self):
        self._assert_blocks(_clause(self.doc, "clause.governing-law")["signed_variants"][0]["text"])

    def test_the_collapsed_equivalent_exemplar_blocks(self):
        entry = next(
            e for e in _clause(self.doc, "clause.confidentiality")["signed_variants"] if "exemplars" in e
        )
        self._assert_blocks(entry["exemplars"][0]["text"])

    def test_a_refused_ask_and_an_unsigned_opening_block(self):
        clause = _clause(self.doc, "clause.indemnification")
        self._assert_blocks(clause["refused_asks"][0]["text"])
        self._assert_blocks(clause["changed_openings"][0]["text"])

    def test_the_precedent_record_is_read_beyond_a_capped_digest(self):
        doc = _vector_doc(CAPPED_VECTOR)
        clause = _clause(doc, "clause.limitation_of_liability")
        listed = [entry["text"] for entry in clause["signed_variants"]]
        self.assertEqual(len(listed), 5, "vector 003's digest lists the top 5 signed variants")
        self.assertEqual(clause["n_variants_total"], 7)
        # A listed entry's text is the representative's text or its verbatim
        # sentence-boundary summary, so a record is listed when its signed
        # text starts with a listed text.
        unlisted = [
            record["signed_text"]["text"]
            for record in doc["evidence"]["precedent"]
            if not any(record["signed_text"]["text"].startswith(text) for text in listed)
        ]
        self.assertEqual(len(unlisted), 2, "the cap leaves two signed variants only in the record")
        corpus = _corpus(doc)
        for text in unlisted:
            outcome = _scan(corpus, f"Overall: {text}")
            self.assertTrue(outcome.blocked, f"the per-deal record must keep {text!r} in the corpus")
            self.assertEqual(outcome.rule_id, "playbook-ngram")


class TestOurStandardIsAllowlisted(unittest.TestCase):
    def setUp(self):
        self.doc = _load(FULL)
        self.corpus = _corpus(self.doc)
        self.standard = _clause(self.doc, "clause.governing-law")["our_standard"]["parts"][0]["text"]

    def test_our_standard_is_a_standard_clause_gram_not_a_playbook_gram(self):
        self.assertIn(self.standard, self.corpus.standard_clause_ngrams)
        norms = {leakage_scan._normalize(g) for g in self.corpus.playbook_ngrams}
        self.assertNotIn(leakage_scan._normalize(self.standard), norms)

    def test_restoring_our_standard_in_replacement_text_passes(self):
        outcome = _scan(self.corpus, self.standard, replacement=True)
        self.assertFalse(outcome.blocked, f"{outcome.category} / {outcome.rule_id}")

    def test_standard_texts_of_the_record_stay_out_of_the_blocked_corpus(self):
        records = self.doc["evidence"]["precedent"]
        standard_signed = [r["signed_text"]["text"] for r in records if r["standard"]]
        standard_openings = [
            r["opening_text"]["text"] for r in records if r["opened_with"] == "standard" and r["opening_text"]
        ]
        self.assertTrue(standard_signed and standard_openings, "fixture must carry both shapes")
        blocked = set(self.corpus.playbook_ngrams)
        for text in standard_signed + standard_openings:
            self.assertNotIn(text, blocked)


class TestAReasonIsNotBlockedContent(unittest.TestCase):
    def test_a_judged_reason_does_not_block(self):
        doc = _load(FULL)
        reason = _clause(doc, "clause.governing-law")["signed_variants"][0]["reason"]
        self.assertTrue(reason)
        outcome = _scan(_corpus(doc), f"This draft {reason[0].lower()}{reason[1:]}")
        self.assertFalse(outcome.blocked, f"{outcome.category} / {outcome.rule_id}")


def _derived_section_texts(doc: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Every text field of the manifest and the dossiers that the corpus is
    meant to cover, as (field, pointer, text). The deliberate exclusions named in
    `from_opf_document`'s docstring are not collected: `vs_standard`
    (reasons, part reasons, addition summaries), each hard rule's
    `condition` and `permissible_proof`, and the identifiers/titles."""
    texts: list[tuple[str, str, str]] = []
    for index, rule in enumerate((doc.get("manifest") or {}).get("hard_rules") or []):
        for key in ("statement", "fallback_language"):
            if rule.get(key):
                texts.append((f"manifest.{key}", f"manifest.hard_rules[{index}].{key}", rule[key]))
    for clause_id, dossier in (doc.get("dossiers") or {}).items():
        where = f"dossiers[{clause_id}]"
        for part in (dossier.get("our_standard") or {}).get("parts") or []:
            texts.append(("dossiers.our_standard", f"{where}.our_standard", part["text"]))
        for rule in dossier.get("floor_rules") or []:
            for key in ("statement", "rationale"):
                if rule.get(key):
                    texts.append((f"dossiers.floor_rules.{key}", f"{where}.floor_rules.{key}", rule[key]))
        for excerpt in dossier.get("excerpts") or []:
            for key in ("opening", "signed"):
                if excerpt.get(key):
                    pointer = f"{where}.excerpts[{excerpt['precedent_id']}].{key}"
                    texts.append((f"dossiers.excerpts.{key}", pointer, excerpt[key]))
    return texts


class TestDerivedSectionsAreCoveredByDerivation(unittest.TestCase):
    """Issue #128's "Scans" bullet: the leakage corpus covers the text fields
    of the engine #228 manifest and dossiers. On engine output they are
    selections of the Floor, our standard and the per-deal record, all of
    which the corpus reads -- and this pins that on real engine output. (The
    manifest is also read directly since issue #163, the dossiers since
    issue #40.)"""

    def _assert_covered(self, doc: dict[str, Any]) -> int:
        corpus = _corpus(doc)
        playbook = set(corpus.playbook_ngrams)
        standard = set(corpus.standard_clause_ngrams)
        vocabulary = leakage_scan._opf_public_vocabulary(doc)
        joined_standards = {
            "\n\n".join(part["text"] for part in (clause.get("our_standard") or {}).get("parts") or [])
            for clause in doc["digest"]["clauses"]
        }
        texts = _derived_section_texts(doc)
        for _field, pointer, text in texts:
            covered = (
                text in playbook
                or text in standard
                # our standard of several parts, each part allowlisted
                or text in joined_standards
                # public clause vocabulary / a sub-minimum gram (#616)
                or not leakage_scan._without_public_vocabulary([text], vocabulary)
            )
            self.assertTrue(covered, f"{pointer} is in neither the corpus nor the allowlist")
        return len(texts)

    def test_the_gold_fixture(self):
        self.assertGreater(self._assert_covered(_load(FULL)), 0)

    def test_the_engine_example(self):
        self.assertGreater(self._assert_covered(_load(NDA_EXAMPLE)), 0)

    def test_every_conformance_vector(self):
        for path in sorted(VECTORS.glob("*.json")):
            with self.subTest(vector=path.name):
                self._assert_covered(_vector_doc(path))

    def test_vector_008_exercises_every_field(self):
        fields = {field for field, _pointer, _text in _derived_section_texts(_vector_doc(DOSSIER_VECTOR))}
        self.assertEqual(
            fields,
            {
                "manifest.statement",
                "manifest.fallback_language",
                "dossiers.our_standard",
                "dossiers.floor_rules.statement",
                "dossiers.floor_rules.rationale",
                "dossiers.excerpts.opening",
                "dossiers.excerpts.signed",
            },
        )

    def test_a_floor_rule_and_a_dossier_excerpt_block(self):
        doc = _vector_doc(DOSSIER_VECTOR)
        corpus = _corpus(doc)
        rule = doc["manifest"]["hard_rules"][0]["statement"]
        excerpt = doc["dossiers"]["clause.assignment"]["excerpts"][1]
        self.assertEqual(excerpt["kind"], "refused_ask")
        for text in (rule, excerpt["opening"]):
            outcome = _scan(corpus, f"Overall: {text}")
            self.assertTrue(outcome.blocked, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
