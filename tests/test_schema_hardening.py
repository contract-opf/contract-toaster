#!/usr/bin/env python3
"""
Issue #6: Schema hardening regression gate.

Five hostile structural invariants that MUST be rejected but currently are not
enforced by either schema.json or a Python structural gate:

  (a) A hard_rejection rule with `exemptTerms` (camelCase typo) instead of
      `exempt_terms` is silently accepted because there is no
      additionalProperties guard on the rule object.  Detection: check that
      schema.json has additionalProperties:false on hard_rejections.items; also
      verify the seed playbook has no unknown keys in any rule.

  (b) An `on_remove_or_alter` rule carrying `exempt_terms` — an on_insert-only
      field — is currently unconstrained.  Detection: verify schema.json forbids
      `exempt_terms` / `match` / `match_surface` under the on_remove_or_alter
      conditional; also scan the live playbook for violations.

  (c) A present-section topic (not_in_standard absent/false) with empty
      section_anchors [] is not caught.  Detection: verify schema.json encodes
      the implication as if/then (minItems:1) for present-section topics; also
      scan the live playbook.

  (d) A `not_in_standard: true` topic with section_anchors containing a real
      section anchor (not 'sec-_new') is not caught.  Detection: verify
      schema.json encodes the implication (maxItems:1, items const 'sec-_new')
      for not_in_standard topics; also scan the live playbook.

  (e) A rule whose `applies_to_topics` names a nonexistent topic id — referential
      integrity — is not a stated CI rule.  Detection: Python scan of the live
      playbook applying the referential integrity check.

Run on every change to playbooks/ by CI.
Exit codes: 0 = all five invariants are enforced, 1 = one or more gaps found.
"""

import copy
import json
import sys
from pathlib import Path

try:
    import jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"

ON_INSERT_ONLY_FIELDS = {"trigger_terms", "match", "match_surface", "exempt_terms", "regex_trigger_terms"}
ON_REMOVE_ONLY_FIELDS = {"protects"}
# Fields explicitly defined in the hard_rejections.items properties
KNOWN_RULE_FIELDS = {
    "id", "description", "kind",
    "trigger_terms", "match", "match_surface", "exempt_terms", "regex_trigger_terms",
    "protects", "applies_to_topics",
}
# Fields explicitly defined in the topics.items properties
KNOWN_TOPIC_FIELDS = {
    "id", "section_ref", "section_anchors", "not_in_standard",
    "exos_standard", "acceptable_variations", "must_preserve",
    "reject_if_proposed", "hard_rejection_refs", "replacement_text",
}


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ── (a) additionalProperties on hard_rejections.items ────────────────────────

def check_a_schema_has_additional_props_on_rules(schema: dict) -> tuple[bool, str]:
    """
    Verify schema.json has additionalProperties:false on hard_rejections.items.
    A typo like `exemptTerms` must not validate cleanly.
    """
    items_schema = (
        schema
        .get("properties", {})
        .get("hard_rejections", {})
        .get("items", {})
    )
    if items_schema.get("additionalProperties") is False:
        return True, "PASS (a-schema): hard_rejections.items has additionalProperties:false"
    return False, (
        "FAIL (a-schema): hard_rejections.items does NOT have additionalProperties:false. "
        "A typo like `exemptTerms` (instead of `exempt_terms`) silently passes schema "
        "validation, dropping the guard (C1 bug class). "
        "Fix: add additionalProperties:false to hard_rejections.items in schema.json."
    )


def check_a_seed_no_unknown_rule_keys(playbook: dict) -> tuple[bool, str]:
    """
    Scan the live playbook for unknown keys in hard_rejections items.
    This detects any existing typos even before schema fix is applied.
    """
    violations = []
    for rule in playbook.get("hard_rejections", []):
        unknown = set(rule.keys()) - KNOWN_RULE_FIELDS
        if unknown:
            violations.append(
                f"rule '{rule.get('id', '?')}' has unknown key(s): {sorted(unknown)}"
            )
    if violations:
        return False, (
            f"FAIL (a-playbook): hard_rejections rules have unknown keys: "
            + "; ".join(violations)
        )
    return True, "PASS (a-playbook): no unknown keys in hard_rejections rules."


