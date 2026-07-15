#!/usr/bin/env python3
"""
Third-party paper: position-level findings (accept/flag/reject) over
matched clauses (issue #250, Third-party-paper support Slice 4 of 5).

## Problem this solves

The first-party pipeline produces findings from DIFF HUNKS against your
form. Third-party paper has no such diff (#202) -- there is nothing to
"hunk", because a counterparty's own template has no heading-level
correspondence to your form (#249's whole reason for existing). Instead,
each segmented counterparty clause (#248) has already been assigned to
zero-or-one playbook topic by content similarity (#249's
`match_clauses_to_playbook()`), and THIS module walks that assignment --
per playbook TOPIC, not per diff hunk -- and decides accept / flag /
reject.

A playbook position with NO matched clause is itself a finding: the
counterparty simply omitted a term you require. Silence is never a valid
outcome here (see `_missing_position_finding`).

## Design: deterministic hard-position checks first, model judgement second

Two kinds of check, applied in this order for every matched clause:

  1. MECHANICAL (deterministic, no model call): does the clause text fire
     any `hard_rejections` rule scoped to this topic (`applies_to_topics`)?
     Reuses `scripts/detector_common.py`'s span-level `on_insert` /
     `on_remove_or_alter` checkers UNCHANGED (issue #212/#213/#220) --
     the same rule grammar and semantics the first-party diff-hunk path
     already uses, just applied to the WHOLE clause text as the "surface"
     instead of a diff hunk's inserted/modified span. This is the natural
     third-party analog: on third-party paper the counterparty's clause
     IS what they proposed (an "insertion" from your form's point of
     view), and a required-token check against that same clause text
     tells you whether it preserves what you require -- no diff needed
     for either kind of rule.

     A fire here is authoritative (`decision="reject"`) -- these are your
     Floor rules; the model is never given a chance to override a rule
     fire, exactly as the first-party path never lets the model auto-
     accept past one (see `docs/playbook-governance.md`,
     `backend/src/disposition.py`).

  2. JUDGEMENT (only when no rule fired): the clause may still deviate
     from your position in a way no lexical rule catches -- ambiguous
     phrasing, a numeric threshold, one of a topic's free-text
     `reject_if_proposed` deviations that (per `playbooks/schema.json`'s
     own `hard_rejections[].protects` description) is deliberately NOT
     decided by the deterministic detector layer. This slice asks the
     injectable, deterministic `FakeBedrockClient`
     (`backend/src/model_client.py`) for a structured `{"decision":
     "accept"|"flag"|"reject", "rationale": "..."}` judgement, offline and
     reproducible -- no live Bedrock call.

## Missing positions

A playbook topic with an empty `topic_matches[topic_id]` list (#249's
output) never reached step 1 or 2 above -- there is no clause to check.
Whether that omission is itself a hard rejection is decided mechanically,
without a model call: a topic is treated as REQUIRED (missing => `reject`)
when either (a) its own `hard_rejection_refs` is non-empty (the playbook
already names it as guarded by a Floor rule), or (b) an `on_remove_or_alter`
hard_rejection rule is scoped to it via `applies_to_topics` (that rule
protects language that must appear SOMEWHERE; total absence trivially
fails its `required_tokens` check). A non-required topic's omission still
produces a finding -- `decision="flag"` -- never silence.

## De-branding

Every rationale string this module emits is a TEMPLATE this module
controls, or comes from a `FakeBedrockClient` response an injecting
caller controls -- never verbatim playbook prose (`hard_rejections[].
description` and `topics[].exos_standard` routinely contain the literal
word "Exos" -- see `playbooks/eiaa-v1.0.0.json`). Rationale text uses
"your" voicing and is asserted (in `tests/test_third_party_position_
findings.py`) free of "Exos"/"EXOS", per the project-wide de-brand rule.

## Output

`evaluate_position_findings()` returns a list of findings, one per
(topic, matched clause) pair plus one per topic with no matched clause,
covering every playbook topic id at least once:

  {
    "playbook_topic_id": <id>,
    "clause_id": <id> | None,       # None only for a missing-position finding
    "decision": "accept" | "flag" | "reject",
    "rationale": "<'your'-voiced, Exos-free human-facing text>",
    "source": "hard_rejection" | "model_judgement" | "missing_position",
  }

A `source="hard_rejection"` finding additionally carries `"rule_id"`
(the firing `hard_rejections[].id`) for machine/audit use -- never
interpolated into `rationale` itself, since some rule ids (e.g.
"no-exos-indemnity") name the org they protect.

Findings carry the source `clause_id` so Slice 5 (output/redline
integration, out of scope here) can anchor a finding back to the
counterparty text it came from.

See: issue #250, issue #249 (`scripts/third_party_clause_matching.py`,
this module's assignment input shape), `scripts/detector_common.py`
(the shared on_insert/on_remove_or_alter rule checkers), `backend/src/
model_client.py` (`FakeBedrockClient`), `playbooks/eiaa-v1.0.0.json`
(topics, hard_rejections).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import detector_common  # noqa: E402
import model_client as model_client_module  # noqa: E402

VALID_DECISIONS = ("accept", "flag", "reject")

DEFAULT_MAX_OUTPUT_TOKENS = 512

# System prompt for the "softer judgement" model call -- issued only for a
# matched clause no deterministic hard_rejection rule fired on. Instructs
# the model to respond with ONLY a JSON object so this module can parse it
# without a heavier structured-output/schema-validation/retry stack (that
# machinery is Slice-5/primary-review-pass territory; this slice's model
# call is a narrow, single-purpose judgement, not the full review pass).
_SYSTEM_PROMPT = (
    "You are a contract-review assistant judging ONE counterparty-proposed "
    "clause against a single negotiation position from your playbook. "
    "Decide exactly one of: 'accept' (the clause is within your position or "
    "a documented acceptable variation), 'flag' (the clause needs attorney "
    "judgement -- ambiguous, partial, or a gray area no deterministic rule "
    "covers), or 'reject' (the clause matches one of your listed "
    "unacceptable deviations). Respond with ONLY a JSON object of the exact "
    'shape {"decision": "accept|flag|reject", "rationale": "<one or two '
    "sentences, second-person 'your' voicing, never the words 'Exos' or "
    '\'EXOS\'>"}. No prose outside the JSON object.'
)


class PositionFindingError(ValueError):
    """Raised when a matched clause record or a model judgement response
    cannot be turned into a finding -- e.g. a clause_id absent from the
    supplied clause_records, or a model response that is not valid JSON /
    does not carry a recognized decision. Per this codebase's fail-loud
    convention for detector/judgement config errors (see
    `detector_common.DetectorConfigError`), a malformed judgement is a
    build/response failure, not something silently downgraded to a
    guessed decision.
    """


def _clause_lookup(clause_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {clause["clause_id"]: clause for clause in clause_records}


def _clause_surface_text(clause: dict[str, Any]) -> str:
    heading = clause.get("heading") or ""
    text = clause.get("text") or ""
    return f"{heading}\n{text}".strip()


def _applicable_hard_rejections(
    topic_id: str, hard_rejections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        rule for rule in hard_rejections if topic_id in rule.get("applies_to_topics", [])
    ]


def _topic_requires_presence(
    topic: dict[str, Any], applicable_rules: list[dict[str, Any]]
) -> bool:
    """A topic is REQUIRED -- its total absence is itself a hard rejection
    -- when the playbook already names it as Floor-guarded
    (`hard_rejection_refs` non-empty), or when an `on_remove_or_alter` rule
    is scoped to it: that rule protects language that must appear
    somewhere, so a topic with no matched clause at all trivially fails
    the rule's `required_tokens` check."""
    if topic.get("hard_rejection_refs"):
        return True
    return any(rule.get("kind") == "on_remove_or_alter" for rule in applicable_rules)


