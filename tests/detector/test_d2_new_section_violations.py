#!/usr/bin/env python3
"""
D2 planted-violation gate: new-standalone-section insertions.

The three rules that were dead config (issue #1):
  - no-counterparty-home-arbitration  (topic: governing-law-and-venue)
  - no-excess-insurance-levels        (topic: insurance)
  - no-exos-indemnity                 (topic: indemnification, limitation-of-liability)

Each should fire when a counterparty inserts a NEW standalone section (not an
edit to an existing standard-form section) containing the trigger terms.

This test simulates the detector logic over a synthetic diff that represents a
wholly new inserted section (tagged with pseudo-anchor "sec-_new").

A hunk in the diff is modeled as:
  {
    "anchor": str,          # section_anchor of the diff hunk
    "text": str,            # inserted/modified-new text
    "kind": "inserted"      # diff kind
  }

The detector logic (simplified):
  For an on_insert rule, a hunk is IN SCOPE if its anchor is in the union of
  section_anchors over applies_to_topics. The rule fires if any trigger_term
  appears in an in-scope inserted/modified-new hunk (respecting exempt_terms).

This test FAILS (RED) when not_in_standard topics do NOT carry "sec-_new" in
their section_anchors, because the detector then has no anchors to match
against and the rule cannot fire.

Exit codes: 0 = all planted violations detected, 1 = any missed
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"

SEC_NEW = "sec-_new"


def load_playbook():
    with open(PLAYBOOK_PATH) as f:
        return json.load(f)


def build_topic_map(playbook):
    return {t["id"]: t for t in playbook.get("topics", [])}


def build_rule_map(playbook):
    return {r["id"]: r for r in playbook.get("hard_rejections", [])}


def compute_rule_anchors(rule, topic_map):
    """Union of section_anchors over applies_to_topics (or '*' for corpus-wide)."""
    applies_to = rule.get("applies_to_topics", [])
    if not applies_to:
        return {"*"}
    anchors = set()
    for tid in applies_to:
        t = topic_map.get(tid, {})
        anchors.update(t.get("section_anchors", []))
    return anchors


def hunk_in_scope(hunk_anchor, rule_anchors):
    """True if the hunk's anchor is in the rule's effective anchor set."""
    if "*" in rule_anchors:
        return True
    return hunk_anchor in rule_anchors


def rule_fires_on_hunk(rule, hunk, rule_anchors):
    """
    Check if an on_insert rule fires on a given diff hunk.
    Returns True if a trigger_term matches (respecting exempt_terms and scope).
    """
    if rule.get("kind") != "on_insert":
        return False
    if hunk.get("kind") not in ("inserted", "modified_new"):
        return False
    if not hunk_in_scope(hunk["anchor"], rule_anchors):
        return False

    text = hunk["text"].lower()
    trigger_terms = [t.lower() for t in rule.get("trigger_terms", [])]
    exempt_terms = [e.lower() for e in rule.get("exempt_terms", [])]
    match_mode = rule.get("match", "word_boundary")

    def matches(term, text):
        if match_mode == "substring":
            return term in text
        elif match_mode == "word_boundary":
            pattern = r"\b" + re.escape(term) + r"\b"
            return bool(re.search(pattern, text))
        else:
            return bool(re.search(term, text))

    for trigger in trigger_terms:
        if not matches(trigger, text):
            continue
        # Check if any exempt term covers this hit
        exempted = False
        for exempt in exempt_terms:
            if matches(exempt, text):
                exempted = True
                break
        if not exempted:
            return True
    return False


# ---- Synthetic D2 test cases ----
# Each case represents a counterparty inserting a NEW standalone section
# (tagged with sec-_new) containing the trigger term.

D2_CASES = [
    {
        "case_id": "d2-new-section-arbitration",
        "description": (
            "Counterparty inserts a standalone 'Dispute Resolution' article "
            "requiring mandatory arbitration in counterparty's home venue. "
            "This is a wholly new section (not a modification of an existing standard section)."
        ),
        "rule_id": "no-counterparty-home-arbitration",
        "hunk": {
            "anchor": SEC_NEW,
            "kind": "inserted",
            "text": (
                "ARTICLE 12. DISPUTE RESOLUTION. Any dispute arising out of or "
                "relating to this Agreement shall be resolved by mandatory arbitration "
                "administered by AAA in the city where the Institution maintains its "
                "principal office."
            ),
        },
        "expected_fire": True,
    },
    {
        "case_id": "d2-new-section-insurance",
        "description": (
            "Counterparty inserts a standalone 'Insurance Requirements' article "
            "specifying minimum coverage levels exceeding Exos's standard program."
        ),
        "rule_id": "no-excess-insurance-levels",
        "hunk": {
            "anchor": SEC_NEW,
            "kind": "inserted",
            "text": (
                "ARTICLE 13. INSURANCE REQUIREMENTS. Exos shall maintain commercial "
                "general liability insurance with minimum coverage limits of $5,000,000 "
                "per occurrence. Exos shall provide certificates evidencing such "
                "insurance limits and coverage levels to the Institution upon request."
            ),
        },
        "expected_fire": True,
    },
    {
        "case_id": "d2-new-section-indemnification",
        "description": (
            "Counterparty inserts a standalone 'Indemnification' article requiring "
            "Exos to indemnify the institution. This is the primary scenario the issue "
            "describes: a not_in_standard topic whose on_insert rule cannot fire because "
            "the anchor model never defined what anchor a wholly new inserted section receives."
        ),
        "rule_id": "no-exos-indemnity",
        "hunk": {
            "anchor": SEC_NEW,
            "kind": "inserted",
            "text": (
                "ARTICLE 14. INDEMNIFICATION. Exos shall indemnify, defend, and hold "
                "harmless the Institution and its officers, employees, and agents from "
                "and against any and all claims, damages, losses, and expenses arising "
                "out of or resulting from the acts or omissions of Exos or the students "
                "placed by Exos at the Institution's facilities."
            ),
        },
        "expected_fire": True,
    },
]


def main():
    playbook = load_playbook()
    topic_map = build_topic_map(playbook)
    rule_map = build_rule_map(playbook)

    failures = []
    passes = []

    for case in D2_CASES:
        rule_id = case["rule_id"]
        rule = rule_map.get(rule_id)
        if rule is None:
            failures.append(
                f"MISSING RULE: case '{case['case_id']}' references rule '{rule_id}' "
                f"which does not exist in the playbook."
            )
            continue

        rule_anchors = compute_rule_anchors(rule, topic_map)
        fired = rule_fires_on_hunk(rule, case["hunk"], rule_anchors)
        expected = case["expected_fire"]

        if fired == expected:
            passes.append(
                f"PASS: case '{case['case_id']}': rule '{rule_id}' "
                f"{'fired' if fired else 'did not fire'} as expected."
            )
        else:
            details = []
            details.append(f"FAIL: case '{case['case_id']}': rule '{rule_id}'")
            details.append(f"  Description: {case['description']}")
            details.append(f"  Hunk anchor: {case['hunk']['anchor']}")
            details.append(f"  Effective rule anchors: {sorted(rule_anchors)}")
            details.append(
                f"  Hunk in scope: {hunk_in_scope(case['hunk']['anchor'], rule_anchors)}"
            )
            details.append(
                f"  Expected rule to fire: {expected}, actual: {fired}"
            )
            if not rule_anchors or (SEC_NEW not in rule_anchors and case["hunk"]["anchor"] == SEC_NEW):
                details.append(
                    f"  ROOT CAUSE: rule '{rule_id}' has no anchor covering '{SEC_NEW}' "
                    f"because the not_in_standard topics it references have empty "
                    f"section_anchors. The pseudo-anchor '{SEC_NEW}' is not yet defined."
                )
            failures.append("\n".join(details))

    print(f"D2 new-standalone-section planted violation tests")
    print(f"  Cases: {len(D2_CASES)}, Pass: {len(passes)}, Fail: {len(failures)}\n")

    for p in passes:
        print(p)
    for f in failures:
        print(f)
        print()

    if failures:
        print(
            f"\nTotal D2 failures: {len(failures)}\n"
            f"These failures confirm the dead-config bug described in issue #1.\n"
            f"Fix: define '{SEC_NEW}' as a reserved pseudo-anchor in schema.json, "
            f"ARCHITECTURE.md, and docs/playbook-governance.md; add it to the "
            f"section_anchors of every not_in_standard topic."
        )
        sys.exit(1)
    else:
        print(
            f"\nAll D2 new-section planted violations detected correctly."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
