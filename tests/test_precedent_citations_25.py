#!/usr/bin/env python3
"""A citation the model writes is checked against what it was shown (QA 2026-11-03 #25).

Synthetic playbook only (the engine's NDA example). Aliases, versions and
clause paths are the only things read or written; no precedent text.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import precedent_citations as pc  # noqa: E402

NDA = REPO_ROOT / "tests" / "fixtures" / "opf" / "engine-nda-example.opf.json"


def _doc() -> dict:
    return json.loads(NDA.read_text(encoding="utf-8"))


class CitationCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _doc()
        self.shown = pc.shown_refs(self.doc)
        alias, version, path = sorted(self.shown)[0]
        self.real = f"[{alias}@{version} §{path}]"

    def test_a_citation_the_digest_showed_resolves_and_is_listed(self) -> None:
        status, refs = pc.check_citation(f"Signed in a similar form {self.real}", self.shown)
        self.assertEqual(status, pc.STATUS_RESOLVED)
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].startswith("deal-"))

    def test_an_invented_alias_is_unresolved(self) -> None:
        self.assertEqual(pc.check_citation("[deal-99@1 §3]", self.shown)[0], pc.STATUS_UNRESOLVED)

    def test_a_real_alias_at_a_location_never_shown_is_unresolved(self) -> None:
        alias = sorted(self.shown)[0][0]
        self.assertEqual(pc.check_citation(f"[{alias}@9 §999]", self.shown)[0], pc.STATUS_UNRESOLVED)

    def test_one_bad_token_spoils_the_citation(self) -> None:
        self.assertEqual(pc.check_citation(f"{self.real} and [deal-99@1 §3]", self.shown)[0], pc.STATUS_UNRESOLVED)

    def test_text_with_no_citation_in_it_is_unresolved_and_empty_is_none(self) -> None:
        self.assertEqual(pc.check_citation("we signed this before", self.shown)[0], pc.STATUS_UNRESOLVED)
        self.assertEqual(pc.check_citation(None, self.shown)[0], pc.STATUS_NONE)
        self.assertEqual(pc.check_citation("  ", self.shown)[0], pc.STATUS_NONE)

    def test_annotate_strips_unresolved_lists_resolved_and_never_mutates(self) -> None:
        findings = [
            {"section_ref": "1", "internal_precedent_citation": self.real},
            {"section_ref": "2", "internal_precedent_citation": "[deal-99@1 §3]"},
            {"section_ref": "3", "internal_precedent_citation": None},
        ]
        out, counts = pc.annotate_findings(findings, self.doc)
        self.assertEqual(counts, {"checked": 2, "resolved": 1, "unresolved": 1})
        self.assertEqual(out[0]["precedent_citation_status"], pc.STATUS_RESOLVED)
        self.assertEqual(out[0]["precedent_basis"], [self.real[1:-1]])
        self.assertEqual(out[1]["precedent_citation_status"], pc.STATUS_UNRESOLVED)
        self.assertIsNone(out[1]["internal_precedent_citation"])
        self.assertNotIn("precedent_basis", out[1])
        self.assertEqual(out[2]["precedent_citation_status"], pc.STATUS_NONE)
        self.assertEqual(findings[1]["internal_precedent_citation"], "[deal-99@1 §3]")  # input untouched

    def test_a_review_without_a_digest_is_left_alone(self) -> None:
        findings = [{"internal_precedent_citation": "[x@1 §1]"}]
        out, counts = pc.annotate_findings(findings, None)
        self.assertIs(out, findings)
        self.assertEqual(counts["checked"], 0)

    def test_the_basis_carries_aliases_only_never_a_deal_id(self) -> None:
        deal_ids = {d["document_id"] for d in self.doc["corpus"]["documents"]}
        out, _ = pc.annotate_findings([{"internal_precedent_citation": self.real}], self.doc)
        self.assertFalse(any(d in ref for d in deal_ids for ref in out[0]["precedent_basis"]))


if __name__ == "__main__":
    unittest.main()
