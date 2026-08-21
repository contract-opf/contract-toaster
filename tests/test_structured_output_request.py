#!/usr/bin/env python3
"""
Unit tests for issue #567: schema-enforced model output at the PROVIDER
layer on both first-class adapters (supersedes #418's env-flagged forced-
tool-use, closed separately in the landing comment).

## What is asserted here (mirrors the issue's acceptance criteria)

  1. `scripts/model_output_schema.py::project_output_schema_for_provider`:
     built on top of `model_facing_output_schema` (stamped fields --
     `schema_version` / per-issue `provenance` -- still dropped); every
     string/numeric constraint keyword in the documented unsupported set
     (`minLength`/`maxLength`/`pattern`/`format`/`minimum`/`maximum`/
     `exclusiveMinimum`/`exclusiveMaximum`/`multipleOf`) is stripped
     everywhere it appears in output-schema-v2.json; `additionalProperties`
     stays `false` on every object-shaped node in the (already-compliant)
     real schema, and is FORCED to `false` on a synthetic schema missing
     it (output-schema-v2.json has no such gap today -- the normalization
     is defensive, exercised via a hand-built schema); a recursive `$ref`
     chain (exercised via a synthetic schema -- output-schema-v2.json has
     none) is flattened at the cycle point; the function is PROJECTION
     ONLY -- the source file on disk, and `model_facing_output_schema`'s own
     output, are both untouched.
  2. `LiveBedrockModelClient.invoke`: with a capability-True model_id
     (model-policy/bedrock-us-east-1.json's pinned primary/critic), the
     built payload carries `output_config.format.schema == output_schema`;
     with a capability-False model_id, the payload is BYTE-IDENTICAL to a
     call that never passed `output_schema` at all (test both).
  3. `OpenRouterModelClient.invoke`: with a capability-True model_id
     (a `selectable` entry), the request body carries
     `response_format.json_schema.strict == True` and
     `response_format.json_schema.schema == output_schema`; with a
     capability-False model_id (the policy-pinned primary/critic, which
     openrouter.json declares neither capability field for), the body is
     byte-identical to a call that never passed `output_schema`.
  4. Protocol signature parity: `FakeBedrockClient.invoke` accepts
     `output_schema` and records it on `self.calls` (recorded, not acted
     on -- capability-gate behavior is asserted against the REAL clients
     per the fixture-fidelity doctrine); neither client explodes on
     `output_schema=None`.
  5. Threading (`scripts/primary_review_pass.py::run_primary_pass` /
     `scripts/critic_review_pass.py::run_critic_pass`): `output_schema` is
     resolved from the SAME `model_capabilities` #562 already plumbs, and
     passed to `model_client.invoke()` ONLY when the capability is True --
     proved against a pre-#567-shaped fake whose `invoke()` has no
     `output_schema` parameter at all, so a capability-less run against it
     still succeeds. The returned result dict AND the ledgered
     `ModelInvocationRecord` both carry `schema_enforcement_requested`
     matching what was actually resolved.
  6. `scripts/review_spine.py::run_review`'s result dict carries
     `primary_schema_enforcement_requested` / `critic_schema_enforcement_
     requested`, read straight off each pass's own result -- asserted in
     `tests/test_review_spine.py` (Part 4), NOT duplicated here: that file
     already owns the docx-fixture harness `run_review` needs, and per
     this codebase's per-file test-process isolation
     (`scripts/check.sh`'s own docstring), test files must not import one
     another's fixtures.
  7. Post-hoc fail-closed: a canned response that satisfies the LOOSER
     projected schema (e.g. an over-length `section_ref`, an uppercase
     `playbook_topic_id`) but violates the FULL v2 schema still fails
     `validate_model_response` -- server-side enforcement is belt, local
     validation stays braces, exactly as documented.
  8. July-incident regression stays covered even when schema enforcement
     was requested: a prose-wrapped, `schema_version`-less OpenRouter
     response still round-trips through `_extract_json_object` /
     `validate_model_response` on the SAME `output_schema`-carrying call --
     the provider request field changes what is ASKED for, never disables
     the extractor/stamping fallback path.

Fully offline: policy JSON read straight off disk, injected fake HTTP /
bedrock-runtime transports stand in for httpx / boto3.

Run: python3 tests/test_structured_output_request.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
MODEL_RESPONSES_DIR = REPO_ROOT / "tests" / "fixtures" / "model_responses"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

for _dir in (BACKEND_SRC, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import jsonschema  # noqa: E402
import model_client as mc  # noqa: E402
import model_output_schema as mos  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import critic_review_pass as cp  # noqa: E402

# model-policy/bedrock-us-east-1.json: both True/True.
BEDROCK_PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
# model-policy/bedrock-us-east-1.json: declares neither field -> all-False.
BEDROCK_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"

# model-policy/openrouter.json: pinned primary/critic declare NEITHER
# capability field -> all-False.
OPENROUTER_PRIMARY_MODEL_ID = "anthropic/claude-opus-4.8"
OPENROUTER_CRITIC_MODEL_ID = "anthropic/claude-sonnet-4.6"
# model-policy/openrouter.json `selectable`: structured_outputs true.
OPENROUTER_SELECTABLE_MODEL_ID = "anthropic/claude-opus-5"

_PRIMARY_VALID_FIXTURE = "primary_request_change_valid.json"


def _load_fixture(name: str) -> dict[str, Any]:
    with open(MODEL_RESPONSES_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _unstamp(parsed: dict[str, Any]) -> dict[str, Any]:
    unstamped = json.loads(json.dumps(parsed))  # deep copy, stdlib-only
    unstamped.pop("schema_version", None)
    for issue in unstamped.get("issues") or []:
        issue.pop("provenance", None)
    critic_delta = unstamped.get("critic_delta")
    if isinstance(critic_delta, dict):
        for added in critic_delta.get("added_issues") or []:
            added.pop("provenance", None)
    return unstamped


def _fill_absent_source_quote_with_null(obj: dict[str, Any]) -> dict[str, Any]:
    """Issue #567 fix round 3, finding 1: `project_output_schema_for_
    provider()` now KEEPS `source_quote` and REQUIRES it present (with a
    `null` branch -- see `model_output_schema.py::_ISSUE_FIELDS_NEEDING_
    A_NEW_NULL_BRANCH`) rather than dropping it, because it is the ONLY way
    a REQUEST_CHANGE issue locates its redline target -- fix round 2's
    "drop it" remedy meant every issue on a structured-outputs-capable
    model shipped zero redlines (see that constant's docstring for the
    full incident). This test-local helper builds, on a COPY, the document
    a provider enforcing the PROJECTED schema can actually produce: every
    Issue missing `source_quote` gets an explicit `null` filled in -- a
    strict-mode provider cannot OMIT a required key, even to say "none",
    the way the fallback (non-enforced) prompt path can. A no-op wherever
    the fixture already carries a real value (present on-disk in exactly
    one fixture, `primary_request_change_with_source_quote_valid.json`).

    Fix round 3's companion piece, `primary_review_pass.py::
    _denullify_unrepresentable_issue_fields`, is what makes a `null` this
    helper fills back in survive the FULL schema check too (strips it back
    to absent) -- see `TestProjectedSchemaRoundTripsThroughFullValidation`
    below, which feeds every fixture through this helper, the projected
    schema, and THEN `validate_model_response`'s full-schema path, on the
    same instance, closing that loop.

    Replaces fix round 2's `_strip_provider_unrepresentable_fields` (name
    and direction both wrong once source_quote stopped being dropped) and,
    before that, fix round 1's `_fill_strict_mode_nulls` (which validated
    its filled document ONLY against the projected schema, never round-
    tripping it back through `validate_model_response` -- exactly how that
    fix missed the incompatibility fix round 2 caught, and precisely the
    gap `TestProjectedSchemaRoundTripsThroughFullValidation` exists to
    close for good)."""
    filled = json.loads(json.dumps(obj))  # deep copy, stdlib-only
    for issue in filled.get("issues") or []:
        issue.setdefault("source_quote", None)
    critic_delta = filled.get("critic_delta")
    if isinstance(critic_delta, dict):
        for added in critic_delta.get("added_issues") or []:
            added.setdefault("source_quote", None)
    return filled


# ---------------------------------------------------------------------------
# 1. project_output_schema_for_provider
# ---------------------------------------------------------------------------


class TestProjectOutputSchemaForProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = mos.project_output_schema_for_provider()

    def test_is_a_valid_json_schema(self) -> None:
        jsonschema.Draft7Validator.check_schema(self.schema)

    def test_stamped_fields_still_dropped(self) -> None:
        self.assertNotIn("schema_version", self.schema["properties"])
        self.assertNotIn("provenance", self.schema["definitions"]["Issue"]["properties"])
        # Fix round 1, finding 1: `required` now enumerates EVERY root
        # property (not just the ones that were already mandatory) --
        # `confidence_band`/`critic_delta`/`verdict_summary` joined
        # `decision`/`confidence_state`/`issues` once
        # `_force_all_properties_required_in_place` landed. `schema_version`
        # stays excluded (both from `properties`, asserted above, and thus
        # from `required`) -- the stamped-field removal this test's name
        # refers to.
        self.assertEqual(
            set(self.schema["required"]),
            {
                "decision",
                "confidence_state",
                "confidence_band",
                "issues",
                "critic_delta",
                "verdict_summary",
            },
        )

    def _find_keys(self, node: Any, bad: set[str], path: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            found += [f"{path}/{k}" for k in node if k in bad]
            for k, v in node.items():
                found += self._find_keys(v, bad, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                found += self._find_keys(v, bad, f"{path}[{i}]")
        return found

    def test_no_unsupported_string_or_numeric_constraint_survives(self) -> None:
        bad = self._find_keys(self.schema, set(mos._UNSUPPORTED_CONSTRAINT_KEYWORDS))
        self.assertEqual(bad, [], f"unsupported constraint keywords survived at: {bad}")

    def _find_required_gaps(self, node: Any, path: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict) and properties:
                required = set(node.get("required") or [])
                missing = sorted(set(properties.keys()) - required)
                found += [f"{path}: {name}" for name in missing]
            for k, v in node.items():
                found += self._find_required_gaps(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                found += self._find_required_gaps(v, f"{path}[{i}]")
        return found

    def test_required_enumerates_every_property_everywhere(self) -> None:
        # Issue #567 fix round 1, finding 1: `model_client.py`'s OpenRouter
        # adapter sends `"strict": True` (AC2) -- OpenAI-strict-mode-shaped
        # validators reject any object-shaped node whose `required` omits a
        # name present in `properties`. Before this fix, 4 nodes violated
        # this (root, definitions/Issue, definitions/CriticDelta,
        # definitions/CriticDelta/properties/contested_replacements/items) --
        # a capability-True selectable model (e.g. one declaring
        # `structured_outputs: true` in model-policy/openrouter.json's
        # `selectable` allowlist) got a 400 on every review request built
        # from the unfixed schema.
        gaps = self._find_required_gaps(self.schema)
        self.assertEqual(gaps, [], f"`required` omits a property at: {gaps}")

    def test_source_quote_kept_and_forced_nullable(self) -> None:
        # Issue #567 fix round 3, finding 1: fix round 2 DROPPED
        # `source_quote` from the projected schema (the FULL schema's
        # `minLength: 1` with no `null`/`anyOf` branch left no honest value
        # to force it into `required` with) -- but since #379/#380 retired
        # the anchor-joined patch path, `source_quote` is the ONLY way a
        # REQUEST_CHANGE issue locates its redline target, so dropping it
        # meant every issue on a structured-outputs-capable model shipped
        # zero redlines. It is kept and given a NEW `null` branch instead
        # (see `model_output_schema.py::_ISSUE_FIELDS_NEEDING_A_NEW_NULL_
        # BRANCH`), a real value a strict-mode provider can honestly emit
        # for "no quote" -- paired with a post-hoc normalization
        # (`primary_review_pass._denullify_unrepresentable_issue_fields`)
        # that strips it back to absent before the FULL schema ever sees
        # it.
        issue_props = self.schema["definitions"]["Issue"]["properties"]
        self.assertIn("source_quote", issue_props)
        self.assertIn("source_quote", self.schema["definitions"]["Issue"]["required"])
        self.assertEqual(issue_props["source_quote"]["type"], ["string", "null"])

    def test_arrays_and_critic_suggested_replacement_required_without_nullable_union(
        self,
    ) -> None:
        # Issue #567 fix round 2, finding 1: the three CriticDelta arrays
        # and `critic_suggested_replacement` ARE safely representable as
        # required -- the full schema places no `minItems`/`minLength`
        # floor on any of them, so `[]`/`""` already satisfies it -- but
        # must be added to `required` WITH THEIR TYPE UNCHANGED, never
        # widened to accept `null` (which the full schema, with no
        # `anyOf`/`oneOf` branch on any of the four, rejects outright).
        critic_delta_props = self.schema["definitions"]["CriticDelta"]["properties"]
        for name in ("added_issues", "contested_replacements", "rationale_objections"):
            self.assertEqual(critic_delta_props[name]["type"], "array")
        self.assertEqual(
            set(self.schema["definitions"]["CriticDelta"]["required"]),
            {"added_issues", "contested_replacements", "rationale_objections"},
        )

        contested_item = critic_delta_props["contested_replacements"]["items"]
        self.assertEqual(
            contested_item["properties"]["critic_suggested_replacement"]["type"], "string"
        )
        self.assertIn("critic_suggested_replacement", contested_item["required"])
        # A field that was ALREADY required (e.g. section_ref) must stay
        # exactly as-is -- no null added to a field that was never optional.
        self.assertEqual(contested_item["properties"]["section_ref"]["type"], "string")

    def test_root_optional_fields_stay_nullable_via_any_of(self) -> None:
        # Root: confidence_band/critic_delta/verdict_summary were already
        # nullable via `oneOf` (they carried a `{"type": "null"}` branch
        # before this fix too), rewritten to `anyOf` by fix round 2, finding
        # 3 (`_convert_one_of_to_any_of_in_place`) -- becoming required must
        # not double up or otherwise disturb that existing union.
        for name in ("confidence_band", "critic_delta", "verdict_summary"):
            any_of = self.schema["properties"][name]["anyOf"]
            null_branches = [b for b in any_of if b.get("type") == "null"]
            self.assertEqual(len(null_branches), 1, f"{name}: {any_of}")

    def test_no_one_of_key_survives_anywhere(self) -> None:
        # Issue #567 fix round 2, finding 3: OpenAI-strict-mode-shaped
        # validators support `anyOf`, not `oneOf` -- every `oneOf`
        # output-schema-v2.json carries (root `confidence_band`/
        # `critic_delta`/`verdict_summary`, `definitions.Issue.properties.
        # internal_precedent_citation`) must come out the other side as
        # `anyOf`, and `_make_nullable_in_place` must never reintroduce one.
        bad = self._find_keys(self.schema, {"oneOf"})
        self.assertEqual(bad, [], f"oneOf survived at: {bad}")

    def test_non_schema_root_keywords_stripped(self) -> None:
        # Issue #567 fix round 2, finding 3: `$schema`/`$id` (meaningless
        # once embedded as an inline `schema` value in a provider request
        # body) and `output_contract_version` (this repo's own
        # release-tracking metadata, not a jsonschema keyword at all) must
        # not ride along into the provider-facing request.
        for key in mos._NON_SCHEMA_ROOT_KEYWORDS:
            self.assertNotIn(key, self.schema)

    def test_source_schema_actually_had_these_constraints(self) -> None:
        # Negative-control: prove the stripping did something, not that the
        # source never had the keywords to begin with.
        with open(mos.OUTPUT_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertIn("minLength", on_disk["definitions"]["Issue"]["properties"]["section_ref"])
        self.assertIn("maxLength", on_disk["definitions"]["Issue"]["properties"]["section_ref"])
        self.assertIn("pattern", on_disk["definitions"]["Issue"]["properties"]["playbook_topic_id"])

    def test_additional_properties_stays_false_on_every_object(self) -> None:
        # output-schema-v2.json already declares additionalProperties:
        # false on every object-shaped node -- this is a no-op
        # re-confirmation for the real schema; the DEFENSIVE "was missing,
        # now forced" behavior is proven against a synthetic schema in
        # TestAdditionalPropertiesForcedFalse below, since nothing in the
        # real file currently omits it.
        self.assertEqual(self.schema["additionalProperties"], False)
        self.assertEqual(
            self.schema["definitions"]["Issue"]["additionalProperties"], False
        )
        self.assertEqual(
            self.schema["definitions"]["CriticDelta"]["additionalProperties"], False
        )

    def test_source_schema_on_disk_is_untouched(self) -> None:
        second = mos.project_output_schema_for_provider()
        self.assertEqual(self.schema, second)
        with open(mos.OUTPUT_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertIn("schema_version", on_disk["required"])
        self.assertIn("minLength", on_disk["definitions"]["Issue"]["properties"]["section_ref"])

    def test_model_facing_output_schema_output_is_untouched(self) -> None:
        # project_output_schema_for_provider must deep-copy its base, never
        # mutate the dict model_facing_output_schema() itself returns.
        base_before = mos.model_facing_output_schema()
        mos.project_output_schema_for_provider()
        base_after = mos.model_facing_output_schema()
        self.assertEqual(base_before, base_after)
        self.assertIn(
            "minLength", base_after["definitions"]["Issue"]["properties"]["section_ref"]
        )

    def test_every_valid_fixture_accepted_once_unstamped(self) -> None:
        valid_fixtures = sorted(MODEL_RESPONSES_DIR.glob("*_valid.json"))
        self.assertGreater(len(valid_fixtures), 0)
        for path in valid_fixtures:
            with self.subTest(fixture=path.name):
                parsed = json.loads(path.read_text(encoding="utf-8"))
                unstamped = _unstamp(parsed)
                # Fix round 3, finding 1: `source_quote` is now REQUIRED
                # (nullable) on the projected schema -- fill it with `null`
                # on every issue that omits it, since a strict-mode
                # provider cannot omit a required key (see
                # _fill_absent_source_quote_with_null).
                candidate = _fill_absent_source_quote_with_null(unstamped)
                jsonschema.validate(instance=candidate, schema=self.schema)


class TestAdditionalPropertiesForcedFalse(unittest.TestCase):
    """output-schema-v2.json already declares `additionalProperties: false`
    on every object-shaped node -- this normalization has no live gap to
    close in the real file today. Proven here against synthetic schemas
    that DO have the gap, so the defensive behavior is not just assumed."""

    def test_missing_additional_properties_is_forced_false(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            # additionalProperties deliberately absent.
        }
        projected = mos.project_output_schema_for_provider(schema=schema)
        self.assertEqual(projected["additionalProperties"], False)

    def test_explicit_true_is_overridden_to_false(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": True,
        }
        projected = mos.project_output_schema_for_provider(schema=schema)
        self.assertEqual(projected["additionalProperties"], False)

    def test_nested_definition_missing_it_is_also_forced(self) -> None:
        schema = {
            "type": "object",
            "properties": {"item": {"$ref": "#/definitions/Item"}},
            "definitions": {
                "Item": {"type": "object", "properties": {"value": {"type": "string"}}}
            },
        }
        projected = mos.project_output_schema_for_provider(schema=schema)
        self.assertEqual(projected["definitions"]["Item"]["additionalProperties"], False)


class TestRecursiveRefFlattening(unittest.TestCase):
    """output-schema-v2.json has no recursive $ref -- exercised here via a
    synthetic schema so the cycle-breaking walker is actually proven, not
    just assumed to be dead code."""

    def _synthetic_recursive_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["schema_version", "root"],
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "string", "const": "v1"},
                "root": {"$ref": "#/definitions/Node"},
            },
            "definitions": {
                "Node": {
                    "type": "object",
                    # Fully `required` (issue #567 fix round 1, finding 1:
                    # `project_output_schema_for_provider` now forces
                    # `required == properties` everywhere) so this test
                    # schema exercises ONLY the ref-flattening behavior --
                    # neither property here is genuinely optional.
                    "required": ["value", "child"],
                    "properties": {
                        "value": {"type": "string", "minLength": 1},
                        "child": {"$ref": "#/definitions/Node"},
                    },
                }
            },
        }

    def test_self_referencing_ref_is_flattened_not_infinite(self) -> None:
        projected = mos.project_output_schema_for_provider(
            schema=self._synthetic_recursive_schema()
        )
        child = projected["definitions"]["Node"]["properties"]["child"]
        self.assertNotIn("$ref", child)
        # Fix round 3, finding 3: the flattened node carries NO "type" key
        # (see test_flattened_result_is_genuinely_permissive below for why)
        # -- it is a bare `{}`-shaped schema modulo `description`.
        self.assertNotIn("type", child)

    def test_flattened_result_is_still_a_valid_schema(self) -> None:
        projected = mos.project_output_schema_for_provider(
            schema=self._synthetic_recursive_schema()
        )
        jsonschema.Draft7Validator.check_schema(projected)

    def test_flattened_result_is_genuinely_permissive(self) -> None:
        # Fix round 3, finding 3: the flattened node USED TO be substituted
        # as `{"type": "object", "description": ...}`, which reads as
        # permissive but is not -- `_strip_unsupported_constraints_in_place`
        # (the very next pass) forces `additionalProperties: false` onto
        # any node with `"type": "object"`, and a node with no `properties`
        # plus `additionalProperties: false` accepts ONLY the empty object
        # `{}`, not an actual instance of the flattened definition. Proven
        # here, not just asserted: an instance carrying the recursive
        # shape's own fields (`value` + a nested `child`) must validate
        # against the flattened projected schema, since the FULL (real
        # `$ref`) schema is what actually governs post-hoc acceptance --
        # the projected schema only needs to not reject a REQUEST at the
        # provider layer.
        projected = mos.project_output_schema_for_provider(
            schema=self._synthetic_recursive_schema()
        )
        instance = {
            "schema_version": "v1",
            "root": {"value": "x", "child": {"value": "y", "child": {"anything": "at all"}}},
        }
        jsonschema.validate(instance=instance, schema=projected)

    def test_non_recursive_ref_chain_is_left_alone(self) -> None:
        schema = {
            "type": "object",
            # Fully `required` at every level, same reasoning as
            # `_synthetic_recursive_schema` above -- isolates this test to
            # the ref-flattening behavior it actually names, independent of
            # finding 1's required-forcing (which would otherwise wrap the
            # bare `$ref` below into a nullable union and break the
            # byte-equality assertion this test makes on it).
            "required": ["a"],
            "properties": {"a": {"$ref": "#/definitions/A"}},
            "definitions": {
                "A": {
                    "type": "object",
                    "required": ["b"],
                    "properties": {"b": {"$ref": "#/definitions/B"}},
                },
                "B": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
            },
        }
        projected = mos.project_output_schema_for_provider(schema=schema)
        self.assertEqual(
            projected["definitions"]["A"]["properties"]["b"], {"$ref": "#/definitions/B"}
        )

    def test_mutual_recursion_between_two_definitions_is_caught(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"$ref": "#/definitions/A"}},
            "definitions": {
                "A": {"type": "object", "properties": {"b": {"$ref": "#/definitions/B"}}},
                "B": {"type": "object", "properties": {"a_again": {"$ref": "#/definitions/A"}}},
            },
        }
        projected = mos.project_output_schema_for_provider(schema=schema)
        a_again = projected["definitions"]["B"]["properties"]["a_again"]
        self.assertNotIn("$ref", a_again)


