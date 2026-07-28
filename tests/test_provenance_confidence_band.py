#!/usr/bin/env python3
"""
Issue #35: Per-issue provenance and low-confidence band — schema gate.

Three checks (all must pass; exit 1 on any failure):

1. PROVENANCE SCHEMA CHECK: every Issue in output-schema-v1.json carries a
   required `provenance` field constrained to the three valid values:
     - "model"           — the LLM primary reviewer flagged this issue
     - "critic-added"    — the adversarial critic added this issue (not in primary output)
     - pattern matching "detector:<rule_id>" — a deterministic hard-rejection rule fired

   The field is framed as SYSTEM METADATA, not a third legal category. It is
   never rendered as a decision and must not influence ACCEPT/REQUEST_CHANGE.

2. CONFIDENCE-BAND SCHEMA CHECK: the output schema carries a top-level
   `confidence_band` field (optional, string or null) documenting that the
   low-confidence band is surfaced in the result view pre-download. The field
   carries metadata only; it is a system status surface, not a legal verdict.

3. VALIDATOR FIXTURE TESTS: a set of model-response fixtures (valid and invalid)
   is tested to confirm:
   - a valid issue with provenance="model" passes
   - a valid issue with provenance="critic-added" passes
   - a valid issue with provenance="detector:no-exos-indemnity" passes
   - an issue missing provenance fails validation (field is required)
   - an issue with an unrecognized provenance value ("unknown") fails validation
   - a response with LOW_CONFIDENCE and a concrete issue (provenance present) passes
   - a response with a valid confidence_band value passes
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"


# ---------------------------------------------------------------------------
# Minimal JSON-Schema validator (stdlib only — mirrors test_output_schema.py).
# Supports: type, required, properties, enum, maxLength, minLength, pattern,
#           oneOf, items, $ref (local), additionalProperties, const.
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _resolve_ref(ref: str, root_schema: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValidationError(f"Only local $ref supported, got: {ref!r}")
    parts = ref.lstrip("#/").split("/")
    node = root_schema
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            raise ValidationError(f"$ref {ref!r} could not be resolved")
        node = node[part]
    return node


def _validate(obj, schema: dict, root_schema: dict, path: str = "") -> list:
    errors = []

    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root_schema)
        return _validate(obj, resolved, root_schema, path)

    # oneOf
    if "oneOf" in schema:
        matches = 0
        for sub in schema["oneOf"]:
            if not _validate(obj, sub, root_schema, path):
                matches += 1
        if matches != 1:
            errors.append(f"{path}: expected exactly one oneOf to match, got {matches}")
        return errors

    # type check
    schema_type = schema.get("type")
    if schema_type:
        type_map = {
            "object": dict,
            "array": list,
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "null": type(None),
        }
        expected_types = schema_type if isinstance(schema_type, list) else [schema_type]
        python_types = tuple(type_map[t] for t in expected_types if t in type_map)
        if python_types and not isinstance(obj, python_types):
            errors.append(f"{path}: expected type {schema_type!r}, got {type(obj).__name__!r}")
            return errors

    # const check
    if "const" in schema:
        if obj != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}, got {obj!r}")

    # enum check
    if "enum" in schema:
        if obj not in schema["enum"]:
            errors.append(f"{path}: {obj!r} not in enum {schema['enum']!r}")

    # pattern check (strings)
    if "pattern" in schema and isinstance(obj, str):
        import re
        if not re.search(schema["pattern"], obj):
            errors.append(f"{path}: string {obj!r} does not match pattern {schema['pattern']!r}")

    # maxLength / minLength (strings)
    if "maxLength" in schema and isinstance(obj, str):
        if len(obj) > schema["maxLength"]:
            errors.append(
                f"{path}: string length {len(obj)} exceeds maxLength {schema['maxLength']}"
            )
    if "minLength" in schema and isinstance(obj, str):
        if len(obj) < schema["minLength"]:
            errors.append(
                f"{path}: string length {len(obj)} below minLength {schema['minLength']}"
            )

    # required + properties (objects)
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                errors.append(f"{path}: missing required property {req!r}")
        for prop, prop_schema in schema.get("properties", {}).items():
            if prop in obj:
                errors.extend(_validate(obj[prop], prop_schema, root_schema, f"{path}.{prop}"))

    # items (arrays)
    if isinstance(obj, list):
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(obj):
                errors.extend(_validate(item, items_schema, root_schema, f"{path}[{i}]"))

    return errors


def validate(obj, schema: dict) -> list:
    return _validate(obj, schema, schema, "$")


# ---------------------------------------------------------------------------
# Check 1: provenance field in output-schema-v1.json Issue definition
# ---------------------------------------------------------------------------

def check_provenance_in_schema(failures: list) -> None:
    label = "PROVENANCE SCHEMA CHECK"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {OUTPUT_SCHEMA_PATH.name} does not exist."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        schema = json.load(f)

    # Navigate to the Issue definition's properties
    try:
        issue_schema = schema["definitions"]["Issue"]
        issue_props = issue_schema["properties"]
        issue_required = issue_schema.get("required", [])
    except (KeyError, TypeError) as e:
        failures.append(
            f"{label}: could not navigate output-schema-v1.json to "
            f"definitions.Issue.properties: {e}"
        )
        return

    if "provenance" not in issue_props:
        failures.append(
            f"{label}: 'provenance' field is not present in Issue.properties in "
            f"output-schema-v1.json. Every issue must carry provenance metadata "
            f"(detector:<rule_id> | model | critic-added) so lawyers can calibrate "
            f"scrutiny (deterministic hard-rejection vs. LLM judgment call). "
            f"Add a 'provenance' property to the Issue definition in "
            f"playbooks/output-schema-v1.json."
        )
        return

    if "provenance" not in issue_required:
        failures.append(
            f"{label}: 'provenance' is in Issue.properties but NOT in Issue.required. "
            f"It must be required so that every issue in a model response carries it. "
            f"Add 'provenance' to the required array in the Issue definition."
        )
        return

    prov_schema = issue_props["provenance"]
    # Acceptable encodings: oneOf with (enum for "model"/"critic-added") and (pattern for
    # "detector:<rule_id>"), or a single combined pattern, or an anyOf/oneOf covering all three.
    # We check that the schema description references system-metadata framing.
    desc = prov_schema.get("description", "")
    if not any(kw in desc.lower() for kw in ("system metadata", "system-metadata", "metadata", "framing")):
        failures.append(
            f"{label}: 'provenance' field in Issue definition does not document the "
            f"system-metadata framing in its description. The field must be described "
            f"as system metadata (not a legal category) per output-contract.md framing rules."
        )
        return

    print(
        f"  PASS {label}: 'provenance' is a required field in Issue with "
        f"system-metadata framing documented."
    )


# ---------------------------------------------------------------------------
# Check 2: confidence_band in output-schema-v1.json top-level
# ---------------------------------------------------------------------------

def check_confidence_band_in_schema(failures: list) -> None:
    label = "CONFIDENCE-BAND SCHEMA CHECK"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {OUTPUT_SCHEMA_PATH.name} does not exist."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        schema = json.load(f)

    top_props = schema.get("properties", {})

    if "confidence_band" not in top_props:
        failures.append(
            f"{label}: 'confidence_band' field is not present as a top-level property "
            f"in output-schema-v1.json. The low-confidence band must be surfaced in the "
            f"result view pre-download, framed as system metadata. "
            f"Add a 'confidence_band' property to the root of the response schema in "
            f"playbooks/output-schema-v1.json."
        )
        return

    cb_schema = top_props["confidence_band"]
    # Acceptable encoding: oneOf [{type: null}, {type: string, ...}] or similar.
    desc = cb_schema.get("description", "")
    if not any(kw in desc.lower() for kw in ("system metadata", "system-metadata", "metadata", "system status")):
        failures.append(
            f"{label}: 'confidence_band' field description does not reference system "
            f"metadata / system status framing. Per output-contract.md, the band is "
            f"a system-status surface, never a legal verdict."
        )
        return

    print(
        f"  PASS {label}: 'confidence_band' is present at root with system-status "
        f"framing documented."
    )


# ---------------------------------------------------------------------------
# Check 3: validator fixture tests
# ---------------------------------------------------------------------------

def _make_issue(provenance: str | None = "model", missing: bool = False) -> dict:
    """Build a minimal valid Issue dict, optionally with a custom provenance."""
    issue = {
        "section_ref": "8",
        "section_title": "Limitation on Liability",
        "counterparty_change_summary": "Counterparty removed the $150,000 cap.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "Removes the agreed liability ceiling.",
        "proposed_replacement_text": "Liability cap is $150,000.",
        "playbook_topic_id": "limitation-on-liability",
        "internal_precedent_citation": None,
    }
    if not missing:
        issue["provenance"] = provenance
    return issue


def _make_response(issues: list, confidence_band=None) -> dict:
    base = {
        "schema_version": "output-schema-v1",
        "decision": "REQUEST_CHANGE" if issues else "ACCEPT",
        "confidence_state": "OK",
        "issues": issues,
        "critic_delta": None,
    }
    if confidence_band is not None:
        base["confidence_band"] = confidence_band
    return base


PROVENANCE_FIXTURES = [
    # Valid cases
    {
        "name": "provenance_model",
        "obj": _make_response([_make_issue("model")]),
        "expect_valid": True,
        "description": "issue with provenance='model' should be valid",
    },
    {
        "name": "provenance_critic_added",
        "obj": _make_response([_make_issue("critic-added")]),
        "expect_valid": True,
        "description": "issue with provenance='critic-added' should be valid",
    },
    {
        "name": "provenance_detector",
        "obj": _make_response([_make_issue("detector:no-exos-indemnity")]),
        "expect_valid": True,
        "description": "issue with provenance='detector:no-exos-indemnity' should be valid",
    },
    {
        "name": "provenance_detector_generic",
        "obj": _make_response([_make_issue("detector:my-rule-123")]),
        "expect_valid": True,
        "description": "issue with provenance='detector:my-rule-123' should be valid",
    },
    {
        "name": "response_with_confidence_band_low",
        "obj": _make_response([_make_issue("model")], confidence_band="LOW_CONFIDENCE"),
        "expect_valid": True,
        "description": "response with confidence_band='LOW_CONFIDENCE' should be valid",
    },
    {
        "name": "response_with_confidence_band_null",
        "obj": _make_response([_make_issue("model")], confidence_band=None),
        "expect_valid": True,
        "description": "response with confidence_band=null should be valid",
    },
    # Invalid cases
    {
        "name": "provenance_missing",
        "obj": _make_response([_make_issue(missing=True)]),
        "expect_valid": False,
        "description": "issue missing provenance field should fail validation",
    },
    {
        "name": "provenance_unknown_value",
        "obj": _make_response([_make_issue("unknown-source")]),
        "expect_valid": False,
        "description": "issue with unrecognized provenance value should fail validation",
    },
    {
        "name": "provenance_empty_string",
        "obj": _make_response([_make_issue("")]),
        "expect_valid": False,
        "description": "issue with empty provenance string should fail validation",
    },
    {
        "name": "provenance_detector_no_rule_id",
        "obj": _make_response([_make_issue("detector:")]),
        "expect_valid": False,
        "description": "issue with provenance='detector:' (no rule_id) should fail",
    },
]


def check_validator_fixtures(failures: list) -> None:
    label = "VALIDATOR FIXTURE TESTS"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {OUTPUT_SCHEMA_PATH.name} does not exist — cannot run fixtures."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        schema = json.load(f)

    for fixture in PROVENANCE_FIXTURES:
        name = fixture["name"]
        obj = fixture["obj"]
        expect_valid = fixture["expect_valid"]
        desc = fixture["description"]

        errs = validate(obj, schema)
        is_valid = len(errs) == 0

        if is_valid == expect_valid:
            status = "valid" if is_valid else "invalid"
            print(f"  PASS {label}: fixture '{name}' correctly classified as {status}.")
        else:
            if expect_valid:
                failures.append(
                    f"{label}: fixture '{name}' ({desc}) expected VALID but got errors: {errs}"
                )
            else:
                failures.append(
                    f"{label}: fixture '{name}' ({desc}) expected INVALID but passed validation."
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Provenance and confidence-band schema gate (issue #35)\n")
    failures = []

    check_provenance_in_schema(failures)
    check_confidence_band_in_schema(failures)
    check_validator_fixtures(failures)

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print("PASS: all provenance and confidence-band schema checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
