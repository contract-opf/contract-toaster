#!/usr/bin/env python3
"""
Input normalization: documented accept/reject rule for pre-existing revisions.

Issue #65 (BLOCKING GATE): "Redline anchoring + fail-closed patching + input
normalization".

## Problem this solves

An uploaded `.docx` can carry pre-existing tracked changes, comments, hidden
text, fields, footnotes, and embedded objects that would otherwise corrupt
both the standard-form diff (issue #64) and the redline patch (issue #65,
the redline writer). Before any review work, the document must pass a
normalization pass that applies a DOCUMENTED accept/reject rule to existing
revisions and produces a clean canonical body -- or, if it cannot, fails
closed rather than guessing.

This module implements that rule over the PARSED revision list for a
paragraph (the actual OOXML `<w:ins>`/`<w:del>`/`<w:commentReference>`
extraction is owned by the extraction stage, issue #80; this module is the
decision layer that stage calls per paragraph, and is unit-testable without
any OOXML parsing).

See: ARCHITECTURE.md -> "Input normalization (before review)"

## The documented rule (normative) -- revised per issue #199

Issue #199 (audit finding): a counterparty markup of the standard form -- the
flagship use case -- IS a document full of PENDING tracked changes; that is
what a redline is. The original rule below fail-closed on ANY unresolved
tracked change, which routes every realistic counterparty redline to
MANUAL_REVIEW_REQUIRED and defeats the product's core scenario. It also
relied on an 'accepted' status that does not exist in real OOXML: accepting
a change strips the <w:ins>/<w:del> markup entirely, so any revision still
present in a real file is by definition pending.

The rule is redefined: pending counterparty <w:ins>/<w:del> revisions ARE the
proposal under review. Normalization ACCEPTS-ALL a single, unambiguous
pending revision into the operative draft and records that disposition in a
normalization note; the downstream standard-form diff (issue #64) recovers
what changed against the canonical form, making the tracked-change markup
itself redundant as a signal once accepted. Fail-closed is RESERVED for
genuinely ambiguous structures, where silently picking a disposition would be
the "apply the closest match" guess this pipeline prohibits.

For each revision attached to a paragraph:

  | Revision type    | status                  | Disposition                                      |
  |-------------------|--------------------------|---------------------------------------------------|
  | tracked_change    | accepted                 | ACCEPT -- resulting_text is the operative text    |
  | tracked_change    | unresolved / rejected    | ACCEPT-ALL -- the pending revision(s) ARE the      |
  |                    | (any cluster/author      | proposal under review; resulting_text becomes the |
  |                    | count -- issue #563;     | operative text; disposition recorded in a         |
  |                    | inside a field code or   | normalization note, naming cluster/author counts   |
  |                    | not -- issue #530)       | when more than one is present, or the field's      |
  |                    |                          | resolved text when inside a field code             |
  | comment            | any (open or resolved)  | Comments NEVER gate normalization by themselves --|
  |                    |                          | see "Comments never gate" below                    |
  | hidden_text        | n/a                      | STRIP -- never reaches the clean body              |
  | field               | n/a                     | RESOLVE -- replaced by its literal `field_result`  |
  | paragraph_mark     | unresolved               | DISCLOSE ONLY -- the proposed paragraph merge or  |
  |                    | (`operation` is          | split is recorded in a normalization note and      |
  |                    | `deleted` or `inserted`) | applied NOWHERE; no text changes (issue #113)      |

A pending tracked change is "AMBIGUOUS" (still fails closed) when the
following remains true. Issue #563 narrowed this from four conditions to
two, and issue #530 narrows it further to just this one -- see "Multi-
cluster / multi-author acceptance" and "Field code resolution" below for why
more than one pending cluster/author, or a pending change inside a field
code, no longer gate on their own:

  1. Malformed records -- a pending (or accepted) tracked_change whose
     `resulting_text` KEY IS ABSENT (or carries a non-string value) cannot be
     accepted into anything; the operative text is unknown. This is the ONE
     condition that still fails closed: there is genuinely no text to read,
     and no amount of model intelligence helps a truncated or corrupt
     revision record. A `resulting_text` that is PRESENT and EMPTY is NOT
     this condition -- see "Whole-paragraph deletion" below.

### Whole-paragraph deletion (issue #93)

`resulting_text == ""` is a determinate answer, not a missing one: it is the
shape the extractor writes when every run in a `<w:p>` sits inside a
`<w:del>` -- a counterparty striking a whole clause, one of the commonest
things a redline does. `extraction_normalization_stage._build_paragraph_record`
computes `"".join(builder.resulting_parts).strip()` and stamps that value onto
every cluster on the paragraph, so a wholly-struck paragraph arrives here as a
pending `tracked_change` carrying `resulting_text: ""`.

Before issue #93 both the pending branch and the `accepted` branch tested
`if not resulting_text`, which conflates "the key is absent" (genuinely
malformed) with "the key is present and empty" (a complete deletion). The
result was that an ordinary whole-paragraph strike-out failed the WHOLE
upload closed with a note telling the attorney their document was malformed.
It is not: the operative text after accepting that change is the empty
string.

Both branches now test PRESENCE (`isinstance(..., str)`), not truthiness.
Absent/non-string keeps today's fail-closed message verbatim -- nothing about
the fail-closed posture is relaxed. Present-and-empty accepts, and the
paragraph is reported as DELETED IN FULL:

  * `_normalize_paragraph` returns the additive key
    `"deleted_in_full": True` alongside `clean_text: ""`;
  * `normalize()` OMITS that paragraph from `clean_body` entirely rather
    than carrying it as an empty `"<heading>: "` clause -- a clause the
    counterparty struck is not a clause with no text, it is not there;
  * the disposition note says so, and for the accept-all paths it keeps the
    established sentence shape: the FIRST sentence still ends with the exact
    literal `accepted-all into the operative draft.` tail that
    `frontend/src/toaster/receipt.ts`'s `acceptedChangesSummary` counts
    (and still matches its structured parse, so a struck paragraph is
    counted as the one pending edit it is), and a SECOND sentence names the
    deletion. The `accepted`-status branch, which is not an accept-all and
    never was, records its own note WITHOUT that tail, so it cannot inflate
    that count.

`extraction_normalization_stage.normalize_paragraphs` needs no change for
this: it already drops a physical paragraph whose clean text is empty from
`text`/`physical_spans`/`physical_p_indexes` (see its docstring, "A physical
paragraph whose own clean text is empty"), which is the structured-path
equivalent of the `clean_body` omission above.

### Tracked paragraph marks (issue #113)

Word records "merge this paragraph with the next one" as a DELETED paragraph
mark and "split this paragraph in two" as an INSERTED one --
`<w:pPr><w:rPr><w:del/></w:rPr></w:pPr>` and its `<w:ins/>` twin. The marker
wraps no run, so it changes neither text stream the extractor builds, and
before issue #113 it produced no revision record here at all: the proposal
was accepted in complete silence -- stripped from the materialized bytes by
`extraction_normalization_stage._splice_accept_all` without the `<w:p>`
siblings ever being merged or split, and mentioned nowhere the attorney
could see it. That is precisely the "never silent" guarantee ARCHITECTURE.md
attaches to the accept-all path.

`extraction_normalization_stage._paragraph_mark_revisions` now emits a
`paragraph_mark` record for each one, and the disposition here is DISCLOSE
ONLY: a note naming the proposal and stating that it is not applied.
`clean_text` is untouched, `deleted_in_full` is unaffected, and no paragraph
is joined or divided -- so this changes what the attorney is TOLD, never
what the model reads or what the delivered redline contains. The note is
appended AFTER whatever note the paragraph's own content revisions produced
and, like the `accepted`-status branch, deliberately does NOT end with the
`accepted-all into the operative draft.` tail: nothing was accepted into the
operative text, and `frontend/src/toaster/receipt.ts`'s
`acceptedChangesSummary` counts that tail to decide whether its structured
parse saw every accepted edit -- a sentence carrying the tail but not its
`Paragraph 'X': ...` shape would make that check fail and drop the
attorney's whole pending-edit summary line.

An `operation` that is neither `deleted` nor `inserted` fails the document
closed, exactly like an unknown tracked_change `status` above and for the
same reason: there is no documented disposition to apply. Like that branch,
it is unreachable from the OOXML extractor (which only ever writes the two
known values) and exists for the hand-built `normalize()` entry point.

### Multi-cluster / multi-author acceptance (issue #563)

Before issue #563, more than one pending tracked_change on a single
paragraph -- nested/conflicting clusters, or the same split across more than
one author -- failed the WHOLE DOCUMENT closed: the stated reasoning was that
the pipeline "cannot silently decide how multiple pending edits compose or
order." That reasoning does not actually hold for how these revisions reach
this function in the real pipeline: `resulting_text` on EVERY cluster's
revision record for a paragraph is already the SAME value -- the whole
paragraph's accept-all text, computed once from every cluster combined
(`extraction_normalization_stage._build_paragraph_record`) and stamped onto
each cluster's entry -- so there is nothing left to compose or order between
clusters; accepting the first pending revision's `resulting_text` accepts
every cluster on the paragraph at once, same as accepting a single cluster
does today. What changes with more than one cluster or author is
DISCLOSURE, not ambiguity: the normalization note names the paragraph's
heading and how many pending clusters/authors were folded into the operative
text, so an attorney sees that multiple pending edits were combined rather
than the document being silently accepted -- or refused outright, defeating
the flagship multi-cluster/multi-author counterparty-markup scenario the
same way issue #199 found the single-cluster case defeated it.

Accepting the paragraph TEXT this way is only half of issue #563: the
paragraph's own `.docx` bytes still carry the raw `<w:ins>`/`<w:del>`
markup at this point. `extraction_normalization_stage.materialize_accept_all`
physically applies the SAME disposition to the OOXML itself, so every
downstream consumer -- the text the model reads, quote-locate, patch-apply,
and the delivered redline -- operates on one canonical, already-accepted
document, never a text/bytes mismatch.

### Field code resolution (issue #530)

Before issue #530, a pending tracked change whose `inside_field_code` is
true (a cross-reference, auto-numbering, or date field the counterparty is
live-editing) failed the WHOLE DOCUMENT closed: the stated reasoning was
that which literal field result is operative is itself ambiguous, and
accepting blind would be a guess. Owner decision 2026-08-09 found that
reasoning did not survive contact with how the extractor actually resolves
one: `extraction_normalization_stage._process_fld_simple` bubbles a pending
change inside a field's result region up as an ORDINARY pending-revision
cluster (`inside_field_code=True` is a flag on that cluster, not a
different revision shape), whose `resulting_text` is the same
whole-paragraph accept-all text every other cluster on that paragraph
carries -- exactly the value accept-all already knows how to fold into the
operative draft for any other pending revision. There is no separate
"which field result wins" decision left to make; the field code case
resolves the SAME way an ordinary pending change does, and is disclosed
explicitly (naming the field's resolved text, not folded silently into the
generic accept-all note) because "which field result is operative" was the
exact question this branch used to refuse to answer.

Issue #99 corrected what that disclosure quotes. This section used to say
the cluster's `resulting_text` "is already the field's own resolved display
text", and the note quoted it on that basis. It is not: `resulting_text` is
the WHOLE PARAGRAPH's accept-all text, and the two coincide only when the
field is the entire paragraph -- the shape every fixture written for #530
happened to use. A DATE field resolving to `Feb 1` in the middle of a
sentence was therefore reported as resolving to that whole sentence. The
extractor now stamps the field's OWN accept-all display text on each such
cluster as `field_resulting_text`, and the note quotes that:

  | Key                    | Scope                                        |
  |------------------------|----------------------------------------------|
  | `resulting_text`       | the whole paragraph -- the operative text     |
  | `field_resulting_text` | this field alone -- what the note quotes      |

A record whose `field_resulting_text` is absent (a hand-built dict reaching
the `normalize()` entry point; the OOXML extractor always stamps one) or
empty gets the disposition sentence with no quoted value, rather than a
quoted value that is not the field's.

The one condition this does NOT touch is a malformed record with NO
`resulting_text` KEY -- there being no text at all is a different problem than
"the field text is fine, but which field's text applies", and that branch
keeps failing closed regardless of whether it also happens to sit inside a
field code (see `build_unnormalizable_report`'s message for that path).

### Comments never gate

Comments never gate normalization by themselves, REGARDLESS of whether the
same paragraph also carries a pending tracked change. (Prior to issue #199,
an open comment co-located with an unresolved tracked change compounded into
a fail-closed outcome; now that a lone pending tracked change accept-alls
cleanly, an open comment adds no additional ambiguity to that disposition --
it is preserved only as out-of-band reviewer commentary, not as a normalizer
input.) An open comment on an otherwise-ambiguous paragraph (per the AMBIGUOUS
list above) does not change the outcome either way -- the tracked-change
structure alone determines fail-open vs. fail-closed.

### Control-character screen (issue #632)

Separate from the revision-disposition rule above, and applied to the
OPERATIVE text every accepted disposition produces (plus the paragraph's
heading), is a screen for INVISIBLE Unicode. Counterparty-controlled paper
can carry zero-width characters -- text the model reads and a human reviewer
cannot see -- and bidirectional overrides, which make rendered text read in
a different order than the logical order the model is shown. Both are
spoofing and prompt-injection surfaces in exactly the place this pipeline is
least able to notice them: the model's own input.

Screened character classes (any occurrence fails closed):

  U+200B - U+200F   zero-width space/non-joiner/joiner, LRM, RLM
  U+202A - U+202E   LRE, RLE, PDF, LRO, RLO (bidi embedding/override)
  U+2060 - U+2064   word joiner and the invisible math operators
  U+2066 - U+2069   LRI, RLI, FSI, PDI (bidi isolates)
  U+FEFF            anywhere OTHER than the leading position of the screened
                    string, where it is an ordinary byte-order mark

EXPLICITLY ALLOWED, never flagged: `\t` (a tab, already handled as ordinary
whitespace), `\n` (this module's own logical-paragraph join, not document
content), and U+00A0 NON-BREAKING SPACE, which is common and legitimate in
real contracts (defined terms, section references, currency amounts).

Disposition: FAIL CLOSED, exactly like any other unnormalizable input --
never strip, never repair. Silently deleting the characters would hand the
model a document that differs from the one the attorney sees, which is the
same class of text/bytes mismatch `materialize_accept_all` exists to
prevent; and "repair" would decide, on the counterparty's behalf, what the
operative text was supposed to say.

The fail-closed note is COUNTS-ONLY: how many characters were found, in
which scope (`heading` or `text` -- a fixed vocabulary, not document
content), and the offset of the first. It never echoes the offending run,
its neighbours, or the paragraph heading. `tools/document_spine_smoke.py`
classifies the note as the symbolic reason code
`suspicious_control_characters`.

Known cost, stated rather than discovered later: this screen is not free
against real paper. A scan of real counterparty documents found U+200B
inside ordinary `<w:t>` runs in a substantial fraction of them (a handful of
occurrences per document), so this screen WILL route such documents to
MANUAL_REVIEW_REQUIRED rather than reviewing them. That is the disposition
issue #632 chose deliberately -- an invisible character in the model's input
is not something this pipeline is willing to guess about -- but it is a
refusal rate, not a free win.

A document normalizes (`normalizable=True`) iff EVERY paragraph normalizes.
One un-normalizable paragraph fails the whole document closed -- a partially
normalized document (some clauses clean, one paragraph's operative text
unknown) is not a safe input to diff or review.

## Fail-closed status mapping (docs/output-contract.md, normative)

  status = MANUAL_REVIEW_REQUIRED
  reason = "unnormalizable_input"

This is a SYSTEM status, never a legal decision.

Usage:
  from normalize_input import normalize, build_unnormalizable_report

  result = normalize(document)
  # result == {"normalizable": True, "clean_body": "..."}
  #        or {"normalizable": True, "clean_body": "...", "normalization_notes": "..."}
  #           (present when one or more paragraphs had a pending tracked
  #           change accepted-all -- the disposition is always recorded)
  # or       {"normalizable": False, "normalization_notes": "..."}
"""

