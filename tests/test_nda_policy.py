#!/usr/bin/env python3
"""Harvest gate for playbooks/nda-policy-v1.json.

Same shape, and the same generic implementation, as tests/test_policy_document.py
-- which is the spec for this idiom and where the reasoning behind it lives. This
file is the NDA's SPEC: the dispositions, and the assertions that hold the
harvest to them.

## HARVESTED BUT NOT WIRED

The NDA playbook has no anchor_map_path in playbooks/registry.json, so it cannot
be reviewed in any mode, and its own source says it governs no production review.
It is harvested anyway, because the governing rule -- nothing in a source's
governance layer dies in the migration -- has no carve-out for the parts nobody
uses yet. "Nobody is using it" is the condition under which a silent drop goes
unnoticed, not a licence to allow one. check_9 below pins the NOT-WIRED half, so
that harvesting it cannot quietly become wiring it.

## The per-ITEM disposition this playbook is the reason for

The construct tables can only say "take all of general_principles" or "take none
of it". Neither is right here. general_principles[0] is purely a statement ABOUT
the artifact -- a placeholder stub, registered but inactive, whose content is
illustrative -- with no position in it at all. Rule `text` is rendered VERBATIM
into the model's binding instruction set, so harvesting it would tell a reviewing
model that the agreement in front of it is not real. Dropping it silently would
be the exact defect this gate exists to catch. NOT_HARVESTED_ITEMS is the third
option: dropped on purpose, with a reason, under the same "unlisted => FAIL"
discipline as everything else.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_policy_document as tpd  # noqa: E402

REPO_ROOT = TESTS_DIR.parent
PLAYBOOKS_DIR = REPO_ROOT / "playbooks"
POLICY_PATH = PLAYBOOKS_DIR / "nda-policy-v1.json"
SOURCE_PATH = PLAYBOOKS_DIR / "nda-v0.1.0.json"
REGISTRY_PATH = PLAYBOOKS_DIR / "registry.json"

# --------------------------------------------------------------------------
# Source construct dispositions. Unlisted => FAIL.
# --------------------------------------------------------------------------
HARVESTED_TOP_LEVEL = ("general_principles", "hard_rejections", "de_minimis_categories")
HARVESTED_TOPIC_KEYS = ("acceptable_variations", "must_preserve", "reject_if_proposed")

NOT_HARVESTED_TOP_LEVEL = {
    "$schema": "schema pointer, not content",
    "playbook": "identity/metadata. Carries NO legal_approval block (created_by: "
    "playbook-placeholder, status: draft), so there is no determination of record to carry into "
    "approval.carried_determinations",
    "decision_rubric": "the rubric is retired -- the model judges (launch decision; engine #178 "
    "removed posture.rubric). Unlike the Sample Agreement source, retiring it here drops no "
    "disposition: both accept_when entries restate de minimis or a floor harvested elsewhere",
    "topics": "container; each topic's keys are dispositioned by the topic tables below",
    "output_format": "output plumbing (field names, confidence states, footnote phrasing, citation "
    "rules), not a position we hold",
}
NOT_HARVESTED_TOPIC_KEYS = {
    "id": "structural",
    "section_ref": "structural: points into the standard form",
    "not_in_standard": "structural flag",
    "section_anchors": "retired lexical detector mechanics",
    "our_standard": "DESCRIBES a standard form that does not exist yet, rather than prescribing a "
    "review position. Its carve-out list is not lost: the carve-outs are named in the harvested "
    "reject_if_proposed item and reach the model via floor.no-perpetual-unbounded-confidentiality",
    "hard_rejection_refs": "cross-reference into hard_rejections, which is harvested in full",
    "replacement_text": "bounded-edit mechanics for the retired detector layer. Checked "
    "specifically because that layer's retirement leaves the model self-check as the only guard on "
    "replacement text: this topic is {'mode': 'none'} with NO must_not_introduce list and no bounds "
    "of any kind, so there is nothing for the retirement to drop and nothing to carry",
}

# --------------------------------------------------------------------------
# COVERAGE: every prescriptive source item -> the policy rule(s) carrying it.
# --------------------------------------------------------------------------
COVERAGE: dict[tpd.ItemKey, tuple[str, ...]] = {
    # -- general_principles: [0] is dropped on purpose, see NOT_HARVESTED_ITEMS.
    ("general_principles", "", 1): ("general.default-accept",),
    ("general_principles", "", 2): ("general.be-decisive",),
    # -- de_minimis_categories (3) ---------------------------------------
    # All three land on one rule that enumerates them: one definition, not three
    # positions. Note this policy has no rule conditioning on the term -- raised
    # in approval.flagged_for_approver rather than papered over.
    **{("de_minimis_categories", "", i): ("general.de-minimis-categories",) for i in range(3)},
    # -- hard_rejections (1) ---------------------------------------------
    ("hard_rejections", "", "nda-no-perpetual-unbounded-obligation"): (
        "floor.no-perpetual-unbounded-confidentiality",
    ),
    # -- topic: nda-confidentiality ---------------------------------------
    ("must_preserve", "nda-confidentiality", 0): ("confidentiality.preserve-mutuality",),
    ("must_preserve", "nda-confidentiality", 1): ("confidentiality.preserve-bounded-survival",),
    # The mirror of the hard_rejection; lands on the same floor rule.
    ("reject_if_proposed", "nda-confidentiality", 0): (
        "floor.no-perpetual-unbounded-confidentiality",
    ),
}

# --------------------------------------------------------------------------
# Items of a harvested construct that must NOT become rules, each with the
# reason. Unlisted => FAIL, exactly as for the construct tables: an item is in
# COVERAGE or here, never both, never neither.
# --------------------------------------------------------------------------
NOT_HARVESTED_ITEMS: dict[tpd.ItemKey, str] = {
    ("general_principles", "", 0): (
        "META, not a position: it states that this playbook is a registered-but-inactive "
        "'coming soon' placeholder carrying no release bundle, governing no production review, "
        "with illustrative content. There is no prescriptive content in the item to lose -- "
        "unlike the Sample Agreement's general_principles[0], which is a compound whose position "
        "half IS harvested. Rule `text` is rendered VERBATIM into the model's binding instruction "
        "set, so harvesting this would tell a reviewing model that the agreement in front of it "
        "is not real and its review does not count. Its content is preserved for humans in the "
        "policy's approval.note and description, which is the correct home for a statement about "
        "the artifact."
    ),
}

# A readable summary. Derived counts, not an independent source of truth.
EXPECTED_SOURCE_ITEM_COUNTS = {
    "general_principles": 3,
    "hard_rejections": 1,
    "de_minimis_categories": 3,
    "must_preserve": 2,
    "reject_if_proposed": 1,
    "acceptable_variations": 0,
}
EXPECTED_TOTAL_RULES = 6

SPEC = tpd.HarvestSpec(
    playbook_id="nda",
    policy_path=POLICY_PATH,
    source_path=SOURCE_PATH,
    harvested_top_level=HARVESTED_TOP_LEVEL,
    harvested_topic_keys=HARVESTED_TOPIC_KEYS,
    not_harvested_top_level=NOT_HARVESTED_TOP_LEVEL,
    not_harvested_topic_keys=NOT_HARVESTED_TOPIC_KEYS,
    coverage=COVERAGE,
    not_harvested_items=NOT_HARVESTED_ITEMS,
    expected_source_item_counts=EXPECTED_SOURCE_ITEM_COUNTS,
    expected_total_rules=EXPECTED_TOTAL_RULES,
)


def check_1_schema() -> list[str]:
    return tpd.schema_failures(SPEC)


def check_4_harvest_coverage() -> list[str]:
    return tpd.harvest_coverage_failures(SPEC)


def check_4a_no_meta_in_binding_text() -> list[str]:
    """No rule tells the model that the agreement it is reviewing is a stub.

    This is the whole point of dispositioning general_principles[0] out. `text`
    is rendered VERBATIM to the model, so the words that make this playbook a
    placeholder must not survive into a rule. If they ever do, the drop was
    undone and the model has been handed a reason not to take the review
    seriously.
    """
    failures: list[str] = []
    banned = ("placeholder", "coming soon", "illustrative", "stub", "demo", "not-yet-active")
    for rule in SPEC.policy()["rules"]:
        lowered = rule["text"].lower()
        for word in banned:
            if word in lowered:
                failures.append(
                    f"  rule {rule['id']!r} text contains {word!r}: rule text is BINDING "
                    f"instruction rendered verbatim to the model, and must not tell it that the "
                    f"agreement in front of it is not real. Statements about the artifact belong "
                    f"in approval.note"
                )
    return failures


def check_4c_flags_not_decided() -> list[str]:
    """The judgment calls this harvest refused to settle are recorded as open."""
    failures: list[str] = []
    doc = SPEC.policy()
    ids = {r["id"] for r in doc["rules"]}
    flags = doc["approval"].get("flagged_for_approver") or []
    flagged_ids = {f["id"] for f in flags}
    required = (
        "nda-content-is-illustrative-placeholder",
        "nda-floor-conjunction-ambiguity",
        "nda-de-minimis-definition-has-no-referent",
    )
    for req in required:
        if req not in flagged_ids:
            failures.append(
                f"  approval.flagged_for_approver is missing {req!r}: this is a judgment call the "
                f"harvest must surface rather than settle"
            )
    for f in flags:
        if not (f.get("detail") or "").strip():
            failures.append(f"  flag {f['id']!r} has no detail; an unexplained flag is not a question")
        for rid in f.get("rule_ids") or []:
            if rid not in ids:
                failures.append(f"  flag {f['id']!r} cites rule {rid!r}, which does not exist")
    return failures


def check_4d_no_determination_invented() -> list[str]:
    """No sign-off is claimed, because the source carries none."""
    failures: list[str] = []
    doc = SPEC.policy()
    if doc["approval"].get("carried_determinations"):
        failures.append(
            "  policy carries determinations, but the source has no legal_approval block to carry "
            "them from; a determination nobody made is worse than none"
        )
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    if "legal_approval" in (source.get("playbook") or {}):
        failures.append(
            "  the source now HAS a playbook.legal_approval block: it must be carried into "
            "approval.carried_determinations, and this check updated"
        )
    return failures


def check_4e_no_replacement_text_bounds_to_carry() -> list[str]:
    """The claim that there was nothing to carry is itself checked.

    scripts/replacement_text_enforcement.py is being retired, after which the
    model self-check is the only guard on what text reaches a contract. This
    harvest asserts the source carried no lexical replacement-text constraint
    for that retirement to drop; the assertion has to answer for itself.
    """
    failures: list[str] = []
    source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    for topic in source.get("topics") or []:
        spec = topic.get("replacement_text")
        if spec is None:
            continue
        if spec.get("must_not_introduce"):
            failures.append(
                f"  topic {topic['id']!r} now carries a must_not_introduce list. Its enforcement is "
                f"being retired, so it MUST be harvested as a policy rule or the guard is dropped "
                f"with no replacement"
            )
        if set(spec) - {"mode"} or spec.get("mode") != "none":
            failures.append(
                f"  topic {topic['id']!r} replacement_text is no longer a bare {{'mode': 'none'}} "
                f"({spec!r}); approval.note claims this source has no replacement-text bounds to "
                f"carry -- re-check that claim and re-harvest"
            )
    return failures


def check_5_debranded() -> list[str]:
    return tpd.debranded_failures(SPEC)


def check_6_not_falsely_approved() -> list[str]:
    return tpd.not_falsely_approved_failures(SPEC)


def check_7_hashing() -> list[str]:
    return tpd.hashing_failures(SPEC)


def check_8_provenance_resolves() -> list[str]:
    return tpd.provenance_resolves_failures(SPEC)


def check_9_harvested_but_not_wired() -> list[str]:
    """Harvesting the NDA did not wire it.

    The operator decision is explicit: harvest so the governance layer is whole
    and the source's deletion loses nothing, but do NOT wire it anywhere. The
    playbook has no anchor_map_path and so cannot be reviewed in any mode; its
    own source says it governs no production review. This pins that. If someone
    later gives the NDA an anchor map, that is a real decision to activate a
    contract type whose policy is unapproved placeholder text -- it should have
    to come past this check and past
    approval.flagged_for_approver['nda-content-is-illustrative-placeholder'],
    rather than arriving as a side effect.
    """
    failures: list[str] = []
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    entry = (registry.get("playbooks") or {}).get("nda") or {}
    if entry.get("anchor_map_path"):
        failures.append(
            "  registry.json now gives 'nda' an anchor_map_path, so it can be reviewed. This "
            "policy is HARVESTED BUT NOT WIRED and ships as draft placeholder content that no "
            "lawyer has reviewed; activating the contract type needs a real policy first"
        )
    if registry.get("default_playbook_id") == "nda":
        failures.append("  registry.json makes 'nda' the DEFAULT playbook; it is an inactive stub")
    doc = SPEC.policy()
    if doc["approval"]["status"] != "draft":
        failures.append(
            "  nda policy is no longer draft: its content is the source's own 'illustrative "
            "placeholder text', not positions anyone has taken"
        )
    return failures


def main() -> int:
    checks = [
        ("1", "policy validates against policy.schema.json", check_1_schema),
        ("4", "harvest coverage both ways: nothing dropped, nothing invented", check_4_harvest_coverage),
        ("4a", "no rule tells the model the agreement is a stub", check_4a_no_meta_in_binding_text),
        ("4c", "judgment calls are flagged, not decided by the harvest", check_4c_flags_not_decided),
        ("4d", "no determination is claimed that the source never made", check_4d_no_determination_invented),
        ("4e", "the source really has no replacement-text bounds to carry", check_4e_no_replacement_text_bounds_to_carry),
        ("5", "policy is debranded (no tenant-name literal)", check_5_debranded),
        ("6", "policy is draft, not falsely stamped approved", check_6_not_falsely_approved),
        ("7", "policy_content_hash deterministic and covers approval", check_7_hashing),
        ("8", "harvest provenance resolves to a revision in git history", check_8_provenance_resolves),
        ("9", "harvested but NOT wired: no anchor map, not default, still draft", check_9_harvested_but_not_wired),
    ]
    ok = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            ok = False
    print()
    if ok:
        print("All NDA policy checks passed.")
        return 0
    print("One or more NDA policy checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