class TestStripDoesNotTreatPropertiesMapAsASchemaNode(unittest.TestCase):
    """Issue #567 fix round 1, finding 3: `_strip_unsupported_constraints_
    in_place` used to pop unsupported-keyword-named KEYS out of every dict
    it reached, including a `properties` MAP -- which it could not
    distinguish from a schema node. A property literally named `format` (or
    `pattern`, `minLength`, ...) is a PROPERTY NAME, not a schema keyword,
    and used to be silently deleted from `properties` while `required`
    still named it -- an unsatisfiable schema once `additionalProperties:
    false` is also forced (the model has no way to emit a required key with
    no definition)."""

    def test_property_named_format_survives_the_strip(self) -> None:
        schema = {
            "type": "object",
            "required": ["format", "pattern", "decision"],
            "properties": {
                "format": {"type": "string"},
                "pattern": {"type": "string"},
                "decision": {"type": "string", "enum": ["ACCEPT", "REQUEST_CHANGE"]},
            },
        }
        projected = mos.project_output_schema_for_provider(schema=schema)
        self.assertEqual(
            set(projected["properties"].keys()), {"format", "pattern", "decision"}
        )
        self.assertEqual(set(projected["required"]), {"format", "pattern", "decision"})
        # Not just "the keys survived" -- prove the schema is actually
        # SATISFIABLE (additionalProperties: false + every required name
        # still defined in properties).
        jsonschema.Draft7Validator.check_schema(projected)
        jsonschema.validate(
            instance={"format": "f", "pattern": "p", "decision": "ACCEPT"}, schema=projected
        )


