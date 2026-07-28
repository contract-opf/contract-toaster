#!/usr/bin/env python3
"""
Red gate: an empty Posture is never an empty block (PR F1, model-first spine).

## The defect this exists to catch

`_posture_block` ends `return str(posture.get("system_prompt", ""))`. The real
playbook ships `posture == {}`, so on the day-one production path that return
value is `""` -- and `compose_opf_system_blocks` puts it in `blocks[0]`
unconditionally. The review then runs with a knowledge block that occupies a
slot, is hashed and cached like knowledge, and carries no negotiation intent at
all. Every lineage field still records a playbook as having governed it.

## Two seams, two different answers, deliberately

  - `opf_prompt.compose_opf_system_blocks` is PURE COMPOSITION. It has no
    business making a governance decision, so it OMITS the block: absent, never
    empty-string (check 1). `blocks[0] == ""` must be unobservable.
  - `review_knowledge.resolve_knowledge` is the ONE place refusals live, so it
    RAISES `KnowledgeRefusal` (checks 2-4).

The unifying rule the refusal enforces: a review must carry prescriptive intent
from a governed, hashed, human-approved artifact -- `posture.system_prompt` in
playbook mode, the policy's rules in policy-only. When NEITHER exists there is
nothing prescriptive at all, and no flag can make that a review.

`accept_empty_posture` exists because the real playbook + its policy is NOT
intent-free: refusing it outright on day one would train operators to pass the
flag reflexively, which is how a fail-closed becomes a formality. So the flag
is scoped to exactly the "posture empty, policy present" case, and check 4
asserts every use of it WRITES A RECORD.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import review_knowledge  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "gold-fixtures-opf"
FULL_FIXTURE = FIXTURES / "acme-university.opf.json"
REAL_SHAPE_FIXTURE = FIXTURES / "acme-university-real-shape.opf.json"
POLICY_FIXTURE = FIXTURES / "acme-university-policy-v1.json"


def _load(path: Path) -> dict:
    return opf_load.load_opf(path)


def _policy() -> dict:
    with open(POLICY_FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _bundle(doc: dict) -> dict:
    """A minimal bundle_v2 view: resolve_knowledge reads `opf` off it."""
    return {"bundle_schema_version": 2, "playbook_id": "acme-university", "opf": copy.deepcopy(doc)}


def check_1_empty_posture_never_composes_an_empty_block() -> list[str]:
    """`blocks[0] != ""` must be unreachable -- there is no empty block to assert on."""
    failures = []
    doc = _load(REAL_SHAPE_FIXTURE)
    if doc.get("posture") != {}:
        failures.append("  [1] fixture precondition broken: real-shape fixture must ship posture == {}")
        return failures

    blocks = opf_prompt.compose_opf_system_blocks(doc, policy=_policy())
    if any(not b.strip() for b in blocks):
        empties = [i for i, b in enumerate(blocks) if not b.strip()]
        failures.append(
            f"  [1] posture == {{}} composed empty-string block(s) at index {empties}: the model "
            f"receives a knowledge slot with no negotiation intent in it"
        )
    # And the block is genuinely absent, not merely non-empty: the posture prose
    # from the FULL fixture must not have leaked in from anywhere.
    full_posture = _load(FULL_FIXTURE)["posture"]["system_prompt"]
    if any(full_posture in b for b in blocks):
        failures.append("  [1] posture prose appeared for a doc whose posture is {}")
    return failures


def check_2_refuses_when_posture_and_policy_are_both_empty() -> list[str]:
    """Nothing prescriptive at all. Refuse UNCONDITIONALLY: no flag reaches this."""
    failures = []
    doc = _load(REAL_SHAPE_FIXTURE)
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(doc),
            policy=None,
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        )
        failures.append(
            "  [2] posture == {} AND no policy: composed a review with no prescriptive intent "
            "from any governed artifact, instead of refusing"
        )
    except review_knowledge.KnowledgeRefusal:
        pass

    # Not even with every escape hatch set: this refusal has no remedy flag,
    # because there is nothing for a flag to accept.
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(doc),
            policy=None,
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
            accept_empty_posture=True,
            accept_stub_basis=True,
        )
        failures.append(
            "  [2] accept_empty_posture=True suppressed the no-intent-at-all refusal; that flag "
            "is scoped to 'posture empty, policy present' and must not reach this case"
        )
    except review_knowledge.KnowledgeRefusal:
        pass
    return failures


def check_3_refuses_empty_posture_with_policy_unless_accepted() -> list[str]:
    """The real playbook's day-one case: posture == {}, but a policy exists."""
    failures = []
    doc = _load(REAL_SHAPE_FIXTURE)
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(doc),
            policy=_policy(),
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        )
        failures.append(
            "  [3] posture == {} with a policy present must refuse by default (the operator has "
            "to say, on the record, that the policy carries the intent)"
        )
    except review_knowledge.KnowledgeRefusal:
        pass

    # ...and the flag is a real remedy, not decoration.
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(doc),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        accept_empty_posture=True,
    )
    if not knowledge.system_blocks():
        failures.append("  [3] accept_empty_posture=True resolved, but composed no blocks")
    return failures


