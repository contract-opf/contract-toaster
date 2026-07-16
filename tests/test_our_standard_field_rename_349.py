#!/usr/bin/env python3
"""
RED test (TDD) -- issue #349 GRIND SPEC item 1: rename the playbook schema
field `exos_standard` -> `our_standard` everywhere (the 4 playbooks/*.json
data files, the 7 consumer modules, and every test referencing the field).

## What this proves

Before this ticket, `exos_standard` was a schema field key baked into
`playbooks/eiaa-v1.0.0.json` / `playbooks/schema.json` and read by seven
modules (scripts/{third_party_clause_matching, playbook_validation,
leakage_scan, diff_standard_form, third_party_position_findings,
build_anchor_map}.py and backend/src/reviews.py). Issue #349's GRIND SPEC
renames the field to `our_standard` (matching the engine's OPF v0.2 field
of the same name) across the data model and every consumer.

## What this test asserts

  1. `playbooks/schema.json` declares `our_standard` (not `exos_standard`)
     as a required topic property.
  2. Loading the real `eiaa` playbook via
     `scripts/playbook_validation.py::load_and_validate_playbook` (the
     same runtime seam `backend/src/reviews.py` uses) succeeds, and every
     covering topic (per `playbook_validation.topic_missing_standard_text`'s
     "covering" definition) carries a non-blank `our_standard` string.
  3. No topic in the loaded playbook carries the old `exos_standard` key.

This file MUST fail on the pre-fix tree (the field is still named
`exos_standard`) and pass once the rename lands.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import playbook_validation  # noqa: E402


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def check_schema_declares_our_standard() -> list[str]:
    print("\nCheck 1: playbooks/schema.json requires 'our_standard' (not 'exos_standard') …")
    schema = playbook_validation.load_playbook_schema()
    topic_schema = schema.get("properties", {}).get("topics", {}).get("items", {})
    required = topic_schema.get("required", [])
    properties = topic_schema.get("properties", {})

    failures: list[str] = []
    failures += _assert(
        "our_standard" in required,
        "'our_standard' is a required topic property in playbooks/schema.json",
        f"required: {required}",
    )
    failures += _assert(
        "our_standard" in properties,
        "'our_standard' is defined in topics.items.properties",
        f"properties: {sorted(properties)}",
    )
    failures += _assert(
        "exos_standard" not in required and "exos_standard" not in properties,
        "'exos_standard' no longer appears anywhere in playbooks/schema.json's topic schema",
        f"required: {required}\n         properties: {sorted(properties)}",
    )
    return failures


def check_loaded_playbook_uses_our_standard() -> list[str]:
    print("\nCheck 2: loaded 'eiaa' playbook topics carry 'our_standard' text …")
    failures: list[str] = []

    try:
        doc = playbook_validation.load_and_validate_playbook("eiaa")
    except playbook_validation.PlaybookValidationError as exc:
        return _assert(False, "load_and_validate_playbook('eiaa') succeeds", repr(exc))

    topics = doc.get("topics", [])
    failures += _assert(len(topics) > 0, "playbook has at least one topic")
    if failures:
        return failures

    for topic in topics:
        if topic.get("not_in_standard"):
            continue
        anchors = [a for a in topic.get("section_anchors", []) if a != "sec-_new"]
        if not anchors:
            continue
        topic_id = topic.get("id", "<unknown>")
        failures += _assert(
            bool((topic.get("our_standard") or "").strip()),
            f"covering topic {topic_id!r} carries non-blank 'our_standard' text",
            f"topic keys: {sorted(topic)}",
        )
        failures += _assert(
            "exos_standard" not in topic,
            f"covering topic {topic_id!r} does not carry the old 'exos_standard' key",
        )

    return failures


def main() -> int:
    print("our_standard field rename -- structural + runtime gate (issue #349)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_schema_declares_our_standard()
    all_failures += check_loaded_playbook_uses_our_standard()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: 'our_standard' rename verified in schema and the loaded playbook.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