def _first_hard_rejection_fire(
    applicable_rules: list[dict[str, Any]], clause_text: str, topic_id: str
) -> dict[str, Any] | None:
    """The first hard_rejection rule (in playbook order) that fires against
    `clause_text` for this topic, or None if none do. The whole clause text
    is passed as BOTH the on_insert "inserted" surface and the
    on_remove_or_alter "altered" text: on third-party paper the
    counterparty's clause text IS what they proposed (no diff exists to
    separate "inserted" from "unchanged"), so the entire clause is the
    surface a Floor rule must be checked against."""
    for rule in applicable_rules:
        kind = rule.get("kind")
        if kind == "on_insert":
            fires = detector_common.check_on_insert_rule_fires(rule, clause_text, topic_id)
        elif kind == "on_remove_or_alter":
            fires = detector_common.check_on_remove_or_alter_rule_fires(
                rule, clause_text, topic_id
            )
        else:
            fires = []
        if fires:
            return rule
    return None


def _topic_label(topic: dict[str, Any]) -> str:
    return topic.get("section_ref") or topic.get("id", "this position")


def _hard_rejection_finding(
    topic: dict[str, Any], clause_id: str, rule: dict[str, Any]
) -> dict[str, Any]:
    # `rule["id"]` is a machine identifier some playbooks name after the
    # org they protect (e.g. "no-exos-indemnity") -- it is recorded in the
    # finding's own `rule_id` field for machine/audit use, but NEVER
    # interpolated into `rationale`, which is human-facing prose asserted
    # free of "Exos"/"EXOS" (de-brand rule).
    rationale = (
        f"This clause conflicts with a required position under "
        f"{_topic_label(topic)} and cannot be accepted as proposed; it "
        f"violates one of your negotiation Floor rules."
    )
    return {
        "playbook_topic_id": topic["id"],
        "clause_id": clause_id,
        "decision": "reject",
        "rationale": rationale,
        "source": "hard_rejection",
        "rule_id": rule["id"],
    }