# ── (b) on_remove_or_alter must not carry on_insert-only fields ──────────────

def check_b_schema_forbids_insert_fields_on_remove(schema: dict) -> tuple[bool, str]:
    """
    Verify schema.json's on_remove_or_alter conditional forbids on_insert-only
    fields (exempt_terms, match, match_surface).
    """
    items_schema = (
        schema
        .get("properties", {})
        .get("hard_rejections", {})
        .get("items", {})
    )
    all_of = items_schema.get("allOf", [])

    remove_conditional = None
    for clause in all_of:
        cond_props = clause.get("if", {}).get("properties", {})
        kind_const = cond_props.get("kind", {}).get("const")
        if kind_const == "on_remove_or_alter":
            remove_conditional = clause
            break

    if remove_conditional is None:
        return False, (
            "FAIL (b-schema): no on_remove_or_alter conditional found in "
            "hard_rejections.items.allOf. Cannot verify that exempt_terms is forbidden."
        )

    then_clause = remove_conditional.get("then", {})
    # Two patterns for forbidding a field:
    # 1) not: {required: ['field']} -- forbids requiring it but doesn't prevent it
    # 2) additionalProperties:false with only allowed properties listed
    # 3) properties: {exempt_terms: false} or similar
    # The strongest approach: additionalProperties:false (covered by check_a) +
    # not having exempt_terms in the allowed properties of the then clause.
    # We check for an explicit "not: {required: [...]}" or for a "not" key
    # that covers the on_insert-only fields.

    # Look for a 'not' block that forbids at least one of the on_insert-only fields
    not_block = then_clause.get("not", {})
    # could be not: {required: ['exempt_terms', 'match', 'match_surface']}
    # or could be not: {anyOf: [...]}
    not_required = not_block.get("required", [])
    not_any_of = not_block.get("anyOf", [])

    forbidden_in_not = set(not_required)
    for any_item in not_any_of:
        forbidden_in_not.update(any_item.get("required", []))

    insert_only = {"exempt_terms", "match", "match_surface", "regex_trigger_terms"}
    if insert_only & forbidden_in_not:
        return True, (
            f"PASS (b-schema): on_remove_or_alter conditional forbids on_insert-only "
            f"fields: {sorted(insert_only & forbidden_in_not)}"
        )

    return False, (
        "FAIL (b-schema): on_remove_or_alter conditional does NOT forbid on_insert-only "
        "fields (exempt_terms, match, match_surface). A rule like "
        "{kind: 'on_remove_or_alter', exempt_terms: [...]} passes schema validation. "
        "Fix: extend the on_remove_or_alter then clause with "
        "'not: {anyOf: [{required: [exempt_terms]}, {required: [match]}, "
        "{required: [match_surface]}]}' (or similar) in schema.json."
    )


def check_b_seed_no_insert_fields_on_remove(playbook: dict) -> tuple[bool, str]:
    """Scan the live playbook for on_remove_or_alter rules with on_insert-only fields."""
    violations = []
    for rule in playbook.get("hard_rejections", []):
        if rule.get("kind") == "on_remove_or_alter":
            bad = set(rule.keys()) & ON_INSERT_ONLY_FIELDS
            if bad:
                violations.append(
                    f"rule '{rule.get('id', '?')}' (on_remove_or_alter) has "
                    f"on_insert-only field(s): {sorted(bad)}"
                )
    if violations:
        return False, (
            "FAIL (b-playbook): on_remove_or_alter rules carry on_insert-only fields: "
            + "; ".join(violations)
        )
    return True, "PASS (b-playbook): no on_remove_or_alter rules carry on_insert-only fields."


# ── (c) present-section topic must have minItems:1 section_anchors ──────────