class TestPostHocFailClosedDespiteLooserProjection(unittest.TestCase):
    """AC: a canned model output that violates the FULL v2 schema still
    fails closed through validate_model_response even when the projected
    (looser) schema would have accepted it."""

    def test_over_length_section_ref_satisfies_projection_but_fails_post_hoc(self) -> None:
        stamped = _load_fixture(_PRIMARY_VALID_FIXTURE)
        unstamped = _unstamp(stamped)
        # output-schema-v2.json caps section_ref at 200 chars; the projected
        # schema stripped maxLength entirely, so this instance validates
        # against the projection but must still fail the full schema.
        unstamped["issues"][0]["section_ref"] = "x" * 500
        projected = mos.project_output_schema_for_provider()
        # Fix round 3, finding 1: source_quote is required (nullable) on
        # the projection -- fill it in (a no-op value-wise here, this
        # fixture's issue never carries one; kept for consistency with
        # every other test in this file that builds a projection-
        # satisfying candidate; see _fill_absent_source_quote_with_null).
        candidate = _fill_absent_source_quote_with_null(unstamped)
        jsonschema.validate(instance=candidate, schema=projected)  # accepted by the loose schema

        raw = json.dumps(unstamped)
        ok, error = pp.validate_model_response(raw, issue_provenance="model")
        self.assertFalse(ok, "an over-length section_ref must still fail the full schema")
        self.assertIn("schema_invalid", error)

    def test_uppercase_topic_id_satisfies_projection_but_fails_post_hoc(self) -> None:
        stamped = _load_fixture(_PRIMARY_VALID_FIXTURE)
        unstamped = _unstamp(stamped)
        # The `pattern` constraint (rejects uppercase) was stripped from the
        # projection but is still enforced by the full schema.
        unstamped["issues"][0]["playbook_topic_id"] = "UPPERCASE-NOT-ALLOWED"
        projected = mos.project_output_schema_for_provider()
        candidate = _fill_absent_source_quote_with_null(unstamped)
        jsonschema.validate(instance=candidate, schema=projected)

        raw = json.dumps(unstamped)
        ok, error = pp.validate_model_response(raw, issue_provenance="model")
        self.assertFalse(ok)
        self.assertIn("schema_invalid", error)


