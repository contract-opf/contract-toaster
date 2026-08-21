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
4. No tenant-brand strings appears in the composed output for the synthetic
   fixture.
5. (2026-07-16 update, OPF 0.3) The wholesale Evidence projection is
   RETIRED -- it measured ~1M tokens on the real corpus and could never
   reach a model. Bulk evidence (`full_text`, raw observation fields) must
   NOT appear in the prompt: the digest carries summaries only, and the
   lookup_clause_evidence drill-down tool that could fetch detail on demand
   is implemented but not wired into any tool loop (#580). The composed
   prompt must also stay inside the 50K review budget.
6. Context block (`perspective` + `de_minimis`) appears only when present
   in the source document.
7. (issue #579) Guard against naming an unbacked tool: while no tools are
   sent in the request (`structured_output_enabled()` off, the default),
   the composed prompt must not name any tool -- the model has no way to
   honor a call it cannot make. Keyed on the current fact that zero tools
   reach the request; a future change that wires a real tool loop back in
   must update this guard deliberately, not trip over it by accident.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
for _dir in (SCRIPTS_DIR, BACKEND_SRC):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import config  # noqa: E402
import model_client  # noqa: E402
import opf_clause_lookup  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import opf_terminology  # noqa: E402

# The 0.3 gold fixture: this module composes a DIGEST prompt, so its test must
# compose from a digest-bearing document. The 0.2 fixture has no digest section
# at all and now (correctly) raises — that back-compat is the loader's contract
# and is covered by test_opf_load / test_opf_ingest_03, not here.
FIXTURE_PATH = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
FIXTURE_02_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"


def _load_fixture() -> dict:
    return opf_load.load_opf(FIXTURE_PATH)


def check_1_block_order_and_determinism() -> list[str]:
    failures = []
    doc = _load_fixture()
    blocks = opf_prompt.compose_opf_system_blocks(doc)

    if len(blocks) != 4:
        failures.append(
            f"  [1] expected 4 blocks (0.3 fixture has perspective + de_minimis), got {len(blocks)}"
        )
        return failures

    # PR F1 fixed the order at POSTURE, BINDING, DIGEST, GUIDANCE, CONTEXT, so
    # the model reads what BINDS it before the precedent it weighs. The four
    # blocks below are the same four as before -- this fixture has posture
    # prose, one floor invariant, a digest and a context, and no policy -- only
    # their order changed. Note a block is now ABSENT rather than empty when it
    # has nothing to say, so this unpack is only safe because every one of the
    # four is known to be present for THIS fixture.
    posture_block, floor_block, digest_block, _context = blocks

    if posture_block != doc["posture"]["system_prompt"]:
        failures.append("  [1] posture block does not equal posture.system_prompt verbatim")

    # The digest block is PROSE under canonical headers, not a JSON dump: the
    # headers exist so `fallbacks`/`rejected` are not read as the judgements
    # they are not, and dumping JSON would hand the model those raw names.
    for term in opf_terminology.TERMS:
        if term.header not in digest_block:
            failures.append(f"  [1] digest block missing canonical header {term.header!r}")
    for clause in doc["digest"]["clauses"]:
        if f"clause_id: {clause['id']}" not in digest_block:
            failures.append(f"  [1] digest block missing clause_id {clause['id']!r}")

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
        failures.append("  [4] composed output for the synthetic fixture contains tenant-brand strings")
    return failures


def check_5_wholesale_evidence_is_retired() -> list[str]:
    """The inverse of what this check used to assert.

    It previously proved that an unanticipated `evidence` field survived into
    the prompt -- forward-compat by wholesale projection. That property IS the
    ~1M-token design: projecting everything is exactly why it could not fit a
    context window. OPF 0.3 replaced it with the engine-built digest, so the
    property is deliberately gone, and this now guards the replacement instead:
    bulk evidence must NOT reach the prompt.

    Forward-compat did not vanish, it moved: `digest_version` is dispatched on
    at ingest (scripts/opf_load.py), so a digest shape this composer does not
    understand is refused at the door rather than silently half-rendered here.
    Checked by tests/test_opf_ingest_03.py.
    """
    failures = []
    doc = _load_fixture()
    # Bulk fields that only exist in the full evidence section.
    doc["evidence"]["clauses"][0]["observed_positions"][0]["full_text"] = (
        "SENTINEL_FULL_TEXT_MUST_NOT_REACH_THE_PROMPT"
    )
    doc["evidence"]["clauses"][0]["observed_positions"][0]["counterparty_ref"] = {
        "alias": "SENTINEL_COUNTERPARTY_ALIAS",
    }

    joined = "\n".join(opf_prompt.compose_opf_system_blocks(doc))
    for sentinel, why in (
        ("SENTINEL_FULL_TEXT_MUST_NOT_REACH_THE_PROMPT",
         "full_text is the bulk of the corpus; the digest omits it by design and the "
         "lookup tool fetches it on demand"),
        ("SENTINEL_COUNTERPARTY_ALIAS",
         "raw evidence fields must not be projected wholesale"),
    ):
        if sentinel in joined:
            failures.append(f"  [5] evidence leaked into the prompt ({sentinel}): {why}")

    # And the block must be bounded, not merely smaller.
    tokens = len(joined) // 4
    if tokens >= 50_000:
        failures.append(f"  [5] composed prompt is {tokens} tokens, over the 50K review budget")
    return failures


def check_6_context_block_only_when_present() -> list[str]:
    failures = []
    # Strip explicitly rather than relying on the fixture happening to lack
    # them: the 0.3 gold fixture carries both.
    doc_without_context = _load_fixture()
    doc_without_context.pop("perspective", None)
    doc_without_context.pop("de_minimis", None)
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


def check_7_no_unbacked_tool_reference() -> list[str]:
    """Issue #579: DIGEST_INTRO used to instruct the model to call
    `lookup_clause_evidence` to verify exact clause language before relying
    on it. No tool is ever sent when `structured_output_enabled()` is off
    (the default, and the only path production drives today, per
    `backend/src/config.py`'s own docstring: no `tools`/`tool_choice`
    fields, no `tool_spec` kwarg reaches the client at all) -- so the model
    had no way to honor that instruction. Guard: while no tools are sent,
    the composed prompt must not name one.

    Keyed on the fact that no tool is currently sent: re-add the removed
    sentence (or any sentence naming a tool) and this fails; remove it and
    this passes.
    """
    failures: list[str] = []

    with patch.dict("os.environ", {}, clear=True):
        if config.structured_output_enabled():
            failures.append(
                "  [7] structured_output_enabled() is True with a cleared "
                "environment -- this guard's premise (no tools sent) does not "
                "hold, so it cannot check anything"
            )
            return failures

        doc = _load_fixture()
        joined = "\n".join(opf_prompt.compose_opf_system_blocks(doc))

    # The two tool names this codebase defines: the clause-lookup tool (never
    # wired to a tool loop) and the forced structured-output tool (sent only
    # when `structured_output_enabled()` is on, which this guard's premise
    # rules out). A composed prompt naming either -- or any future tool --
    # while no tool reaches the request asks the model to perform a
    # verification it structurally cannot.
    known_tool_names = {opf_clause_lookup.TOOL_NAME, model_client.STRUCTURED_OUTPUT_TOOL_NAME}
    for name in known_tool_names:
        if name in joined:
            failures.append(
                f"  [7] composed prompt names tool {name!r}, but no tool is sent "
                "in the request (structured_output_enabled() is off) -- the "
                "model is asked to call something it will never receive"
            )
    return failures


def main() -> int:
    checks = [
        ("1", "block order + determinism from the slice-1 fixture", check_1_block_order_and_determinism),
        ("2", "exclusion list -- sentinels never leak into any block", check_2_exclusions_sentinel),
        ("3", "posture.rubric present vs absent -> byte-identical output", check_3_rubric_byte_identical),
        ("4", "no tenant-brand strings in composed output", check_4_no_debrand_leak),
        ("5", "wholesale evidence retired: bulk must not reach the prompt", check_5_wholesale_evidence_is_retired),
        ("6", "Context block appears only when perspective/de_minimis present", check_6_context_block_only_when_present),
        ("7", "no unbacked tool reference while no tool is sent", check_7_no_unbacked_tool_reference),
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