def check_c_schema_encodes_present_section_min_anchors(schema: dict) -> tuple[bool, str]:
    """
    Verify schema.json's topics.items encodes: if not_in_standard is absent or
    false, then section_anchors must have minItems:1.
    """
    topics_items = (
        schema
        .get("properties", {})
        .get("topics", {})
        .get("items", {})
    )
    all_of = topics_items.get("allOf", [])

    # Look for a conditional that says: if not not_in_standard (or not_in_standard==false),
    # then section_anchors.minItems >= 1
    for clause in all_of:
        cond = clause.get("if", {})
        then = clause.get("then", {})
        # We want: then.properties.section_anchors.minItems >= 1
        sa = then.get("properties", {}).get("section_anchors", {})
        if sa.get("minItems", 0) >= 1:
            return True, (
                "PASS (c-schema): topics.items has a conditional that requires "
                "minItems:1 on section_anchors for present-section topics."
            )

    return False, (
        "FAIL (c-schema): topics.items does NOT encode a conditional requiring "
        "section_anchors minItems:1 for present-section topics (not_in_standard "
        "absent or false). A topic with section_anchors:[] passes schema validation, "
        "making any on_insert rule scoped to that topic dead config. "
        "Fix: add if/then to topics.items in schema.json: "
        "if not not_in_standard (or not_in_standard const false), then "
        "section_anchors.minItems:1."
    )


def check_c_seed_no_present_section_empty_anchors(playbook: dict) -> tuple[bool, str]:
    """Scan the live playbook for present-section topics with empty section_anchors."""
    violations = []
    for topic in playbook.get("topics", []):
        if not topic.get("not_in_standard", False):
            anchors = topic.get("section_anchors", [])
            if anchors == [] or anchors is None:
                violations.append(
                    f"topic '{topic.get('id', '?')}' (present-section) has empty section_anchors"
                )
    if violations:
        return False, (
            "FAIL (c-playbook): present-section topics have empty section_anchors: "
            + "; ".join(violations)
        )
    return True, "PASS (c-playbook): all present-section topics have non-empty section_anchors."


# ── (d) not_in_standard topic must have exactly ['sec-_new'] ─────────────────

def check_d_schema_encodes_not_in_standard_anchors(schema: dict) -> tuple[bool, str]:
    """
    Verify schema.json's topics.items encodes: if not_in_standard==true, then
    section_anchors must equal exactly ['sec-_new'] (maxItems:1, minItems:1,
    items const or enum 'sec-_new').
    """
    topics_items = (
        schema
        .get("properties", {})
        .get("topics", {})
        .get("items", {})
    )
    all_of = topics_items.get("allOf", [])

    for clause in all_of:
        cond = clause.get("if", {})
        then = clause.get("then", {})
        # Check the condition targets not_in_standard:true
        cond_nis = cond.get("properties", {}).get("not_in_standard", {})
        if cond_nis.get("const") is True or cond_nis.get("enum") == [True]:
            sa = then.get("properties", {}).get("section_anchors", {})
            # Must have maxItems:1 (or items const 'sec-_new') to enforce exactly ['sec-_new']
            if sa.get("maxItems") == 1 or (
                isinstance(sa.get("items"), dict) and (
                    sa["items"].get("const") == "sec-_new" or
                    sa["items"].get("enum") == ["sec-_new"]
                )
            ):
                return True, (
                    "PASS (d-schema): topics.items has a conditional that requires "
                    "section_anchors to be exactly ['sec-_new'] for not_in_standard topics."
                )

    return False, (
        "FAIL (d-schema): topics.items does NOT encode a conditional requiring "
        "section_anchors to equal exactly ['sec-_new'] for not_in_standard:true topics. "
        "A not_in_standard topic with section_anchors:['sec-8'] passes schema validation, "
        "violating the not_in_standard contract. "
        "Fix: add if/then to topics.items in schema.json: "
        "if not_in_standard const:true, then section_anchors "
        "maxItems:1, minItems:1, items const:'sec-_new'."
    )


