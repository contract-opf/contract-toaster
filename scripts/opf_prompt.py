#!/usr/bin/env python3
"""
OPF prompt composition -- issue #284, rebuilt on the DIGEST for OPF 0.3, and
on digest_version 4 (OPF 0.5) by issue #129, and on digest_version 5 (OPF 0.6,
the only format this repo reads) by issue #177.

Pure composition: turn a schema-shaped OPF document (see `scripts/opf_load.py`
for the loader/validator this consumes) plus an optional approved review policy
(see `scripts/policy_load.py`) into review system-prompt blocks.
**Digest + Posture + Floor + Policy in, prompt blocks out.** No I/O, no model
call, and NO REFUSALS beyond the no-digest one below: governance decisions --
which empty sections are acceptable, and what gets recorded when an operator
accepts one -- live in `scripts/review_knowledge.py`, the single home for them.

## A block is absent or it has content (PR F1)

Never an empty string. The composer used to build a fixed 3-or-4-element list,
so an OPF with `posture == {}` shipped `blocks[0] == ""` and an OPF with no
Floor invariants shipped the non-negotiable-invariants intro over an empty
list. Neither is hypothetical: the real playbook ships BOTH `posture == {}` and
`floor == {}`, which made those two lies the day-one production prompt. The
composer now omits what it has nothing to say about, which means POSITION IS
NOT IDENTITY -- match a block by its intro constant, never by `blocks[n]`.

## The wholesale Evidence dump is RETIRED (read this before re-adding one)

This module used to project the WHOLE `evidence` section into the prompt with a
single `json.dumps` -- the reasoning being "evidence IS the knowledge, so
nothing else is dropped". That design was measured at roughly ONE MILLION tokens
on the real corpus. It does not fit any model's context; it could never have run
a single review. It was not too expensive, it was impossible.

The engine-built `digest` section replaces it: the same knowledge projected
for exactly this use. Digest 5 (OPF 0.6) is verdict-free precedent -- per
clause, our standard (in its parts), how many signed deals opened with it and
kept it, the positions of the signed texts, the signed variants (with how many
were reached from our standard and how many were signed as proposed), the
refused asks and the openings not signed as proposed -- each entry carrying a
citation that resolves back into the full OPF. The engine enforces a
~40K-token budget on it BY CONSTRUCTION (tightening per-list caps until it
fits, totals always uncapped), so the block this module emits is bounded by
the artifact, not by hope.

What the digest deliberately omits -- every signed text beyond its 300-char
summary, the per-deal precedent record, the per-part reasons -- is not lost
from the playbook, and this module does not reach it. Depth is
deterministic dossier selection appended by orchestration to the CRITIC's
role suffix (#40, `scripts/critic_dossiers.py`, reading OPF 0.6's
`dossiers`), not a model tool call and not a block here; the unwired
drill-down tool was retired in #161. Summaries by default is the trade that
lets the prompt fit, and the block says what each capped list left out.

So: if a future change is tempted to inline the precedent record, or to "just
include the evidence section too", it is re-proposing the 1M-token design.
Select a bounded dossier, not a dump.

## Plain facts, plain headers (issue #129)

Digest 2 was rendered under four judged headers (preferred variations /
concessions / unacceptable / exemplar forms), a stance sentence, frequency
bands and risk annotations. On the real playbook the stance was "mixed" for 22
of 24 clauses and the bands were inflated, and live runs showed the model
mining the "unacceptable" list as replacement language. Digest 5 carries none
of that, and this module renders it as facts: counts of distinct deals, the
wording, and a citation. The one judged value is the `vs_standard` label (with
its reason and part map), rendered as an index the model must check against
the cited text, never as an instruction -- DIGEST_INTRO says so in as many
words. The display renames (`scripts/opf_terminology.py`) are retired with
digest 2: the digest 5 field names are already plain.

## Digest 5: dates, settled-ness, former forms, absence (issue #177)

Digest 5 adds four kinds of fact, and each renders as a fact, never a
stance (the #129 rule):

  - **Signing months.** Each clause, signed variant, collapsed equivalent
    entry and refused ask says "first signed <YYYY-MM>, last signed
    <YYYY-MM>" (`_signed_span`), and the corpus line gives the corpus span.
    No date is rendered finer than a month, and a verified-absent deal's
    own date is not rendered at all (the decision is recorded on
    `_signed_span`).
  - **How settled a label is** (`settled`). A label whose judgment is
    `adjudicated` (contested, decided by a third reading) or `unchecked`
    carries a marker telling the model to weigh it with more care;
    `agreed` and `owner` render plainly. It qualifies the LABEL, never the
    entry's weight, and DIGEST_INTRO says so.
  - **Our former forms** (`standard_history`). One line per former form:
    its label, RECONSTRUCTED when the producer rebuilt it from the corpus,
    its in-use dates, the signed deals that opened on it and what became of
    them, and its comparison with today's standard; then the owner's
    `why_changed` note, only when the owner wrote one -- the playbook never
    states a reason it does not carry. A signed variant whose deals opened
    on a former form says so by the form's label (read from
    `evidence.precedent[]` through its `precedent_ids`: the record's
    `opened_with` / `opened_with_form`, nothing else): its origin is ours.
  - **Absence.** "absent in N signed deals (verified)" and, separately,
    "possibly absent in M more; not verified" -- never summed, and neither
    is the same as "no precedent" (the heading's coverage and the closing
    no-precedent line, #158).

The owner's note and a form's label are owner/producer free text new in
0.6; both pass through the #162 block-tag neutralisation before they render
(`_neutralise`), on top of the load-time injection scan. A supplied form's
label also renders as its `prior_template:<label>` citation, so every
citation (`_fmt_cite`) is neutralised too.

Mirrors the block/cache-breakpoint CONVENTION of
`scripts/primary_review_pass.py:assemble_system_blocks` (fixed block order,
deterministic projection) without reusing its Anthropic-message-API-shaped
return type: this function returns `list[str]`.

## What is excluded, and why nothing has to special-case it

`compose_opf_system_blocks` only ever reads these paths off the input doc:
`posture.system_prompt`, `digest` (`clauses`, `corpus` and, since issue #158,
`uncovered_clause_types`), `floor.invariants`,
`manifest.hard_rules` (issue #163: each rule's id, statement, clause link,
`required_presence`, `condition` and `fallback_language`, plus the public
`evidence.clauses[].title` its clause ids resolve to -- never its
`permissible_proof`), `perspective`, and `de_minimis` (plus, off the separate policy document,
`rules[].{id,strength,text}`, and off the caller's own `entity_roster`
param -- issue #678's deployment-scoped roster of our own legal entities,
not an OPF field and deliberately never becoming one). The operator's
standing instructions are NOT composed here: since issue #148 they are
per-review control text both passes send after the counterparty document
(`primary_review_pass.render_request_control_text`), so these blocks stay
the same bytes -- and the same cache entry -- for every review of the
playbook. Note `evidence` is NOT among
them any more:
the digest is the projection of it that reaches the model, and nothing reads
the full evidence section into a prompt. (Since issue #173 the deal ids in
`corpus.documents[]` and the `evidence.precedent[]` refs ARE read, by
`deal_aliases.alias_map`, but only to name each deal by an opaque alias in
the digest citations and the Floor rationale; no slug is rendered. Since
issue #177 each `evidence.precedent[]` record's `id`, `opened_with` and
`opened_with_form` are read too, by `_prior_form_openings`, only to say how
many of a signed variant's deals opened on which of our former forms; no
text is read.) Every other top-level section --
`posture.rubric` (excised from the schema entirely per engine #178, but this
function does not validate its input, so a caller-constructed dict carrying
it is simply never looked at), `posture.generation` (interview transcript),
`corpus`, `compiler`, `identity`, `baseline`, `composes`,
`dossiers`, `provenance_index` -- is
excluded automatically because the function never touches it, not via a
per-field denylist. That is also why a doc WITH `posture.rubric` produces
byte-identical output to the same doc without it (issue #284 AC).

## `x_*` vendor extensions (engine #180)

Unknown-provenance vendor extension keys (schema `patternProperties: {"^x_":
true}`, e.g. nested inside a digest clause entry) are stripped recursively
from every block source via `_strip_x_keys` -- the digest clauses and the
optional Context block alike.

## Fail-soft is kept; SILENT fail-soft is not (see `_omit`)

Every renderer below is `.get()`/`isinstance`-guarded, and deliberately so: a
digest entry missing an optional `n`, or carrying a shape this module does not
recognize, must still produce a usable prompt rather than abort a review. But
fail-soft that says nothing is indistinguishable from nothing-to-say. That is
the same bug the vendored-schema shape contract exists to catch
(`tests/test_opf_schema_sync.py`), in prompt form: knowledge reaches the
artifact and never reaches the model, and every lineage field still records a
playbook as having governed the review.

So each guard now RECORDS what it dropped, and `compose_opf_system_blocks`
prints one aggregated `WARNING:` line per kind of omission to stderr -- the
convention the retired standard-form diff used for "discarding this,
but visibly, so a drift signal is not silent data loss". Aggregated, not
per-entry: a real playbook has thousands of entries, and a warning per entry is
a warning nobody reads.

The guards' BEHAVIOR is unchanged, and so is this module's purity in the sense
that matters: the returned blocks are byte-identical to what they were, still a
deterministic function of the input. Only stderr is new.

## Coverage, and the silent-playbook default (issue #158)

The playbook is one input to a review, and for many clauses -- especially on
counterparty paper -- it has thin precedent or none. The digest makes that
legible and this module renders it: each clause heading states `precedent
in <n_deals> of <corpus.n_deals> deals`, and one closing line names the
`uncovered_clause_types` (recognised types with no precedent, each with the
taxonomy id an issue on it cites). What to do then is the toaster's own
direction, not the playbook's: `SILENT_PLAYBOOK_DEFAULT`, a constant inside
DIGEST_INTRO (so inside the shared prefix, #148), which the administrator's
standing instructions override per playbook. It adds no settings surface.

## Precedent stays descriptive

The Digest block renders what the corpus did ("Opened with our standard in 4
signed deals; signed it unchanged in 2") and never rephrases it as an
instruction. The distinction is load-bearing: the moment a prompt renders
precedent as "hold this clause" the playbook has stopped being evidence and
started being policy -- which is the policy document's job, not the digest's.
DIGEST_INTRO says so to the model in as many words.

Unlike the retired wholesale projection, this block renders a KNOWN shape (the
digest 5 clause), so a genuinely new digest field would not appear in the
prompt until this module renders it. That is the deliberate trade for text a
model can read: `digest_version` is checked at ingest (`scripts/opf_load.py`
accepts "5" only), so a shape this module does not understand is refused at
the door rather than silently half-rendered here, and
`tests/test_opf_schema_sync.py` pins every field this module reads at the
shape it reads it.

De-brand: no tenant-brand strings anywhere in this module (project de-brand rule).
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence  # noqa: UP035

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import deal_aliases  # noqa: E402
import entity_normalize  # noqa: E402
import hard_ledger  # noqa: E402

#: Composition modes. `review_knowledge.py` re-exports these and adds its own
#: MODE_V1_PROJECTION (the pre-OPF projection path, which does not compose here).
MODE_PLAYBOOK_DIGEST = "playbook_digest"
MODE_POLICY_ONLY = "policy_only"

BINDING_INTRO = (
    "RULES THAT BIND THIS REVIEW.\n"
    "Every entry is tagged with its provenance, and the two kinds DO NOT bind the same way. "
    "Read the tag before you act on the entry.\n"
    "  [floor:<id>] — a Floor invariant from the governed playbook. These are non-negotiable: "
    "flag any clause that violates one, and do not waive it.\n"
    "  [policy:<id>] — a `must` rule from the approved review policy. Binding when it applies. "
    "If one appears inapplicable, or in tension with the clause at hand or with another rule, "
    "FLAG IT FOR ATTORNEY REVIEW and say why. Do not silently override it, and do not silently "
    "comply with it against the facts — the determination is an attorney's to make, in either "
    "direction.\n"
    "A Floor invariant phrased in terms of \"the counterparty\" is DIRECTIONAL: the "
    "counterparty is the other party to this agreement, never us, and a term that "
    "runs in our favour does not violate such a rule.\n"
    "Every entry here is re-read against your finished redline by the closing self-check."
)

GUIDANCE_INTRO = (
    "WEIGHTED GUIDANCE (approved review policy, `should` rules).\n"
    "Each weighs heavily, but none is absolute: you may decide against one on the facts of this "
    "document, and when you do you must name the rule and say why."
)

# --- Omission recording (see "Fail-soft is kept; SILENT fail-soft is not") ---
#
# Collected for the duration of one compose_opf_system_blocks call and reported
# as aggregated stderr warnings at the end of it. Module-level rather than
# threaded through every renderer's return type, because the alternative is
# every `_fmt_*` returning (str, list[str]) and every caller forwarding it --
# which buries the rendering these functions exist to do.
_omissions: dict[str, list] | None = None


def _omit(kind: str, where: str) -> None:
    """Record that `kind` was dropped from the prompt at `where`.

    No-op outside a `_recording_omissions()` scope, so the renderers stay
    callable (and silent) in isolation.
    """
    if _omissions is None:
        return
    entry = _omissions.setdefault(kind, [0, where])
    entry[0] += 1


@contextmanager
def _recording_omissions() -> Iterator[dict[str, list]]:
    """Collect omissions, then report them, one aggregated line per kind.

    Yields the SAME dict `_omit` mutates in place (`{kind: [count, where]}`),
    so a caller that keeps the yielded reference (e.g.
    `compose_opf_system_blocks`'s `omissions_out` param below) still has its
    final contents after the `with` block exits and the module-level
    `_omissions` pointer has already been restored -- the dict object
    itself outlives the scope that names it.
    """
    global _omissions
    outer = _omissions
    _omissions = {}
    try:
        yield _omissions
    finally:
        collected, _omissions = _omissions, outer
        _report_omissions(collected or {})


def _report_omissions(collected: dict[str, list]) -> None:
    if not collected:
        return
    total = sum(count for count, _ in collected.values())
    print(
        f"WARNING: opf_prompt: {total} value(s) present in the OPF document were "
        f"NOT rendered into the review prompt. Each was dropped by a fail-soft "
        f"guard, so the prompt is well-formed and the model simply never sees "
        f"them. Listed by kind, with the first occurrence:",
        file=sys.stderr,
    )
    for kind, (count, where) in sorted(collected.items()):
        print(f"WARNING: opf_prompt:   x{count} {kind} (first: {where})", file=sys.stderr)


def _strip_x_keys(value: Any) -> Any:
    """Recursively drop any dict key prefixed with 'x_' (OPF vendor
    extensions, engine #180 -- unknown provenance, excluded from every
    prompt block regardless of how deeply nested)."""
    if isinstance(value, dict):
        return {
            key: _strip_x_keys(val)
            for key, val in value.items()
            if not key.startswith("x_")
        }
    if isinstance(value, list):
        return [_strip_x_keys(item) for item in value]
    return value


def _posture_block(opf_doc: dict, overrides: Optional[dict] = None) -> str | None:  # noqa: UP045
    """`overrides.posture.system_prompt` verbatim when a governed
    Posture-version override is given (issue #294 scope item 4 -- a GC
    single-item correction lever); otherwise `posture.system_prompt`
    verbatim off the genesis OPF. `posture.rubric` and `posture.generation`
    are never read here, so they cannot leak.

    Returns None -- no block at all -- when there is no posture prose. The real
    playbook ships `posture == {}`, so this used to return `""` on the day-one
    production path and `compose_opf_system_blocks` shipped it as blocks[0]: a
    slot that is hashed and cached like knowledge and says nothing.

    Composing an empty posture is NOT refused here. This module is pure
    composition and has no business making a governance call; the refusal
    (and its `accept_empty_posture` remedy, and the record that remedy writes)
    lives in `scripts/review_knowledge.py`, the one place refusals live. The
    omission is still RECORDED, so a caller that composes directly does not
    lose the signal.
    """
    posture_override = (overrides or {}).get("posture")
    if posture_override and posture_override.get("system_prompt"):
        return str(posture_override["system_prompt"])
    posture = opf_doc.get("posture") or {}
    system_prompt = posture.get("system_prompt")
    if not system_prompt:
        _omit(
            "posture.system_prompt — the review carries NO Posture block; the "
            "model receives no negotiation intent from the playbook at all",
            "posture",
        )
        return None
    return str(system_prompt)


#: The toaster's own default for a clause the playbook is silent or thin on
#: (issue #158). The playbook reports what it knows and how much -- each
#: clause heading's "precedent in N of M deals" and the closing
#: uncovered-types line -- and stops there; what to do when it knows little
#: is this direction, overridable per playbook by the administrator's
#: standing instructions (`primary_review_pass.STANDING_INSTRUCTIONS_INTRO`,
#: sent after the document). A CONSTANT with no request-specific value in
#: it, so it rides inside DIGEST_INTRO in the shared prefix both passes
#: cache byte-identically (#148, tests/test_shared_prefix_byte_identical.py).
#: The citation sentence is the prompt half of the output contract's rule
#: (docs/output-contract.md -> "Issues the playbook has no precedent for"):
#: no invented `internal_precedent_citation`, and an uncovered clause type
#: is cited by its taxonomy id. The "no precedent" remark goes to the
#: INTERNAL rationale only: the external footnote reaches the counterparty
#: and must not disclose what our playbook does or does not hold.
#: The frontend's standing-instructions hint
#: (`frontend/src/guidancePrecedenceCopy.ts`,
#: `UNCOVERED_CLAUSES_HINT_COPY`) cross-references this text (issue #484's
#: rule); change the two together.
SILENT_PLAYBOOK_DEFAULT = (
    "Where the playbook is silent or thin. Each clause heading below says in how many "
    "of the corpus's deals that clause has precedent, and a closing line, when there is "
    "one, lists the recognised clause types with no precedent at all. Where a clause in the document "
    "has no entry here, or its precedent comes from only one or two deals, the playbook "
    "is silent or thin: do not treat that silence as acceptance or as rejection. "
    "Compare the term with our standard language when the playbook has one, and with "
    "the nearest clause it does cover. Raise an issue only where the term is materially "
    "worse for us than that standard or than customary drafting for this agreement "
    "type, and say that no precedent supports the position in the internal rationale "
    "when one is asked for, never in the external one. An issue raised on that basis "
    "cites no precedent: its internal_precedent_citation is null, and when its clause "
    "type is on the closing no-precedent line, its playbook_topic_id is the id shown "
    "in parentheses beside that type. The administrator's standing instructions may give "
    "different direction for these clauses; where they do, they govern."
)

DIGEST_INTRO = (
    "PLAYBOOK PRECEDENT (descriptive, not prescriptive).\n"
    "This is precedent: what we have signed, and what we have refused, in past deals of "
    "this agreement type, clause by clause. It describes; it does not instruct, and no "
    "entry here is an instruction. Weigh it against the document in front of you and "
    "decide for this contract yourself.\n"
    "How to read each clause:\n"
    "- Our standard: our own template language for the clause, in its parts.\n"
    "- Signed language on record: wording other than our standard that we signed. A "
    "variant reached from our standard is a concession we have signed: it marks the edge "
    "of what we have accepted, not a preference. A variant signed as proposed is wording "
    "the other side opened with that we signed unchanged.\n"
    "- Asked for and refused: wording the other side proposed and we did not sign. It is "
    "NEVER our position and must NEVER be used as replacement language or as a model for "
    "an edit; its only use is precedent for refusing the same ask if it is made again. If "
    "the document already gives us something better, that is a good outcome, not a "
    "defect.\n"
    "- Opening language not signed as proposed: wording a first draft opened with that "
    "did not survive as written. It may have come from either side. Like a refused ask, "
    "it is never a source of replacement language.\n"
    "- A label (less protective of us, more protective of us, mixed, different concept, "
    "equivalent wording) compares an entry with OUR STANDARD, from our side. It was "
    "judged once, against our standard, never against this contract: it is an index, not "
    "an instruction. Check the cited text and decide for the live contract. An unlabelled "
    "entry was not judged. A part map gives one token per part of our standard, in order: "
    "= same legal effect; + more protective of us; - less protective of us; x+, x-, x0 "
    "omitted, with a more protective, less protective or no effect for us. Additions no "
    "part covers are marked +, - or 0 the same way.\n"
    "- A label marked \"adjudicated: weigh with care\" was contested: two independent "
    "readings disagreed and a third decided it. One marked \"unchecked: weigh with care\" "
    "has not been confirmed by an independent reading. Check the cited text with more care "
    "before you rely on either. An unmarked label was confirmed by an independent reading "
    "or set by the owner. How settled a label is says how far to trust the label, never "
    "how much the entry matters.\n"
    "- Dates are signing months (YYYY-MM): first signed and last signed are the earliest "
    "and latest month among the deals behind an entry. A date says when, never how much "
    "an entry weighs.\n"
    "- Our former forms: our own earlier template wording for the clause, each with when "
    "it was in use, the signed deals that opened on it (kept it, conceded from it, or moved "
    "to today's standard) and how it compares with today's standard. A form marked "
    "reconstructed was rebuilt from first drafts in the corpus, not supplied by the owner. "
    "A deal that opened on a former form opened on OUR wording, not the other side's. The "
    "playbook never says why a form changed; only an owner's note, when one is shown, "
    "does.\n"
    "- Signed without this clause: signed deals that carry no text for the clause. Only "
    "\"verified\" means we signed that deal without the clause. \"Possibly absent ... not "
    "verified\" means a passage may carry the clause unrecognised, so it is evidence of "
    "nothing either way; never add it to the verified count. Neither is the same as the "
    "playbook having no precedent for a clause.\n"
    "- Every count is of distinct deals. A citation [document@version §clause] names "
    "where the entry was signed or proposed; the document is an opaque "
    "reference, never the other party's name. A citation of prior_template:<form> "
    "cites one of our own former forms, not a deal.\n"
    "Each quoted entry is a summary of at most 300 characters ending on a sentence "
    "boundary, not necessarily the whole clause: do not present it as quoted in full "
    "from the corpus.\n"
    + SILENT_PLAYBOOK_DEFAULT
)

class PromptCompositionError(ValueError):
    """Raised when an OPF document cannot compose a knowledge prompt.

    Currently: no `digest` section. The 0.6 schema marks `digest` optional (not
    every producer populates it; the reference engine always does), but this is
    a KNOWLEDGE-FIRST review -- the digest IS the knowledge. Composing anyway
    would emit a prompt with no corpus precedent in it and review a contract on
    model judgement alone, silently, while every lineage field still recorded a
    playbook as having governed the review. Refusing is the only honest option:
    a missing digest is a configuration error, not an empty corpus.
    """


#: `vs_standard` label -> the words the model reads. Index, never instruction
#: (DIGEST_INTRO says so). The schema's label enum is pinned EXACTLY in
#: tests/test_opf_schema_sync.py: a sixth label would render as its raw token,
#: which is honest but unexplained by the intro.
_LABEL_WORDS = {
    "less_protective": "less protective of us",
    "more_protective": "more protective of us",
    "mixed": "mixed",
    "different_concept": "different concept",
    "equivalent": "equivalent wording",
}

#: `positions` key -> its words, in the order the positions line reads them:
#: our standard first, then the risky positions, then the rest.
_POSITION_WORDS = (
    ("standard", "our standard"),
    ("less_protective", "less protective of us"),
    ("mixed", "mixed"),
    ("different_concept", "different concept"),
    ("unjudged", "not judged"),
    ("more_protective", "more protective of us"),
    ("equivalent", "equivalent wording"),
)


#: `settled` values (digest 5, issue #248) that carry a marker beside the
#: label they qualify. `adjudicated`: the blind check disagreed and a third
#: reading decided, so the label was contested; `unchecked`: no independent
#: check confirmed it. Both tell the model to weigh the label with more care.
#: `agreed` and `owner` render plainly (no marker). A fact about how settled
#: the LABEL is, never a weight on the entry: DIGEST_INTRO says so. The
#: schema's `settled` enum is pinned EXACTLY in tests/test_opf_schema_sync.py.
_SETTLED_MARKERS = {
    "adjudicated": "adjudicated: weigh with care",
    "unchecked": "unchecked: weigh with care",
}
#: The `settled` values that render plainly.
_SETTLED_PLAIN = frozenset({"agreed", "owner"})


def _plural(n: Any, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _neutralise(text: str) -> str:
    """Owner- or producer-written free text new in OPF 0.6 (a former form's
    label, the owner's why-changed note), passed through the prompt's
    block-tag neutralisation (issue #162) before it is rendered, so it can
    neither open nor close a prompt block. Byte-identical for any text that
    carries no block-tag head, which is every engine-built playbook's.
    Imported lazily: `primary_review_pass` is the one home of the escape,
    and this module is otherwise import-light."""
    import primary_review_pass  # noqa: PLC0415

    return primary_review_pass.neutralise_prompt_delimiters(text)


def _month(value: Any) -> str | None:
    """A signing date at month precision (`YYYY-MM`), or None. The digest's
    own spans are months already; a deal's `signed_on` may be a full date
    (`verified_absent_deals[].signed_on`), and the prompt never renders a
    date finer than a month (see `_signed_span`)."""
    if isinstance(value, str) and len(value) >= 7 and value[4] == "-":
        return value[:7]
    return None


def _signed_span(entry: dict) -> str | None:
    """`first signed 2024-03, last signed 2025-11` from an entry's
    `first_signed` / `last_signed` (digest 5, issue #247), or None when
    neither is known.

    Dates are facts about WHEN, never weights, and they are rendered to the
    month only. Decision recorded for issue #177: a signing month is not
    held in leakage check 3. A month names no deal -- the alias does, and
    the alias-to-slug map never enters the prompt (`deal_aliases`) -- and a
    held month would block every external field that states a date, which
    the document under review is full of. A one-deal entry's span is that
    deal's signing month beside its aliased citation, at month precision;
    a `verified_absent_deals[]` entry's own `signed_on` (which may be a full
    date) is not rendered at all (`_absence_line` names the alias only). What keeps a past deal's date out
    of an external field is the same rule that keeps its precedent out: the
    external footnote must not disclose what our playbook holds
    (SILENT_PLAYBOOK_DEFAULT; docs/output-contract.md)."""
    first, last = _month(entry.get("first_signed")), _month(entry.get("last_signed"))
    if first and last:
        return f"first signed {first}, last signed {last}"
    if last:
        return f"last signed {last}"
    if first:
        return f"first signed {first}"
    return None


def _count(value: Any, field: str, where: str) -> int | None:
    """A required non-negative count, or None (recorded) when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        _omit(f"{field} — not an integer; the count it states is not rendered", where)
        return None
    return value


def _fmt_cite(ref: Any, where: str) -> str:
    """`[doc@v §path]` — grounds the entry in a real corpus location.

    Dropping the citation strands the entry: DIGEST_INTRO tells the model each
    entry names where it was signed or proposed, and an uncited entry does not.

    `doc` is never a deal's `document_id` (issue #173): `_digest_block` hands
    this the digest with each deal ref already swapped for its opaque alias
    (`deal_aliases.alias_digest`), which an operator or auditor resolves back
    to the corpus location with `deal_aliases.resolution_map`, kept out of
    every prompt. Our standard's template ref is not a deal and renders as is.

    The whole cite passes through the #162 block-tag neutralisation
    (`_neutralise`, issue #177): a former form's citation is
    `prior_template:<label>`, so the owner-written form label the line head
    neutralises reaches the prompt a second time here, and must not render
    raw. Byte-identical for any ref that carries no block-tag head, which is
    every engine-built playbook's.
    """
    if ref is None:
        _omit("citation — entry has no ref, so it reaches the model ungrounded, with no corpus location an operator or auditor can resolve it to", where)
        return ""
    if not isinstance(ref, dict):
        _omit(f"citation — ref is a {type(ref).__name__}, not an object; not rendered", where)
        return ""
    doc_id, ver, path = ref.get("document_id"), ref.get("version"), ref.get("clause_path")
    if not doc_id:
        _omit("citation — ref carries no document_id, so it cannot be resolved", where)
        return ""
    return _neutralise(f" [{doc_id}@{ver} §{path}]")


def _settled_marker(entry: dict, where: str) -> str:
    """`; adjudicated: weigh with care` for a label whose `settled` (digest
    5, issue #248) is `adjudicated` or `unchecked`, '' for `agreed` and
    `owner` (plain) and for an entry that carries no `settled` (the
    collapsed equivalent entry, whose counts `_settled_counts_note`
    renders)."""
    settled = entry.get("settled")
    if settled is None or settled in _SETTLED_PLAIN:
        return ""
    marker = _SETTLED_MARKERS.get(settled) if isinstance(settled, str) else None
    if marker is None:
        _omit("settled — not one of the four values; the label renders without it", where)
        return ""
    return f"; {marker}"


def _fmt_label(entry: dict, where: str) -> str:
    """`[less protective of us] ` — the entry's `vs_standard` label, or '' when
    the entry was not judged (`label` null is a fact, not a drop). A label
    whose judgment is `adjudicated` or `unchecked` carries its settled
    marker inside the brackets: `[mixed; unchecked: weigh with care] `."""
    label = entry.get("label")
    if label is None:
        return ""
    if not isinstance(label, str):
        _omit(f"vs_standard label — value is a {type(label).__name__}, not a string; not rendered", where)
        return ""
    return f"[{_LABEL_WORDS.get(label, label)}{_settled_marker(entry, where)}] "


def _settled_counts_note(entry: dict, where: str) -> str | None:
    """`1 adjudicated, 2 unchecked: weigh those with care` from the
    collapsed equivalent entry's `settled_counts` (digest 5, issue #248),
    or None when every collapsed text's label is `agreed` or `owner`."""
    counts = entry.get("settled_counts")
    if counts is None:
        return None
    if not isinstance(counts, dict):
        _omit("settled_counts — not an object; the collapsed entry renders without it", where)
        return None
    parts = []
    for key in ("adjudicated", "unchecked"):
        n = _count(counts.get(key), f"settled_counts.{key}", where)
        if n:
            parts.append(f"{n} {key}")
    if not parts:
        return None
    return f"of these labels {', '.join(parts)}: weigh those with care"


def _prior_form_note(entry: dict, prior_forms: dict[str, str] | None) -> str | None:
    """`opened on our former form "2018 form" in 1` -- how many of a signed
    variant's deals opened on one of OUR former forms (OPF 0.6
    `opened_with: prior_standard`, issue #249), named by the form's label.
    Read from `evidence.precedent[]` through the variant's `precedent_ids`
    (`prior_forms` maps a record id to its `opened_with_form`), because the
    digest variant counts only openings on today's standard
    (`n_from_standard`). Such a variant was reached from our own wording,
    not the other side's: its origin is ours."""
    if not prior_forms:
        return None
    ids = entry.get("precedent_ids")
    counts: dict[str, int] = {}
    for precedent_id in ids if isinstance(ids, list) else []:
        form = prior_forms.get(precedent_id) if isinstance(precedent_id, str) else None
        if form:
            counts[form] = counts.get(form, 0) + 1
    if not counts:
        return None
    return ", ".join(
        f'opened on our former form "{_neutralise(form)}" in {n}' for form, n in counts.items()
    )


def _fmt_quote(entry: dict, where: str) -> str:
    text = entry.get("text")
    if not isinstance(text, str) or not text:
        _omit("entry text — entry rendered with no wording", where)
        return '""'
    return f'"{text}"'


def _fmt_judgment(entry: dict, show_part_map: bool, where: str) -> list[str]:
    """The indented lines under a judged entry: its one-sentence reason (the
    owner's when `label_by` is `owner`) and, where it adds something beyond
    the label, its part map and addition tokens."""
    if entry.get("label") is None:
        return []
    lines: list[str] = []
    reason = entry.get("reason")
    if isinstance(reason, str) and reason:
        who = "Owner's label; owner's reason" if entry.get("label_by") == "owner" else "Judged difference"
        lines.append(f"      {who}: {reason}")
    elif reason is not None:
        _omit("vs_standard reason — not a string; the label renders without its reason", where)
    part_map, additions = entry.get("part_map"), entry.get("additions")
    if show_part_map or additions:
        rendered = []
        if isinstance(part_map, list) and part_map:
            rendered.append("part map " + " ".join(str(t) for t in part_map))
        if isinstance(additions, list) and additions:
            rendered.append("additions " + " ".join(str(t) for t in additions))
        if rendered:
            lines.append("      " + "; ".join(rendered))
    return lines


def _fmt_signed_variant(
    entry: Any, show_part_map: bool, where: str, prior_forms: dict[str, str] | None = None
) -> list[str]:
    """One signed-variant line (plus its judgment), or the collapsed
    equivalent-wording entry."""
    if not isinstance(entry, dict):
        _omit(f"signed variant — entry is a {type(entry).__name__}, not an object; not rendered", where)
        return []
    n_deals = _count(entry.get("n_deals"), "n_deals", where)
    facts = [f"signed in {_plural(n_deals, 'deal')}"] if n_deals is not None else []
    span = _signed_span(entry)
    if span:
        facts.append(span)
    for field, words in (("n_from_standard", "reached from our standard in"),
                         ("n_unchanged", "signed as proposed in")):
        n = _count(entry.get(field), field, where)
        if n:
            facts.append(f"{words} {n}")
    prior = _prior_form_note(entry, prior_forms)
    if prior:
        facts.append(prior)

    if entry.get("label") == "equivalent" and "exemplars" in entry:
        # The engine collapses every text labelled equivalent into ONE entry
        # (`n_texts` distinct texts, at most two exemplars): wording that
        # differs from ours without changing its effect is one fact, not a
        # list to read through.
        n_texts = _count(entry.get("n_texts"), "n_texts", where)
        if n_texts is not None:
            facts.insert(0, _plural(n_texts, "text"))
        settled_note = _settled_counts_note(entry, where)
        if settled_note:
            facts.append(settled_note)
        exemplars = entry.get("exemplars") if isinstance(entry.get("exemplars"), list) else []
        if not exemplars:
            _omit("equivalent-wording exemplars — collapsed entry rendered with no example text", where)
        examples = "; ".join(
            _fmt_quote(e, where) + _fmt_cite(e.get("ref"), where) for e in exemplars if isinstance(e, dict)
        )
        line = f"  - {_fmt_label(entry, where)}{', '.join(facts)}"
        return [line + (f"; for example {examples}" if examples else "")]

    line = f"  - {_fmt_label(entry, where)}{_fmt_quote(entry, where)} — {', '.join(facts)}"
    return [line + _fmt_cite(entry.get("ref"), where), *_fmt_judgment(entry, show_part_map, where)]


def _fmt_refused_ask(entry: Any, show_part_map: bool, where: str) -> list[str]:
    """A REFUSED ask (issue #677): the other side's wording we did not sign.

    Rendered as a record, never as an option: DIGEST_INTRO tells the model it
    is never replacement language (live runs showed a model mining exactly
    this list for replacement wording when it read as a menu).
    """
    if not isinstance(entry, dict):
        _omit(f"refused ask — entry is a {type(entry).__name__}, not an object; not rendered", where)
        return []
    n_deals = _count(entry.get("n_deals"), "n_deals", where)
    facts = [f"refused in {_plural(n_deals, 'deal')}"] if n_deals is not None else []
    span = _signed_span(entry)
    if span:
        facts.append(span)
    joined = f" — {', '.join(facts)}" if facts else ""
    line = f"  - {_fmt_label(entry, where)}{_fmt_quote(entry, where)}{joined}"
    return [line + _fmt_cite(entry.get("ref"), where), *_fmt_judgment(entry, show_part_map, where)]


def _fmt_changed_opening(entry: Any, show_part_map: bool, where: str) -> list[str]:
    """`"<text>" — <n> deal(s)[, ended at our standard in X][, struck in Y] [ref]`."""
    if not isinstance(entry, dict):
        _omit(f"changed opening — entry is a {type(entry).__name__}, not an object; not rendered", where)
        return []
    n_deals = _count(entry.get("n_deals"), "n_deals", where)
    facts = [_plural(n_deals, "deal")] if n_deals is not None else []
    for field, words in (("n_to_standard", "ended at our standard in"), ("n_struck", "struck in")):
        n = _count(entry.get(field), field, where)
        if n:
            facts.append(f"{words} {n}")
    line = f"  - {_fmt_label(entry, where)}{_fmt_quote(entry, where)} — {', '.join(facts)}"
    return [line + _fmt_cite(entry.get("ref"), where), *_fmt_judgment(entry, show_part_map, where)]


def _entry_list(clause: dict, field: str, where: str) -> list:
    entries = clause.get(field)
    if entries is None:
        _omit(f"{field} — absent; the section is not rendered", where)
        return []
    if not isinstance(entries, list):
        _omit(f"{field} — value is a {type(entries).__name__}, not a list; the whole section is skipped", where)
        return []
    return entries


def _texts_shown(variants: list) -> int:
    """Distinct signed texts the listed entries stand for: the collapsed
    equivalent-wording entry stands for `n_texts` of them."""
    shown = 0
    for entry in variants:
        if isinstance(entry, dict) and entry.get("label") == "equivalent" and "exemplars" in entry:
            n_texts = entry.get("n_texts")
            shown += n_texts if isinstance(n_texts, int) and not isinstance(n_texts, bool) else 1
        else:
            shown += 1
    return shown


def _positions_line(clause: dict, where: str) -> str | None:
    """`Positions of the 6 signed deals: 2 our standard, 4 less protective of us.`"""
    positions = clause.get("positions")
    if not isinstance(positions, dict):
        _omit("positions — absent or not an object; no positions line rendered", where)
        return None
    parts: list[str] = []
    total = 0
    for key, words in _POSITION_WORDS:
        n = positions.get(key)
        if isinstance(n, bool) or not isinstance(n, int):
            continue
        total += n
        if n:
            parts.append(f"{n} {words}")
    if not total:
        return None
    return f"Positions of the {_plural(total, 'signed deal')}: {', '.join(parts)}."


def _in_use_text(form: dict) -> str:
    """`in use 2018-01 to 2019-12`, `in use from 2021`, `in use until
    2020-05 (from deal dates)` or `in-use dates unknown` -- a former form's
    `in_use_from` / `in_use_until` at the precision given, and where they
    came from (`in_use_basis`: the owner's config, the deals that opened on
    the form, or both). Never a guessed date: a null end stays unstated."""
    start, end = form.get("in_use_from"), form.get("in_use_until")
    start = start if isinstance(start, str) and start else None
    end = end if isinstance(end, str) and end else None
    if start and end:
        text = f"in use {start} to {end}"
    elif start:
        text = f"in use from {start}"
    elif end:
        text = f"in use until {end}"
    else:
        return "in-use dates unknown"
    basis = {"deal_dates": "from deal dates", "mixed": "partly from deal dates"}.get(
        form.get("in_use_basis")
    )
    return f"{text} ({basis})" if basis else text


def _fmt_former_form(form: Any, show_part_map: bool, where: str) -> list[str]:
    """One former form of ours (digest 5 `standard_history.forms[]`, issue
    #249) on one line: its label, whether it is RECONSTRUCTED from the
    corpus rather than supplied by the owner (issue #250), its in-use
    dates, the signed deals that opened on it and what became of them, and
    how its clause compares with today's standard -- the label (with its
    settled marker) and, where it adds something, its part map. Facts only:
    no reason for the change is stated here; the owner's note, when there
    is one, is the clause's `why_changed` line (`_standard_history_lines`).
    """
    if not isinstance(form, dict):
        _omit(f"former form — entry is a {type(form).__name__}, not an object; not rendered", where)
        return []
    label = form.get("form")
    if not isinstance(label, str) or not label:
        _omit("former form label — absent; the form is not rendered", where)
        return []
    head = f'  - "{_neutralise(label)}"'
    if form.get("basis") == "reconstructed":
        head += " (reconstructed from the corpus, not supplied by the owner)"
    head += f", {_in_use_text(form)}"
    n_opened = _count(form.get("n_opened"), "n_opened", where)
    facts: list[str] = []
    if n_opened:
        facts.append(f"opened on it in {_plural(n_opened, 'signed deal')}")
        for field, words in (("n_kept", "kept it in"), ("n_conceded", "conceded in"),
                             ("n_moved_to_current", "moved to today's standard in")):
            n = _count(form.get(field), field, where)
            if n is not None:
                facts.append(f"{words} {n}")
    elif n_opened == 0:
        facts.append("no signed deal opened on it")
    if form.get("unchanged") is True:
        facts.append("reads as today's standard")
    elif form.get("label") is not None:
        compared = f"{_fmt_label(form, where).strip()} compared with today's standard"
        part_map, additions = form.get("part_map"), form.get("additions")
        tokens = []
        if (show_part_map or additions) and isinstance(part_map, list) and part_map:
            tokens.append("part map " + " ".join(str(t) for t in part_map))
        if isinstance(additions, list) and additions:
            tokens.append("additions " + " ".join(str(t) for t in additions))
        facts.append(compared + (f" ({'; '.join(tokens)})" if tokens else ""))
    else:
        facts.append("not compared with today's standard")
    line = f"{head}: {'; '.join(facts)}"
    return [line + _fmt_cite(form.get("ref"), where)]


def _standard_history_lines(clause: dict, show_part_map: bool, where: str) -> list[str]:
    """The clause's `standard_history` (digest 5, issue #249): one line per
    former form of ours, then the owner's `why_changed` note -- the ONLY
    reason a change is ever given, rendered only when the owner wrote one.
    Empty (no section) when the clause has neither."""
    history = clause.get("standard_history")
    if history is None:
        _omit("standard_history — absent; the clause's former forms are not rendered", where)
        return []
    if not isinstance(history, dict):
        _omit(
            f"standard_history — value is a {type(history).__name__}, not an object; not rendered",
            where,
        )
        return []
    forms = history.get("forms")
    if not isinstance(forms, list):
        _omit("standard_history.forms — not a list; the former forms are not rendered", where)
        forms = []
    lines: list[str] = []
    for index, form in enumerate(forms):
        lines.extend(_fmt_former_form(form, show_part_map, f"{where} / standard_history.forms[{index}]"))
    note = history.get("why_changed")
    note_text = note.get("text") if isinstance(note, dict) else None
    if note is not None and not (isinstance(note_text, str) and note_text):
        _omit("why_changed — present but carries no text; not rendered", where)
    if not lines and not (isinstance(note_text, str) and note_text):
        return []
    out = ["\nOur former forms of this clause:" if lines else "\nOur former forms of this clause: none on record."]
    out.extend(lines)
    if isinstance(note_text, str) and note_text:
        out.append(f"  Owner's note on why our standard changed: \"{_neutralise(note_text)}\"")
    return out


def _absence_line(clause: dict, where: str) -> str | None:
    """`Signed without this clause: absent in 4 signed deals (verified:
    deal-03, deal-07); possibly absent in 2 more; not verified.` (digest 5,
    issue #251), or None when the clause was signed in every signed deal.

    The two counts are never merged: only `n_signed_without_verified` is a
    deal we verifiably signed WITHOUT the clause type; a "possibly" deal may
    carry it unrecognised, so it is no evidence either way. Distinct, too,
    from "no precedent" (the heading's coverage, and the closing line of
    clause types the playbook has no precedent for at all, #158). The
    verified deals are named by their opaque aliases (`verified_absent_deals`
    is aliased with the rest of the digest, #173), capped like the other
    lists, with the uncapped remainder stated."""
    verified = _count(clause.get("n_signed_without_verified"), "n_signed_without_verified", where)
    possible = _count(clause.get("n_signed_without_possible"), "n_signed_without_possible", where)
    parts: list[str] = []
    if verified:
        deals = clause.get("verified_absent_deals")
        names = [
            entry["document_id"]
            for entry in (deals if isinstance(deals, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("document_id"), str) and entry["document_id"]
        ]
        if not isinstance(deals, list):
            _omit("verified_absent_deals — not a list; the verified deals are not named", where)
        listed = ", ".join(names)
        if names and verified > len(names):
            listed += f", and {verified - len(names)} more"
        parts.append(
            f"absent in {_plural(verified, 'signed deal')} (verified"
            + (f": {listed})" if listed else ")")
        )
    if possible:
        more = " more" if verified else ""
        noun = "" if verified else (" signed deal" if possible == 1 else " signed deals")
        parts.append(f"possibly absent in {possible}{more}{noun}; not verified")
    if not parts:
        return None
    return f"Signed without this clause: {'; '.join(parts)}."


def _digest_clause_block(
    clause: dict, corpus_n_deals: int | None = None, prior_forms: dict[str, str] | None = None
) -> str:
    """One digest-5 clause, rendered as plain facts under plain headers.

    Text, not JSON: a raw dump would hand the model field names
    (`refused_asks`, `changed_openings`) without the one thing that matters
    about them -- that neither is ever replacement language.

    `corpus_n_deals` is `digest.corpus.n_deals` (None when it is not a
    count, which `_corpus_line` already records). With it, the heading
    states the clause's coverage -- `precedent in <n_deals> of
    <corpus.n_deals> deals` (issue #158) -- so a clause seen in one deal of
    forty is legibly thin rather than read as settled; DIGEST_INTRO's
    silent-playbook paragraph says what to do then.

    Digest 5 (issue #177) adds the clause's signing months to the heading,
    a line of the deals signed WITHOUT the clause (verified versus possibly,
    never summed, `_absence_line`), our former forms of it
    (`_standard_history_lines`), how settled each judged label is, and the
    signing months of each entry. `prior_forms` maps an
    `evidence.precedent[]` id to its `opened_with_form` for a record that
    opened on one of our former forms (`_digest_block` builds it), so a
    signed variant reached from our former wording says so.
    """
    clause_id = clause.get("id")
    where = f"clause {clause_id!r}"
    lines: list[str] = []
    title = clause.get("title") or clause.get("taxonomy_id") or clause.get("id")
    if not clause.get("title"):
        _omit("clause title — headed by taxonomy_id/id instead", where)
    n_deals = _count(clause.get("n_deals"), "n_deals", where)
    coverage = (
        f" — precedent in {n_deals} of {_plural(corpus_n_deals, 'deal')}"
        if n_deals is not None and corpus_n_deals is not None
        else ""
    )
    span = _signed_span(clause)
    if span:
        coverage += f", {span}"
    lines.append(f"## {title}  [clause_id: {clause_id}]{coverage}")

    our_standard = clause.get("our_standard")
    parts = our_standard.get("parts") if isinstance(our_standard, dict) else None
    part_texts = [p.get("text") for p in parts or [] if isinstance(p, dict) and p.get("text")]
    if part_texts:
        cite = _fmt_cite(our_standard.get("source_ref"), f"{where} / our_standard")
        if len(part_texts) == 1:
            lines.append(f'Our standard: "{part_texts[0]}"{cite}')
        else:
            lines.append(f"Our standard, in {len(part_texts)} parts:{cite}")
            lines.extend(f'  {i}. "{text}"' for i, text in enumerate(part_texts, start=1))
    elif our_standard is not None:
        _omit(
            "our_standard — present but carries no readable part text; the clause's "
            "standard language never reaches the model",
            where,
        )
    else:
        lines.append("Our standard: none on record for this clause.")
    show_part_map = len(part_texts) > 1

    n_opened = _count(clause.get("n_opened_standard"), "n_opened_standard", where)
    n_kept = _count(clause.get("n_kept_standard"), "n_kept_standard", where)
    if n_opened:
        kept = f"; signed it unchanged in {n_kept}" if n_kept is not None else ""
        lines.append(f"Opened with our standard in {_plural(n_opened, 'signed deal')}{kept}.")

    positions = _positions_line(clause, where)
    if positions:
        lines.append(positions)

    absence = _absence_line(clause, where)
    if absence:
        lines.append(absence)

    lines.extend(_standard_history_lines(clause, show_part_map, where))

    variants = _entry_list(clause, "signed_variants", where)
    n_standard = _count(clause.get("n_signed_standard"), "n_signed_standard", where)
    if n_standard is not None and n_deals is not None:
        header = (
            f"\nSigned language on record ({n_standard} of {_plural(n_deals, 'deal')} "
            "signed our standard unchanged):"
        )
    else:
        header = "\nSigned language on record:"
    lines.append(header if variants else header + " no other wording signed.")
    for index, entry in enumerate(variants):
        lines.extend(
            _fmt_signed_variant(entry, show_part_map, f"{where} / signed_variants[{index}]", prior_forms)
        )

    refused = _entry_list(clause, "refused_asks", where)
    if refused:
        lines.append("\nAsked for and refused (never replacement language):")
        for index, entry in enumerate(refused):
            lines.extend(_fmt_refused_ask(entry, show_part_map, f"{where} / refused_asks[{index}]"))

    openings = _entry_list(clause, "changed_openings", where)
    if openings:
        lines.append("\nOpening language not signed as proposed (never replacement language):")
        for index, entry in enumerate(openings):
            lines.extend(_fmt_changed_opening(entry, show_part_map, f"{where} / changed_openings[{index}]"))

    # The digest caps each list; its totals are uncapped. Say what the cap
    # left out rather than let the model read a partial list as the whole one.
    # Each noun carries its own plural: the head noun of "opening not signed
    # as proposed" is not the phrase's last word.
    hidden = []
    for total_field, shown, singular, plural in (
        ("n_variants_total", _texts_shown(variants), "signed variant", "signed variants"),
        ("n_refused_total", len(refused), "refused ask", "refused asks"),
        ("n_changed_openings_total", len(openings),
         "opening not signed as proposed", "openings not signed as proposed"),
    ):
        total = _count(clause.get(total_field), total_field, where)
        if total is not None and total > shown:
            hidden.append(f"{total - shown} more {singular if total - shown == 1 else plural}")
    if hidden:
        lines.append(f"\n({', '.join(hidden)} on record, not shown here.)")
    return "\n".join(lines)


def _corpus_line(digest: dict) -> str | None:
    """`From 6 deals (6 signed), signed between 2022-03 and 2024-11.` (digest
    5's corpus span is to the month, issue #247)."""
    corpus = digest.get("corpus")
    if not isinstance(corpus, dict):
        _omit("digest corpus — absent; the block does not say how many deals it rests on", "digest")
        return None
    n_deals, n_signed = corpus.get("n_deals"), corpus.get("n_signed")
    if not isinstance(n_deals, int) or not isinstance(n_signed, int):
        _omit("digest corpus counts — not integers; the block does not say how many deals it rests on", "digest")
        return None
    line = f"From {_plural(n_deals, 'deal')} ({n_signed} signed)"
    first, last = corpus.get("first_signed"), corpus.get("last_signed")
    if first and last:
        line += f", signed between {first} and {last}"
    return line + "."


def _corpus_n_deals(digest: dict) -> int | None:
    """`digest.corpus.n_deals` when it is a count, else None (unrecorded:
    `_corpus_line` records the same defect once)."""
    corpus = digest.get("corpus")
    n_deals = corpus.get("n_deals") if isinstance(corpus, dict) else None
    if isinstance(n_deals, bool) or not isinstance(n_deals, int):
        return None
    return n_deals


#: Lead of the closing coverage line (issue #158). DIGEST_INTRO's
#: silent-playbook paragraph refers to it as "the closing no-precedent line".
UNCOVERED_LINE_LEAD = "Recognised clause types with no precedent in this playbook: "


def _uncovered_line(digest: dict) -> str | None:
    """`Recognised clause types with no precedent in this playbook: Audit
    Rights (audit_rights), Term (term).` -- or None when the list is empty
    (an empty list is omitted, never shown as an empty header).

    `uncovered_clause_types` is every recognised (active or custom) taxonomy
    entry with no evidence, uncapped and sorted by the engine. The id in
    parentheses is what an issue on such a clause cites as its
    `playbook_topic_id` (DIGEST_INTRO says so; docs/output-contract.md).
    """
    entries = digest.get("uncovered_clause_types")
    if entries is None:
        _omit("uncovered_clause_types — absent; the block does not say which clause types have no precedent", "digest")
        return None
    if not isinstance(entries, list):
        _omit(
            f"uncovered_clause_types — value is a {type(entries).__name__}, not a list; "
            "the no-precedent line is not rendered",
            "digest",
        )
        return None
    items: list[str] = []
    for index, entry in enumerate(entries):
        where = f"digest / uncovered_clause_types[{index}]"
        taxonomy_id = entry.get("taxonomy_id") if isinstance(entry, dict) else None
        if not isinstance(taxonomy_id, str) or not taxonomy_id:
            _omit("uncovered clause type — entry carries no taxonomy_id; not rendered", where)
            continue
        label = entry.get("label")
        if not isinstance(label, str) or not label:
            _omit("uncovered clause type label — absent; rendered by its taxonomy_id alone", where)
            items.append(taxonomy_id)
            continue
        items.append(f"{label} ({taxonomy_id})")
    if not items:
        return None
    return f"{UNCOVERED_LINE_LEAD}{', '.join(items)}."


def _prior_form_openings(opf_doc: dict) -> dict[str, str]:
    """`{precedent id: opened_with_form}` for every `evidence.precedent[]`
    record that opened on one of our former forms (OPF 0.6 `opened_with:
    prior_standard`, issue #249). Only the record's id and these two fields
    are read -- never a text, never a deal id -- so a signed variant can say
    how many of its deals opened on which of our former forms."""
    evidence = opf_doc.get("evidence")
    records = evidence.get("precedent") if isinstance(evidence, dict) else None
    out: dict[str, str] = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict) or record.get("opened_with") != "prior_standard":
            continue
        record_id, form = record.get("id"), record.get("opened_with_form")
        if isinstance(record_id, str) and isinstance(form, str) and form:
            out[record_id] = form
    return out


def _digest_block(opf_doc: dict) -> str:
    """The model-facing knowledge block, built from the OPF 0.6 `digest`
    (digest_version 5, the only one `scripts/opf_load.py` accepts).

    Replaces the retired ~1M-token wholesale `evidence` projection -- see the
    module docstring before considering a change here.
    """
    digest = opf_doc.get("digest")
    if not isinstance(digest, dict) or not digest.get("clauses"):
        raise PromptCompositionError(
            "cannot compose a review prompt: this OPF document has no digest.clauses. "
            "The digest is the model-facing knowledge; without it the review would run "
            "on model judgement alone while lineage still recorded a governing playbook."
        )
    # Issue #173: every deal `document_id` a citation names becomes its
    # opaque alias before anything renders it (`deal_aliases`), so no slug
    # reaches the model through `_fmt_cite`. A function of the document, so
    # the block stays the same bytes for every review of this version.
    digest = deal_aliases.alias_digest(_strip_x_keys(digest), deal_aliases.alias_map(opf_doc))
    corpus_n_deals = _corpus_n_deals(digest)
    prior_forms = _prior_form_openings(opf_doc)
    body = "\n\n".join(
        _digest_clause_block(c, corpus_n_deals, prior_forms)
        for c in digest["clauses"]
        if isinstance(c, dict)
    )
    corpus = _corpus_line(digest)
    head = f"{DIGEST_INTRO}\n{corpus}" if corpus else DIGEST_INTRO
    uncovered = _uncovered_line(digest)
    tail = f"\n\n{uncovered}" if uncovered else ""
    return f"{head}\n\n{body}{tail}"


def resolve_floor_invariants(opf_doc: dict, overrides: Optional[dict] = None) -> list[dict[str, Any]]:  # noqa: UP045
    """Union of `opf.floor.invariants` and `overrides.floor_additions`,
    genesis first, stable order (issue #294 scope item 4). No dedup logic
    needed: `scripts/bind_bundle.py::bind_bundle` already rejects any
    `floor_additions` id colliding with a genesis id at bind time, so the
    two lists are disjoint by the time this function ever sees a bound
    bundle's contents. Used both by `_floor_block` (prompt text) and by
    callers judging Floor invariants (`scripts/floor_judge.py`) so a
    floor_addition is judged alongside genesis invariants, never
    separately or with different scope.

    `overrides` absent/None (or carrying no `floor_additions`) returns
    `opf.floor.invariants` verbatim -- byte-identical to pre-#294 behavior.
    """
    floor = opf_doc.get("floor") or {}
    invariants = list(floor.get("invariants") or [])
    if overrides:
        invariants = invariants + list(overrides.get("floor_additions") or [])
    return invariants


def policy_rules_by_strength(policy: Optional[dict], strength: str) -> list[dict]:  # noqa: UP045
    """Renderable policy rules of `strength`, in document order.

    Guarded like every other renderer here (a policy reaches this module as a
    plain dict, and composition must not abort a review over one malformed
    rule), but a dropped `must` rule is a rule the model is not bound by while
    the policy hash still says it was -- so each drop is RECORDED.
    """
    if not policy:
        return []
    rules = policy.get("rules")
    if not isinstance(rules, list):
        _omit(
            f"policy.rules — value is a {type(rules).__name__}, not a list; NO policy rule "
            f"reaches the model",
            "policy",
        )
        return []
    out: list[dict] = []
    for index, rule in enumerate(rules):
        where = f"policy.rules[{index}]"
        if not isinstance(rule, dict):
            _omit(f"policy rule — entry is a {type(rule).__name__}, not an object", where)
            continue
        if rule.get("strength") != strength:
            continue
        if not rule.get("text"):
            _omit(
                f"policy rule text — `{strength}` rule {rule.get('id')!r} carries no text, so it "
                f"binds nothing while the policy hash still records it",
                where,
            )
            continue
        out.append(rule)
    return out


def _binding_block(
    opf_doc: dict, overrides: Optional[dict] = None, policy: Optional[dict] = None  # noqa: UP045
) -> str | None:
    """The rules that bind: Floor invariants, then policy `must` rules.

    Returns None -- NO BLOCK -- when nothing binds. It used to open
    unconditionally with `lines = [FLOOR_INTRO]`, so a doc with no invariants
    shipped "The following invariants are non-negotiable... You cannot waive
    them." over an empty list. The real playbook ships `floor == {}`: that was
    the day-one production prompt, not an edge case, and it left the model to
    either invent the invariants or learn that this prompt's promises are not
    load-bearing.

    Entries are TAGGED by provenance (`[floor:<id>]` / `[policy:<rule_id>]`)
    for two reasons. The model needs it because the two kinds bind differently
    (see BINDING_INTRO: a Floor invariant is unwaivable; a policy `must` in
    tension is FLAGGED for an attorney, never silently overridden in either
    direction -- shipping both under the old intro instructed the model to do
    the one thing playbooks/policy.schema.json forbids). The closing self-check
    and the G2 attribution manifest need it because they cite entries apart.

    Policy `text` is rendered VERBATIM. `tests/test_policy_document.py` enforces
    that attorney-override determinations live IN a rule's text -- search it for
    "the model reads `text`, not approval metadata" -- precisely because that is
    the only field the model ever sees. Rendering byte-exact preserves that here
    for free; paraphrasing or truncating would silently void it.
    """
    invariants = resolve_floor_invariants(opf_doc, overrides)
    must_rules = policy_rules_by_strength(policy, "must")
    if not invariants and not must_rules:
        # Issue #582 (Defect 2/3 follow-up): the real playbook ships
        # `floor: {}` BY DESIGN (issue #479 DECISION), and `policy` is
        # `None` for every review today (nothing in this deployment stores
        # a review-policy artifact reachable at review time -- see
        # `backend/src/pipeline_runner.py::run_real_pipeline`, which never
        # passes `policy=` to `review_spine.run_review`). So this branch is
        # not a hypothetical: it is what EVERY production OPF review
        # composes today. Before this, that fact was invisible -- this
        # function just returned `None` with nothing recorded, unlike
        # `_posture_block`'s equivalent "nothing here" case just above.
        # Recording it is what lets `review_knowledge.resolve_knowledge`'s
        # `lineage_record()` say, honestly, that a review whose
        # `posture_source` reads "playbook" carried no Binding content
        # either -- never inferred silently from an absent block.
        _omit(
            "binding — the review carries NO Binding block; neither a Floor "
            "invariant nor a policy `must` rule reaches the model",
            "binding",
        )
        return None

    lines = [BINDING_INTRO]
    index = 0
    # Issue #173: engine-written rationale names deals by slug in prose;
    # each slug is swapped for its alias by exact token replacement
    # (`deal_aliases.alias_text`), the rest of the text untouched. The map
    # is the genesis document's, so an `overrides.floor_additions`
    # rationale is aliased the same way. The statement is not aliased: see
    # `deal_aliases`' module docstring.
    aliases = deal_aliases.alias_map(opf_doc)
    for invariant in invariants:
        index += 1
        invariant_id = invariant.get("id", "")
        statement = invariant.get("statement", "")
        rationale = invariant.get("rationale")
        if not statement:
            # A Floor invariant IS its statement. An empty one is a numbered,
            # non-negotiable, unwaivable instruction to check nothing.
            _omit(
                "floor invariant statement — invariant rendered as a numbered "
                "line with nothing to enforce",
                f"floor invariant #{index} [{invariant_id}]",
            )
        line = f"{index}. [floor:{invariant_id}] {statement}"
        if rationale:
            line += f" (Rationale: {deal_aliases.alias_text(rationale, aliases)})"
        lines.append(line)
    for rule in must_rules:
        index += 1
        lines.append(f"{index}. [policy:{rule.get('id', '')}] {rule['text']}")
    return "\n".join(lines)


MANIFEST_INTRO = (
    "HARD-RULE MANIFEST (the playbook's hard rules, one per signed Floor invariant).\n"
    "Each rule is a signed Floor invariant, tagged [floor:<id>] as in the rules that bind "
    "this review, with how the playbook checks the clause it names:\n"
    "- Presence required: the clause's absence or deletion is itself a hard rejection. Code "
    "enforces it after the review: unless the clause is shown in this agreement and passes "
    "its test, code inserts the rule's fallback language itself.\n"
    "- Test: what the clause's own text must show. A judged rule has no machine test; it is "
    "decided on the clause's own text.\n"
    "- Fallback language: our standard text for the clause, the language code inserts for a "
    "presence-required rule that is not shown to be met. It is given verbatim on the indented "
    "lines under its label."
)

#: A `"judged"` condition in words. `hard_ledger.describe_condition` renders a
#: non-dict condition as a presence check, which is right for a ledger entry
#: (every entry demands presence) and wrong for a manifest rule that does not.
_JUDGED_TEST = "judged; no machine test"


def _manifest_rule_lines(
    index: int, position: int, rule: dict, titles: dict[str, str]
) -> list[str]:
    """One manifest rule, numbered `index`: tag and statement, then its clause
    and presence, its test, its fallback language. Never the
    `permissible_proof` list (see `_manifest_block`)."""
    where = f"manifest.hard_rules[{position}]"
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id:
        _omit("manifest rule id — rule rendered with an empty [floor:] tag", where)
        rule_id = ""
    statement = rule.get("statement")
    if not isinstance(statement, str) or not statement:
        _omit("manifest rule statement — rule rendered with nothing to enforce", where)
        statement = ""
    lines = [f"{index}. [floor:{rule_id}] {statement}".rstrip()]

    # The clause by its public title; failing that, by the id or taxonomy id
    # the rule names (public clause vocabulary either way -- the engine
    # nulls `clause_id` when the corpus has no evidence for the taxonomy id).
    clause_id = rule.get("clause_id")
    taxonomy_id = rule.get("taxonomy_id")
    title = titles.get(clause_id) if isinstance(clause_id, str) else None
    for fallback_name in (clause_id, taxonomy_id):
        if title is None and isinstance(fallback_name, str) and fallback_name:
            title = fallback_name
    clause = f"Clause: {title}." if title else "Clause: none named."
    presence = rule.get("required_presence")
    if presence is True:
        clause += " Presence required."
    elif presence is False:
        clause += " Presence not required."
    else:
        _omit("manifest required_presence — not a boolean; presence not stated", where)
    lines.append(f"   {clause}")

    condition = rule.get("condition")
    if condition == "judged":
        test = _JUDGED_TEST
    elif isinstance(condition, dict) and condition.get("type") in (
        "required_phrases",
        "numeric_bound",
        "cross_reference",
    ):
        reference = condition.get("clause_id")
        test = hard_ledger.describe_condition(
            condition, titles.get(reference) if isinstance(reference, str) else None
        )
    else:
        _omit("manifest condition — not a known predicate; test not stated", where)
        test = None
    if test is not None:
        lines.append(f"   Test: {test}.")

    fallback = rule.get("fallback_language")
    if isinstance(fallback, str) and fallback.strip():
        # The label on a line of its own, then the text, unquoted, one
        # indented line per line of it. Never `label: "text"`: the leakage
        # corpus derives check-1 grams from this block by line and sentence
        # (`leakage_scan.system_prompt_ngrams_from_blocks`), and exempts our
        # standard only where a piece is inside it or contains all of it. A
        # piece that mixes a fallback sentence with a quote mark or the label
        # is neither, so a multi-sentence fallback would turn its first and
        # last sentences into check-1 grams and fail closed every review
        # that quotes our standard faithfully. On their own lines, every
        # piece is a piece of the fallback (exempt), and the label is
        # shorter than any gram.
        lines.append("   Fallback language:")
        lines.extend(
            f"      {line.strip()}" for line in fallback.splitlines() if line.strip()
        )
    elif fallback is None:
        lines.append("   Fallback language: none.")
    else:
        _omit("manifest fallback_language — not a non-empty string; not rendered", where)
    return lines


def _manifest_block(opf_doc: dict) -> str | None:
    """The OPF hard-rule manifest (`manifest.hard_rules`,
    playbook-engine#228) as one block of the shared prefix (issue #163): per
    rule, its `[floor:<id>]` tag and statement, the clause it names (by public
    title), whether that clause's presence is required, its test (the
    predicate in words, via `hard_ledger.describe_condition` -- the critic's
    ledger tasking says the same words), and its fallback language.

    Returns None -- no block -- when the document carries no manifest, or a
    manifest with no rules. The real playbook ships `floor == {}`, so its
    manifest is empty and its prompt is unchanged by this block. Neither is
    recorded as an omission: the manifest is optional in the 0.6 schema and an
    empty one is derived from an empty Floor, which the Binding block already
    records.

    Not rendered, deliberately: a rule's `permissible_proof` (the Floor's
    `x_permissible_proof` -- the issue scopes the block to presence rules,
    predicates and fallback language) and a `numeric_bound`'s regex `pattern`
    (the bound and unit are rendered). `taxonomy_id` is not rendered beside
    the clause title: it is the same public clause name in another spelling,
    and stands in for the title only when the rule names no evidence clause.

    Request-independent like every block here: a deterministic function of
    the document, so both passes and every review of the playbook send the
    same bytes (#148), ahead of the system-side cache breakpoint.
    """
    manifest = opf_doc.get("manifest")
    if not isinstance(manifest, dict):
        return None
    manifest = _strip_x_keys(manifest)
    rules = manifest.get("hard_rules")
    if not isinstance(rules, list) or not rules:
        return None
    titles = hard_ledger.clause_titles(opf_doc)
    lines = [MANIFEST_INTRO]
    index = 0
    for position, rule in enumerate(rules):
        if not isinstance(rule, dict):
            _omit(
                f"manifest rule — entry is a {type(rule).__name__}, not an object",
                f"manifest.hard_rules[{position}]",
            )
            continue
        index += 1
        lines.extend(_manifest_rule_lines(index, position, rule, titles))
    if index == 0:
        return None
    return "\n".join(lines)


def _guidance_block(policy: Optional[dict] = None) -> str | None:  # noqa: UP045
    """Policy `should` rules, verbatim. None when the policy has none.

    Separate from the Binding block because `should` and `must` are different
    instructions, and a `should` rendered among binding rules is a `must` the
    model was never meant to be bound by.
    """
    should_rules = policy_rules_by_strength(policy, "should")
    if not should_rules:
        # Same "record the nothing-here case" fix as `_binding_block` above
        # -- `policy` is `None` for every review today (see that block's
        # comment), so this is the routine case, not an edge case, and was
        # previously invisible to `_report_omissions`/`lineage_record()`.
        _omit(
            "guidance — the review carries NO Guidance block; no policy "
            "`should` rule reaches the model",
            "guidance",
        )
        return None
    lines = [GUIDANCE_INTRO]
    lines.extend(f"  - [policy:{rule.get('id', '')}] {rule['text']}" for rule in should_rules)
    return "\n".join(lines)


#: The key the Context block renders our recognition set under, REPLACING
#: `perspective.party` (issue #678). See `resolve_party_recognition_set`.
OUR_ENTITIES_KEY = "our_entities"

#: The key the fixed recognition sentence rides under, rendered in the SAME
#: object as `our_entities` and only when that list is rendered (issue #55).
OUR_ENTITIES_NOTE_KEY = "our_entities_note"

#: Issue #55 (audit finding F6): the flat list is the canonical typed
#: spellings and NOTHING else -- rendering a variants list alongside it
#: would reintroduce exactly the "one real name plus its also-rans" shape
#: #678 flattened the block to eliminate. So the model is told the rule in
#: one fixed, factual sentence instead, and `entity_normalize` does the
#: actual folding everywhere the SYSTEM (rather than the model) has to
#: decide whether two spellings are one entity.
#:
#: Fixed and deterministic: it does not name an entity, does not vary with
#: the roster, and is byte-identical on every review, so the composed block
#: stays a pure function of the document plus the roster.
OUR_ENTITIES_NOTE = (
    "A legal-form suffix (GmbH, B.V., S.r.l., SAS, S.A., Ltd, Inc, LLC, "
    "plc, AG, S.p.A., Pty Ltd, K.K., Oy, AB, Corp, Co, LP, LLP), its "
    "punctuation, and either half of a d/b/a or trading-as name are the "
    "same entity as the spelling listed here."
)


def _recognition_key(name: str) -> str:
    """The identity a recognition-set entry is deduplicated on.

    Issue #55: delegates to `entity_normalize.recognition_key`, so the
    prompt-composition side folds legal-form suffixes, punctuation and
    diacritics exactly the way `backend/src/entity_roster.py
    ::normalize_entities` does. "Acme Holdings, LLC", "acme  holdings llc"
    and "ACME HOLDINGS L.L.C." are one entity typed three ways, not three
    entities to show the model.
    """
    return entity_normalize.recognition_key(name)


def resolve_party_recognition_set(
    opf_doc: dict, entity_roster: Optional[Sequence[str]] = None  # noqa: UP045
) -> list[str]:
    """Every legal-entity name that IS us, as ONE flat, deduplicated,
    source-free list (issue #678).

    The union of the playbook's `perspective.party` and the deployment's
    admin-managed entity roster (`backend/src/entity_roster.py`). The
    roster is organisation-scoped and the playbook is not: we are ~25 legal
    entities, any of which can be the contracting party, and on third-party
    paper the counterparty drafts with whichever name it was given -- a
    subsidiary, a former name, a d/b/a.

    THE SHAPE IS THE POINT. The playbook-engine owner's objection to
    splitting the recognition set across two sources is that it makes the
    "one primary + alternates" reading MORE tempting, not less -- the
    playbook supplies a single named party, deployment config supplies the
    rest, and that asymmetry is structural. So this returns one list in
    which nothing marks where an entry came from:

      - `perspective.party` is an ordinary member, not a head element.
      - The order is a case-insensitive sort, so position carries no rank
        and no source (and composition stays deterministic).
      - Entries are deduplicated case- and whitespace-insensitively, which
        is what makes the roster containing `perspective.party` -- the
        intended configuration -- a no-op rather than a duplicate.

    `perspective.party` is NOT canonical here and no caller may treat it as
    such (issue #678): under the flat-set model that field is populated
    because the schema requires it, and which of our entities happens to be
    in it is arbitrary.

    Fail-soft like every other renderer in this module: a malformed
    `perspective`, a non-string entry, or a blank name is skipped, never
    raised on. Returns `[]` when nothing usable is on either side, which is
    what makes the Context block fall back to rendering `perspective`
    unchanged rather than inventing an empty list.
    """
    candidates: list[str] = []
    perspective = opf_doc.get("perspective")
    if isinstance(perspective, dict):
        party = perspective.get("party")
        if isinstance(party, str):
            candidates.append(party)
    for entry in entity_roster or ():
        if isinstance(entry, str):
            candidates.append(entry)

    # Sorted BEFORE the dedup, on `(identity, spelling)`, so that when the
    # same entity is typed two ways ("Acme Holdings, LLC" in the playbook,
    # "ACME HOLDINGS, LLC" in the roster) the surviving spelling is a
    # function of the spellings themselves and NOT of which source happened
    # to be read first. Deduplicating in encounter order would have made the
    # playbook's spelling win every tie -- a small, quiet way for the source
    # to remain visible in the output.
    by_identity: dict[str, str] = {}
    for name in sorted(
        (n.strip() for n in candidates if n.strip()), key=lambda n: (_recognition_key(n), n)
    ):
        by_identity.setdefault(_recognition_key(name), name)
    return sorted(by_identity.values(), key=lambda n: (_recognition_key(n), n))


def _context_block(opf_doc: dict, entity_roster: Optional[Sequence[str]] = None) -> str | None:  # noqa: UP045
    """`perspective` and `de_minimis`, only if at least one is present in
    the source doc. Returns None (no block emitted) when neither is
    present.

    `perspective.party` is REPLACED by `perspective.our_entities` (issue
    #678): the flat recognition set `resolve_party_recognition_set` builds
    from that field and the deployment's entity roster together. The single
    named party never survives into the rendered block alongside the list,
    because `{"party": X, "aliases": [...]}` is precisely the shape that
    invites the model to read one entity as the real principal and the rest
    as also-rans. With no roster configured the list is that one name --
    same content as before, same shape as with twenty-five.

    Issue #55 adds ONE sibling key, `perspective.our_entities_note`: the
    fixed `OUR_ENTITIES_NOTE` sentence, rendered only where the list itself
    is rendered. A variants list was the alternative and was rejected -- it
    would put a canonical name beside its alternates, the exact shape above.
    """
    context: dict[str, Any] = {}
    if "perspective" in opf_doc:
        perspective = opf_doc["perspective"]
        our_entities = resolve_party_recognition_set(opf_doc, entity_roster)
        if isinstance(perspective, dict) and our_entities:
            rendered = {k: v for k, v in perspective.items() if k != "party"}
            rendered[OUR_ENTITIES_KEY] = our_entities
            rendered[OUR_ENTITIES_NOTE_KEY] = OUR_ENTITIES_NOTE
            context["perspective"] = rendered
        else:
            # Nothing usable to flatten (a malformed `perspective`, or a
            # blank party and no roster): render what the document says
            # rather than substituting an empty list for it.
            context["perspective"] = perspective
    if "de_minimis" in opf_doc:
        context["de_minimis"] = opf_doc["de_minimis"]
    if not context:
        return None
    return json.dumps(_strip_x_keys(context), sort_keys=True)


def compose_opf_system_blocks(
    opf_doc: dict,
    overrides: Optional[dict] = None,  # noqa: UP045
    *,
    policy: Optional[dict] = None,  # noqa: UP045
    mode: str = MODE_PLAYBOOK_DIGEST,
    entity_roster: Optional[Sequence[str]] = None,  # noqa: UP045
    omissions_out: Optional[dict[str, list]] = None,  # noqa: UP045
) -> list[str]:
    """Compose an OPF document's knowledge into review system-prompt blocks.

    Fixed order -- POSTURE, BINDING, DIGEST, MANIFEST, GUIDANCE, CONTEXT -- so
    the model reads what BINDS it before the precedent it weighs, and reads
    prescriptive intent before either. MANIFEST (issue #163) is the OPF
    hard-rule manifest, directly after the digest: like every block here it
    precedes the system-side cache breakpoint, so it caches with the digest. Every block is ABSENT or has content; none is ever an
    empty string, so a caller cannot index a slot that says nothing (the shape
    the real playbook's `posture == {}` and `floor == {}` used to produce).
    Because blocks are absent rather than empty, POSITION IS NOT IDENTITY: match
    on a block's intro, not on `blocks[n]`.

    `overrides` (issue #294, optional): a bound bundle's `overrides` block
    carrying a GC single-item correction -- `overrides.posture
    .system_prompt` redirects the Posture block source (genesis prose
    otherwise); `overrides.floor_additions` is unioned into the Binding
    block via `resolve_floor_invariants` (genesis first, stable order).

    `policy` (optional): an approved review policy document
    (playbooks/policy.schema.json), already loaded and validated by
    `scripts/policy_load.py`. Its `must` rules join the Binding block and its
    `should` rules become the Guidance block, `text` VERBATIM in both.

    `mode`: MODE_PLAYBOOK_DIGEST (default) composes the Digest block and
    refuses without one. MODE_POLICY_ONLY composes NO Digest block -- a review
    governed by policy alone. That mode must be POSITIVELY DECLARED by a caller
    and is never inferred from missing data; `scripts/review_knowledge.py` is
    what enforces the declaration, and calling this function with a bare
    MODE_POLICY_ONLY bypasses that check, which is why callers should go
    through `resolve_knowledge` rather than here.

    Both new parameters default to the pre-existing behavior: `policy=None,
    mode=MODE_PLAYBOOK_DIGEST` composes exactly what a caller that has never
    heard of a policy got before, and the no-digest `PromptCompositionError` is
    unchanged.

    No model call, no runtime wiring. Deterministic: the same inputs always
    produce byte-identical blocks (no timestamps, sorted JSON keys throughout).
    The only I/O is diagnostic — every value a fail-soft guard dropped is
    reported to stderr as an aggregated `WARNING:` (see the module docstring);
    the returned blocks are unaffected by it.

    `entity_roster` (issue #678, optional): the deployment's admin-managed
    roster of OUR legal entity names, resolved once per review by
    `backend/src/pipeline_runner.py` from `backend/src/entity_roster.py`
    and threaded here read-only. Unioned with the playbook's
    `perspective.party` into the ONE flat list the Context block renders --
    see `resolve_party_recognition_set` for why the union is flattened
    rather than rendered as a party plus its aliases. `None`/empty (every
    caller before this parameter existed, and every deployment with no
    roster configured) renders exactly the same single-name list the
    playbook alone supplies.

    `omissions_out` (optional, default `None`): when given, this function
    populates it (in place, via `dict.update`) with the SAME `{kind: [count,
    where]}` mapping the stderr warning is built from, so a caller that
    needs the omission record as DATA -- not merely as a diagnostic line --
    can persist it (`scripts/review_knowledge.py::resolve_knowledge` is the
    one caller that does, onto `ReviewKnowledge.lineage_record()`) without
    scraping stderr or re-deriving what was dropped. Never mutates the
    returned `blocks`, and a caller that omits this parameter (every caller
    before this one existed) sees no behavior change at all.
    """
    with _recording_omissions() as omissions:
        blocks: list[str] = []
        for block in (
            _posture_block(opf_doc, overrides),
            _binding_block(opf_doc, overrides, policy),
            None if mode == MODE_POLICY_ONLY else _digest_block(opf_doc),
            _manifest_block(opf_doc),
            _guidance_block(policy),
            _context_block(opf_doc, entity_roster),
        ):
            if block is not None:
                blocks.append(block)
    if omissions_out is not None:
        omissions_out.update(omissions)
    return blocks
