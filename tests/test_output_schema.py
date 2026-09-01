#!/usr/bin/env python3
"""
Output-contract schema gate — issue #4, pointed at output-schema-v2.json
(issue #376: v2 is a clean-break successor to v1 adding an optional
issues[].source_quote field; v2's Issue shape is a strict superset of v1's,
so this file's checks and fixtures hold for both).

Three checks (all must pass; exit 1 on any failure):

1. SUBSET CHECK: every field in output_format.every_issue_includes is a top-level
   property of the ACTIVE output contract (primary_review_pass.OUTPUT_SCHEMA_PATH),
   not of a hard-coded generation of it (issue #636 scope 3).

2. BUNDLE-COMPOSITION CHECK: playbooks/schema.json requires release.output_contract_hash
   (or the active playbook's release block carries output_contract_hash).  Fails
   today because neither the playbook schema nor the EIAA release block has that
   field.

3. VALIDATOR UNIT TESTS: a set of fixture model-responses (valid and invalid) is
   tested against the output schema.  Fails today because the schema does not exist.
   Includes v2-specific fixtures covering issues[].source_quote both present and
   absent (issue #376 acceptance criterion).
"""

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The v2 artifact, still the subject of this file's VALIDATOR UNIT TESTS: those
# fixtures pin the superseded contract on purpose (issue #376) and must keep
# reading it.
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v2.json"

PRIMARY_REVIEW_PASS_SRC = REPO_ROOT / "scripts" / "primary_review_pass.py"


def _active_output_schema_path() -> Path:
    """`primary_review_pass.OUTPUT_SCHEMA_PATH`, read WITHOUT importing it.

    The SUBSET CHECK below asks "does this playbook instruct the field set the
    model is actually validated against?", so it has to read the contract in
    force rather than a hard-coded generation of it -- issue #627 flipped the
    active artifact to output-schema-v3.json (which makes `issue_key` REQUIRED
    on every Issue), and a correctly-aligned playbook failed this check purely
    because the check was pinned to v2 (issue #636 scope 3).

    It is resolved out of the SOURCE with `ast` instead of by importing the
    module, because `.github/workflows/output-schema.yml` runs this file as
    `python3 tests/test_output_schema.py` on a bare interpreter and states the
    invariant explicitly: "Only the v3 step below needs it; every other step in
    this job is stdlib-only." Importing `primary_review_pass` pulls in
    `model_client` -> `jsonschema` and fails that job with ModuleNotFoundError
    while passing locally in a venv that has the dependency -- the CI blind
    spot this repo has already been bitten by twice.

    Raises rather than falling back to a guessed path: a silent fallback would
    make this check pass against a contract that is not the live one, which is
    the exact failure mode it exists to prevent.
    """
    tree = ast.parse(PRIMARY_REVIEW_PASS_SRC.read_text(encoding="utf-8"))
    assignments: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value

    name = "OUTPUT_SCHEMA_PATH"
    seen: set[str] = set()
    while name in assignments and isinstance(assignments[name], ast.Name):
        if name in seen:
            raise RuntimeError(f"cyclic alias resolving OUTPUT_SCHEMA_PATH at {name!r}")
        seen.add(name)
        name = assignments[name].id  # type: ignore[union-attr]

    if name not in assignments:
        raise RuntimeError(
            f"could not resolve OUTPUT_SCHEMA_PATH in {PRIMARY_REVIEW_PASS_SRC}"
        )

    # Expected shape: REPO_ROOT / "playbooks" / "<artifact>.json"
    parts: list[str] = []
    node = assignments[name]
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not isinstance(node.right, ast.Constant) or not isinstance(node.right.value, str):
            raise RuntimeError(f"unexpected path component in OUTPUT_SCHEMA_PATH: {ast.dump(node.right)}")
        parts.append(node.right.value)
        node = node.left
    if not (isinstance(node, ast.Name) and node.id == "REPO_ROOT") or not parts:
        raise RuntimeError(
            "OUTPUT_SCHEMA_PATH is no longer a REPO_ROOT-anchored path expression; "
            "update this resolver rather than guessing."
        )
    return REPO_ROOT.joinpath(*reversed(parts))


ACTIVE_OUTPUT_SCHEMA_PATH = _active_output_schema_path()
PLAYBOOK_SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"
EIAA_PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

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
            "external_rationale_for_footnote": "This clause imposes a new indemnification obligation on the Company beyond our standard form.",
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

