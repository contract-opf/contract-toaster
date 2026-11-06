#!/usr/bin/env python3
"""
Recognising a trailing SIGNATURE BLOCK inside the physical paragraphs of a
clause block.

## Why this exists

`extraction_normalization_stage.extract_document_paragraphs` groups every
non-boundary `<w:p>` under the most recent boundary paragraph, so the
signature blocks at the end of an agreement -- which have no heading of their
own -- become physical siblings of the LAST numbered clause's body. A
whole-block `delete_block` (`redline_block_apply`) strikes every physical
paragraph of the block it is told to delete, so deleting the last clause
struck the signature blocks with it, and the ACCEPTED document came back
unsigned. Nothing in the transcript can say otherwise: the model addresses
one block id, and the block it was shown really does contain those
paragraphs.

This module answers one question about a block's physical paragraphs, in
text only (no OOXML, no repo imports): where does a trailing signature block
begin? The compiler leaves everything from that paragraph on untouched.

## The contract is asymmetric on purpose

A mistake in one direction is a visible nuisance; in the other it is silent
damage, and the rules are built around telling them apart:

  - NOT recognising a signature block keeps the old behaviour -- it is struck
    with the clause -- and is visible in the redline. The compiler narrows
    this with `ambiguous` (below): signature-looking lines it cannot place in
    a clean tail make it REFUSE the delete rather than guess.
  - RECOGNISING clause text as a signature block would leave real clause body
    standing behind a deleted clause, and nothing would say so. So a paragraph
    is retained only when EVERY line of it is signature-shaped.

## What counts

A paragraph is read as lines (a soft line break inside a `<w:p>` reads as
`"\\n"`). Each non-blank line is exactly one of:

  - a SIGNATURE line: it STARTS with a signature label (`By:`, `Name:`,
    `Title:`, `Date:`, `Signature:`, `Signed:`, `Its:`, `Printed Name:`,
    `Authorized Signatory:`), a blank-field run of three or more underscores,
    or `/s/`, and what is left once labels, blank runs and `/s/` are removed
    is short (at most 8 words after a label, 3 after an underscore run or
    `/s/` -- a typed name or title, not a sentence);
  - a CAPTION line: a party line -- at most 10 words, ending in a formal
    entity suffix (`LLC`, `Inc.`, `Incorporated`, `Corp.`, `Ltd`, `LLP`, ...)
    -- or a role label of at most 3 words ending in a colon (`Company:`);
  - a LEAD-IN line: starts `IN WITNESS WHEREOF`, `The parties have
    executed/caused`, `Agreed and accepted`, `Accepted and agreed`, or a
    `[Signature page ...]` marker;
  - anything else (OTHER).

A paragraph is then:

  - SIGNATURE: no OTHER line, at least one signature line;
  - WEAK: no OTHER line, no signature line -- only captions and lead-ins;
  - MIXED: it has a line that STARTS like a signature line, and an OTHER line
    (or a signature-shaped start followed by prose). It cannot be split, so
    it is neither struck nor kept with confidence;
  - BODY: everything else.

The tail is the longest run of SIGNATURE and WEAK paragraphs ending at the
last paragraph, and it exists only if it holds at least one SIGNATURE
paragraph (a lone party line or lead-in is not a signature block).

## `ambiguous`

`find_signature_tail` reports `ambiguous` -- and the compiler refuses the
delete, naming the reason, instead of striking or keeping anything -- when:

  - the scan back from the end stops on a MIXED paragraph (prose and a
    signature line share a `<w:p>`), or
  - a SIGNATURE or MIXED paragraph sits ahead of the tail, behind text the
    heuristic does not recognise (signature lines the clean tail does not
    reach: a typed name under the line with no label, a table layout it
    cannot read).

Both mean "this block contains a signature block I cannot cleanly separate
from the clause", and the only safe answers are to strike it all (the bug) or
to keep everything (clause text left behind) -- so neither is done.

Deliberately NOT recognised, so struck with their clause: contact labels
(`Attn:`, `Email:`, `Address:`), a blank-field line that does not start with a
label (`Fee: $____`), a short sentence, and party lines without a formal
entity suffix. Notice blocks therefore stay clause text.

This is a HEURISTIC over text. It is conservative by construction -- a
paragraph is kept only if every line of it fits -- and it is documented in
`docs/output-contract.md`, "Deleting the last clause leaves the signature
blocks".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Signature labels: a label word(s) followed by a colon.
_LABEL_RE = re.compile(
    r"(?<!\w)(?:by|name|title|date|signature|signed|its|"
    r"print(?:ed)?\s+name|authori[sz]ed\s+signatory|signatory)\s*:",
    re.IGNORECASE,
)
# A blank field to sign on: three or more underscores in a row.
_BLANK_RE = re.compile(r"_{3,}")
# An electronic signature marker.
_SLASH_S_RE = re.compile(r"/s/", re.IGNORECASE)

#: A line is signature-SHAPED only if it starts with one of these.
_STARTS_LIKE_SIGNATURE_RE = re.compile(
    r"^(?:" + _LABEL_RE.pattern + r"|_{3,}|/s/)", re.IGNORECASE
)

#: Most words left on a signature line once labels, blanks and `/s/` are gone.
_MAX_VALUE_WORDS_AFTER_LABEL = 8
_MAX_VALUE_WORDS_AFTER_BLANK = 3

# Formal entity suffixes only: a party line is a name that ENDS in one. Common
# words ("Company", "Trust", "Bank") are excluded on purpose -- a sentence can
# end in them.
_ENTITY_SUFFIX_RE = re.compile(
    r"(?:\b(?:llc|inc|incorporated|corp|corporation|ltd|limited|llp|lp|plc|gmbh|pty|pte|co)\b\.?"
    r"|\bl\.l\.c\.?|\bl\.p\.|\bs\.a\.|\bn\.v\.|\bb\.v\.)$",
    re.IGNORECASE,
)
_MAX_CAPTION_WORDS = 10
_MAX_CAPTION_CHARS = 100
_MAX_ROLE_LABEL_WORDS = 3

_LEAD_IN_RE = re.compile(
    r"^\s*(?:in\s+witness\s+whereof"
    r"|the\s+parties\s+(?:have\s+)?(?:executed|caused)"
    r"|agreed\s+(?:and|to\s+and)\s+accepted"
    r"|accepted\s+and\s+agreed"
    r"|\[\s*signature\s+pages?\b)",
    re.IGNORECASE,
)
_MAX_LEAD_IN_CHARS = 400

_WORD_RE = re.compile(r"[^\W_]+")

# Line kinds.
_SIG, _CAPTION, _LEAD_IN, _OTHER = "signature", "caption", "lead_in", "other"
# Paragraph kinds.
SIGNATURE, WEAK, MIXED, BODY = "signature", "weak", "mixed", "body"

AMBIGUITY_MIXED_PARAGRAPH = "mixed_paragraph"
AMBIGUITY_SIGNATURE_BEFORE_UNRECOGNISED_TEXT = "signature_before_unrecognised_text"


@dataclass(frozen=True)
class SignatureTail:
    """Where a block's trailing signature block begins.

    `start` is the index of its first paragraph; it equals `count` when the
    block has no signature tail. `ambiguous_at` is the index of the paragraph
    that made the answer ambiguous (see the module docstring) and
    `ambiguity` names which way; both are unset for a clean answer, and
    `start` is not meaningful when `ambiguous` is true.
    """

    start: int
    count: int
    ambiguous_at: int | None = None
    ambiguity: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.ambiguous_at is not None

    @property
    def present(self) -> bool:
        """True when there IS a clean signature tail to leave alone."""
        return not self.ambiguous and self.start < self.count

    @property
    def is_whole_block(self) -> bool:
        """True when the signature tail is every paragraph the block has."""
        return self.present and self.start == 0


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _signature_value(line: str) -> str:
    """`line` with its labels, blank runs and `/s/` markers removed."""
    stripped = _LABEL_RE.sub(" ", line)
    stripped = _BLANK_RE.sub(" ", stripped)
    return _SLASH_S_RE.sub(" ", stripped)


def _starts_like_signature(line: str) -> bool:
    return _STARTS_LIKE_SIGNATURE_RE.match(line) is not None


def _classify_line(line: str) -> str:
    """One of `_SIG`, `_CAPTION`, `_LEAD_IN`, `_OTHER` for a non-blank line."""
    if _starts_like_signature(line):
        words = _words(_signature_value(line))
        limit = (
            _MAX_VALUE_WORDS_AFTER_LABEL
            if _LABEL_RE.match(line)
            else _MAX_VALUE_WORDS_AFTER_BLANK
        )
        return _SIG if len(words) <= limit else _OTHER
    if _LEAD_IN_RE.match(line) and len(line) <= _MAX_LEAD_IN_CHARS:
        return _LEAD_IN
    words = _words(line)
    if (
        words
        and len(words) <= _MAX_CAPTION_WORDS
        and len(line) <= _MAX_CAPTION_CHARS
        and _ENTITY_SUFFIX_RE.search(line)
    ):
        return _CAPTION
    if words and len(words) <= _MAX_ROLE_LABEL_WORDS and line.endswith(":"):
        return _CAPTION
    return _OTHER


def classify_paragraph(text: str) -> str:
    """`SIGNATURE`, `WEAK`, `MIXED` or `BODY` for one physical paragraph."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return BODY
    kinds = [_classify_line(line) for line in lines]
    starts_like_signature = any(_starts_like_signature(line) for line in lines)
    if _OTHER in kinds:
        return MIXED if starts_like_signature else BODY
    return SIGNATURE if _SIG in kinds else WEAK


