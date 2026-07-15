#!/usr/bin/env python3
"""
CI structural gate: no hard_rejections[] rule may have a provably empty
effective hunk-scope.

Effective scope for a rule = union of section_anchors over applies_to_topics,
PLUS pseudo-anchor eligibility (sec-_new) for on_insert rules whose
applies_to_topics includes a not_in_standard topic.

A rule's scope is PROVABLY EMPTY when:
  - It has applies_to_topics defined, AND
  - Every referenced topic has section_anchors == [] (empty), AND
  - The rule is NOT eligible for sec-_new pseudo-anchor coverage.

Pseudo-anchor eligibility:
  - Rule kind == "on_insert", AND
  - At least one topic in applies_to_topics has not_in_standard == true, AND
  - "sec-_new" appears in that topic's section_anchors (after the fix).

This test FAILS (RED) if:
  - Any topic with not_in_standard: true has an empty section_anchors
    (i.e., does not include "sec-_new"), AND
  - That topic is referenced by an on_insert rule via applies_to_topics.

After the GREEN fix, not_in_standard topics will carry ["sec-_new"] in their
section_anchors, giving on_insert rules referencing them a non-empty scope.

Exit codes: 0 = pass, 1 = fail
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"

SEC_NEW = "sec-_new"


def load_playbook():
    with open(PLAYBOOK_PATH) as f:
        return json.load(f)


def build_topic_map(playbook):
    """Return dict: topic_id -> topic object."""
    return {t["id"]: t for t in playbook.get("topics", [])}


def compute_effective_scope(rule, topic_map):
    """
    Returns a tuple (anchors: set[str], pseudo_eligible: bool).

    anchors: the union of section_anchors across all applies_to_topics topics.
    pseudo_eligible: True if the rule is an on_insert rule referencing at least
                     one not_in_standard topic that carries sec-_new.
    """
    applies_to = rule.get("applies_to_topics", [])
    if not applies_to:
        # Corpus-wide: not empty by definition (hits everything)
        return ({"*"}, False)

    anchors = set()
    pseudo_eligible = False

    for tid in applies_to:
        topic = topic_map.get(tid)
        if topic is None:
            continue
        topic_anchors = set(topic.get("section_anchors", []))
        anchors |= topic_anchors
        # Check pseudo-anchor eligibility
        if (
            rule.get("kind") == "on_insert"
            and topic.get("not_in_standard", False)
            and SEC_NEW in topic_anchors
        ):
            pseudo_eligible = True

    return (anchors, pseudo_eligible)


def is_scope_empty(anchors, pseudo_eligible):
    """A scope is provably empty if anchors is empty and not pseudo-eligible."""
    return len(anchors) == 0 and not pseudo_eligible


def main():
    playbook = load_playbook()
    topic_map = build_topic_map(playbook)
    hard_rejections = playbook.get("hard_rejections", [])

    failures = []

    for rule in hard_rejections:
        rule_id = rule.get("id", "<unknown>")
        kind = rule.get("kind")
        applies_to = rule.get("applies_to_topics", [])

        # Only check rules with explicit applies_to_topics
        if not applies_to:
            continue

        anchors, pseudo_eligible = compute_effective_scope(rule, topic_map)
        empty = is_scope_empty(anchors, pseudo_eligible)

        if empty:
            topic_details = []
            for tid in applies_to:
                t = topic_map.get(tid, {})
                topic_details.append(
                    f"  topic '{tid}': not_in_standard={t.get('not_in_standard', False)}, "
                    f"section_anchors={t.get('section_anchors', [])}"
                )
            failures.append(
                f"EMPTY SCOPE: rule '{rule_id}' (kind={kind}) has provably empty "
                f"effective hunk-scope.\n"
                f"  applies_to_topics: {applies_to}\n"
                + "\n".join(topic_details) + "\n"
                f"  FIX: add '{SEC_NEW}' to section_anchors of each not_in_standard "
                f"topic, then define {SEC_NEW} semantics in schema.json and ARCHITECTURE.md."
            )

    if failures:
        print("FAIL: empty-scope gate detected rules with no effective hunk-scope.")
        print(
            "These rules are dead config: they can never fire on any diff hunk.\n"
        )
        for f in failures:
            print(f)
        print(
            f"\nTotal rules with empty effective scope: {len(failures)}\n"
            f"These are the rules identified in issue #1 as dead config."
        )
        sys.exit(1)
    else:
        print(
            f"PASS: all {len(hard_rejections)} hard_rejection rules have "
            f"non-empty effective hunk-scope."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
