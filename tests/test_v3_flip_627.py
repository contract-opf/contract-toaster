#!/usr/bin/env python3
"""
Slice test for issue #627: "switch both passes to v3 block-transcript
authoring (hard cutover)".

## The failure mode this file exists to prevent

Model-output-contract DRIFT. The output contract lives in two places that
must always agree: the PROMPT, which tells the model which envelope to emit
and which fields to fill, and the VALIDATOR, which decides whether what came
back is acceptable. This repo has already shipped a review pipeline where
those two disagreed -- every real review failed at `run_review` while CI
stayed green, because the fixtures were written against the validator and
nothing tested the prompt against it.

So the load-bearing assertion here is not "the prompt mentions v3" and it is
not "the validator loads v3". It is that the literal the prompt INSTRUCTS and
the const the ACTIVE artifact DEMANDS are the same string, checked in one
test, against the real prompt and the real artifact -- see
`test_the_instructed_envelope_and_the_active_validator_are_one_contract`.

## What this file asserts (the issue's Acceptance criteria)

  1. The assembled PRIMARY prompt carries the block-id markers, the
     transcript-authoring instructions, and the verbatim minimality
     instruction -- and carries NO `STANDARD_FORM_DIFF` / `ANCHORED_CLAUSES`
     blocks, in either pass.
  2. The instructed `schema_version` and the active validator artifact agree
     (both v3), asserted TOGETHER so they cannot drift apart again.
  3. `document_text_for_review` marks every logical paragraph with the SAME
     block id `build_block_map` keys that paragraph by -- the ids the model
     is told to name are the ids a transcript can actually resolve.
  4. Marker hygiene: a segment text and an issue field that open with a
     `"[p0007] "` marker are stripped by the deterministic backstop and the
     response validates, instead of failing the whole transcript as
     `source_mismatch`.
  5. A `source_mismatch` inside the pass buys EXACTLY ONE informed retry,
     whose correction block carries the divergence context from both sides.
  6. A fake v3 response drives the FULL pipeline (`review_spine.run_review`
     -- extract, primary, critic, reconcile, leakage, redline) to a
     delivered multi-edit tracked-changes `.docx`.
  7. `issue_key` uniqueness survives the MERGE of primary + critic +
     detector fires, not just one model response -- because under v3 the key
     is the only join between a proven edit and the issue that authored it,
     and a collision silently re-attributes the edit (and its footnote) to
     somebody else's issue while the review still reports OK.
  8. Stage 5.5 (the re-quote repair call sites) is gone from the spine.

## Fixture fidelity

Every block id and every `keep`/`delete` segment text in this file is
DERIVED from the synthetic `.docx` the test builds, by the same production
code the pipeline uses (`extraction_normalization_stage.build_block_map`),
never typed in. A hand-typed transcript would be exactly the unreachable
fixture the repo's own doctrine warns about: it would assert over a state no
document produces, and its green run would mean nothing. The one place a
literal marker string appears is the marker-poisoning test, where planting
the marker IS the scenario -- and even there the id comes from the real map.

Nothing here calls a live model. `FakeBedrockClient` returns canned v3
bodies; the ONLY paid check of real-model behavior is the owner's live run
after this lands (the issue's own Notes).

Run standalone: `python3 tests/test_v3_flip_627.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import block_transcript  # noqa: E402
import extraction_normalization_stage as ens  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import reconciliation as recon  # noqa: E402
import review_spine  # noqa: E402

# Cross-file reuse of the already-proven synthetic docx builder and the
# document-inspection helpers (same convention
# tests/test_doc_text_headings_fidelity.py uses to import from
# tests/test_review_spine.py).
from test_block_mode_e2e import (  # noqa: E402
    SECTIONS,
    _deleted_texts,
    _footnote_texts,
    _inserted_texts,
    _make_docx,
    _plain_text,
)

_PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"


def _bundle() -> dict[str, Any]:
    with open(_PLAYBOOK_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _paragraphs(docx_bytes: bytes) -> list[dict[str, Any]]:
    normalized = ens.extract_and_normalize(docx_bytes)
    assert normalized["status"] == "normalized", normalized
    return normalized["paragraphs"]


# ---------------------------------------------------------------------------
# 1 + 2. The prompt and the validator name ONE contract.
# ---------------------------------------------------------------------------


def test_the_instructed_envelope_and_the_active_validator_are_one_contract(
    failures: list[str],
) -> None:
    """The AC's anti-drift assertion, in ONE test on purpose.

    Two facts are read INDEPENDENTLY -- the prompt is read as assembled
    text, the artifact is read off disk -- and then compared. Splitting them
    across two tests is what let them drift before: each half can pass
    against a stale expectation of its own.
    """
    schema = pp.load_output_schema(pp.OUTPUT_SCHEMA_PATH)
    artifact_const = ((schema.get("properties") or {}).get("schema_version") or {}).get("const")

    if pp.OUTPUT_SCHEMA_PATH.name != "output-schema-v3.json":
        failures.append(
            f"[1a] the ACTIVE output contract must be v3 after the cutover; "
            f"primary_review_pass.OUTPUT_SCHEMA_PATH is {pp.OUTPUT_SCHEMA_PATH.name}"
        )
    if artifact_const != "output-schema-v3":
        failures.append(
            f"[1b] the active artifact's own schema_version const is {artifact_const!r}, "
            "expected 'output-schema-v3'"
        )

    overlay = pp.BINARY_DECISION_OVERLAY_BLOCK
    instructed = f'"schema_version" (string, exactly "{artifact_const}")'
    if instructed not in overlay:
        failures.append(
            f"[1c] DRIFT: the assembled prompt does not instruct the envelope literal the "
            f"ACTIVE validator demands. Expected the prompt to contain {instructed!r}."
        )
    # And the OTHER direction: no stale literal left anywhere in the block.
    for stale in ("output-schema-v1", "output-schema-v2"):
        if stale in overlay:
            failures.append(
                f"[1d] DRIFT: the assembled prompt still names {stale!r}, which the active "
                "validator rejects."
            )

    # The model-facing / provider-safe request projections are built from
    # the same artifact, or the REQUEST half drifts while the prompt and the
    # validator agree.
    import model_output_schema as mos

    if mos.OUTPUT_SCHEMA_PATH.name != pp.OUTPUT_SCHEMA_PATH.name:
        failures.append(
            f"[1e] model_output_schema defaults to {mos.OUTPUT_SCHEMA_PATH.name} while the "
            f"pass validates against {pp.OUTPUT_SCHEMA_PATH.name}"
        )

    # And the whole loop closes: a response that obeys the instruction the
    # prompt gives actually validates.
    ok, parsed = pp.validate_model_response(
        json.dumps(
            {
                "schema_version": artifact_const,
                "decision": "ACCEPT",
                "confidence_state": "OK",
                "issues": [],
                "block_patches": [],
                "block_ops": [],
            }
        )
    )
    if not ok:
        failures.append(
            f"[1f] a response carrying exactly the instructed envelope literal was rejected "
            f"by the active validator: {parsed}"
        )


def test_no_v2_field_instruction_survives_anywhere_in_the_assembled_prompt(
    failures: list[str],
) -> None:
    """AC-2, over the FULL system prompt rather than one constant.

    The overlay is not the whole output contract. `assemble_system_blocks`
    composes the Floor block, the replacement-text-modes block and the
    playbook's own projected `output_format` into the SAME system prompt,
    and each of those can instruct a field the v3 Issue forbids. Asserting
    over `BINARY_DECISION_OVERLAY_BLOCK` alone cannot see that: a block
    outside the overlay may contradict it and every offline test still
    passes, because `FakeBedrockClient` never reads the prompt.

    That is not hypothetical -- it is how the v3 cutover first shipped a
    Floor block still ordering `source_quote` (issue #627 review round 3).
    So this asserts over `render_system_prompt(assemble_system_blocks(...))`
    for a playbook carrying BOTH `hard_rejections` (renders the Floor block)
    and `topics` (renders the modes block), which is the only shape that
    composes every contract-bearing block at once.
    """
    playbook_path = (
        REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
    )
    playbook = json.loads(playbook_path.read_text(encoding="utf-8"))

    # The fixture must actually exercise both blocks, or this test silently
    # degrades into a weaker version of itself.
    if not playbook.get("hard_rejections"):
        failures.append(
            "[9a] fixture playbook carries no `hard_rejections`, so the Floor block never "
            "renders and this anti-drift test cannot see Floor-block drift."
        )
    if not playbook.get("topics"):
        failures.append(
            "[9b] fixture playbook carries no `topics`, so the replacement-text-modes block "
            "never renders and this anti-drift test cannot see modes-block drift."
        )

    prompt = pp.render_system_prompt(pp.assemble_system_blocks(playbook))

    # v1/v2 Issue fields the v3 contract forbids. Each may appear ONLY
    # inside the overlay's own prohibition of it -- anywhere else in the
    # assembled prompt is an instruction to emit a field
    # `output-schema-v3.json` rejects (`additionalProperties: false`).
    for field in ("source_quote", "proposed_replacement_text"):
        # The overlay writes the second clause lower-cased ("... and do NOT
        # include a ..."), so match case-insensitively rather than pinning
        # one capitalisation.
        prohibition = f'not include a "{field}" key'
        occurrences = prompt.count(field)
        allowed = pp.BINARY_DECISION_OVERLAY_BLOCK.count(field)
        if prohibition not in prompt.lower():
            failures.append(
                f"[9c] the assembled prompt never prohibits {field!r}; the overlay's "
                f"prohibition is the only place it may legitimately appear."
            )
        if occurrences > allowed:
            failures.append(
                f"[9d] DRIFT: {field!r} appears {occurrences}x in the ASSEMBLED prompt but "
                f"only {allowed}x in the overlay, so {occurrences - allowed} occurrence(s) "
                f"come from another block (Floor, replacement-text modes, or the playbook's "
                f"projected `output_format`) that still instructs a field the active v3 "
                f"validator rejects. A model obeying it fails validation, burns the single "
                f"retry, and terminates the review."
            )

    # And the required v3 join key must be instructed, not merely permitted.
    if '"issue_key"' not in prompt:
        failures.append(
            "[9e] the assembled prompt never instructs `issue_key`, which v3 makes REQUIRED "
            "on every Issue -- a model following this prompt emits a response the active "
            "validator rejects for a missing required property."
        )

    # The playbook's own projected output_format is prompt text too (it is in
    # PROMPT_KNOWLEDGE_KEYS), so a stale `every_issue_includes` instructs the
    # model just as directly as a hard-coded block does.
    every_issue_includes = (playbook.get("output_format") or {}).get("every_issue_includes")
    if isinstance(every_issue_includes, list):
        if "proposed_replacement_text" in every_issue_includes:
            failures.append(
                "[9f] the fixture playbook's `output_format.every_issue_includes` still names "
                "`proposed_replacement_text`, which the overlay forbids -- and this array is "
                "projected verbatim into both passes' prompts."
            )
        if "issue_key" not in every_issue_includes:
            failures.append(
                "[9g] the fixture playbook's `output_format.every_issue_includes` omits "
                "`issue_key`, which v3 requires on every Issue."
            )


def test_the_primary_prompt_teaches_block_transcript_authoring(failures: list[str]) -> None:
    overlay = pp.BINARY_DECISION_OVERLAY_BLOCK

    required = {
        "the block-id marker format": '"[p0001] "',
        "the marker no-copy rule": "NEVER copy the \"[pNNNN]\" markers into any output field",
        "the block_patches carrier": '"block_patches"',
        "the block_ops carrier": '"block_ops"',
        "one entry per block_id": "EXACTLY ONE entry per block_id",
        "the keep segment": '{"op": "keep", "text": "..."}',
        "the delete segment": '"op": "delete"',
        "the insert segment": '"op": "insert"',
        "issue_key on every issue": '"issue_key"',
        "delete_block": "delete_block",
        "insert_block_after": "insert_block_after",
        "the replacement_scope_note requirement": '"replacement_scope_note"',
    }
    for label, needle in required.items():
        if needle not in overlay:
            failures.append(f"[2a] the overlay does not state {label} ({needle!r} absent)")

    # VERBATIM, per the ticket: a paraphrase is a different instruction.
    minimality = (
        "Make the smallest coherent change that achieves the required legal "
        "outcome while preserving all acceptable language, structure, defined "
        "terms, drafting voice, and formatting. Do not make stylistic "
        "improvements or normalize the clause to house form."
    )
    if minimality not in overlay:
        failures.append(
            "[2b] the minimality instruction must appear VERBATIM in the assembled overlay"
        )
    if pp.MINIMALITY_INSTRUCTION != minimality:
        failures.append(
            f"[2c] MINIMALITY_INSTRUCTION drifted from the ticket's wording: "
            f"{pp.MINIMALITY_INSTRUCTION!r}"
        )

    # The v2 contract is un-asked, not merely un-validated: a prompt that
    # still asks for a source_quote would keep producing one for a schema
    # that forbids the key.
    if 'Do NOT include a "source_quote" key' not in overlay:
        failures.append("[2d] the overlay must explicitly forbid emitting a source_quote")


def test_the_dead_manifest_blocks_are_gone_from_both_passes(failures: list[str]) -> None:
    """`STANDARD_FORM_DIFF`/`ANCHORED_CLAUSES` were permanently empty from
    issue #380 and kept only for prompt-shape stability. The cutover rewrote
    the shape, so their rationale is spent."""
    primary_prompt = pp.assemble_user_prompt_primary(
        retrieved_precedent=[], doc_text="[p0001] Some clause text."
    )
    critic_prompt = pp.assemble_user_prompt_critic(
        primary_output={"decision": "ACCEPT", "issues": []},
        doc_text="[p0001] Some clause text.",
    )
    for label, prompt in (("primary", primary_prompt), ("critic", critic_prompt)):
        for tag in ("STANDARD_FORM_DIFF", "ANCHORED_CLAUSES"):
            if f"<{tag}>" in prompt:
                failures.append(f"[3a] the {label} prompt still composes a <{tag}> block")
            if tag in pp.UNTRUSTED_BEARING_TAGS:
                failures.append(
                    f"[3b] {tag} is still in UNTRUSTED_BEARING_TAGS; no assembler can emit it"
                )
    # The blocks that DO carry document text are untouched.
    if "<COUNTERPARTY_DOCUMENT>" not in primary_prompt:
        failures.append("[3c] the primary prompt lost its COUNTERPARTY_DOCUMENT block")
    if "<PRIMARY_REVIEWER_OUTPUT>" not in critic_prompt:
        failures.append("[3d] the critic prompt lost its PRIMARY_REVIEWER_OUTPUT block")


# ---------------------------------------------------------------------------
# 3. The ids the model reads are the ids a transcript can resolve.
# ---------------------------------------------------------------------------


def test_rendered_block_ids_match_the_block_map_exactly(failures: list[str]) -> None:
    docx_bytes = _make_docx(SECTIONS)
    paragraphs = _paragraphs(docx_bytes)
    block_map = ens.build_block_map(paragraphs)
    doc_text = review_spine.document_text_for_review(paragraphs)

    rendered_blocks = doc_text.split("\n\n")
    if len(rendered_blocks) != len(paragraphs):
        failures.append(
            f"[4a] setup: {len(rendered_blocks)} rendered blocks vs {len(paragraphs)} paragraphs"
        )
        return

    for block_id, rendered in zip(block_map, rendered_blocks):
        marker = f"[{block_id}] "
        # Issue #642: a heading-bearing paragraph renders its heading on its
        # own line ABOVE the marker ("## Heading\n[pNNNN] body"), so the
        # marker introduces the block's OWN TEXT rather than the block's first
        # line. What must hold is that the marker is present exactly once and
        # that everything after it is the block's provable text -- asserting
        # on "first line" was asserting on the layout, not the invariant.
        body_line = rendered.split("\n", 1)[-1] if rendered.startswith("## ") else rendered
        if not body_line.startswith(marker):
            failures.append(
                f"[4b] block {block_id} is not marked where its own text begins; rendered as "
                f"{rendered[:60]!r}"
            )
        # The marker is the ONLY thing added: strip it and the rest is what
        # the pre-#627 renderer produced.
        body = rendered[len(marker):] if rendered.startswith(marker) else rendered
        heading = (block_map[block_id].get("heading") or "").strip()
        if heading and heading != "<untitled>" and not body.startswith(f"## {heading}"):
            failures.append(
                f"[4c] block {block_id}: the heading line must follow the marker on the same "
                f"line; got {body[:60]!r}"
            )
        if block_map[block_id]["text"] not in body:
            failures.append(
                f"[4d] block {block_id}: the paragraph's own text is not in its rendered block"
            )

    # And the marker is orientation only -- it is in NO paragraph's text, so
    # a transcript proven against the block map can never contain one.
    for block_id, block in block_map.items():
        if "[p" in block["text"]:
            failures.append(f"[4e] block {block_id}'s real text contains a marker-shaped token")

    # The reader half (`primary_review_pass`) and the writer half
    # (`review_spine`) are duplicated constants -- pin them together.
    first_id = next(iter(block_map))
    written = review_spine.render_block_marker(first_id)
    if pp.RENDERED_BLOCK_MARKER_PATTERN.sub("", written + "TAIL") != "TAIL":
        failures.append(
            f"[4f] primary_review_pass.RENDERED_BLOCK_MARKER_PATTERN does not strip the marker "
            f"review_spine actually renders ({written!r})"
        )


# ---------------------------------------------------------------------------
# 4. Marker hygiene: the deterministic backstop.
# ---------------------------------------------------------------------------


def test_marker_poisoned_segments_and_fields_are_stripped_and_validate(
    failures: list[str],
) -> None:
    docx_bytes = _make_docx(SECTIONS)
    paragraphs = _paragraphs(docx_bytes)
    block_map = ens.build_block_map(paragraphs)
    block_id = next(iter(block_map))
    real_text = block_map[block_id]["text"]
    marker = f"[{block_id}] "

    head, _, tail = real_text.partition(" ")

    # The scenario: the model copied the marker it saw at the head of the
    # paragraph into the FIRST keep segment, and into a prose field. Both are
    # exactly what the prompt forbids and what a model does anyway.
    poisoned = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "issues": [
            {
                "issue_key": "I1",
                "section_ref": "1",
                "section_title": "Term and Fee",
                "counterparty_change_summary": f"{marker}The term was extended.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "The standard term is shorter.",
                "playbook_topic_id": "term-length",
                "internal_precedent_citation": None,
            }
        ],
        "block_patches": [
            {
                "block_id": block_id,
                "segments": [
                    {"op": "keep", "text": f"{marker}{head}"},
                    {"op": "delete", "text": " ", "issue_key": "I1"},
                    {"op": "insert", "text": "  ", "issue_key": "I1"},
                    {"op": "keep", "text": tail},
                ],
            }
        ],
        "block_ops": [],
    }

    ok, parsed = pp.validate_model_response(json.dumps(poisoned))
    if not ok:
        failures.append(f"[5a] a marker-poisoned response failed validation outright: {parsed}")
        return

    segment_texts = [seg["text"] for seg in parsed["block_patches"][0]["segments"]]
    if segment_texts[0] != head:
        failures.append(
            f"[5b] the leading block marker was NOT stripped from the keep segment; got "
            f"{segment_texts[0]!r}"
        )
    if parsed["issues"][0]["counterparty_change_summary"].startswith("["):
        failures.append(
            "[5c] the leading block marker was NOT stripped from an issue's prose field"
        )

    # The point of the backstop: the stripped transcript now PROVES against
    # the real document, where the poisoned one could not.
    proven = block_transcript.validate_block_patches(
        parsed["block_patches"], parsed["block_ops"], block_map
    )
    if proven["status"] != "proven":
        failures.append(
            f"[5d] the stripped transcript still does not prove against the document: "
            f"{proven['failures']}"
        )

    # Watch it fail without the backstop: the SAME transcript, unstripped,
    # is rejected -- so [5d] is not passing for some unrelated reason.
    unstripped = block_transcript.validate_block_patches(
        json.loads(json.dumps(poisoned))["block_patches"], [], block_map
    )
    if unstripped["status"] == "proven":
        failures.append(
            "[5e] the UNSTRIPPED marker-poisoned transcript proved anyway -- this test "
            "cannot detect a missing backstop"
        )


# ---------------------------------------------------------------------------
# 5. The informed retry.
# ---------------------------------------------------------------------------


def _mismatched_response(block_id: str, real_text: str) -> str:
    """A transcript that is schema-perfect and does NOT match the document:
    one word of the block retyped rather than copied. This is the single
    most likely real failure of the new contract, and the one an informed
    retry can actually fix."""
    head, _, tail = real_text.partition(" ")
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": "1",
                    "section_title": "Term and Fee",
                    "counterparty_change_summary": "The term was extended.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "The standard term is shorter.",
                    "playbook_topic_id": "term-length",
                    "internal_precedent_citation": None,
                }
            ],
            "block_patches": [
                {
                    "block_id": block_id,
                    "segments": [
                        # "MISQUOTED" is not in the document at this offset.
                        {"op": "keep", "text": "MISQUOTED"},
                        {"op": "delete", "text": " ", "issue_key": "I1"},
                        {"op": "insert", "text": "  ", "issue_key": "I1"},
                        {"op": "keep", "text": tail},
                    ],
                }
            ],
            "block_ops": [],
        }
    )


def _proving_response(block_id: str, real_text: str) -> str:
    head, _, tail = real_text.partition(" ")
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": "1",
                    "section_title": "Term and Fee",
                    "counterparty_change_summary": "The term was extended.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "The standard term is shorter.",
                    "playbook_topic_id": "term-length",
                    "internal_precedent_citation": None,
                }
            ],
            "block_patches": [
                {
                    "block_id": block_id,
                    "segments": [
                        {"op": "keep", "text": head},
                        {"op": "delete", "text": " ", "issue_key": "I1"},
                        {"op": "insert", "text": "  ", "issue_key": "I1"},
                        {"op": "keep", "text": tail},
                    ],
                }
            ],
            "block_ops": [],
        }
    )


_TEST_MODEL_ID = "anthropic.claude-opus-4-8"


def test_a_source_mismatch_buys_exactly_one_informed_retry(failures: list[str]) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_map = ens.build_block_map(_paragraphs(docx_bytes))
    block_id = next(iter(block_map))
    real_text = block_map[block_id]["text"]

    client = model_client.FakeBedrockClient(
        {
            _TEST_MODEL_ID: [
                _mismatched_response(block_id, real_text),
                _proving_response(block_id, real_text),
            ]
        }
    )
    ledger: list[Any] = []
    result = pp.run_primary_pass(
        review_id="review-627-informed-retry",
        retrieved_precedent=[],
        playbook=_bundle(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=ledger.append,
        doc_text=review_spine.document_text_for_review(_paragraphs(docx_bytes)),
        block_map=block_map,
    )

    if result.get("status") != "OK":
        failures.append(f"[6a] the retry did not recover the pass; got {result!r}")
        return
    if result.get("attempts") != 2:
        failures.append(
            f"[6b] a source_mismatch must buy EXACTLY ONE retry (2 attempts); got "
            f"{result.get('attempts')!r}"
        )
    if len(client.calls) != 2:
        failures.append(f"[6c] expected exactly 2 model invocations; got {len(client.calls)}")
        return

    # The correction actually reached the second attempt's prompt, and it
    # carries the divergence context -- not a bare "rejected".
    second = client.calls[1]["user_prompt"]
    if not isinstance(second, str):
        second = "\n".join(b.get("text", "") for b in second if isinstance(b, dict))
    first = client.calls[0]["user_prompt"]
    if not isinstance(first, str):
        first = "\n".join(b.get("text", "") for b in first if isinstance(b, dict))

    if pp.RETRY_CORRECTION_HEADING in first:
        failures.append("[6d] attempt 1's prompt must carry no correction block at all")
    if pp.RETRY_CORRECTION_HEADING not in second:
        failures.append("[6e] attempt 2's prompt carries no correction block")
        return
    correction = second[second.index(pp.RETRY_CORRECTION_HEADING):]

    if block_transcript.REASON_SOURCE_MISMATCH not in correction:
        failures.append(
            f"[6f] the correction does not name the rejection reason; got {correction[:400]!r}"
        )
    if block_id not in correction:
        failures.append("[6g] the correction does not name WHICH block diverged")
    if "the document has:" not in correction or "you transcribed:" not in correction:
        failures.append(
            "[6h] the correction must carry the divergence context from BOTH sides -- "
            "'informed retry' means the model can see where it parted from the document"
        )
    if "MISQUOTED" not in correction:
        failures.append(
            "[6i] the correction does not echo the model's own diverging transcription back "
            "to it, so it cannot tell which span to re-copy"
        )

    # Every attempt ledgered, with the rejected one carrying its token.
    if len(ledger) != 2:
        failures.append(f"[6j] expected 2 ledger rows (one per attempt); got {len(ledger)}")
        return
    if ledger[0].error_token != pp.BLOCK_TRANSCRIPT_ERROR_TOKEN:
        failures.append(
            f"[6k] the rejected attempt's ledger row must carry the transcript token; got "
            f"{ledger[0].error_token!r}"
        )


def test_a_transcript_that_never_proves_is_terminal_not_silently_accepted(
    failures: list[str],
) -> None:
    """Budget spent and still mismatched: fail the pass. Passing it on would
    burn the critic pass and the leakage scan to reach the same rejection at
    stage 5 with less information about why."""
    docx_bytes = _make_docx(SECTIONS)
    block_map = ens.build_block_map(_paragraphs(docx_bytes))
    block_id = next(iter(block_map))
    real_text = block_map[block_id]["text"]

    client = model_client.FakeBedrockClient(
        {
            _TEST_MODEL_ID: [
                _mismatched_response(block_id, real_text),
                _mismatched_response(block_id, real_text),
            ]
        }
    )
    result = pp.run_primary_pass(
        review_id="review-627-terminal",
        retrieved_precedent=[],
        playbook=_bundle(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=lambda record: None,
        doc_text="ignored",
        block_map=block_map,
    )
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[7a] expected a terminal pass result; got {result.get('status')!r}")
    if not str(result.get("last_error", "")).startswith(pp.BLOCK_TRANSCRIPT_ERROR_TOKEN):
        failures.append(f"[7b] the terminal result must name the transcript fault; got {result!r}")


def test_no_block_map_means_no_pre_check(failures: list[str]) -> None:
    """The pre-check is opt-in. A caller with no block map (every unit test
    of the pass itself) behaves exactly as it did before this issue -- the
    authoritative proof still runs at stage 5."""
    docx_bytes = _make_docx(SECTIONS)
    block_map = ens.build_block_map(_paragraphs(docx_bytes))
    block_id = next(iter(block_map))
    client = model_client.FakeBedrockClient(
        {_TEST_MODEL_ID: [_mismatched_response(block_id, block_map[block_id]["text"])]}
    )
    result = pp.run_primary_pass(
        review_id="review-627-no-map",
        retrieved_precedent=[],
        playbook=_bundle(),
        model_client=client,
        model_id=_TEST_MODEL_ID,
        ledger_write=lambda record: None,
        doc_text="ignored",
    )
    if result.get("status") != "OK" or result.get("attempts") != 1:
        failures.append(
            f"[8a] with no block_map the pass must accept the response in one attempt; "
            f"got {result.get('status')!r}/{result.get('attempts')!r}"
        )


# ---------------------------------------------------------------------------
# 6. The full pipeline, end to end.
# ---------------------------------------------------------------------------


def _full_pipeline_response(block_map: dict) -> str:
    """A v3 response whose transcript is DERIVED from the real block map:
    two issues editing ONE paragraph, plus a whole new clause after another.

    Every segment text below is sliced out of `block_map[...]["text"]`, so
    this fixture cannot drift from the document it addresses -- change the
    synthetic sections and this response follows.
    """
    ids = list(block_map)
    term_id, notice_id = ids[0], ids[1]
    term_text = block_map[term_id]["text"]

    # Two disjoint spans of the SAME paragraph, located in the real text.
    sixty_at = term_text.index("sixty (60)")
    hundred_at = term_text.index("one hundred dollars")
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "verdict_summary": "Three departures from your standard positions.",
            "issues": [
                {
                    "issue_key": "I1",
                    "section_ref": "Section 1. Term and Fee",
                    "section_title": "Section 1. Term and Fee",
                    "counterparty_change_summary": "The term was extended beyond your standard.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "The standard term is thirty days.",
                    "playbook_topic_id": "term-length",
                    "internal_precedent_citation": None,
                },
                {
                    "issue_key": "I2",
                    "section_ref": "Section 1. Term and Fee",
                    "section_title": "Section 1. Term and Fee",
                    "counterparty_change_summary": "The fee exceeds your standard rate.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "The standard fee is lower.",
                    "playbook_topic_id": "fee-cap",
                    "internal_precedent_citation": None,
                },
                {
                    "issue_key": "I3",
                    "section_ref": "Section 2. Notices",
                    "section_title": "Section 2. Notices",
                    "counterparty_change_summary": "Electronic notice is not permitted at all.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Electronic notice must be available.",
                    "playbook_topic_id": "notice-method",
                    "internal_precedent_citation": None,
                    "replacement_scope_note": "The provision is absent, so it is supplied whole.",
                },
            ],
            "block_patches": [
                {
                    "block_id": term_id,
                    "segments": [
                        {"op": "keep", "text": term_text[:sixty_at]},
                        {"op": "delete", "text": "sixty (60)", "issue_key": "I1"},
                        {"op": "insert", "text": "thirty (30)", "issue_key": "I1"},
                        {
                            "op": "keep",
                            "text": term_text[sixty_at + len("sixty (60)") : hundred_at],
                        },
                        {"op": "delete", "text": "one hundred dollars", "issue_key": "I2"},
                        {"op": "insert", "text": "fifty dollars", "issue_key": "I2"},
                        {"op": "keep", "text": term_text[hundred_at + len("one hundred dollars"):]},
                    ],
                }
            ],
            "block_ops": [
                {
                    "op": "insert_block_after",
                    "anchor_block_id": notice_id,
                    "new_text": "Notices may also be sent by electronic mail.",
                    "issue_key": "I3",
                }
            ],
        }
    )


def _critic_accept() -> str:
    return json.dumps(
        {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "issues": [],
            "block_patches": [],
            "block_ops": [],
            "critic_delta": None,
        }
    )


def test_a_fake_v3_response_drives_the_full_pipeline_to_a_multi_edit_redline(
    failures: list[str],
) -> None:
    docx_bytes = _make_docx(SECTIONS)
    block_map = ens.build_block_map(_paragraphs(docx_bytes))
    bundle = _bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]

    client = model_client.FakeBedrockClient(
        {
            primary_id: [_full_pipeline_response(block_map)],
            critic_id: [_critic_accept()],
        }
    )
    result = review_spine.run_review(
        docx_bytes, bundle, client, review_id="review-627-full-pipeline"
    )

    if result.get("status") != "OK":
        failures.append(f"[9a] the full pipeline did not complete: {result!r}")
        return
    if result.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[9b] expected REQUEST_CHANGE; got {result.get('decision')!r}")
    redline = result.get("redline_bytes")
    if not redline:
        failures.append("[9c] no redline document was delivered")
        return

    # The document really carries all three edits, as tracked changes.
    deleted = " ".join(_deleted_texts(redline))
    inserted = " ".join(_inserted_texts(redline))
    for needle in ("sixty (60)", "one hundred dollars"):
        if needle not in deleted:
            failures.append(f"[9d] {needle!r} is not struck through in the delivered document")
    for needle in ("thirty (30)", "fifty dollars", "electronic mail"):
        if needle not in inserted:
            failures.append(f"[9e] {needle!r} was not inserted into the delivered document")

    # Multi-edit really means one paragraph carrying two issues' edits.
    if len(_deleted_texts(redline)) < 2:
        failures.append("[9f] expected at least two separate tracked deletions")

    # The primary prompt the pipeline actually sent carried the markers the
    # response addressed -- the model-to-document interface, end to end.
    primary_call = next(c for c in client.calls if c["model_id"] == primary_id)
    sent = primary_call["user_prompt"]
    if not isinstance(sent, str):
        sent = "\n".join(b.get("text", "") for b in sent if isinstance(b, dict))
    for block_id in block_map:
        if f"[{block_id}] " not in sent:
            failures.append(f"[9g] the prompt sent to the model did not mark block {block_id}")

    # `proposed_replacement_text` is DERIVED, never model-authored: the
    # response above carries none at all and the findings still have it.
    findings = result.get("findings") or []
    derived = {f.get("issue_key"): f.get("proposed_replacement_text") for f in findings}
    if derived.get("I1") != "thirty (30)":
        failures.append(f"[9h] I1's replacement text was not derived from the transcript: {derived!r}")

    # And nothing about the counterparty document was lost.
    if "Effective Date" not in _plain_text(redline):
        failures.append("[9i] untouched contract text is missing from the delivered document")


# ---------------------------------------------------------------------------
# 6b. `issue_key` uniqueness has to survive the MERGE, not just one response.
#
# Under v3 the `issue_key` stopped being decoration: it is the ONLY thing
# joining a proven block edit to the issue that authored it
# (`redline_generate.generate_redline_from_blocks` builds `issues_by_key` and
# resolves every segment through it, LAST key wins). `primary_review_pass
# ._duplicate_issue_key_error` proves those keys are mutually unique WITHIN
# one model response -- it never sees a second one -- while `reconcile`
# merges THREE producers into one `issues` list. Both non-primary producers
# can therefore land on a key the primary already used, and the collision is
# silent: the review still reports OK, still delivers a document, and simply
# attributes the primary's edits (and the counterparty-facing footnote they
# carry) to somebody else's issue.
#
# The critic collision is the LIKELY one, not a contrived one: both passes
# read the same `BINARY_DECISION_OVERLAY_BLOCK`, which tells each of them to
# number its own issues "I1", "I2", "I3", ..., and `CRITIC_TASKING_BLOCK`
# never mentions `issue_key` at all -- so a critic that adds its first issue
# emits "I1", the key the primary's first issue almost always has.
# ---------------------------------------------------------------------------


def _primary_result_with_key(issue_key: str) -> dict[str, Any]:
    """A reconcile-shaped primary response body carrying ONE keyed issue."""
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [
            {
                "issue_key": issue_key,
                "section_ref": "Section 1. Term and Fee",
                "section_title": "Section 1. Term and Fee",
                "counterparty_change_summary": "The term was extended beyond your standard.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "The standard term is thirty days.",
                "playbook_topic_id": "term-length",
                "internal_precedent_citation": None,
                "provenance": "model",
            }
        ],
        "critic_delta": None,
        "verdict_summary": "One departure from your standard positions.",
    }


def _critic_result_adding_key(issue_key: str) -> dict[str, Any]:
    """A reconcile-shaped critic response body adding ONE issue under
    `issue_key`. The added issue's `(playbook_topic_id, section_ref)` is
    distinct from the primary's, so `_issue_key`'s topic-level dedupe does
    NOT swallow it -- the only thing being collided here is the handle."""
    return {
        "schema_version": recon.SCHEMA_VERSION,
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": {
            "added_issues": [
                {
                    "issue_key": issue_key,
                    "section_ref": "Section 3. Governing Law",
                    "section_title": "Section 3. Governing Law",
                    "counterparty_change_summary": "The forum was moved.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "The forum must stay in your state.",
                    "playbook_topic_id": "governing-law",
                    "internal_precedent_citation": None,
                    "provenance": "critic-added",
                }
            ],
            "contested_replacements": [],
            "rationale_objections": [],
        },
        "verdict_summary": None,
    }


def test_a_critic_added_issue_cannot_take_a_primary_issues_key(
    failures: list[str],
) -> None:
    """The critic's "I1" is re-keyed against the MERGED result; the
    primary's "I1" is the one that keeps it, because the primary's keys are
    what `block_patches` segments name."""
    merged = recon.reconcile(
        primary_result=_primary_result_with_key("I1"),
        critic_result=_critic_result_adding_key("I1"),
    )
    issues = merged["issues"]
    keys = [issue.get("issue_key") for issue in issues]
    if len(keys) != len(set(keys)):
        failures.append(
            f"[11a] the merged issues list carries a duplicate issue_key: {keys!r}"
        )
    by_topic = {issue.get("playbook_topic_id"): issue for issue in issues}
    if "governing-law" not in by_topic:
        failures.append(f"[11b] the critic-added issue never reached the merge: {keys!r}")
        return
    if by_topic.get("term-length", {}).get("issue_key") != "I1":
        failures.append(
            "[11c] the PRIMARY's key was moved instead of the critic's -- that orphans "
            f"every block_patches segment naming it: {keys!r}"
        )
    if by_topic["governing-law"].get("issue_key") != "I2":
        failures.append(
            "[11d] the critic-added issue was not re-keyed to the lowest unused index: "
            f"{by_topic['governing-law'].get('issue_key')!r}"
        )

    # The audit record and the merged issue are the SAME object, so the
    # critic_delta the UI renders cannot disagree with the issues list.
    recorded = (merged.get("critic_delta") or {}).get("added_issues") or []
    if not recorded:
        failures.append("[11e] critic_delta lost the added issue entirely")
    elif recorded[0].get("issue_key") != by_topic["governing-law"].get("issue_key"):
        failures.append(
            "[11f] critic_delta still records the pre-merge key "
            f"{recorded[0].get('issue_key')!r} while issues says "
            f"{by_topic['governing-law'].get('issue_key')!r}"
        )

    # A critic key that does NOT collide is left exactly as the model wrote
    # it -- re-keying is a collision repair, not a renumbering.
    untouched = recon.reconcile(
        primary_result=_primary_result_with_key("I1"),
        critic_result=_critic_result_adding_key("I7"),
    )
    added = [i for i in untouched["issues"] if i.get("playbook_topic_id") == "governing-law"]
    if not added or added[0].get("issue_key") != "I7":
        failures.append(
            f"[11g] a non-colliding critic key was renumbered anyway: {added!r}"
        )


def test_a_detector_fire_cannot_take_a_model_issues_key(failures: list[str]) -> None:
    """The other producer feeding the same merged list. A code-built fire
    arrives either unkeyed (nothing named it) or carrying a key minted
    without sight of the model's -- both have to be resolved against the
    merge, and neither had an assertion anywhere before this."""
    def _fire(topic: str, **extra: Any) -> dict[str, Any]:
        fire = {
            "section_ref": f"Section {topic}",
            "section_title": f"Section {topic}",
            "counterparty_change_summary": "Deterministic hard rejection.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "This term is not acceptable.",
            "playbook_topic_id": topic,
            "internal_precedent_citation": None,
            "provenance": "detector:synthetic-rule",
        }
        fire.update(extra)
        return fire

    merged = recon.reconcile(
        primary_result=_primary_result_with_key("I1"),
        detector_fires=[_fire("uncapped-liability", issue_key="I1"), _fire("assignment")],
    )
    keys = [issue.get("issue_key") for issue in merged["issues"]]
    if len(keys) != len(set(keys)) or None in keys or "" in keys:
        failures.append(
            f"[12a] detector fires did not get response-unique issue_keys: {keys!r}"
        )
    by_topic = {i.get("playbook_topic_id"): i.get("issue_key") for i in merged["issues"]}
    if by_topic.get("term-length") != "I1":
        failures.append(f"[12b] the model's own key was moved by a fire: {by_topic!r}")
    if by_topic.get("uncapped-liability") == "I1":
        failures.append(f"[12c] a colliding fire kept the model's key: {by_topic!r}")


def _critic_adds_colliding_issue() -> str:
    """The wire-shaped version of the same critic response: what the pass
    actually returns, keys and all, with the pipeline-stamped fields the
    model is never asked for left off."""
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "issues": [],
            "block_patches": [],
            "block_ops": [],
            "critic_delta": {
                "added_issues": [
                    {
                        "issue_key": "I1",
                        "section_ref": "Section 3. Governing Law",
                        "section_title": "Section 3. Governing Law",
                        "counterparty_change_summary": "The forum was moved.",
                        "decision": "REQUEST_CHANGE",
                        "external_rationale_for_footnote": (
                            "The forum must stay in your state."
                        ),
                        "playbook_topic_id": "governing-law",
                        "internal_precedent_citation": None,
                    }
                ],
                "contested_replacements": [],
                "rationale_objections": [],
            },
        }
    )


def test_a_colliding_critic_key_does_not_misattribute_the_delivered_redline(
    failures: list[str],
) -> None:
    """End to end, through `run_review`: the same primary transcript as the
    full-pipeline test, but the critic adds an issue keyed "I1". The
    delivered document must footnote the primary's own rationale on the
    primary's own edit."""
    docx_bytes = _make_docx(SECTIONS)
    block_map = ens.build_block_map(_paragraphs(docx_bytes))
    bundle = _bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]

    client = model_client.FakeBedrockClient(
        {
            primary_id: [_full_pipeline_response(block_map)],
            critic_id: [_critic_adds_colliding_issue()],
        }
    )
    result = review_spine.run_review(
        docx_bytes, bundle, client, review_id="review-627-key-collision"
    )
    if result.get("status") != "OK":
        failures.append(f"[13a] the pipeline did not complete: {result!r}")
        return
    redline = result.get("redline_bytes")
    if not redline:
        failures.append("[13b] no redline document was delivered")
        return

    findings = result.get("findings") or []
    keys = [f.get("issue_key") for f in findings]
    if len(keys) != len(set(keys)):
        failures.append(f"[13c] the delivered findings carry a duplicate issue_key: {keys!r}")

    by_topic = {f.get("playbook_topic_id"): f for f in findings}
    if by_topic.get("term-length", {}).get("proposed_replacement_text") != "thirty (30)":
        failures.append(
            "[13d] the primary's derived replacement text did not land on the primary's "
            f"issue: {[(f.get('playbook_topic_id'), f.get('proposed_replacement_text')) for f in findings]!r}"
        )
    if by_topic.get("governing-law", {}).get("proposed_replacement_text"):
        failures.append(
            "[13e] the critic-added issue (which authored no edit at all) was handed the "
            f"primary's derived replacement text: {by_topic['governing-law']!r}"
        )

    # The counterparty-facing footnote on the term edit explains the TERM,
    # not whatever the critic happened to key "I1".
    notes = " ".join(_footnote_texts(redline))
    if "The standard term is thirty days." not in notes:
        failures.append(
            f"[13f] the primary's own rationale is missing from the footnotes: {notes!r}"
        )


