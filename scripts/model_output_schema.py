#!/usr/bin/env python3
"""
Model-facing output schema projection (issue #418) and provider-safe schema
projection (issue #567).

READING NOTE ON ARTIFACT NAMES. This docstring names
`playbooks/output-schema-v2.json` throughout because that was the sole
artifact when #418/#567 were written. The ACTIVE contract is
`playbooks/output-schema-v3.json` as of issue #627 (see `OUTPUT_SCHEMA_PATH`
below and the #624/#627 paragraph further down); every mechanism described
here is artifact-agnostic and unchanged -- only which file the default names
moved. Read a bare "output-schema-v2.json" below as "the full artifact being
projected", not as a claim about which one is active.

The full artifact is the pipeline's FULL validation contract for a model
response -- but two of its required fields are not something the model can
honestly answer:

  - top-level `schema_version` -- a fixed const the pipeline itself stamps
    (`scripts/primary_review_pass.py::_stamp_pipeline_envelope`), never a
    model judgment.
  - each `Issue`'s `provenance` -- which pipeline component produced the
    issue ("model" / "critic-added" / "detector:<rule_id>"), likewise
    stamped after the fact, never emitted by the model itself.

Under `OPENROUTER_STRUCTURED_OUTPUT=1` (issue #418,
`backend/src/config.py::structured_output_enabled`), the model is FORCED
(via a tool call) to emit an object matching a schema -- so that schema
must not require fields the model was never asked to produce. This module
derives that model-facing schema by removing both fields from
output-schema-v2.json.

A THIRD field is removed for a different reason, and CONDITIONALLY (issue
#522, epic #519 item D): each `Issue`'s optional
`internal_rationale_for_footnote` is genuine model prose, but whether a
review asks for internal-audience content at all is that review's notes
mode -- so the projection takes a `notes_mode` and keeps the property in
`internal`/`both`, strips it everywhere else. See
`_ISSUE_FIELDS_REQUESTED_ONLY_WITH_INTERNAL_NOTES` below. An
unconditional property would be a standing request on every review, which
is the one thing that epic forbids; a permanently absent one would leave
the renderer that consumes the field (`redline_docx_writer.
footnote_texts_for_notes_mode`) with nothing able to produce it, since
under provider enforcement the projected schema -- not the prompt's prose
-- decides what the model may emit.

`model_facing_output_schema()` is PROJECTION ONLY: the pipeline's actual
acceptance criterion is unchanged. `primary_review_pass.validate_model_response`
still runs `_stamp_pipeline_envelope` (filling both fields back in) and then
validates the full, unmodified `output-schema-v2.json` exactly as before --
this module changes what the model is ASKED for, never what is ACCEPTED.

Issue #567 (schema-enforced model output at the PROVIDER layer, on both
first-class adapters) adds a second, stricter projection:
`project_output_schema_for_provider()`. Provider structured-output
validators (Bedrock's Anthropic `output_config.format`, OpenRouter's
`response_format.json_schema`) reject jsonschema features
`output-schema-v2.json` uses freely -- string/numeric constraints,
a missing/non-`false` `additionalProperties`, a recursive `$ref` chain, and
(fix round 1, finding 1) a `required` list that omits a name present in
`properties`: OpenAI-strict-mode-shaped validators -- what
`backend/src/model_client.py`'s OpenRouter adapter requests via
`"strict": True` -- require EVERY property to appear in `required`; a
genuinely optional field is modelled as a nullable union (`"type":
["<type>", "null"]`, or an added `{"type": "null"}` `anyOf` branch) rather
than left out of `required`.

Fix round 2 tightened this further, after fix round 1's own nullable-union
conversion turned out to still be strictly LOOSER than the full schema, not
merely looser in the intended (harmless) direction -- see
`_already_permits_null` for the two-part fix: (a) `_make_nullable_in_place`
is now only ever called (via that pass's own gate) on a property the full
schema ALREADY makes nullable -- a property that was merely optional (no
null branch) is instead added to `required` with its type UNCHANGED, safe
exactly where the full schema already accepts an empty `""`/`[]` for it;
(b) v2's optional `Issue` quote field -- the one property with neither a
null branch NOR an emittable empty value (`minLength: 1`) -- was dropped
from the projected schema entirely rather than forced into `required` with
no honest value to give it. Fix round 2, finding 3 also rewrites every
`oneOf` this projection produces or preserves to `anyOf` (OpenAI-strict-
mode's supported-keyword subset has the latter, not the former) and strips
the non-JSON-Schema-validation root keywords (`$schema` / `$id` /
`output_contract_version`) the source file carries.

Fix round 3 REVERSED that: under v2 the quote field was the ONLY way a
REQUEST_CHANGE issue located its redline target, so dropping it meant every
issue on a structured-outputs-capable model shipped zero redlines. The fix
was a NEW `null` branch instead (`_make_issue_fields_nullable_in_place` /
`_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`) -- a real, emittable "no value"
-- paired with a post-hoc normalization in
`primary_review_pass.py::_denullify_unrepresentable_issue_fields` that
strips a `null`/empty value back to ABSENT (a value the full schema already
treats identically) before the full schema check runs. Issue #627 removed
the quote field from the contract and issue #628 deleted the locator that
read it, so that field is gone from both lists; the MACHINERY survives
because `internal_rationale_for_footnote` (issue #522) has exactly the same
shape and still needs it. Fix round 3 also corrected `_break_recursive_refs_
in_place`'s flattened substitution node, which had been getting
`additionalProperties: false` forced onto it with no `properties` to
match -- accepting only `{}` rather than the "genuinely permissive" node
its own docstring claimed.

Issue #624 added a THIRD artifact both entry points must handle,
`playbooks/output-schema-v3.json` (the Candidate E block-transcript
contract), reached by passing its path as the `path` argument. Issue #627's
hard cutover then made it the DEFAULT (see `OUTPUT_SCHEMA_PATH` below), so
every request this module projects for a first-party review is now a v3
request; v2 is what a caller passes explicitly. Two v3-only shapes are the
ones that must survive the strict passes
above intact: the top-level `block_patches[]` (each entry a `block_id`
plus an ordered `segments[]` transcript) and `block_ops[]` (whole-block
`delete_block` / `insert_block_after`). Nothing here drops them -- every
pass above either rewrites a keyword in place or adds to `required` -- and
this module's tests assert that end to end, because fix round 3's lesson
was precisely that a field silently missing from the projected schema
ships ZERO redlines on every structured-outputs model while every fixture
test stays green. One of the passes is a no-op on v3 in the notes modes that strip it:
`_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH` names only
`internal_rationale_for_footnote`, which the projection carries in the
`internal`/`both` modes alone, so `_make_issue_fields_nullable_in_place`
finds nothing to widen in `none`/`external`; and v3's
`Issue.replacement_scope_note` / `proposed_replacement_text` are optional
with NO `minLength` floor, so `_force_all_properties_required_in_place`
adds them to `required` with their type untouched, `""` being a value the
full v3 schema already accepts.

This is a SEPARATE projection from `model_facing_output_schema` (built ON
TOP of it -- the model still cannot honestly emit `schema_version` /
`provenance` under provider enforcement either), never a replacement: the
model-facing tool-mode schema (#418) and the provider-safe schema (#567)
are two independent request-shaping seams that happen to share the same
stamped-field starting point. Same PROJECTION-ONLY discipline applies
throughout: the full, unmodified ACTIVE artifact (`output-schema-v3.json`
since issue #627) still governs post-hoc validation in
`primary_review_pass.validate_model_response`
regardless of which (if either) projection a given request used --
`_denullify_unrepresentable_issue_fields` above is what keeps that true
now that the projection can emit a value (`internal_rationale_for_
footnote: null`) the full schema does not itself accept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
# Issue #627 (the hard cutover) flipped this default from
# `output-schema-v2.json` to v3, in the same diff that flipped
# `primary_review_pass.OUTPUT_SCHEMA_PATH` and the prompt. Both request-side
# projections below are built from whichever artifact is named here, so a
# default left on v2 would have projected a v2-shaped tool schema for a
# v3-shaped prompt -- the request half of the very drift this cutover exists
# to prevent. `run_primary_pass`/`run_critic_pass` pass their own
# `output_schema_path` explicitly; this default serves every other caller.
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"

# Pipeline-stamped fields the model must not be asked to produce -- see the
# module docstring. Kept as their own named tuples (rather than one shared
# list) so a future stamped field can be added to just the level it applies
# to without disturbing the other.
_TOP_LEVEL_STAMPED_FIELDS = ("schema_version",)
_ISSUE_STAMPED_FIELDS = ("provenance",)

# Issue-level fields the PIPELINE derives under a block-transcript contract
# (issue #627), removed from the model-facing projection for the same reason
# `_ISSUE_STAMPED_FIELDS` are: the model is not the source of truth for them,
# so asking is at best noise and at worst a contradiction.
#
# `proposed_replacement_text` is v3's case. The v3 prompt explicitly tells the
# model NOT to send it -- its edits ARE its proposal, and
# `redline_generate.derived_replacement_text_by_issue` computes this field
# from the PROVEN transcript. Left in the projection it would be a field a
# strict-mode provider is contractually REQUIRED to emit
# (`_force_all_properties_required_in_place` makes every remaining property
# required) while the prompt in the same request forbids it: an impossible
# instruction, and precisely the prompt/request drift the cutover exists to
# remove. Under v1/v2 the model DOES author this field, so the removal is
# conditional on the artifact -- see `authors_block_transcripts`.
_ISSUE_FIELDS_DERIVED_UNDER_A_BLOCK_TRANSCRIPT_CONTRACT = ("proposed_replacement_text",)


def authors_block_transcripts(schema: dict[str, Any]) -> bool:
    """Whether `schema` is a block-transcript output contract -- i.e. it
    defines a top-level `block_patches` carrier (issue #627).

    Read off the ARTIFACT, never off a version literal: the contract is what
    the file says. This module owns the predicate because it owns schema
    introspection; `primary_review_pass.authors_block_transcripts` is the
    same function re-exported, so both halves of the pipeline decide "does
    the model author replacement text here?" from ONE answer.
    """
    return "block_patches" in (schema.get("properties") or {})

# Issue-level fields the model is asked for ONLY when this review's notes
# mode puts internal-audience content in scope -- removed from the
# model-facing projection (and therefore from the provider projection built
# on top of it) exactly like a stamped field, for a DIFFERENT reason.
#
# `internal_rationale_for_footnote` (issue #522, epic #519 item D) is real
# model prose, not pipeline metadata -- but WHETHER a review asks the model
# for internal-audience content is decided from that review's notes mode
# (epic #519: "notes mode is a pipeline INPUT, not a render-time filter" --
# internal content has to be *requested* to exist, and a review in
# `none`/`external` must never be told to produce any). An UNCONDITIONAL
# property in the model-facing schema would be an invitation on EVERY
# review, and under a strict provider projection
# (`_force_all_properties_required_in_place`) a field the model is
# contractually obliged to fill in or null out -- the exact posture the
# epic rules out. So the property is stripped in `none`/`external` and kept
# in `internal`/`both`, in step with the prompt half of the same gate
# (`primary_review_pass.render_binary_decision_overlay_block`, which adds
# the key to the issue-object contract in exactly those two modes).
#
# BOTH halves have to open together. Prose alone cannot produce the field
# on a provider-enforced request (the projected schema decides what may be
# emitted, and `additionalProperties: false` is forced on every object
# node); a schema property alone would be a key the prompt's "EXACTLY these
# keys and no others" sentence forbids. Either half left closed leaves
# `redline_docx_writer.footnote_texts_for_notes_mode` rendering a field
# nothing can populate -- a renderer that is dead on every real review.
#
# The FULL schema declares the field optional in every mode, so this
# constant governs only what the model is ASKED for, never what is
# ACCEPTED: a value that arrives some other way validates and renders.
_ISSUE_FIELDS_REQUESTED_ONLY_WITH_INTERNAL_NOTES = ("internal_rationale_for_footnote",)

# Notes modes that put internal-audience content in scope for a review
# (epic #519 axis 1). Deliberately a local copy of the same one-line
# predicate `primary_review_pass._notes_mode_includes_internal`,
# `redline_generate._notes_mode_includes_internal_content` and
# `redline_docx_writer.footnote_texts_for_notes_mode` each keep: this
# module is imported BY `primary_review_pass`, so importing it back would
# be a cycle, and a shared constants module for one boolean would be a
# layer for its own sake. Unrecognized/blank is NOT internal -- the same
# fail-closed direction all four take, so a caller that failed to validate
# upstream gets the counterparty-safe projection.
_NOTES_MODES_WITH_INTERNAL_CONTENT = ("internal", "both")


def _notes_mode_includes_internal_content(notes_mode: str) -> bool:
    """Whether `notes_mode` puts internal-audience content in scope -- see
    `_NOTES_MODES_WITH_INTERNAL_CONTENT` above."""
    return (notes_mode or "").strip().lower() in _NOTES_MODES_WITH_INTERNAL_CONTENT


def _strip_stamped_fields(
    properties: dict[str, Any], required: list[Any], fields: tuple[str, ...]
) -> list[Any]:
    """Remove every name in `fields` from `properties` (in place, so a
    $ref'd definition mutated here stays mutated for every reader of that
    same dict) and return a NEW `required` list with them removed --
    `required`'s remaining order is preserved, `properties` is mutated
    directly since schema objects have no separate "remove key" op."""
    for field in fields:
        properties.pop(field, None)
    return [name for name in required if name not in fields]


def model_facing_output_schema(
    path: Path = OUTPUT_SCHEMA_PATH, notes_mode: str = "external"
) -> dict[str, Any]:
    """The projected JSON Schema sent as the forced tool's `parameters`
    under structured output (issue #418): `output-schema-v2.json` with the
    pipeline-stamped fields removed from every `required` list AND every
    `properties` dict they appear in --

      - top-level `schema_version` (removed from the document root).
      - `provenance` on the shared `definitions.Issue` schema -- reached
        from BOTH the top-level `issues` array and
        `critic_delta.added_issues`, since both `$ref` the identical
        definition, so one removal covers both.

    ...plus `_ISSUE_FIELDS_REQUESTED_ONLY_WITH_INTERNAL_NOTES`
    (`internal_rationale_for_footnote`, issue #522), removed the same way
    for a different reason -- see that constant's own comment.

    `notes_mode` (issue #522, epic #519 item D, default `"external"` --
    matching `primary_review_pass.assemble_system_blocks`' own default, so
    an un-migrated caller gets today's projection byte for byte) governs
    only that last removal: `internal`/`both` KEEP
    `internal_rationale_for_footnote` (the modes whose delivered document
    renders it, and whose prompt asks for it -- `primary_review_pass.
    render_binary_decision_overlay_block`), every other value strips it.
    Unrecognized/blank strips it, the fail-closed direction.

    Loads `path` fresh on every call (no module-level cache) -- this is a
    small on-disk file read once per pass, not a hot loop, matching this
    codebase's existing per-call policy-file reads (e.g.
    `model_client.load_openrouter_policy`). The returned dict is this
    call's own object (nothing else on the process holds a reference to
    it), so a caller serializing or further mutating it cannot corrupt
    another caller's copy.
    """
    with open(path, "r", encoding="utf-8") as fh:
        schema: dict[str, Any] = json.load(fh)

    top_properties = schema.get("properties") or {}
    top_required = schema.get("required") or []
    schema["required"] = _strip_stamped_fields(
        top_properties, top_required, _TOP_LEVEL_STAMPED_FIELDS
    )

    issue_def = (schema.get("definitions") or {}).get("Issue") or {}
    issue_properties = issue_def.get("properties") or {}
    issue_required = issue_def.get("required") or []
    if issue_properties or issue_required:
        issue_required = _strip_stamped_fields(
            issue_properties, issue_required, _ISSUE_STAMPED_FIELDS
        )
        internal_notes_fields = (
            ()
            if _notes_mode_includes_internal_content(notes_mode)
            else _ISSUE_FIELDS_REQUESTED_ONLY_WITH_INTERNAL_NOTES
        )
        issue_required = _strip_stamped_fields(
            issue_properties, issue_required, internal_notes_fields
        )
        derived_fields = (
            _ISSUE_FIELDS_DERIVED_UNDER_A_BLOCK_TRANSCRIPT_CONTRACT
            if authors_block_transcripts(schema)
            else ()
        )
        issue_def["required"] = _strip_stamped_fields(
            issue_properties, issue_required, derived_fields
        )

    return schema


# ---------------------------------------------------------------------------
# Provider-safe schema projection (issue #567).
# ---------------------------------------------------------------------------

# String/numeric jsonschema constraint keywords that provider structured-
# output validators (OpenAI-strict-mode-shaped, which OpenRouter's
# `response_format.json_schema` and Bedrock's Anthropic `output_config`
# both follow) are documented to reject outright rather than merely ignore.
# Stripped wherever they appear -- output-schema-v2.json uses `minLength`
# `/maxLength`/`pattern` extensively (every free-text Issue field) and
# `maxLength` again on CriticDelta's nested objects; `format` and the
# numeric keywords are included for completeness even though no CURRENT
# property in this schema uses them, so a future schema edit that adds one
# does not silently reintroduce a provider-rejected request.
_UNSUPPORTED_STRING_CONSTRAINT_KEYWORDS = ("minLength", "maxLength", "pattern", "format")
_UNSUPPORTED_NUMERIC_CONSTRAINT_KEYWORDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)
# Array-cardinality keywords, same rejection class as the string/numeric ones
# above. No CURRENT property in `output-schema-v2.json` uses any of them --
# but `playbooks/output-schema-v3.json` (issue #624) does: its
# `BlockPatch.segments` carries `minItems: 1`, since a block patch with no
# segments transcribes nothing. Stripping it keeps the projection's own
# documented direction of looseness ("requests fewer/broader things"): a
# provider-enforced response that sends `segments: []` anyway still fails
# closed at `primary_review_pass.validate_model_response`'s FULL-schema
# check, exactly like an over-length `section_ref` that survived the
# `maxLength` strip. `maxItems`/`uniqueItems` are listed for completeness,
# same forward-compat reasoning as the unused numeric keywords.
_UNSUPPORTED_ARRAY_CONSTRAINT_KEYWORDS = ("minItems", "maxItems", "uniqueItems")
_UNSUPPORTED_CONSTRAINT_KEYWORDS = (
    _UNSUPPORTED_STRING_CONSTRAINT_KEYWORDS
    + _UNSUPPORTED_NUMERIC_CONSTRAINT_KEYWORDS
    + _UNSUPPORTED_ARRAY_CONSTRAINT_KEYWORDS
)

# Root JSON-Schema-file keywords `playbooks/output-schema-v2.json` carries
# that are not part of the validation-relevant subset a provider structured-
# output validator is documented to support: `$schema` / `$id` identify the
# file as a standalone, dereferenceable schema document (meaningless -- and
# per fix round 2, finding 3, a further candidate for outright rejection by
# an OpenAI-strict-mode-shaped validator -- once embedded as an inline
# `schema` VALUE inside a provider request body rather than served as a
# document in its own right), and `output_contract_version` is this
# repo's own release-tracking metadata (see this file's top-level
# description), not a jsonschema keyword at all. Stripped from the root of
# the projected schema only -- neither key is ever repeated on a nested
# node in this file. This is the same failure class fix round 1, finding 1
# (and fix round 2, finding 3's `oneOf` -> `anyOf` rewrite below) already
# caught: a request-shape a provider's structured-output validator rejects
# outright, previously via a different keyword.
_NON_SCHEMA_ROOT_KEYWORDS = ("$schema", "$id", "output_contract_version")

# Optional `definitions.Issue` properties (fix round 2, finding 1; REVISED
# fix round 3, finding 1) whose FULL schema definition offers NO value a
# provider-enforced request could honestly emit for "no value": `minLength:
# 1` (an empty string is rejected) and no `null`/`oneOf`/`anyOf` branch.
# Every OTHER property `_force_all_properties_required_in_place` newly adds
# to `required` -- the three `CriticDelta` arrays and
# `contested_replacements.items.critic_suggested_replacement` -- has an
# emittable "no value" the full schema already accepts (`[]` / `""`, no
# `minItems`/`minLength` floor), so those are simply added to `required`
# with their type untouched (see `_already_permits_null`).
#
# HISTORY, because the reasoning is what makes the exception safe. Under
# v2 this list also named that schema's optional quote field -- the address
# a REQUEST_CHANGE issue located its redline target by. Fix round 2 dropped
# it from the projected schema entirely to sidestep the problem; fix round 3
# found that unacceptable, because a schema-enforced call could then never
# carry an address, so EVERY issue on EVERY structured-outputs-capable model
# (all six `model-policy/openrouter.json` `selectable` entries, plus
# Bedrock's pinned primary/critic) would route to `MANUAL_REVIEW_REQUIRED`
# with `docx_bytes=None`: zero redlines produced, silently, on the very
# capability that ticket hardened. The fix was to make the field NULLABLE in
# the projection (a real, emittable "no value" a strict-mode provider can
# send) and pair it with a normalization in
# `primary_review_pass.py::_denullify_unrepresentable_issue_fields` that
# strips a `null` (or empty-string) value back to ABSENT before the
# full-schema check runs -- the full schema already treats "absent" and "no
# value" identically, so this loses no information the model actually
# conveyed; it only reshapes "I have none" into the form both schemas agree
# on. Issue #627 replaced quote addressing with block transcripts and issue
# #628 deleted the locator, so that field is gone from the contract and from
# this list.
#
# Widening the FULL artifact instead was considered and rejected then, and
# the reasoning still governs the surviving entry: editing
# `playbooks/output-schema-v3.json` changes the pipeline's single validation
# source of truth and, per that file's own top-level description and
# docs/output-contract.md, requires a new `release.output_contract_hash`
# plus legal-governance review -- out of scope for this projection-only
# module, and unnecessary once the post-hoc normalization exists. NOT the
# same category as `_TOP_LEVEL_STAMPED_FIELDS`/`_ISSUE_STAMPED_FIELDS`
# above (pipeline-owned metadata the model was never asked to produce, on
# EITHER projection): these are model judgments both the non-enforced
# fallback path (`model_facing_output_schema`, #418) and the strict provider
# projection below request and can both receive.
#
# `internal_rationale_for_footnote` (issue #522, fix round 2) is the
# surviving field of exactly this shape: `minLength: 1` with no
# `null`/`anyOf` branch in the full schema, so once it is present in the
# projection at all (`internal`/`both` only) the force-required pass would
# otherwise oblige the model to invent an internal note on EVERY issue --
# turning an optional note into a mandatory one and filling the delivered
# document with `[INTERNAL]` footnotes nobody needed. Listing it here is
# harmless in the modes that strip it: `_make_issue_fields_nullable_in_
# place` no-ops on a field its `properties` does not carry, so
# `none`/`external` projections are untouched.
_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH = ("internal_rationale_for_footnote",)


def _make_issue_fields_nullable_in_place(schema: dict[str, Any]) -> None:
    """Give every field in `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH` a
    `null` branch on `schema["definitions"]["Issue"]["properties"]` (schema
    mutated in place, via `_make_nullable_in_place`) -- reached from BOTH
    the top-level `issues` array and `critic_delta.added_issues` since both
    `$ref` the identical Issue definition, so one call covers both, exactly
    like `_strip_stamped_fields` above.

    Calls `_make_nullable_in_place` UNCONDITIONALLY (never gated by
    `_already_permits_null`) -- unlike every property
    `_force_all_properties_required_in_place` nullifies, these fields are
    deliberately gaining a null branch the FULL schema does NOT already
    have; `_already_permits_null` answering False for them is precisely the
    reason they are handled here rather than by that pass's generic gate.
    See `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`'s own docstring for why
    this is safe: the projected schema's new `null` branch is a real value
    a provider can emit, and a companion normalization
    (`primary_review_pass._denullify_unrepresentable_issue_fields`) strips
    it back to "absent" -- a value the full schema already accepts -- before
    the full-schema check runs.

    Must run BEFORE `_force_all_properties_required_in_place` (`project_
    output_schema_for_provider`'s pass order) so that pass's own
    `_already_permits_null` gate sees the null branch this function just
    added and simply adds the field to `required` without re-deriving
    anything -- the same "idempotent confirmation" path every
    already-nullable property takes. `properties.get` no-ops if the field
    is already absent (a synthetic test schema need not carry it), same
    defensive-no-op contract `_strip_stamped_fields` has.
    """
    issue_def = (schema.get("definitions") or {}).get("Issue") or {}
    issue_properties = issue_def.get("properties") or {}
    for field in _ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH:
        prop_schema = issue_properties.get(field)
        if prop_schema is not None:
            _make_nullable_in_place(prop_schema)


# Dict keys whose VALUE is a map of arbitrary NAME -> schema -- "properties"
# (`{"decision": {<the decision property's own schema>}, ...}`) and
# "definitions" (`{"Issue": {<the Issue definition's own schema>}, ...}`).
# Neither map is itself a schema node: its KEYS are property/definition
# names, never schema keywords, even when a name happens to collide with
# one (a property literally named "format" or "pattern", say). Every walker
# below that recurses generically over `node.values()` must special-case
# these two keys and recurse into the MAP'S VALUES instead of the map
# itself -- see `_strip_unsupported_constraints_in_place`'s docstring
# (fix round 1, finding 3) for the bug this constant closes.
_SCHEMA_VALUE_MAP_KEYS = ("properties", "definitions")


def _walk_schema_values(node: dict[str, Any], visit: Callable[[Any], None]) -> None:
    """Shared traversal helper: call `visit` on every VALUE reachable one
    level down from schema node `node`, treating `properties` and
    `definitions` specially (their own dict is a name->schema MAP, not a
    schema node -- see `_SCHEMA_VALUE_MAP_KEYS` -- so `visit` is called on
    each of ITS values, never on the map dict itself) and every other key
    (`items`, `oneOf`, `anyOf`, `allOf`, ...) generically. Used by both
    `_strip_unsupported_constraints_in_place` and
    `_force_all_properties_required_in_place` so the two walkers cannot
    drift apart on this distinction."""
    for key, value in node.items():
        if key in _SCHEMA_VALUE_MAP_KEYS and isinstance(value, dict):
            for sub_schema in value.values():
                visit(sub_schema)
        else:
            visit(value)


def _strip_unsupported_constraints_in_place(node: Any) -> None:
    """Recursively remove every keyword in `_UNSUPPORTED_CONSTRAINT_KEYWORDS`
    from every schema object reachable from `node` (properties, items,
    definitions, oneOf/anyOf/allOf branches -- anything nested), and force
    `additionalProperties: false` on every object-shaped schema node (one
    declaring `"type": "object"` or carrying a `"properties"` key).

    output-schema-v2.json already declares `additionalProperties: false` on
    every object-shaped node as of this writing (a no-op re-assignment
    here), so this normalization is currently defensive rather than fixing
    a live gap -- but the projected schema is a REQUEST-SHAPE contract a
    provider validator can reject outright on a missing/`true` value, so a
    future schema edit that adds an object without it must not silently
    produce a provider-rejected request. See this module's own tests for a
    synthetic schema that DOES exercise the "was missing, now forced"
    case.

    Fix round 1, finding 3: the keyword-popping loop below runs ONLY on
    `node` itself, a schema node -- it must never run on a `properties` (or
    `definitions`) MAP, whose keys are arbitrary names, not schema
    keywords. A property literally named `format`/`pattern`/`minLength`/
    etc. used to be silently deleted from `properties` (while `required`
    kept naming it, since `required` is a list of strings this function
    never touches) -- an unsatisfiable schema once `additionalProperties:
    false` is also forced, since the model would have no way to emit a
    required key with no definition. `_walk_schema_values` (shared with
    `_force_all_properties_required_in_place`) is what keeps this walker
    from ever treating a `properties`/`definitions` map as a schema node in
    the first place. See this module's own tests for a synthetic schema
    with a property named `format` that DOES exercise this.

    Mutates `node` in place; the caller (`project_output_schema_for_provider`
    below) is responsible for handing this a deep copy it owns, never the
    cached/loaded source schema.
    """
    if isinstance(node, list):
        for item in node:
            _strip_unsupported_constraints_in_place(item)
        return
    if not isinstance(node, dict):
        return
    for keyword in _UNSUPPORTED_CONSTRAINT_KEYWORDS:
        node.pop(keyword, None)
    if node.get("type") == "object" or "properties" in node:
        node["additionalProperties"] = False
    _walk_schema_values(node, _strip_unsupported_constraints_in_place)


def _convert_one_of_to_any_of_in_place(node: Any) -> None:
    """Recursively rewrite every `"oneOf"` key to `"anyOf"` on every schema
    object reachable from `node` (properties, items, definitions --
    anything nested, via the same `_walk_schema_values` traversal
    `_strip_unsupported_constraints_in_place` and
    `_force_all_properties_required_in_place` use, so a `properties`/
    `definitions` MAP is never mistaken for a schema node here either).

    Fix round 2, finding 3: OpenAI-strict-mode-shaped validators --
    `backend/src/model_client.py`'s OpenRouter adapter sends `"strict":
    True` on the structured-output request -- support `anyOf` but not
    `oneOf` in their documented supported-keyword subset.
    `output-schema-v2.json` itself uses `oneOf` at four sites reached by
    this projection (root `confidence_band` / `critic_delta` /
    `verdict_summary`, `definitions.Issue.properties.
    internal_precedent_citation`) to express "this or null", and
    `_make_nullable_in_place` (below) used to emit further `oneOf` unions
    for the same reason -- the same failure class fix round 1, finding 1
    already caught once (a `required` list a strict-mode validator
    rejects), reintroduced here via a different unsupported keyword.

    `oneOf`'s stricter "exactly one branch matches" semantics and
    `anyOf`'s looser "at least one branch matches" are interchangeable
    here specifically because every branch in every one of these unions is
    mutually exclusive by construction -- a `{"type": "null"}` branch can
    never also match a non-null branch, and vice versa -- so swapping the
    keyword changes no VALUE this schema accepts or rejects, only which
    keyword names the union.

    Runs BEFORE `_force_all_properties_required_in_place` in `project_
    output_schema_for_provider`'s pass order so that pass's own null-branch
    bookkeeping (`_already_permits_null`, `_make_nullable_in_place`) only
    ever has to recognize ONE union keyword (`anyOf`), never both.

    Mutates `node` in place, same ownership contract as this module's other
    structural passes.
    """
    if isinstance(node, list):
        for item in node:
            _convert_one_of_to_any_of_in_place(item)
        return
    if not isinstance(node, dict):
        return
    if "oneOf" in node:
        node["anyOf"] = node.pop("oneOf")
    _walk_schema_values(node, _convert_one_of_to_any_of_in_place)


def _definition_ref_name(node: Any, definitions: dict[str, Any]) -> str | None:
    """If `node` is a bare `{"$ref": "#/definitions/<Name>"}` pointer into
    `definitions`, return `<Name>`; otherwise None. A node carrying `$ref`
    ALONGSIDE other keys (not produced anywhere in this codebase's schemas,
    but not a jsonschema violation either) is deliberately not matched --
    only an exact single-key `$ref` pointer is treated as a traversable
    definition reference."""
    if isinstance(node, dict) and set(node.keys()) == {"$ref"}:
        ref = node["$ref"]
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            name = ref[len("#/definitions/") :]
            if name in definitions:
                return name
    return None


def _break_recursive_refs_in_place(schema: dict[str, Any]) -> None:
    """Flatten a recursive `$ref` chain in `schema["definitions"]` -- a
    definition that, directly or through one or more other definitions,
    refers back to itself -- to a genuinely permissive `{}`-shaped node
    (a `description` key only -- no `"type"`, no `"properties"`) at the
    exact point the cycle would close.

    Provider structured-output validators are documented to reject an
    unbounded/self-referencing schema (see the module docstring's
    "recursion" bullet); this walker is a plain depth-first traversal of
    the ACTUAL definition bodies (never resolving `$ref` at every site,
    only when the pointer targets a known definition), tracking the chain
    of definition names on the current path so a `$ref` back to any name
    already on that path -- direct self-reference or mutual recursion
    between two-or-more definitions -- is caught and flattened.

    Fix round 3, finding 3: this used to substitute `{"type": "object",
    "description": ...}`, which reads as permissive but is NOT -- the next
    pass, `_strip_unsupported_constraints_in_place`, forces
    `additionalProperties: false` on any node with `"type": "object"`,
    and a node with no `"properties"` key plus `additionalProperties:
    false` accepts ONLY the empty object `{}`, not an actual instance of
    the flattened definition. Omitting `"type"` here (a bare `{}` schema
    modulo the `description` key, which is non-constraining metadata) means
    the later pass has nothing to force `additionalProperties` onto, so the
    node stays genuinely permissive -- correct for a point where recursion
    is being intentionally truncated and the FULL schema (with the real
    `$ref`) is relied on for the actual post-hoc check anyway.

    NOT currently reachable from output-schema-v2.json: neither `Issue`
    nor `CriticDelta` refers back to itself or to each other (both only
    ever REACH `Issue`, never the other way around). This exists to guard
    a future schema edit from silently producing a provider-rejected
    request rather than because today's file needs it -- see this
    module's own tests for a synthetic schema that DOES exercise it.

    Mutates `schema` in place, same contract as
    `_strip_unsupported_constraints_in_place`.
    """
    definitions = schema.get("definitions")
    if not isinstance(definitions, dict) or not definitions:
        return

    def _walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item, path)
            return
        if not isinstance(node, dict):
            return
        target = _definition_ref_name(node, definitions)
        if target is not None:
            if target in path:
                node.clear()
                node["description"] = (
                    "Recursive reference flattened for provider "
                    "structured-output compatibility; the FULL schema "
                    "(with the real $ref) still governs post-hoc validation."
                )
                return
            _walk(definitions[target], path + (target,))
            return
        for value in node.values():
            _walk(value, path)

    for name, body in list(definitions.items()):
        _walk(body, (name,))


def _make_nullable_in_place(prop_schema: Any) -> None:
    """Convert `prop_schema` (a property's OWN schema, mutated in place) to
    accept `null` in addition to whatever it already accepted.

    Fix round 2, finding 1: as of that fix, `_force_all_properties_
    required_in_place` calls this ONLY when `_already_permits_null(
    prop_schema)` is already True (see that predicate's docstring) -- so
    from THAT call site, every invocation is a no-op confirmation that the
    union is already well-formed, never a live widening. Fix round 3 added
    a second call site, `_make_issue_fields_nullable_in_place`, that calls
    this UNCONDITIONALLY on the fields it names -- a genuine, live
    widening the full schema does not already offer (see
    `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`'s docstring for why that
    field is the deliberate exception). The three branches below stay
    fully general (not narrowed to "already nullable") on purpose: this
    function's own contract is just "make it nullable", never "decide
    whether that is safe against some other schema" -- that decision
    belongs to the CALLER (`_already_permits_null`'s gate for the first
    call site; `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`'s own reasoning,
    paired with the post-hoc denullify normalization, for the second) --
    so a future caller with a different
    safety requirement is not forced to fork this primitive.

    Three shapes, in the order real (and this module's synthetic test)
    schemas actually use them:

      - `"type"` is a bare string (`"string"`, `"array"`, ...): becomes a
        `["<type>", "null"]` list.
      - `"type"` is already a list: `"null"` is appended if not already
        present (idempotent -- calling this twice on the same node is
        harmless).
      - Neither: e.g. an `"anyOf"` union (root's `confidence_band` /
        `critic_delta` / `verdict_summary` are ALREADY shaped this way by
        the time this runs -- `_convert_one_of_to_any_of_in_place`, fix
        round 2 finding 3, has already rewritten their original `oneOf` to
        `anyOf` earlier in `project_output_schema_for_provider`'s pass
        order -- anyOf-ing `{"type": "null"}` against the real branch,
        since they were nullable-but-optional before fix round 1 -- a
        `{"type": "null"}` branch is appended only if one is not already
        present) or a bare `{"$ref": ...}` pointer / an empty `{}` schema
        (neither occurs in output-schema-v2.json today, but a synthetic
        recursive-`$ref` test schema exercises the `$ref` case once
        flattened) -- wrapped as `{"anyOf": [<original-schema>, {"type":
        "null"}]}` so the union stays valid regardless of the original
        shape.
    """
    if not isinstance(prop_schema, dict):
        return
    existing_type = prop_schema.get("type")
    if isinstance(existing_type, str):
        prop_schema["type"] = [existing_type, "null"]
        return
    if isinstance(existing_type, list):
        if "null" not in existing_type:
            prop_schema["type"] = existing_type + ["null"]
        return
    any_of = prop_schema.get("anyOf")
    if isinstance(any_of, list):
        already_nullable = any(
            isinstance(branch, dict) and branch.get("type") == "null" for branch in any_of
        )
        if not already_nullable:
            any_of.append({"type": "null"})
        return
    # No "type" and no "anyOf" -- a bare `$ref`, or an empty/maximally
    # permissive `{}` schema. Wrap the schema AS-IS alongside a null
    # branch so the union stays well-formed regardless of shape.
    original = dict(prop_schema)
    prop_schema.clear()
    prop_schema["anyOf"] = [original, {"type": "null"}] if original else [{"type": "null"}]


def _already_permits_null(prop_schema: Any) -> bool:
    """True if `prop_schema` (a property's OWN schema) ALREADY accepts
    `null` without any modification -- `"type"` already a list containing
    `"null"`, or an `"anyOf"` already carrying a `{"type": "null"}` branch.
    Checked as `anyOf`, never `oneOf`: `_convert_one_of_to_any_of_in_place`
    (fix round 2, finding 3) runs earlier in `project_output_schema_for_
    provider`'s pass order and has already renamed every surviving `oneOf`
    by the time this runs.

    Fix round 2, finding 1: before this predicate existed,
    `_force_all_properties_required_in_place` called `_make_nullable_
    in_place` on EVERY property it newly added to `required`, regardless of
    whether the FULL schema offered a
    `null` branch for that property at all -- widening 5 fields (v2's
    `Issue` quote field, `CriticDelta.{added_issues,
    contested_replacements,rationale_objections}`,
    `CriticDelta.contested_replacements.items.
    critic_suggested_replacement`) to accept a value (`null`) the FULL
    schema outright rejects, so a strict-mode-compliant response -- which
    MUST emit every required key with SOME value -- became exactly the
    response the pipeline's own post-hoc validation throws away. This
    predicate gates that call: only a property the full schema ALREADY
    makes nullable may be (redundantly, idempotently) run through
    `_make_nullable_in_place`; every other newly-required property is left
    with its ORIGINAL type -- safe because the full schema places no
    `minItems`/`minLength` floor on the four array/string properties above
    (an empty `[]`/`""` is a value BOTH schemas accept). Every field in
    `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH` (which DOES have a
    `minLength: 1` floor with no null escape in the FULL schema) is handled
    differently, not by this predicate: fix round 3 gives it a NEW null
    branch BEFORE this pass runs, via
    `_make_issue_fields_nullable_in_place` -- so by the time THIS
    predicate checks it, the null branch already exists (added moments
    ago, not inherited from the full schema) and `_already_permits_null`
    correctly answers True, folding it into the same idempotent-
    confirmation path as a property the full schema made nullable itself.
    See that constant's own comment for why those fields need the special
    upstream step at all: unlike the four properties above, they have no
    full-schema-accepted "empty" value to fall back to un-widened.
    """
    if not isinstance(prop_schema, dict):
        return False
    existing_type = prop_schema.get("type")
    if isinstance(existing_type, list) and "null" in existing_type:
        return True
    any_of = prop_schema.get("anyOf")
    if isinstance(any_of, list):
        return any(
            isinstance(branch, dict) and branch.get("type") == "null" for branch in any_of
        )
    return False


def _force_all_properties_required_in_place(node: Any) -> None:
    """Recursively force `required == list(properties)` on every object-
    shaped schema node reachable from `node` (properties, items,
    definitions, oneOf/anyOf/allOf branches -- anything nested, via the
    same `_walk_schema_values` traversal `_strip_unsupported_constraints_
    in_place` uses -- so a `properties`/`definitions` MAP is never mistaken
    for a schema node here either).

    Fix round 1, finding 1: `model_client.py`'s OpenRouter adapter sends
    `"strict": True` on the structured-output request (AC2) -- OpenAI-
    strict-mode-shaped validators reject ANY object-shaped node whose
    `required` omits a name present in `properties`, even a genuinely
    optional field. Before this pass, four nodes in the real projected
    schema violated that: the document root (`confidence_band`,
    `critic_delta`, `verdict_summary`), `definitions.Issue` (its optional
    properties), `definitions.CriticDelta` (`added_issues`,
    `contested_replacements`, `rationale_objections`), and
    `definitions.CriticDelta.properties.contested_replacements.items`
    (`critic_suggested_replacement`) -- a live admin-selectable
    `structured_outputs: true` model (model-policy/openrouter.json's
    `selectable` allowlist) would get a 400 on every request built from
    the unfixed schema.

    Fix round 2, finding 1: fix round 1's own remedy above turned out to
    still be LOOSER than the full schema, not merely differently-shaped --
    unconditionally nullifying every newly-required property let a strict-
    mode-compliant model emit `null` for `Issue`'s optional fields / the
    three `CriticDelta` arrays / `critic_suggested_replacement`, a value the FULL
    schema (no `null`/`anyOf` branch on any of the four) rejects outright,
    so the provider-compliant response became exactly the one
    `primary_review_pass.validate_model_response` throws away. A property
    that was NOT already required is now converted to a nullable union
    FIRST (`_make_nullable_in_place`) only when `_already_permits_null`
    says the full schema already offers one (root's `confidence_band` /
    `critic_delta` / `verdict_summary`, unaffected by this fix). The other
    four are NOT run through THIS pass's own gated conversion: three of
    them (`CriticDelta`'s arrays, `critic_suggested_replacement`) are
    simply added to `required` with their type UNCHANGED, since the full
    schema already accepts an empty `[]`/`""` for each -- no `minItems`/
    `minLength` floor. A field in `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`
    (which DOES have a floor, `minLength: 1`, with no escape) is handled
    differently still: fix round 3 gives it
    a null branch BEFORE this pass runs, via `project_output_schema_for_
    provider`'s `_make_issue_fields_nullable_in_place` step (see that
    constant's own comment for why those fields, uniquely, need a NEW null
    branch the full schema does not already have) -- so by the time THIS
    pass reaches it,
    `_already_permits_null` already answers True and it is folded into the
    ordinary already-nullable path above, no special case needed here.
    A property that was ALREADY required is left completely untouched (no
    nullable conversion, gated or otherwise) -- only `required`'s own list
    gets rebuilt, to `properties`' exact key order.

    Mutates `node` in place; same ownership contract as this module's other
    normalization passes (the caller hands this a deep copy it owns).
    """
    if isinstance(node, list):
        for item in node:
            _force_all_properties_required_in_place(item)
        return
    if not isinstance(node, dict):
        return
    properties = node.get("properties")
    if isinstance(properties, dict) and properties:
        already_required = set(node.get("required") or [])
        for name, prop_schema in properties.items():
            if name not in already_required and _already_permits_null(prop_schema):
                _make_nullable_in_place(prop_schema)
        node["required"] = list(properties.keys())
    _walk_schema_values(node, _force_all_properties_required_in_place)


def project_output_schema_for_provider(
    schema: dict[str, Any] | None = None,
    path: Path = OUTPUT_SCHEMA_PATH,
    notes_mode: str = "external",
) -> dict[str, Any]:
    """The provider-safe schema (issue #567) sent as the structured-output
    request field on BOTH first-class adapters -- Bedrock's
    `output_config.format.schema` and OpenRouter's
    `response_format.json_schema.schema` (`backend/src/model_client.py`).

    Built on top of `model_facing_output_schema(path)` (removes the two
    pipeline-stamped fields the model cannot honestly emit -- same
    reasoning as issue #418's tool-mode schema), with five further passes,
    in this order:

      1. `_break_recursive_refs_in_place` -- flattens a self-referencing
         `$ref` chain, if the schema ever grows one. Runs FIRST: originally
         (fix round 1, finding 2) so the node it substitutes still received
         `additionalProperties: false` from the next pass -- fix round 3
         made that node genuinely permissive instead (no `"type"`, so
         nothing to force), which changed WHY this must run first but not
         THAT it must: a later pass, `_make_nullable_in_place` (step 5),
         can rewrite a bare `{"$ref": ...}` property into `{"anyOf":
         [<original>, {"type": "null"}]}` -- and `_definition_ref_name`
         only recognizes an EXACT single-key `{"$ref": ...}` node as a
         traversable pointer, so a `$ref` this step has not yet flattened
         would stop being reachable at all once wrapped, silently leaving
         a real cycle unbroken.
      2. `_strip_unsupported_constraints_in_place` -- removes string/numeric
         constraint keywords the provider validators reject, and forces
         `additionalProperties: false` everywhere an object-shaped node
         is missing or loosens it. The node step 1 substitutes is
         genuinely permissive (no `"type"`, no `"properties"`), so this
         step has nothing to force onto it -- see
         `_break_recursive_refs_in_place`'s own docstring (fix round 3,
         finding 3) for why that is the correct outcome, not a gap.
      3. `_convert_one_of_to_any_of_in_place` -- rewrites every `oneOf` to
         `anyOf` (fix round 2, finding 3: OpenAI-strict-mode-shaped
         validators support the latter, not the former). Runs BEFORE step
         5 so that step's own null-branch bookkeeping only ever has to
         recognize `anyOf`.
      4. `_make_issue_fields_nullable_in_place` -- gives every field in
         `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH` a NEW `null` branch (fix
         round 3, finding 1: dropping such a field, as fix round 2 did,
         leaves every issue on a structured-outputs-capable model unable to
         carry it at all -- see that constant's own comment). Runs
         BEFORE step 5 so that step's `_already_permits_null` gate sees the
         null branch this step just added and simply confirms it, rather
         than needing its own special case.
      5. `_force_all_properties_required_in_place` -- forces
         `required == list(properties)` on every object-shaped node,
         converting a genuinely optional property to a nullable union
         ONLY where the full schema already permits `null` there (fix
         round 1, finding 1; gated per fix round 2, finding 1 -- see
         `_already_permits_null`) -- required LAST so it sees every
         object-shaped node the prior steps could still add or reshape
         (the flattened recursive-`$ref` substitution has no `properties`
         of its own, so this step is a no-op there).

    Finally, `_NON_SCHEMA_ROOT_KEYWORDS` (`$schema` / `$id` /
    `output_contract_version` -- fix round 2, finding 3) are popped off the
    projected schema's root.

    This is PROJECTION ONLY, exactly like `model_facing_output_schema`: the
    pipeline's actual acceptance criterion is unchanged.
    `primary_review_pass.validate_model_response` still runs the full,
    unmodified ACTIVE artifact (via `load_output_schema`)
    against the parsed response post-hoc -- this function changes only what
    the provider is ASKED to enforce, never what is ACCEPTED. A response
    that satisfies this looser projected schema but violates a stripped
    constraint (e.g. an over-length `section_ref`) still fails
    `validate_model_response`'s post-hoc check exactly as it does today --
    but (fix round 2) "looser" now means "requests fewer/broader things",
    never "accepts a value the full schema rejects": every value this
    projected schema can make a provider emit is also a value the FULL
    schema accepts (via `_already_permits_null`), OR is normalized into one
    before the full-schema check runs (fix round 3's
    `_denullify_unrepresentable_issue_fields` in `primary_review_pass.py`,
    for `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`) -- never silently
    accepted by the projection and then thrown away by validation, which
    was the case fix round 2 left unresolved.

    `schema` (default None): inject an already-built schema dict directly
    instead of deriving it from `path` -- used by this module's own tests
    to exercise the constraint-stripping and cycle-breaking walkers against
    small synthetic schemas that do not exist as fixture files on disk (in
    particular, a schema WITH a recursive `$ref`, which
    output-schema-v2.json does not have). `path` is ignored when `schema`
    is given. A real caller passes neither and gets
    `model_facing_output_schema(path)` as the starting point.

    `notes_mode` (issue #522, default `"external"`) is forwarded to
    `model_facing_output_schema` and does nothing else here -- see that
    function's docstring. It is ignored when `schema` is given, exactly
    like `path`, since an injected schema has already been projected.
    `internal_rationale_for_footnote` therefore reaches this function's
    passes only in `internal`/`both`, where
    `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH` gives it the null branch a
    strict-mode provider needs to say "no internal note on this issue", and
    `primary_review_pass._denullify_unrepresentable_issue_fields`
    strips that null back to absent before the full-schema check.

    Returns a fresh dict this call owns -- neither the source file's cached
    read (`model_facing_output_schema` never caches either) nor the
    caller-supplied `schema` argument is mutated.
    """
    base = (
        schema
        if schema is not None
        else model_facing_output_schema(path, notes_mode=notes_mode)
    )
    projected: dict[str, Any] = json.loads(json.dumps(base))
    _break_recursive_refs_in_place(projected)
    _strip_unsupported_constraints_in_place(projected)
    _convert_one_of_to_any_of_in_place(projected)
    _make_issue_fields_nullable_in_place(projected)
    _force_all_properties_required_in_place(projected)
    for key in _NON_SCHEMA_ROOT_KEYWORDS:
        projected.pop(key, None)
    return projected
