#!/usr/bin/env python3
"""
Slice test (TDD) for issue #516 (epic #519, item B): "the prompt tells the
model to narrate playbook deviations into the counterparty-facing footnote
-- internal posture ships in the redline."

## Root problem this proves fixed

Before this fix, `primary_review_pass.TOASTER_GUIDANCE_INTRO` UNCONDITIONALLY
instructed the model that, on a guidance/playbook conflict, it must "say you
did so (name the point of conflict) in verdict_summary or the relevant
issue's external_rationale_for_footnote" -- regardless of this review's
notes mode. Both of those fields are counterparty-facing by design in every
mode (`external_rationale_for_footnote` becomes a Word footnote in the
redline sent to the counterparty; `verdict_summary` is persisted as
`summary`, rendered in the UI, and the threat model already assumes it is
realistically copy-pasted into email -- owner ruling on #516, 2026-08-03,
which explicitly rejected routing the narration to `verdict_summary`
instead of the footnote as no improvement). So the prompt itself was asking
the model to leak internal negotiating posture -- "the reviewing team
directed a departure from our standard position" -- straight into the
counterparty's hands, in writing, on every review with a non-empty
`toaster_guidance` that ever conflicted with the playbook. This is
`sev:high`: on main before this fix, EVERY reachable review ran in the
default `external` notes mode, so the narration instruction was live in
100% of toaster-guidance-conflict reviews.

The owner's decision (2026-08-03) is explicit that the fix is a PROMPT
GATE, not an output filter: "when internal notes are off, the model is
never instructed to narrate the conflict into external_rationale_for_
footnote. Filtering it out after generation would be wrong -- it would
mean internal reasoning was produced into a counterparty-bound field and
merely stripped on the way out, which is exactly the posture the leakage
scan exists to prevent."

## What this test asserts (mirrors the issue's acceptance criteria)

  1. `primary_review_pass.render_toaster_guidance_block` /
     `assemble_system_blocks`: for `notes_mode in ("none", "external")`
     (internal notes OFF -- the only two modes reachable on main while
     issue #572's `NOTES_MODE_ENABLED` kill switch stays off), the rendered
     block never instructs the model to narrate a conflict into
     `verdict_summary` or `external_rationale_for_footnote` -- watched
     failing against the PRE-#516 `TOASTER_GUIDANCE_INTRO`, which carried
     that instruction unconditionally.
  2. For `notes_mode in ("internal", "both")` (internal notes ON), the
     narration instruction is UNCHANGED from today's wording -- item B's
     scope is the gate, not what happens once #521/#522 make the internal
     channel itself safe.
  3. The gate changes ONLY the narration clause: the precedence statement
     ("GOVERNS") and the Floor carve-out survive verbatim in every mode,
     and the two false/true groups are each internally byte-identical.
  4. `run_primary_pass` / `run_critic_pass` (not just the pure
     `assemble_system_blocks` unit) thread `notes_mode` into the ACTUAL
     system prompt text sent to the (fake) model -- an integration check,
     not just the isolated block-assembly one above.
  5. End to end (issue's own AC, "asserted on generated document bytes"):
     `review_spine.run_review()`, given a `toaster_guidance` that conflicts
     with the playbook's stated Section 8 position and the DEFAULT notes
     mode, produces a real tracked-changes `.docx` whose `word/footnotes
     .xml` text contains the issue's ordinary (non-narrating) rationale but
     none of a battery of deviation-narration signal phrases -- AND, within
     that SAME run, the system prompt actually sent to both model calls
     lacked the narration instruction, tying the document-level guarantee
     back to the prompt-level cause rather than asserting it in isolation.
  6. Transparency is not silently lost as a side effect of the gate: the
     toaster_guidance text itself, and the explicit "GOVERNS" precedence
     statement, still reach the model in every mode -- only the instruction
     to NAME the conflict in a counterparty-bound field is gated.

Offline: pure function-level checks plus a `FakeBedrockClient`-driven
end-to-end run (`backend/src/model_client.py`). No AWS, no network, no
model.

Run standalone: `python3 tests/test_toaster_guidance_notes_mode_gate_516.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402
import diff_standard_form as dsf_module  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

# Cross-test-file import (established convention -- see
# tests/test_llm_native_overlay.py importing from tests/test_review_spine.py
# the same way): reuse the already-proven docx builder, canned fixtures, and
# playbook loader rather than re-deriving them.
from test_review_spine import (  # noqa: E402
    _SEC8_DRAFT_TEXT,
    _build_draft_docx,
    _critic_no_delta_response,
    _load_bundle,
    _primary_request_change_response,
)

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _footnote_text(redline_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(redline_bytes)) as zf:
        if "word/footnotes.xml" not in zf.namelist():
            return ""
        root = ET.fromstring(zf.read("word/footnotes.xml"))
        return "".join((t.text or "") for t in root.findall(f".//{_qn('t')}"))


# Deliberately conflicts with the synthetic-generic playbook's own Section 8
# (liability cap) position, mirroring test_llm_native_overlay.py's own
# guidance-conflict fixture -- what matters here is that the GUIDANCE text
# conflicts, not that the (canned) model actually resolves it any particular
# way.
_CONFLICTING_GUIDANCE = (
    "For this deal only, this reviewing team is fine accepting a lower "
    "liability cap than the playbook's standard position -- do not hold "
    "the line on Section 8 this time."
)

# Signal phrases a model narrating "the reviewing team told me to deviate
# from the playbook" would plausibly use. None of these appear in the clean
# fixture rationale (`_primary_request_change_response`'s
# `external_rationale_for_footnote`, unchanged from test_review_spine.py),
# so their absence from the generated document is a meaningful assertion,
# not a vacuous one.
_NARRATION_SIGNAL_PHRASES = (
    "reviewing team",
    "toaster",
    "per-review guidance",
    "TOASTER_GUIDANCE",
    "departed from",
    "departure from",
    "deviat",  # covers deviate/deviation/deviated
    "at the team's direction",
)


def _sample_playbook() -> dict[str, Any]:
    return _load_bundle()["playbook"]


# ---------------------------------------------------------------------------
# 1. render_toaster_guidance_block / assemble_system_blocks: the narration
#    clause is gated on notes_mode, nothing else about the block moves.
# ---------------------------------------------------------------------------


def test_gated_modes_never_instruct_narration(failures: list[str]) -> None:
    for mode in ("none", "external"):
        block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE, notes_mode=mode)
        if block is None:
            failures.append(f"[1a-{mode}] Expected a rendered block for non-empty guidance.")
            continue
        for banned in ("say you did so", "external_rationale_for_footnote", "verdict_summary"):
            if banned in block:
                failures.append(
                    f"[1b-{mode}] notes_mode={mode!r} (internal notes OFF) must never instruct "
                    f"the model to narrate into a counterparty-facing field; found {banned!r}."
                )


def test_default_argument_matches_external(failures: list[str]) -> None:
    """A caller that does not pass notes_mode at all (an un-migrated call
    site) must get the SAME gated behaviour as explicit `external` -- the
    default has to be the safe one, not an opt-in."""
    default_block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE)
    external_block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE, notes_mode="external")
    if default_block != external_block:
        failures.append(
            "[2a] render_toaster_guidance_block's default notes_mode must render "
            "byte-identically to notes_mode='external'."
        )
    if default_block is not None and "say you did so" in default_block:
        failures.append("[2b] The default (un-migrated-caller) block must not instruct narration either.")


def test_internal_modes_keep_the_narration_instruction(failures: list[str]) -> None:
    """Item B's scope is the gate itself, not what #521/#522 later do to
    make the internal channel safe -- so `internal`/`both` keep today's
    instruction unchanged."""
    for mode in ("internal", "both"):
        block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE, notes_mode=mode)
        if block is None:
            failures.append(f"[3a-{mode}] Expected a rendered block for non-empty guidance.")
            continue
        if "say you did so" not in block:
            failures.append(
                f"[3b-{mode}] notes_mode={mode!r} (internal notes ON) must keep instructing the "
                "model to name a guidance/playbook conflict -- item B only gates when notes are off."
            )
        for expected in ("verdict_summary", "external_rationale_for_footnote"):
            if expected not in block:
                failures.append(f"[3c-{mode}] Expected {expected!r} still named as a narration target.")


def test_only_the_narration_clause_varies_by_mode(failures: list[str]) -> None:
    """The gate must be surgical: everything else about the block (the
    precedence statement, the Floor carve-out, the guidance text itself)
    stays put regardless of mode, and the two mode-groups are each
    internally identical."""
    none_block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE, notes_mode="none")
    external_block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE, notes_mode="external")
    internal_block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE, notes_mode="internal")
    both_block = pp.render_toaster_guidance_block(_CONFLICTING_GUIDANCE, notes_mode="both")

    if none_block != external_block:
        failures.append("[4a] 'none' and 'external' (both internal-notes-off) must render byte-identically.")
    if internal_block != both_block:
        failures.append("[4b] 'internal' and 'both' (both internal-notes-on) must render byte-identically.")
    if external_block == internal_block:
        failures.append("[4c] The gated and ungated groups must actually differ -- otherwise the gate is a no-op.")

    for label, block in (("none", none_block), ("internal", internal_block)):
        if block is None:
            continue
        if "GOVERNS" not in block:
            failures.append(f"[4d-{label}] Explicit precedence wording ('GOVERNS') must survive the gate.")
        if "MUST-NOT FLOOR" not in block or "Floor" not in block:
            failures.append(f"[4e-{label}] The Floor carve-out sentence must survive the gate.")
        if _CONFLICTING_GUIDANCE not in block:
            failures.append(f"[4f-{label}] The guidance text itself must still be present regardless of mode.")


def test_assemble_system_blocks_threads_notes_mode_to_the_guidance_block(failures: list[str]) -> None:
    """Not just the pure render function -- the composed assembly caller
    (`assemble_system_blocks`) must actually pass its own `notes_mode`
    argument through, since that is the seam both passes call."""
    playbook = _sample_playbook()
    external_blocks = pp.assemble_system_blocks(
        playbook, _CONFLICTING_GUIDANCE, "", notes_mode="external"
    )
    internal_blocks = pp.assemble_system_blocks(
        playbook, _CONFLICTING_GUIDANCE, "", notes_mode="internal"
    )
    external_text = pp.render_system_prompt(external_blocks)
    internal_text = pp.render_system_prompt(internal_blocks)

    if "say you did so" in external_text:
        failures.append("[5a] assemble_system_blocks(notes_mode='external') must not carry the narration instruction.")
    if "say you did so" not in internal_text:
        failures.append("[5b] assemble_system_blocks(notes_mode='internal') must still carry the narration instruction.")


# ---------------------------------------------------------------------------
# 2. run_primary_pass / run_critic_pass thread notes_mode into the ACTUAL
#    system prompt text sent to the model (integration, not just the pure
#    assemble_system_blocks unit checks above).
# ---------------------------------------------------------------------------


def test_run_primary_pass_gates_narration_by_notes_mode(failures: list[str]) -> None:
    playbook = _sample_playbook()
    primary_id = playbook["metadata"]["primary_model_id"]

    external_client = model_client.FakeBedrockClient(
        {primary_id: [_primary_request_change_response()]}
    )
    pp.run_primary_pass(
        review_id="review-516-primary-external",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=playbook,
        model_client=external_client,
        model_id=primary_id,
        ledger_write=lambda record: None,
        doc_text="Some document text.",
        toaster_guidance=_CONFLICTING_GUIDANCE,
        notes_mode="external",
    )
    if len(external_client.calls) != 1:
        failures.append(f"[6a] Expected exactly 1 model invocation; got {len(external_client.calls)}")
    elif "say you did so" in external_client.calls[0]["system_prompt"]:
        failures.append(
            "[6b] run_primary_pass(notes_mode='external') sent a system prompt that still "
            "instructs the model to narrate a guidance conflict into a counterparty-facing field."
        )

    internal_client = model_client.FakeBedrockClient(
        {primary_id: [_primary_request_change_response()]}
    )
    pp.run_primary_pass(
        review_id="review-516-primary-internal",
        diff_hunks=[],
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=playbook,
        model_client=internal_client,
        model_id=primary_id,
        ledger_write=lambda record: None,
        doc_text="Some document text.",
        toaster_guidance=_CONFLICTING_GUIDANCE,
        notes_mode="internal",
    )
    if len(internal_client.calls) != 1:
        failures.append(f"[6c] Expected exactly 1 model invocation; got {len(internal_client.calls)}")
    elif "say you did so" not in internal_client.calls[0]["system_prompt"]:
        failures.append(
            "[6d] run_primary_pass(notes_mode='internal') should still carry the narration "
            "instruction -- item B only gates when internal notes are off."
        )


def test_run_critic_pass_gates_narration_by_notes_mode(failures: list[str]) -> None:
    playbook = _sample_playbook()
    critic_id = playbook["metadata"]["critic_model_id"]
    primary_output = {
        "decision": "REQUEST_CHANGE",
        "confidence_state": "OK",
        "confidence_band": None,
        "issues": [],
        "critic_delta": None,
        "verdict_summary": None,
    }

    external_client = model_client.FakeBedrockClient(
        {critic_id: [_critic_no_delta_response()]}
    )
    cp.run_critic_pass(
        review_id="review-516-critic-external",
        diff_hunks=[],
        anchored_clauses=[],
        primary_output=primary_output,
        playbook=playbook,
        model_client=external_client,
        model_id=critic_id,
        ledger_write=lambda record: None,
        toaster_guidance=_CONFLICTING_GUIDANCE,
        notes_mode="external",
    )
    if not external_client.calls:
        failures.append("[7a] Expected at least 1 critic model invocation.")
    elif "say you did so" in external_client.calls[0]["system_prompt"]:
        failures.append(
            "[7b] run_critic_pass(notes_mode='external') sent a system prompt that still "
            "instructs the model to narrate a guidance conflict into a counterparty-facing field."
        )


# ---------------------------------------------------------------------------
# 3. End to end (the issue's own AC wording): run_review() over a real
#    document produces .docx bytes with no narration in the footnote, and
#    the model calls that produced it never asked for narration either.
# ---------------------------------------------------------------------------


def test_end_to_end_redline_carries_no_narration_in_default_mode(failures: list[str]) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]

    docx_bytes = _build_draft_docx(dsf_module, {"sec-8": _SEC8_DRAFT_TEXT})
    fake_client = model_client.FakeBedrockClient(
        {
            primary_id: [_primary_request_change_response()],
            critic_id: [_critic_no_delta_response()],
        }
    )

    # No notes_mode passed -- exercises the DEFAULT, which is what every
    # reachable review on main runs today (issue #572's kill switch keeps
    # internal/both unreachable in production).
    result = review_spine.run_review(
        docx_bytes,
        bundle,
        fake_client,
        review_id="spine-516-default-mode",
        toaster_guidance=_CONFLICTING_GUIDANCE,
    )

    if result["status"] != "OK" or result["decision"] != "REQUEST_CHANGE":
        failures.append(
            f"[8a] Expected status=OK/decision=REQUEST_CHANGE; got "
            f"{result.get('status')}/{result.get('decision')}"
        )
        return
    if not result.get("redline_bytes"):
        failures.append("[8b] Expected non-empty redline_bytes on the REQUEST_CHANGE path.")
        return

    # -- Prompt-level cause, asserted on the SAME run that produced the
    #    document below. --------------------------------------------------
    for call in fake_client.calls:
        if "say you did so" in call["system_prompt"]:
            failures.append(
                f"[8c] The {call['model_id']} system prompt for this default-mode run must not "
                "instruct narration of the guidance conflict into a counterparty-facing field."
            )

    # -- Document-level effect: the issue's own AC, "asserted on generated
    #    document bytes". ------------------------------------------------
    footnote_text = _footnote_text(result["redline_bytes"])
    if not footnote_text:
        failures.append("[8d] Expected non-empty word/footnotes.xml text in the generated redline.")
        return

    if "Section 8 must retain the standard aggregate liability cap" not in footnote_text:
        failures.append(
            f"[8e] Expected the issue's ordinary (non-narrating) rationale in the footnote "
            f"text; not found. Footnote text: {footnote_text!r}"
        )

    for phrase in _NARRATION_SIGNAL_PHRASES:
        if phrase.lower() in footnote_text.lower():
            failures.append(
                f"[8f] Deviation-narration signal phrase {phrase!r} found in the generated "
                f".docx footnote text -- internal posture leaked into the counterparty-bound "
                f"document. Footnote text: {footnote_text!r}"
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_TESTS = [
    test_gated_modes_never_instruct_narration,
    test_default_argument_matches_external,
    test_internal_modes_keep_the_narration_instruction,
    test_only_the_narration_clause_varies_by_mode,
    test_assemble_system_blocks_threads_notes_mode_to_the_guidance_block,
    test_run_primary_pass_gates_narration_by_notes_mode,
    test_run_critic_pass_gates_narration_by_notes_mode,
    test_end_to_end_redline_carries_no_narration_in_default_mode,
]


def main() -> int:
    failures: list[str] = []
    for test_fn in _ALL_TESTS:
        before = len(failures)
        test_fn(failures)
        status_word = "PASS" if len(failures) == before else "FAIL"
        print(f"{status_word}: {test_fn.__name__}")

    if failures:
        print("\nFAIL: toaster-guidance notes-mode prompt gate (issue #516).\n")
        for f in failures:
            print(f)
        print(f"\nTotal failures: {len(failures)}")
        return 1

    print("\nPASS: deviation narration is gated by notes mode (issue #516).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
