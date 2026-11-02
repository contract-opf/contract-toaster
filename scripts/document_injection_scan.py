#!/usr/bin/env python3
"""
Document injection scan (issue #506): does the counterparty's .docx contain
text addressed to an AI?

## Why this exists

`scripts/opf_injection_scan.py` has been a real tripwire since the OPF work,
wired into PLAYBOOK ingestion. Nothing scanned the counterparty's document --
which is the one piece of text in this system an adversary actually controls.

The structural defences are decent: the hostile-file gauntlet, the untrusted
delimiter on the document block, schema-validated output, no tools available to
the model, and an adversarial critic from a different lab. Two things were
absent. An independent detection signal, and -- more important -- **telling the
human it happened**. Even when the model correctly ignores an injection, the
fact that a counterparty attempted one is evidence about that counterparty, and
it was being silently discarded.

## The highest-signal rule is structural, not lexical

A contract clause is never invisible.

Text hidden with `w:vanish` or sized below 4pt is flagged REGARDLESS of what it
says -- entirely deterministic, and it catches the payload class regexes
cannot: a PARAPHRASED injection, hidden from the human reader. The lexical
rules can be evaded by rewording; "this text is invisible to the person signing
the contract" cannot.

## Two rules were removed after measuring them against real paper

The issue proposed flagging "font color at/near the background" as well. That
was measured against all 24 documents of the real corpus and **fired on 98
paragraphs across 16 of them, every one a false positive**: e-signature
placeholder tags (`[signature.user_1]`) are deliberately white so the signing
platform's overlay shows instead, and white headings sit on shaded table cells.
A run's colour cannot be judged without its background, and the background is
not knowable from the run. `w:vanish` and `w:sz` carry no such ambiguity, and
measured **zero** false positives across the same 24 documents.

The inherited `invisible-text` rule needed narrowing for the same reason: Word
leaves runs of trailing U+200B in real headings, which tripped it on 9 of 24
documents. Here it fires only when a zero-width character sits BETWEEN word
characters -- the actual smuggling shape, splitting a trigger phrase so a human
reads one thing and the model another. Padding at the end of a heading is an
artifact; a zero-width space inside a word is not. Direction-override
characters stay unconditional: those have no benign use in contract prose.

A tripwire that fires on two thirds of ordinary paper is one nobody reads,
which makes it worse than no tripwire at all.

## Advisory, always

This scan never blocks a review and never changes an outcome. Legitimate
contracts contain odd strings, and refusing to review a counterparty's paper is
not an acceptable failure mode -- the same posture as the cheap-model preflight.
Anything unparseable yields no findings rather than raising: the hostile-file
gauntlet is what refuses a bad package, and this must never become a second,
quieter way for a review to fail.

## What it must never do

**Reach the prompt.** Injecting "this document is suspicious" into the model's
context would itself be a manipulation surface -- an attacker who can get text
into the document can then aim it at that sentence -- and the untrusted
delimiter already carries the instruction-immunity contract. The findings go to
the review row and to the human. Not to the model.

**Echo the matched text.** Findings carry a rule id and a locator only, the
same discipline `opf_injection_scan` and `leakage_scan` keep. The UI may tell
the document's own uploader WHERE and WHAT CATEGORY; nothing echoes matched
strings into logs, audit rows, or any admin-visible surface. The owner opens
their own document to read the actual words.

## Relationship to the existing scanner

The lexical rules are not duplicated: `_scan_text` from `opf_injection_scan` is
the single implementation, called here against document paragraphs. One rule
set, one place to update, and a rule added for playbook ingestion protects
uploaded contracts for free.

## It reads what the model reads (QA remediation W7-3)

The rules used to run over regexes of the raw `word/document.xml`, and four
encodings the XML parser undoes walked straight past them: a character
reference (`&#73;gnore`), WordprocessingML bound to a namespace prefix other
than `w:`, a phrase split across runs, and text hidden through a STYLE
rather than a direct `w:vanish`. The scan now reads
`extraction_normalization_stage.scan_paragraphs` -- the extractor's own
decoded, namespace-aware walk, each run carrying its EFFECTIVE hidden flag
(direct formatting -> character style -> paragraph style -> document
defaults) -- and the lexical rules match a comparison form with format and
invisible characters removed and common Cyrillic/Greek lookalikes folded
onto Latin (`scripts/confusables.py`). Findings keep their exact shape:
`{rule_id, locator}`, nothing else.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import confusables  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import normalize_input  # noqa: E402
import opf_injection_scan  # noqa: E402

# Re-exported so callers name rules through one module rather than reaching
# into the playbook scanner for half of them.
RULE_INSTRUCTION_OVERRIDE = opf_injection_scan.RULE_INSTRUCTION_OVERRIDE
RULE_ROLE_TOKEN_SMUGGLING = opf_injection_scan.RULE_ROLE_TOKEN_SMUGGLING
RULE_TOOL_CALL_SYNTAX = opf_injection_scan.RULE_TOOL_CALL_SYNTAX
RULE_EXFILTRATION_DIRECTIVE = opf_injection_scan.RULE_EXFILTRATION_DIRECTIVE
RULE_ENCODED_BLOB = opf_injection_scan.RULE_ENCODED_BLOB
RULE_INVISIBLE_TEXT = opf_injection_scan.RULE_INVISIBLE_TEXT

# Structural rules -- this module's own, because they are properties of the
# OOXML, not of the prose.
RULE_HIDDEN_TEXT = "hidden-text"
RULE_UNREADABLE_TEXT = "unreadable-text"

_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# `w:sz` is in HALF-points, so 4 == 2pt. Real contracts are full of 8pt small
# print, and flagging that would make this tripwire noise. Below 4pt is not
# something a drafter does by accident.
_MIN_READABLE_HALF_POINTS = 8

# A zero-width or otherwise invisible character sitting BETWEEN two word
# characters -- the smuggling shape, where a trigger phrase is split so a
# human reads one thing and the model another. Trailing/leading runs of the
# same characters are Word artifacts and are deliberately not matched; see
# the module docstring. W7-2 widened the class with the other invisible
# characters `normalize_input.SUSPICIOUS_CONTROL_RANGES` now refuses
# (combining grapheme joiner, the Hangul fillers, the Mongolian vowel
# separator, the word joiner and invisible operators, variation selectors).
_INTERLEAVED_ZERO_WIDTH_RE = re.compile(
    "\\w[\u200b-\u200d\ufeff\u034f\u115f\u1160\u180e\u2060-\u2064\u3164"
    "\ufe00-\ufe0f\uffa0\U000e0100-\U000e01ef]+\\w"
)
# Direction overrides and isolates have no benign use in contract prose, and
# neither does the Unicode TAG block (W7-2), an invisible twin of ASCII that
# spells out whole instructions -- so these stay unconditional.
_DIRECTION_OVERRIDE_RE = re.compile("[\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")


def _comparison_text(text: str) -> str:
    """What the lexical rules match against (W7-3): NFKC (fullwidth and
    compatibility forms onto their ordinary letters), every format character
    (category `Cf`: zero-width, soft hyphen, tags, bidi controls) and every
    character `normalize_input.SUSPICIOUS_CONTROL_RANGES` lists removed so
    none can split a trigger phrase, and the confusables skeleton
    (`scripts/confusables.py`) so one Cyrillic or Greek lookalike cannot
    either. A COMPARISON form only -- never stored, never echoed."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        ch
        for ch in text
        if unicodedata.category(ch) != "Cf" and not normalize_input.find_suspicious_control_characters(ch)
    )
    return confusables.skeleton(text)


