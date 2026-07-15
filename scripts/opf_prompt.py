#!/usr/bin/env python3
"""
OPF prompt composition -- issue #284 (slice 2 of 5 of the #278 OPF-bind
chain).

Pure composition slice: turn a schema-shaped OPF v0.2 document (see #283's
`scripts/opf_load.py` for the loader/validator this slice consumes) into
review system-prompt blocks. **Evidence + Posture + Floor in, prompt blocks
out.** No I/O, no model call, no runtime wiring -- wiring into
`run_primary_pass`/`run_critic_pass` happens later, when the spine consumes
v2 bundles (out of scope here).

Mirrors the block/cache-breakpoint CONVENTION of
`scripts/primary_review_pass.py:assemble_system_blocks` (fixed block order,
`json.dumps(..., sort_keys=True)` for a deterministic compact projection)
without reusing its Anthropic-message-API-shaped return type: this function
returns `list[str]`, per the issue.

## What is excluded, and why nothing has to special-case it

`compose_opf_system_blocks` only ever reads five paths off the input doc:
`posture.system_prompt`, `evidence` (wholesale), `floor.invariants`,
`perspective`, and `de_minimis`. Every other top-level section --
`posture.rubric` (excised from the schema entirely per engine #178, but this
function does not validate its input, so a caller-constructed dict carrying
it is simply never looked at), `posture.generation` (interview transcript),
`corpus`, `compiler`, `identity`, `curation`, `baseline`, `composes` -- is
excluded automatically because the function never touches it, not via a
per-field denylist. That is also why a doc WITH `posture.rubric` produces
byte-identical output to the same doc without it (issue #284 AC).

## `x_*` vendor extensions (engine #180)

Unknown-provenance vendor extension keys (schema `patternProperties: {"^x_":
true}`, e.g. nested inside an `evidence.clauses[].observed_positions[]`
entry) are stripped recursively wherever a block source IS included
wholesale (`evidence`, the optional Context block) via `_strip_x_keys`.

## `historical_stance` stays descriptive (OPF §2.2)

The Evidence block is a straight `json.dumps` projection of the `evidence`
section -- it never rephrases `historical_stance` (or any other evidence
field) imperatively. It is passed through as data, same as every other
evidence field, which is also what makes forward-compat "free": an evidence
field this module's author never anticipated (negotiation dynamics --
`proposed_by`, `counterparty_ref`, `negotiation_trail` -- per engine #177)
survives into the Evidence block unmodified because nothing here selects a
field allowlist out of `evidence` -- only the wholesale-minus-`x_*`
projection.

De-brand: no 'Exos'/'EXOS' anywhere in this module (project de-brand rule).
"""

from __future__ import annotations

import json
from typing import Any, Optional

FLOOR_INTRO = (
    "The following invariants are non-negotiable. You must flag any clause "
    "that violates one. You cannot waive them."
)


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


def _posture_block(opf_doc: dict, overrides: Optional[dict] = None) -> str:
    """`overrides.posture.system_prompt` verbatim when a governed
    Posture-version override is given (issue #294 scope item 4 -- a GC
    single-item correction lever); otherwise `posture.system_prompt`
    verbatim off the genesis OPF. `posture.rubric` and `posture.generation`
    are never read here, so they cannot leak."""
    posture_override = (overrides or {}).get("posture")
    if posture_override and posture_override.get("system_prompt"):
        return str(posture_override["system_prompt"])
    posture = opf_doc.get("posture") or {}
    return str(posture.get("system_prompt", ""))


def _evidence_block(opf_doc: dict) -> str:
    """Compact, deterministic (`sort_keys=True`) projection of the WHOLE
    `evidence` section, `x_*` keys stripped -- evidence IS the knowledge,
    so nothing else is dropped (issue #284 scope)."""
    evidence = opf_doc.get("evidence") or {}
    return json.dumps(_strip_x_keys(evidence), sort_keys=True)


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


def _floor_block(opf_doc: dict, overrides: Optional[dict] = None) -> str:
    """Numbered plain-text list of `resolve_floor_invariants(opf_doc,
    overrides)` (id, statement, rationale), introduced by the fixed
    non-negotiable-invariants sentence.
    """
    invariants = resolve_floor_invariants(opf_doc, overrides)
    lines = [FLOOR_INTRO]
    for index, invariant in enumerate(invariants, start=1):
        invariant_id = invariant.get("id", "")
        statement = invariant.get("statement", "")
        rationale = invariant.get("rationale")
        line = f"{index}. [{invariant_id}] {statement}"
        if rationale:
            line += f" (Rationale: {rationale})"
        lines.append(line)
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


def compose_opf_system_blocks(opf_doc: dict, overrides: Optional[dict] = None) -> list[str]:
    """Compose an OPF document's knowledge into review system-prompt
    blocks, in fixed order: Posture, Evidence, Floor, then an optional
    Context block (only if `perspective` and/or `de_minimis` is present).

    `overrides` (issue #294, optional): a bound bundle's `overrides` block
    carrying a GC single-item correction -- `overrides.posture
    .system_prompt` redirects the Posture block source (genesis prose
    otherwise); `overrides.floor_additions` is unioned into the Floor
    block via `resolve_floor_invariants` (genesis first, stable order).
    Omitted/None reproduces pre-#294 behavior exactly.

    Pure: no I/O, no model call, no runtime wiring. Deterministic: the same
    `opf_doc` (and `overrides`) always produces byte-identical blocks (no
    timestamps, sorted JSON keys throughout).
    """
    blocks = [
        _posture_block(opf_doc, overrides),
        _evidence_block(opf_doc),
        _floor_block(opf_doc, overrides),
    ]
    context_block = _context_block(opf_doc)
    if context_block is not None:
        blocks.append(context_block)
    return blocks