import sys
from typing import Any  # noqa: F401

# --- Control-character screen (issue #632) ---------------------------------
#
# See the module docstring, "Control-character screen", for WHY these classes
# and why the disposition is fail-closed rather than strip-and-continue.
# Inclusive `(first, last)` codepoint ranges; U+FEFF is handled separately
# below because its leading occurrence is a legitimate byte-order mark.
SUSPICIOUS_CONTROL_RANGES: tuple[tuple[int, int], ...] = (
    (0x200B, 0x200F),  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    (0x202A, 0x202E),  # LRE, RLE, PDF, LRO, RLO
    (0x2060, 0x2064),  # word joiner, invisible times/separator/plus
    (0x2066, 0x2069),  # LRI, RLI, FSI, PDI
)

BOM = "﻿"

# The STABLE substring `tools/document_spine_smoke.py` matches to classify
# this fail-closed path as `suspicious_control_characters`. Everything
# interpolated after it is a count, an offset, or a fixed scope word --
# never document text (module docstring, "Control-character screen").
CONTROL_CHARACTER_NOTE_PREFIX = "Suspicious control characters in extracted"

# Structured sub-reason for the control-character screen (issue #100). Every
# other fail-closed branch in `_normalize_paragraph` folds into the SAME
# top-level `reason="unnormalizable_input"` (docs/output-contract.md), which
# is why the frontend used to explain a control-character refusal with copy
# written for a malformed TRACKED CHANGE -- the one branch this token is
# not. This constant is the token's single source of truth; carried on the
# fail-closed result as `reason_detail` (see `_screen_control_characters`,
# `normalize`, `build_unnormalizable_report`) rather than re-derived by
# matching `CONTROL_CHARACTER_NOTE_PREFIX` as a substring of the note text
# the way `tools/document_spine_smoke.py` still does -- that string match is
# a legitimate way to classify a note after the fact for a smoke-test
# histogram, but it is not something a fail-closed RESULT should have to do
# to explain itself to the reader.
REASON_DETAIL_SUSPICIOUS_CONTROL_CHARACTERS = "suspicious_control_characters"