def _structural_rules(paragraph: dict[str, Any]) -> list[str]:
    """The rules that depend on how a paragraph LOOKS, not what it says.

    Read off the parsed paragraph, namespace-aware (W7-3), not the raw XML:
    a run is hidden when its EFFECTIVE `w:vanish` is on -- direct
    formatting, its character style, its paragraph's style or the document
    defaults (`extraction_normalization_stage.VanishStyles`) -- which is the
    same decision that keeps its text from the model. A `w:vanish` on the
    paragraph MARK (`w:pPr/w:rPr`) still counts on its own, and so does any
    `w:sz` under 4pt anywhere in the paragraph: a tripwire is allowed to be
    coarse; it is not allowed to be evadable by moving an attribute one
    level up.
    """
    hits: list[str] = []
    element = paragraph["element"]
    mark_hidden = extraction_normalization_stage._vanish_value(element.find(f"{_NS}pPr/{_NS}rPr"))
    if mark_hidden or any(hidden for _text, hidden in paragraph["runs"]):
        hits.append(RULE_HIDDEN_TEXT)

    for size in element.iter(f"{_NS}sz"):
        try:
            if int(size.get(f"{_NS}val") or "") < _MIN_READABLE_HALF_POINTS:
                hits.append(RULE_UNREADABLE_TEXT)
                break
        except ValueError:
            continue
    return hits