class TestProjectedSchemaRoundTripsThroughFullValidation(unittest.TestCase):
    """Issue #567 fix round 2, finding 2 (STILL enforced after fix round
    3's reversal of the source_quote-dropping remedy): fix round 1's
    `_fill_strict_mode_nulls` validated a strict-mode-filled document ONLY
    against the projected schema, never fed that same document back
    through `pp.validate_model_response` (the FULL schema) -- so it never
    caught that a manufactured `null` can be a value output-schema-v2.json
    itself rejects. `TestPostHocFailClosedDespiteLooserProjection`'s two
    cases DELIBERATELY round-trip the wrong document for the projection
    half (the over-length/uppercase `unstamped`, not `filled`) -- proving
    fail-CLOSED, not fail-open -- so they cannot substitute for this.

    This class closes the loop the other direction: every document
    asserted to satisfy the PROJECTED schema is also asserted to satisfy
    `validate_model_response`'s FULL schema check, on the SAME instance.
    This is now the load-bearing proof for fix round 3's `source_quote`
    remedy specifically: `_fill_absent_source_quote_with_null` fills a
    `null` the projected schema requires and the FULL schema does NOT
    accept -- the only reason this class's round trip can still pass is
    `primary_review_pass.validate_model_response`'s own
    `_denullify_unrepresentable_issue_fields` step stripping that `null`
    back to absent before the full-schema check runs. Run against fix
    round 2's code (source_quote dropped, not nulled) this class's helper
    would need no fill at all for that field and the round trip would
    already pass, which is exactly why fix round 3, finding 1 was a review
    finding, not something this class alone would have caught -- the
    regression this class DOES catch is a `null` sent to the full schema
    unstripped: revert `_denullify_unrepresentable_issue_fields` to a
    no-op and `_assert_round_trips` fails at the `validate_model_response`
    step with `schema_invalid: None is not of type 'string' (at
    issues/0/source_quote)`.
    """

    def _assert_round_trips(self, parsed: dict[str, Any], *, issue_provenance: str) -> None:
        candidate = _fill_absent_source_quote_with_null(_unstamp(parsed))
        projected = mos.project_output_schema_for_provider()
        jsonschema.validate(instance=candidate, schema=projected)
        ok, error = pp.validate_model_response(
            json.dumps(candidate), issue_provenance=issue_provenance
        )
        self.assertTrue(ok, error)

    def test_every_valid_fixture_round_trips(self) -> None:
        valid_fixtures = sorted(MODEL_RESPONSES_DIR.glob("*_valid.json"))
        self.assertGreater(len(valid_fixtures), 0)
        for path in valid_fixtures:
            with self.subTest(fixture=path.name):
                parsed = json.loads(path.read_text(encoding="utf-8"))
                self._assert_round_trips(parsed, issue_provenance="model")

    def test_critic_shaped_response_with_populated_critic_delta_round_trips(self) -> None:
        # Explicit per the ticket's ask, even though this fixture is also
        # covered by the glob above: exercises ALL THREE previously-buggy
        # CriticDelta arrays (added_issues=[], rationale_objections=[], both
        # legitimately empty; contested_replacements populated) PLUS
        # critic_suggested_replacement with a genuinely non-empty value, in
        # one document.
        self._assert_round_trips(
            _load_fixture("critic_contested_replacement_valid.json"),
            issue_provenance="critic-added",
        )