def check_4_every_escape_writes_a_record() -> list[str]:
    """A refusal suppressed without a record is a defect: the review would look
    exactly like one that never had anything to suppress."""
    failures = []
    doc = _load(REAL_SHAPE_FIXTURE)
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(doc),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        accept_empty_posture=True,
    )
    record = knowledge.lineage_record()
    if record.get("accepted_empty_posture") is not True:
        failures.append(
            f"  [4] lineage_record() does not record accepted_empty_posture=True; got "
            f"{record.get('accepted_empty_posture')!r}. The escape is invisible downstream."
        )
    if record.get("posture_source") != "policy":
        failures.append(
            f"  [4] lineage_record() must say POSITIVELY where the review's intent came from; "
            f"posture_source={record.get('posture_source')!r}, expected 'policy'"
        )

    # The honest negative: a doc WITH posture prose records no escape at all.
    clean = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(_load(FULL_FIXTURE)),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    )
    clean_record = clean.lineage_record()
    if clean_record.get("accepted_empty_posture") is not False:
        failures.append(
            f"  [4] a doc with real posture prose recorded accepted_empty_posture="
            f"{clean_record.get('accepted_empty_posture')!r}, expected False"
        )
    if clean_record.get("posture_source") != "playbook":
        failures.append(
            f"  [4] a doc with real posture prose must record posture_source='playbook'; got "
            f"{clean_record.get('posture_source')!r}"
        )
    return failures


def check_5_content_hash_covers_composed_blocks() -> list[str]:
    """The hash must describe what was SENT, not what we meant to send.

    `primary_review_pass.projected_playbook_hash` hashes an INPUT VIEW -- the
    projection's source, not its rendering. Every defect in this PR (an empty
    posture block, FLOOR_INTRO over nothing) is invisible to a hash of that
    shape: the input view is unchanged while the model's actual prompt is a lie.
    Hashing the composed blocks closes that gap by construction.
    """
    failures = []
    doc = _load(FULL_FIXTURE)
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(doc),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    )
    baseline = knowledge.content_hash()
    if not baseline.startswith("sha256:"):
        failures.append(f"  [5] content_hash() is not a sha256: string; got {baseline!r}")
    if knowledge.content_hash() != baseline:
        failures.append("  [5] content_hash() is not stable across two calls")

    # Change ONLY the rendered text (a floor invariant's statement). An
    # input-view hash and a composed-blocks hash both move here...
    mutated_doc = copy.deepcopy(doc)
    mutated_doc["floor"]["invariants"][0]["statement"] = "A different, materially weaker invariant."
    mutated = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(mutated_doc),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    )
    if mutated.content_hash() == baseline:
        failures.append("  [5] content_hash() did not move when the composed prompt changed")

    # ...but the point: the hash is over the BLOCKS, so it is a function of the
    # rendering. Two knowledge objects whose blocks are equal hash equal.
    twin = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(_load(FULL_FIXTURE)),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    )
    if twin.system_blocks() == knowledge.system_blocks() and twin.content_hash() != baseline:
        failures.append("  [5] equal composed blocks produced different content_hash values")
    return failures


def main() -> int:
    checks = [
        ("1", "empty posture composes no empty-string block", check_1_empty_posture_never_composes_an_empty_block),
        ("2", "posture AND policy both empty -> unconditional refusal", check_2_refuses_when_posture_and_policy_are_both_empty),
        ("3", "empty posture + policy -> refuse unless accepted", check_3_refuses_empty_posture_with_policy_unless_accepted),
        ("4", "every escape writes a positive record", check_4_every_escape_writes_a_record),
        ("5", "content_hash covers the composed blocks", check_5_content_hash_covers_composed_blocks),
    ]

    overall_pass = True
    for code, name, fn in checks:
        try:
            failures = fn()
        except Exception as exc:  # noqa: BLE001
            failures = [f"  [{code}] raised {type(exc).__name__}: {exc}"]
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All posture-required checks passed.")
        return 0
    print("One or more posture-required checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