def find_signature_tail(paragraph_texts: list[str]) -> SignatureTail:
    """Where the trailing signature block of `paragraph_texts` begins.

    `paragraph_texts` are the block's physical paragraphs' clean texts, in
    document order. See the module docstring for what counts and for when the
    answer is `ambiguous`.
    """
    count = len(paragraph_texts)
    kinds = [classify_paragraph(text) for text in paragraph_texts]

    index = count - 1
    while index >= 0 and kinds[index] in (SIGNATURE, WEAK):
        index -= 1
    run_start = index + 1  # the contiguous SIGNATURE/WEAK run ends the block

    if index >= 0 and kinds[index] == MIXED:
        return SignatureTail(
            start=count,
            count=count,
            ambiguous_at=index,
            ambiguity=AMBIGUITY_MIXED_PARAGRAPH,
        )

    if not any(kind == SIGNATURE for kind in kinds[run_start:]):
        # The run holds only party lines / lead-ins, which are no signature
        # block on their own. Whatever precedes it is clause text.
        run_start = count

    for position in range(run_start):
        if kinds[position] in (SIGNATURE, MIXED):
            return SignatureTail(
                start=count,
                count=count,
                ambiguous_at=position,
                ambiguity=AMBIGUITY_SIGNATURE_BEFORE_UNRECOGNISED_TEXT,
            )

    return SignatureTail(start=run_start, count=count)