# ---------------------------------------------------------------------------
# 2. LiveBedrockModelClient.invoke -- capability-gated output_config
# ---------------------------------------------------------------------------


class _FakeBedrockBody:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return json.dumps({"content": [{"text": self._text}]}).encode("utf-8")


class _FakeBedrockRuntime:
    def __init__(self, response_text: str = "ok") -> None:
        self.calls: list[dict[str, Any]] = []
        self._response_text = response_text

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"body": _FakeBedrockBody(self._response_text)}

    def last_body(self) -> dict[str, Any]:
        return json.loads(self.calls[-1]["body"])


class TestLiveBedrockInvokeOutputSchema(unittest.TestCase):
    def test_capability_true_model_carries_output_config(self) -> None:
        runtime = _FakeBedrockRuntime()
        client = mc.LiveBedrockModelClient(bedrock_runtime_client=runtime)
        schema = mos.project_output_schema_for_provider()
        client.invoke(
            model_id=BEDROCK_PRIMARY_MODEL_ID,
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=100,
            output_schema=schema,
        )
        body = runtime.last_body()
        self.assertEqual(
            body["output_config"], {"format": {"type": "json_schema", "schema": schema}}
        )

    def test_capability_false_model_payload_byte_identical_to_no_output_schema(self) -> None:
        schema = mos.project_output_schema_for_provider()
        runtime_a = _FakeBedrockRuntime()
        runtime_b = _FakeBedrockRuntime()
        mc.LiveBedrockModelClient(bedrock_runtime_client=runtime_a).invoke(
            model_id=BEDROCK_EMBEDDING_MODEL_ID,
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=100,
        )
        mc.LiveBedrockModelClient(bedrock_runtime_client=runtime_b).invoke(
            model_id=BEDROCK_EMBEDDING_MODEL_ID,
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=100,
            output_schema=schema,
        )
        body_a = runtime_a.last_body()
        body_b = runtime_b.last_body()
        self.assertEqual(body_a, body_b)
        self.assertNotIn("output_config", body_b)

    def test_output_schema_none_never_adds_output_config_even_for_a_capable_model(self) -> None:
        runtime = _FakeBedrockRuntime()
        client = mc.LiveBedrockModelClient(bedrock_runtime_client=runtime)
        client.invoke(
            model_id=BEDROCK_PRIMARY_MODEL_ID,
            system_prompt="SYS",
            user_prompt="USER",
            max_output_tokens=100,
        )
        self.assertNotIn("output_config", runtime.last_body())