def _is_smuggled_invisible(text: str) -> bool:
    """Is this invisible-character usage the smuggling shape, or an artifact?

    Zero-width characters count only when INTERLEAVED inside a word; a run of
    them padding the end of a heading is what Word actually produces and was
    measured on 9 of the 24 real corpus documents. Direction overrides and
    isolates, and TAG characters, count unconditionally -- they have no
    benign use in contract prose.
    """
    return bool(
        _INTERLEAVED_ZERO_WIDTH_RE.search(text) or _DIRECTION_OVERRIDE_RE.search(text)
    )


def scan_document(docx_bytes: bytes) -> list[dict[str, Any]]:
    """Every finding in an uploaded .docx, as `{rule_id, locator}` dicts.

    `locator` is a paragraph ordinal (`"paragraph 4"`) -- enough for a human to
    find it in their own copy, and carrying none of the text. A finding is
    emitted at most once per rule per paragraph, so a paragraph that repeats an
    override phrase five times is one finding, not five.

    The rules run over `extraction_normalization_stage.scan_paragraphs` --
    the extractor's own decoded, namespace-aware view of each paragraph,
    hidden runs included and flagged -- never over the raw XML (W7-3). The
    raw-XML regexes this replaced were walked past by a character reference
    (`&#73;gnore`), by binding WordprocessingML to a prefix other than `w:`,
    by splitting a phrase across runs, and by hiding text through a style.

    Returns `[]` for anything that cannot be read. The hostile-file gauntlet
    refuses bad packages; this is advisory and must never be a second way for a
    review to fail.
    """
    try:
        paragraphs = extraction_normalization_stage.scan_paragraphs(docx_bytes)
    except Exception:  # noqa: BLE001 - advisory: degrade, never raise
        return []

    findings: list[dict[str, Any]] = []
    for index, paragraph in enumerate(paragraphs, start=1):
        locator = f"paragraph {index}"
        rules = _structural_rules(paragraph)
        # Every run, hidden ones included: a hidden payload is flagged for
        # being hidden AND for what it says.
        text = "".join(run_text for run_text, _hidden in paragraph["runs"]).strip()
        if text:
            # The lexical rules are the playbook scanner's, called rather than
            # copied: one rule set, one place to update, and a rule added there
            # protects uploaded contracts for free.
            #
            # `invisible-text` is the one exception, re-decided here on the
            # UNFOLDED text (the fold removes exactly the characters it looks
            # for): the playbook scanner flags ANY zero-width character, which
            # is right for a playbook (nobody pastes Word artifacts into one)
            # and wrong for a .docx, where Word leaves them in real headings.
            # See the module docstring for the measurement.
            for rule_id in opf_injection_scan._scan_text(_comparison_text(text)):
                if rule_id != RULE_INVISIBLE_TEXT:
                    rules.append(rule_id)
            if _is_smuggled_invisible(text):
                rules.append(RULE_INVISIBLE_TEXT)
        for rule_id in dict.fromkeys(rules):
            findings.append({"rule_id": rule_id, "locator": locator})
    return findings


def summarise(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """The ids-and-counts projection recorded on the review row.

    Deliberately not the findings list itself: a row is read by more surfaces
    than a reviewer's own screen, and this shape carries no locator that could
    accumulate into a map of the document. Empty findings summarise to an empty
    dict, so a clean review's row is byte-identical to one written before this
    landed.
    """
    if not findings:
        return {}
    rule_ids = sorted({finding["rule_id"] for finding in findings})
    return {
        "injection_scan_rule_ids": rule_ids,
        "injection_scan_finding_count": len(findings),
    }


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    if len(sys.argv) < 2:
        print("usage: document_injection_scan.py <path.docx>")
        raise SystemExit(2)
    with open(sys.argv[1], "rb") as handle:
        findings = scan_document(handle.read())
    for finding in findings:
        print(f"{finding['rule_id']}\t{finding['locator']}")
    print(f"{len(findings)} finding(s)")


if __name__ == "__main__":
    main()