def _missing_position_finding(topic: dict[str, Any], required: bool) -> dict[str, Any]:
    label = _topic_label(topic)
    if required:
        rationale = (
            f"No counterparty clause was matched to your required position "
            f"under {label}; this is a term you require, and its omission "
            f"cannot be accepted without addressing it."
        )
        decision = "reject"
    else:
        rationale = (
            f"No counterparty clause was matched to your position under "
            f"{label}; confirm with attorney judgement whether leaving this "
            f"unaddressed is acceptable for this agreement."
        )
        decision = "flag"
    return {
        "playbook_topic_id": topic["id"],
        "clause_id": None,
        "decision": decision,
        "rationale": rationale,
        "source": "missing_position",
    }


def _build_user_prompt(topic: dict[str, Any], clause_text: str) -> str:
    payload = {
        "topic_section": topic.get("section_ref"),
        "your_position": topic.get("exos_standard"),
        "must_preserve": topic.get("must_preserve", []),
        "acceptable_variations": [
            variation.get("to") for variation in topic.get("acceptable_variations", [])
        ],
        "unacceptable_deviations": topic.get("reject_if_proposed", []),
        "counterparty_clause_text": clause_text,
    }
    return json.dumps(payload, indent=2)


def _parse_model_judgement(raw_response: str, topic_id: str, clause_id: str) -> dict[str, str]:
    try:
        data = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise PositionFindingError(
            f"model judgement for topic={topic_id!r} clause_id={clause_id!r} was not "
            f"valid JSON: {raw_response!r} ({exc})"
        ) from exc

    decision = data.get("decision")
    if decision not in VALID_DECISIONS:
        raise PositionFindingError(
            f"model judgement for topic={topic_id!r} clause_id={clause_id!r} has "
            f"unrecognized decision {decision!r} (expected one of {VALID_DECISIONS}): "
            f"{raw_response!r}"
        )

    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise PositionFindingError(
            f"model judgement for topic={topic_id!r} clause_id={clause_id!r} is missing "
            f"a non-empty 'rationale' string: {raw_response!r}"
        )

    return {"decision": decision, "rationale": rationale}