# Issue #113. The two documented `operation` values a `paragraph_mark`
# revision can carry (`extraction_normalization_stage.
# _PARAGRAPH_MARK_OPERATIONS` is the only producer), and the prose each one
# turns into. Split in two so the note names BOTH what the markup is
# (a deletion / an insertion of the paragraph mark) and what it PROPOSES
# (a merge / a split) -- a reader who knows neither Word's representation
# nor the jargon can still tell what the counterparty asked for.
_PARAGRAPH_MARK_OPERATION_WORDS = {"deleted": "deletion", "inserted": "insertion"}
_PARAGRAPH_MARK_PROPOSALS = {
    "deleted": "merging this paragraph with the one that follows it",
    "inserted": "splitting this paragraph into two",
}


def find_suspicious_control_characters(text: str) -> list[int]:
    """
    Return the OFFSETS (never the characters, never their context) of every
    zero-width or bidirectional control character in `text`, in order.

    U+FEFF at offset 0 is an ordinary byte-order mark and is NOT reported;
    anywhere else it is a zero-width no-break space and IS. Tab, newline and
    U+00A0 non-breaking space are explicitly allowed and never reported.
    """
    offsets: list[int] = []
    for index, char in enumerate(text):
        code = ord(char)
        if char == BOM:
            if index != 0:
                offsets.append(index)
            continue
        if any(low <= code <= high for low, high in SUSPICIOUS_CONTROL_RANGES):
            offsets.append(index)
    return offsets


