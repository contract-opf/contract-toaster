#!/usr/bin/env python3
"""
Model-facing output schema projection (issue #418) and provider-safe schema
projection (issue #567).

`playbooks/output-schema-v2.json` is the pipeline's FULL validation
contract for a model response -- but two of its required fields are not
something the model can honestly answer:

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
(b) `Issue.source_quote`, the one optional property with neither a null
branch NOR an emittable empty value (`minLength: 1`), was dropped from the
projected schema entirely rather than forced into `required` with no
honest value to give it. Fix round 2, finding 3 also rewrites every
`oneOf` this projection produces or preserves to `anyOf` (OpenAI-strict-
mode's supported-keyword subset has the latter, not the former) and strips
the non-JSON-Schema-validation root keywords (`$schema` / `$id` /
`output_contract_version`) the source file carries.

Fix round 3 REVERSED fix round 2's `source_quote` handling: since
#379/#380 retired the anchor-joined patch path, `source_quote` is the ONLY
way a REQUEST_CHANGE issue locates its redline target, so dropping it
meant every issue on a structured-outputs-capable model shipped zero
redlines. `source_quote` is now given a NEW `null` branch instead (`_make_
issue_fields_nullable_in_place` / `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_
BRANCH`) -- a real, emittable "no value" -- paired with a post-hoc
normalization in `primary_review_pass.py::_denullify_unrepresentable_
issue_fields` that strips a `null`/empty `source_quote` back to ABSENT
(a value the full schema already treats identically) before the full
schema check runs. Fix round 3 also corrected `_break_recursive_refs_
in_place`'s flattened substitution node, which had been getting
`additionalProperties: false` forced onto it with no `properties` to
match -- accepting only `{}` rather than the "genuinely permissive" node
its own docstring claimed.

This is a SEPARATE projection from `model_facing_output_schema` (built ON
TOP of it -- the model still cannot honestly emit `schema_version` /
`provenance` under provider enforcement either), never a replacement: the
model-facing tool-mode schema (#418) and the provider-safe schema (#567)
are two independent request-shaping seams that happen to share the same
stamped-field starting point. Same PROJECTION-ONLY discipline applies
throughout: the full, unmodified `output-schema-v2.json` still governs
post-hoc validation in `primary_review_pass.validate_model_response`
regardless of which (if either) projection a given request used --
`_denullify_unrepresentable_issue_fields` above is what keeps that true
now that the projection can emit a value (`source_quote: null`) the full
schema does not itself accept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v2.json"

# Pipeline-stamped fields the model must not be asked to produce -- see the
# module docstring. Kept as their own named tuples (rather than one shared
# list) so a future stamped field can be added to just the level it applies
# to without disturbing the other.
_TOP_LEVEL_STAMPED_FIELDS = ("schema_version",)
_ISSUE_STAMPED_FIELDS = ("provenance",)


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


def model_facing_output_schema(path: Path = OUTPUT_SCHEMA_PATH) -> dict[str, Any]:
    """The projected JSON Schema sent as the forced tool's `parameters`
    under structured output (issue #418): `output-schema-v2.json` with the
    pipeline-stamped fields removed from every `required` list AND every
    `properties` dict they appear in --

      - top-level `schema_version` (removed from the document root).
      - `provenance` on the shared `definitions.Issue` schema -- reached
        from BOTH the top-level `issues` array and
        `critic_delta.added_issues`, since both `$ref` the identical
        definition, so one removal covers both.

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
        issue_def["required"] = _strip_stamped_fields(
            issue_properties, issue_required, _ISSUE_STAMPED_FIELDS
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
_UNSUPPORTED_CONSTRAINT_KEYWORDS = (
    _UNSUPPORTED_STRING_CONSTRAINT_KEYWORDS + _UNSUPPORTED_NUMERIC_CONSTRAINT_KEYWORDS
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

# Optional `definitions.Issue` property (fix round 2, finding 1; REVISED fix
# round 3, finding 1) whose FULL schema definition
# (`playbooks/output-schema-v2.json`) offers NO value a provider-enforced
# request could honestly emit for "no value": `minLength: 1` (an empty
# string is rejected) and no `null`/`oneOf`/`anyOf` branch (see that file's
# `definitions.Issue.properties.source_quote`). Every OTHER property
# `_force_all_properties_required_in_place` newly adds to `required` -- the
# three `CriticDelta` arrays and `contested_replacements.items.
# critic_suggested_replacement` -- has an emittable "no value" the full
# schema already accepts (`[]` / `""`, no `minItems`/`minLength` floor), so
# those are simply added to `required` with their type untouched (see
# `_already_permits_null`).
#
# Fix round 2 dropped `source_quote` from the projected schema entirely to
# sidestep this. Fix round 3 found that unacceptable: since #379/#380
# retired the anchor-joined patch path, `source_quote` is the ONLY way a
# REQUEST_CHANGE issue locates its target for redline patching
# (`scripts/review_spine.py`, `scripts/redline_quote_apply.py`) -- a
# schema-enforced call could never carry one, so EVERY issue on EVERY
# structured-outputs-capable model (all six `model-policy/openrouter.json`
# `selectable` entries, plus Bedrock's pinned primary/critic) would route
# to `MANUAL_REVIEW_REQUIRED` with `docx_bytes=None`: zero redlines
# produced, silently, on the very capability this ticket hardens.
#
# The fix instead makes `source_quote` NULLABLE in the projection (a real,
# emittable "no value" a strict-mode provider can send), and pairs it with
# a normalization in `primary_review_pass.py::_denullify_unrepresentable_
# issue_fields` that strips a `null` (or empty-string) `source_quote` back
# to ABSENT before the full-schema check runs -- the full schema already
# treats "absent" and "no locatable quote" identically (issue #376's
# original design), so this loses no information the model actually
# conveyed; it only reshapes "I have none" into the form both schemas
# agree on. Adding a `null` branch to `output-schema-v2.json` itself was
# considered and rejected: that changes the pipeline's single validation
# source of truth and, per that file's own top-level description and
# docs/output-contract.md, requires a new `release.output_contract_hash`
# plus legal-governance review -- out of scope for this projection-only
# module, and unnecessary once the post-hoc normalization exists. NOT the
# same category as `_TOP_LEVEL_STAMPED_FIELDS`/`_ISSUE_STAMPED_FIELDS`
# above (pipeline-owned metadata the model was never asked to produce, on
# EITHER projection): `source_quote` is a model judgment both the
# non-enforced fallback path (`model_facing_output_schema`, #418) and the
# strict provider projection below now request and can both receive.
_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH = ("source_quote",)


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
    this UNCONDITIONALLY on `source_quote` specifically -- a genuine, live
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
    whether the FULL schema (`playbooks/output-schema-v2.json`) offered a
    `null` branch for that property at all -- widening 5 fields
    (`Issue.source_quote`, `CriticDelta.{added_issues,
    contested_replacements,rationale_objections}`,
    `CriticDelta.contested_replacements.items.
    critic_suggested_replacement`) to accept a value (`null`) the FULL
    schema outright rejects, so a strict-mode-compliant response -- which
    MUST emit every required key with SOME value -- became exactly the
    response the pipeline's own post-hoc validation throws away. This
    predicate gates that call: only a property the full schema ALREADY
    makes nullable may be (redundantly, idempotently) run through
    `_make_nullable_in_place`; every other newly-required property is left
    with its ORIGINAL type -- safe because output-schema-v2.json places no
    `minItems`/`minLength` floor on the four array/string properties above
    (an empty `[]`/`""` is a value BOTH schemas accept). `source_quote`
    (which DOES have a `minLength: 1` floor with no null escape in the
    FULL schema) is handled differently, not by this predicate: fix round
    3 gives it a NEW null branch BEFORE this pass runs, via
    `_make_issue_fields_nullable_in_place` -- so by the time THIS
    predicate checks it, the null branch already exists (added moments
    ago, not inherited from the full schema) and `_already_permits_null`
    correctly answers True, folding it into the same idempotent-
    confirmation path as a property the full schema made nullable itself.
    See `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`'s docstring for why
    `source_quote` needs this special upstream step at all: unlike the
    four properties above, it has no full-schema-accepted "empty" value to
    fall back to un-widened.
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
    `critic_delta`, `verdict_summary`), `definitions.Issue`
    (`source_quote`), `definitions.CriticDelta` (`added_issues`,
    `contested_replacements`, `rationale_objections`), and
    `definitions.CriticDelta.properties.contested_replacements.items`
    (`critic_suggested_replacement`) -- a live admin-selectable
    `structured_outputs: true` model (model-policy/openrouter.json's
    `selectable` allowlist) would get a 400 on every request built from
    the unfixed schema.

    Fix round 2, finding 1: fix round 1's own remedy above turned out to
    still be LOOSER than the full schema, not merely differently-shaped --
    unconditionally nullifying every newly-required property let a strict-
    mode-compliant model emit `null` for `source_quote` / the three
    `CriticDelta` arrays / `critic_suggested_replacement`, a value the FULL
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
    `minLength` floor. `source_quote` (which DOES have a floor, `minLength:
    1`, with no escape) is handled differently still: fix round 3 gives it
    a null branch BEFORE this pass runs, via `project_output_schema_for_
    provider`'s `_make_issue_fields_nullable_in_place` step (see
    `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`'s docstring for why this
    field, uniquely, needs a NEW null branch the full schema does not
    already have) -- so by the time THIS pass reaches it,
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
    schema: dict[str, Any] | None = None, path: Path = OUTPUT_SCHEMA_PATH
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
      4. `_make_issue_fields_nullable_in_place` -- gives `Issue.source_quote`
         a NEW `null` branch (fix round 3, finding 1: dropping it, as fix
         round 2 did, left every issue on a structured-outputs-capable
         model unable to carry the field redline generation depends on --
         see `_ISSUE_FIELDS_NEEDING_A_NEW_NULL_BRANCH`'s docstring). Runs
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
    unmodified `playbooks/output-schema-v2.json` (via `load_output_schema`)
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
    was the case fix round 2 left unresolved for `source_quote`.

    `schema` (default None): inject an already-built schema dict directly
    instead of deriving it from `path` -- used by this module's own tests
    to exercise the constraint-stripping and cycle-breaking walkers against
    small synthetic schemas that do not exist as fixture files on disk (in
    particular, a schema WITH a recursive `$ref`, which
    output-schema-v2.json does not have). `path` is ignored when `schema`
    is given. A real caller passes neither and gets
    `model_facing_output_schema(path)` as the starting point.

    Returns a fresh dict this call owns -- neither the source file's cached
    read (`model_facing_output_schema` never caches either) nor the
    caller-supplied `schema` argument is mutated.
    """
    base = schema if schema is not None else model_facing_output_schema(path)
    projected: dict[str, Any] = json.loads(json.dumps(base))
    _break_recursive_refs_in_place(projected)
    _strip_unsupported_constraints_in_place(projected)
    _convert_one_of_to_any_of_in_place(projected)
    _make_issue_fields_nullable_in_place(projected)
    _force_all_properties_required_in_place(projected)
    for key in _NON_SCHEMA_ROOT_KEYWORDS:
        projected.pop(key, None)
    return projected