def _model_judged_finding(
    topic: dict[str, Any],
    clause_id: str,
    clause_text: str,
    model_client: Any,
    model_id: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    raw_response = model_client.invoke(
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(topic, clause_text),
        max_output_tokens=max_output_tokens,
    )
    judgement = _parse_model_judgement(raw_response, topic["id"], clause_id)
    return {
        "playbook_topic_id": topic["id"],
        "clause_id": clause_id,
        "decision": judgement["decision"],
        "rationale": judgement["rationale"],
        "source": "model_judgement",
    }


def evaluate_position_findings(
    clause_records: list[dict[str, Any]],
    match_result: dict[str, Any],
    playbook: dict[str, Any],
    model_client: Any,
    *,
    model_id: str | None = None,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> list[dict[str, Any]]:
    """Evaluate one position-level finding per playbook topic, over that
    topic's matched clauses (#249's `match_result["topic_matches"]`).

    `clause_records` is #248's segmented-clause list (`{"clause_id": ...,
    "heading": ..., "text": ..., "order": ...}`); `match_result` is #249's
    `match_clauses_to_playbook()` output. `model_client` is any object
    implementing `model_client.BedrockModelClient`'s `invoke()` Protocol --
    ordinarily a `FakeBedrockClient` in tests/offline runs. `model_id`
    defaults to the pinned primary-reviewer model id
    (`model_client.primary_model_id()`) when not supplied.

    Returns a list of findings (see module docstring for the shape), one
    per (topic, matched clause) pair plus one per topic with NO matched
    clause -- every playbook topic id appears in at least one finding.
    Deterministic and offline: no randomness, no network call of any kind;
    the only "judgement" step goes through the injected `model_client`.
    """
    resolved_model_id = model_id if model_id is not None else model_client_module.primary_model_id()

    clause_by_id = _clause_lookup(clause_records)
    hard_rejections = playbook.get("hard_rejections", [])
    topic_matches = match_result.get("topic_matches", {})

    findings: list[dict[str, Any]] = []
    for topic in playbook.get("topics", []):
        topic_id = topic.get("id")
        if not topic_id:
            continue
        applicable_rules = _applicable_hard_rejections(topic_id, hard_rejections)
        matched_clause_ids = topic_matches.get(topic_id, [])

        if not matched_clause_ids:
            required = _topic_requires_presence(topic, applicable_rules)
            findings.append(_missing_position_finding(topic, required))
            continue

        for clause_id in matched_clause_ids:
            clause = clause_by_id.get(clause_id)
            if clause is None:
                raise PositionFindingError(
                    f"topic {topic_id!r} was matched to clause_id {clause_id!r}, but "
                    f"no clause record with that id was supplied in clause_records"
                )
            clause_text = _clause_surface_text(clause)

            fired_rule = _first_hard_rejection_fire(applicable_rules, clause_text, topic_id)
            if fired_rule is not None:
                findings.append(_hard_rejection_finding(topic, clause_id, fired_rule))
                continue

            findings.append(
                _model_judged_finding(
                    topic,
                    clause_id,
                    clause_text,
                    model_client,
                    resolved_model_id,
                    max_output_tokens,
                )
            )

    return findings


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: evaluates findings for a couple of hand-built
    clauses against the committed eiaa-v1.0.0 playbook, using a
    FakeBedrockClient seeded with one canned 'accept' response."""
    import corpus
    import third_party_clause_matching

    playbook = corpus._load_playbook()
    clauses = [
        {
            "clause_id": "clause_smoke_confidentiality",
            "heading": "Confidentiality",
            "text": (
                "Each party agrees to maintain the confidentiality of all "
                "Confidential Information disclosed by the other party."
            ),
            "order": 0,
        },
    ]
    match_result = third_party_clause_matching.match_clauses_to_playbook(clauses, playbook)
    model_id = model_client_module.primary_model_id()
    fake_client = model_client_module.FakeBedrockClient(
        {
            model_id: [
                json.dumps(
                    {
                        "decision": "accept",
                        "rationale": "This clause matches your position and can be accepted.",
                    }
                )
            ]
        }
    )
    findings = evaluate_position_findings(clauses, match_result, playbook, fake_client, model_id=model_id)
    for finding in findings[:5]:
        print(
            f"{finding['playbook_topic_id']} / {finding['clause_id']} -> "
            f"{finding['decision']} ({finding['source']})"
        )


if __name__ == "__main__":
    main()
    sys.exit(0)
