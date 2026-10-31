#!/usr/bin/env python3
"""
A small confusables skeleton (QA remediation W7-3): fold the common Cyrillic
and Greek letters that render identically to Latin ones onto those Latin
letters, so a scanner's patterns cannot be walked past by swapping one
lookalike into a trigger word ("Ign\u043ere all prior instructions" with a
Cyrillic `\u043e`).

## Deliberately small

This is NOT Unicode TR39's full confusables table (thousands of entries,
many many-to-one, several of them multi-character). It is the handful of
lookalikes a human cannot tell from Latin in an ordinary contract font --
the ones an adversary would actually reach for -- and every entry maps ONE
character to ONE ASCII character. That 1:1 property is what lets a caller
apply it to a string without disturbing anything that indexes into it, and
`tests/test_invisible_text_and_injection_scan_w7.py` pins it.

## Comparison-only

The skeleton is a COMPARISON form, never written back into a document,
never shown to anyone and never used as the text of an edit -- the same
contract `scripts/text_fold.py` states for its typographic fold. Two
callers use it:

  - `scripts/document_injection_scan.py`, which runs its lexical rules over
    the skeleton of each paragraph's decoded text; and
  - `scripts/leakage_scan.py::_normalize`, the normal form both sides of
    every confidential n-gram comparison are put in, so a model restating a
    playbook gram with one lookalike swapped in still matches.

A genuine Cyrillic or Greek word in a document is folded too. That is
harmless for both callers: the injection rules are English-language
patterns, and the leakage comparison folds the corpus side identically, so
what matched before still matches.
"""

from __future__ import annotations

#: One lookalike -> one ASCII letter. Cyrillic first, then Greek, each in
#: code-point order. Only letters whose usual glyph is indistinguishable
#: from the Latin letter in common text fonts are listed.
CONFUSABLE_FOLD: dict[str, str] = {
    # Cyrillic capitals
    "\u0405": "S",  # DZE
    "\u0406": "I",  # BYELORUSSIAN-UKRAINIAN I
    "\u0408": "J",  # JE
    "\u0410": "A",
    "\u0412": "B",
    "\u0415": "E",
    "\u041a": "K",
    "\u041c": "M",
    "\u041d": "H",
    "\u041e": "O",
    "\u0420": "P",
    "\u0421": "C",
    "\u0422": "T",
    "\u0425": "X",
    "\u04ae": "Y",  # STRAIGHT U
    "\u04c0": "I",  # PALOCHKA
    # Cyrillic small
    "\u0430": "a",
    "\u0435": "e",
    "\u043e": "o",
    "\u0440": "p",
    "\u0441": "c",
    "\u0443": "y",
    "\u0445": "x",
    "\u0455": "s",  # DZE
    "\u0456": "i",  # BYELORUSSIAN-UKRAINIAN I
    "\u0458": "j",  # JE
    "\u04bb": "h",  # SHHA
    "\u0501": "d",  # KOMI DE
    "\u051b": "q",  # QA
    "\u051d": "w",  # WE
    # Greek capitals
    "\u0391": "A",
    "\u0392": "B",
    "\u0395": "E",
    "\u0396": "Z",
    "\u0397": "H",
    "\u0399": "I",
    "\u039a": "K",
    "\u039c": "M",
    "\u039d": "N",
    "\u039f": "O",
    "\u03a1": "P",
    "\u03a4": "T",
    "\u03a5": "Y",
    "\u03a7": "X",
    # Greek small
    "\u03b9": "i",  # IOTA
    "\u03bd": "v",  # NU
    "\u03bf": "o",  # OMICRON
    "\u03c1": "p",  # RHO
}

_TABLE = str.maketrans(CONFUSABLE_FOLD)


def skeleton(text: str) -> str:
    """`text` with every `CONFUSABLE_FOLD` lookalike replaced by its ASCII
    letter. Same length as `text`, character for character."""
    return text.translate(_TABLE)
