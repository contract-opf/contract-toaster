#!/usr/bin/env python3
"""
Slice test for issue #674: "the output contract's length budgets are sized for
fixtures -- real reviews exceed critic_objection (800) and verdict_summary
(2000)".

## What was wrong

Two defects, one class.

1. THE CAPS WERE IN THE WRONG CLASS. Every `maxLength` in
   `playbooks/output-schema-v3.json` is either a LAYOUT/IDENTITY cap (the
   value lands somewhere with a shape of its own -- a footnote in the
   delivered .docx, a result-view heading, an audit reference, a
   code-assigned block id) or a FREE-PROSE cap (nothing about the
   destination bounds it; the number exists only so one response cannot be
   unbounded -- this artifact's number for that is 8000).
   `critic_objection` and `rationale_objections[].objection` are internal
   audit content no counterparty ever reads, and `verdict_summary` is never
   rendered verbatim into the delivered document -- yet all three carried
   layout-class caps (800/800/2000). Live runs against the real
   `educational-affiliation` playbook rejected a complete review on each of
   them, twice on the same attempt budget (issue #671, layers 4 and 5).

2. THE MODEL WAS NEVER TOLD THE NUMBERS. Structured output does not help:
   `model_output_schema._UNSUPPORTED_STRING_CONSTRAINT_KEYWORDS` strips
   `maxLength` out of the provider-facing projection, so a provider enforces
   the SHAPE of a response and never its LENGTH. And jsonschema's own
   rejection message (`'...' is too long`) carries neither the limit nor the
   overage, so the "informed" retry was as uninformed as the first attempt --
   which is exactly what both live failures did.

## Invariants asserted here

  [1] A realistic multi-sentence value validates for each field whose class
      changed -- AND is rejected by the SAME artifact with that one cap
      restored to its pre-#674 value. The control half is what stops this
      being a tautology: a value short enough to have passed at 800 would
      pass both ways and prove nothing.
  [2] The caps that were NOT changed still hold their exact old numbers.
      Raising every cap reflexively is the failure mode the ticket names;
      this pins the ones that are deliberate.
  [3] Every cap in the artifact is accounted for exactly once -- stated to
      the model, or deliberately unstated with a recorded reason. A new
      field cannot arrive silently unbudgeted the way these two did.
  [4] The assembled prompts actually carry the numbers -- primary overlay,
      internal-notes variant, and both critic-tasking variants -- read off
      the artifact, so a schema edit moves the prompt with it. Asserted as
      "the line naming this field also carries this number", not as a pinned
      sentence.
  [5] The critic's own field names stay OUT of the shared overlay: that
      block tells every reader "include these top-level keys and ONLY
      these", so naming a critic-only key there would invite the PRIMARY
      pass to emit it.
  [6] A `maxLength` rejection names the field, its actual length and its
      maximum, and the retry correction built from it says to condense the
      prose rather than drop a finding. The non-length schema failure still
      gets the generic framing -- both branches, not just the new one.

## Fixture provenance (fixture rule)

Every value here is a synthetic string handed to the REAL
`primary_review_pass.validate_model_response` / the REAL shipped artifact via
`jsonschema.validate` -- the same two seams production runs a model response
through (`run_primary_pass` / `run_critic_pass` call
`validate_model_response` on the provider's raw body). Nothing is faked. The
prose describes an invented clinical affiliation agreement with no real
party, no real counterparty text and no real precedent.

The ONE constructed artifact is `_schema_with_cap_restored`, which deep-copies
the shipped schema and puts a single cap back to its pre-#674 value. That is
a CONTROL for the red half of [1], never a production shape: production
always validates against the artifact on disk.

Run with: python3 tests/test_length_budgets_674.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import jsonschema  # noqa: E402

import primary_review_pass as pp  # noqa: E402

SCHEMA = pp.load_output_schema()


# ---------------------------------------------------------------------------
# Synthetic realistic values
#
# THESE ARE CONTROLS, NOT EVIDENCE. A string this file wrote cannot justify a
# cap; only real model output can, and that measurement lives in
# `docs/output-contract.md` "Length budgets" and in the schema descriptions
# themselves (live #671 `--dump-dir` runs, 2026-09-02: `verdict_summary`
# observed at 2068 / 2202 / 2635, `critic_objection` at 910 / 1275).
# What these values do is make the red/green control in [1] meaningful: each
# is shaped like the live failure it stands in for -- an adversarial objection
# that quotes its evidence before stating its conclusion, a verdict summary
# that accounts for every substantive issue in a multi-clause agreement -- and
# each is longer than the cap it used to hit and shorter than the cap it hits
# now, so [1] fails against the pre-#674 schema and passes against the shipped
# one. The two with a live counterpart sit inside its observed range (1000
# against 910-1275; 2240 against 2068-2635) and are deliberately not read as
# though they set it. `REALISTIC_RATIONALE_OBJECTION` has no live counterpart
# at all -- that field was never populated in any of the ten runs -- which is
# recorded on the cap itself rather than papered over by this string.
# ---------------------------------------------------------------------------

REALISTIC_CRITIC_OBJECTION = (
    "The first reviewer inserted the limiting phrase \"arising solely from the "
    "negligent acts of the Institution's own personnel\" into the indemnity at "
    "p0021, and that phrase narrows the indemnity further than the position "
    "requires. Read literally it excludes third-party tort claims brought "
    "against the Facility for harm caused on the Facility's premises by a "
    "student the Institution placed there, because such a claim arises from the "
    "student's conduct rather than from any act of the Institution's personnel. "
    "That is the single largest exposure this agreement creates, and the edit as "
    "drafted moves it onto the wrong party without saying so anywhere in the "
    "rationale offered to the counterparty. The safer formulation ties each "
    "party's obligation to the acts or omissions of its own employees, agents "
    "and students in connection with the Program, which is symmetrical, does not "
    "depend on characterising whose negligence caused a given loss, and is "
    "consistent with the shape the position actually asks for."
)

REALISTIC_RATIONALE_OBJECTION = (
    "The rationale offered for the issue at p0009 says the notice period "
    "\"departs from the required position\", but the clause it points at sets a "
    "thirty-day cure period, and the position governs the termination notice "
    "period rather than the cure period; the two are different mechanics in this "
    "agreement and the document keeps them in separate subsections. As written "
    "the objection would be read back to the counterparty as an assertion about "
    "a clause that says something else, which is the kind of correction that "
    "costs credibility on every other issue in the same letter. Either re-aim "
    "the rationale at the termination notice in the preceding subsection, or "
    "withdraw it: on the notice period actually stated, this document already "
    "complies and nothing needs to change. The same rationale then cites the "
    "corpus as support, which compounds the problem, because the placements it "
    "points at were negotiated under a form that kept cure and termination "
    "notice in a single subsection; the comparison does not carry across to "
    "this document's structure and should not be offered as though it did."
)

REALISTIC_VERDICT_SUMMARY = (
    "The counterparty returned the affiliation agreement with substantive edits "
    "in five places, and this review requests changes in four of them. First, "
    "the indemnity at p0021 was rewritten from a mutual obligation into a "
    "one-way obligation running only from the Institution, and the carve-out "
    "for the Facility's own negligence was deleted; the requested edit restores "
    "the mutual shape and ties each party's obligation to the acts or omissions "
    "of its own personnel and students in connection with the Program. Second, "
    "the insurance article at p0024 was reduced from occurrence-form coverage to "
    "claims-made coverage with no tail, which leaves a gap for claims reported "
    "after the placements end; the requested edit restores occurrence form and, "
    "failing that, requires an extended reporting period covering the survival "
    "term of the indemnity. Third, the student-records clause at p0013 was "
    "broadened to permit disclosure to the counterparty's affiliates for "
    "unspecified operational purposes, which is wider than the education-records "
    "handling this agreement can support; the requested edit narrows disclosure "
    "to personnel with a legitimate educational interest in the placement and "
    "keeps the existing confidentiality obligation attached to it. Fourth, the "
    "termination article at p0028 was changed so that termination for "
    "convenience takes effect immediately on notice, with no provision for "
    "students already on placement; the requested edit adds a teach-out so a "
    "student mid-rotation completes the term already begun, which is the point "
    "of the article rather than an accommodation. Fifth, and accepted without "
    "change, the counterparty tightened the background-check article at p0017 by "
    "adding a re-screening obligation on the Institution; that is more "
    "protective than the version sent out and there is no reason to push back on "
    "it. The mutual limitation of liability at p0015 is symmetrical and "
    "consistent with the shape the liability position requires, so it is left "
    "alone, and the governing-law choice and the arbitration and notice "
    "mechanics are accepted as returned in order to close. Confidence is normal: "
    "every clause in the document was reached, and nothing in it was ambiguous "
    "enough to need a second reader before these four edits go back."
)


# The value each cap carried BEFORE issue #674 -- the control for [1].
PRE_674_CAPS: dict[str, int] = {
    "/properties/verdict_summary/oneOf/1": 2000,
    "/definitions/CriticDelta/properties/contested_replacements/items/properties/critic_objection": 800,
    "/definitions/CriticDelta/properties/rationale_objections/items/properties/objection": 800,
}

# [2] The caps issue #674 deliberately did NOT touch, and their exact values.
# `external_rationale_for_footnote` and `internal_rationale_for_footnote` are
# typeset into footnotes of the delivered .docx; the rest are display cells,
# an audit reference, and code-assigned ids.
UNCHANGED_CAPS: dict[str, int] = {
    "/definitions/Issue/properties/section_ref": 200,
    "/definitions/Issue/properties/section_title": 300,
    "/definitions/Issue/properties/counterparty_change_summary": 2000,
    "/definitions/Issue/properties/external_rationale_for_footnote": 800,
    "/definitions/Issue/properties/internal_rationale_for_footnote": 800,
    "/definitions/Issue/properties/replacement_scope_note": 300,
    "/definitions/Issue/properties/internal_precedent_citation/oneOf/1": 500,
    "/definitions/BlockPatch/properties/block_id": 64,
    "/definitions/BlockOp/oneOf/0/properties/block_id": 64,
    "/definitions/BlockOp/oneOf/1/properties/anchor_block_id": 64,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_with_cap_restored(pointer: str, cap: int) -> dict[str, Any]:
    """The shipped artifact with ONE cap put back to its pre-#674 value.

    The control for the red half of [1]. Deep-copied so the module-level
    `SCHEMA` (memoized by `load_output_schema` and shared with every other
    caller in this process) is never mutated.
    """
    schema = copy.deepcopy(SCHEMA)
    node = pp.resolve_schema_pointer(schema, pointer)
    assert isinstance(node, dict), pointer
    node["maxLength"] = cap
    return schema


def _validates(instance: Any, schema: dict[str, Any]) -> str | None:
    """`None` when `instance` validates, else the validator's message."""
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path)
        return f"{exc.message[:120]} (at {location})"
    return None


