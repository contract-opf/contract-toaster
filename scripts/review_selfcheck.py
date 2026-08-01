#!/usr/bin/env python3
"""The closing self-check: re-read the FINISHED REDLINE against what bound it.

`opf_prompt.BINDING_INTRO` ends every Binding block with a promise to the
model: "Every entry here is re-read against your finished redline by the
closing self-check." This module is that re-read, and until it existed the
promise was prose. The only thing checking whether the output honoured the
rules was the model that wrote it.

## NOT YET WIRED — read this before assuming it runs

Landed 2026-08-01 as a tested module only. `review_spine.run_review()` does
NOT call it, and no live review invokes it today. It sits alongside the rest
of the OPF knowledge path (`scripts/opf_prompt.py`, `scripts/review_knowledge
.py`), which is likewise built, tested, and not yet composed into the running
pipeline -- `scripts/primary_review_pass.py` says so explicitly at its Floor-
block comment ("explicitly not composed through this module").

So the `BINDING_INTRO` promise above is currently latent, not broken: the
block that makes it is not reached by a live review either. Wiring the two
together is a single deliberate step, and it must be done as a pair -- wiring
the prompt path without this module would start making the model a promise
nothing keeps.

The wiring itself (threading a `ReviewKnowledge` through `run_review` into
both model passes, plus the registry/reviews-side plumbing) lives unmerged on
`feat/opf-03-f1-wiring`, together with its own tests
(`test_registry_knowledge_mode.py`, `test_lineage_cannot_lie.py`) which are
red against `main` precisely because that plumbing is absent.

## What a verdict is, and what it is not

Per `must` rule, exactly one verdict against the finished redline:
`compliant` or `tension_flagged`. There is no third value, and
`tension_flagged` is deliberately not "violated": a policy `must` in tension
with the facts of a document is an attorney's determination in EITHER
direction (`playbooks/policy.schema.json`; `tests/test_policy_document.py`
-> "the model reads `text`, not approval metadata"), so this step's whole job
is to surface the tension, never to resolve it. The transcript ships with
the attribution manifest for a human to read.

## Coverage is the load-bearing part (see `fail_closed`)

A self-check that judges 6 of 7 rules is worse than no self-check: it
produces a complete-LOOKING transcript attesting to a re-read that did not
happen for the one rule that mattered. So an UNJUDGED rule (invalid response
after its one bounded retry) never becomes silence -- it lands in `unjudged`,
`fail_closed` goes True, and `terminal_status_for` maps that to the
`MANUAL_REVIEW_REQUIRED` system status (docs/output-contract.md: "The
decision is binary; uncertainty is a system status"). Identical shape to
`scripts/floor_judge.py`'s per-invariant coverage gate, which this module
otherwise clones: fixed system prompt, strict JSON, one bounded re-invoke,
fail closed.

And ZERO `must` rules produces an EMPTY transcript, never a fabricated pass.
"nothing to check" and "checked, all clear" are different facts.

## Replacement-text bounds (`replacement_bounds`)

`scripts/replacement_text_enforcement.py` and its only config source,
`playbooks/pen-rules.defaults.json`, are retired by PR F2. Those defaults are
toaster-GLOBAL bind config (issue #293): they forbid a small set of phrases
in replacement text for EVERY playbook, and of the three shipped today
("unlimited liability", "irrevocable", "perpetual") only the first two are
covered by `must` rules in the new policies. After F2 this step is the ONLY
thing between a model's proposed language and a contract, so it judges each
introduced text against each bound as well.

Two properties of that check are non-negotiable:

  1. The bounds are an INPUT, never literals in this file. They are governed
     config -- an operator approves them, a bind records them -- and a bound
     hardcoded here is a rule no one approved and no lineage records.
     `bind_config_replacement_bounds` reads them off the bound artifact.
  2. The judgment is the MODEL's. No regex, no phrase search, not even as a
     pre-filter: a lexical gate is precisely what this migration replaces.
     "may not be revoked, withdrawn, or terminated" introduces irrevocability
     without the word, and `find_spans` cannot see it.

## NO rule text or document substance in logs/exceptions

Only ids ever surface here -- `check_id` (rule-id-shaped), `subject_id`, a
`section_ref` -- never a rule's `text`, never an introduced replacement, never
a raw model response body. Rule prose is confidential playbook substance and
introduced text is contract substance, and a transcript ships with the
attribution manifest while a log line ships wherever logs ship. Same
discipline as `scripts/floor_judge.py` and
`scripts/review_spine.py::_issue_from_detector_fire`.

MOCKED-MODEL, offline, deterministic: driven entirely by an injected
`model_client.BedrockModelClient`. No network.

Voicing: white-labelled -- "you"/"your", never a tenant name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

VERDICT_COMPLIANT = "compliant"
VERDICT_TENSION_FLAGGED = "tension_flagged"

#: The complete verdict vocabulary. A response outside it is INVALID, not a
#: new category: "unclear"/"partial" from a model is the uncertainty that
#: `fail_closed` exists to turn into a system status.
VERDICTS = frozenset({VERDICT_COMPLIANT, VERDICT_TENSION_FLAGGED})

KIND_MUST_RULE = "must_rule"
KIND_REPLACEMENT_BOUND = "replacement_bound"

#: docs/output-contract.md's system status for uncertainty. Mirrors
#: `review_spine.STATUS_MANUAL_REVIEW_REQUIRED`; owned here too so
#: `terminal_status_for` is a complete, testable statement of the mapping
#: rather than a convention each caller re-implements (and one day forgets).
STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

#: The reason code a caller surfaces with that status.
REASON_SELF_CHECK_UNJUDGED = "self_check_unjudged"

# One bounded re-invoke per check, then that check fails closed. Mirrors
# floor_judge._MAX_RETRIES_PER_INVARIANT / primary_review_pass
# .MAX_RETRIES_PER_PASS's "1 initial + 1 retry" shape.
_MAX_RETRIES_PER_CHECK = 1

_TENSION_NOTE_MAX_CHARS = 200


class SelfCheckConfigError(ValueError):
    """Raised when an input cannot produce an attributable verdict.

    Currently: a `must` rule with no `id`. A verdict has to be recorded
    AGAINST something -- an id-less rule would ship a transcript attesting to
    the re-read of a rule nobody can name, which is the complete-looking
    transcript this module exists to prevent. A config bug, so it is loud
    (mirrors `detector_common.DetectorConfigError`), and per this module's
    logging discipline the message names the rule by position, never by
    quoting it.
    """


# ---------------------------------------------------------------------------
# Fixed system prompts. Only the user prompt varies per check -- one per
# question, because the two questions are genuinely different and a single
# merged prompt would ask the model to hold both at once.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_MUST_RULE = """You are the closing self-check for a contract review system.

