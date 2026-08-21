#!/usr/bin/env python3
"""
Bounded re-quote repair pass (issue #569).

Today a REQUEST_CHANGE patch that fails to locate/apply is terminal: the
pipeline emitted its `source_quote`/`proposed_replacement_text` once, blind,
and any mismatch (`not_found` / `ambiguous` / `spans_paragraph_break` --
`scripts/quote_locate.py` / `scripts/redline_quote_apply.py`'s own
vocabulary) becomes a permanent flag-only entry the attorney must apply by
hand. This module gives the model its eyes back for exactly ONE bounded
correction round, after the redline stage has already run once
(`scripts/review_spine.py` wires this in as its own orchestration; see that
module's docstring "Re-quote repair" section) -- never a re-judgment, never
a second chance for anything else.

## What this corrects, and what it must never touch

This corrects an ADDRESS (`source_quote`, and `new_text` only insofar as it
must still read naturally against the corrected quote), never a JUDGMENT.
The model is given, per failed patch: the reason its previous quote could
not be applied, its own ALREADY-DECIDED rationale (verbatim, read-only --
never regenerated), and the actual text of the paragraph its corrected
quote should come from. It is told, structurally and in the prompt, not to
introduce a new issue, drop an issue, or edit a rationale -- enforced by
keying every correction to a LOCAL, request-scoped `issue_id`
(`build_repair_request` mints these; the pipeline's own `output-schema-v2
.json` Issue has no stable id of its own to reuse) and discarding anything
the response returns for an id this request never handed out
(`_validate_requote_response`).

## Hard bounds (issue #569 Scope)

  - ONE model call, ever -- no retry on a malformed/schema-invalid response;
    an unusable response degrades to "no corrections obtained", never a
    second attempt.
  - `rationale` (`external_rationale_for_footnote`) is never sent back for
    the model to regenerate and never mutated by this module -- only
    `source_quote` and `proposed_replacement_text` are ever written back
    onto an issue.
  - Only the three LOCATE reasons (`not_found` / `ambiguous` /
    `spans_paragraph_break`) are eligible for repair (`ELIGIBLE_REASONS`).
    `redline_quote_apply.REASON_ROUND_TRIP_FAILED` is deliberately excluded:
    that is a writer bug on text that already located and applied once --
    re-quoting the SOURCE cannot fix it, and asking the model to try would
    spend a call on a class of failure this pass cannot possibly help.

## Merging a correction: by object identity, not a second schema

`build_repair_request` correlates each local `issue_id` back to the flag-only
entry's own `_source_issue` -- the SAME issue dict `reconciled_result
["issues"]` holds (`redline_generate._issues_to_quote_patches` sets this key
BY REFERENCE, not by copy; see that module's docstring). Applying a valid
correction is therefore a two-field in-place mutation on the caller's own
object (`source_quote`, `proposed_replacement_text`), never a rebuild of the
issues list and never a second correlation scheme the caller would have to
keep in sync with the first.

This mutation is staged, not final: `run_requote_repair` writes it BEFORE
the caller's redline re-run can know whether the correction actually
worked, so a correction that still fails to locate would otherwise leave
the issue permanently overwritten with the repair model's UNRECONCILED,
still-wrong address. `revert_unrecovered` (below) is how the caller undoes
that staging for every issue still failing after the retry, restoring it
to be byte-identical to its pre-repair state (issue #569 AC2) --
`scripts/review_spine.py` calls it, then re-runs `generate_redline` once
more, immediately after every retry.

## Schema-enforced output (issue #567's seam, not its projection)

The correction response is asked for under the SAME two independent,
capability-gated seams the primary/critic passes use
(`scripts/primary_review_pass.py::run_primary_pass`): `tool_spec` (forced
tool-use, gated on `config.structured_output_enabled()`) and `output_schema`
(provider-native structured output, gated on the injected client's own
`capabilities(model_id)["structured_outputs"]`). This is a NEW, small,
purpose-built schema (`REQUOTE_OUTPUT_SCHEMA` below) -- not a projection of
`playbooks/output-schema-v2.json` (`scripts/model_output_schema.py`'s job,
which projects the REVIEW output shape; this pass emits a completely
different shape). It is written already "provider-safe" (every property
required, `additionalProperties: false` everywhere, no `minLength`/
`pattern`/`oneOf`) so no separate projection pass is needed for it.

## Leakage: no separate gate here, by design

This module never runs `leakage_scan` itself. The corrected
`source_quote`/`new_text` are merged onto the SAME issue objects
`reconciled_result["issues"]` already holds, and `scripts/review_spine.py`
re-runs `scripts/redline_generate.py::generate_redline` (which runs the
leakage gate FIRST, over the whole `reconciled_result`, before any quote
patching) on the corrected result -- so the corrected text is scanned by the
exact same gate every other model output goes through, not a duplicate.

## Pen rules: NOT covered by the leakage re-run above -- checked HERE

`generate_redline`'s re-run covers leakage (confidential-corpus text
appearing where it should not) but never runs `replacement_text_enforcement`
-- that check happens exactly once, inside `primary_review_pass.run_primary
_pass` / `critic_review_pass.run_critic_pass`, on the model's ORIGINAL
`proposed_replacement_text` (issue #293 scope item 6). A repair correction's
`new_text` is a NEW piece of model-authored text that has never been through
that check, so `run_requote_repair` runs it itself, per issue, against the
SAME resolved pen rules the primary/critic passes used
(`replacement_text_enforcement.resolve_pen_rules`, given this module's own
`pen_rules_bundle` param -- `None` for an OPF-shaped bundle, exactly
`primary_review_pass.py`'s own `pen_rules_bundle` resolution), BEFORE
merging it onto `_source_issue`. A correction that violates its topic's
`max_chars` / `must_not_introduce` is discarded entirely -- the issue is left
at its original, already-enforced `source_quote`/`proposed_replacement_text`
and counted as `still_failed`, never merged -- so this module can never be
the one path in the pipeline where model-authored replacement text reaches a
delivered redline unbounded by the pen rules every other path already
enforces.

MOCKED-MODEL, offline, deterministic (this repo's owner-approved scope,
extended to this module, mirroring `scripts/floor_judge.py`'s identical
posture): driven entirely by an injected `model_client.BedrockModelClient`
(ordinarily `FakeBedrockClient`). No live Bedrock, no network.
"""

