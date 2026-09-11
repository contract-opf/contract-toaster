#!/usr/bin/env python3
"""
Executable tests for issue #55 (2026-09-05 audit finding F6, action A6):
`scripts/entity_normalize.py`.

Before this module, party recognition folded only case and whitespace, so a
roster entry `Synthetic Holdings GmbH` did not match `SYNTHETIC HOLDINGS
G.m.b.H.` in a document, `Synthetic, Inc.` did not match `Synthetic Inc`,
`Société` did not match `Societe`, and a `d/b/a` compound matched neither
half alone. This file is the table that pins the fold.

Table-driven over every case the issue enumerates:

  (1) `fold` — diacritics, `&` -> `and`, punctuation, whitespace, the
      leading article, and IDEMPOTENCE (`fold(fold(x)) == fold(x)`).
  (2) `strip_legal_form` — EVERY canonical suffix in `LEGAL_FORM_SUFFIXES`,
      under EVERY spelling the table lists, plus longest-match-wins
      (`pty ltd` over `ltd`) and the suffix-only name that must keep its
      suffix rather than strip to an empty core.
  (3) `split_dba` — every separator in `DBA_SEPARATORS`, the no-separator
      passthrough, and the word-boundary rule (a separator's letters
      INSIDE a word must not split it).
  (4) `recognition_key` — the pairs that must collide and the pairs that
      must NOT (the fold is lossy; it must not be so lossy that two real
      entities merge).
  (5) `recognition_variants` — content and DETERMINISTIC SORTED ORDER.

All names here are synthetic and invented (`tests/lint-brand-free.py` and
`tests/lint-counterparty-names.py` forbid a real tenant or counterparty
name anywhere in this tree).

This test MUST FAIL on the pre-fix tree (`ModuleNotFoundError:
entity_normalize`) and PASS after.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import entity_normalize as en  # noqa: E402


# ---------------------------------------------------------------------------
# (1) fold
# ---------------------------------------------------------------------------


FOLD_CASES: tuple[tuple[str, str, str], ...] = (
    ("plain", "Synthetic Holdings", "synthetic holdings"),
    ("case", "SYNTHETIC HOLDINGS", "synthetic holdings"),
    ("inner whitespace", "Synthetic   Holdings", "synthetic holdings"),
    ("outer whitespace", "  Synthetic Holdings  ", "synthetic holdings"),
    ("tab and newline collapse", "Synthetic\tHoldings", "synthetic holdings"),
    ("comma dropped", "Synthetic, Inc.", "synthetic inc"),
    ("no comma", "Synthetic Inc", "synthetic inc"),
    ("dotted suffix", "Synthetic G.m.b.H.", "synthetic gmbh"),
    ("undotted suffix", "Synthetic GmbH", "synthetic gmbh"),
    ("dotted bv", "Synthetic B.V.", "synthetic bv"),
    ("dotted llc", "Synthetic L.L.C.", "synthetic llc"),
    ("acute accent", "Société Anonyme Test", "societe anonyme test"),
    ("no accent", "Societe Anonyme Test", "societe anonyme test"),
    ("cedilla", "Façade Synthetic", "facade synthetic"),
    ("umlaut", "Grün Synthetic", "grun synthetic"),
    ("ampersand", "Alpha & Beta", "alpha and beta"),
    ("ampersand spelled", "Alpha and Beta", "alpha and beta"),
    ("ampersand unspaced", "Alpha&Beta", "alpha and beta"),
    ("leading article", "The Synthetic Depot", "synthetic depot"),
    ("no leading article", "Synthetic Depot", "synthetic depot"),
    ("repeated leading article", "The The Synthetic Depot", "synthetic depot"),
    ("article only mid-name kept", "Synthetic The Depot", "synthetic the depot"),
    ("internal hyphen kept", "Smith-Jones Holdings", "smith-jones holdings"),
    ("slash kept", "Alpha Holdings d/b/a Beta", "alpha holdings d/b/a beta"),
    ("apostrophe dropped", "O'Brien Holdings", "obrien holdings"),
    ("parentheses dropped", "Synthetic (Europe) Limited", "synthetic europe limited"),
    ("quotes dropped", '"Synthetic" Holdings', "synthetic holdings"),
    ("digits kept", "Synthetic 360 Holdings", "synthetic 360 holdings"),
    ("empty", "", ""),
    ("whitespace only", "   ", ""),
    ("article only", "The", "the"),
)


class TestFold(unittest.TestCase):
    def test_fold_table(self):
        for label, raw, expected in FOLD_CASES:
            with self.subTest(label):
                self.assertEqual(en.fold(raw), expected)

    def test_fold_is_idempotent_over_every_case(self):
        """`fold` is applied to both sides of a comparison and, in
        `recognition_variants`, to its own output. A non-idempotent fold
        would make the answer depend on how many times it ran."""
        for label, raw, _expected in FOLD_CASES:
            with self.subTest(label):
                once = en.fold(raw)
                self.assertEqual(en.fold(once), once)

    def test_fold_of_a_non_string_is_empty_never_raises(self):
        for value in (None, 17, [], {}, object()):
            with self.subTest(repr(value)):
                self.assertEqual(en.fold(value), "")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (2) strip_legal_form -- every suffix, every spelling
# ---------------------------------------------------------------------------


class TestStripLegalForm(unittest.TestCase):
    def test_every_canonical_suffix_under_every_listed_spelling(self):
        """The table drives the test, so a suffix added to
        `LEGAL_FORM_SUFFIXES` without working is a failure here rather than
        a silent gap."""
        self.assertGreaterEqual(len(en.LEGAL_FORM_SUFFIXES), 19)
        for canonical, spellings in en.LEGAL_FORM_SUFFIXES.items():
            self.assertTrue(spellings, f"{canonical} has no spellings")
            for spelling in spellings:
                with self.subTest(f"{canonical}/{spelling}"):
                    core, found = en.strip_legal_form(
                        en.fold(f"Synthetic Holdings {spelling}")
                    )
                    self.assertEqual(core, "synthetic holdings")
                    self.assertEqual(found, canonical)

    def test_longest_spelling_wins(self):
        """`Pty Ltd` must strip as one suffix, not leave `... pty` behind
        after stripping `ltd`."""
        self.assertEqual(
            en.strip_legal_form(en.fold("Synthetic Holdings Pty Ltd")),
            ("synthetic holdings", "pty"),
        )
        self.assertEqual(
            en.strip_legal_form(en.fold("Synthetic Holdings Proprietary Limited")),
            ("synthetic holdings", "pty"),
        )

    def test_only_one_suffix_is_removed(self):
        core, canonical = en.strip_legal_form(en.fold("Synthetic Holdings Ltd Inc"))
        self.assertEqual(canonical, "inc")
        self.assertEqual(core, "synthetic holdings ltd")

    def test_no_suffix_is_left_alone(self):
        self.assertEqual(
            en.strip_legal_form(en.fold("Synthetic Holdings")),
            ("synthetic holdings", None),
        )

    def test_a_name_that_is_only_a_suffix_keeps_it(self):
        """Stripping would leave an empty core -- an identity every
        suffix-only name would share, which is how two unrelated entities
        would merge."""
        for spelling in ("Limited", "GmbH", "Inc."):
            with self.subTest(spelling):
                core, canonical = en.strip_legal_form(en.fold(spelling))
                self.assertEqual(core, en.fold(spelling))
                self.assertIsNone(canonical)

    def test_a_word_merely_ending_in_a_suffix_is_not_stripped(self):
        """`Uninc` ends with `inc`; it is not a suffix because it is not a
        whole trailing word."""
        self.assertEqual(
            en.strip_legal_form(en.fold("Synthetic Uninc")),
            ("synthetic uninc", None),
        )

    def test_empty_input(self):
        self.assertEqual(en.strip_legal_form(""), ("", None))
        self.assertEqual(en.strip_legal_form("   "), ("", None))

    def test_no_two_canonicals_claim_one_folded_spelling(self):
        """Folded spellings are the actual lookup keys, so a collision would
        make the reported canonical arbitrary. The module asserts this at
        import; this is the readable statement of the same rule."""
        seen: dict[str, str] = {}
        for canonical, spellings in en.LEGAL_FORM_SUFFIXES.items():
            for spelling in spellings:
                folded = en.fold(spelling)
                self.assertEqual(
                    seen.setdefault(folded, canonical),
                    canonical,
                    f"{folded!r} is claimed twice",
                )

    def test_the_suffix_lookup_order_is_deterministic(self):
        """Built from a SET, so the sort key must fully order it -- a
        PYTHONHASHSEED-dependent order would make stripping vary between
        processes."""
        keys = [(-len(sp), sp, canon) for sp, canon in en._FOLDED_SUFFIXES]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))


# ---------------------------------------------------------------------------
# (3) split_dba
# ---------------------------------------------------------------------------


class TestSplitDba(unittest.TestCase):
    def test_every_separator_splits_both_halves_out(self):
        for separator in en.DBA_SEPARATORS:
            with self.subTest(separator):
                name = f"Synthetic Holdings LLC {separator} Synthetic Legal"
                self.assertEqual(
                    en.split_dba(name), ["Synthetic Holdings LLC", "Synthetic Legal"]
                )

    def test_separators_are_case_insensitive(self):
        for separator in ("D/B/A", "DBA", "Doing Business As", "Trading As", "T/A"):
            with self.subTest(separator):
                self.assertEqual(
                    en.split_dba(f"Alpha Holdings {separator} Beta Studio"),
                    ["Alpha Holdings", "Beta Studio"],
                )

    def test_no_separator_returns_the_name_unchanged(self):
        self.assertEqual(en.split_dba("Synthetic Holdings GmbH"), ["Synthetic Holdings GmbH"])

    def test_word_boundaries_are_respected(self):
        """`dba` inside `Dbase` and `t/a` inside a longer slash run must not
        split a real name."""
        for name in ("Dbase Synthetic Limited", "Synthetic Dbaker Holdings"):
            with self.subTest(name):
                self.assertEqual(en.split_dba(name), [name])

    def test_empty_and_blank(self):
        self.assertEqual(en.split_dba(""), [""])
        self.assertEqual(en.split_dba("   "), ["   "])


# ---------------------------------------------------------------------------
# (4) recognition_key -- what collides, and what must NOT
# ---------------------------------------------------------------------------


COLLIDING_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("gmbh dotted", "Synthetic Holdings GmbH", "SYNTHETIC HOLDINGS G.m.b.H."),
    ("gmbh spelled out", "Synthetic Holdings GmbH",
     "Synthetic Holdings Gesellschaft mit beschränkter Haftung"),
    ("bv", "Synthetic Holdings B.V.", "synthetic holdings bv"),
    ("srl", "Synthetic Holdings S.r.l.", "SYNTHETIC HOLDINGS SRL"),
    ("sas", "Synthetic Holdings SAS", "Synthetic Holdings S.A.S."),
    ("sa", "Synthetic Holdings S.A.", "Synthetic Holdings SA"),
    ("ltd/limited", "Synthetic Holdings Ltd.", "Synthetic Holdings Limited"),
    ("inc/incorporated", "Synthetic Holdings Inc", "Synthetic Holdings Incorporated"),
    ("llc", "Synthetic Holdings LLC", "Synthetic Holdings L.L.C."),
    ("plc", "Synthetic Holdings plc", "Synthetic Holdings P.L.C."),
    ("ag", "Synthetic Holdings AG", "Synthetic Holdings A.G."),
    ("spa", "Synthetic Holdings S.p.A.", "Synthetic Holdings SPA"),
    ("pty", "Synthetic Holdings Pty Ltd", "Synthetic Holdings Proprietary Limited"),
    ("kk", "Synthetic Holdings K.K.", "Synthetic Holdings Kabushiki Kaisha"),
    ("corp", "Synthetic Holdings Corp.", "Synthetic Holdings Corporation"),
    ("co", "Synthetic Holdings Co.", "Synthetic Holdings Company"),
    ("lp", "Synthetic Holdings L.P.", "Synthetic Holdings LP"),
    ("llp", "Synthetic Holdings L.L.P.", "Synthetic Holdings LLP"),
    ("oy", "Nordic Synthetic Oy", "NORDIC SYNTHETIC OY"),
    ("ab", "Nordic Synthetic AB", "nordic  synthetic  ab"),
    ("punctuation", "Synthetic, Inc.", "Synthetic Inc"),
    ("diacritics", "Société Synthetic SAS", "Societe Synthetic S.A.S."),
    ("ampersand", "Alpha & Beta Limited", "Alpha and Beta Ltd"),
    ("leading article", "The Synthetic Depot Ltd", "Synthetic Depot Limited"),
    ("suffix present vs absent", "Synthetic Holdings", "Synthetic Holdings GmbH"),
)

DISTINCT_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("different core", "Synthetic Holdings GmbH", "Synthetic Ventures GmbH"),
    ("hyphen is significant", "Smith-Jones Ltd", "Smith Jones Ltd"),
    ("suffix-only names do not merge", "Limited", "GmbH"),
    ("digits matter", "Synthetic 360 Ltd", "Synthetic 361 Ltd"),
    ("a dba compound is not its own half", "Alpha Holdings LLC d/b/a Beta Studio",
     "Alpha Holdings LLC"),
)


class TestRecognitionKey(unittest.TestCase):
    def test_pairs_that_must_collide(self):
        for label, left, right in COLLIDING_PAIRS:
            with self.subTest(label):
                self.assertEqual(
                    en.recognition_key(left),
                    en.recognition_key(right),
                    f"{left!r} and {right!r} are the same entity",
                )

    def test_pairs_that_must_stay_distinct(self):
        for label, left, right in DISTINCT_PAIRS:
            with self.subTest(label):
                self.assertNotEqual(
                    en.recognition_key(left),
                    en.recognition_key(right),
                    f"{left!r} and {right!r} are different entities",
                )

    def test_the_key_is_never_the_stored_spelling(self):
        """The key is lossy on purpose (the suffix is gone). This pins the
        property that makes it unfit to render, so a future caller that
        tries cannot claim it looked safe."""
        self.assertEqual(en.recognition_key("Synthetic Holdings GmbH"), "synthetic holdings")

    def test_key_of_blank_is_blank_never_raises(self):
        self.assertEqual(en.recognition_key(""), "")
        self.assertEqual(en.recognition_key("   "), "")


# ---------------------------------------------------------------------------
# (5) recognition_variants
# ---------------------------------------------------------------------------


class TestRecognitionVariants(unittest.TestCase):
    def test_a_suffixed_name_carries_every_alternative_spelling(self):
        variants = en.recognition_variants("Synthetic Holdings GmbH")
        self.assertIn("Synthetic Holdings GmbH", variants)  # as typed
        self.assertIn("synthetic holdings gmbh", variants)  # folded
        self.assertIn("synthetic holdings", variants)  # core
        for spelling in en.LEGAL_FORM_SUFFIXES["gmbh"]:
            self.assertIn(f"synthetic holdings {spelling}", variants)

    def test_a_dba_compound_carries_both_halves_and_each_half_s_forms(self):
        variants = en.recognition_variants("Alpha Holdings LLC d/b/a Beta Studio")
        self.assertIn("Alpha Holdings LLC d/b/a Beta Studio", variants)
        self.assertIn("Alpha Holdings LLC", variants)
        self.assertIn("Beta Studio", variants)
        self.assertIn("alpha holdings", variants)
        self.assertIn("alpha holdings l.l.c.", variants)
        self.assertIn("beta studio", variants)

    def test_the_order_is_deterministic_and_sorted(self):
        first = en.recognition_variants("Société Synthetic SAS")
        second = en.recognition_variants("Société Synthetic SAS")
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))
        self.assertEqual(len(first), len(set(first)))

    def test_a_suffixless_name_still_yields_its_folded_form(self):
        variants = en.recognition_variants("The Synthetic Depot")
        self.assertIn("The Synthetic Depot", variants)
        self.assertIn("synthetic depot", variants)

    def test_blank_and_non_string_yield_nothing(self):
        self.assertEqual(en.recognition_variants(""), [])
        self.assertEqual(en.recognition_variants("   "), [])
        self.assertEqual(en.recognition_variants(None), [])  # type: ignore[arg-type]

    def test_every_variant_folds_into_the_document_search_the_route_runs(self):
        """The variants exist to be searched for in folded document text
        (`review_routes.compute_party_recognised`). Each one must survive
        that fold as a non-empty string, or it is dead weight that can
        never match."""
        for variant in en.recognition_variants("Alpha & Beta Holdings Pty Ltd"):
            with self.subTest(variant):
                self.assertTrue(en.fold(variant))


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestFold,
        TestStripLegalForm,
        TestSplitDba,
        TestRecognitionKey,
        TestRecognitionVariants,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