You are given ONE binding rule and the FINISHED REDLINE a reviewer produced.
Your ONLY task is to decide whether that redline, as written, complies with
that ONE rule. You do not review the document for anything else, you do not
propose replacement language, and you do not re-decide the review.

Verdicts:
  "compliant" -- the finished redline is consistent with the rule.
  "tension_flagged" -- the redline is in tension with the rule, or the rule
    appears inapplicable to what the redline did, or you cannot reconcile the
    two.

Do NOT resolve a tension. Surfacing it is the entire task: the determination
is an attorney's to make, in either direction. When in doubt, flag it.

Respond with STRICT JSON ONLY -- no prose, no markdown fencing -- in exactly
this shape:

{"check_id": "<the check_id you were given>", "verdict": "compliant"|"tension_flagged", "tension_note": "<one short sentence naming the tension, <=200 chars, empty string when compliant>"}
"""

_SYSTEM_PROMPT_REPLACEMENT_BOUND = """You are the closing self-check on replacement language for a contract review system.

You are given ONE bound that replacement language must not introduce into a
contract, and ONE passage of INTRODUCED_TEXT that a reviewer proposes to
insert. Your ONLY task is to decide whether that passage introduces that
bound. You do not judge whether the passage is good drafting, and you do not
propose alternative language.

