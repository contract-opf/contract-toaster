#!/usr/bin/env python3
"""
OPF prompt composition -- issue #284, rebuilt on the DIGEST for OPF 0.3.

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

OPF 0.3 replaces it with the engine-built `digest` section, which is the same
knowledge projected for exactly this use: per clause, the stance plus four
deduplicated, frequency-annotated lists, each entry carrying a citation that
resolves back into the full OPF. The engine enforces a ~40K-token budget on it
BY CONSTRUCTION (tightening per-list caps until it fits), so the block this
module emits is bounded by the artifact, not by hope. The real playbook measures
~38K tokens.

What the digest deliberately omits -- every observation's `full_text`, and (as
of digest_version 2) each preferred variation's compiler-written `rationale` --
is NOT lost: `scripts/opf_clause_lookup.py` is the model's tool for fetching it
on demand. Summaries by default, detail where the decision needs it. That
trade is the entire reason the prompt fits.

So: if a future change is tempted to inline `full_text`, or to "just include the
evidence section too", it is re-proposing the 1M-token design. Add a lookup, not
a dump.

## Terminology

Headers come from `scripts/opf_terminology.py`, verbatim from the engine's
renderer, so the words in the model's prompt match the words in the playbook a
reviewer reads. OPF field names are untouched -- the mapping is display-only.

Mirrors the block/cache-breakpoint CONVENTION of
`scripts/primary_review_pass.py:assemble_system_blocks` (fixed block order,
deterministic projection) without reusing its Anthropic-message-API-shaped
return type: this function returns `list[str]`.

## What is excluded, and why nothing has to special-case it

`compose_opf_system_blocks` only ever reads five paths off the input doc:
`posture.system_prompt`, `digest.clauses`, `floor.invariants`,
`perspective`, and `de_minimis` (plus, off the separate policy document,
`rules[].{id,strength,text}`). Note `evidence` is NOT among them any more:
the digest is the projection of it that reaches the model, and the full
evidence section is read only on demand, off disk, by the lookup tool.
Every other top-level section --
`posture.rubric` (excised from the schema entirely per engine #178, but this
function does not validate its input, so a caller-constructed dict carrying
it is simply never looked at), `posture.generation` (interview transcript),
`corpus`, `compiler`, `identity`, `curation`, `baseline`, `composes` -- is
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
convention `scripts/diff_standard_form.py` already uses for "discarding this,
but visibly, so a drift signal is not silent data loss". Aggregated, not
per-entry: a real playbook has thousands of entries, and a warning per entry is
a warning nobody reads.

The guards' BEHAVIOR is unchanged, and so is this module's purity in the sense
that matters: the returned blocks are byte-identical to what they were, still a
deterministic function of the input. Only stderr is new.

## `historical_stance` stays descriptive (OPF §2.2)

The Digest block renders `historical_stance` as a labelled fact ("Historical
stance: usually_held (held 8 of 10 ...)") and never rephrases it as an
instruction. The distinction is load-bearing: "usually_held" describes what the
corpus did, and the moment a prompt renders it as "hold this clause" the
playbook has stopped being evidence and started being policy -- which is the
policy document's job, not the digest's. DIGEST_INTRO says so to the model in
as many words.

Unlike the retired wholesale projection, this block renders a KNOWN shape (the
digest's four lists), so a genuinely new digest field would not appear in the
prompt until this module renders it. That is the deliberate trade for headers a
model can read: `digest_version` is dispatched on at ingest
(`scripts/opf_load.py`), so a shape this module does not understand is refused
at the door rather than silently half-rendered here.

De-brand: no tenant-brand strings anywhere in this module (project de-brand rule).
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_clause_lookup  # noqa: E402
import opf_terminology  # noqa: E402

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
def _recording_omissions() -> Iterator[None]:
    """Collect omissions, then report them, one aggregated line per kind."""
    global _omissions
    outer = _omissions
    _omissions = {}
    try:
        yield
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


def _posture_block(opf_doc: dict, overrides: Optional[dict] = None) -> str | None:
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


DIGEST_INTRO = (
    "PLAYBOOK KNOWLEDGE (descriptive, not prescriptive).\n"
    "What the signed corpus has actually shown for each clause. This is precedent, "
    "not instruction: it tells you what we have done, never what you must do. "
    "Weigh it by n -- the number of corpus observations behind an entry "
    "(bands: often n>=10, sometimes n=2-9, rare n=1). A position held across many "
    "deals is strong evidence; a single instance is weak.\n"
    "These are SUMMARIES. Each entry cites its source; use the "
    f"{opf_clause_lookup.TOOL_NAME} tool to read the full clause text or a "
    "variation's full rationale before relying on it for exact language."
)

class PromptCompositionError(ValueError):
    """Raised when an OPF document cannot compose a knowledge prompt.

    Currently: no `digest` section. The 0.3 schema marks `digest` optional (not
    every producer populates it; the reference engine always does), but this is
    a KNOWLEDGE-FIRST review -- the digest IS the knowledge. Composing anyway
    would emit a prompt with no corpus precedent in it and review a contract on
    model judgement alone, silently, while every lineage field still recorded a
    playbook as having governed the review. Refusing is the only honest option:
    a missing digest is a configuration error, not an empty corpus.
    """


def _fmt_n(entry: dict, where: str) -> str:
    """`(n=6, often)` — the precedent weight, or '' when the entry carries none.

    Dropping n is not cosmetic: DIGEST_INTRO tells the model to WEIGH every
    entry by n, so an entry rendered without one is precedent of unstated
    strength presented alongside precedent of stated strength.
    """
    n = entry.get("n")
    if n is None:
        _omit("precedent weight (n) — entry rendered with no n for the model to weigh by", where)
        return ""
    band = entry.get("band")
    if not band:
        _omit("frequency band — n rendered without its often/sometimes/rare label", where)
        return f" (n={n})"
    return f" (n={n}, {band})"


def _fmt_cite(ref: Any, where: str) -> str:
    """`[doc@v §path]` — compact, and enough for the lookup tool to resolve.

    Dropping the citation strands the entry: DIGEST_INTRO promises the model it
    can look every summary up, and an uncited entry is one it cannot.
    """
    if ref is None:
        _omit("citation — entry has no ref, so the model cannot look this summary up", where)
        return ""
    if not isinstance(ref, dict):
        _omit(f"citation — ref is a {type(ref).__name__}, not an object; not rendered", where)
        return ""
    doc_id, ver, path = ref.get("document_id"), ref.get("version"), ref.get("clause_path")
    if not doc_id:
        _omit("citation — ref carries no document_id, so it cannot be resolved", where)
        return ""
    return f" [{doc_id}@{ver} §{path}]"


def _fmt_risk(risk: Any, where: str) -> str:
    """`{risk: worse/material}` — direction is from OUR perspective."""
    if risk is None:
        return ""  # optional by schema on the digest summaries; absence is not a drop.
    if not isinstance(risk, dict):
        _omit(f"risk_delta — value is a {type(risk).__name__}, not an object; not rendered", where)
        return ""
    direction, magnitude = risk.get("direction"), risk.get("magnitude")
    if not direction:
        _omit("risk_delta — carries no direction, so the whole risk annotation is dropped", where)
        return ""
    if not magnitude:
        _omit("risk magnitude — direction rendered without none/minor/material", where)
        return f" {{risk: {direction}}}"
    return f" {{risk: {direction}/{magnitude}}}"


def _fmt_preferred(entry: Any, where: str) -> str:
    if isinstance(entry, str):  # legacy bare-string entry
        _omit(
            "preferred variation structure — legacy bare-string entry, so no "
            "if/to split, no n/band weight and no citation",
            where,
        )
        return f"  - {entry}"
    # digest_version 2 projects these to {if, to, observation_ref, n, band};
    # `rationale` stays in the full OPF, reachable via the lookup tool.
    line = f"  - FROM: {entry.get('if')}\n    ACCEPTABLE AS: {entry.get('to')}"
    return line + _fmt_n(entry, where) + _fmt_cite(entry.get("observation_ref"), where)


def _fmt_summary_entry(entry: dict, where: str) -> str:
    if not entry.get("text_summary"):
        _omit("text_summary — entry rendered as an empty bullet", where)
    return (
        f"  - {entry.get('text_summary', '')}"
        + _fmt_n(entry, where)
        + _fmt_risk(entry.get("risk_delta"), where)
        + _fmt_cite(entry.get("example_ref"), where)
    )


_LIST_FORMATTERS = {
    "preferred_variations": _fmt_preferred,
    "concessions": _fmt_summary_entry,
    "unacceptable": _fmt_summary_entry,
    "exemplar_forms": _fmt_summary_entry,
}


def _digest_clause_block(clause: dict) -> str:
    """One clause, rendered under the canonical headers.

    Text, not JSON: these headers exist to stop `fallbacks`/`rejected` being
    read as the judgements they are not, and a raw JSON dump would hand the
    model exactly those misleading field names.
    """
    clause_id = clause.get("id")
    where = f"clause {clause_id!r}"
    lines: list[str] = []
    title = clause.get("title") or clause.get("taxonomy_id") or clause.get("id")
    if not clause.get("title"):
        _omit("clause title — headed by taxonomy_id/id instead", where)
    lines.append(f"## {title}  [clause_id: {clause_id}]")

    stance = clause.get("historical_stance")
    detail = clause.get("stance_detail") or {}
    if not stance:
        _omit("historical_stance — clause rendered with no stance line at all", where)
    else:
        if detail.get("held") is not None and detail.get("of") is not None:
            held = f" (held {detail['held']} of {detail['of']} on {detail.get('basis', 'all')} paper)"
        else:
            # The held-rate is the EVIDENCE behind the stance label; without it
            # "usually_held" reaches the model as an assertion with no count.
            _omit("stance_detail held-rate — stance label rendered with no held X of Y behind it", where)
            held = ""
        lines.append(f"Historical stance: {stance}{held}")

    our_standard = clause.get("our_standard")
    if isinstance(our_standard, dict) and our_standard.get("text"):
        lines.append(f"Our standard language: {our_standard['text']}")
    elif our_standard is not None:
        _omit(
            "our_standard — present but carries no readable `text`; the clause's "
            "standard language never reaches the model",
            where,
        )

    for term in opf_terminology.TERMS:
        entries = clause.get(term.digest_field) or []
        if not isinstance(entries, list):
            _omit(
                f"{term.digest_field} — value is a {type(entries).__name__}, not a list; "
                f"the whole section is skipped",
                where,
            )
            continue
        if not entries:
            # Say nothing rather than print an empty header: an empty section
            # reads as "we looked and found none", which is a claim the digest
            # does not make.
            continue
        lines.append(f"\n{term.header} ({len(entries)})")
        lines.append(f"  {term.help}")
        fmt = _LIST_FORMATTERS[term.digest_field]
        lines.extend(
            fmt(e, f"{where} / {term.digest_field}[{index}]")
            for index, e in enumerate(entries)
        )
    return "\n".join(lines)


def _digest_block(opf_doc: dict) -> str:
    """The model-facing knowledge block, built from the OPF 0.3 `digest`.

    Replaces the retired ~1M-token wholesale `evidence` projection -- see the
    module docstring before considering a change here.
    """
    digest = opf_doc.get("digest")
    if not isinstance(digest, dict) or not digest.get("clauses"):
        raise PromptCompositionError(
            "cannot compose a review prompt: this OPF document has no digest.clauses. "
            "The digest is the model-facing knowledge; without it the review would run "
            "on model judgement alone while lineage still recorded a governing playbook. "
            "An OPF 0.2 document has no digest section at all and cannot drive this path."
        )
    clauses = _strip_x_keys(digest["clauses"])
    body = "\n\n".join(_digest_clause_block(c) for c in clauses)
    return f"{DIGEST_INTRO}\n\n{body}"


def resolve_floor_invariants(opf_doc: dict, overrides: Optional[dict] = None) -> list[dict[str, Any]]:
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


def policy_rules_by_strength(policy: Optional[dict], strength: str) -> list[dict]:
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
    opf_doc: dict, overrides: Optional[dict] = None, policy: Optional[dict] = None
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
        return None

    lines = [BINDING_INTRO]
    index = 0
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
            line += f" (Rationale: {rationale})"
        lines.append(line)
    for rule in must_rules:
        index += 1
        lines.append(f"{index}. [policy:{rule.get('id', '')}] {rule['text']}")
    return "\n".join(lines)


def _guidance_block(policy: Optional[dict] = None) -> str | None:
    """Policy `should` rules, verbatim. None when the policy has none.

    Separate from the Binding block because `should` and `must` are different
    instructions, and a `should` rendered among binding rules is a `must` the
    model was never meant to be bound by.
    """
    should_rules = policy_rules_by_strength(policy, "should")
    if not should_rules:
        return None
    lines = [GUIDANCE_INTRO]
    lines.extend(f"  - [policy:{rule.get('id', '')}] {rule['text']}" for rule in should_rules)
    return "\n".join(lines)


def _context_block(opf_doc: dict) -> str | None:
    """`perspective` and `de_minimis`, only if at least one is present in
    the source doc. Returns None (no block emitted) when neither is
    present."""
    context: dict[str, Any] = {}
    if "perspective" in opf_doc:
        context["perspective"] = opf_doc["perspective"]
    if "de_minimis" in opf_doc:
        context["de_minimis"] = opf_doc["de_minimis"]
    if not context:
        return None
    return json.dumps(_strip_x_keys(context), sort_keys=True)


def compose_opf_system_blocks(
    opf_doc: dict,
    overrides: Optional[dict] = None,
    *,
    policy: Optional[dict] = None,
    mode: str = MODE_PLAYBOOK_DIGEST,
) -> list[str]:
    """Compose an OPF document's knowledge into review system-prompt blocks.

    Fixed order -- POSTURE, BINDING, DIGEST, GUIDANCE, CONTEXT -- so the model
    reads what BINDS it before the precedent it weighs, and reads prescriptive
    intent before either. Every block is ABSENT or has content; none is ever an
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
    """
    with _recording_omissions():
        blocks: list[str] = []
        for block in (
            _posture_block(opf_doc, overrides),
            _binding_block(opf_doc, overrides, policy),
            None if mode == MODE_POLICY_ONLY else _digest_block(opf_doc),
            _guidance_block(policy),
            _context_block(opf_doc),
        ):
            if block is not None:
                blocks.append(block)
    return blocks
