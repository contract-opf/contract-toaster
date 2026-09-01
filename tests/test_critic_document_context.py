#!/usr/bin/env python3
"""
Slice test for issue #618: the adversarial critic gets the counterparty
document and an explicit tasking prompt.

## Root problem this proves fixed

The critic pass shipped with two holes that only opened up over time.

1. NO DOCUMENT. The #29 manifest deliberately withheld the raw document from
   the critic, on the theory that the standard-form diff and the anchored
   clause text carried enough counterparty wording for the critic to work
   from. Issue #380 retired both: `scripts/review_spine.py::run_review` has
   passed empty diff/anchored-clause lists ever since (issue #627 deleted
   both the parameters and the blocks they fed), so those two
   blocks arrive PERMANENTLY EMPTY and the critic was left reasoning over
   the primary's JSON alone. It could not check a single `source_quote`
   against the document it was copied from, nor notice a clause the primary
   never mentioned -- the two jobs a second reader exists to do.

2. NO TASKING. Both passes share the same system blocks, and those blocks
   describe one job: review the document, emit the output contract. Nothing
   in any assembled prompt said "you are the adversarial second reader."
   ARCHITECTURE.md describes that tasking; no prompt contained it.

## What this file asserts

  1. The assembled critic user prompt carries the full document text inside
     a delimited block that is MARKED UNTRUSTED -- adjacency, the property
     tests/test_untrusted_block_marking.py pins, not mere presence.
  2. The tasking text reaches the real assembled prompt `run_critic_pass`
     sends, names all four duties, demands evidence before conclusion, and
     states explicitly that there is NO MINIMUM NUMBER OF FINDINGS.
  3. An oversized document fails closed as `document_too_large` from the
     critic path with the fake model client NEVER invoked (call count == 0)
     and nothing ledgered.
  4. `doc_text=""` (every pre-#618 caller) composes the pre-#618 block
     sequence exactly -- no empty labelled document slot.
  5. End to end through `review_spine.run_review`: the document text the
     CRITIC is shown is byte-identical to the one the PRIMARY was shown.
     Since issue #625 there is no mode in which the primary sees less than
     the whole document, so this holds on every review that gets a critic.

## What this file deliberately does NOT prove

That a real model uses the document, or honours the tasking. These are all
offline `FakeBedrockClient` (canned, schema-perfect) runs: they prove the
PLUMBING and the assembled prompt text, never model behaviour. That is a
live check, run separately.

All fixtures below are synthetic.

Run standalone: `python3 tests/test_critic_document_context.py`
Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"
MODEL_RESPONSES_DIR = TESTS_DIR / "fixtures" / "model_responses"
PLAYBOOK_PATH = TESTS_DIR / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import critic_review_pass as cp  # noqa: E402
import diff_standard_form as dsf_module  # noqa: E402
import model_client  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import review_spine  # noqa: E402

# Cross-test-file import of already-proven helpers -- the same established
# convention tests/test_llm_native_overlay.py uses for this exact set.
from test_review_spine import (  # noqa: E402
    _build_draft_docx,
    _critic_no_delta_response,
    _load_bundle,
    _primary_accept_response,
)

# Synthetic document text. Distinctive enough that finding it in a prompt
# cannot be an accident, and short enough to sit well under every cap.
SYNTHETIC_DOC_TEXT = (
    "## Term\n"
    "This agreement begins on the effective date and continues for two years.\n\n"
    "## Limitation of Liability\n"
    "Each party's aggregate liability shall not exceed seventy-five thousand dollars.\n\n"
    "## Assignment\n"
    "Either party may assign this agreement without the other party's consent."
)


def _load_fixture_text(name: str) -> str:
    return (MODEL_RESPONSES_DIR / name).read_text(encoding="utf-8")


def _sample_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _primary_output() -> dict[str, Any]:
    return json.loads(_load_fixture_text("primary_request_change_valid.json"))


def _critic_model_id() -> str:
    return _sample_playbook()["playbook"]["metadata"]["critic_model_id"]


def _is_marked_untrusted(prompt: str, tag: str) -> bool:
    """Is `<tag>`'s opening delimiter IMMEDIATELY preceded by the untrusted
    warning? Adjacency is the property (a warning tens of thousands of tokens
    earlier is not a warning about the block being read) -- the same check
    tests/test_untrusted_block_marking.py makes."""
    index = prompt.find(f"<{tag}>")
    if index == -1:
        return False
    return prompt[:index].rstrip().endswith(pp.UNTRUSTED_BLOCK_WARNING.rstrip())


def _run_critic(
    failures: list[str],
    *,
    review_id: str,
    doc_text: str,
    **kwargs: Any,
) -> tuple[dict[str, Any], Any, list[Any]]:
    critic_id = _critic_model_id()
    client = model_client.FakeBedrockClient(
        {critic_id: [_load_fixture_text("critic_no_delta_accept_valid.json")]}
    )
    ledger: list[Any] = []
    result = cp.run_critic_pass(
        review_id=review_id,
        doc_text=doc_text,
        primary_output=_primary_output(),
        playbook=_sample_playbook(),
        model_client=client,
        model_id=critic_id,
        ledger_write=ledger.append,
        **kwargs,
    )
    return result, client, ledger


# ---------------------------------------------------------------------------
# 1. AC: the assembled critic user prompt contains the full document text
#    inside a delimited untrusted block.
# ---------------------------------------------------------------------------


def test_document_reaches_the_assembled_critic_prompt(failures: list[str]) -> None:
    prompt = pp.assemble_user_prompt_critic(
        primary_output=_primary_output(),
        doc_text=SYNTHETIC_DOC_TEXT,
    )

    if "<COUNTERPARTY_DOCUMENT>" not in prompt:
        failures.append(
            "[1a] The critic prompt must carry a <COUNTERPARTY_DOCUMENT> block -- since "
            "issue #380 emptied the diff/anchored-clause blocks it is the critic's only "
            "sight of the document at all."
        )
        return

    start = prompt.index("<COUNTERPARTY_DOCUMENT>") + len("<COUNTERPARTY_DOCUMENT>\n")
    end = prompt.index("</COUNTERPARTY_DOCUMENT>")
    inside = prompt[start:end].rstrip("\n")
    if inside != SYNTHETIC_DOC_TEXT:
        failures.append(
            f"[1b] The delimited block must carry the document string VERBATIM (the "
            f"critic checks the primary's verbatim source_quotes against it); got {inside!r}"
        )

    if not _is_marked_untrusted(prompt, "COUNTERPARTY_DOCUMENT"):
        failures.append(
            "[1c] The document block must be marked untrusted, with the warning "
            "IMMEDIATELY before its opening delimiter -- the critic is the structural "
            "defense against an injection that fooled the primary."
        )

    if "COUNTERPARTY_DOCUMENT" not in pp.UNTRUSTED_BEARING_TAGS:
        failures.append(
            "[1d] COUNTERPARTY_DOCUMENT must be registered in UNTRUSTED_BEARING_TAGS -- "
            "marking is derived from the tag, never passed per call site (issue #505)."
        )

    doc_pos = prompt.index("<COUNTERPARTY_DOCUMENT>")
    primary_pos = prompt.index("<PRIMARY_REVIEWER_OUTPUT>")
    if doc_pos > primary_pos:
        failures.append(
            "[1e] The document must precede the primary's output: the tasking asks the "
            "critic to ground each objection in the evidence FIRST and object SECOND."
        )


def test_document_reaches_the_prompt_run_critic_pass_actually_sends(
    failures: list[str],
) -> None:
    """[1a-1e] assert the assembler. This asserts the wire: `run_critic_pass`
    threads `doc_text` into the prompt the model client is invoked with."""
    _result, client, _ledger = _run_critic(
        failures, review_id="critic-doc-wire", doc_text=SYNTHETIC_DOC_TEXT
    )
    if len(client.calls) != 1:
        failures.append(f"[1f] Expected exactly 1 critic invocation; got {len(client.calls)}")
        return
    user_prompt = client.calls[0]["user_prompt"]
    if SYNTHETIC_DOC_TEXT not in user_prompt:
        failures.append(
            "[1g] run_critic_pass must thread doc_text into the user prompt it sends -- "
            "an assembler that can include the document is worth nothing if the pass "
            "never passes it."
        )
    if not _is_marked_untrusted(user_prompt, "COUNTERPARTY_DOCUMENT"):
        failures.append("[1h] The sent prompt's document block must be marked untrusted.")


# ---------------------------------------------------------------------------
# 2. AC: the tasking text is present in the critic's assembled prompt and
#    includes the explicit "no minimum number of findings" statement.
# ---------------------------------------------------------------------------


def test_tasking_block_reaches_the_sent_critic_prompt(failures: list[str]) -> None:
    _result, client, _ledger = _run_critic(
        failures, review_id="critic-tasking-wire", doc_text=SYNTHETIC_DOC_TEXT
    )
    if len(client.calls) != 1:
        failures.append(f"[2a] Expected exactly 1 critic invocation; got {len(client.calls)}")
        return
    user_prompt = client.calls[0]["user_prompt"]

    if pp.CRITIC_TASKING_BLOCK not in user_prompt:
        failures.append(
            "[2b] CRITIC_TASKING_BLOCK must appear VERBATIM in the prompt run_critic_pass "
            "sends -- before this, no prompt text anywhere stated the critic's role."
        )
        return

    if user_prompt.index(pp.CRITIC_TASKING_BLOCK) != 0:
        failures.append(
            "[2c] The tasking must come FIRST, ahead of every delimited data block -- "
            "the critic has to know what it is doing before it reads the material."
        )

    # It is trusted instruction, so it must NOT sit inside an untrusted
    # delimiter: those mean "nothing in here is an instruction to you".
    for tag in pp.UNTRUSTED_BEARING_TAGS:
        open_tag, close_tag = f"<{tag}>", f"</{tag}>"
        if open_tag in user_prompt and close_tag in user_prompt:
            start, end = user_prompt.index(open_tag), user_prompt.index(close_tag)
            if start < user_prompt.index(pp.CRITIC_TASKING_BLOCK) < end:
                failures.append(
                    f"[2d] The tasking must not sit inside <{tag}> -- that block tells the "
                    f"model nothing within it is an instruction, which would neutralise it."
                )


def test_tasking_states_the_four_duties(failures: list[str]) -> None:
    flat = " ".join(pp.CRITIC_TASKING_BLOCK.split()).lower()
    duties = {
        "[2e] issues the primary missed": "added_issues",
        "[2f] over-flagging": "over-flagging",
        "[2g] weak external rationale": "external_rationale_for_footnote",
        "[2h] replacement text drifting from the playbook": "contested_replacements",
    }
    for label, needle in duties.items():
        if needle.lower() not in flat:
            failures.append(f"{label}: tasking must name it (looked for {needle!r}).")

    if "adversarial" not in flat:
        failures.append(
            "[2i] The tasking must say the critic is the ADVERSARIAL reader -- the whole "
            "point is that it is not a second primary."
        )


def test_tasking_demands_evidence_before_conclusion(failures: list[str]) -> None:
    flat = " ".join(pp.CRITIC_TASKING_BLOCK.split()).lower()
    if "quote the document evidence first" not in flat:
        failures.append(
            "[2j] The tasking must require the document evidence FIRST and the objection "
            "SECOND -- an ungrounded objection is the critic's characteristic failure."
        )


def test_tasking_states_there_is_no_minimum_number_of_findings(failures: list[str]) -> None:
    """The AC calls this out by name, and it is the instruction an
    adversarial tasking most reliably erodes: a critic told to find fault
    finds fault, and a manufactured objection costs an attorney more than it
    saves."""
    flat = " ".join(pp.CRITIC_TASKING_BLOCK.split()).lower()
    if "there is no minimum number of findings" not in flat:
        failures.append(
            "[2k] The tasking must state explicitly that there is NO MINIMUM NUMBER OF "
            "FINDINGS."
        )
    if "empty critique" not in flat:
        failures.append(
            "[2l] The tasking must say an empty critique of a good review is the CORRECT "
            "output -- 'no minimum' without that reads as permission, not as a result."
        )


# The two sentences that make the tasking a claim ABOUT the document block,
# rather than a claim the prompt can honour with or without one.
_ASSERTS_A_DOCUMENT_IS_PRESENT = "counterparty document shown to you below"
_FORBIDS_OBJECTIONS_UNGROUNDED_IN_THE_DOCUMENT = (
    "An objection you cannot ground in the document text shown to you is an "
    "objection you must not raise."
)


def test_the_tasking_variant_follows_whether_a_document_is_actually_present(
    failures: list[str],
) -> None:
    """A prompt constant that asserts a data block is present is a promise
    about that block, and the assembler that can drop the block owns keeping
    it true. Emitting the document-bearing tasking over a prompt with no
    `COUNTERPARTY_DOCUMENT` would tell the critic a document is "shown to you
    below" when none is, and then forbid any objection it cannot ground in
    that absent text -- forbidding, read literally, EVERY objection. That
    silences the adversarial pass entirely, and it would do so invisibly:
    the pass still runs, still returns schema-valid output, and simply stops
    finding anything."""
    with_doc = pp.assemble_user_prompt_critic(
        primary_output=_primary_output(),
        doc_text=SYNTHETIC_DOC_TEXT,
    )
    without_doc = pp.assemble_user_prompt_critic(
        primary_output=_primary_output(),
    )

    if not with_doc.startswith(pp.CRITIC_TASKING_BLOCK):
        failures.append(
            "[2m] With a document block composed, the prompt must open with the "
            "document-bearing tasking (CRITIC_TASKING_BLOCK)."
        )
    if not without_doc.startswith(pp.CRITIC_TASKING_BLOCK_NO_DOCUMENT):
        failures.append(
            "[2n] With NO document block composed, the prompt must open with "
            "CRITIC_TASKING_BLOCK_NO_DOCUMENT."
        )

    if _ASSERTS_A_DOCUMENT_IS_PRESENT in without_doc:
        failures.append(
            "[2o] A prompt with no COUNTERPARTY_DOCUMENT block must never claim the "
            f"document is {_ASSERTS_A_DOCUMENT_IS_PRESENT!r}."
        )
    if _FORBIDS_OBJECTIONS_UNGROUNDED_IN_THE_DOCUMENT in without_doc:
        failures.append(
            "[2p] A prompt with no COUNTERPARTY_DOCUMENT block must not forbid every "
            "objection it cannot ground in document text it was never shown -- that "
            "instruction, over an absent block, silences the critic outright."
        )
    if _FORBIDS_OBJECTIONS_UNGROUNDED_IN_THE_DOCUMENT not in with_doc:
        failures.append(
            "[2q] With the document present, the grounding requirement must still be "
            "stated -- the no-document variant redirects it, it does not repeal it."
        )

    # The variants differ ONLY where they must: the two paragraphs that speak
    # about the document. Everything else -- the four duties, the no-minimum
    # rule -- is the critic's job description and does not depend on which
    # material it was handed.
    flat = " ".join(pp.CRITIC_TASKING_BLOCK_NO_DOCUMENT.split()).lower()
    for label, needle in {
        "[2r] added_issues": "added_issues",
        "[2s] over-flagging": "over-flagging",
        "[2t] external rationale": "external_rationale_for_footnote",
        "[2u] contested replacements": "contested_replacements",
        "[2v] no minimum findings": "there is no minimum number of findings",
        "[2w] adversarial framing": "adversarial",
    }.items():
        if needle not in flat:
            failures.append(
                f"{label}: the no-document tasking must still carry it (looked for "
                f"{needle!r}) -- withholding the document withholds material, not the "
                f"critic's job description."
            )

    if "is not shown to you" not in flat:
        failures.append(
            "[2x] The no-document tasking must SAY the document is not shown to it. "
            "Silence leaves the critic to infer, from a prompt that discusses a "
            "document review, that it simply failed to find the document block."
        )
    if "must not raise" not in flat:
        failures.append(
            "[2y] The no-document tasking must still refuse ungrounded objections, "
            "against the material the critic does have. Dropping the bar here would "
            "trade a silenced critic for an inventive one."
        )


# ---------------------------------------------------------------------------
# 3. AC: an oversized document produces `document_too_large` from the critic
#    path with the fake model client never invoked.
# ---------------------------------------------------------------------------


def test_oversized_document_fails_closed_without_any_model_call(failures: list[str]) -> None:
    oversized = "The parties agree to the following synthetic clause. " * 40_000

    result, client, ledger = _run_critic(
        failures, review_id="critic-doc-too-large", doc_text=oversized
    )

    if result.get("status") != "MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[3a] Expected status=MANUAL_REVIEW_REQUIRED for an oversized critic prompt; "
            f"got {result!r}"
        )
    if result.get("reason") != "document_too_large":
        failures.append(
            f"[3b] Expected reason='document_too_large' (the SAME token the primary's "
            f"step-14 gate uses); got {result.get('reason')!r}"
        )
    if len(client.calls) != 0:
        failures.append(
            f"[3c] The cap check must run BEFORE any invocation -- expected 0 model "
            f"calls; got {len(client.calls)}"
        )
    if ledger:
        failures.append(
            f"[3d] Nothing was attempted, so nothing may be ledgered; got {len(ledger)} rows."
        )
    if result.get("max_input_tokens") != cp.MAX_INPUT_TOKENS:
        failures.append(
            f"[3e] The result must report the cap it was measured against; got "
            f"{result.get('max_input_tokens')!r}"
        )
    assembled = result.get("assembled_tokens")
    if not isinstance(assembled, int) or assembled <= cp.MAX_INPUT_TOKENS:
        failures.append(
            f"[3f] The result must report the measured assembled size, and it must exceed "
            f"the cap; got {assembled!r}"
        )


def test_the_cap_is_the_primary_passs_cap_not_a_second_number(failures: list[str]) -> None:
    if cp.MAX_INPUT_TOKENS != pp.MAX_INPUT_TOKENS:
        failures.append(
            f"[3g] The critic's cap must be the primary's cap ({pp.MAX_INPUT_TOKENS}); got "
            f"{cp.MAX_INPUT_TOKENS} -- two independently drifting caps is exactly the bug "
            f"this mirrors away."
        )


def test_a_document_under_the_cap_still_runs(failures: list[str]) -> None:
    """The gate must fail closed on oversize WITHOUT becoming a blanket
    refusal -- otherwise [3a] would pass on a pass that never calls a model
    at all."""
    result, client, _ledger = _run_critic(
        failures, review_id="critic-doc-under-cap", doc_text=SYNTHETIC_DOC_TEXT
    )
    if result.get("status") != "OK":
        failures.append(f"[3h] A normal-sized document must still reach the model; got {result!r}")
    if len(client.calls) != 1:
        failures.append(f"[3i] Expected exactly 1 model call under the cap; got {len(client.calls)}")


def test_the_cap_is_enforced_at_the_boundary_the_parameter_names(
    failures: list[str],
) -> None:
    """Same document, cap lowered below it: the gate is measuring the
    ASSEMBLED prompt against `max_input_tokens`, not pattern-matching a
    suspiciously long string."""
    result, client, _ledger = _run_critic(
        failures,
        review_id="critic-doc-tight-cap",
        doc_text=SYNTHETIC_DOC_TEXT,
        max_input_tokens=1,
    )
    if result.get("reason") != "document_too_large":
        failures.append(
            f"[3j] A cap of 1 token must reject even a small prompt; got {result!r}"
        )
    if len(client.calls) != 0:
        failures.append(f"[3k] Expected 0 model calls under a cap of 1; got {len(client.calls)}")


# ---------------------------------------------------------------------------
# 4. AC: all existing critic tests pass unchanged -- the property behind that
#    is that a caller passing no document gets the pre-#618 block sequence.
# ---------------------------------------------------------------------------


def test_no_document_composes_no_empty_document_slot(failures: list[str]) -> None:
    prompt = pp.assemble_user_prompt_critic(
        primary_output=_primary_output(),
    )
    if "COUNTERPARTY_DOCUMENT" in prompt:
        failures.append(
            "[4a] doc_text='' must OMIT the block entirely, never compose an empty "
            "labelled slot that advertises a document and shows none (issue #582's "
            "absent-or-populated doctrine)."
        )
    if "<PRIMARY_REVIEWER_OUTPUT>" not in prompt:
        failures.append(
            "[4b] <PRIMARY_REVIEWER_OUTPUT> must survive unchanged -- it is the critic's "
            "whole subject matter."
        )
    # Issue #627 deleted the two permanently-empty manifest blocks along with
    # the prompt-shape-stability rationale that kept them (see
    # `pp.assemble_user_prompt_primary`). A tag no assembler can emit is not
    # a shape worth stabilizing.
    for tag in ("<STANDARD_FORM_DIFF>", "<ANCHORED_CLAUSES>"):
        if tag in prompt:
            failures.append(f"[4c] {tag} is still composed; issue #627 removed it.")


# ---------------------------------------------------------------------------
# 5. End to end: review_spine hands the critic the document the PRIMARY read.
#    A unit test of the assembler cannot see this seam -- and this is the
#    seam that was actually broken.
# ---------------------------------------------------------------------------


def _delimited_document_in(prompt: str) -> str | None:
    if "<COUNTERPARTY_DOCUMENT>" not in prompt:
        return None
    start = prompt.index("<COUNTERPARTY_DOCUMENT>") + len("<COUNTERPARTY_DOCUMENT>\n")
    return prompt[start : prompt.index("</COUNTERPARTY_DOCUMENT>")]


def test_run_review_shows_the_critic_exactly_what_the_primary_saw(
    failures: list[str],
) -> None:
    bundle = _load_bundle()
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    docx_bytes = _build_draft_docx(dsf_module, {})

    client = model_client.FakeBedrockClient(
        {
            primary_id: [_primary_accept_response()],
            critic_id: [_critic_no_delta_response()],
        }
    )
    result = review_spine.run_review(
        docx_bytes, bundle, client, review_id="spine-618-critic-document"
    )
    if result.get("status") != "OK":
        failures.append(f"[5a] Expected a clean OK run; got {result.get('status')!r}")

    calls_by_model = {call["model_id"]: call for call in client.calls}
    if primary_id not in calls_by_model or critic_id not in calls_by_model:
        failures.append(
            f"[5b] Expected both a primary and a critic invocation; got "
            f"{[call['model_id'] for call in client.calls]!r}"
        )
        return

    primary_doc = _delimited_document_in(calls_by_model[primary_id]["user_prompt"])
    critic_doc = _delimited_document_in(calls_by_model[critic_id]["user_prompt"])

    if primary_doc is None:
        failures.append("[5c] The primary prompt must carry a document block (pre-existing).")
        return
    if critic_doc is None:
        failures.append(
            "[5d] review_spine must pass the document to the critic -- without it the "
            "critic reasons over the primary's JSON alone."
        )
        return
    if critic_doc != primary_doc:
        failures.append(
            "[5e] The critic must be shown the primary's EXACT document string. Any drift "
            "means the critic is checking source_quotes against a different document than "
            "the one they were copied from."
        )

    if pp.CRITIC_TASKING_BLOCK not in calls_by_model[critic_id]["user_prompt"]:
        failures.append("[5f] The end-to-end critic prompt must carry the tasking block.")
    if pp.CRITIC_TASKING_BLOCK in calls_by_model[primary_id]["user_prompt"]:
        failures.append(
            "[5g] The critic tasking must NEVER reach the primary pass -- it would tell "
            "the first reviewer to critique a review that does not exist yet."
        )


TESTS = [
    test_document_reaches_the_assembled_critic_prompt,
    test_document_reaches_the_prompt_run_critic_pass_actually_sends,
    test_tasking_block_reaches_the_sent_critic_prompt,
    test_tasking_states_the_four_duties,
    test_tasking_demands_evidence_before_conclusion,
    test_tasking_states_there_is_no_minimum_number_of_findings,
    test_the_tasking_variant_follows_whether_a_document_is_actually_present,
    test_oversized_document_fails_closed_without_any_model_call,
    test_the_cap_is_the_primary_passs_cap_not_a_second_number,
    test_a_document_under_the_cap_still_runs,
    test_the_cap_is_enforced_at_the_boundary_the_parameter_names,
    test_no_document_composes_no_empty_document_slot,
    test_run_review_shows_the_critic_exactly_what_the_primary_saw,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        print(f"-- {test.__name__}")
        test(failures)

    if failures:
        print("\nFAIL: issue #618 -- critic document context / tasking")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print(
        "\nPASS: the critic receives the primary's document in a marked untrusted block, "
        "an explicit adversarial tasking, and the primary's own input-size cap (issue #618)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