def check_d_seed_not_in_standard_anchors_correct(playbook: dict) -> tuple[bool, str]:
    """Scan the live playbook for not_in_standard topics with non-sec-_new anchors."""
    violations = []
    for topic in playbook.get("topics", []):
        if topic.get("not_in_standard", False):
            anchors = topic.get("section_anchors", [])
            if anchors != ["sec-_new"]:
                violations.append(
                    f"topic '{topic.get('id', '?')}' (not_in_standard) has "
                    f"section_anchors={anchors!r} (must be exactly ['sec-_new'])"
                )
    if violations:
        return False, (
            "FAIL (d-playbook): not_in_standard topics have incorrect section_anchors: "
            + "; ".join(violations)
        )
    return True, "PASS (d-playbook): all not_in_standard topics have section_anchors=['sec-_new']."


# ── (e) applies_to_topics referential integrity ───────────────────────────────

def check_e_applies_to_topics_referential_integrity(playbook: dict) -> tuple[bool, str]:
    """
    Every id in hard_rejections[].applies_to_topics must exist in topics[].id.
    JSON Schema draft-07 cannot express cross-array referential integrity, so
    this is a Python structural check.
    """
    topic_ids = {t["id"] for t in playbook.get("topics", [])}
    violations = []
    for rule in playbook.get("hard_rejections", []):
        rule_id = rule.get("id", "<unknown>")
        for tid in rule.get("applies_to_topics", []):
            if tid not in topic_ids:
                violations.append(
                    f"rule '{rule_id}' references nonexistent topic '{tid}'"
                )
    if violations:
        return False, (
            "FAIL (e): applies_to_topics referential integrity violated: "
            + "; ".join(violations)
        )
    return True, (
        f"PASS (e): all applies_to_topics ids resolve to existing topics "
        f"(checked {len(playbook.get('hard_rejections', []))} rules against "
        f"{len(topic_ids)} topic ids)."
    )


def check_e_governance_doc_mentions_referential_integrity(schema: dict) -> tuple[bool, str]:
    """
    Verify the schema.json documents applies_to_topics → topic id resolution as
    a CI constraint (the description field).
    """
    at_prop = (
        schema
        .get("properties", {})
        .get("hard_rejections", {})
        .get("items", {})
        .get("properties", {})
        .get("applies_to_topics", {})
    )
    desc = at_prop.get("description", "")
    # Check for some mention of referential integrity or resolution
    if any(kw in desc.lower() for kw in ("resolv", "exist", "ci", "referential")):
        return True, "PASS (e-schema): applies_to_topics description mentions CI resolution."
    return False, (
        "FAIL (e-schema): applies_to_topics description in schema.json does not mention "
        "referential integrity / CI resolution check. "
        "Fix: update the description to state that CI verifies every applies_to_topics id "
        "exists in topics[].id."
    )


# ── (f) validator-based hostile-document rejection fixtures ──────────────────
#
# These tests instantiate concrete hostile documents (fixtures a-d from the
# issue TDD plan) and assert that a Draft7 JSON Schema validator actually
# rejects them.  A structural introspection check (does the schema contain
# the right keywords?) can pass even if a conditional regression lets a
# hostile doc through; validator-based tests catch that class of regression.
#
# Fixture (a): hard_rejection rule with a camelCase typo key (exemptTerms).
# Fixture (b): on_remove_or_alter rule that also carries exempt_terms.
# Fixture (c): present-section topic with section_anchors: [].
# Fixture (d): not_in_standard:true topic with a real anchor (not 'sec-_new').
# Seed     : the live playbook — must still PASS (regression guard).

def _build_validator(schema: dict):
    """Return a Draft7Validator for schema, or None if jsonschema is unavailable."""
    if not _JSONSCHEMA_AVAILABLE:
        return None
    return jsonschema.Draft7Validator(schema)


def _first_rule_of_kind(playbook: dict, kind: str) -> dict | None:
    for rule in playbook.get("hard_rejections", []):
        if rule.get("kind") == kind:
            return rule
    return None


def _first_topic(playbook: dict, not_in_standard: bool | None = None) -> dict | None:
    for topic in playbook.get("topics", []):
        nis = topic.get("not_in_standard", False)
        if not_in_standard is None or nis == not_in_standard:
            return topic
    return None


