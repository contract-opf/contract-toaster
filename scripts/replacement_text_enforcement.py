#!/usr/bin/env python3
"""
Post-validation enforcement of `topics[].replacement_text` constraints —
issue #216.

## Problem this solves

`playbooks/output-schema-v1.json` (`Issue.proposed_replacement_text`
description, ~lines 120-123) promises: "Bounded by the topic's
replacement_text constraints (mode, max_chars, must_not_introduce) ... The
pipeline enforces topic-level max_chars post-validation." Before this
module, that promise was unfulfilled: `grep -r must_not_introduce|max_chars
--include=*.py` found zero enforcement anywhere in the repo. A model (or an
adversarial prompt-injection attempt riding in on counterparty document
text) could emit a `proposed_replacement_text` of unbounded length, or one
that reintroduces a concept the topic explicitly forbids (e.g.
'indemnify', 'uncapped'), and nothing would catch it before the text
reached a generated `.docx` redline.

This module is a pure function, deliberately un-wired into any specific
transport (Bedrock response handler, API route, etc.) so it can be called
from wherever in the pipeline `proposed_replacement_text` first becomes
available, and unit-tested without any of that machinery.

## must_not_introduce is PER-TOPIC, not global

`scripts/detector_common.find_spans` (issue #212/#213/#220's shared
detector-matching module) is reused here for the actual phrase search, so
`must_not_introduce` phrases are matched the same way (case-insensitive,
word-boundary) as `hard_rejections[].trigger_terms` are matched elsewhere
in the pipeline — one matching semantics, not a second divergent one.

Critically, `must_not_introduce` is read from THIS topic's
`replacement_text.must_not_introduce` list, not from a blanket list shared
across topics. Before issue #216, every topic in
`playbooks/eiaa-v1.0.0.json` carried an identical copy-pasted
`must_not_introduce` list that included `"consequential damages"` —
including `limitation-of-liability`, whose own `must_preserve` is "Mutual
consequential damages waiver." A correct §8 replacement clause stating
that waiver would have been rejected by its own topic's constraint. Issue
#216 removed `"consequential damages"` from `limitation-of-liability`'s
`must_not_introduce` list (see `playbooks/eiaa-v1.0.0.json`); this module
is what makes that per-topic list load-bearing instead of decorative.

## Named failure routes

`check_replacement_text` never raises for a routine bounds/content
violation — it returns a `ReplacementTextCheckResult` with a `failure`
code from `FAILURE_CODES` so a caller can route each failure kind
differently (e.g. surface a different manual-review reason). A config
problem (topic missing `replacement_text` entirely) raises
`ReplacementTextConfigError`, mirroring `detector_common.DetectorConfigError`
-- a playbook authoring bug should be a loud build failure, not a silent
pass.

## Pen rules are toaster-owned bind config (issue #293)

`resolve_pen_rules` (below) resolves the EFFECTIVE `mode`/`max_chars`/
`must_not_introduce` for one topic from up to three layers, most specific
first: a v2 bundle's `pen_rules.per_topic[topic_id]`, that same bundle's
`pen_rules.default`, and the toaster-global
`playbooks/pen-rules.defaults.json` artifact. Scalars (`mode`, `max_chars`)
are taken wholesale from the most specific layer that defines them.
`must_not_introduce` entries carrying a `floor_ref` (naming an
`opf.floor.invariants` id) are STICKY: the union of every floor_ref entry
across all three layers always survives, on top of the most specific
layer's own plain (non-floor_ref) entries -- a more-specific layer can add
rules but can never silently drop a Floor-derived one (stricter-wins).

A v1 playbook (per-topic `replacement_text`, no bundle `pen_rules`) is
passed through UNCHANGED for full backward compatibility -- this is the
`bundle` argument's `topics`-shaped branch below. `check_replacement_text`
itself (unchanged) already reads any resolved `replacement_text`-shaped
dict, v1 (plain-string `must_not_introduce` entries) or v2 (dict entries
with `phrase`/`floor_ref`) alike.

Imported by:
  - tests/test_replacement_text_enforcement.py
  - tests/test_pen_rules_resolution.py
  - scripts/primary_review_pass.py / scripts/critic_review_pass.py (pipeline
    wiring: check_issues_replacement_text)
  - scripts/bind_bundle.py (--pen-rules floor_ref validation reuses
    PEN_RULES_DEFAULTS_PATH's shape, not resolve_pen_rules itself)
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import detector_common  # noqa: E402

PEN_RULES_DEFAULTS_PATH = REPO_ROOT / "playbooks" / "pen-rules.defaults.json"

MAX_CHARS_EXCEEDED = "max_chars_exceeded"
MUST_NOT_INTRODUCE_VIOLATION = "must_not_introduce_violation"
REPLACEMENT_NOT_PERMITTED = "replacement_not_permitted"

FAILURE_CODES = frozenset(
    {MAX_CHARS_EXCEEDED, MUST_NOT_INTRODUCE_VIOLATION, REPLACEMENT_NOT_PERMITTED}
)


class ReplacementTextConfigError(ValueError):
    """Raised when a topic's `replacement_text` block is missing or
    malformed -- a playbook authoring bug, not a model-output problem.
    """


@dataclass
class ReplacementTextCheckResult:
    """Outcome of checking one `proposed_replacement_text` against one
    topic's `replacement_text` constraints.

    `passed` is False for any violation. `failure` is a named code from
    FAILURE_CODES (None when passed). `detail` is a human-readable
    explanation suitable for an audit log or manual-review reason.
    `matched_terms` carries the specific must_not_introduce phrases that
    fired (empty unless failure == MUST_NOT_INTRODUCE_VIOLATION).
    """

    passed: bool
    failure: str | None = None
    detail: str = ""
    matched_terms: list[str] = field(default_factory=list)


def check_replacement_text(
    topic: dict, proposed_replacement_text: str
) -> ReplacementTextCheckResult:
    """Pure post-validation function: does `proposed_replacement_text`
    satisfy `topic["replacement_text"]`'s constraints?

    `topic` is one entry from a playbook's `topics[]` array (must carry a
    `replacement_text` object per playbooks/schema.json). Per
    output-schema-v1.json's `proposed_replacement_text` description, an
    empty string always signals mode='none' (flag only, no replacement
    proposed) and trivially passes -- there is nothing to bound or scan.

    Checks, in order (first violation wins, matching the "first reason to
    block" convention in scripts/leakage_scan.py's LeakageScanner.scan):

      1. mode == 'none': any non-empty proposed_replacement_text is a
         violation (REPLACEMENT_NOT_PERMITTED) -- this topic permits flag-
         only issues, never a redline.
      2. max_chars: len(proposed_replacement_text) > replacement_text
         ['max_chars'] is a violation (MAX_CHARS_EXCEEDED) -- the schema's
         "the pipeline enforces topic-level max_chars post-validation"
         promise, previously unimplemented (issue #216).
      3. must_not_introduce: any THIS TOPIC's must_not_introduce phrase
         with a match span in proposed_replacement_text is a violation
         (MUST_NOT_INTRODUCE_VIOLATION). Matching reuses
         detector_common.find_spans (word_boundary, case-insensitive) --
         the same span-level matching semantics used for hard_rejections
         trigger_terms, not a second divergent implementation.
    """
    replacement_text_spec = topic.get("replacement_text")
    if not isinstance(replacement_text_spec, dict):
        raise ReplacementTextConfigError(
            f"topic {topic.get('id')!r} has no replacement_text block -- "
            "every topic must carry mode/max_chars/must_not_introduce per "
            "playbooks/schema.json."
        )

    text = proposed_replacement_text or ""
    if not text:
        return ReplacementTextCheckResult(passed=True)

    mode = replacement_text_spec.get("mode", "none")
    if mode == "none":
        return ReplacementTextCheckResult(
            passed=False,
            failure=REPLACEMENT_NOT_PERMITTED,
            detail=(
                f"topic {topic.get('id')!r} has replacement_text.mode='none' "
                "(flag only) but a non-empty proposed_replacement_text was "
                "supplied."
            ),
        )

    max_chars = replacement_text_spec.get("max_chars")
    if max_chars is not None and len(text) > max_chars:
        return ReplacementTextCheckResult(
            passed=False,
            failure=MAX_CHARS_EXCEEDED,
            detail=(
                f"topic {topic.get('id')!r}: proposed_replacement_text is "
                f"{len(text)} chars, exceeding max_chars={max_chars}."
            ),
        )

    # v1 entries are plain phrase strings; v2-resolved entries (issue #293's
    # resolve_pen_rules) are {"phrase": ..., "floor_ref": ...} dicts -- accept
    # both so this function works unchanged whether `topic` came straight
    # from a v1 playbook or from resolve_pen_rules's v2 resolution.
    must_not_introduce = replacement_text_spec.get("must_not_introduce", [])
    matched_terms = [
        phrase
        for entry in must_not_introduce
        for phrase in [entry if isinstance(entry, str) else entry.get("phrase")]
        if phrase and detector_common.phrase_matches(text, phrase, "word_boundary")
    ]
    if matched_terms:
        return ReplacementTextCheckResult(
            passed=False,
            failure=MUST_NOT_INTRODUCE_VIOLATION,
            detail=(
                f"topic {topic.get('id')!r}: proposed_replacement_text "
                f"introduces forbidden phrase(s): {', '.join(matched_terms)}."
            ),
            matched_terms=matched_terms,
        )

    return ReplacementTextCheckResult(passed=True)


def find_topic(playbook: dict, topic_id: str) -> dict | None:
    """Look up a topic by id in a loaded playbook dict. Returns None if not
    found (callers decide whether that's a config error)."""
    for topic in playbook.get("topics", []):
        if topic.get("id") == topic_id:
            return topic
    return None


# ---------------------------------------------------------------------------
# Pen-rules resolution (issue #293).
# ---------------------------------------------------------------------------

_PEN_RULES_DEFAULTS_CACHE: dict[str, dict] = {}


def _load_pen_rules_defaults(defaults_path: Path) -> dict:
    key = str(defaults_path)
    if key not in _PEN_RULES_DEFAULTS_CACHE:
        with open(defaults_path, encoding="utf-8") as f:
            _PEN_RULES_DEFAULTS_CACHE[key] = json.load(f)
    return _PEN_RULES_DEFAULTS_CACHE[key]


def _normalize_must_not_introduce(entries: Any) -> list[dict[str, Any]]:
    """Normalize a must_not_introduce list to `{"phrase", "floor_ref"}` dicts
    -- accepts v1 plain-string entries too (only used internally, for the
    layered union computation below; the v1-passthrough branch of
    resolve_pen_rules never goes through this and returns v1 entries
    unchanged)."""
    normalized = []
    for entry in entries or []:
        if isinstance(entry, str):
            normalized.append({"phrase": entry, "floor_ref": None})
        elif isinstance(entry, dict):
            normalized.append({"phrase": entry.get("phrase"), "floor_ref": entry.get("floor_ref")})
    return normalized


def resolve_pen_rules(
    bundle: dict | None,
    topic_id: str,
    defaults_path: Path = PEN_RULES_DEFAULTS_PATH,
) -> dict[str, Any]:
    """Resolve the EFFECTIVE `mode`/`max_chars`/`must_not_introduce` pen
    rules for one topic (issue #293).

    `bundle` is whatever pen-rules configuration source is currently active:

      - `None`: no bundle-level config at all -- return the toaster-global
        defaults artifact's `"default"` block (deep copy).
      - a v1 PLAYBOOK dict (has a top-level `"topics"` list and no
        `"default"`/`"per_topic"` keys of its own): V1 COMPATIBILITY --
        return that topic's own `replacement_text` block UNCHANGED (deep
        copy). Global defaults and floor-ref stickiness never apply here --
        byte-identical to pre-#293 behavior for eiaa. Raises
        `ReplacementTextConfigError` if the topic or its `replacement_text`
        block is missing (same failure mode `check_replacement_text` uses
        for a playbook-authoring bug).
      - a v2 `pen_rules` BLOCK (has `"default"` and/or `"per_topic"`):
        resolve `per_topic[topic_id]` > `default` > the global defaults
        artifact. Scalars (`mode`, `max_chars`) are taken wholesale from the
        most specific layer that defines the key. `must_not_introduce`
        entries carrying a `floor_ref` are STICKY: the union of every
        floor_ref entry across all three layers is always present in the
        result, on top of the most specific non-empty layer's own plain
        (non-floor_ref) entries -- a more specific layer can add rules but
        can never silently drop a Floor-derived one.

    Returns a `replacement_text`-shaped dict: `{"mode", "max_chars",
    "must_not_introduce"}`, directly consumable by `check_replacement_text`
    (wrap it as `{"id": topic_id, "replacement_text": resolved}`).
    """
    global_defaults = _load_pen_rules_defaults(defaults_path).get("default", {})

    if bundle is None:
        return copy.deepcopy(global_defaults)

    if "default" not in bundle and "per_topic" not in bundle:
        # V1 playbook passthrough.
        topic = find_topic(bundle, topic_id)
        if topic is None:
            raise ReplacementTextConfigError(
                f"v1 playbook has no topic {topic_id!r} to resolve pen rules for."
            )
        replacement_text_spec = topic.get("replacement_text")
        if not isinstance(replacement_text_spec, dict):
            raise ReplacementTextConfigError(
                f"topic {topic_id!r} has no replacement_text block -- "
                "every topic must carry mode/max_chars/must_not_introduce per "
                "playbooks/schema.json."
            )
        return copy.deepcopy(replacement_text_spec)

    # V2 pen_rules block: per_topic[topic_id] > bundle default > global
    # defaults artifact, most-specific-first.
    bundle_default = bundle.get("default") or {}
    per_topic = bundle.get("per_topic") or {}
    topic_override = per_topic.get(topic_id) or {}

    layers_most_to_least_specific = [topic_override, bundle_default, global_defaults]

    most_specific_nonempty: dict[str, Any] = {}
    for layer in layers_most_to_least_specific:
        if layer:
            most_specific_nonempty = layer
            break

    def _scalar(key: str, fallback: Any = None) -> Any:
        for layer in layers_most_to_least_specific:
            if key in layer:
                return layer[key]
        return fallback

    sticky_floor_entries: dict[str, dict[str, Any]] = {}
    for layer in layers_most_to_least_specific:
        for entry in _normalize_must_not_introduce(layer.get("must_not_introduce")):
            floor_ref = entry.get("floor_ref")
            if floor_ref:
                # Keyed by (phrase, floor_ref) so distinct floor invariants
                # forbidding the same phrase both survive; a more specific
                # layer re-declaring the SAME floor_ref/phrase pair does not
                # duplicate it.
                sticky_floor_entries[(entry.get("phrase"), floor_ref)] = entry

    plain_entries = [
        entry
        for entry in _normalize_must_not_introduce(most_specific_nonempty.get("must_not_introduce"))
        if not entry.get("floor_ref")
    ]

    return {
        "mode": _scalar("mode", "none"),
        "max_chars": _scalar("max_chars"),
        "must_not_introduce": list(sticky_floor_entries.values()) + plain_entries,
    }


def collect_checkable_issues(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Every Issue-shaped object in a validated primary/critic response that
    `check_issues_replacement_text` should enforce against (issue #293
    scope item 6): the top-level `issues` list (primary pass; empty on a
    critic response) PLUS `critic_delta.added_issues` (the critic pass's
    own new issues -- `output-schema-v1.json`'s `CriticDelta.added_issues`
    is itself a list of full `Issue` objects, `playbook_topic_id` and
    `proposed_replacement_text` included).

    Returns references to the SAME dict objects nested inside `response`
    (not copies) -- `demote_issue_to_flag_only` mutating one of these
    mutates `response` in place, wherever in the structure it lives.

    `critic_delta.contested_replacements` is deliberately excluded: those
    entries carry no `playbook_topic_id` (only a `section_ref`) and
    `output-schema-v1.json` is explicit that "the critic may not silently
    overwrite the primary's replacement text" -- a contested replacement is
    a reconciliation-time concern (scripts/reconciliation.py), not a
    per-issue pen-rules enforcement target.
    """
    issues = list(response.get("issues") or [])
    critic_delta = response.get("critic_delta") or {}
    issues.extend(critic_delta.get("added_issues") or [])
    return issues


def check_issues_replacement_text(
    issues: list[dict[str, Any]],
    bundle: dict | None,
    defaults_path: Path = PEN_RULES_DEFAULTS_PATH,
) -> list[tuple[dict[str, Any], ReplacementTextCheckResult]]:
    """Pipeline wiring (issue #293 scope item 6): run `check_replacement_text`
    for every issue's `proposed_replacement_text` against its RESOLVED pen
    rules (`resolve_pen_rules`).

    Returns one `(issue, ReplacementTextCheckResult)` pair per issue that
    FAILS the check -- empty list means every issue passed (or had nothing
    to check). An issue with no `playbook_topic_id` or an empty
    `proposed_replacement_text` (flag-only, including every Floor-derived
    fire from `scripts/floor_judge.floor_fires`) is skipped -- there is
    nothing to bound or scan. A topic that cannot be resolved (config
    problem, not a model-output problem) is also skipped here rather than
    raised -- callers running this post-validation, mid-pipeline have no
    good recovery for a playbook-authoring bug beyond what
    `check_replacement_text`'s own `ReplacementTextConfigError` already
    surfaces to direct callers of that function.
    """
    failures: list[tuple[dict[str, Any], ReplacementTextCheckResult]] = []
    for issue in issues:
        topic_id = issue.get("playbook_topic_id")
        text = issue.get("proposed_replacement_text") or ""
        if not topic_id or not text:
            continue
        try:
            resolved = resolve_pen_rules(bundle, topic_id, defaults_path=defaults_path)
        except ReplacementTextConfigError:
            continue
        result = check_replacement_text({"id": topic_id, "replacement_text": resolved}, text)
        if not result.passed:
            failures.append((issue, result))
    return failures


def demote_issue_to_flag_only(issue: dict[str, Any]) -> None:
    """Mutate `issue` in place to a flag-only issue (issue #293 scope item
    6): `proposed_replacement_text` -> `""`, per output-schema-v1.json's
    convention that an empty string signals mode='none' (flag only, no
    replacement proposed) -- and per issue #260, renders with NO patch."""
    issue["proposed_replacement_text"] = ""
