#!/usr/bin/env python3
"""Vendored-OPF-schema sync + shape-contract gate (OPF 0.3 launch).

The OPF schemas under ``playbooks/opf/`` are VENDORED copies of the
playbook-engine spec: physically committed here because this repo's CI has no
sibling checkout and must validate uploads hermetically. The cost of vendoring
is that a copy is a SNAPSHOT — the source can move and ours silently won't.
That happened three times during the 0.3 launch: 0.2 drifted by a lone
`description` string; 0.3 gained digest `n`/`band` (engine 3993b4f); 0.3 then
swapped `preferred_variations`' item type to `digestPreferredVariation` and
started enforcing a 40K-token budget (engine 1cc0237).

UPSTREAM HAS SINCE FIXED THE ROOT CAUSE (engine 458ac7a, 2026-07-16):
`spec/CHANGELOG.md` makes a published `opf_version` IMMUTABLE once a consumer
exists — shape *or semantic* changes go to a new version (0.3 → 0.4), never an
in-place edit, with the rule stated as "the schema still validates old data is
NOT sufficient: two artifacts claiming the same version must mean the same
thing". `digest_version` governs the digest independently under the same rule,
and their CI pins each schema's sha256 to that changelog. OPF 0.3 is FROZEN at
digest_version 2. The pins below are byte-identical to the engine's published
frozen pins, so we are vendored at the frozen shape.

That makes drift structurally unlikely rather than merely detectable — but this
gate stays, because "unlikely" is not "impossible" and a frozen upstream is a
promise, not a mechanism we control. It alarms in three layers, deliberately at
different urgencies:

  1. SHAPE CONTRACT (high urgency, runs in CI): the vendored schema must still
     declare every field THIS repo consumes, AT THE SHAPE we consume it. Fires
     only when the engine changes something that can break us — so cosmetic
     churn stays quiet. This is the check that distinguishes "the format broke
     us" from "the format was reworded". It is bidirectional: see the contract
     header for why a WIDENING is a break too.
  2. UPSTREAM PIN (medium, runs in CI): each vendored schema's sha256 is pinned
     below at the value the ENGINE PUBLISHED as that version's frozen sha
     (engine 458ac7a `spec/CHANGELOG.md`), not at whatever our copy happens to
     hash to. Be clear-eyed about what that buys and what it does not:

       IT DETECTS  — a hand-edit of our copy; a re-vendor that forgot the pin;
                     a re-vendor from the wrong file, the wrong version, or a
                     source that does not match the engine's published sha.
       IT CANNOT   — see upstream move AFTER we transcribed the number. The
                     pin lives in this repo beside the copy, and the re-vendor
                     procedure updates both. Nothing here re-fetches upstream,
                     so a pin+copy updated together are consistent with each
                     other whether or not they match the world. Only layer 3
                     (or upstream's own freeze) covers that.

     So this is a self-pin with an upstream-sourced VALUE. That is strictly
     more than a self-pin with a self-sourced value — the number has to come
     from the engine's changelog and match — but it is not a drift detector,
     and this gate does not claim to be one.
  3. SOURCE PARITY (low — a chore signal, LOCAL-ONLY): when the sibling repo is
     present (``../playbook-engine/spec/*.json`` or ``$PLAYBOOK_ENGINE_REPO``),
     the vendored copy must be BYTE-IDENTICAL to source. This is the ONLY layer
     that compares against something outside this repo, and therefore the only
     true upstream-drift detector — and it does not run in CI, because CI has
     no sibling checkout. It skips cleanly when the sibling is absent. Treat it
     as a backstop that fires on a dev's laptop, never as the primary alarm.
     That asymmetry is precisely why layer 1 carries the weight.

Re-vendor procedure: copy ``spec/playbook.schema-0.X.json`` byte-for-byte over
``playbooks/opf/playbook.schema-0.X.json``, then set the pin below FROM THE
ENGINE'S PUBLISHED FROZEN SHA (`spec/CHANGELOG.md`) — never from `sha256sum` of
the file you just copied, which would make the pin a tautology — and record the
engine commit in playbooks/opf/README.md.

Re-vendor + re-verify before binding a real playbook remains the standing rule.
Under the freeze a re-vendor should now be a no-op; if it is NOT, that is a
loud signal (either 0.3 moved despite the freeze, or the playbook was compiled
against a different version) and must be understood, never papered over by
re-pinning.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPF_DIR = REPO_ROOT / "playbooks" / "opf"

# The sha256 the ENGINE PUBLISHED as each version's FROZEN sha (engine 458ac7a,
# `spec/CHANGELOG.md`), which our vendored bytes must equal. Transcribe from the
# engine changelog on re-vendor; do NOT regenerate from our own copy (see the
# module docstring, layer 2 — a pin computed from the file it pins proves only
# that the file equals itself).
UPSTREAM_FROZEN_SHA256 = {
    "playbook.schema-0.2.json": "eae5f882f9289f2144cc784109d3dd04de7673d6e563d195fd693fd38ae1138d",
    "playbook.schema-0.3.json": "d2d81ca1c4f7547b508b2a22310906ce9a3bf43a2436e8730f0b1e4c9b0a0e15",
}


# ---------------------------------------------------------------------------
# Shape contract: every field this repo actually CONSUMES from an OPF document.
#
# Each entry is (dotted_path_into_schema, assertion, expected). This is a
# deliberate inventory of our dependency surface, not a mirror of the schema:
# if the engine drops, renames, retypes or WIDENS one of these, the failure
# message names what we lost and which code needs it. If the engine rewords a
# `description` or adds a field we don't read, nothing here fires.
#
# Assertions:
#   exists        — the node is present at all. LAST RESORT: it asserts nothing
#                   beyond presence, so it cannot see a retype. Use it only
#                   where the value's shape genuinely does not matter to us.
#   equals        — the node equals `expected` exactly
#   type_is       — the node is a schema whose `type` equals `expected`
#                   (a string, or a list like ["object", "null"])
#   enum_is       — the node is an enum list whose members are EXACTLY
#                   `expected` — no more, no fewer
#   superset_of   — the node (a list) contains every element of `expected`.
#                   Correct for `required`: the engine adding a required field
#                   cannot break a reader.
#   refs_exactly  — the node's $ref targets (following oneOf/anyOf/allOf
#                   branches) are EXACTLY the set `expected`
#   array_of      — the node is an array whose items' $ref targets are exactly
#                   `expected` AND which declares no maxItems cap
#
# WHY BOTH DIRECTIONS. The first cut of this contract asserted that
# digestClause.preferred_variations EXISTED and that $defs.acceptableIfEntry
# still existed -- and passed clean through engine 1cc0237, which swapped
# preferred_variations' item type from acceptableIfEntry to a new
# digestPreferredVariation (dropping `rationale`). Both assertions were true;
# the thing that broke us was the TYPE of the items, which nothing asserted.
#
# The second cut fixed that with a one-directional vocabulary — presence,
# superset, ref-membership — and so caught REMOVAL and NARROWING while staying
# structurally blind to ADDITION and WIDENING. A mutation matrix put numbers on
# it: 8 caught, 11 missed, and every miss a widening or a retype. That blindness
# is not a smaller version of the same bug, it is the same bug: this repo does
# not merely need the fields to be THERE, it renders them at an assumed shape,
# and everything downstream of that assumption fails SOFT.
#
# Concretely, the two lies a widening tells:
#   (a) opf_prompt.DIGEST_INTRO ships the model a HARDCODED band legend --
#       "often n>=10, sometimes n=2-9, rare n=1". An added band value, or a
#       re-banded n, does not break the render: it makes the legend LIE to the
#       model about how to weight precedent. Hence `enum_is`, not `superset_of`.
#   (b) opf_prompt's renderers are uniformly .get()/isinstance-guarded, so a
#       retype (n integer->string), a foreign type union'd into a list, or a
#       maxItems cap silently truncating a list produces a SHORTER prompt, not
#       an error. Hence `type_is`/`array_of`, not `exists`.
#
# So: every consumed list pins what its items ARE and that it is still a list;
# every consumed scalar pins its type; every consumed enum pins its exact
# membership; every consumed object we read unknown-key-free pins
# additionalProperties. `exists` survives only where shape truly is irrelevant.
#
# The bar for a row here is CONSUMPTION, not existence. A field this repo never
# reads does not belong in this table -- pinning it would make the gate fire on
# churn that cannot hurt us, and a gate that cries wolf gets muted. (Example:
# digestExemplarForm.deviation is surfaced by opf_clause_lookup.py off the full
# OPF, never rendered from the digest, so it is deliberately uncontracted.)
# ---------------------------------------------------------------------------

# Consumed by both 0.2 and 0.3.
_COMMON_CONTRACT: list[tuple[str, str, object]] = [
    # opf_load.resolve_opf_version dispatches on this (the const VALUE differs
    # per file, so only its presence and type are common to both).
    ("properties.opf_version", "type_is", "string"),
    ("properties.opf_version.const", "exists", None),
    # opf_canonicalize.verify_content_hash / bind_bundle lineage.
    ("properties.identity.properties.content_hash.pattern", "equals", "^sha256:[0-9a-f]{64}$"),
    ("properties.identity.properties.section_digests.required", "superset_of",
     ["evidence", "posture", "floor"]),
    # review_spine / opf_prompt read the evidence clause tree.
    ("properties.evidence.properties.clauses", "array_of", {"clausePosition"}),
    ("$defs.clausePosition.required", "superset_of",
     ["id", "taxonomy_id", "title", "observed_positions", "summary"]),
    ("$defs.clausePosition.properties.observed_positions", "array_of", {"observation"}),
    ("$defs.clausePosition.properties.our_standard", "type_is", ["object", "null"]),
    ("$defs.clausePosition.properties.summary.properties.historical_stance", "type_is", "string"),
    # `acceptable_if` items are oneOf[bare string (legacy), acceptableIfEntry];
    # the string branch carries no $ref, so the ref-target set is the entry type
    # alone. A THIRD branch appearing is exactly what these must fail on.
    ("$defs.clausePosition.properties.summary.properties.acceptable_if", "array_of",
     {"acceptableIfEntry"}),
    ("$defs.clausePosition.properties.summary.properties.fallbacks", "array_of", {"observation"}),
    ("$defs.clausePosition.properties.summary.properties.rejected", "array_of", {"observation"}),
    # opf_clause_lookup drills down to full_text + citations; the digest omits
    # full_text by design, so losing it upstream would break the lookup tool.
    # It is rendered as text: an object here would print as a Python repr.
    ("$defs.observation.properties.full_text", "type_is", "string"),
    ("$defs.observation.properties.precedent_count", "type_is", "integer"),
    ("$defs.observation.required", "superset_of",
     ["text_summary", "example_ref", "deviation", "risk_delta", "provenance", "outcome"]),
    # Terminology mapping + the prompt's preferred-variations block.
    ("$defs.acceptableIfEntry.required", "superset_of", ["if", "to", "rationale", "observation_ref"]),
    # opf_prompt._fmt_cite renders all three as `[doc@v §path]`.
    ("$defs.citation.required", "superset_of", ["document_id", "version", "clause_path"]),
    ("$defs.citation.properties.document_id", "type_is", "string"),
    ("$defs.citation.properties.clause_path", "type_is", "string"),
    # opf_prompt._fmt_risk renders `{risk: direction/magnitude}`. The values are
    # rendered VERBATIM, so their enums may grow without lying to anyone — only
    # the keys are contracted.
    ("$defs.riskDelta.required", "superset_of", ["direction", "magnitude"]),
    # Floor invariants drive the floor prompt block (and its suppression).
    ("properties.floor.properties.invariants", "type_is", "array"),
    ("properties.floor.properties.invariants.items.required", "superset_of", ["id", "statement"]),
    ("properties.posture", "type_is", "object"),
]

# Consumed only from 0.3.
_V03_CONTRACT: list[tuple[str, str, object]] = [
    # The spine refuses to run on a stub basis unless explicitly accepted.
    ("properties.compiler.properties.stub_basis_present.type", "equals", "boolean"),
    # The digest IS the model-facing prompt surface in 0.3.
    ("properties.digest.required", "superset_of", ["digest_version", "clause_count", "clauses"]),
    # opf_load.SUPPORTED_DIGEST_VERSIONS compares this against a set of STRINGS
    # and fail-closes on ingest, so a retype here is caught at runtime too --
    # but caught at re-vendor time is cheaper than caught on a live upload.
    ("properties.digest.properties.digest_version", "type_is", "string"),
    ("properties.digest.properties.clauses", "array_of", {"digestClause"}),
    ("$defs.digestClause.required", "superset_of",
     ["id", "taxonomy_id", "title", "historical_stance",
      "preferred_variations", "concessions", "unacceptable", "exemplar_forms"]),
    # `.get(...) or {}` / isinstance-guarded in opf_prompt._digest_clause_block:
    # a retype to a bare string does not raise, it just stops rendering.
    ("$defs.digestClause.properties.our_standard", "type_is", ["object", "null"]),
    ("$defs.digestClause.properties.stance_detail", "type_is", ["object", "null"]),
    ("$defs.digestClause.properties.historical_stance", "type_is", ["string", "null"]),
    # ITEM TYPES of every digest list the prompt renders, and that each is still
    # a LIST, uncapped. A type swap here is the #358 breakage; a union widened
    # to admit a foreign type is #358 from the other side; a maxItems cap is the
    # same class again (the prompt renders whatever survives, and says nothing
    # about what did not). `preferred_variations` items are oneOf[bare string
    # (legacy, handled by _fmt_preferred), digestPreferredVariation].
    ("$defs.digestClause.properties.preferred_variations", "array_of", {"digestPreferredVariation"}),
    ("$defs.digestClause.properties.concessions", "array_of", {"digestObservationSummary"}),
    ("$defs.digestClause.properties.unacceptable", "array_of", {"digestObservationSummary"}),
    ("$defs.digestClause.properties.exemplar_forms", "array_of", {"digestExemplarForm"}),
    # n-counts + frequency bands are what the prompt weights precedent by, and
    # DIGEST_INTRO hardcodes the band legend -- so `enum_is`, exactly these
    # three. A fourth band, or a dropped one, makes the legend lie.
    ("$defs.digestExemplarForm.required", "superset_of", ["text_summary", "n", "band"]),
    ("$defs.digestExemplarForm.properties.n", "type_is", "integer"),
    ("$defs.digestExemplarForm.properties.n.minimum", "equals", 1),
    ("$defs.digestExemplarForm.properties.band.enum", "enum_is", ["often", "sometimes", "rare"]),
    # _fmt_summary_entry renders risk_delta and example_ref on BOTH summary
    # types. Neither was pinned before, so upstream could have dropped the risk
    # annotations out of the model's prompt without a single check firing.
    ("$defs.digestExemplarForm.properties.risk_delta", "refs_exactly", {"riskDelta"}),
    ("$defs.digestExemplarForm.properties.example_ref", "refs_exactly", {"citation"}),
    ("$defs.digestExemplarForm.additionalProperties", "equals", False),
    ("$defs.digestObservationSummary.required", "superset_of", ["text_summary"]),
    ("$defs.digestObservationSummary.properties.text_summary", "type_is", "string"),
    ("$defs.digestObservationSummary.properties.n", "type_is", "integer"),
    ("$defs.digestObservationSummary.properties.n.minimum", "equals", 1),
    ("$defs.digestObservationSummary.properties.band.enum", "enum_is", ["often", "sometimes", "rare"]),
    ("$defs.digestObservationSummary.properties.risk_delta", "refs_exactly", {"riskDelta"}),
    ("$defs.digestObservationSummary.properties.example_ref", "refs_exactly", {"citation"}),
    ("$defs.digestObservationSummary.additionalProperties", "equals", False),
    # preferred_variations ship if/to verbatim + n/band; `rationale` deliberately
    # stays in the full OPF (reached via the clause-evidence lookup tool).
    ("$defs.digestPreferredVariation.required", "superset_of", ["if", "to", "n", "band"]),
    ("$defs.digestPreferredVariation.properties.if", "type_is", "string"),
    ("$defs.digestPreferredVariation.properties.to", "type_is", "string"),
    ("$defs.digestPreferredVariation.properties.observation_ref", "refs_exactly", {"citation"}),
    ("$defs.digestPreferredVariation.properties.n", "type_is", "integer"),
    # `minimum: 1` is SEMANTIC, not cosmetic: _fmt_n prints `(n=0, rare)` as
    # readily as `(n=6, often)`. Relaxing it to 0 lets "no precedent" render as
    # precedent, which is the one thing the digest exists to prevent.
    ("$defs.digestPreferredVariation.properties.n.minimum", "equals", 1),
    ("$defs.digestPreferredVariation.properties.band.enum", "enum_is", ["often", "sometimes", "rare"]),
    # additionalProperties:false is what makes "the four lists are the digest"
    # true. Opened up, the engine can ship a clause field the prompt silently
    # drops -- knowledge that reaches the artifact but never the model.
    ("$defs.digestPreferredVariation.additionalProperties", "equals", False),
    ("$defs.digestClause.additionalProperties", "equals", False),
]

SHAPE_CONTRACT: dict[str, list[tuple[str, str, object]]] = {
    "playbook.schema-0.2.json": _COMMON_CONTRACT,
    "playbook.schema-0.3.json": _COMMON_CONTRACT + _V03_CONTRACT,
}

_MISSING = object()


def _at(schema: object, dotted: str) -> object:
    """Resolve a dotted path into the schema, or return _MISSING."""
    node: object = schema
    for seg in dotted.split("."):
        if not isinstance(node, dict) or seg not in node:
            return _MISSING
        node = node[seg]
    return node


def _ref_targets(node: object) -> set[str]:
    """$defs names an item schema references, directly or via oneOf/anyOf branches."""
    targets: set[str] = set()
    if not isinstance(node, dict):
        return targets
    ref = node.get("$ref")
    if isinstance(ref, str):
        targets.add(ref.rsplit("/", 1)[-1])
    for key in ("oneOf", "anyOf", "allOf"):
        for branch in node.get(key) or []:
            targets |= _ref_targets(branch)
    return targets


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_spec_dir() -> Path | None:
    """Locate the playbook-engine spec/ dir if the sibling repo is present."""
    env = os.environ.get("PLAYBOOK_ENGINE_REPO")
    candidates = []
    if env:
        candidates.append(Path(env) / "spec")
    candidates.append(REPO_ROOT.parent / "playbook-engine" / "spec")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def check_shape_contract() -> list[str]:
    """The vendored schema must still declare every field this repo consumes."""
    failures: list[str] = []
    for name, contract in SHAPE_CONTRACT.items():
        path = OPF_DIR / name
        if not path.exists():
            failures.append(f"  MISSING vendored schema: {name}")
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        for dotted, assertion, expected in contract:
            node = _at(schema, dotted)
            if node is _MISSING:
                failures.append(
                    f"  {name}: contract path GONE: {dotted}\n"
                    f"    -> this repo reads it; the engine removed or renamed it."
                )
                continue
            if assertion == "exists":
                continue
            if assertion == "equals":
                if node != expected:
                    failures.append(f"  {name}: {dotted} == {node!r}, contract expects {expected!r}")
            elif assertion == "type_is":
                actual = node.get("type") if isinstance(node, dict) else None
                if actual != expected:
                    failures.append(
                        f"  {name}: {dotted} is type {actual!r}, contract expects {expected!r}\n"
                        f"    -> a RETYPE. This repo renders the value at the expected type; "
                        f"the renderers are .get()/isinstance-guarded, so the wrong type "
                        f"omits it silently rather than raising."
                    )
            elif assertion == "enum_is":
                if not isinstance(node, list):
                    failures.append(f"  {name}: {dotted} is {type(node).__name__}, expected an enum list")
                    continue
                added = sorted(set(node) - set(expected))
                removed = sorted(set(expected) - set(node))
                if added or removed:
                    detail = ", ".join(
                        part for part in (
                            f"ADDED {added}" if added else "",
                            f"REMOVED {removed}" if removed else "",
                        ) if part
                    )
                    failures.append(
                        f"  {name}: {dotted} enum changed: {detail} (now {sorted(node)!r})\n"
                        f"    -> this repo pins the EXACT membership: an added value is as "
                        f"breaking as a removed one, because the prompt hardcodes what "
                        f"these values mean."
                    )
            elif assertion == "superset_of":
                if not isinstance(node, list):
                    failures.append(f"  {name}: {dotted} is {type(node).__name__}, expected a list")
                    continue
                missing = [e for e in expected if e not in node]
                if missing:
                    failures.append(
                        f"  {name}: {dotted} no longer includes {missing} (has {node!r})"
                    )
            elif assertion == "refs_exactly":
                targets = _ref_targets(node)
                if targets != set(expected):
                    failures.append(
                        f"  {name}: {dotted} references {sorted(targets) or 'no $ref'}, "
                        f"contract expects exactly {sorted(expected)}\n"
                        f"    -> the TYPE changed (or the union widened); this repo reads "
                        f"{sorted(expected)} here."
                    )
            elif assertion == "array_of":
                if not isinstance(node, dict):
                    failures.append(f"  {name}: {dotted} is {type(node).__name__}, expected an array schema")
                    continue
                if node.get("type") != "array":
                    failures.append(
                        f"  {name}: {dotted} is type {node.get('type')!r}, contract expects 'array'\n"
                        f"    -> this repo iterates it as a list; anything else renders as "
                        f"nothing or as garbage."
                    )
                    continue
                targets = _ref_targets(node.get("items"))
                if targets != set(expected):
                    failures.append(
                        f"  {name}: items reference {sorted(targets) or 'no $ref'}, "
                        f"contract expects exactly {sorted(expected)}\n"
                        f"    -> the item TYPE changed, or the item union WIDENED to admit a "
                        f"type this repo does not render."
                    )
                if "maxItems" in node:
                    failures.append(
                        f"  {name}: {dotted} gained maxItems={node['maxItems']!r}\n"
                        f"    -> a cap TRUNCATES this list upstream. The prompt renders what "
                        f"survives and says nothing about what did not, so the model would "
                        f"read a partial corpus as the whole one."
                    )
            else:  # pragma: no cover - programming error in the table above
                failures.append(f"  {name}: unknown assertion {assertion!r} for {dotted}")
    return failures


def check_upstream_pin() -> list[str]:
    """Vendored bytes must equal the sha the engine PUBLISHED as frozen.

    Runs in CI (no sibling checkout needed). See the module docstring, layer 2,
    for what this does and does not prove — in short, it proves our copy is the
    one the engine froze at the moment we transcribed the number, and nothing
    about upstream since.
    """
    failures: list[str] = []
    for name, expected in UPSTREAM_FROZEN_SHA256.items():
        path = OPF_DIR / name
        if not path.exists():
            failures.append(f"  MISSING vendored schema: {path.relative_to(REPO_ROOT)}")
            continue
        actual = _sha256(path)
        if actual != expected:
            failures.append(
                f"  {name}: vendored bytes do NOT match the engine's published frozen sha\n"
                f"    upstream frozen: {expected}\n"
                f"    vendored:        {actual}\n"
                f"    -> either our copy was hand-edited, or it was re-vendored from "
                f"something other than the frozen {name}. Re-vendor byte-for-byte from "
                f"playbook-engine and transcribe the pin from the engine's "
                f"spec/CHANGELOG.md — do NOT sha256sum our copy into "
                f"UPSTREAM_FROZEN_SHA256, which would only re-state the drift as fact."
            )
    return failures


def check_source_parity() -> list[str]:
    """LOCAL-ONLY upstream-drift detector; skips when the sibling is absent.

    The only layer that compares our copy against something outside this repo.
    CI cannot run it (no sibling checkout), so it is a chore signal on a dev's
    laptop, not a gate. See the module docstring, layer 3.
    """
    spec_dir = _source_spec_dir()
    if spec_dir is None:
        print(
            "  (playbook-engine sibling not present — skipped. This is the only "
            "check that can see UPSTREAM move; CI never runs it. Set "
            "PLAYBOOK_ENGINE_REPO to exercise it.)"
        )
        return []
    failures: list[str] = []
    for name in UPSTREAM_FROZEN_SHA256:
        vendored = OPF_DIR / name
        source = spec_dir / name
        if not source.exists():
            failures.append(f"  source missing: {source}")
            continue
        if vendored.read_bytes() != source.read_bytes():
            failures.append(
                f"  {name}: vendored copy DIFFERS from source {source} "
                f"(re-vendor byte-for-byte + update the pin)."
            )
    return failures


def main() -> int:
    checks = [
        ("1", "shape contract: every field this repo consumes is still declared, at the shape we consume it", check_shape_contract),
        ("2", "vendored schema sha256 matches the engine's PUBLISHED frozen sha (runs in CI)", check_upstream_pin),
        ("3", "vendored schema byte-identical to playbook-engine source (chore signal, local-only)", check_source_parity),
    ]
    ok = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            ok = False
    print()
    if ok:
        print("All OPF schema-sync checks passed.")
        return 0
    print("One or more OPF schema-sync checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