# v2 (issue #376): issues[].source_quote is OPTIONAL. VALID_RESPONSE above
# already covers the "omits it" half of the acceptance criterion (no
# source_quote key on its one issue). VALID_RESPONSE_WITH_SOURCE_QUOTE below
# covers the "includes it" half.
VALID_RESPONSE_WITH_SOURCE_QUOTE = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted a one-sided indemnification clause.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "This clause imposes a new indemnification obligation on the Company beyond our standard form.",
            "proposed_replacement_text": "Neither party shall indemnify the other for third-party claims.",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": "INTERNAL-ONLY: precedent-003",
            "provenance": "detector:no-exos-indemnity",
            "source_quote": "Company shall indemnify, defend, and hold harmless Counterparty from any and all claims.",
        }
    ],
    "critic_delta": None,
}

# Invalid: wrong type for source_quote (must be a string, not e.g. an
# integer). The minimal stdlib validator below implements "type" but not
# "minLength", so this is a type-check regression guard rather than a
# minLength one -- see check_validator_fixtures' docstring.
INVALID_SOURCE_QUOTE_WRONG_TYPE = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted a one-sided indemnification clause.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "This clause imposes a new indemnification obligation on the Company beyond our standard form.",
            "proposed_replacement_text": "Neither party shall indemnify the other for third-party claims.",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": None,
            "provenance": "model",
            "source_quote": 12345,
        }
    ],
    "critic_delta": None,
}

VALIDATOR_FIXTURES = [
    {"name": "valid_response", "obj": VALID_RESPONSE, "expect_valid": True},
    {"name": "valid_response_with_source_quote", "obj": VALID_RESPONSE_WITH_SOURCE_QUOTE, "expect_valid": True},
    {"name": "missing_decision", "obj": INVALID_MISSING_DECISION, "expect_valid": False},
    {"name": "wrong_decision_type", "obj": INVALID_WRONG_DECISION_TYPE, "expect_valid": False},
    {"name": "invalid_decision_value", "obj": INVALID_DECISION_VALUE, "expect_valid": False},
    {"name": "issue_missing_field", "obj": INVALID_ISSUE_MISSING_FIELD, "expect_valid": False},
    {"name": "replacement_too_long", "obj": INVALID_REPLACEMENT_TOO_LONG, "expect_valid": False},
    {"name": "source_quote_wrong_type", "obj": INVALID_SOURCE_QUOTE_WRONG_TYPE, "expect_valid": False},
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
    """every_issue_includes ⊆ properties of the ACTIVE output contract's issues[]."""
    label = "SUBSET CHECK"
    schema_name = ACTIVE_OUTPUT_SCHEMA_PATH.name

    if not ACTIVE_OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {schema_name} does not exist. "
            f"Author playbooks/{schema_name} to fix."
        )
        return

    with open(ACTIVE_OUTPUT_SCHEMA_PATH) as f:
        output_schema = json.load(f)

    with open(EIAA_PLAYBOOK_PATH) as f:
        playbook = json.load(f)

    every_issue_includes = playbook.get("output_format", {}).get("every_issue_includes", [])
    if not every_issue_includes:
        failures.append(f"{label}: output_format.every_issue_includes is empty or missing in playbook.")
        return

    # Navigate to the issue object schema, resolving $ref if needed.
    # Expected path: output-schema-v2.json -> properties.issues.items -> (resolve $ref) -> properties
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
            f"{label}: could not navigate {schema_name} to "
            f"properties.issues.items.properties (with $ref resolution): {e}"
        )
        return

    missing = [field for field in every_issue_includes if field not in issue_props]
    if missing:
        failures.append(
            f"{label}: fields in every_issue_includes not found in {schema_name} "
            f"issue properties: {missing}"
        )
    else:
        print(
            f"  PASS {label}: all {len(every_issue_includes)} every_issue_includes fields "
            f"are properties of {schema_name} issues[]."
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
    """Run valid/invalid model-response fixtures against output-schema-v2.json."""
    label = "VALIDATOR UNIT TESTS"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {OUTPUT_SCHEMA_PATH.name} does not exist — cannot run fixture tests. "
            "Author playbooks/output-schema-v2.json to fix."
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
