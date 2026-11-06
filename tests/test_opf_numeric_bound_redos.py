#!/usr/bin/env python3
"""
An OPF `numeric_bound` pattern cannot stall the hard ledger (QA 2026-10-31
report item 12).

WHY THIS GATE EXISTS
--------------------
Playbooks are untrusted input, and `scripts/hard_ledger.py::evaluate_predicate`
compiles a `numeric_bound` rule's `pattern` straight from the playbook and runs
it over clause text. The v1 ReDoS gate (docs/playbook-governance.md
"Regex-dialect and ReDoS constraint (CI)") covered only v1 `trigger_terms`, and
only in CI. A realistic pattern such as `\\$((?:\\d+,?)+) in the aggregate`
backtracks exponentially on a long run of digits -- seconds at two dozen,
quadrupling every two more -- and Python's `re` cannot be interrupted, so one
clause could hold a worker indefinitely.

  A. The shared structural check (`scripts/regex_safety.py`, also what the v1
     CI gate runs) flags the nested-quantifier forms, including a group whose
     inner quantifier is not its last token, and passes the plain capture
     patterns real playbooks use.
  B. `opf_load` refuses, at load/upload time, a document carrying a
     structurally dangerous, uncompilable, or wrong-group-count
     `numeric_bound` pattern -- with a JSON Pointer and without echoing the
     pattern.
  C. At run time the pattern executes under a wall-clock bound: a pattern
     that slips past the static check returns None (fails closed) within the
     budget instead of running for minutes, and a safe one still decides.

Run: python3 tests/test_opf_numeric_bound_redos.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import copy
import json
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hard_ledger  # noqa: E402
import opf_integrity  # noqa: E402
import opf_load  # noqa: E402

VECTOR_008 = opf_load.OPF_CONFORMANCE_VECTORS_DIR / "008-hard-rules-and-dossiers.json"

#: The report's example: exponential on a long digit run.
EVIL = r"\$((?:\d+,?)+) in the aggregate"
EVIL_INPUT = "$" + "1" * 27 + " in total"

DANGEROUS = [
    EVIL,
    r"((a+)+)",
    r"((\d*)+)",
    r"((?:\w+\s?)*) years",
    r"((?:a|ab)+)",
    r"(x{2,})+(\d)",
]
SAFE = [
    r"([0-9]+) years",
    r"\$([0-9,]+)",
    r"\(([0-9]+)\) years",
    r"up to ([0-9]{1,3}(?:,[0-9]{3})*) dollars",
    r"(\d+(?:\.\d+)?) per cent",
]


def _doc_with_pattern(pattern: str) -> dict:
    vector = json.loads(VECTOR_008.read_text(encoding="utf-8"))
    doc = copy.deepcopy(vector["input"])
    for section in ("digest", "manifest", "dossiers", "provenance_index"):
        doc[section] = vector["expected"][section]
    doc.pop("identity", None)
    condition = {"type": "numeric_bound", "pattern": pattern, "max": 1000000, "unit": "usd"}
    # One Floor line carries the bound; the manifest is then derived from it
    # (intake recomputes the manifest from the Floor, QA 2026-11-03 #24), and
    # the vector's dossiers, which name other Floor rules, are dropped.
    doc["floor"] = {
        "invariants": [
            {
                "id": "r-bound",
                "statement": "Liability is capped.",
                "x_taxonomy_id": "term",
                "x_required_presence": True,
                "x_condition": condition,
            }
        ]
    }
    doc["dossiers"] = {}
    doc["manifest"] = {"hard_rules": opf_integrity._expected_hard_rules(doc)}
    # The Floor carries the same pattern; keep the manifest first in document
    # order so the refusal still names the manifest pointer this suite asserts.
    doc["floor"] = doc.pop("floor")
    return doc


class TestStructuralCheck(unittest.TestCase):
    def test_a_dangerous_patterns_are_flagged(self) -> None:
        import regex_safety  # noqa: PLC0415

        for pattern in DANGEROUS:
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(regex_safety.structural_hazard(pattern))

    def test_a_safe_patterns_pass(self) -> None:
        import regex_safety  # noqa: PLC0415

        for pattern in SAFE:
            with self.subTest(pattern=pattern):
                self.assertIsNone(regex_safety.structural_hazard(pattern))


class TestLoadTimeRefusal(unittest.TestCase):
    def test_b_dangerous_pattern_is_refused_without_echoing_it(self) -> None:
        for pattern in (EVIL, "([0-9]+", "[0-9]+ years", "([0-9]+) (years)"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(opf_load.OpfValidationError) as ctx:
                    opf_load._validate_doc(_doc_with_pattern(pattern), require_identity=False)
                message = str(ctx.exception)
                self.assertIn("/manifest/hard_rules/0/condition/pattern", message)
                self.assertNotIn(pattern, message)

    def test_b_safe_pattern_loads(self) -> None:
        for pattern in SAFE:
            with self.subTest(pattern=pattern):
                opf_load._validate_doc(_doc_with_pattern(pattern), require_identity=False)


#: Polynomial, not exponential, so the structural check passes it -- but at
#: ninety digits it runs for minutes in-process. Only the deadline stops it.
SLIPS_PAST = r"(\d+\d+\d+\d+\d+)!"
SLIPS_PAST_INPUT = "1" * 90 + "x"


class TestRuntimeBound(unittest.TestCase):
    def test_c_pattern_past_the_static_check_times_out_closed(self) -> None:
        import regex_safety  # noqa: PLC0415

        self.assertIsNone(regex_safety.structural_hazard(SLIPS_PAST))
        entry = {"condition": {"type": "numeric_bound", "pattern": SLIPS_PAST, "max": 10}}
        started = time.monotonic()
        result = hard_ledger.evaluate_predicate(entry, SLIPS_PAST_INPUT)
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, hard_ledger.NUMERIC_BOUND_TIMEOUT_S + 2.0)

    def test_c_evil_pattern_fails_closed_within_budget(self) -> None:
        entry = {"condition": {"type": "numeric_bound", "pattern": EVIL, "max": 10}}
        started = time.monotonic()
        result = hard_ledger.evaluate_predicate(entry, EVIL_INPUT)
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, hard_ledger.NUMERIC_BOUND_TIMEOUT_S + 2.0)

    def test_c_safe_pattern_still_decides(self) -> None:
        entry = {"condition": {"type": "numeric_bound", "pattern": r"([0-9]+) years", "min": 1, "max": 5}}
        self.assertTrue(hard_ledger.evaluate_predicate(entry, "This runs for 3 years."))
        self.assertFalse(hard_ledger.evaluate_predicate(entry, "This runs for 9 years."))
        self.assertFalse(hard_ledger.evaluate_predicate(entry, "No term is stated."))


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (TestStructuralCheck, TestLoadTimeRefusal, TestRuntimeBound):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