# ---------------------------------------------------------------------------
# 7. Stage 5.5 is gone.
# ---------------------------------------------------------------------------


def test_stage_five_and_a_half_is_gone_from_the_spine(failures: list[str]) -> None:
    """#627 removed the CALL SITES; #628 then deleted the module and its
    `REQUOTE_ENABLED` flag outright. Both halves are asserted here."""
    import ast

    tree = ast.parse((SCRIPTS_DIR / "review_spine.py").read_text(encoding="utf-8"))

    # Parsed, not grepped: this module's own docstring NARRATES the removal
    # (naming `requote_repair` and `config.requote_enabled()` to explain
    # where the stage went), and a substring search cannot tell that prose
    # from a live call -- it would fail on the very comment that documents
    # the fix. The AST sees only real code.
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for name in ("run_requote_repair", "requote_enabled", "revert_unrecovered",
                 "count_recovered"):
        if name in called:
            failures.append(f"[10a] review_spine still calls {name}()")

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    if "requote_repair" in imported:
        failures.append("[10b] review_spine still imports requote_repair")

    # Issue #628 finished the job: the module and its deployment flag are
    # gone, not merely unreferenced.
    if (SCRIPTS_DIR / "requote_repair.py").exists():
        failures.append(
            "[10c] scripts/requote_repair.py still exists; issue #628 deletes it"
        )
    import config as _config  # type: ignore

    if hasattr(_config, "requote_enabled"):
        failures.append(
            "[10d] backend/src/config.py still defines requote_enabled(); issue #628 "
            "removes the flag with the module it gated"
        )


TESTS = [
    test_the_instructed_envelope_and_the_active_validator_are_one_contract,
    test_the_primary_prompt_teaches_block_transcript_authoring,
    test_the_dead_manifest_blocks_are_gone_from_both_passes,
    test_rendered_block_ids_match_the_block_map_exactly,
    test_marker_poisoned_segments_and_fields_are_stripped_and_validate,
    test_a_source_mismatch_buys_exactly_one_informed_retry,
    test_a_transcript_that_never_proves_is_terminal_not_silently_accepted,
    test_no_block_map_means_no_pre_check,
    test_a_fake_v3_response_drives_the_full_pipeline_to_a_multi_edit_redline,
    test_a_critic_added_issue_cannot_take_a_primary_issues_key,
    test_a_detector_fire_cannot_take_a_model_issues_key,
    test_a_colliding_critic_key_does_not_misattribute_the_delivered_redline,
    test_stage_five_and_a_half_is_gone_from_the_spine,
    test_no_v2_field_instruction_survives_anywhere_in_the_assembled_prompt,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
    if failures:
        print("\nFAIL: v3 hard-cutover gate (issue #627).")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: all v3 hard-cutover (issue #627) assertions satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
