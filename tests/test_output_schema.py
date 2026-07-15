#!/usr/bin/env python3
"""
Output-contract schema gate — issue #4.

Three checks (all must pass; exit 1 on any failure):

1. SUBSET CHECK: every field in output_format.every_issue_includes is a top-level
   property of playbooks/output-schema-v1.json.  Fails today because the schema
   does not exist.

2. BUNDLE-COMPOSITION CHECK: playbooks/schema.json requires release.output_contract_hash
   (or the active playbook's release block carries output_contract_hash).  Fails
   today because neither the playbook schema nor the EIAA release block has that
   field.

3. VALIDATOR UNIT TESTS: a set of fixture model-responses (valid and invalid) is
   tested against the output schema.  Fails today because the schema does not exist.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"
PLAYBOOK_SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"
EIAA_PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"

# ---------------------------------------------------------------------------
# Inline model-response fixtures (valid and invalid)
# ---------------------------------------------------------------------------

VALID_RESPONSE = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted a one-sided indemnification clause.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "This clause imposes a new indemnification obligation on Exos beyond our standard form.",
            "proposed_replacement_text": "Neither party shall indemnify the other for third-party claims.",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": "INTERNAL-ONLY: precedent-003",
            "provenance": "detector:no-exos-indemnity",
        }
    ],
    "critic_delta": None,
}

# Invalid: missing required top-level field 'decision'
INVALID_MISSING_DECISION = {
    "schema_version": "output-schema-v1",
    "confidence_state": "OK",
    "issues": [],
}

# Invalid: wrong type for 'decision'
INVALID_WRONG_DECISION_TYPE = {
    "schema_version": "output-schema-v1",
    "decision": 42,
    "confidence_state": "OK",
    "issues": [],
}

# Invalid: decision value not in enum
INVALID_DECISION_VALUE = {
    "schema_version": "output-schema-v1",
    "decision": "MAYBE",
    "confidence_state": "OK",
    "issues": [],
}

# Invalid: issue missing required field 'section_ref'
INVALID_ISSUE_MISSING_FIELD = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_title": "Indemnification",
            "counterparty_change_summary": "...",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "...",
            "proposed_replacement_text": "...",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": None,
        }
    ],
}

# Invalid: proposed_replacement_text exceeds a reasonable character length.
# The schema should enforce max_length on proposed_replacement_text (e.g. 8000 chars).
INVALID_REPLACEMENT_TOO_LONG = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted indemnification.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "Creates new obligation.",
            "proposed_replacement_text": "X" * 9000,  # Exceeds max
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": None,
        }
    ],
}

VALIDATOR_FIXTURES = [
    {"name": "valid_response", "obj": VALID_RESPONSE, "expect_valid": True},
    {"name": "missing_decision", "obj": INVALID_MISSING_DECISION, "expect_valid": False},
    {"name": "wrong_decision_type", "obj": INVALID_WRONG_DECISION_TYPE, "expect_valid": False},
    {"name": "invalid_decision_value", "obj": INVALID_DECISION_VALUE, "expect_valid": False},
    {"name": "issue_missing_field", "obj": INVALID_ISSUE_MISSING_FIELD, "expect_valid": False},
    {"name": "replacement_too_long", "obj": INVALID_REPLACEMENT_TOO_LONG, "expect_valid": False},
]


# ---------------------------------------------------------------------------
# Minimal JSON-Schema validator (stdlib only — no jsonschema package).
# Supports: type, required, properties, enum, maxLength, items, $ref (local).
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _resolve_ref(ref: str, root_schema: dict) -> dict:
    """Resolve a $ref like '#/definitions/Issue' within root_schema."""
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
    """Return list of error strings (empty = valid)."""
    errors = []

    # Resolve $ref
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root_schema)
        return _validate(obj, resolved, root_schema, path)

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
            return errors  # further checks would be type-unsafe

    # enum check
    if "enum" in schema:
        if obj not in schema["enum"]:
            errors.append(f"{path}: {obj!r} not in enum {schema['enum']!r}")

    # maxLength check (strings)
    if "maxLength" in schema and isinstance(obj, str):
        if len(obj) > schema["maxLength"]:
            errors.append(
                f"{path}: string length {len(obj)} exceeds maxLength {schema['maxLength']}"
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
# Check 1: subset check
# ---------------------------------------------------------------------------

def check_subset(failures: list) -> None:
    """every_issue_includes ⊆ properties of output-schema-v1.json issues[]."""
    label = "SUBSET CHECK"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {OUTPUT_SCHEMA_PATH.name} does not exist. "
            "Author playbooks/output-schema-v1.json to fix."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        output_schema = json.load(f)

    with open(EIAA_PLAYBOOK_PATH) as f:
        playbook = json.load(f)

    every_issue_includes = playbook.get("output_format", {}).get("every_issue_includes", [])
    if not every_issue_includes:
        failures.append(f"{label}: output_format.every_issue_includes is empty or missing in playbook.")
        return

    # Navigate to the issue object schema, resolving $ref if needed.
    # Expected path: output-schema-v1.json -> properties.issues.items -> (resolve $ref) -> properties
    try:
        items_schema = output_schema["properties"]["issues"]["items"]
        # Resolve $ref if present (e.g. "#/definitions/Issue")
        if "$ref" in items_schema:
            ref = items_schema["$ref"]
            parts = ref.lstrip("#/").split("/")
            node = output_schema
            for part in parts:
                node = node[part]
            items_schema = node
        issue_props = items_schema["properties"]
    except (KeyError, TypeError) as e:
        failures.append(
            f"{label}: could not navigate output-schema-v1.json to "
            f"properties.issues.items.properties (with $ref resolution): {e}"
        )
        return

    missing = [field for field in every_issue_includes if field not in issue_props]
    if missing:
        failures.append(
            f"{label}: fields in every_issue_includes not found in output-schema-v1.json "
            f"issue properties: {missing}"
        )
    else:
        print(
            f"  PASS {label}: all {len(every_issue_includes)} every_issue_includes fields "
            f"are properties of output-schema-v1.json issues[]."
        )


# ---------------------------------------------------------------------------
# Check 2: bundle-composition check
# ---------------------------------------------------------------------------

def check_bundle_composition(failures: list) -> None:
    """release block in schema.json should require output_contract_hash."""
    label = "BUNDLE-COMPOSITION CHECK"

    with open(PLAYBOOK_SCHEMA_PATH) as f:
        schema = json.load(f)

    # The release block's required fields live at:
    # properties.playbook.properties.release.required
    try:
        release_required = (
            schema["properties"]["playbook"]["properties"]["release"]["required"]
        )
    except (KeyError, TypeError) as e:
        failures.append(
            f"{label}: could not navigate schema.json to "
            f"properties.playbook.properties.release.required: {e}"
        )
        return

    if "output_contract_hash" not in release_required:
        failures.append(
            f"{label}: 'output_contract_hash' is not in the release block's required "
            f"fields in playbooks/schema.json. Current required: {release_required}"
        )
    else:
        print(
            f"  PASS {label}: 'output_contract_hash' is required in the release bundle."
        )


# ---------------------------------------------------------------------------
# Check 3: validator unit tests
# ---------------------------------------------------------------------------

def check_validator_fixtures(failures: list) -> None:
    """Run valid/invalid model-response fixtures against output-schema-v1.json."""
    label = "VALIDATOR UNIT TESTS"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {OUTPUT_SCHEMA_PATH.name} does not exist — cannot run fixture tests. "
            "Author playbooks/output-schema-v1.json to fix."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        output_schema = json.load(f)

    for fixture in VALIDATOR_FIXTURES:
        name = fixture["name"]
        obj = fixture["obj"]
        expect_valid = fixture["expect_valid"]

        errs = validate(obj, output_schema)
        is_valid = len(errs) == 0

        if is_valid == expect_valid:
            status = "valid" if is_valid else "invalid"
            print(f"  PASS {label}: fixture '{name}' correctly classified as {status}.")
        else:
            if expect_valid:
                failures.append(
                    f"{label}: fixture '{name}' expected VALID but got errors: {errs}"
                )
            else:
                failures.append(
                    f"{label}: fixture '{name}' expected INVALID but passed validation."
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Output-contract schema gate (issue #4)\n")
    failures = []

    check_subset(failures)
    check_bundle_composition(failures)
    check_validator_fixtures(failures)

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print("PASS: all output-contract schema checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