def check_f_validator_rejects_hostile_fixtures(schema: dict, playbook: dict) -> tuple[bool, str]:
    """
    Instantiate the four schema-encodable hostile fixtures (a-d) and assert
    that a real Draft7 JSON Schema validator rejects each one.  Also assert
    the seed playbook still validates cleanly (regression guard).

    Fixture (a): hard_rejection rule with camelCase typo key 'exemptTerms'.
    Fixture (b): on_remove_or_alter rule carrying on_insert-only field 'exempt_terms'.
    Fixture (c): present-section topic with section_anchors: [] (empty).
    Fixture (d): not_in_standard:true topic with section_anchors: ['sec-1'] (real anchor).
    Seed:        the live eiaa-v1.0.0.json playbook must validate without errors.
    """
    if not _JSONSCHEMA_AVAILABLE:
        return False, (
            "FAIL (f-fixtures): jsonschema package is not installed — this check is "
            "FAIL-CLOSED and must not be skipped in CI.  "
            "Add 'pip install jsonschema' to the workflow before running this test.  "
            "Install the package locally with 'pip install jsonschema' and re-run."
        )

    validator = _build_validator(schema)
    results = []

    # ── seed must still validate ─────────────────────────────────────────────
    seed_errors = list(validator.iter_errors(playbook))
    if seed_errors:
        msgs = "; ".join(e.message[:120] for e in seed_errors[:3])
        results.append(f"SEED falsely rejected: {msgs}")

    # ── fixture (a): camelCase typo key on an on_insert rule ────────────────
    on_insert_rule = _first_rule_of_kind(playbook, "on_insert")
    if on_insert_rule is None:
        results.append("fixture-a: no on_insert rule found in seed; cannot build fixture")
    else:
        doc_a = copy.deepcopy(playbook)
        # Find and mutate the rule
        for rule in doc_a["hard_rejections"]:
            if rule.get("kind") == "on_insert" and rule.get("id") == on_insert_rule["id"]:
                rule["exemptTerms"] = ["non-exclusive"]  # camelCase typo
                break
        errors_a = list(validator.iter_errors(doc_a))
        if not errors_a:
            results.append(
                "fixture-a ACCEPTED (bug): rule with camelCase 'exemptTerms' typo key "
                "passed schema validation — additionalProperties:false not enforced on "
                "hard_rejections.items"
            )

    # ── fixture (b): on_remove_or_alter rule carrying exempt_terms ───────────
    on_remove_rule = _first_rule_of_kind(playbook, "on_remove_or_alter")
    if on_remove_rule is None:
        results.append("fixture-b: no on_remove_or_alter rule found in seed; cannot build fixture")
    else:
        doc_b = copy.deepcopy(playbook)
        for rule in doc_b["hard_rejections"]:
            if rule.get("kind") == "on_remove_or_alter" and rule.get("id") == on_remove_rule["id"]:
                rule["exempt_terms"] = ["non-exclusive"]  # on_insert-only field
                break
        errors_b = list(validator.iter_errors(doc_b))
        if not errors_b:
            results.append(
                "fixture-b ACCEPTED (bug): on_remove_or_alter rule with 'exempt_terms' "
                "passed schema validation — the on_remove_or_alter conditional does not "
                "forbid on_insert-only fields"
            )

    # ── fixture (c): present-section topic with empty section_anchors ────────
    present_topic = _first_topic(playbook, not_in_standard=False)
    if present_topic is None:
        results.append("fixture-c: no present-section topic found in seed; cannot build fixture")
    else:
        doc_c = copy.deepcopy(playbook)
        for topic in doc_c["topics"]:
            if topic.get("id") == present_topic["id"]:
                topic["section_anchors"] = []  # empty — must be rejected
                break
        errors_c = list(validator.iter_errors(doc_c))
        if not errors_c:
            results.append(
                "fixture-c ACCEPTED (bug): present-section topic with section_anchors:[] "
                "passed schema validation — the minItems:1 conditional is missing or broken"
            )

    # ── fixture (d): not_in_standard:true topic with a real anchor ───────────
    nis_topic = _first_topic(playbook, not_in_standard=True)
    if nis_topic is None:
        results.append("fixture-d: no not_in_standard:true topic found in seed; cannot build fixture")
    else:
        doc_d = copy.deepcopy(playbook)
        for topic in doc_d["topics"]:
            if topic.get("id") == nis_topic["id"]:
                topic["section_anchors"] = ["sec-1"]  # real anchor — must be rejected
                break
        errors_d = list(validator.iter_errors(doc_d))
        if not errors_d:
            results.append(
                "fixture-d ACCEPTED (bug): not_in_standard:true topic with "
                "section_anchors:['sec-1'] passed schema validation — the "
                "maxItems:1 / items const:'sec-_new' conditional is missing or broken"
            )

    if results:
        return False, (
            "FAIL (f-fixtures): validator-based hostile-document tests found issues:\n  "
            + "\n  ".join(results)
        )
    return True, (
        "PASS (f-fixtures): all four hostile fixtures (a-d) are rejected by the "
        "Draft7 validator and the seed playbook still validates cleanly."
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    schema = load_json(SCHEMA_PATH)
    playbook = load_json(PLAYBOOK_PATH)

    tests = [
        # (a) additionalProperties:false on rules
        check_a_schema_has_additional_props_on_rules,
        check_a_seed_no_unknown_rule_keys,
        # (b) on_remove_or_alter must not carry on_insert-only fields
        check_b_schema_forbids_insert_fields_on_remove,
        check_b_seed_no_insert_fields_on_remove,
        # (c) present-section topic: section_anchors minItems:1
        check_c_schema_encodes_present_section_min_anchors,
        check_c_seed_no_present_section_empty_anchors,
        # (d) not_in_standard: section_anchors == ['sec-_new']
        check_d_schema_encodes_not_in_standard_anchors,
        check_d_seed_not_in_standard_anchors_correct,
        # (e) applies_to_topics referential integrity
        check_e_applies_to_topics_referential_integrity,
        check_e_governance_doc_mentions_referential_integrity,
        # (f) validator-based hostile-document rejection fixtures
        check_f_validator_rejects_hostile_fixtures,
    ]

    print("Schema hardening regression gate (issue #6)")
    print("=" * 60)

    import inspect

    failures = []
    for fn in tests:
        # Dispatch based on parameter signature
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        if params == ["schema"]:
            passed, message = fn(schema)
        elif params == ["playbook"]:
            passed, message = fn(playbook)
        elif params == ["schema", "playbook"]:
            passed, message = fn(schema, playbook)
        else:
            # Unknown signature — skip
            continue

        status = "PASS" if passed else "FAIL"
        print(f"\n[{status}] {fn.__name__}")
        print(f"  {message}")
        if not passed:
            failures.append(fn.__name__)

    print("\n" + "=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} check(s) not met:")
        for f in failures:
            print(f"  - {f}")
        print(
            "\nFix required (issue #6):\n"
            "  1. additionalProperties:false on hard_rejections.items (and topics.items,\n"
            "     replacement_text.properties, protects.properties, and playbook root).\n"
            "  2. Extend on_remove_or_alter conditional to forbid exempt_terms, match,\n"
            "     and match_surface via 'not: {anyOf: [...]}'.\n"
            "  3. Add if/then to topics.items:\n"
            "     - not_in_standard absent/false => section_anchors minItems:1\n"
            "     - not_in_standard const:true  => section_anchors maxItems:1,\n"
            "       minItems:1, items const:'sec-_new'\n"
            "  4. Update applies_to_topics description in schema.json to state that\n"
            "     CI verifies every id resolves to an existing topic.\n"
            "  5. Update playbook-governance.md CI rule list to include applies_to_topics\n"
            "     → topic id referential integrity (both directions).\n"
            "  See: https://github.com/contract-opf/contract-toaster/issues/6"
        )
        return 1
    else:
        print("PASS: all five hostile invariants are enforced; schema and playbook are consistent.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
