#!/usr/bin/env python3
"""
Red gate for issue #284: compose an OPF document's Evidence + Posture +
Floor knowledge into review system-prompt blocks (slice 2 of 5 of the #278
OPF-bind chain).

`scripts/opf_prompt.compose_opf_system_blocks` is a PURE function: no I/O,
no model call, no runtime wiring (that is a later slice). Checks, in order
(per the issue's "Acceptance criteria" + the 2026-07-12 update):

1. Blocks composed in the specified order (Posture, Evidence, Floor, then
   an optional Context block) from the slice-1 fixture
   (tests/fixtures/opf/synthetic-eiaa.opf.json, which has no
   perspective/de_minimis -- so 3 blocks); deterministic across two runs
   (byte-identical, i.e. `==`, on independently deep-copied input).
2. Exclusion list: none of `posture.rubric`, `posture.generation`,
   `corpus`, `compiler`, `identity`, `curation`, `baseline`, `composes`, or
   any `x_*`-prefixed key (nested inside `evidence`, engine #180) leaks
   into any composed block -- checked via sentinel strings planted in each
   of those sections.
3. A doc WITH `posture.rubric` produces byte-identical output to the same
   doc without it.
4. No 'Exos'/'EXOS' appears in the composed output for the synthetic
   fixture.
5. (2026-07-12 update, engine #177) An evidence field unrelated to the
   compose function's own enumeration -- negotiation dynamics
   (`proposed_by`, `counterparty_ref`, `negotiation_trail`) -- survives
   into the Evidence block untouched (forward-compat proof: evidence is
   projected wholesale, never selectively).
6. Context block (`perspective` + `de_minimis`) appears only when present
   in the source document.

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

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"


def _load_fixture() -> dict:
    return opf_load.load_opf(FIXTURE_PATH)


def check_1_block_order_and_determinism() -> list[str]:
    failures = []
    doc = _load_fixture()
    blocks = opf_prompt.compose_opf_system_blocks(doc)

    if len(blocks) != 3:
        failures.append(
            f"  [1] expected 3 blocks (no perspective/de_minimis in slice-1 fixture), got {len(blocks)}"
        )
        return failures

    posture_block, evidence_block, floor_block = blocks

    if posture_block != doc["posture"]["system_prompt"]:
        failures.append("  [1] posture block does not equal posture.system_prompt verbatim")

    try:
        evidence_json = json.loads(evidence_block)
    except json.JSONDecodeError as exc:
        failures.append(f"  [1] evidence block is not valid JSON: {exc}")
    else:
        if evidence_json != doc["evidence"]:
            failures.append("  [1] evidence block JSON does not round-trip the full evidence section")

    if "non-negotiable" not in floor_block:
        failures.append("  [1] floor block missing the fixed intro sentence")
    for invariant in doc["floor"]["invariants"]:
        if invariant["id"] not in floor_block:
            failures.append(f"  [1] floor block missing invariant id {invariant['id']!r}")
        if invariant["statement"] not in floor_block:
            failures.append(f"  [1] floor block missing invariant statement for {invariant['id']!r}")

    # Determinism: independently deep-copied doc -> byte-identical blocks.
    doc_copy = copy.deepcopy(doc)
    blocks_again = opf_prompt.compose_opf_system_blocks(doc_copy)
    if blocks != blocks_again:
        failures.append("  [1] compose_opf_system_blocks is not deterministic across two runs")

    return failures


def check_2_exclusions_sentinel() -> list[str]:
    failures = []
    doc = _load_fixture()

    doc["corpus"]["documents"][0]["title"] = "SENTINEL_CORPUS_MUST_NOT_LEAK"
    doc["compiler"]["name"] = "SENTINEL_COMPILER_MUST_NOT_LEAK"
    doc["baseline"]["notes"] = "SENTINEL_BASELINE_MUST_NOT_LEAK"
    doc["posture"]["rubric"] = {"note": "SENTINEL_RUBRIC_MUST_NOT_LEAK"}
    doc["posture"]["generation"] = {
        "generated_by": "SENTINEL_GENERATION_MUST_NOT_LEAK",
        "interview": [
            {"q": "q1", "question": "Q", "answer": "SENTINEL_INTERVIEW_MUST_NOT_LEAK"}
        ],
    }
    doc["identity"] = {
        "content_hash": "sha256:" + "0" * 64,
        "section_digests": {
            "evidence": "sha256:" + "1" * 64,
            "posture": "sha256:" + "1" * 64,
            "floor": "sha256:" + "1" * 64,
        },
        "id": "SENTINEL_IDENTITY_MUST_NOT_LEAK",
    }
    doc["curation"] = {
        "pins": [
            {
                "clause_id": "clause-liability-cap",
                "item_id": "C1",
                "position": "SENTINEL_CURATION_MUST_NOT_LEAK",
                "baseline_stance": "mixed",
                "pinned_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    doc["composes"] = [
        {
            "module": "sentinel-module",
            "version": "1.0.0",
            "integrity": "sha256:" + "2" * 64,
            "role": "SENTINEL_COMPOSES_MUST_NOT_LEAK",
        }
    ]
    doc["evidence"]["clauses"][0]["x_test_ext"] = {"note": "SENTINEL_XEXT_MUST_NOT_LEAK"}

    blocks = opf_prompt.compose_opf_system_blocks(doc)
    joined = "\n".join(blocks)

    sentinels = [
        "SENTINEL_CORPUS_MUST_NOT_LEAK",
        "SENTINEL_COMPILER_MUST_NOT_LEAK",
        "SENTINEL_BASELINE_MUST_NOT_LEAK",
        "SENTINEL_RUBRIC_MUST_NOT_LEAK",
        "SENTINEL_GENERATION_MUST_NOT_LEAK",
        "SENTINEL_INTERVIEW_MUST_NOT_LEAK",
        "SENTINEL_IDENTITY_MUST_NOT_LEAK",
        "SENTINEL_CURATION_MUST_NOT_LEAK",
        "SENTINEL_COMPOSES_MUST_NOT_LEAK",
        "SENTINEL_XEXT_MUST_NOT_LEAK",
    ]
    for sentinel in sentinels:
        if sentinel in joined:
            failures.append(f"  [2] {sentinel} leaked into composed blocks")

    return failures


def check_3_rubric_byte_identical() -> list[str]:
    failures = []
    doc_without = _load_fixture()
    doc_with = copy.deepcopy(doc_without)
    doc_with["posture"]["rubric"] = {"anything": "here", "weights": [1, 2, 3]}

    blocks_without = opf_prompt.compose_opf_system_blocks(doc_without)
    blocks_with = opf_prompt.compose_opf_system_blocks(doc_with)

    if blocks_without != blocks_with:
        failures.append(
            "  [3] a doc WITH posture.rubric did not produce byte-identical output to the same doc without it"
        )
    return failures


def check_4_no_debrand_leak() -> list[str]:
    failures = []
    doc = _load_fixture()
    blocks = opf_prompt.compose_opf_system_blocks(doc)
    joined = "\n".join(blocks)
    if "Exos" in joined or "EXOS" in joined:
        failures.append("  [4] composed output for the synthetic fixture contains 'Exos'/'EXOS'")
    return failures


def check_5_forward_compat_evidence_field_survives() -> list[str]:
    failures = []
    doc = _load_fixture()
    doc["evidence"]["clauses"][0]["observed_positions"][0]["counterparty_ref"] = {
        "alias": "SENTINEL_COUNTERPARTY_ALIAS_SURVIVES",
        "counterparty_type": "Educational Institution",
    }
    doc["evidence"]["clauses"][0]["observed_positions"][0]["proposed_by"] = "counterparty"
    doc["evidence"]["clauses"][0]["negotiation_trail"] = [
        {
            "document_id": "synthetic-doc-002",
            "round": 1,
            "moved_by": "counterparty",
            "change_summary": "SENTINEL_NEGOTIATION_TRAIL_SURVIVES",
            "ref": {"document_id": "synthetic-doc-002", "version": 1, "clause_path": "9.1"},
        }
    ]

    blocks = opf_prompt.compose_opf_system_blocks(doc)
    evidence_block = blocks[1]
    if "SENTINEL_COUNTERPARTY_ALIAS_SURVIVES" not in evidence_block:
        failures.append("  [5] unknown evidence field counterparty_ref.alias did not survive into the Evidence block")
    if "SENTINEL_NEGOTIATION_TRAIL_SURVIVES" not in evidence_block:
        failures.append("  [5] unknown evidence field negotiation_trail did not survive into the Evidence block")
    return failures


def check_6_context_block_only_when_present() -> list[str]:
    failures = []
    doc_without_context = _load_fixture()
    blocks_without = opf_prompt.compose_opf_system_blocks(doc_without_context)
    if len(blocks_without) != 3:
        failures.append(
            f"  [6] expected no Context block when perspective/de_minimis absent, got {len(blocks_without)} blocks"
        )

    doc_with_context = copy.deepcopy(doc_without_context)
    doc_with_context["perspective"] = {"party": "Our Org", "counterparty_type": "Educational Institution"}
    doc_with_context["de_minimis"] = ["typo fixes", "formatting-only changes"]
    blocks_with = opf_prompt.compose_opf_system_blocks(doc_with_context)
    if len(blocks_with) != 4:
        failures.append(
            f"  [6] expected a 4th Context block when perspective/de_minimis present, got {len(blocks_with)} blocks"
        )
    else:
        context_block = blocks_with[3]
        if "Our Org" not in context_block:
            failures.append("  [6] Context block missing perspective.party")
        if "typo fixes" not in context_block:
            failures.append("  [6] Context block missing de_minimis entry")

    return failures


def main() -> int:
    checks = [
        ("1", "block order + determinism from the slice-1 fixture", check_1_block_order_and_determinism),
        ("2", "exclusion list -- sentinels never leak into any block", check_2_exclusions_sentinel),
        ("3", "posture.rubric present vs absent -> byte-identical output", check_3_rubric_byte_identical),
        ("4", "no 'Exos'/'EXOS' in composed output", check_4_no_debrand_leak),
        ("5", "unknown evidence field survives (forward-compat)", check_5_forward_compat_evidence_field_survives),
        ("6", "Context block appears only when perspective/de_minimis present", check_6_context_block_only_when_present),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All OPF prompt-composition checks passed.")
        return 0
    else:
        print("One or more OPF prompt-composition checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