def _response(**overrides: Any) -> dict[str, Any]:
    """A minimal ACCEPT-shaped response -- the envelope every real model
    response carries, with the pipeline-stamped `schema_version` const the
    artifact demands."""
    body: dict[str, Any] = {
        "schema_version": pp.OUTPUT_SCHEMA_VERSION,
        "decision": "ACCEPT",
        "confidence_state": "OK",
        "issues": [],
        "block_patches": [],
        "block_ops": [],
    }
    body.update(overrides)
    return body


def _critic_response(*, objection: str | None = None, contested: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {"added_issues": [], "contested_replacements": [], "rationale_objections": []}
    if contested is not None:
        delta["contested_replacements"].append(
            {
                "section_ref": "8 Indemnification",
                "primary_replacement_text": (
                    "Each party shall indemnify and hold harmless the other party from "
                    "third-party claims arising from the acts or omissions of its own "
                    "employees, agents and students in connection with the Program."
                ),
                "critic_objection": contested,
            }
        )
    if objection is not None:
        delta["rationale_objections"].append({"section_ref": "12 Term and Termination", "objection": objection})
    return _response(critic_delta=delta)


def _all_schema_cap_pointers(node: Any, pointer: str = "") -> list[str]:
    """Every JSON pointer in the artifact carrying a `maxLength`."""
    found: list[str] = []
    if isinstance(node, dict):
        if "maxLength" in node:
            found.append(pointer)
        for key, value in node.items():
            found.extend(_all_schema_cap_pointers(value, f"{pointer}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_all_schema_cap_pointers(value, f"{pointer}/{index}"))
    return found


def _states_budget(prompt: str, label: str, cap: int) -> bool:
    """True when SOME line of `prompt` names `label` and carries `cap` as a
    standalone number.

    The invariant is "the model is told this field's limit", not any
    particular sentence -- so this deliberately does not pin the wording the
    renderer happens to use today.

    The label match is bounded on identifier characters so `objection` cannot
    be satisfied by the `critic_objection` line: both carry the same cap, so a
    plain substring match would let one budget stand in for the other and
    a dropped line would go unnoticed.
    """
    label_pattern = rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])"
    for line in prompt.splitlines():
        if re.search(label_pattern, line) and re.search(rf"\b{cap}\b", line):
            return True
    return False


# ---------------------------------------------------------------------------
# [1] Realistic values validate now, and would not have before
# ---------------------------------------------------------------------------


def test_realistic_critic_objection_validates(failures: list[str]) -> None:
    pointer = (
        "/definitions/CriticDelta/properties/contested_replacements/items/properties/critic_objection"
    )
    body = _critic_response(contested=REALISTIC_CRITIC_OBJECTION)
    error = _validates(body, SCHEMA)
    if error is not None:
        failures.append(
            f"a realistic {len(REALISTIC_CRITIC_OBJECTION)}-character critic_objection is "
            f"rejected by the shipped artifact: {error}"
        )
    control = _validates(body, _schema_with_cap_restored(pointer, PRE_674_CAPS[pointer]))
    if control is None:
        failures.append(
            "the critic_objection fixture also validates at the pre-#674 cap of "
            f"{PRE_674_CAPS[pointer]} -- it is too short to exercise the raised budget, "
            "so the green run above proves nothing"
        )


def test_realistic_rationale_objection_validates(failures: list[str]) -> None:
    pointer = "/definitions/CriticDelta/properties/rationale_objections/items/properties/objection"
    body = _critic_response(objection=REALISTIC_RATIONALE_OBJECTION)
    error = _validates(body, SCHEMA)
    if error is not None:
        failures.append(
            f"a realistic {len(REALISTIC_RATIONALE_OBJECTION)}-character rationale objection is "
            f"rejected by the shipped artifact: {error}"
        )
    control = _validates(body, _schema_with_cap_restored(pointer, PRE_674_CAPS[pointer]))
    if control is None:
        failures.append(
            "the rationale objection fixture also validates at the pre-#674 cap of "
            f"{PRE_674_CAPS[pointer]} -- too short to exercise the raised budget"
        )


def test_realistic_verdict_summary_validates(failures: list[str]) -> None:
    pointer = "/properties/verdict_summary/oneOf/1"
    body = _response(verdict_summary=REALISTIC_VERDICT_SUMMARY)
    error = _validates(body, SCHEMA)
    if error is not None:
        failures.append(
            f"a realistic {len(REALISTIC_VERDICT_SUMMARY)}-character verdict_summary is "
            f"rejected by the shipped artifact: {error}"
        )
    control = _validates(body, _schema_with_cap_restored(pointer, PRE_674_CAPS[pointer]))
    if control is None:
        failures.append(
            "the verdict_summary fixture also validates at the pre-#674 cap of "
            f"{PRE_674_CAPS[pointer]} -- too short to exercise the raised budget"
        )


def test_raised_fields_reached_the_free_prose_class(failures: list[str]) -> None:
    """The three raised fields carry the SAME bound the artifact already gives
    every other free-prose field. The invariant is the class, not the digits:
    if the free-prose bound moves, these move with it or this fails."""
    free_prose_reference = pp.schema_max_length(
        SCHEMA,
        "/definitions/CriticDelta/properties/contested_replacements/items/properties/critic_suggested_replacement",
    )
    for pointer in PRE_674_CAPS:
        cap = pp.schema_max_length(SCHEMA, pointer)
        if cap != free_prose_reference:
            failures.append(
                f"{pointer} carries maxLength {cap}, not the free-prose class bound "
                f"{free_prose_reference} the artifact uses for model-authored text with no "
                "layout constraint"
            )


# ---------------------------------------------------------------------------
# [2] The deliberate caps are unchanged
# ---------------------------------------------------------------------------


def test_deliberate_caps_are_unchanged(failures: list[str]) -> None:
    for pointer, expected in UNCHANGED_CAPS.items():
        actual = pp.schema_max_length(SCHEMA, pointer)
        if actual != expected:
            failures.append(
                f"{pointer} is {actual}, expected {expected}: issue #674 raises the caps with "
                "evidence and leaves the rest -- a layout/identity cap that moves without its "
                "own justification is the reflexive widening the ticket forbids"
            )


# ---------------------------------------------------------------------------
# [3] Every cap in the artifact is accounted for
# ---------------------------------------------------------------------------


def test_every_cap_is_stated_or_deliberately_unstated(failures: list[str]) -> None:
    stated = {
        pointer
        for _label, pointer in (
            pp._PRIMARY_LENGTH_BUDGETS
            + pp._INTERNAL_NOTES_LENGTH_BUDGETS
            + pp._CRITIC_LENGTH_BUDGETS
        )
    }
    unstated = set(pp.LENGTH_BUDGETS_DELIBERATELY_UNSTATED)
    overlap = stated & unstated
    if overlap:
        failures.append(f"pointers claimed as both stated and deliberately unstated: {sorted(overlap)}")
    for pointer in _all_schema_cap_pointers(SCHEMA):
        if pointer not in stated and pointer not in unstated:
            failures.append(
                f"{pointer} carries a maxLength the model is never told about and which is not "
                "recorded as deliberately unstated -- add it to a budget tuple or to "
                "LENGTH_BUDGETS_DELIBERATELY_UNSTATED with a reason"
            )


def test_every_budget_pointer_resolves(failures: list[str]) -> None:
    for label, pointer in (
        pp._PRIMARY_LENGTH_BUDGETS + pp._INTERNAL_NOTES_LENGTH_BUDGETS + pp._CRITIC_LENGTH_BUDGETS
    ):
        if pp.schema_max_length(SCHEMA, pointer) is None:
            failures.append(
                f"the budget for \"{label}\" points at {pointer}, which carries no maxLength in the "
                "active artifact -- the renderer silently omits that line, so the model is told "
                "nothing"
            )
    for pointer in pp.LENGTH_BUDGETS_DELIBERATELY_UNSTATED:
        if pp.schema_max_length(SCHEMA, pointer) is None:
            failures.append(
                f"{pointer} is recorded as deliberately unstated but carries no maxLength -- a "
                "stale exception hides the next real one"
            )


# ---------------------------------------------------------------------------
# [4] The assembled prompts carry the numbers
# ---------------------------------------------------------------------------


def test_primary_overlay_states_every_primary_budget(failures: list[str]) -> None:
    overlay = pp.BINARY_DECISION_OVERLAY_BLOCK
    for label, pointer in pp._PRIMARY_LENGTH_BUDGETS:
        cap = pp.schema_max_length(SCHEMA, pointer)
        if cap is None or not _states_budget(overlay, label, cap):
            failures.append(
                f"the OUTPUT CONTRACT block never tells the model that \"{label}\" is capped at "
                f"{cap} characters"
            )


def test_internal_notes_budget_is_gated_on_the_notes_mode(failures: list[str]) -> None:
    """The internal footnote's budget appears only in the variant that asks
    for the field. A review never told to write internal notes must not be
    handed a budget for one -- the same fail-closed direction the rest of the
    #522 gate takes."""
    internal = pp.render_binary_decision_overlay_block("internal")
    external = pp.render_binary_decision_overlay_block("external")
    for label, pointer in pp._INTERNAL_NOTES_LENGTH_BUDGETS:
        cap = pp.schema_max_length(SCHEMA, pointer)
        if cap is None or not _states_budget(internal, label, cap):
            failures.append(
                f"the internal-notes overlay never states the {cap}-character budget for "
                f"\"{label}\""
            )
        if label in external:
            failures.append(
                f"the external-notes overlay names \"{label}\", a field that mode never asks for"
            )


def test_critic_tasking_states_the_critic_budgets(failures: list[str]) -> None:
    for name, tasking in (
        ("CRITIC_TASKING_BLOCK", pp.CRITIC_TASKING_BLOCK),
        ("CRITIC_TASKING_BLOCK_NO_DOCUMENT", pp.CRITIC_TASKING_BLOCK_NO_DOCUMENT),
    ):
        for label, pointer in pp._CRITIC_LENGTH_BUDGETS:
            cap = pp.schema_max_length(SCHEMA, pointer)
            if cap is None or not _states_budget(tasking, label, cap):
                failures.append(f"{name} never states the {cap}-character budget for \"{label}\"")


def test_critic_budgets_reach_the_assembled_critic_prompt(failures: list[str]) -> None:
    """Assembled through the real assembler, not read off the constant: the
    tasking is a USER-prompt block, and issue #637 records what happens when
    only the constant is asserted over."""
    prompt = pp.assemble_user_prompt_critic(
        primary_output={"decision": "ACCEPT", "issues": []},
        doc_text="[p0001] The Institution shall place students with the Facility.",
    )
    cap = pp.schema_max_length(
        SCHEMA,
        "/definitions/CriticDelta/properties/contested_replacements/items/properties/critic_objection",
    )
    if cap is None or not _states_budget(prompt, "critic_objection", cap):
        failures.append(
            "the assembled critic user prompt does not carry critic_objection's character budget"
        )


# ---------------------------------------------------------------------------
# [5] Critic-only keys stay out of the shared block
# ---------------------------------------------------------------------------


def test_shared_overlay_names_no_critic_only_field(failures: list[str]) -> None:
    overlay = pp.BINARY_DECISION_OVERLAY_BLOCK
    for label in ("critic_objection", "critic_suggested_replacement", "critic_delta"):
        if label in overlay:
            failures.append(
                f"the shared OUTPUT CONTRACT block names \"{label}\": that block tells every "
                "reader to include the listed top-level keys and ONLY those, so naming a "
                "critic-only field there invites the PRIMARY pass to emit it"
            )


# ---------------------------------------------------------------------------
# [6] The rejection and the retry carry the numbers
# ---------------------------------------------------------------------------


def test_over_long_field_rejection_names_length_and_maximum(failures: list[str]) -> None:
    cap = pp.schema_max_length(SCHEMA, "/properties/verdict_summary/oneOf/1")
    assert isinstance(cap, int)
    over_long = "A" * (cap + 41)
    is_valid, error = pp.validate_model_response(
        json.dumps(_response(verdict_summary=over_long)), issue_provenance="model"
    )
    if is_valid:
        failures.append("an over-long verdict_summary was accepted")
        return
    text = str(error)
    if pp.LENGTH_BUDGET_MARKER not in text:
        failures.append(f"the rejection carries no length-budget clause: {text[:160]}")
    if f"{cap + 41}" not in text:
        failures.append("the rejection does not say how long the offending value actually was")
    if f"{cap}" not in text:
        failures.append("the rejection does not say what the maximum is")
    if "verdict_summary" not in text:
        failures.append("the rejection does not name the field")


def test_in_budget_response_still_validates(failures: list[str]) -> None:
    """The other side of the branch: a value AT the budget is accepted, so the
    clause above is a report on a real overage rather than a new rejection."""
    cap = pp.schema_max_length(SCHEMA, "/properties/verdict_summary/oneOf/1")
    assert isinstance(cap, int)
    is_valid, error = pp.validate_model_response(
        json.dumps(_response(verdict_summary="A" * cap)), issue_provenance="model"
    )
    if not is_valid:
        failures.append(f"a verdict_summary exactly at its {cap}-character budget was rejected: {error}")


def test_retry_correction_carries_the_budget_and_forbids_dropping_work(failures: list[str]) -> None:
    cap = pp.schema_max_length(SCHEMA, "/properties/verdict_summary/oneOf/1")
    assert isinstance(cap, int)
    _is_valid, error = pp.validate_model_response(
        json.dumps(_response(verdict_summary="A" * (cap + 7))), issue_provenance="model"
    )
    correction = pp.render_retry_correction_block(error)
    if f"{cap}" not in correction:
        failures.append(
            "the retry correction does not carry the field's maximum -- the model is asked to "
            "shorten to a target it was never given, which is what burned both live attempt "
            "budgets"
        )
    if f"{cap + 7}" not in correction:
        failures.append("the retry correction does not carry the offending value's own length")
    lowered = correction.lower()
    if "condense" not in lowered:
        failures.append("the retry correction does not tell the model to condense the prose")
    if "do not drop an issue" not in lowered:
        failures.append(
            "the retry correction does not forbid dropping an issue to fit: a model told only "
            "\"too long\" can comply by saying less, which is a shorter response and a worse "
            "review"
        )


def test_non_length_schema_failure_keeps_the_generic_framing(failures: list[str]) -> None:
    """Branch coverage: the length branch must not swallow every other
    schema failure. A bad `confidence_state` is the failure #417 was written
    for and must still be framed as a shape problem."""
    _is_valid, error = pp.validate_model_response(
        json.dumps(_response(confidence_state="medium")), issue_provenance="model"
    )
    correction = pp.render_retry_correction_block(error)
    if "did not match the required response shape" not in correction:
        failures.append(
            "a non-length schema failure no longer gets the generic shape framing: "
            f"{correction[:200]}"
        )
    if "character budget" in correction:
        failures.append("a non-length schema failure was framed as a length failure")


def test_length_budget_detail_handles_a_root_level_failure(failures: list[str]) -> None:
    """`location` is empty for a failure at the document root. The clause
    must still read as a sentence rather than quoting an empty name."""
    detail = pp.render_length_budget_detail(location="", value_length=12, maximum=8)
    if '""' in detail:
        failures.append(f"an empty location renders as an empty quoted field name: {detail}")
    if "12" not in detail or "8" not in detail:
        failures.append(f"the clause dropped one of its numbers: {detail}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_realistic_critic_objection_validates,
    test_realistic_rationale_objection_validates,
    test_realistic_verdict_summary_validates,
    test_raised_fields_reached_the_free_prose_class,
    test_deliberate_caps_are_unchanged,
    test_every_cap_is_stated_or_deliberately_unstated,
    test_every_budget_pointer_resolves,
    test_primary_overlay_states_every_primary_budget,
    test_internal_notes_budget_is_gated_on_the_notes_mode,
    test_critic_tasking_states_the_critic_budgets,
    test_critic_budgets_reach_the_assembled_critic_prompt,
    test_shared_overlay_names_no_critic_only_field,
    test_over_long_field_rejection_names_length_and_maximum,
    test_in_budget_response_still_validates,
    test_retry_correction_carries_the_budget_and_forbids_dropping_work,
    test_non_length_schema_failure_keeps_the_generic_framing,
    test_length_budget_detail_handles_a_root_level_failure,
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
        else:
            for failure in failures[before:]:
                print(f"FAIL: {failure}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: all length-budget (issue #674) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