from __future__ import annotations

import difflib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import config as _config  # noqa: E402
import model_client as _model_client  # noqa: E402
import primary_review_pass as _primary_review_pass  # noqa: E402
import quote_locate  # noqa: E402
import redline_quote_apply  # noqa: E402
import replacement_text_enforcement as _rte  # noqa: E402

# The only flag-only reasons a corrected QUOTE could possibly resolve -- see
# module docstring, "Hard bounds". `redline_quote_apply.REASON_ROUND_TRIP_
# FAILED` is deliberately absent.
ELIGIBLE_REASONS = frozenset(
    {
        redline_quote_apply.REASON_NOT_FOUND,
        redline_quote_apply.REASON_AMBIGUOUS,
        redline_quote_apply.REASON_SPANS_PARAGRAPH_BREAK,
    }
)

# Small, purpose-built, already provider-safe (issue #567's structured-
# output seam -- see module docstring). Deliberately NOT a projection of
# playbooks/output-schema-v2.json: this response describes corrected
# addresses for an existing batch of issues, a different shape entirely.
REQUOTE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "issue_id": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["issue_id", "source_quote", "new_text"],
            },
        },
    },
    "required": ["corrections"],
}

MAX_OUTPUT_TOKENS = 4096

_SYSTEM_PROMPT = """You are correcting the QUOTED ADDRESS of a set of contract edits that have already been decided. You are NOT re-reviewing the document, NOT deciding whether any edit should happen, and NOT writing new rationale.

For each entry in the user message you are given:
  - issue_id: a label for this entry. Your response MUST use these exact ids and no others -- do not invent a new issue_id and do not respond about an issue_id you were not given.
  - reason: why your previous quote could not be applied (not_found / ambiguous / spans_paragraph_break).
  - rationale: the ALREADY-DECIDED reason for this edit. This is context only -- copy it nowhere, and do not let it change your answer.
  - previously_attempted_quote / previously_attempted_new_text: what you tried before.
  - target_paragraph_text: the ACTUAL text of the paragraph most likely to contain what you were trying to quote.

For each entry, return a corrected source_quote that appears VERBATIM, EXACTLY ONCE, inside its target_paragraph_text (copy it character-for-character -- do not paraphrase, do not fix typos in the document's own text), and a new_text that still reads naturally as a replacement for that corrected quote (repeat previously_attempted_new_text unchanged if it still fits).

If you cannot find a correct, uniquely-locatable quote for an entry, OMIT that issue_id from your response entirely rather than guessing.

Respond with STRICT JSON ONLY -- no prose, no markdown fencing -- in exactly this shape:

{"corrections": [{"issue_id": "<id>", "source_quote": "<corrected verbatim quote>", "new_text": "<replacement text>"}, ...]}
"""


