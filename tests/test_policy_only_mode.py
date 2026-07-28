#!/usr/bin/env python3
"""
Red gate: policy-only is DECLARED, never inferred (PR F1, model-first spine).

A policy-only review -- policy rules, no corpus digest -- is legitimate: a
document type with no compiled corpus behind it can still be reviewed against
approved rules, and saying so honestly is better than pretending a playbook
governed it.

## The one thing that must never happen

Policy-only must never be REACHED BY INFERENCE. "No digest? Must be policy-only,
then." is the same defect in a new costume: a misconfigured playbook -- a failed
compile, a bad upload, an OPF 0.2 document on a 0.3 path -- would quietly
degrade into a corpus-less review, and every lineage field would still record a
playbook as having governed it. The difference between "we chose to review on
policy alone" and "the knowledge silently went missing" is invisible downstream
unless the mode is a POSITIVE DECLARATION made before the data is inspected.

So `declared_mode` is a required keyword with no default, a digest-less document
in playbook mode still raises `PromptCompositionError` (check 3), and the
lineage record states `playbook_evidence: "none"` as a claim rather than leaving
it to be inferred from an absence (check 2).

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


def _load(path: Path = FULL_FIXTURE) -> dict:
    return opf_load.load_opf(path)


def _policy() -> dict:
    with open(POLICY_FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _bundle(doc: dict) -> dict:
    return {"bundle_schema_version": 2, "playbook_id": "acme-university", "opf": copy.deepcopy(doc)}


def _resolve_policy_only(doc: dict | None = None, policy: dict | None = None):
    return review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(doc if doc is not None else _load(REAL_SHAPE_FIXTURE)),
        policy=policy if policy is not None else _policy(),
        declared_mode=review_knowledge.MODE_POLICY_ONLY,
    )


def check_1_policy_only_composes_no_digest_block() -> list[str]:
    failures = []
    knowledge = _resolve_policy_only()
    blocks = knowledge.system_blocks()
    if not blocks:
        failures.append("  [1] policy-only composed no blocks at all")
        return failures
    joined = "\n".join(b["text"] for b in blocks)
    if opf_prompt.DIGEST_INTRO in joined:
        failures.append(
            "  [1] policy-only composed a Digest block: the mode's entire meaning is that no "
            "corpus precedent reaches the model"
        )
    # The policy's rules DID reach it -- otherwise "no digest" is trivially true
    # of a prompt with nothing in it.
    must_ids = [r["id"] for r in knowledge.must_rules()]
    if not must_ids:
        failures.append("  [1] policy-only resolved with no must rules reaching the prompt")
    for rule in _policy()["rules"]:
        if rule["text"] not in joined:
            failures.append(f"  [1] policy-only prompt is missing rule {rule['id']!r} verbatim")
    return failures


def check_2_lineage_states_the_absence_positively() -> list[str]:
    failures = []
    record = _resolve_policy_only().lineage_record()
    if record.get("review_knowledge_mode") != "policy_only":
        failures.append(
            f"  [2] review_knowledge_mode is {record.get('review_knowledge_mode')!r}, expected "
            f"'policy_only'"
        )
    if record.get("playbook_evidence") != "none":
        failures.append(
            f"  [2] playbook_evidence is {record.get('playbook_evidence')!r}, expected 'none'. "
            f"A downstream reader must be TOLD no corpus governed this review, not left to "
            f"infer it from a missing field."
        )
    # The contrast that makes the claim mean something.
    playbook_record = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(_load()),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    ).lineage_record()
    if playbook_record.get("playbook_evidence") != "digest":
        failures.append(
            f"  [2] a real playbook review recorded playbook_evidence="
            f"{playbook_record.get('playbook_evidence')!r}, expected 'digest'"
        )
    return failures


def check_3_undeclared_policy_only_raises() -> list[str]:
    """A digest-less doc in playbook mode must NOT degrade into policy-only."""
    failures = []
    no_digest = _load()
    no_digest.pop("digest", None)
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(no_digest),
            policy=_policy(),
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        )
        failures.append(
            "  [3] a digest-less doc in playbook mode resolved: the review silently became "
            "policy-only, which is the inference this mode exists to prevent"
        )
    except opf_prompt.PromptCompositionError:
        pass

    # And the mode cannot be smuggled in as an unknown/None string.
    for bogus in (None, "", "policy", "POLICY_ONLY", "v1_projection"):
        try:
            review_knowledge.resolve_knowledge(
                bundle_v2=_bundle(_load()),
                policy=_policy(),
                declared_mode=bogus,
            )
            failures.append(f"  [3] declared_mode={bogus!r} was accepted as a real mode")
        except review_knowledge.KnowledgeRefusal:
            pass
    return failures


def check_4_policy_only_with_zero_rules_raises() -> list[str]:
    failures = []
    for name, policy in (
        ("no policy at all", None),
        ("policy with an empty rules list", {"playbook_id": "acme-university", "version": 1, "rules": []}),
    ):
        try:
            _resolve_policy_only(policy=policy if policy is not None else {"rules": []})
            failures.append(
                f"  [4] policy-only with {name} resolved: no corpus AND no rules is a bare model "
                f"call recorded as a governed review"
            )
        except review_knowledge.KnowledgeRefusal:
            pass
    # Explicitly None, too (the kwarg default path).
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(_load(REAL_SHAPE_FIXTURE)),
            policy=None,
            declared_mode=review_knowledge.MODE_POLICY_ONLY,
        )
        failures.append("  [4] policy-only with policy=None resolved")
    except review_knowledge.KnowledgeRefusal:
        pass
    return failures


def check_5_cache_breakpoint_on_last_static_block() -> list[str]:
    failures = []
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(_load()),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    )
    blocks = knowledge.system_blocks()
    if not blocks:
        failures.append("  [5] no blocks composed")
        return failures
    if blocks[-1].get("cache_control") != {"type": "ephemeral"}:
        failures.append(
            f"  [5] last block must carry cache_control={{'type': 'ephemeral'}}; got "
            f"{blocks[-1].get('cache_control')!r}"
        )
    for index, block in enumerate(blocks[:-1]):
        if "cache_control" in block:
            failures.append(
                f"  [5] block[{index}] carries cache_control; only the last static knowledge "
                f"block may hold the breakpoint"
            )
    for index, block in enumerate(blocks):
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            failures.append(f"  [5] block[{index}] is not an Anthropic-shaped text block: {block!r}")
    return failures


def main() -> int:
    checks = [
        ("1", "policy-only composes NO digest block", check_1_policy_only_composes_no_digest_block),
        ("2", "lineage states the absence positively", check_2_lineage_states_the_absence_positively),
        ("3", "undeclared policy-only raises, never degrades", check_3_undeclared_policy_only_raises),
        ("4", "policy-only with zero rules raises", check_4_policy_only_with_zero_rules_raises),
        ("5", "cache breakpoint on the last static block", check_5_cache_breakpoint_on_last_static_block),
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
        print("All policy-only mode checks passed.")
        return 0
    print("One or more policy-only mode checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