Judge SUBSTANCE, not wording. A passage introduces the bound if it achieves
it in effect, whatever words it uses -- language that cannot be revoked
introduces irrevocability whether or not the word appears. Equally, a passage
that merely mentions the bound (for example, to disclaim or exclude it) does
not thereby introduce it.

Verdicts:
  "compliant" -- the passage does not introduce the bound.
  "tension_flagged" -- the passage introduces the bound, or arguably does.

When in doubt, flag it: this text is about to reach a contract.

Respond with STRICT JSON ONLY -- no prose, no markdown fencing -- in exactly
this shape:

{"check_id": "<the check_id you were given>", "verdict": "compliant"|"tension_flagged", "tension_note": "<one short sentence naming the tension, <=200 chars, empty string when compliant>"}
"""


@dataclass(frozen=True)
class _Check:
    """One question, already rendered. `subject_id` is what a verdict is
    recorded against (a rule id, or a bound's floor_ref/position)."""

    check_id: str
    kind: str
    subject_id: str
    section_ref: Optional[str]
    system_prompt: str
    user_prompt: str


@dataclass
class SelfCheckTranscript:
    """The re-read, as a record.

    `entries`: one per check that produced a VALID verdict, each
    `{check_id, kind, subject_id, section_ref, verdict, tension_note}`.
    `unjudged`: `check_id`s with no valid verdict after their bounded retry --
    the coverage gate. Ids only in both: no rule text, no contract text.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    unjudged: list[str] = field(default_factory=list)

    @property
    def fail_closed(self) -> bool:
        """True whenever ANY check went unjudged. An unjudged binding rule is
        not a quiet pass: see `terminal_status_for`."""
        return bool(self.unjudged)

    def flagged(self) -> list[dict[str, Any]]:
        """Every recorded tension. Never derived from silence: an entry exists
        here only because a judge returned a valid `tension_flagged`."""
        return [e for e in self.entries if e["verdict"] == VERDICT_TENSION_FLAGGED]

    def record(self) -> dict[str, Any]:
        """The POSITIVE record, for the attribution manifest (G2 consumes it).

        States what was checked rather than leaving a reader to infer it:
        `checks_run: 0` is a claim ("nothing bound this redline"), whereas a
        transcript with no entries and no count is an ambiguity between that
        and "the self-check never ran".
        """
        return {
            "checks_run": len(self.entries) + len(self.unjudged),
            "entries": [dict(entry) for entry in self.entries],
            "unjudged": list(self.unjudged),
            "flagged_count": len(self.flagged()),
            "fail_closed": self.fail_closed,
        }


def terminal_status_for(transcript: SelfCheckTranscript) -> Optional[str]:
    """`MANUAL_REVIEW_REQUIRED` when the transcript is fail-closed, else None.

    The mapping lives here, next to the gate it interprets, so a caller cannot
    hold a fail-closed transcript and quietly continue -- and so the mapping
    itself is a property a test can assert without standing up a pipeline.
    A flagged tension is NOT terminal: it is a fact for an attorney to read,
    and blocking on it would make the reviewer's own judgement unappealable.
    """
    return STATUS_MANUAL_REVIEW_REQUIRED if transcript.fail_closed else None


# ---------------------------------------------------------------------------
# Rendering the finished redline for the judge.
# ---------------------------------------------------------------------------


def introduced_texts(reconciled_result: dict[str, Any]) -> list[tuple[str, str]]:
    """`(section_ref, text)` for every non-empty `proposed_replacement_text`.

    This IS the `<w:ins>` content: `redline_generate.generate_redline` ->
    `redline_patch` inserts each issue's `proposed_replacement_text` verbatim
    as the inserted run. Running BEFORE the redline is generated (rather than
    re-parsing OOXML after) checks the same bytes one stage earlier, and is
    what lets a fail-closed self-check stop the document from being built at
    all. An empty replacement is a flag-only issue -- no text reaches the
    contract, so there is nothing to bound.
    """
    out: list[tuple[str, str]] = []
    for issue in reconciled_result.get("issues") or []:
        text = issue.get("proposed_replacement_text") or ""
        if text:
            out.append((issue.get("section_ref") or "", text))
    return out


def render_redline_for_selfcheck(reconciled_result: dict[str, Any]) -> str:
    """The finished redline as the judge reads it: per issue, where it lands,
    what it decided, and the language it introduces."""
    lines = [f"Overall decision: {reconciled_result.get('decision')}"]
    for issue in reconciled_result.get("issues") or []:
        lines.append("")
        lines.append(f"[{issue.get('section_ref')}] {issue.get('section_title') or ''}".rstrip())
        lines.append(f"  decision: {issue.get('decision')}")
        lines.append(f"  change under review: {issue.get('counterparty_change_summary') or ''}")
        replacement = issue.get("proposed_replacement_text") or ""
        lines.append(
            f"  introduced text: {replacement}"
            if replacement
            else "  introduced text: (none -- flagged only, no language is inserted)"
        )
    return "\n".join(lines)


def bind_config_replacement_bounds(bundle: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The replacement-text bounds a bound artifact carries, or `[]`.

    Reads `pen_rules.default.must_not_introduce` off a v2 bundle -- the
    governed bind-config home these values already have (issue #293;
    `bind_bundle.py --pen-rules` validates each `floor_ref` against the OPF's
    own `floor.invariants` ids at bind time). PR F2 deletes
    `playbooks/pen-rules.defaults.json`, the toaster-global LAYER of that same
    config, along with its only consumer; the values it ships must move into
    this bind config rather than into a literal in
    `scripts/review_selfcheck.py`, or an operator loses both the approval and
    the record.

    Returns `[]` for a bundle with no pen rules (every v1 playbook). That is
    deliberately not a refusal HERE: this function reports what the artifact
    says, and whether an unbounded review is acceptable is a governance call,
    which in this codebase has exactly one home (`scripts/review_knowledge.py`)
    and it is not this module.
    """
    pen_rules = (bundle or {}).get("pen_rules") or {}
    default = pen_rules.get("default") or {}
    bounds = default.get("must_not_introduce") or []
    return [entry for entry in bounds if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# Check construction.
# ---------------------------------------------------------------------------


def _must_rule_checks(must_rules: list[dict[str, Any]], redline: str) -> list[_Check]:
    checks: list[_Check] = []
    for index, rule in enumerate(must_rules):
        rule_id = rule.get("id")
        if not rule_id:
            raise SelfCheckConfigError(
                f"must rule at position {index} carries no `id`: its verdict could not be "
                f"attributed to any rule, and the transcript would attest to a re-read nobody "
                f"can trace. Give the rule an id in the approved policy."
            )
        checks.append(
            _Check(
                check_id=f"must:{rule_id}",
                kind=KIND_MUST_RULE,
                subject_id=str(rule_id),
                section_ref=None,
                system_prompt=_SYSTEM_PROMPT_MUST_RULE,
                user_prompt=(
                    f"check_id: must:{rule_id}\n"
                    f"rule: {rule.get('text', '')}\n"
                    "\n"
                    "<FINISHED_REDLINE>\n"
                    f"{redline}\n"
                    "</FINISHED_REDLINE>\n"
                ),
            )
        )
    return checks


def _bound_checks(
    replacement_bounds: list[dict[str, Any]], introductions: list[tuple[str, str]]
) -> list[_Check]:
    """One check per (bound, introduction) pair.

    Per pair rather than per introduction: a judge asked about three bounds at
    once answers about the one it noticed, and a single verdict covering three
    bounds cannot record which of them it was actually about -- the same
    everything-in-one-slot shape the Binding block's provenance tags exist to
    break.
    """
    checks: list[_Check] = []
    for index, bound in enumerate(replacement_bounds):
        phrase = bound.get("phrase")
        if not phrase:
            raise SelfCheckConfigError(
                f"replacement bound at position {index} carries no `phrase`: there is nothing "
                f"for the judge to be asked about (floor_ref={bound.get('floor_ref')!r})."
            )
        subject_id = bound.get("floor_ref") or f"bound.{index}"
        for section_ref, text in introductions:
            check_id = f"bound:{index}@{section_ref}"
            checks.append(
                _Check(
                    check_id=check_id,
                    kind=KIND_REPLACEMENT_BOUND,
                    subject_id=str(subject_id),
                    section_ref=section_ref,
                    system_prompt=_SYSTEM_PROMPT_REPLACEMENT_BOUND,
                    user_prompt=(
                        f"check_id: {check_id}\n"
                        f"bound: this text must not introduce: {phrase}\n"
                        f"section: {section_ref}\n"
                        "\n"
                        "<INTRODUCED_TEXT>\n"
                        f"{text}\n"
                        "</INTRODUCED_TEXT>\n"
                    ),
                )
            )
    return checks


def _validate_response(raw_text: str, *, expected_check_id: str) -> tuple[bool, dict[str, Any] | None]:
    """Parse + strictly validate one judge response.

    Mirrors `floor_judge._validate_judge_response` (and, behind it,
    `primary_review_pass.validate_model_response`): reject non-JSON, a wrong
    `check_id`, a verdict outside `VERDICTS`, or a non-string/over-length
    note. Never best-effort-patched, and never coerced to `compliant`: an
    unparseable answer is an ABSENT verdict, which is what `unjudged` is for.
    """
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return False, None
    if not isinstance(parsed, dict):
        return False, None
    if parsed.get("check_id") != expected_check_id:
        return False, None
    verdict = parsed.get("verdict")
    if verdict not in VERDICTS:
        return False, None
    note = parsed.get("tension_note", "")
    if not isinstance(note, str) or len(note) > _TENSION_NOTE_MAX_CHARS:
        return False, None
    return True, {"verdict": verdict, "tension_note": note}


def run_self_check(
    *,
    reconciled_result: dict[str, Any],
    must_rules: list[dict[str, Any]],
    replacement_bounds: list[dict[str, Any]],
    model_client: Any,
    model_id: str,
    max_output_tokens: int = 1024,
) -> SelfCheckTranscript:
    """Re-read the finished redline against every rule that bound it.

    `must_rules` is `review_knowledge.ReviewKnowledge.must_rules()` -- only
    the rules that actually REACHED the prompt (a textless rule is dropped and
    recorded by `opf_prompt.policy_rules_by_strength`), because re-reading the
    output against a rule the model was never given tests the model on a rule
    it could not have followed.

    `replacement_bounds` is bind config, ordinarily
    `bind_config_replacement_bounds(bundle)`. Never defaulted to a literal
    here: see the module docstring.

    `reconciled_result` is `reconciliation.reconcile()`'s merged
    output-schema-v1 dict -- the finished redline's content, one stage before
    `redline_generate` turns it into OOXML.

    One `model_client.invoke()` per check, plus one bounded retry for a check
    whose response fails validation. Never raises on a judge failure (an
    invalid-after-retry check lands in `unjudged` -> `fail_closed`); raises
    `SelfCheckConfigError` only for an input that could not produce an
    attributable verdict at all.

    With no rules and no bounds it makes NO model call and returns an EMPTY
    transcript -- not a pass.
    """
    checks = _must_rule_checks(must_rules, render_redline_for_selfcheck(reconciled_result))
    checks += _bound_checks(replacement_bounds, introduced_texts(reconciled_result))

    transcript = SelfCheckTranscript()
    attempts_allowed = 1 + _MAX_RETRIES_PER_CHECK

    for check in checks:
        verdict: dict[str, Any] | None = None
        for _attempt in range(attempts_allowed):
            raw_response = model_client.invoke(
                model_id=model_id,
                system_prompt=check.system_prompt,
                user_prompt=check.user_prompt,
                max_output_tokens=max_output_tokens,
            )
            is_valid, parsed = _validate_response(raw_response, expected_check_id=check.check_id)
            if is_valid:
                verdict = parsed
                break

        if verdict is None:
            transcript.unjudged.append(check.check_id)
            continue
        transcript.entries.append(
            {
                "check_id": check.check_id,
                "kind": check.kind,
                "subject_id": check.subject_id,
                "section_ref": check.section_ref,
                "verdict": verdict["verdict"],
                "tension_note": verdict["tension_note"],
            }
        )

    return transcript