def _screen_control_characters(heading: str, clean_text: str) -> dict | None:
    """
    Screen a paragraph's operative text (and its heading) for the character
    classes above. Returns an un-normalizable result carrying a COUNTS-ONLY
    note, or None when the paragraph is clean.

    Called on the OPERATIVE text -- after accept-all has chosen it -- so a
    hostile `resulting_text` can never reach a disposition note either.
    """
    for scope, value in (("heading", heading), ("text", clean_text)):
        offsets = find_suspicious_control_characters(value or "")
        if not offsets:
            continue
        return {
            "normalizable": False,
            "reason_detail": REASON_DETAIL_SUSPICIOUS_CONTROL_CHARACTERS,
            "note": (
                f"{CONTROL_CHARACTER_NOTE_PREFIX} {scope}: {len(offsets)} "
                f"zero-width or bidirectional control character(s) detected "
                f"(first at offset {offsets[0]}) -- invisible characters in "
                f"the model's input are a spoofing and prompt-injection "
                f"surface; refusing rather than stripping."
            ),
        }
    return None


def _normalize_paragraph(paragraph: dict) -> dict:
    """
    Apply the documented accept/reject rule to a single paragraph's
    `revisions` list. Returns:
      {"normalizable": True, "clean_text": "..."}
      or
      {"normalizable": True, "clean_text": "...", "note": "..."}
        (a pending tracked change was accepted-all; `note` records the
        disposition and MUST be surfaced, per issue #199)
      or
      {"normalizable": True, "clean_text": "", "note": "...",
       "deleted_in_full": True}
        (issue #93 -- the accepted revision strikes the WHOLE paragraph;
        `clean_text` is the empty string because that IS the operative text,
        and `deleted_in_full` tells the caller to omit the paragraph rather
        than carry an empty clause. ADDITIVE: a caller that does not know
        the key still reads `clean_text` correctly.)
      or
      {"normalizable": False, "note": "..."}
    """
    revisions = paragraph.get("revisions", [])
    heading = paragraph.get("heading", "<untitled>")
    clean_text = paragraph.get("text", "")

    pending_tracked_changes = []  # unresolved/rejected tracked_change revisions
    # Issue #113. Disclosure-only records (see the module docstring,
    # "Tracked paragraph marks"): each produces one sentence appended to
    # whatever note this paragraph otherwise carries, and influences no
    # text, no `deleted_in_full`, and no fail-closed decision except its own
    # unknown-`operation` guard below.
    paragraph_mark_notes: list[str] = []
    # Set by the `accepted` branch below when the accepted revision's
    # resulting_text is present-and-empty (issue #93). Deliberately NOT
    # conflated with "clean_text happens to be empty": a paragraph that was
    # always empty, or one emptied by nothing in particular, is not a
    # counterparty striking a clause and must keep its existing disposition.
    accepted_empty = False

    for rev in revisions:
        rev_type = rev.get("type")

        if rev_type == "tracked_change":
            status = rev.get("status")
            if status == "accepted":
                resulting_text = rev.get("resulting_text")
                # PRESENCE, not truthiness (issue #93): an absent key -- or a
                # non-string value, which is no more readable than an absent
                # one -- is the malformed record this branch has always
                # refused. A present, EMPTY string is a determinate answer:
                # the whole paragraph was struck.
                if not isinstance(resulting_text, str):
                    return {
                        "normalizable": False,
                        "note": (
                            f"Paragraph '{heading}': tracked change marked "
                            f"'accepted' but has no resulting_text -- "
                            f"malformed revision record."
                        ),
                    }
                accepted_empty = resulting_text == ""
                clean_text = resulting_text
            elif status in ("unresolved", "rejected"):
                # Pending revision -- disposition decided below, once every
                # revision on the paragraph has been seen (accept-all
                # requires knowing whether this is the ONLY pending
                # revision, per issue #199's ambiguity rules).
                pending_tracked_changes.append(rev)
            else:
                return {
                    "normalizable": False,
                    "note": (
                        f"Paragraph '{heading}': tracked change has unknown "
                        f"status '{status}' -- cannot determine operative text."
                    ),
                }

        elif rev_type == "comment":
            # Comments never gate normalization by themselves -- issue #199
            # explicitly retires the old "open comment + unresolved change"
            # compounding rule. See module docstring, "Comments never gate".
            continue

        elif rev_type == "hidden_text":
            # STRIP: hidden text never reaches the clean body, regardless of
            # its content. It is not surfaced as if it were visible text.
            continue

        elif rev_type == "paragraph_mark":
            # Issue #113. DISCLOSE ONLY -- see the module docstring.
            operation = rev.get("operation")
            if operation not in _PARAGRAPH_MARK_PROPOSALS:
                return {
                    "normalizable": False,
                    "note": (
                        f"Paragraph '{heading}': tracked paragraph mark has "
                        f"unknown operation '{operation}' -- no documented "
                        f"disposition; cannot safely normalize."
                    ),
                }
            paragraph_mark_notes.append(
                f"Paragraph '{heading}': a pending tracked paragraph-mark "
                f"{_PARAGRAPH_MARK_OPERATION_WORDS[operation]} "
                f"(author: {rev.get('author', 'unknown')}) proposes "
                f"{_PARAGRAPH_MARK_PROPOSALS[operation]}; it is disclosed but "
                f"not applied -- the paragraph structure of the operative "
                f"draft and of the delivered redline is unchanged."
            )

        elif rev_type == "field":
            # RESOLVE: a field's literal result is folded into clause text
            # (the field CODE itself -- e.g. "{ REF ... }" -- is discarded;
            # only the displayed result matters to the operative clause).
            field_result = rev.get("field_result")
            if field_result and field_result not in clean_text:
                clean_text = f"{clean_text} {field_result}".strip()

        else:
            return {
                "normalizable": False,
                "note": (
                    f"Paragraph '{heading}': unrecognized revision type "
                    f"'{rev_type}' -- no documented disposition; cannot "
                    f"safely normalize."
                ),
            }

    def _disclose_paragraph_marks(result: dict) -> dict:
        """Appends issue #113's disclosure sentence(s) to a NORMALIZABLE
        result's note, creating the note when the paragraph had none.
        Applied to every normalizable return below and to none of the
        fail-closed ones: a document that fails closed already reports the
        failure, and a paragraph-mark sentence cannot be read as a
        disposition for a paragraph that got none. Appended LAST so the
        accept-all sentence keeps first position -- it is the one
        `frontend/src/toaster/receipt.ts::acceptedChangesSummary` parses."""
        if not paragraph_mark_notes:
            return result
        appended = " ".join(paragraph_mark_notes)
        existing = result.get("note")
        result["note"] = f"{existing} {appended}" if existing else appended
        return result

    if not pending_tracked_changes:
        # Control-character screen on the operative text (issue #632) --
        # every fail-closed branch in this module documents itself; this one
        # is documented in the module docstring under "Control-character
        # screen".
        blocked = _screen_control_characters(heading, clean_text)
        if blocked:
            return blocked
        if accepted_empty and not clean_text:
            # Issue #93. `accepted_empty and not clean_text` rather than
            # `accepted_empty` alone: a `field` revision later in the same
            # paragraph can fold its resolved result back in, and a paragraph
            # that ends up with text is not a deleted one. No accept-all tail
            # here -- this is not an accept-all disposition, and
            # `acceptedChangesSummary` must not count it as a pending edit.
            return _disclose_paragraph_marks(
                {
                    "normalizable": True,
                    "clean_text": "",
                    "deleted_in_full": True,
                    "note": (
                        f"Paragraph '{heading}': tracked change marked "
                        f"'accepted' strikes the paragraph in full; the "
                        f"struck paragraph is omitted from the operative draft."
                    ),
                }
            )
        return _disclose_paragraph_marks({"normalizable": True, "clean_text": clean_text})

    # --- Pending tracked change(s): accept-all unless genuinely ambiguous ---
    #
    # Only ONE condition still fails closed (issue #563 narrowed this from
    # four to two; issue #530 narrows it further to just this one -- see
    # module docstring, "Multi-cluster / multi-author acceptance" and "Field
    # code resolution"): a malformed record with no resulting_text. Neither
    # more than one pending cluster/author, nor a pending change inside a
    # field code, fail closed on their own any more.

    inside_field_code = [
        rev for rev in pending_tracked_changes if rev.get("inside_field_code")
    ]

    # ACCEPT-ALL. Per issue #199, the pending counterparty revision IS the
    # proposal under review; the downstream standard-form diff recovers what
    # changed. Per issue #563, more than one pending cluster/author accepts
    # the SAME way: every cluster's resulting_text is identical for a given
    # paragraph (see module docstring), so the first one already IS the
    # whole paragraph's operative text regardless of how many clusters or
    # authors contributed pending edits to it. Per issue #530, a pending
    # change inside a field code accepts the SAME way too: its
    # resulting_text is already the field's own resolved display text (see
    # module docstring, "Field code resolution") -- there is no separate
    # "which field result wins" decision left to make.
    rev = pending_tracked_changes[0]
    resulting_text = rev.get("resulting_text")
    # PRESENCE, not truthiness (issue #93) -- see the module docstring,
    # "Whole-paragraph deletion". An absent key (or a non-string value) is
    # still the malformed record this branch has always refused, with the
    # same message; a present, EMPTY string is a whole-paragraph strike-out,
    # whose operative text is determinately "".
    if not isinstance(resulting_text, str):
        return {
            "normalizable": False,
            "note": (
                f"Paragraph '{heading}': pending tracked change has no "
                f"resulting_text -- malformed revision record; cannot "
                f"determine the operative text to accept."
            ),
        }
    deleted_in_full = resulting_text == ""
    clean_text = resulting_text

    # Control-character screen (issue #632) runs BEFORE any disposition note
    # is built: the field-code note below quotes `resulting_text` back, so a
    # hostile accepted text must fail closed here rather than be echoed into
    # a note that is surfaced to the attorney.
    blocked = _screen_control_characters(heading, clean_text)
    if blocked:
        return blocked

    if inside_field_code:
        # Named explicitly (issue #530) rather than folded into the generic
        # accept-all note below: "which field result is operative" was the
        # exact question this branch used to refuse to answer, so the
        # disclosure states what it resolved to, not just that something
        # pending was accepted. Two sentences, deliberately: the FIRST ends
        # with the exact "accepted-all into the operative draft." tail every
        # other accept-all note ends with (frontend/src/toaster/receipt.ts's
        # `acceptedChangesSummary` counts sentences by that literal tail),
        # so a field-code disposition is never silently absent from that
        # count-consistency check the way an entirely different tail would
        # be -- it correctly trips the "parsed fewer than the tail count"
        # safeguard and drops the compact summary rather than under-
        # reporting how many dispositions actually happened. The SECOND
        # sentence, not part of that established template, is what actually
        # names what the field resolved to.
        note = (
            f"Paragraph '{heading}': pending tracked change inside a field "
            f"code accepted-all into the operative draft."
        )
        # ISSUE #99. That second sentence quotes the FIELD's own resolved
        # display text (`field_resulting_text`) and never `resulting_text`,
        # which is the WHOLE PARAGRAPH's accept-all text -- the same
        # paragraph-level value
        # `extraction_normalization_stage._build_paragraph_record` stamps on
        # every cluster the paragraph carries. A DATE field resolving to
        # "Feb 1" inside a longer sentence was reported as resolving to that
        # entire sentence: a false statement about the one question this
        # branch exists to answer. The two values coincide only when the
        # field IS the whole paragraph, which is why every fixture written
        # for issue #530 stayed green over the bug.
        #
        # Distinct values, in document order: ONE paragraph can carry
        # pending edits in MORE THAN ONE field (two `<w:fldSimple>`s, each
        # under its own live edit), and each field's cluster carries that
        # field's own text. Deduplicated because several clusters inside a
        # SINGLE field (two authors editing its result back-to-back) all
        # carry that one field's text, and repeating it would claim more
        # fields than the paragraph has.
        field_texts: list[str] = []
        for field_rev in inside_field_code:
            field_text = field_rev.get("field_resulting_text")
            # Empty and absent are both dropped rather than quoted, and for
            # the same reason issue #93 gave for a struck paragraph: "the
            # field now resolves to ''" reads as a parse failure rather than
            # as the struck field result it is. Absent means a hand-built
            # record from the `normalize()` entry point that never said what
            # the field resolved to (the OOXML extractor always stamps one)
            # -- so this says nothing rather than something false. The FIRST
            # sentence still discloses the disposition either way; a
            # field-code accept-all is never silent.
            if isinstance(field_text, str) and field_text and field_text not in field_texts:
                field_texts.append(field_text)
        if field_texts and not deleted_in_full:
            # The deleted-in-full sentence below replaces this one: naming
            # what a field resolves to contradicts "the struck paragraph is
            # omitted from the operative draft" in the very next sentence.
            quoted = [f"'{text}'" for text in field_texts]
            if len(quoted) == 1:
                note += f" The field now resolves to {quoted[0]}."
            else:
                note += (
                    f" The fields now resolve to "
                    f"{', '.join(quoted[:-1])} and {quoted[-1]}."
                )
    elif len(pending_tracked_changes) > 1:
        authors = {rev.get("author") for rev in pending_tracked_changes}
        note = (
            f"Paragraph '{heading}': {len(pending_tracked_changes)} pending "
            f"tracked changes from {len(authors)} author(s) accepted-all "
            f"into the operative draft."
        )
    else:
        note = (
            f"Paragraph '{heading}': pending tracked change "
            f"(author: {rev.get('author', 'unknown')}, status: {rev.get('status')}) "
            f"accepted-all into the operative draft."
        )

    if deleted_in_full:
        # Issue #93. APPENDED as a second sentence, never woven into the
        # first: the first sentence keeps the exact `accepted-all into the
        # operative draft.` tail AND the structured shape
        # `frontend/src/toaster/receipt.ts`'s `acceptedChangesSummary`
        # parses, so a struck paragraph is counted as the pending edit it is
        # rather than tripping that function's parsed-vs-tail-count
        # safeguard. The second sentence is what names the disposition, and
        # carries no document text -- only the fixed words below.
        #
        # Worded at PHYSICAL-paragraph granularity, deliberately: this
        # function is called once per physical `<w:p>` by
        # `extraction_normalization_stage.normalize_paragraphs` while a
        # LOGICAL clause heading is interpolated over several such calls, so
        # "the paragraph" here must never be read as "the clause". It says
        # only that THIS struck paragraph contributes nothing -- never that
        # the clause it sits under, which may carry other, surviving
        # physical paragraphs, was itself removed. (`normalize_input.normalize`
        # is the one caller where paragraph and clause coincide, and its own
        # `clean_body` omission -- not this note's wording -- is what makes
        # the clause-level claim there.)
        note += " The struck paragraph is omitted from the operative draft."
        return _disclose_paragraph_marks(
            {
                "normalizable": True,
                "clean_text": "",
                "deleted_in_full": True,
                "note": note,
            }
        )

    return _disclose_paragraph_marks(
        {"normalizable": True, "clean_text": clean_text, "note": note}
    )


