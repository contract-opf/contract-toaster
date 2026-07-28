#!/usr/bin/env python3
"""
Red gate: the Binding block is ABSENT when nothing binds (PR F1, model-first
spine).

## The defect this exists to catch

`compose_opf_system_blocks` builds the Floor block as `lines = [FLOOR_INTRO]`
and then appends one line per invariant. When there are no invariants the list
never grows, so the block ships as exactly:

    "The following invariants are non-negotiable. You must flag any clause
     that violates one. You cannot waive them."

...followed by nothing. That is not an edge case: the real playbook ships
`floor == {}`, so THIS IS THE DAY-ONE PRODUCTION PROMPT. The model is told a
list of unwaivable invariants follows, and then told nothing. Either it invents
the invariants or it learns that this prompt's promises are not load-bearing.

## Why the positive control is not optional

A renderer that ALWAYS drops the Binding block passes every absence assertion
below. This repo's recurring defect is precisely that shape -- the specified
check passing while the property is broken. So `check_3` asserts the block is
PRESENT, with its entries, whenever anything actually binds: a genesis floor
invariant, an `overrides.floor_additions` entry, or a policy `must` rule.

The property under test is therefore two-sided and states as one sentence:
the Binding block exists if and only if something binds.

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

FIXTURES = REPO_ROOT / "tests" / "gold-fixtures-opf"
FULL_FIXTURE = FIXTURES / "acme-university.opf.json"
EMPTY_FLOOR_FIXTURE = FIXTURES / "acme-university-empty-floor.opf.json"
REAL_SHAPE_FIXTURE = FIXTURES / "acme-university-real-shape.opf.json"
POLICY_FIXTURE = FIXTURES / "acme-university-policy-v1.json"

#: The retired prose. It is a lie over an empty list in BOTH halves: there are
#: no invariants to be non-negotiable about, and (see check_4) a policy `must`
#: rule is never "unwaivable" in the first place -- it is flagged for attorney
#: review when in tension, which is the opposite instruction.
FLOOR_INTRO_SENTINEL = "You cannot waive them"


def _load(path: Path) -> dict:
    return opf_load.load_opf(path)


def _policy(strengths: list[str]) -> dict:
    """The gold policy fixture, filtered to rules of the given strength(s)."""
    with open(POLICY_FIXTURE, encoding="utf-8") as f:
        doc = json.load(f)
    doc = copy.deepcopy(doc)
    doc["rules"] = [r for r in doc["rules"] if r["strength"] in strengths]
    return doc


def _binding_blocks(blocks: list[str]) -> list[str]:
    """Every block carrying binding-block prose, by its intro."""
    return [b for b in blocks if opf_prompt.BINDING_INTRO in b]


def check_1_no_binding_block_when_nothing_binds() -> list[str]:
    """Parametrized over the two shapes that reach production with no floor."""
    failures = []
    cases = [
        (
            "empty-floor fixture + should-only policy",
            _load(EMPTY_FLOOR_FIXTURE),
            _policy(["should"]),
        ),
        (
            "real-shape fixture (floor == {}) + no policy",
            _load(REAL_SHAPE_FIXTURE),
            None,
        ),
    ]
    for name, doc, policy in cases:
        blocks = opf_prompt.compose_opf_system_blocks(doc, policy=policy)
        binding = _binding_blocks(blocks)
        if binding:
            failures.append(
                f"  [1] {name}: nothing binds, but a Binding block was composed: {binding[0][:120]!r}"
            )
        for index, block in enumerate(blocks):
            if FLOOR_INTRO_SENTINEL in block:
                failures.append(
                    f"  [1] {name}: block[{index}] tells the model it faces invariants it "
                    f"cannot waive, over an empty list: {block[:160]!r}"
                )
    return failures


def check_2_no_empty_string_blocks() -> list[str]:
    """A block is ABSENT or it has content. An empty-string block is neither.

    The real-shape fixture ships `posture == {}`, which today composes to
    `blocks[0] == ""` -- a block that occupies a slot, is hashed and cached like
    knowledge, and says nothing.
    """
    failures = []
    for name, path in (
        ("full", FULL_FIXTURE),
        ("empty-floor", EMPTY_FLOOR_FIXTURE),
        ("real-shape", REAL_SHAPE_FIXTURE),
    ):
        blocks = opf_prompt.compose_opf_system_blocks(_load(path))
        for index, block in enumerate(blocks):
            if not block.strip():
                failures.append(
                    f"  [2] {name} fixture: block[{index}] is empty/whitespace "
                    f"({block!r}); blocks must be absent, never empty"
                )
    return failures


def check_3_positive_control_block_present_when_something_binds() -> list[str]:
    """THE control. Without it, "always drop the Binding block" passes checks 1-2.

    Three independent sources of a binding rule, each asserted to reach the
    model's prompt on its own.
    """
    failures = []

    # (a) A genesis floor invariant.
    full = _load(FULL_FIXTURE)
    blocks = opf_prompt.compose_opf_system_blocks(full)
    binding = _binding_blocks(blocks)
    if not binding:
        failures.append(
            "  [3a] the full fixture has a genesis floor invariant, but NO Binding block was "
            "composed -- a renderer that always drops the block would pass checks 1 and 2"
        )
    else:
        for invariant in full["floor"]["invariants"]:
            if invariant["statement"] not in binding[0]:
                failures.append(
                    f"  [3a] Binding block is missing genesis invariant {invariant['id']!r}'s statement"
                )

    # (b) An overrides.floor_additions entry, on a doc whose genesis floor is empty.
    addition = {
        "id": "no-perpetual-indemnity",
        "statement": "Never accept an indemnity that survives termination without a time limit.",
        "rationale": "Fixture floor addition.",
    }
    blocks = opf_prompt.compose_opf_system_blocks(
        _load(EMPTY_FLOOR_FIXTURE), overrides={"floor_additions": [addition]}
    )
    binding = _binding_blocks(blocks)
    if not binding:
        failures.append(
            "  [3b] a floor_additions override binds, but NO Binding block was composed"
        )
    elif addition["statement"] not in binding[0]:
        failures.append("  [3b] Binding block is missing the floor_additions statement")

    # (c) A policy `must` rule, on the real-shape doc (floor == {}): the ONLY
    #     thing that binds a real review today comes from the policy.
    must_policy = _policy(["must"])
    blocks = opf_prompt.compose_opf_system_blocks(_load(REAL_SHAPE_FIXTURE), policy=must_policy)
    binding = _binding_blocks(blocks)
    if not binding:
        failures.append(
            f"  [3c] a policy with {len(must_policy['rules'])} `must` rule(s) binds the "
            f"real-shape doc, but NO Binding block was composed"
        )
    else:
        for rule in must_policy["rules"]:
            if rule["text"] not in binding[0]:
                failures.append(f"  [3c] Binding block is missing policy must-rule {rule['id']!r}")
    return failures


def check_4_provenance_tags_and_honest_prose() -> list[str]:
    """Floor invariants and policy must-rules bind DIFFERENTLY; say so, and tag
    each entry so the self-check and the attribution manifest can cite them apart.

    A floor invariant is unwaivable. A policy `must` rule in tension with the
    clause at hand is FLAGGED FOR ATTORNEY REVIEW and never silently overridden
    in either direction (playbooks/policy.schema.json -> rules.items.strength;
    scripts/policy_load.py -> "Strength semantics"). Shipping the two under one "you cannot waive
    them" intro instructs the model to do the one thing the policy contract
    forbids.
    """
    failures = []
    doc = _load(FULL_FIXTURE)
    policy = _policy(["must"])
    blocks = opf_prompt.compose_opf_system_blocks(doc, policy=policy)
    binding = _binding_blocks(blocks)
    if not binding:
        failures.append("  [4] no Binding block composed for floor + policy musts")
        return failures
    block = binding[0]

    for invariant in doc["floor"]["invariants"]:
        if f"[floor:{invariant['id']}]" not in block:
            failures.append(
                f"  [4] genesis invariant {invariant['id']!r} is not tagged [floor:{invariant['id']}]"
            )
    for rule in policy["rules"]:
        if f"[policy:{rule['id']}]" not in block:
            failures.append(f"  [4] policy rule {rule['id']!r} is not tagged [policy:{rule['id']}]")

    if FLOOR_INTRO_SENTINEL in block:
        failures.append(
            "  [4] the Binding block still tells the model it cannot waive its entries, "
            "which is false for every policy must-rule in it"
        )
    # Assert the INSTRUCTION, not the vocabulary. A bare `"attorney" in block`
    # passes on prose that merely mentions attorneys in passing -- BINDING_INTRO
    # says "the determination is an attorney's to make" elsewhere, so deleting
    # the actual escalation instruction left that check green (caught by
    # mutation, which is why it now names the escalation and the action).
    lowered = block.lower()
    if "attorney review" not in lowered:
        failures.append(
            "  [4] the Binding block ships policy must-rules without naming ATTORNEY REVIEW as "
            "where a must-rule in tension goes -- the policy contract's defining property is "
            "missing from the only place the model can read it"
        )
    if "flag" not in lowered:
        failures.append(
            "  [4] the Binding block never tells the model to FLAG anything: a must-rule in "
            "tension has no escalation, so the model is left to resolve it silently"
        )
    if "silently" not in lowered:
        failures.append(
            "  [4] the Binding block does not forbid silently overriding/complying with a policy "
            "must-rule; 'never in EITHER direction' is the half operators get wrong"
        )
    return failures


def check_5_policy_text_verbatim() -> list[str]:
    """`text` reaches the model exactly as a human wrote it.

    `tests/test_policy_document.py` enforces that attorney-override
    determinations live IN a rule's `text` -- search it for "the model reads
    `text`, not approval metadata" -- precisely because that is the only field
    the model ever sees. Paraphrasing or truncating here would silently void
    that guarantee, so rendering is asserted to be byte-exact.
    """
    failures = []
    policy = _policy(["must", "should"])
    long_rule = {
        "id": "fixture.long-verbatim-rule",
        "strength": "must",
        "topic": "indemnification",
        "text": (
            "Per the 2026-01-01 determination of record, a narrow mutual negligence-based "
            "indemnity may be accepted ONLY on an attorney's express disposition; it is never "
            "a silent auto-accept, and the reviewer must reproduce this sentence in the flag."
        ),
    }
    policy["rules"] = policy["rules"] + [long_rule]
    blocks = opf_prompt.compose_opf_system_blocks(_load(FULL_FIXTURE), policy=policy)
    joined = "\n".join(blocks)
    for rule in policy["rules"]:
        if rule["text"] not in joined:
            failures.append(
                f"  [5] policy rule {rule['id']!r} ({rule['strength']}) was not rendered verbatim; "
                f"a paraphrased/truncated rule voids the 'the model reads text' guarantee"
            )
    return failures


def check_6_defaults_are_todays_behavior() -> list[str]:
    """The new `policy`/`mode` params default to today's composition.

    F1 is the additive half of the spine: a caller that has not been taught about
    policy yet must get byte-identical blocks.
    """
    failures = []
    doc = _load(FULL_FIXTURE)
    if opf_prompt.compose_opf_system_blocks(doc) != opf_prompt.compose_opf_system_blocks(
        doc, policy=None
    ):
        failures.append("  [6] policy=None is not identical to omitting the parameter")
    if opf_prompt.compose_opf_system_blocks(doc) != opf_prompt.compose_opf_system_blocks(
        copy.deepcopy(doc)
    ):
        failures.append("  [6] composition is not deterministic across two runs")

    # The no-digest refusal must be UNCHANGED by default.
    no_digest = _load(FULL_FIXTURE)
    no_digest.pop("digest", None)
    try:
        opf_prompt.compose_opf_system_blocks(no_digest)
        failures.append("  [6] a doc with no digest must still raise PromptCompositionError")
    except opf_prompt.PromptCompositionError:
        pass
    return failures


def main() -> int:
    checks = [
        ("1", "no Binding block when nothing binds", check_1_no_binding_block_when_nothing_binds),
        ("2", "blocks are absent, never empty-string", check_2_no_empty_string_blocks),
        ("3", "POSITIVE CONTROL: Binding block present when something binds",
         check_3_positive_control_block_present_when_something_binds),
        ("4", "provenance tags + prose honest about floor vs policy-must",
         check_4_provenance_tags_and_honest_prose),
        ("5", "policy rule text rendered verbatim", check_5_policy_text_verbatim),
        ("6", "new params default to today's behavior", check_6_defaults_are_todays_behavior),
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
        print("All Binding-block absence checks passed.")
        return 0
    print("One or more Binding-block absence checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
