"""
Party-name normalisation — issue #55 (audit finding F6, action A6).

## What this is for

Recognising OUR OWN legal entities in a counterparty's draft is the whole
basis of party binding (#677/#678). Before this module the comparison was
`" ".join(name.split()).casefold()` in two places
(`backend/src/entity_roster.py::normalize_entities` and
`scripts/opf_prompt.py::_recognition_key`), which means a roster entry
`Synthetic Holdings GmbH` did NOT match `SYNTHETIC HOLDINGS G.m.b.H.` in
the document, `Synthetic, Inc.` did not match `Synthetic Inc`, `Société`
did not match `Societe`, and one roster line carrying a `d/b/a` compound
matched neither half on its own.

This module is the ONE place those four foldings live. It is stdlib-only
and has no I/O, so it imports cleanly from `backend/src` and from
`scripts/` alike (`backend/src/pipeline_runner.py` and
`backend/src/entity_roster.py` both put `scripts/` on `sys.path`).

## The two vocabulary words

**recognition key** — the identity two spellings are THE SAME ENTITY
under. `recognition_key(name)` = the folded name with ONE trailing
legal-form suffix removed. It is what `normalize_entities` and
`resolve_party_recognition_set` deduplicate on, and nothing else: it is
deliberately lossy (it throws the suffix away), so it must never be
rendered, stored or shown. The spelling an admin typed is what gets
stored and what reaches the prompt.

**variant** — a spelling the SAME entity might plausibly appear under in a
document. `recognition_variants(name)` returns the deterministic, sorted
set of them: the name as typed, each half of a `d/b/a` compound, the
folded form, the suffix-less core, and the core under every alternative
spelling of the suffix it actually carries. Variants are for SEARCHING
extracted document text (`backend/src/review_routes.py`'s preflight party
signal); they are advisory and never rendered into a prompt.

## Deliberate non-goals

No fuzzy matching, no edit distance, no token-subset scoring. Every rule
here is a table lookup or a Unicode operation, so the same two names
compare the same way forever and a failure is explainable by reading the
table. A near-miss that this module does not fold is a MISSED recognition
(fail-open to "we did not recognise it"), never a false one: the roster is
who we are, and binding a review to the wrong principal is the failure
class #677 exists to prevent.
"""

from __future__ import annotations

import re
import unicodedata

#: Canonical legal-form token -> every spelling we fold onto it. Spellings
#: are matched AFTER `fold`, so the punctuated variants here (`g.m.b.h.`,
#: `b.v.`) collapse onto their unpunctuated twins by construction; they are
#: written out anyway because this table doubles as the source of the
#: variants `recognition_variants` emits, where the punctuation is exactly
#: what a document search needs to look for.
LEGAL_FORM_SUFFIXES: dict[str, tuple[str, ...]] = {
    "gmbh": ("gmbh", "g.m.b.h.", "g.m.b.h", "gesellschaft mit beschränkter haftung"),
    "bv": ("b.v.", "bv", "besloten vennootschap"),
    "srl": ("s.r.l.", "srl", "s.r.l"),
    "sas": ("sas", "s.a.s."),
    "sa": ("s.a.", "sa"),
    "ltd": ("ltd", "ltd.", "limited"),
    "inc": ("inc", "inc.", "incorporated"),
    "llc": ("llc", "l.l.c.", "l.l.c"),
    "plc": ("plc", "p.l.c."),
    "ag": ("ag", "a.g."),
    "spa": ("s.p.a.", "spa"),
    "pty": ("pty ltd", "pty. ltd.", "proprietary limited"),
    "kk": ("k.k.", "kk", "kabushiki kaisha"),
    "oy": ("oy",),
    "ab": ("ab",),
    "corp": ("corp", "corp.", "corporation"),
    "co": ("co", "co.", "company"),
    "lp": ("lp", "l.p."),
    "llp": ("llp", "l.l.p."),
}

#: The separators that mark a trading-name compound. Either half of such a
#: name can appear in a document on its own, so both halves are variants of
#: the one roster entry.
DBA_SEPARATORS = ("d/b/a", "dba", "d.b.a.", "doing business as", "trading as", "t/a")

#: Punctuation `fold` keeps. A hyphen is load-bearing inside a real name
#: (`Smith-Jones Ltd`), and a slash is what makes `d/b/a` still detectable
#: after folding.
_KEPT_PUNCTUATION = frozenset("-/")

_LEADING_ARTICLE = "the"

_DBA_PATTERN = re.compile(
    r"(?<![\w/.])(?:"
    + "|".join(
        re.escape(sep) for sep in sorted(DBA_SEPARATORS, key=lambda s: (-len(s), s))
    )
    + r")(?![\w/.])",
    re.IGNORECASE,
)


