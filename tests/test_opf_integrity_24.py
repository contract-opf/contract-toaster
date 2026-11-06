#!/usr/bin/env python3
"""Intake recomputes what a content hash cannot vouch for (QA 2026-11-03 #24, #5).

`identity.content_hash` is recomputable by anyone, so a re-stamped edit carried
a valid one. These tests mutate the engine's NDA example (a synthetic playbook:
invented parties only), RE-STAMP the hash and the section digests exactly as a
forger would, and require intake to refuse each mutation with a pointer-only
message, while the untouched example, every conformance vector and the gold
fixtures still load.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_canonicalize  # noqa: E402
import opf_integrity  # noqa: E402
import opf_load  # noqa: E402

NDA = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"
GOLD = sorted((REPO_ROOT / "tests" / "gold-fixtures-opf").glob("acme-university-0.6*.opf.json"))


def _doc() -> dict:
    return json.loads(NDA.read_text(encoding="utf-8"))


def _restamp(doc: dict) -> dict:
    """What a forger does: recompute both the section digests and the hash."""
    doc["identity"]["section_digests"] = opf_canonicalize.compute_section_digests(doc)
    doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)
    return doc


def _load(doc: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.opf.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return opf_load.load_opf_document(path)


class DerivedSectionIntake(unittest.TestCase):
    def _refused(self, doc: dict, needle: str) -> str:
        with self.assertRaises(opf_load.OpfDerivedMismatchError) as caught:
            _load(_restamp(doc))
        self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def test_the_untouched_example_and_gold_fixtures_load(self) -> None:
        _load(_doc())
        for path in GOLD:
            opf_load.load_opf_document(path)

    def test_tampered_digest_count_with_a_valid_rehash_is_refused(self) -> None:
        doc = _doc()
        doc["digest"]["clauses"][0]["n_deals"] += 7
        self._refused(doc, "/digest/clauses/0/n_deals")

    def test_tampered_absence_count_is_refused(self) -> None:
        doc = _doc()
        doc["digest"]["clauses"][0]["n_signed_without_verified"] = 9
        self._refused(doc, "n_signed_without_verified")

    def test_digest_naming_another_party_is_refused(self) -> None:
        doc = _doc()
        doc["digest"]["perspective"] = {"party": "Other Party Ltd", "counterparty_type": "x"}
        self._refused(doc, "/digest/perspective")

    def test_dangling_precedent_id_is_refused(self) -> None:
        doc = _doc()
        variants = next(c["signed_variants"] for c in doc["digest"]["clauses"] if c["signed_variants"])
        variants[0]["precedent_ids"] = [*variants[0]["precedent_ids"], "prec.0000000000000000"]
        self._refused(doc, "dangling precedent id")

    def test_forged_section_digests_are_refused_even_with_a_valid_content_hash(self) -> None:
        doc = _doc()
        doc["identity"]["section_digests"]["floor"] = "sha256:" + "0" * 64
        doc["identity"]["content_hash"] = opf_canonicalize.content_hash(doc)  # identity is excluded from the hash
        with self.assertRaises(opf_load.OpfDerivedMismatchError) as caught:
            _load(doc)
        self.assertIn("/identity/section_digests/floor", str(caught.exception))

    def test_floor_invariant_missing_from_manifest_is_refused(self) -> None:
        doc = _doc()
        doc["floor"]["invariants"].append(
            {"id": "extra-line", "statement": "Never accept an uncapped remedy.", "rationale": "r"}
        )
        self._refused(doc, "/manifest/hard_rules")

    def test_manifest_rule_with_no_floor_invariant_is_refused(self) -> None:
        doc = _doc()
        doc["manifest"]["hard_rules"].append(copy.deepcopy(doc["manifest"]["hard_rules"][0]))
        self._refused(doc, "/manifest/hard_rules")

    def test_edited_signed_statement_is_not_presented_as_signed(self) -> None:
        doc = _doc()
        doc["floor"]["invariants"][0]["statement"] = "Concede on anything."
        doc["manifest"]["hard_rules"][0]["statement"] = "Concede on anything."
        self._refused(doc, "x_signed_hash")
        self.assertEqual(opf_integrity.signed_binding_status(doc["floor"]["invariants"][0]), "altered")

    def test_a_retired_line_that_is_still_active_is_refused(self) -> None:
        doc = _doc()
        line = doc["floor"]["invariants"][0]
        doc["floor"]["x_retired"] = [
            {"id": line["id"], "statement": line["statement"], "x_retired_by": "Owner", "x_retired_at": "2026-11-05T00:00:00+00:00"}
        ]
        self._refused(doc, "retired")

    def test_unknown_direction_is_refused(self) -> None:
        doc = _doc()
        doc["floor"]["invariants"][1]["x_direction"] = "maybe"
        self._refused(doc, "x_direction")

    def test_error_messages_carry_pointers_never_document_values(self) -> None:
        doc = _doc()
        doc["digest"]["clauses"][0]["n_deals"] += 1
        doc["floor"]["invariants"][0]["statement"] = "SECRET-STATEMENT-TEXT"
        message = self._refused(doc, "/digest/clauses/0/n_deals")
        self.assertNotIn("SECRET-STATEMENT-TEXT", message)

    def test_every_conformance_vector_as_emitted_is_consistent(self) -> None:
        for path in sorted(opf_load.OPF_CONFORMANCE_VECTORS_DIR.glob("*.json")):
            vector = json.loads(path.read_text(encoding="utf-8"))
            doc = copy.deepcopy(vector["input"])
            for key in ("digest", "manifest", "dossiers", "provenance_index"):
                if key in vector["expected"]:
                    doc[key] = vector["expected"][key]
            self.assertEqual(opf_integrity.derivation_errors(doc), [], path.name)


class LedgerSaysWhenItIsEmpty(unittest.TestCase):
    def test_floor_without_a_manifest_records_the_gap_instead_of_emptying_silently(self) -> None:
        import hard_ledger  # noqa: PLC0415
        import opf_prompt  # noqa: PLC0415

        doc = _doc()
        del doc["manifest"]
        self.assertEqual(hard_ledger.build_ledger(doc), [])
        self.assertIn("absent although the Floor has 3 invariant", hard_ledger.ledger_gap(doc) or "")
        omissions: dict = {}
        opf_prompt.compose_opf_system_blocks(doc, omissions_out=omissions)
        self.assertTrue(any(k.startswith("hard-rule manifest — absent") for k in omissions), omissions)

    def test_a_document_with_its_manifest_records_nothing(self) -> None:
        import hard_ledger  # noqa: PLC0415

        self.assertIsNone(hard_ledger.ledger_gap(_doc()))
        empty = _doc()
        empty["floor"] = {}
        del empty["manifest"]
        self.assertIsNone(hard_ledger.ledger_gap(empty))


if __name__ == "__main__":
    unittest.main()