# ---------------------------------------------------------------------------
# 3. OpenRouterModelClient.invoke -- capability-gated response_format
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002 - mirror httpx sig
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response

    def close(self) -> None:
        pass


def _content_response(content: str, finish_reason: str = "stop") -> FakeResponse:
    return FakeResponse(
        200,
        {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]},
    )


class TestOpenRouterInvokeOutputSchema(unittest.TestCase):
    def _client(self, http: FakeHttpClient) -> mc.OpenRouterModelClient:
        return mc.OpenRouterModelClient(
            api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
        )

    def test_capability_true_model_carries_response_format(self) -> None:
        schema = mos.project_output_schema_for_provider()
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT","issues":[]}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=OPENROUTER_SELECTABLE_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                output_schema=schema,
            )
        body = http.calls[0]["json"]
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], schema)
        self.assertEqual(
            body["response_format"]["json_schema"]["name"], mc.STRUCTURED_OUTPUT_SCHEMA_NAME
        )

    def test_capability_false_model_payload_byte_identical_to_no_output_schema(self) -> None:
        schema = mos.project_output_schema_for_provider()
        http_a = FakeHttpClient(_content_response('{"decision":"ACCEPT","issues":[]}'))
        http_b = FakeHttpClient(_content_response('{"decision":"ACCEPT","issues":[]}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http_a).invoke(
                model_id=OPENROUTER_PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
            )
            self._client(http_b).invoke(
                model_id=OPENROUTER_PRIMARY_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                output_schema=schema,
            )
        body_a = http_a.calls[0]["json"]
        body_b = http_b.calls[0]["json"]
        self.assertEqual(body_a, body_b)
        self.assertNotIn("response_format", body_b)

    def test_critic_pinned_model_id_also_fails_closed(self) -> None:
        schema = mos.project_output_schema_for_provider()
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT","issues":[]}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=OPENROUTER_CRITIC_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                output_schema=schema,
            )
        self.assertNotIn("response_format", http.calls[0]["json"])

    def test_response_format_and_tool_mode_are_independent_fields(self) -> None:
        # Both may be requested on the same call (env flag on AND capability
        # True) -- they are independent request keys, neither one disables
        # the other. Uses the selectable (capability-True) model id.
        schema = mos.project_output_schema_for_provider()
        tool_schema = mos.model_facing_output_schema()
        http = FakeHttpClient(_content_response('{"decision":"ACCEPT","issues":[]}'))
        with patch.dict("os.environ", {}, clear=True):
            self._client(http).invoke(
                model_id=OPENROUTER_SELECTABLE_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                tool_spec=tool_schema,
                output_schema=schema,
            )
        body = http.calls[0]["json"]
        self.assertIn("tools", body)
        self.assertIn("response_format", body)