def _closest_paragraph_text(paragraphs: list[dict[str, Any]], quote: str) -> str:
    """Best-effort nearest-paragraph search for a quote `quote_locate`
    could not place at all (`not_found`) or placed in more than one spot
    (`ambiguous`, which reports no single location): the paragraph whose
    text has the highest `difflib.SequenceMatcher` similarity to `quote`,
    compared whitespace-collapsed and lowercased (a cheap, deterministic,
    dependency-free proxy for "which real paragraph was the model probably
    aiming at" -- exact word content/casing is irrelevant here, this is
    disclosure of what is actually near the target, not another locate
    attempt). Returns "" for an empty paragraph list or an empty quote."""
    if not quote or not paragraphs:
        return ""
    normalized_quote = " ".join(quote.split()).lower()
    best_text = ""
    best_ratio = -1.0
    for paragraph in paragraphs:
        text = paragraph.get("text", "") or ""
        normalized_text = " ".join(text.split()).lower()
        ratio = difflib.SequenceMatcher(None, normalized_quote, normalized_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_text = text
    return best_text


def _target_paragraph_text(paragraphs: list[dict[str, Any]], quote: str) -> str:
    """The paragraph text to show the model for one failed patch's
    correction.

    `quote_locate.locate_quote_in_paragraphs` is re-run against the SAME
    normalized paragraphs the model was originally shown (this repair pass
    never re-derives its own view of the document) to recover whatever it
    can: for `spans_paragraph_break`, it DOES report which logical
    paragraph the quote is genuinely in (the join is the only problem, not
    the location -- `quote_locate.py`'s own "located, but ..." case), so
    that paragraph's real text is used directly. For `not_found`/
    `ambiguous` (no single location to report), falls back to
    `_closest_paragraph_text`'s best-effort nearest match.
    """
    loc = quote_locate.locate_quote_in_paragraphs(paragraphs, quote)
    if loc["status"] in ("found", "spans_paragraph_break") and loc.get("para_index") is not None:
        return paragraphs[loc["para_index"]].get("text", "") or ""
    return _closest_paragraph_text(paragraphs, quote)


def build_repair_request(
    flag_only: list[dict[str, Any]], draft_paragraphs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build the per-entry prompt data and the local `issue_id` ->
    flag-only-entry correlation map for one repair request.

    `flag_only` is ALREADY filtered by the caller to `ELIGIBLE_REASONS` --
    this function does not re-filter. Each entry is `apply_quote_patches`'s
    own flag-only shape (`source_quote`, `new_text`, `rationale`, `reason`,
    `_source_issue`).

    Returns `(entries, entry_by_id)`: `entries` is the list handed to
    `_build_user_prompt`; `entry_by_id` maps each minted `issue_id` (a
    plain positional string, `"0"`, `"1"`, ... -- this repair round's OWN
    local identifier, never a model-facing "issue id" from
    `output-schema-v2.json`, which has none -- see module docstring) back
    to the ORIGINAL flag-only entry, so a valid correction can be merged
    onto `entry["_source_issue"]` by reference.
    """
    entries: list[dict[str, Any]] = []
    entry_by_id: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(flag_only):
        issue_id = str(index)
        entry_by_id[issue_id] = entry
        original_quote = entry.get("source_quote") or ""
        entries.append(
            {
                "issue_id": issue_id,
                "reason": entry.get("reason"),
                "rationale": entry.get("rationale") or "",
                "previously_attempted_quote": original_quote,
                "previously_attempted_new_text": entry.get("new_text") or "",
                "target_paragraph_text": _target_paragraph_text(draft_paragraphs, original_quote),
            }
        )
    return entries, entry_by_id


def _build_user_prompt(entries: list[dict[str, Any]]) -> str:
    blocks = []
    for entry in entries:
        blocks.append(
            f"issue_id: {entry['issue_id']}\n"
            f"reason: {entry['reason']}\n"
            f"rationale: {entry['rationale']}\n"
            f"previously_attempted_quote: {entry['previously_attempted_quote']}\n"
            f"previously_attempted_new_text: {entry['previously_attempted_new_text']}\n"
            "<TARGET_PARAGRAPH_TEXT>\n"
            f"{entry['target_paragraph_text']}\n"
            "</TARGET_PARAGRAPH_TEXT>\n"
        )
    return "\n".join(blocks)


def _validate_requote_response(
    raw_text: str, valid_ids: set
) -> tuple[bool, dict[str, dict[str, str]]]:
    """Parse + structurally validate one re-quote response.

    Mirrors `floor_judge._validate_judge_response`'s strictness (that
    module's own small, purpose-built judge response, not the full
    `output-schema-v2.json` path): strict `json.loads`, no markdown-fence/
    prose unwrapping -- the system prompt asks for STRICT JSON ONLY, and
    per this module's "one pass ever" bound a response that ignores that
    instruction is not worth a second attempt to salvage, only a clean
    "no corrections obtained" degrade.

    Returns `(True, corrections)` on a well-shaped JSON response (even one
    with zero usable corrections in it -- an empty `corrections: []` is a
    valid, if unhelpful, answer), `(False, {})` when the response is not
    even JSON, not an object, or has no `corrections` list at all.

    Each item is checked structurally: `issue_id` must be one this request
    actually handed out (`valid_ids`) -- anything else is DISCARDED, never
    an error, per this module's own "keying corrections to existing issue
    ids and discarding anything else" contract; `source_quote`/`new_text`
    must both be non-empty strings, else that single item is discarded (an
    empty string is not a usable correction, not a signal to leave the
    field blank). A duplicate `issue_id` in the response has its LAST
    occurrence win -- harmless either way since at most one write ever
    reaches the same `_source_issue`.
    """
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return False, {}
    if not isinstance(parsed, dict):
        return False, {}
    corrections = parsed.get("corrections")
    if not isinstance(corrections, list):
        return False, {}
    result: dict[str, dict[str, str]] = {}
    for item in corrections:
        if not isinstance(item, dict):
            continue
        issue_id = item.get("issue_id")
        if issue_id not in valid_ids:
            continue
        source_quote = item.get("source_quote")
        new_text = item.get("new_text")
        if not isinstance(source_quote, str) or not source_quote.strip():
            continue
        if not isinstance(new_text, str) or not new_text.strip():
            continue
        result[issue_id] = {"source_quote": source_quote, "new_text": new_text}
    return True, result


def run_requote_repair(
    *,
    review_id: str,
    flag_only: list[dict[str, Any]],
    draft_paragraphs: list[dict[str, Any]],
    model_client: Any,
    model_id: str,
    pen_rules_bundle: Optional[dict[str, Any]] = None,
    ledger_write: Optional[Callable[["_model_client.ModelInvocationRecord"], None]] = None,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    cancel_checkpoint: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Run the ONE bounded re-quote model call for a batch of eligible
    flag-only patches, and merge any usable, pen-rules-clean correction
    straight onto its `_source_issue` (by reference -- see module docstring
    "Merging a correction" and "Pen rules" sections).

    `pen_rules_bundle` is the SAME bundle `primary_review_pass.py` resolves
    (`None` for an OPF-shaped bundle, the raw `bundle`/`playbook` dict
    otherwise -- see that module's own `pen_rules_bundle` resolution) --
    callers pass it through unchanged so this pass's enforcement can never
    silently diverge from what the primary/critic passes already enforced.

    `flag_only` MUST already be filtered to `ELIGIBLE_REASONS` by the
    caller (`scripts/review_spine.py`) -- this function does not filter
    again, so its own `attempted` count is exactly `len(flag_only)`.

    Returns `{"attempted": N, "corrected_count": M}` -- `corrected_count`
    is how many issues this call actually rewrote; whether a rewritten
    issue's corrected quote goes on to LOCATE on the caller's re-run of
    `redline_generate.generate_redline` is something only that re-run can
    determine (`review_spine.run_review` computes the eventual
    `recovered`/`still_failed` split via `count_recovered`, below).

    A no-op (no model call at all) when `flag_only` is empty, returning
    `{"attempted": 0, "corrected_count": 0}` -- the caller's own "flag_only
    is non-empty" gate should make this the rare case, but this function
    stays safe to call unconditionally.

    Ledgers exactly ONE attempt (`pass_name="requote"`, `attempt_number=1`)
    via the SAME `ledger_write` seam the primary/critic/floor passes use --
    success or failure alike, in a `finally` path, mirroring
    `floor_judge.judge_floor_invariants`'s identical discipline. Never
    retried: a malformed/schema-invalid response, OR the model call itself
    raising any `_model_client.ModelInvocationError` (timeout, empty
    content, context-length, truncation, or a transport/retry-exhaustion
    failure -- every one of the real client's failure modes shares that
    base class), degrades to `corrected_count=0`, not a second attempt and
    never a propagated exception (module docstring, "Hard bounds"; this is
    an ANCILLARY step over an ALREADY-COMPUTED redline, so it must never
    re-terminate that result -- mirrors
    `backend/src/pipeline_runner.py::_settle_reservation_safely`'s
    identical doctrine).
    """
    ledger_write = ledger_write or (lambda record: None)
    attempted = len(flag_only)
    if attempted == 0:
        return {"attempted": 0, "corrected_count": 0}

    entries, entry_by_id = build_repair_request(flag_only, draft_paragraphs)
    user_prompt = _build_user_prompt(entries)

    if cancel_checkpoint is not None:
        cancel_checkpoint()

    # Issue #567's seam, reused verbatim (see module docstring) -- NOT its
    # projection helpers, which are specific to output-schema-v2.json's
    # shape. `REQUOTE_OUTPUT_SCHEMA` is already provider-safe.
    model_capabilities = (
        model_client.capabilities(model_id) if hasattr(model_client, "capabilities") else None
    )
    tool_spec = REQUOTE_OUTPUT_SCHEMA if _config.structured_output_enabled() else None
    output_schema = (
        REQUOTE_OUTPUT_SCHEMA if (model_capabilities or {}).get("structured_outputs") else None
    )

    invoke_kwargs: dict[str, Any] = dict(
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_output_tokens=max_output_tokens,
    )
    if tool_spec is not None:
        invoke_kwargs["tool_spec"] = tool_spec
    if output_schema is not None:
        invoke_kwargs["output_schema"] = output_schema

    outcome = "failure"
    raw_response: str | None = None
    corrections: dict[str, dict[str, str]] = {}
    attempt_started_monotonic = time.monotonic()
    attempt_duration_ms: int | None = None
    try:
        raw_response = model_client.invoke(**invoke_kwargs)
        attempt_duration_ms = int((time.monotonic() - attempt_started_monotonic) * 1000)
        is_valid, corrections = _validate_requote_response(raw_response, set(entry_by_id))
        outcome = "success" if is_valid else "failure"
    except _model_client.ModelInvocationError:
        # Catches every failure mode of a real client -- not just
        # `ModelOutputTruncatedError` (its subclass): `ModelTimeoutError`,
        # `ModelEmptyContentError`, `ModelContextLengthExceededError`, and
        # the generic retry-exhaustion `ModelInvocationError` itself all
        # inherit from this same base (backend/src/model_client.py). An
        # ancillary repair pass must never re-terminate an
        # already-computed redline -- the SAME doctrine
        # `backend/src/pipeline_runner.py::_settle_reservation_safely`
        # states explicitly: "a destroyed result is not [recoverable]".
        # No retry, no widened budget -- "one pass ever" (module docstring).
        # The batch simply yields no corrections; every entry stays
        # flag-only with its original reason, exactly as if this pass had
        # never run.
        outcome = "failure"
    finally:
        actual_usage = (
            getattr(model_client, "last_usage", None) if raw_response is not None else None
        )
        ledger_write(
            _model_client.ModelInvocationRecord(
                review_id=review_id,
                pass_name="requote",
                model_id=model_id,
                attempt_number=1,
                outcome=outcome,
                input_tokens_est=_primary_review_pass.estimate_tokens(_SYSTEM_PROMPT)
                + _primary_review_pass.estimate_tokens(user_prompt),
                output_tokens_est=_primary_review_pass.estimate_tokens(raw_response or ""),
                served_model_id=getattr(model_client, "last_served_model", None) or "",
                generation_id=getattr(model_client, "last_generation_id", None) or "",
                actual_input_tokens=(actual_usage or {}).get("input_tokens"),
                actual_output_tokens=(actual_usage or {}).get("output_tokens"),
                duration_ms=(
                    attempt_duration_ms
                    if attempt_duration_ms is not None
                    else int((time.monotonic() - attempt_started_monotonic) * 1000)
                ),
                cache_read_input_tokens=(actual_usage or {}).get("cache_read_input_tokens"),
                cache_creation_input_tokens=(actual_usage or {}).get("cache_creation_input_tokens"),
                schema_enforcement_requested=output_schema is not None,
            )
        )

    corrected_count = 0
    for issue_id, correction in corrections.items():
        source_issue = entry_by_id[issue_id].get("_source_issue")
        if not isinstance(source_issue, dict):
            continue  # pragma: no cover - defensive; redline_generate always sets this
        # Issue #569 review round 3, finding 1: a correction's `new_text` is
        # NEW model-authored text that has never been through
        # replacement_text_enforcement (that check runs once, inside the
        # primary/critic passes, on the ORIGINAL proposed_replacement_text --
        # see module docstring "Pen rules" section). Check it against the
        # SAME resolved pen rules the primary/critic passes used, keyed off
        # THIS issue's own playbook_topic_id, before merging -- a violation
        # is discarded entirely (never merged, never counted as corrected),
        # leaving source_issue at its original, already-enforced values.
        topic_id = source_issue.get("playbook_topic_id")
        rt_failures = _rte.check_issues_replacement_text(
            [{"playbook_topic_id": topic_id, "proposed_replacement_text": correction["new_text"]}],
            pen_rules_bundle,
        )
        if rt_failures:
            continue
        # ONLY the address fields -- never rationale, never section_ref/
        # counterparty_change_summary/playbook_topic_id. See module
        # docstring, "What this corrects".
        source_issue["source_quote"] = correction["source_quote"]
        source_issue["proposed_replacement_text"] = correction["new_text"]
        corrected_count += 1

    return {"attempted": attempted, "corrected_count": corrected_count}


def count_recovered(
    attempted_entries: list[dict[str, Any]], retry_flag_only: list[dict[str, Any]]
) -> int:
    """Of `attempted_entries` (the flag-only entries a repair round was
    given -- each still carrying its `_source_issue` reference), how many
    are NO LONGER present, by that SAME object identity, in
    `retry_flag_only` (the raw `flag_only` list a re-run of
    `redline_generate.generate_redline` produced) -- i.e. now applied.

    Identity (`id(...)`), not equality: two issues can legitimately carry
    identical field values, but `_source_issue` is always the literal same
    object across both `generate_redline` calls (review_spine.py never
    rebuilds `reconciled_result["issues"]` between them), so identity is
    the only correlation that cannot be fooled by coincidental content.

    The caller (`scripts/review_spine.py`) is responsible for treating a
    retry that never reached quote-patching at all (e.g. the corrected
    text tripped the leakage gate, which runs before any patch is
    attempted) as ZERO recovered rather than calling this with an empty
    `retry_flag_only` -- an empty list here unconditionally means
    "everything attempted is now recovered", which is only true when the
    retry genuinely ran quote-patching to completion.
    """
    still_failed_ids = {id(entry.get("_source_issue")) for entry in retry_flag_only}
    return sum(
        1 for entry in attempted_entries if id(entry.get("_source_issue")) not in still_failed_ids
    )


def revert_unrecovered(
    attempted_entries: list[dict[str, Any]], retry_flag_only: list[dict[str, Any]]
) -> int:
    """Undo `run_requote_repair`'s in-place correction on every entry whose
    corrected quote is STILL present, by object identity, in
    `retry_flag_only` -- i.e. a correction that did NOT actually recover
    its patch.

    Without this, a correction that still fails to locate leaves its
    `_source_issue["source_quote"]`/`["proposed_replacement_text"]`
    permanently overwritten with the repair model's UNRECONCILED,
    still-wrong address (`run_requote_repair` writes it on by reference
    BEFORE the caller's retry can know whether it actually worked) -- so a
    still-failed patch's `reason` on the caller's SUBSEQUENT
    `generate_redline` re-run gets RECOMPUTED off the corrected-but-wrong
    quote (e.g. `not_found` -> `ambiguous`) instead of staying the
    ORIGINAL reason, violating issue #569 AC2 ("a patch whose correction
    still fails remains flag-only with its original reason").

    `attempted_entries` is the SAME `eligible_flag_only` list
    `run_requote_repair` was given -- each entry still carries its
    ORIGINAL, pre-correction `source_quote`/`new_text` (a repair merge
    writes only onto `entry["_source_issue"]`, never back onto `entry`
    itself; see that function's own "Merging a correction" docstring
    section). `retry_flag_only` is a re-run of `redline_generate
    .generate_redline`'s own `flag_only` list, computed AFTER the repair's
    mutation -- like `count_recovered` above, this is never `None` here:
    the caller is responsible for treating a retry that never reached
    quote-patching at all (e.g. leakage-blocked -- a DELIBERATE, different
    outcome this function must never undo) separately, without calling
    this function.

    Identity (`id(...)`), not equality, exactly like `count_recovered` --
    for every entry whose `_source_issue` is still present in
    `retry_flag_only`, this reverts `_source_issue["source_quote"]` /
    `_source_issue["proposed_replacement_text"]` back to that entry's
    original `source_quote` / `new_text`, restoring it to be
    byte-identical to its pre-repair state. The caller (`scripts
    /review_spine.py`) then re-runs the (deterministic, no-model-spend)
    `generate_redline` once more so the DELIVERED `analysis_report`/
    `flag_only`/`findings` are derived from the reverted issue, not the
    corrected-but-unrecovered one -- re-running is itself deterministic on
    the now-reverted input, so it reproduces the exact pre-repair
    `reason`, never a fresh computation that could differ.

    A safe no-op for any entry that was never actually corrected in the
    first place (the model omitted its `issue_id`, `_validate_requote_
    response` discarded its response, or the model call itself raised) --
    `_source_issue` already holds the original values there, so reverting
    it to itself changes nothing.

    Returns how many entries were reverted -- the caller re-runs
    `generate_redline` once more iff this is > 0, since 0 means there is
    nothing this function needed to undo.
    """
    still_failed_ids = {id(entry.get("_source_issue")) for entry in retry_flag_only}
    reverted = 0
    for entry in attempted_entries:
        source_issue = entry.get("_source_issue")
        if not isinstance(source_issue, dict) or id(source_issue) not in still_failed_ids:
            continue
        source_issue["source_quote"] = entry.get("source_quote") or ""
        source_issue["proposed_replacement_text"] = entry.get("new_text") or ""
        reverted += 1
    return reverted
