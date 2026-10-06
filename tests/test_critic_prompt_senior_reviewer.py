#!/usr/bin/env python3
"""
Issue #137 (epic #134, ADR 0001): the critic prompt frames the critic as the
SENIOR reviewer with the last word, and tells it to re-review sceptically.

`tests/test_critic_document_context.py` (#618) pins what the old "adversarial
second reader" tasking said. ADR 0001 changed the critic's job from flagging to
authoring the final result, so the prompt has to say so -- and, because the
critic is the pass with the most information and the last word, it has to guard
against the failure that authority invites: ANCHORING on the first reviewer's
answer. The sceptical-review instruction therefore fixes an ORDER (derive every
issue from the document and the playbook FIRST, compare SECOND) and a BAR
(override what is wrong, weak or non-compliant; never reword what is sound).

## What this file proves

Asserted on the REAL composed request -- the system and user prompt
`run_critic_pass` actually sends, captured from `FakeBedrockClient.calls` over
the same synthetic-generic bundle `tests/test_critic_failure_reason_665.py`
drives -- never on a constant standing in for it:

  1. The user prompt carries the document text, the first reviewer's full
     output (its issues AND its block transcript), and the sceptical-review
     instruction, in the order derive-then-compare: the instruction precedes
     both data blocks, and the document precedes the first reviewer's output.
  2. The sceptical instruction states the phrase this ticket fixes: re-derive
     every issue from the document and the playbook FIRST, compare SECOND,
     override where the first reviewer is wrong, weak, or non-compliant with
     the playbook or the pen rules -- not to polish.
  3. The role is the senior reviewer with the last word, no longer the
     "adversarial second reader" of #618 (the word "adversarial" survives: the
     re-review is still adversarial).
  4. The critic sees the SAME composed playbook knowledge the primary does: its
     system prompt is byte-identical to the primary's and carries the projected
     playbook view and the pen-rule block the instruction tells it to hold the
     first reviewer to.
  5. The final-result contract is in the prompt: its own decision / issues /
     block edits, the one extra top-level key, KEEP / REVISE / DROP for every
     first-reviewer issue, and the override record -- plus the budgets for the
     new free-text fields, read off the artifact.
  6. The no-document variant re-derives from the material it has, says it
     cannot author block edits, and still carries the order and the bar.
  7. The critic-only vocabulary stays out of the SHARED system prompt (naming
     it there would invite the PRIMARY pass to emit it).

## What this file deliberately does NOT prove

That a real model obeys the instruction or avoids anchoring. Offline
`FakeBedrockClient` runs prove the PLUMBING and the prompt text; whether the
critic overrides the right things is a live-model question, which is what the
eval gate ADR 0001 names is for.

Run standalone: `python3 tests/test_critic_prompt_senior_reviewer.py`
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402, I001
import extraction_normalization_stage as ens  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402
import synthetic_form_paragraphs as sfp_module  # noqa: E402

from test_review_spine import (  # noqa: E402
    _SEC8_DRAFT_TEXT,
    _build_draft_docx,
    _critic_no_delta_response,
    _load_bundle,
    _primary_request_change_response_with_transcript,
)

REVIEW_ID = "00000000-0000-4000-a000-000000000137"

# The phrases this ticket fixes for the sceptical instruction, each stated as
# the sentence fragment the prompt must contain (whitespace-normalised, since
# the constants are wrapped source strings).
SCEPTICAL_PHRASES = (
    "re-derive every issue yourself from the document and the playbook",
    "only then compare your issues with the first reviewer's",
    "override wherever the first reviewer is wrong, weak, or non-compliant "
    "with the playbook or the pen rules",
    "override to correct, not to polish",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


class _Composed:
    """The real requests both passes send over one real document."""

    def __init__(self) -> None:
        self.bundle = _load_bundle()
        meta = self.bundle["playbook"]["metadata"]
        self.primary_id = meta["primary_model_id"]
        self.critic_id = meta["critic_model_id"]
        self.docx = _build_draft_docx(sfp_module, {"sec-8": _SEC8_DRAFT_TEXT})
        normalized = ens.extract_and_normalize(self.docx)
        assert normalized["status"] == "normalized", normalized
        self.block_map = ens.build_block_map(normalized["paragraphs"])
        self.doc_text = review_spine.document_text_for_review(normalized["paragraphs"])
        self.primary_output = json.loads(
            _primary_request_change_response_with_transcript(self.docx)
        )

        client = model_client.FakeBedrockClient(
            {
                self.primary_id: [json.dumps(self.primary_output)],
                self.critic_id: [_critic_no_delta_response()],
            }
        )
        primary = pp.run_primary_pass(
            review_id=REVIEW_ID,
            retrieved_precedent=[],
            playbook=self.bundle,
            model_client=client,
            model_id=self.primary_id,
            ledger_write=[].append,
            doc_text=self.doc_text,
            block_map=self.block_map,
        )
        assert primary["status"] == "OK", primary
        critic = cp.run_critic_pass(
            review_id=REVIEW_ID,
            primary_output=primary["response"],
            playbook=self.bundle,
            model_client=client,
            model_id=self.critic_id,
            ledger_write=[].append,
            doc_text=self.doc_text,
            block_map=self.block_map,
        )
        assert critic["status"] == "OK", critic
        self.primary_call = next(c for c in client.calls if c["model_id"] == self.primary_id)
        self.critic_call = next(c for c in client.calls if c["model_id"] == self.critic_id)
        self.primary_response = primary["response"]

        # The document-less variant, composed the way the pass composes it.
        self.no_doc_prompt = pp.assemble_user_prompt_critic(primary_output=self.primary_output)


# ---------------------------------------------------------------------------
# [1] Document, first reviewer's output and the instruction, in order
# ---------------------------------------------------------------------------


def test_the_prompt_carries_document_playbook_and_primary_output(
    failures: list[str], c: _Composed
) -> None:
    user = c.critic_call["user_prompt"]
    system = c.critic_call["system_prompt"]

    if c.doc_text not in user:
        failures.append("[1a] the critic's user prompt does not contain the document text, verbatim")
    if "<COUNTERPARTY_DOCUMENT>" not in user:
        failures.append("[1b] the document is not inside its delimited block")

    block = user.split("<PRIMARY_REVIEWER_OUTPUT>", 1)
    if len(block) != 2:
        failures.append("[1c] the critic's user prompt has no PRIMARY_REVIEWER_OUTPUT block")
    else:
        carried = block[1].split("</PRIMARY_REVIEWER_OUTPUT>", 1)[0]
        for issue in c.primary_response["issues"]:
            if issue["issue_key"] not in carried or issue["section_title"] not in carried:
                failures.append(
                    f"[1d] the first reviewer's issue {issue['issue_key']} is not in the critic's prompt"
                )
        if c.primary_response["block_patches"][0]["block_id"] not in carried:
            failures.append("[1e] the first reviewer's block transcript is not in the critic's prompt")

    projected = json.dumps(pp.project_playbook_for_prompt(c.bundle), sort_keys=True)
    if projected not in system:
        failures.append("[1f] the critic's system prompt does not carry the projected playbook knowledge")

    # Order: instruction, THEN the document, THEN the first reviewer's output --
    # derive first, compare second.
    at_instruction = user.find(_flat(pp.CRITIC_SCEPTICAL_INSTRUCTION)[:60])
    at_document = user.find("<COUNTERPARTY_DOCUMENT>")
    at_primary = user.find("<PRIMARY_REVIEWER_OUTPUT>")
    if not (0 <= at_instruction < at_document < at_primary):
        failures.append(
            "[1g] the order must be: sceptical instruction, document, first reviewer's output "
            f"(offsets {at_instruction}, {at_document}, {at_primary})"
        )


# ---------------------------------------------------------------------------
# [2] The sceptical instruction
# ---------------------------------------------------------------------------


def test_the_prompt_states_the_sceptical_review_instruction(
    failures: list[str], c: _Composed
) -> None:
    flat = _flat(c.critic_call["user_prompt"]).lower()
    for phrase in SCEPTICAL_PHRASES:
        if phrase.lower() not in flat:
            failures.append(f"[2a] the composed critic prompt never says {phrase!r}")
    if pp.CRITIC_SCEPTICAL_INSTRUCTION not in c.critic_call["user_prompt"]:
        failures.append("[2b] CRITIC_SCEPTICAL_INSTRUCTION is not in the prompt run_critic_pass sends, verbatim")
    # The order is stated, not just implied.
    first = flat.find("first re-derive")
    then = flat.find("only then compare")
    if not 0 <= first < then:
        failures.append("[2c] the instruction must say FIRST re-derive and ONLY THEN compare, in that order")
    # And the bar: a sound review is kept exactly, not polished.
    if "keep its issue and its edit exactly as they are" not in flat:
        failures.append("[2d] the instruction must say a correct issue and edit are kept exactly as they are")
    if "no minimum number of findings" not in flat:
        failures.append("[2e] the no-minimum rule must survive: a critic with authority must not manufacture overrides")


# ---------------------------------------------------------------------------
# [3] The role
# ---------------------------------------------------------------------------


def test_the_role_is_the_senior_reviewer_with_the_last_word(
    failures: list[str], c: _Composed
) -> None:
    flat = _flat(c.critic_call["user_prompt"]).lower()
    for needle in (
        "you are the senior reviewer",
        "you have the last word",
        "your response is the final review",
    ):
        if needle not in flat:
            failures.append(f"[3a] the critic prompt never says {needle!r}")
    if "adversarial second reader" in flat:
        failures.append("[3b] the prompt still frames the critic as the #618 'adversarial second reader'")
    if "adversarial" not in flat:
        failures.append("[3c] the re-review must still be called adversarial -- it is not a second primary")
    if not c.critic_call["user_prompt"].startswith(pp.CRITIC_TASKING_BLOCK):
        failures.append("[3d] the role must open the user prompt, ahead of every data block")


# ---------------------------------------------------------------------------
# [4] The same knowledge the primary gets
# ---------------------------------------------------------------------------


def test_the_critic_reads_the_same_playbook_knowledge_as_the_primary(
    failures: list[str], c: _Composed
) -> None:
    if c.critic_call["system_prompt"] != c.primary_call["system_prompt"]:
        failures.append(
            "[4a] the critic's system prompt differs from the primary's -- the two passes must read "
            "the same composed knowledge blocks"
        )
    system = c.critic_call["system_prompt"]
    modes = pp.render_replacement_text_modes_block(c.bundle)
    if modes is not None and modes not in system:
        failures.append("[4b] the pen-rule block the instruction holds the first reviewer to is not in the critic's system prompt")
    if "Do NOT add any other top-level key" not in system:
        failures.append("[4c] the shared output contract changed; the critic's extra key is announced by the tasking")


# ---------------------------------------------------------------------------
# [5] The final-result contract
# ---------------------------------------------------------------------------


def test_the_prompt_states_the_final_result_contract(failures: list[str], c: _Composed) -> None:
    flat = _flat(c.critic_call["user_prompt"])
    for needle, why in (
        ("YOUR RESPONSE IS THE FINAL RESULT", "the framing of the output"),
        ('"block_patches", "block_ops" and "verdict_summary"', "the carriers it authors itself"),
        ("anchored to the SAME block ids the document shows", "block anchoring against the shared map"),
        ('ONE top-level key beyond the ones the OUTPUT CONTRACT lists: "critic_delta"', "the extra key"),
        ("FIRST REVIEWER's issue_key", "which key issue_id names"),
        ('"KEEP"', "disposition vocabulary"),
        ('"REVISE"', "disposition vocabulary"),
        ('"DROP"', "disposition vocabulary"),
        ("one entry for EVERY issue the first reviewer raised", "dispositions are exhaustive"),
        ("is rejected", "a missing disposition is a failure, and the critic is told so"),
        (
            'takes a key that none of your own "issues" uses',
            "a finding recorded in both its own issues and the deprecated added_issues must not "
            "reuse a key -- _duplicate_issue_key_error spans both arrays",
        ),
        ('"primary_value"', "the override record"),
        ('"critic_value"', "the override record"),
        ('"replacement_text"', "the override field vocabulary"),
    ):
        if needle not in flat:
            failures.append(f"[5a] the critic prompt never states {needle!r} -- {why}")

    # Budgets for the new free-text fields come off the artifact, not off a literal.
    schema = pp.load_output_schema()
    for label, pointer in (
        ("reason", f"{pp._DISPOSITION_POINTER}/reason"),
        ("primary_value", f"{pp._OVERRIDE_POINTER}/primary_value"),
        ("critic_value", f"{pp._OVERRIDE_POINTER}/critic_value"),
    ):
        cap = pp.schema_max_length(schema, pointer)
        if cap is None or f'"{label}": at most {cap} characters' not in c.critic_call["user_prompt"]:
            failures.append(f"[5b] the critic prompt does not state {label}'s budget ({cap})")

    # The deprecated channels are still asked for -- the reconciler reads them this release.
    for name in ("added_issues", "contested_replacements", "rationale_objections"):
        if name not in flat:
            failures.append(f"[5c] the prompt dropped the still-read deprecated channel {name!r}")
    if "deprecated" not in flat.lower():
        failures.append("[5d] the prompt does not tell the critic those arrays are deprecated")
    if "Never silently rewrite" in flat:
        failures.append("[5e] 'never silently rewrite' contradicts ADR 0001: the critic has the last word")


# ---------------------------------------------------------------------------
# [6] No document
# ---------------------------------------------------------------------------


def test_the_no_document_variant_keeps_the_order_and_the_bar(
    failures: list[str], c: _Composed
) -> None:
    flat = _flat(c.no_doc_prompt)
    if not c.no_doc_prompt.startswith(pp.CRITIC_TASKING_BLOCK_NO_DOCUMENT):
        failures.append("[6a] a prompt with no document must open with the no-document tasking")
    if "counterparty document shown to you below" in flat:
        failures.append("[6b] the no-document prompt claims a document is shown")
    lowered = flat.lower()
    for needle in (
        "you are the senior reviewer",
        "re-derive every issue yourself from the material shown to you and the playbook",
        "only then compare",
        "override to correct, not to polish",
        "you cannot see its block ids",
    ):
        if needle not in lowered:
            failures.append(f"[6c] the no-document prompt never says {needle!r}")
    if "anchored to the SAME block ids" in flat:
        failures.append("[6d] the no-document prompt tells the critic to author edits it cannot address")
    # [6e] (independent review of #137): the WHOLE composed no-document block
    # -- duties and their intro included, not just the final-result passage --
    # must never tell the critic to author block edits, and must still tell it
    # to leave both arrays empty.
    nodoc_block = " ".join(pp.CRITIC_TASKING_BLOCK_NO_DOCUMENT.split())
    for authoring in ('your own edit in YOUR "block_patches"', "Fix each one in YOUR OWN final result"):
        if authoring in nodoc_block:
            failures.append(f"[6e] the no-document tasking still says: {authoring!r}")
    if 'leave "block_patches" and "block_ops" empty' not in nodoc_block:
        failures.append("[6e] the no-document tasking lost its leave-the-arrays-empty instruction")
    doc_block = " ".join(pp.CRITIC_TASKING_BLOCK.split())
    if 'your own edit in YOUR "block_patches"' not in doc_block:
        failures.append("[6e] the document-bearing tasking lost duty 4's authoring sentence")


# ---------------------------------------------------------------------------
# [7] Critic-only vocabulary stays out of the shared system prompt
# ---------------------------------------------------------------------------


def test_critic_only_vocabulary_stays_out_of_the_shared_prompt(
    failures: list[str], c: _Composed
) -> None:
    system = c.primary_call["system_prompt"]
    for needle in ("dispositions", "overrides", "critic_delta", "SENIOR reviewer"):
        if needle in system:
            failures.append(
                f"[7a] the SHARED system prompt names {needle!r} -- that invites the primary pass to emit it"
            )
    if "SENIOR reviewer" in c.primary_call["user_prompt"]:
        failures.append("[7b] the primary pass was told it is the senior reviewer")


TESTS = [
    test_the_prompt_carries_document_playbook_and_primary_output,
    test_the_prompt_states_the_sceptical_review_instruction,
    test_the_role_is_the_senior_reviewer_with_the_last_word,
    test_the_critic_reads_the_same_playbook_knowledge_as_the_primary,
    test_the_prompt_states_the_final_result_contract,
    test_the_no_document_variant_keeps_the_order_and_the_bar,
    test_critic_only_vocabulary_stays_out_of_the_shared_prompt,
]


def main() -> int:
    failures: list[str] = []
    composed = _Composed()
    for test in TESTS:
        print(f"-- {test.__name__}")
        try:
            test(failures, composed)
        except Exception as exc:  # noqa: BLE001 - a crash is a failure with its name attached
            failures.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")

    if failures:
        print("\nFAIL: issue #137 -- the critic prompt is the senior reviewer's")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print(
        "\nPASS: the critic prompt frames the senior reviewer with the last word, carries the "
        "document, the playbook knowledge and the first reviewer's full output, and states the "
        "sceptical derive-then-compare instruction (issue #137)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
