#!/usr/bin/env python3
"""
Red gate: a stub-basis playbook does not silently govern a review (PR F1).

`compiler.stub_basis_present` is the reference engine's watermark on
quick-compile output: a playbook that is STRUCTURALLY VALID BUT SEMANTICALLY
BLANK. It renders. It hashes. Its digest has clauses in it. A review driven by
one produces a lineage record identical in every field to a review governed by
a playbook compiled against a real corpus -- which is the whole problem, and
why the refusal is here rather than in a renderer.

## The misreading this test pins shut

`stub_basis_present` looks like it means "this playbook has no corpus behind
it", which invites the inverse reading: that `False` certifies a corpus. It
does not. It is a watermark the QUICK-COMPILE path writes, and a document that
genuinely has no knowledge in it is caught by the DIGEST check, flag or no flag
(check 3). Reading the flag as the corpus-less gate would leave the actual
corpus-less document -- the one with no digest at all -- to sail through on
`stub_basis_present: False`.

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
POLICY_FIXTURE = FIXTURES / "acme-university-policy-v1.json"


def _load(path: Path = FULL_FIXTURE) -> dict:
    return opf_load.load_opf(path)


def _policy() -> dict:
    with open(POLICY_FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _bundle(doc: dict) -> dict:
    return {"bundle_schema_version": 2, "playbook_id": "acme-university", "opf": copy.deepcopy(doc)}


def _stub_doc() -> dict:
    doc = _load()
    doc["compiler"]["stub_basis_present"] = True
    return doc


def check_1_stub_basis_refuses_by_default() -> list[str]:
    failures = []
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(_stub_doc()),
            policy=_policy(),
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        )
        failures.append(
            "  [1] stub_basis_present=True resolved without complaint: a semantically blank "
            "playbook would govern a review and the lineage would not say so"
        )
    except review_knowledge.KnowledgeRefusal as exc:
        if "stub_basis" not in str(exc):
            failures.append(f"  [1] refusal message does not name stub_basis_present: {exc}")
    return failures


def check_2_accept_stub_basis_resolves_and_records() -> list[str]:
    failures = []
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(_stub_doc()),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        accept_stub_basis=True,
    )
    if not knowledge.system_blocks():
        failures.append("  [2] accept_stub_basis=True resolved but composed no blocks")
    record = knowledge.lineage_record()
    if record.get("accepted_stub_basis") is not True:
        failures.append(
            f"  [2] lineage_record() does not record accepted_stub_basis=True; got "
            f"{record.get('accepted_stub_basis')!r}. An escape with no record is invisible: this "
            f"review is now indistinguishable from one governed by a real compile."
        )
    return failures


def check_3_flag_is_not_a_corpus_less_escape_hatch() -> list[str]:
    """The load-bearing one.

    A document with NO digest is the genuinely corpus-less case. It carries
    `stub_basis_present: False` -- the quick-compile path never touched it -- so
    if the flag were the corpus-less gate, this document would pass. It must
    still refuse, via the digest check.
    """
    failures = []
    corpus_less = _load()
    corpus_less.pop("digest", None)
    if corpus_less.get("compiler", {}).get("stub_basis_present") is not False:
        failures.append("  [3] fixture precondition broken: expected stub_basis_present False")
        return failures
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(corpus_less),
            policy=_policy(),
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        )
        failures.append(
            "  [3] a doc with NO digest and stub_basis_present=False resolved: the review would "
            "run on model judgement alone while lineage recorded a governing playbook"
        )
    except opf_prompt.PromptCompositionError:
        pass  # The digest check caught it, which is the point.
    except review_knowledge.KnowledgeRefusal:
        pass  # Also fail-closed; the refusal reached is what matters.

    # ...and accept_stub_basis must not buy it a pass either: that flag accepts
    # a WATERMARK, not an absence of knowledge.
    try:
        review_knowledge.resolve_knowledge(
            bundle_v2=_bundle(corpus_less),
            policy=_policy(),
            declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
            accept_stub_basis=True,
        )
        failures.append(
            "  [3] accept_stub_basis=True let a digest-less document through; the flag is not a "
            "corpus-less escape hatch"
        )
    except (opf_prompt.PromptCompositionError, review_knowledge.KnowledgeRefusal):
        pass
    return failures


def check_4_clean_doc_records_the_honest_negative() -> list[str]:
    """A real compile records accepted_stub_basis=False -- positively, not by omission."""
    failures = []
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle(_load()),
        policy=_policy(),
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    )
    record = knowledge.lineage_record()
    if record.get("accepted_stub_basis") is not False:
        failures.append(
            f"  [4] a normally-compiled playbook recorded accepted_stub_basis="
            f"{record.get('accepted_stub_basis')!r}, expected False"
        )
    if "accepted_stub_basis" not in record:
        failures.append("  [4] lineage_record() omits accepted_stub_basis entirely")
    return failures


def main() -> int:
    checks = [
        ("1", "stub_basis_present=True refuses by default", check_1_stub_basis_refuses_by_default),
        ("2", "accept_stub_basis=True resolves AND records", check_2_accept_stub_basis_resolves_and_records),
        ("3", "the flag is not a corpus-less escape hatch", check_3_flag_is_not_a_corpus_less_escape_hatch),
        ("4", "a clean doc records the honest negative", check_4_clean_doc_records_the_honest_negative),
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
        print("All stub-basis refusal checks passed.")
        return 0
    print("One or more stub-basis refusal checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