def normalize(document: dict) -> dict:
    """
    Normalize a full document (a dict with a `paragraphs` list, each carrying
    `heading`, `text`, and a `revisions` list -- see tests/redline/fixtures/
    for the shape).

    Returns:
      Normalizable, no pending changes accepted:
        {"normalizable": True, "clean_body": "<heading>: <clean_text>\\n..."}
      Normalizable, one or more pending tracked changes accepted-all (issue
      #199 -- the disposition is always recorded, never silent):
        {"normalizable": True, "clean_body": "...",
         "normalization_notes": "<joined accept-all disposition notes>"}
      Un-normalizable (fails closed -- genuinely ambiguous structures only):
        {"normalizable": False, "normalization_notes": "<joined notes>"}
        (plus "reason_detail": "<token>" when the FIRST failing paragraph's
        disposition carries one -- see below)

    A document normalizes iff every paragraph normalizes -- one
    un-normalizable paragraph fails the whole document closed.

    A paragraph whose accepted operative text is EMPTY because the
    counterparty struck the whole clause (issue #93 -- `_normalize_paragraph`
    returns `deleted_in_full`) contributes NO line to `clean_body`; its
    disposition note is still recorded in `normalization_notes`, so the
    omission is disclosed rather than silent.

    `reason_detail` (issue #100): a structured sub-reason for WHY the
    document failed to normalize, distinct from the free-text `note` --
    e.g. `REASON_DETAIL_SUSPICIOUS_CONTROL_CHARACTERS` when
    `_screen_control_characters` is what refused. Most fail-closed branches
    set no `reason_detail` at all (an ordinary malformed-tracked-change
    refusal has none), which this function reads as "no sub-classification
    -- the default `unnormalizable_input` copy already fits." When more than
    one paragraph fails with a reason_detail set, the FIRST one found wins
    (same "first offset wins" convention `find_suspicious_control_characters`
    already uses within a single paragraph) -- a document-level result names
    ONE reason, not a set of them.
    """
    paragraphs = document.get("paragraphs", [])

    fail_notes = []
    fail_reason_detail = None
    accept_notes = []
    clean_lines = []

    for paragraph in paragraphs:
        result = _normalize_paragraph(paragraph)
        if not result["normalizable"]:
            fail_notes.append(result["note"])
            if fail_reason_detail is None and result.get("reason_detail"):
                fail_reason_detail = result["reason_detail"]
        else:
            heading = paragraph.get("heading", "<untitled>")
            if not result.get("deleted_in_full"):
                # A paragraph the counterparty struck in full is OMITTED
                # (issue #93), not carried as an empty `"<heading>: "`
                # clause: an empty clause reads to every downstream consumer
                # as a clause that exists and says nothing, which is not what
                # the document says. The disposition is still disclosed --
                # `result["note"]` is appended below either way, so the
                # omission is never silent.
                clean_lines.append(f"{heading}: {result['clean_text']}")
            if result.get("note"):
                accept_notes.append(result["note"])

    if fail_notes:
        failed: dict = {
            "normalizable": False,
            "normalization_notes": " ".join(fail_notes),
        }
        if fail_reason_detail:
            failed["reason_detail"] = fail_reason_detail
        return failed

    normalized = {
        "normalizable": True,
        "clean_body": "\n".join(clean_lines),
    }
    if accept_notes:
        # Disposition is always recorded, never silent -- issue #199.
        normalized["normalization_notes"] = " ".join(accept_notes)
    return normalized