# ---------------------------------------------------------------------------
# 4. Protocol signature parity
# ---------------------------------------------------------------------------


class TestProtocolSignatureParity(unittest.TestCase):
    def test_fake_bedrock_client_records_output_schema(self) -> None:
        client = mc.FakeBedrockClient({"m": ['{"decision":"ACCEPT"}']})
        schema = {"type": "object"}
        client.invoke(
            model_id="m",
            system_prompt="s",
            user_prompt="u",
            max_output_tokens=1,
            output_schema=schema,
        )
        self.assertEqual(client.calls[0]["output_schema"], schema)

    def test_fake_bedrock_client_defaults_output_schema_to_none(self) -> None:
        client = mc.FakeBedrockClient({"m": ['{"decision":"ACCEPT"}']})
        client.invoke(model_id="m", system_prompt="s", user_prompt="u", max_output_tokens=1)
        self.assertIsNone(client.calls[0]["output_schema"])


# ---------------------------------------------------------------------------
# 5. Threading through run_primary_pass / run_critic_pass
# ---------------------------------------------------------------------------


class LegacyShapedFakeClient:
    """Mirrors a pre-#567 (and pre-#418) hand-rolled test double: no
    `output_schema` (or `tool_spec`) parameter, no **kwargs, and no
    `capabilities` method -- calling it with an unexpected kwarg raises
    TypeError, exactly as a real pre-existing double would."""

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def invoke(self, *, model_id: str, system_prompt: str, user_prompt: str, max_output_tokens: int) -> str:
        self.calls.append({"model_id": model_id, "max_output_tokens": max_output_tokens})
        return self._response_text


def _sample_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _sample_diff_hunks() -> list[dict[str, Any]]:
    return [{"kind": "modified_new", "anchor": "sec-8", "text": "text"}]


def _sample_anchored_clauses() -> list[dict[str, Any]]:
    return [
        {
            "anchor": "sec-8",
            "standard_text": "standard",
            "counterparty_text": "counterparty",
            "delta": "delta",
        }
    ]