def fold(name: str) -> str:
    """`name` reduced to the form two spellings of one entity share.

    NFKD-decompose and drop combining marks (`Société` -> `Societe`),
    casefold, expand `&` to `and`, DELETE every punctuation mark except
    hyphen and slash, collapse whitespace, and strip a leading definite
    article.

    Punctuation is deleted rather than replaced by a space on purpose:
    deleting is what makes `G.m.b.H.` fold onto `gmbh`, which is the
    single most common suffix spelling in this repo's corpus and the
    example the finding is written around. The cost is that a name whose
    parts are joined by punctuation with NO surrounding space
    (`Acme,Inc`) folds to `acmeinc`; that is a fold MISS, which is the
    safe direction (see the module docstring).

    Idempotent: `fold(fold(x)) == fold(x)` for every `x`, including names
    that begin with repeated articles. A non-string returns `""`.
    """
    if not isinstance(name, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_marks.casefold().replace("&", " and ")
    kept = [
        ch
        for ch in lowered
        if ch.isalnum() or ch.isspace() or ch in _KEPT_PUNCTUATION
    ]
    text = " ".join("".join(kept).split())
    # Looped, not a single strip: "The The Depot" must reach the same
    # answer as folding its own output, or `fold` is not idempotent.
    while True:
        head, _, rest = text.partition(" ")
        if head != _LEADING_ARTICLE or not rest.strip():
            break
        text = rest.strip()
    return text


# Longest spelling first so `proprietary limited` wins over `limited` and
# `pty ltd` over `ltd`; the secondary sort key keeps the order stable -- and
# therefore the stripping deterministic -- across interpreter runs. Built
# here rather than beside the table above because it is expressed in FOLDED
# spellings and `fold` has to exist first.
_FOLDED_SUFFIXES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        {
            (folded, canonical)
            for canonical, spellings in LEGAL_FORM_SUFFIXES.items()
            for folded in (fold(spelling) for spelling in spellings)
            if folded
        },
        # The canonical is part of the key too: the set above has arbitrary
        # iteration order, so two canonicals folding onto ONE spelling would
        # otherwise pick a winner that varies with PYTHONHASHSEED. There is
        # no such collision today (`_assert_no_suffix_collision` below pins
        # that), and this keeps the order defined if one is ever introduced.
        key=lambda pair: (-len(pair[0]), pair[0], pair[1]),
    )
)


def _assert_no_suffix_collision() -> None:
    """Two canonical tokens must not fold onto one spelling.

    They would be indistinguishable after `fold`, so `strip_legal_form`
    would report one of them arbitrarily and `recognition_variants` would
    emit the other's spellings for a name that never carried them. Checked
    at import because the table is a literal: a bad edit fails loudly here
    rather than quietly downstream.
    """
    seen: dict[str, str] = {}
    for canonical, spellings in LEGAL_FORM_SUFFIXES.items():
        for spelling in spellings:
            folded = fold(spelling)
            if not folded:
                continue
            previous = seen.setdefault(folded, canonical)
            if previous != canonical:
                raise ValueError(
                    f"LEGAL_FORM_SUFFIXES: {folded!r} is claimed by both "
                    f"{previous!r} and {canonical!r}"
                )


_assert_no_suffix_collision()


def split_dba(name: str) -> list[str]:
    """`name` split on any `DBA_SEPARATORS` entry (case-insensitive,
    word-bounded), or `[name]` when it carries none.

    "Synthetic Holdings LLC d/b/a Synthetic Legal" is ONE roster entry and
    TWO names a document may use, so both halves are returned. Blank halves
    (a name that begins or ends with a separator) are dropped; if that
    leaves nothing, the original is returned unchanged rather than an empty
    list, so a caller never has to special-case "the split ate the name".
    """
    if not isinstance(name, str) or not name.strip():
        return [name] if isinstance(name, str) else []
    parts = [part.strip() for part in _DBA_PATTERN.split(name)]
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        return [name]
    return parts


def strip_legal_form(folded: str) -> tuple[str, str | None]:
    """`(core, canonical_suffix)` for an ALREADY-FOLDED name.

    Removes exactly ONE trailing legal-form suffix, longest spelling first
    (`pty ltd` before `ltd`, `proprietary limited` before `limited`), and
    returns the canonical token it folded onto. `(folded, None)` when the
    name carries no suffix this table knows.

    A name that is NOTHING BUT a suffix (`Limited`) keeps it: stripping
    would leave an empty core, and an empty core is an identity every
    suffix-only name would share.
    """
    text = " ".join((folded or "").split())
    if not text:
        return "", None
    for spelling, canonical in _FOLDED_SUFFIXES:
        if text.endswith(" " + spelling):
            core = text[: -(len(spelling) + 1)].strip()
            if core:
                return core, canonical
    return text, None


def recognition_key(name: str) -> str:
    """The identity two spellings of one entity are deduplicated on.

    `strip_legal_form(fold(name))[0]` — so `Synthetic Holdings GmbH`,
    `SYNTHETIC HOLDINGS G.m.b.H.` and `The Synthetic Holdings` all key the
    same. LOSSY BY DESIGN (the suffix is gone): never store, render or
    show this value — see the module docstring.
    """
    return strip_legal_form(fold(name))[0]


def recognition_variants(name: str) -> list[str]:
    """Every spelling `name` might plausibly appear under in a document,
    sorted and deduplicated — deterministic, so a caller can log it.

    The name as typed; each half of a `d/b/a` compound; each of those
    folded; each folded half's suffix-less core; and that core under every
    alternative spelling of the suffix it carries. An empty or non-string
    name yields `[]`.

    These are for SEARCHING extracted document text. They are advisory and
    are deliberately NOT rendered into a prompt: the flat recognition set
    the model is shown stays the canonical typed spellings
    (`opf_prompt.resolve_party_recognition_set`), with a fixed sentence
    telling it that suffixes, punctuation and `d/b/a` halves name the same
    entity — see issue #55's "do not render variants" default.
    """
    if not isinstance(name, str):
        return []
    raw = name.strip()
    if not raw:
        return []

    variants: set[str] = {raw}
    for part in split_dba(raw):
        part = part.strip()
        if not part:
            continue
        variants.add(part)
        folded = fold(part)
        if not folded:
            continue
        variants.add(folded)
        core, canonical = strip_legal_form(folded)
        if core:
            variants.add(core)
        if canonical:
            for spelling in LEGAL_FORM_SUFFIXES[canonical]:
                variants.add(f"{core} {spelling}".strip())
    return sorted(variants)