def build_unnormalizable_report(normalize_result: dict) -> dict:
    """
    Build the `analysis_report` artifact for the un-normalizable-input
    fail-closed path, per docs/output-contract.md -> "Fail-closed internal
    analysis report" -> "Format" (normative field shape).

    This artifact NEVER carries a `decision` field: the fail-closed outcome
    is a SYSTEM status (`status=MANUAL_REVIEW_REQUIRED`), never a legal
    decision.

    `reason_detail` (issue #100): present, alongside the fixed
    `reason="unnormalizable_input"` above, ONLY when `normalize_result`
    carries one -- i.e. `normalize()` found a sub-classified fail-closed
    branch (currently just the control-character screen). ABSENT, never a
    null placeholder, for every other un-normalizable refusal (a malformed
    tracked-change record, an unknown revision status, ...), matching the
    "absent, never null" convention `normalization_notes` itself would use
    if it had nothing to say. `reason` itself is UNCHANGED either way --
    every un-normalizable path still keys off it the same as before this
    issue; `reason_detail` only narrows what it means, for the one
    consumer (the frontend copy map) that needs to say something more
    specific than "a tracked change" when there is no tracked change.
    """
    report: dict = {
        "report_type": "analysis_report",
        "reason": "unnormalizable_input",
        "fail_closed_path": (
            "The normalization pass could not produce a clean, unambiguous "
            "document body."
        ),
        "changes_not_applied": [],
        "normalization_notes": normalize_result.get("normalization_notes", ""),
        "status": "MANUAL_REVIEW_REQUIRED",
    }
    reason_detail = normalize_result.get("reason_detail")
    if reason_detail:
        report["reason_detail"] = reason_detail
    return report


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test over an inline clean document."""
    sample_document = {
        "paragraphs": [
            {
                "heading": "Governing Law",
                "text": "This Agreement shall be governed by the laws of Delaware.",
                "revisions": [],
            }
        ]
    }
    print(normalize(sample_document))


if __name__ == "__main__":
    main()
    sys.exit(0)