class TestRunPrimaryPassThreading(unittest.TestCase):
    def test_capability_false_never_sends_output_schema_even_to_a_legacy_client(self) -> None:
        legacy = LegacyShapedFakeClient(json.dumps(_load_fixture(_PRIMARY_VALID_FIXTURE)))
        records: list[Any] = []
        with patch.dict("os.environ", {}, clear=True):
            result = pp.run_primary_pass(
                review_id="r-1",
                diff_hunks=_sample_diff_hunks(),
                anchored_clauses=_sample_anchored_clauses(),
                retrieved_precedent=[],
                playbook=_sample_playbook(),
                model_client=legacy,
                model_id="anthropic.claude-opus-4-8",
                ledger_write=records.append,
                doc_text="Section 8 text.",
            )
        self.assertEqual(result["status"], "OK")
        self.assertFalse(result["schema_enforcement_requested"])
        self.assertFalse(records[-1].schema_enforcement_requested)

    def test_capability_true_passes_the_projected_schema_as_output_schema(self) -> None:
        stamped = _load_fixture(_PRIMARY_VALID_FIXTURE)
        # Issue #567 fix round 3, finding 2: `FakeBedrockClient` now
        # validates a capability-True call's canned response against its
        # own output_schema -- the raw unstamped fixture omits
        # `source_quote` entirely, which the projected schema no longer
        # permits (it is required, nullable); fill it in the way a real
        # strict-mode provider would (see _fill_absent_source_quote_with_null).
        candidate = _fill_absent_source_quote_with_null(_unstamp(stamped))
        client = mc.FakeBedrockClient(
            {"anthropic.claude-opus-4-8": [json.dumps(candidate)]},
            capabilities={"structured_outputs": True},
        )
        records: list[Any] = []
        result = pp.run_primary_pass(
            review_id="r-2",
            diff_hunks=_sample_diff_hunks(),
            anchored_clauses=_sample_anchored_clauses(),
            retrieved_precedent=[],
            playbook=_sample_playbook(),
            model_client=client,
            model_id="anthropic.claude-opus-4-8",
            ledger_write=records.append,
            doc_text="Section 8 text.",
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(
            client.calls[0]["output_schema"], mos.project_output_schema_for_provider()
        )
        self.assertTrue(result["schema_enforcement_requested"])
        self.assertTrue(records[-1].schema_enforcement_requested)

    def test_default_fake_client_capability_false_output_schema_not_sent(self) -> None:
        stamped = _load_fixture(_PRIMARY_VALID_FIXTURE)
        client = mc.FakeBedrockClient({"anthropic.claude-opus-4-8": [json.dumps(stamped)]})
        result = pp.run_primary_pass(
            review_id="r-3",
            diff_hunks=_sample_diff_hunks(),
            anchored_clauses=_sample_anchored_clauses(),
            retrieved_precedent=[],
            playbook=_sample_playbook(),
            model_client=client,
            model_id="anthropic.claude-opus-4-8",
            ledger_write=lambda _rec: None,
            doc_text="Section 8 text.",
        )
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(client.calls[0]["output_schema"])
        self.assertFalse(result["schema_enforcement_requested"])


class TestRunCriticPassThreading(unittest.TestCase):
    def test_capability_false_never_sends_output_schema_even_to_a_legacy_client(self) -> None:
        legacy = LegacyShapedFakeClient(json.dumps(_load_fixture("critic_no_delta_accept_valid.json")))
        primary_output = _load_fixture(_PRIMARY_VALID_FIXTURE)
        records: list[Any] = []
        result = cp.run_critic_pass(
            review_id="r-4",
            diff_hunks=_sample_diff_hunks(),
            anchored_clauses=_sample_anchored_clauses(),
            primary_output=primary_output,
            playbook=_sample_playbook(),
            model_client=legacy,
            model_id="anthropic.claude-sonnet-4-6",
            ledger_write=records.append,
        )
        self.assertEqual(result["status"], "OK")
        self.assertFalse(result["schema_enforcement_requested"])
        self.assertFalse(records[-1].schema_enforcement_requested)

    def test_capability_true_passes_the_projected_schema_as_output_schema(self) -> None:
        stamped = _load_fixture("critic_no_delta_accept_valid.json")
        client = mc.FakeBedrockClient(
            {"anthropic.claude-sonnet-4-6": [json.dumps(_unstamp(stamped))]},
            capabilities={"structured_outputs": True},
        )
        primary_output = _load_fixture(_PRIMARY_VALID_FIXTURE)
        records: list[Any] = []
        result = cp.run_critic_pass(
            review_id="r-5",
            diff_hunks=_sample_diff_hunks(),
            anchored_clauses=_sample_anchored_clauses(),
            primary_output=primary_output,
            playbook=_sample_playbook(),
            model_client=client,
            model_id="anthropic.claude-sonnet-4-6",
            ledger_write=records.append,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(
            client.calls[0]["output_schema"], mos.project_output_schema_for_provider()
        )
        self.assertTrue(result["schema_enforcement_requested"])
        self.assertTrue(records[-1].schema_enforcement_requested)


# ---------------------------------------------------------------------------
# 6. ModelInvocationRecord default. (review_spine result-detail plumbing --
#    primary_schema_enforcement_requested / critic_schema_enforcement_
#    requested -- is asserted in tests/test_review_spine.py's own Part 4,
#    which already owns the docx-fixture harness run_review needs; see the
#    module docstring's item 6.)
# ---------------------------------------------------------------------------


class TestModelInvocationRecordDefault(unittest.TestCase):
    def test_default_is_false_and_existing_construction_sites_unaffected(self) -> None:
        record = mc.ModelInvocationRecord(
            review_id="r",
            pass_name="primary",
            model_id="m",
            attempt_number=1,
            outcome="success",
            input_tokens_est=1,
            output_tokens_est=1,
        )
        self.assertFalse(record.schema_enforcement_requested)

    def test_explicit_true_is_carried_through(self) -> None:
        record = mc.ModelInvocationRecord(
            review_id="r",
            pass_name="primary",
            model_id="m",
            attempt_number=1,
            outcome="success",
            input_tokens_est=1,
            output_tokens_est=1,
            schema_enforcement_requested=True,
        )
        self.assertTrue(record.schema_enforcement_requested)


# ---------------------------------------------------------------------------
# 7. July-incident regression: fallback path unaffected by the new request
#    field.
# ---------------------------------------------------------------------------


class TestJulyIncidentRegressionStillCovered(unittest.TestCase):
    def test_prose_wrapped_response_still_parses_on_a_schema_enforcement_requesting_call(
        self,
    ) -> None:
        stamped = _load_fixture(_PRIMARY_VALID_FIXTURE)
        unstamped = _unstamp(stamped)
        prose_wrapped = (
            "Here is my review:\n```json\n" + json.dumps(unstamped) + "\n```\nEnd of review."
        )
        http = FakeHttpClient(_content_response(prose_wrapped))
        with patch.dict("os.environ", {}, clear=True):
            raw = mc.OpenRouterModelClient(
                api_key="sk-test", http_client=http, max_retries=0, sleep_fn=lambda _s: None
            ).invoke(
                model_id=OPENROUTER_SELECTABLE_MODEL_ID,
                system_prompt="SYS",
                user_prompt="USER",
                max_output_tokens=100,
                output_schema=mos.project_output_schema_for_provider(),
            )
        ok, parsed = pp.validate_model_response(raw, issue_provenance="model")
        self.assertTrue(ok, parsed)
        self.assertEqual(parsed["schema_version"], "output-schema-v1")
        self.assertEqual(parsed["issues"][0]["provenance"], "model")
        # The request itself DID carry response_format (capability-True
        # selectable id) -- proving the fallback extractor is exercised
        # regardless, never bypassed because enforcement was requested.
        self.assertIn("response_format", http.calls[0]["json"])


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (
        TestProjectOutputSchemaForProvider,
        TestAdditionalPropertiesForcedFalse,
        TestRecursiveRefFlattening,
        TestStripDoesNotTreatPropertiesMapAsASchemaNode,
        TestPostHocFailClosedDespiteLooserProjection,
        TestProjectedSchemaRoundTripsThroughFullValidation,
        TestLiveBedrockInvokeOutputSchema,
        TestOpenRouterInvokeOutputSchema,
        TestProtocolSignatureParity,
        TestRunPrimaryPassThreading,
        TestRunCriticPassThreading,
        TestModelInvocationRecordDefault,
        TestJulyIncidentRegressionStillCovered,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
