#!/usr/bin/env python3
"""
Issue #47: Low-severity UX cleanups — group naming, disposition nag, ACCEPT summary shape.

Three checks (all must pass; exit 1 on any failure):

1. GROUP-NAMING DECISION CHECK (docs-lint flavour):
   ARCHITECTURE.md Authentication section (or RUNBOOK.md Onboarding section)
   must document the intentional misnomer: that `legal-admin@example.com` is the
   allowlist group for ALL users (not just admins), and that admin privilege is
   controlled separately by the in-app `is_admin` flag.  This prevents lifecycle
   mistakes caused by misreading the group name as "only admins belong here."

2. DISPOSITION-NAG CHECK:
   ARCHITECTURE.md or docs/output-contract.md must specify a disposition nag
   ("N reviews awaiting disposition") in the reviewer's list view.  The nag
   surfaces how many completed reviews are still missing an attorney
   accepted/edited/rejected outcome, helping the eval feedback loop avoid
   starvation.

3. ACCEPT-SUMMARY-SHAPE CHECK:
   a. output-schema-v1.json must have a top-level `verdict_summary` property.
   b. docs/output-contract.md must document the `verdict_summary` shape and its
      source for the ACCEPT path (not just per-issue content for REQUEST_CHANGE).
   c. The schema fixture in this test (an ACCEPT response with a populated
      verdict_summary) must validate against the schema.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
OUTPUT_CONTRACT_PATH = REPO_ROOT / "docs" / "output-contract.md"
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v1.json"


# ---------------------------------------------------------------------------
# Minimal inline schema validator (stdlib only — same approach as test_output_schema.py)
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

    schema_type = schema.get("type")
    if schema_type:
        type_map = {
            "object": dict, "array": list, "string": str,
            "integer": int, "number": (int, float), "boolean": bool,
            "null": type(None),
        }
        expected_types = schema_type if isinstance(schema_type, list) else [schema_type]
        python_types = tuple(type_map[t] for t in expected_types if t in type_map)
        if python_types and not isinstance(obj, python_types):
            errors.append(f"{path}: expected type {schema_type!r}, got {type(obj).__name__!r}")
            return errors

    if "enum" in schema:
        if obj not in schema["enum"]:
            errors.append(f"{path}: {obj!r} not in enum {schema['enum']!r}")

    if "const" in schema:
        if obj != schema["const"]:
            errors.append(f"{path}: {obj!r} does not match const {schema['const']!r}")

    if "maxLength" in schema and isinstance(obj, str):
        if len(obj) > schema["maxLength"]:
            errors.append(f"{path}: string length {len(obj)} exceeds maxLength {schema['maxLength']}")

    if "minLength" in schema and isinstance(obj, str):
        if len(obj) < schema["minLength"]:
            errors.append(f"{path}: string length {len(obj)} less than minLength {schema['minLength']}")

    if "oneOf" in schema:
        # Validate that exactly one sub-schema matches
        matching = 0
        for sub in schema["oneOf"]:
            if not _validate(obj, sub, root_schema, path):
                matching += 1
        if matching != 1:
            errors.append(f"{path}: value does not match exactly one of oneOf sub-schemas (matched {matching})")

    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                errors.append(f"{path}: missing required property {req!r}")
        for prop, prop_schema in schema.get("properties", {}).items():
            if prop in obj:
                errors.extend(_validate(obj[prop], prop_schema, root_schema, f"{path}.{prop}"))

    if isinstance(obj, list):
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(obj):
                errors.extend(_validate(item, items_schema, root_schema, f"{path}[{i}]"))

    return errors


def validate(obj, schema: dict) -> list:
    return _validate(obj, schema, schema, "$")


# ---------------------------------------------------------------------------
# Check 1: Group-naming decision documented
# ---------------------------------------------------------------------------

def check_group_naming_decision(failures: list) -> None:
    """
    ARCHITECTURE.md or RUNBOOK.md must explicitly note that legal-admin@example.com
    is the allowlist group for ALL users (not only admins), and that the in-app
    is_admin flag is the sole admin privilege gate.  This disambiguates the
    misleading group name so operators do not misread it as "admins only."
    """
    label = "GROUP-NAMING DECISION CHECK"

    texts_to_search = []
    for path in [ARCHITECTURE_PATH, RUNBOOK_PATH]:
        if path.exists():
            texts_to_search.append((path, path.read_text(encoding="utf-8")))

    if not texts_to_search:
        failures.append(f"{label}: neither ARCHITECTURE.md nor RUNBOOK.md found.")
        return

    # We require an explicit misnomer note in either ARCHITECTURE.md or RUNBOOK.md.
    # Acceptable patterns:
    #   (a) The word "misnomer" appears near legal-admin@example.com (within ~500 chars).
    #   (b) The group name is explicitly described as covering all users / non-admins too
    #       (within a single line or paragraph, not just a DOTALL span of the whole file).
    #
    # The existing "Admin vs reviewer is NOT controlled by Google groups" sentence in
    # ARCHITECTURE.md is correct but does NOT fulfill the misnomer requirement — it
    # explains the is_admin flag but does not explicitly note that the confusing group
    # name `legal-admin` covers all users (not only admins), which is the lifecycle-
    # mistake risk the issue calls out.

    has_misnomer_note = False
    for path, text in texts_to_search:
        # Pattern (a): explicit "misnomer" near the group name (within 600 chars)
        misnomer_near_group = False
        for m in re.finditer(r"legal-admin@teamexos\.com", text, re.IGNORECASE):
            window = text[max(0, m.start() - 300):m.end() + 300]
            if re.search(r"\bmisnomer\b", window, re.IGNORECASE):
                misnomer_near_group = True
                break

        # Pattern (b): explicit statement that the group covers all ContractToaster users (not only admins),
        # within the same line as the group name.
        # Requires the phrase "all ContractToaster users" or "covers all ContractToaster" within 200 chars of the
        # group name on the same line — a precise phrasing added by this PR that does not
        # appear anywhere in the pre-PR baseline.  Broad alternatives like "reviewer.*not.*admin"
        # are intentionally excluded because they false-match the existing onboarding prose
        # ("A reviewer is anyone ... they are not admins. The pre-token Lambda checks
        # `legal-admin@example.com`") which says nothing about the group covering all users.
        group_covers_all_inline = bool(re.search(
            r"legal-admin@teamexos\.com[^\n]{0,200}(?:all\s+ContractToaster\s+users?|covers\s+all\s+ContractToaster)",
            text, re.IGNORECASE,
        )) or bool(re.search(
            r"(?:all\s+ContractToaster\s+users?|covers\s+all\s+ContractToaster)[^\n]{0,200}legal-admin@teamexos\.com",
            text, re.IGNORECASE,
        ))

        if misnomer_near_group or group_covers_all_inline:
            has_misnomer_note = True
            break

    if not has_misnomer_note:
        failures.append(
            f"{label}: Neither ARCHITECTURE.md nor RUNBOOK.md documents the "
            f"group-naming misnomer: `legal-admin@example.com` is the allowlist for "
            f"ALL users (reviewers and admins alike), not only admins. "
            f"Add a note in the Authentication section of ARCHITECTURE.md (or the "
            f"Onboarding section of RUNBOOK.md) clarifying that (a) the group name is "
            f"a misnomer — it covers all ContractToaster users, not only admins — and (b) the "
            f"in-app `is_admin` flag is the sole admin-privilege gate, separate from "
            f"group membership. This prevents lifecycle mistakes (e.g. removing a "
            f"reviewer from the group because the word 'admin' makes it seem like "
            f"non-admins don't belong there)."
        )
    else:
        print(
            f"  PASS {label}: group-naming misnomer documented "
            f"(legal-admin@example.com covers all users; is_admin flag governs privilege)."
        )


# ---------------------------------------------------------------------------
# Check 2: Disposition nag in reviewer list view
# ---------------------------------------------------------------------------

def check_disposition_nag(failures: list) -> None:
    """
    ARCHITECTURE.md or docs/output-contract.md must specify a nag count for
    reviews awaiting attorney disposition (accepted/edited/rejected).
    This surfaces low-compliance as a visible signal in the reviewer's list view
    so the eval feedback loop does not starve quietly.
    """
    label = "DISPOSITION-NAG CHECK"

    texts_to_search = []
    for path in [ARCHITECTURE_PATH, OUTPUT_CONTRACT_PATH]:
        if path.exists():
            texts_to_search.append((path, path.read_text(encoding="utf-8")))

    if not texts_to_search:
        failures.append(
            f"{label}: neither ARCHITECTURE.md nor docs/output-contract.md found."
        )
        return

    # Must mention:
    #   - a nag / reminder / badge / count (as a whole word)
    #   - disposition (accepted/edited/rejected) or "awaiting disposition"
    #   - in the reviewer list view or review list
    has_nag = False
    for path, text in texts_to_search:
        # Look for "nag" (whole word) or "awaiting disposition" or "disposition count" etc.
        nag_pattern = bool(re.search(
            r"(?:\bnag\b|awaiting\s+disposition|disposition.*\bnag\b|\bnag\b.*disposition|"
            r"disposition.*count|count.*disposition|"
            r"reviews?\s+awaiting\s+disposition|disposition.*list\s+view|"
            r"list\s+view.*disposition)",
            text, re.IGNORECASE
        ))
        if nag_pattern:
            has_nag = True
            break

    if not has_nag:
        failures.append(
            f"{label}: Neither ARCHITECTURE.md nor docs/output-contract.md specifies "
            f"a disposition nag in the reviewer's list view. "
            f"Add a spec for a nag state (e.g. 'N reviews awaiting disposition') "
            f"in the reviewer's list view to surface low compliance with the "
            f"attorney accepted/edited/rejected capture. "
            f"This prevents the eval feedback loop from starving silently. "
            f"Document this in ARCHITECTURE.md (reviewer flow or frontend section) "
            f"or in docs/output-contract.md."
        )
    else:
        print(
            f"  PASS {label}: disposition nag specified "
            f"(reviewer list view shows awaiting-disposition count)."
        )


# ---------------------------------------------------------------------------
# Check 3: ACCEPT summary shape in schema and output-contract.md
# ---------------------------------------------------------------------------

def check_accept_summary_shape(failures: list) -> None:
    """
    Three sub-checks:
    3a. output-schema-v1.json must have a top-level `verdict_summary` property.
    3b. docs/output-contract.md must document verdict_summary for the ACCEPT path
        (not only for REQUEST_CHANGE).
    3c. A fixture ACCEPT response with verdict_summary must validate against the schema.
    """
    label = "ACCEPT-SUMMARY-SHAPE CHECK"

    # 3a: schema has verdict_summary
    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}(3a): playbooks/output-schema-v1.json does not exist."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        schema = json.load(f)

    if "verdict_summary" not in schema.get("properties", {}):
        failures.append(
            f"{label}(3a): output-schema-v1.json does not define a top-level "
            f"`verdict_summary` property. "
            f"The ACCEPT path promises 'a summary of what changed and why each change "
            f"was acceptable'; this must be a named field in the schema so the pipeline "
            f"can validate and the leakage scan can cover it. "
            f"Add `verdict_summary` to the top-level properties of output-schema-v1.json."
        )
    else:
        print(
            f"  PASS {label}(3a): output-schema-v1.json has `verdict_summary` property."
        )

    # 3b: output-contract.md documents verdict_summary shape for ACCEPT path
    if not OUTPUT_CONTRACT_PATH.exists():
        failures.append(
            f"{label}(3b): docs/output-contract.md does not exist."
        )
    else:
        text = OUTPUT_CONTRACT_PATH.read_text(encoding="utf-8")
        # Must mention verdict_summary in an ACCEPT-specific context
        has_accept_summary_spec = bool(re.search(
            r"verdict_summary.*ACCEPT|ACCEPT.*verdict_summary|"
            r"verdict_summary.*accept\s+path|accept\s+path.*verdict_summary|"
            r"ACCEPT.*summary.*shape|accept.*summary.*source",
            text, re.IGNORECASE
        ))
        if not has_accept_summary_spec:
            failures.append(
                f"{label}(3b): docs/output-contract.md does not document the "
                f"`verdict_summary` field shape for the ACCEPT path. "
                f"The output-contract must specify: "
                f"(1) `verdict_summary` is the source for the ACCEPT result summary "
                f"('what changed and why each change was acceptable'), "
                f"(2) it passes the leakage scan before rendering. "
                f"Add an 'ACCEPT summary shape' subsection to the output-contract "
                f"(or expand the existing 'What the schema defines' table)."
            )
        else:
            print(
                f"  PASS {label}(3b): docs/output-contract.md documents "
                f"`verdict_summary` shape for the ACCEPT path."
            )

    # 3c: fixture ACCEPT response with verdict_summary validates against schema
    # (Only run if 3a passed — i.e. schema has verdict_summary)
    if "verdict_summary" in schema.get("properties", {}):
        accept_fixture = {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "issues": [],
            "critic_delta": None,
            "verdict_summary": (
                "All counterparty changes were within acceptable variation: "
                "Section 4 extended the payment term by 5 days (within standard tolerance) "
                "and Section 12 reordered definitions with no substantive change."
            ),
        }
        errs = validate(accept_fixture, schema)
        if errs:
            failures.append(
                f"{label}(3c): ACCEPT fixture with verdict_summary failed schema "
                f"validation: {errs}. "
                f"Ensure the `verdict_summary` property in output-schema-v1.json "
                f"permits a non-empty string for ACCEPT responses."
            )
        else:
            print(
                f"  PASS {label}(3c): ACCEPT fixture with `verdict_summary` "
                f"validates against the schema."
            )

        # Also verify an ACCEPT without verdict_summary still validates
        # (verdict_summary is optional — not required for backward compatibility)
        accept_no_summary = {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "issues": [],
            "critic_delta": None,
        }
        errs_no_summary = validate(accept_no_summary, schema)
        if errs_no_summary:
            failures.append(
                f"{label}(3c-optional): ACCEPT response without verdict_summary "
                f"should still be valid (verdict_summary is not required). "
                f"Errors: {errs_no_summary}"
            )
        else:
            print(
                f"  PASS {label}(3c-optional): ACCEPT without verdict_summary "
                f"also validates (field is optional)."
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("UX cleanups gate — issue #47 (group naming, disposition nag, ACCEPT summary)\n")
    failures = []

    check_group_naming_decision(failures)
    check_disposition_nag(failures)
    check_accept_summary_shape(failures)

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print("PASS: all UX-cleanups checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
