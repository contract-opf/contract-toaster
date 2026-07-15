#!/usr/bin/env python3
"""
CI lint: acceptable-variations produce zero detector fires.

Per issue #2: the playbook contradicts itself because some acceptable_variations
describe text that contains trigger terms for hard-rejection rules. This lint
renders every acceptable_variations[].if/to text through the full on_insert
detector pass (simulating insertion in its topic's anchored section) and asserts
zero hard-rejection fires.

This is the RED test -- it fails today for:
  - indemnification: acceptable_variations use "indemnification" which fires no-exos-indemnity
  - insurance: acceptable_variations use "additional insured" which fires no-excess-insurance-levels

Issue #212: on_insert rule matching is now delegated to
scripts/detector_common.check_on_insert_rule_fires, the single shared
implementation of SPAN-level exempt_terms semantics (a trigger match is
suppressed only when that match's own span falls inside an exempt-phrase
span, not merely because an exempt phrase appears anywhere in the text),
instead of a local hunk-wide copy.

Issue #213: this lint only ever rendered acceptable_variations through the
on_insert pass -- the on_remove_or_alter half of hard_rejections[] (protects.
required_tokens Floor rules, e.g. preserve-liability-cap) was never simulated
here at all, so an acceptable_variation that necessarily deletes/alters a
protected required_token (e.g. limitation-of-liability's "$500,000 mutual
cap raise" variation vs. preserve-liability-cap's required_tokens=['$150,000',
'aggregate liability']) passed CI silently. This is the exact bug class #212
fixed for on_insert, recurring one layer down.

This lint now ALSO renders every acceptable_variations[].to text (only 'to' --
'if' describes the counterparty's proposal, never what we'd actually put in
the contract, so it is not a meaningful stand-in for the accepted/remaining
clause text an on_remove_or_alter rule reads) through
scripts/detector_common.check_on_remove_or_alter_rule_fires and asserts zero
fires -- UNLESS the variation is explicitly marked
acceptable_variations[].requires_attorney_override: true (schema.json,
issue #213). That marker documents a variation whose real-world implementation
deterministically fires a Floor rule (schema.json's hard_rejections[].protects
description is explicit that numeric-threshold/similar judgment calls are NOT
decided by this detector -- they stay with attorney review), so the Floor
correctly forces REQUEST_CHANGE and the acceptance path is the attorney's
disposition of that REQUEST_CHANGE (backend/src/disposition.py), never a
silent auto-accept. A variation marked true that does NOT actually fire
anything is stale documentation and is also a lint failure.

Issue #288: this lint now iterates every registry entry
(scripts/playbook_registry.py::list_playbook_ids/resolve_playbook) instead of
assuming a single hard-coded eiaa playbook, and SKIPs "knowledge" profile
entries explicitly (printed, never silent) -- see playbook_registry.profile().
Only "precision" profile entries are linted, so a precision entry (e.g. eiaa)
loses no enforcement.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402
from detector_common import check_on_insert_rule_fires as _check_on_insert_rule_fires  # noqa: E402
from detector_common import check_on_remove_or_alter_rule_fires as _check_on_remove_or_alter_rule_fires  # noqa: E402


def load_playbook(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def check_on_insert_rule_fires(
    rule: dict,
    variation_text: str,
    topic_id: str,
) -> list[dict]:
    """
    Simulate running an on_insert rule against a variation text inserted as a diff hunk.
    Returns a list of firing events (empty if no fires).
    """
    return [
        {
            "rule_id": fire["rule_id"],
            "trigger_term": fire["trigger_term"],
            "variation_text": variation_text[:120],
        }
        for fire in _check_on_insert_rule_fires(rule, variation_text, topic_id)
    ]


def check_on_remove_or_alter_rule_fires(
    rule: dict,
    variation_text: str,
    topic_id: str,
) -> list[dict]:
    """
    Simulate running an on_remove_or_alter rule against a variation's 'to'
    text standing in for the accepted/remaining clause text. Returns a list
    of firing events (empty if no fires).
    """
    return [
        {
            "rule_id": fire["rule_id"],
            "trigger_term": ",".join(fire["missing_tokens"]),
            "variation_text": variation_text[:120],
        }
        for fire in _check_on_remove_or_alter_rule_fires(rule, variation_text, topic_id)
    ]


def lint_playbook(playbook_path: Path, playbook_id: str) -> tuple[list[dict], list[dict]]:
    """Run the acceptable-variations lint against a single playbook file.
    Returns (all_fires, stale_overrides) -- both empty lists on a clean
    playbook. Each fire/stale dict is tagged with playbook_id."""
    playbook = load_playbook(playbook_path)

    hard_rejections = playbook.get("hard_rejections", [])
    topics = playbook.get("topics", [])

    all_fires = []
    stale_overrides = []

    for topic in topics:
        topic_id = topic["id"]
        acceptable_variations = topic.get("acceptable_variations", [])

        for variation in acceptable_variations:
            # Test both the 'if' (what counterparty might propose) and 'to' (what we'd accept)
            # The 'to' field is especially important - it describes what gets inserted in the draft
            for field_name in ("if", "to"):
                variation_text = variation.get(field_name, "")
                if not variation_text:
                    continue

                for rule in hard_rejections:
                    fires = check_on_insert_rule_fires(rule, variation_text, topic_id)
                    for fire in fires:
                        fire["playbook_id"] = playbook_id
                        fire["topic_id"] = topic_id
                        fire["variation_field"] = field_name
                        all_fires.append(fire)

            # on_remove_or_alter (issue #213): only the 'to' field is a meaningful
            # stand-in for the accepted/remaining clause text a protects.required_tokens
            # Floor rule reads -- 'if' is the counterparty's proposal, not our text.
            to_text = variation.get("to", "")
            requires_override = variation.get("requires_attorney_override", False)
            remove_or_alter_fires = []
            if to_text:
                for rule in hard_rejections:
                    fires = check_on_remove_or_alter_rule_fires(rule, to_text, topic_id)
                    for fire in fires:
                        fire["playbook_id"] = playbook_id
                        fire["topic_id"] = topic_id
                        fire["variation_field"] = "to"
                        remove_or_alter_fires.append(fire)

            if remove_or_alter_fires and not requires_override:
                all_fires.extend(remove_or_alter_fires)
            elif requires_override and not remove_or_alter_fires:
                stale_overrides.append({
                    "playbook_id": playbook_id,
                    "topic_id": topic_id,
                    "variation_text": to_text[:120],
                })

    return all_fires, stale_overrides


def main() -> int:
    all_fires: list[dict] = []
    stale_overrides: list[dict] = []
    ran_any = False

    for playbook_id in playbook_registry.list_playbook_ids():
        entry = playbook_registry.resolve_playbook(playbook_id)
        prof = playbook_registry.profile(entry)
        if prof == "knowledge":
            print(f"SKIP (knowledge profile): acceptable-variations-lint {playbook_id}")
            continue
        ran_any = True
        fires, stale = lint_playbook(entry.playbook_path, playbook_id)
        all_fires.extend(fires)
        stale_overrides.extend(stale)

    if not ran_any:
        print("NOTE: no precision-profile registry entries were found to lint.")

    if all_fires:
        print("FAIL: acceptable_variations produce hard-rejection detector fires.\n")
        print("The following acceptable variations fire hard-rejection rules:")
        for fire in all_fires:
            print(f"  playbook={fire['playbook_id']!r}  topic={fire['topic_id']!r}  field={fire['variation_field']!r}")
            print(f"  rule={fire['rule_id']!r}  trigger={fire['trigger_term']!r}")
            print(f"  text: {fire['variation_text']!r}")
            print()
        print(f"Total fires: {len(all_fires)}")
        print()
        print("Fix (on_insert fires): narrow trigger_terms or add exempt_terms to the firing rules.")
        print("Fix (on_remove_or_alter fires): if the fire is a genuine, unavoidable consequence of")
        print("implementing the variation (e.g. a numeric figure the Floor protects), mark the")
        print("acceptable_variations[] entry 'requires_attorney_override': true (schema.json, issue")
        print("#213) instead of silently letting the contradiction pass CI.")
        print("See: https://github.com/contract-opf/contract-toaster/issues/2")
        print("See: https://github.com/contract-opf/contract-toaster/issues/213")
        return 1

    if stale_overrides:
        print("FAIL: acceptable_variations marked requires_attorney_override but do not fire.\n")
        print("The following acceptable variations are marked 'requires_attorney_override': true")
        print("but produce zero on_remove_or_alter detector fires on their 'to' text -- the marker")
        print("is stale documentation and should be removed:")
        for stale in stale_overrides:
            print(f"  playbook={stale['playbook_id']!r}  topic={stale['topic_id']!r}")
            print(f"  text: {stale['variation_text']!r}")
            print()
        print("See: https://github.com/contract-opf/contract-toaster/issues/213")
        return 1

    print("PASS: zero acceptable-variation detector fires (all requires_attorney_override markers are live).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
