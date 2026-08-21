#!/usr/bin/env python3
"""
Adversarial critic pass (issue #82): manifest-exact critic prompt assembly
(reusing #81's shared assembler), structured validated output, bounded
retry, terminal statuses, and per-attempt ledgering -- the critic-pass
mirror of #81's `scripts/primary_review_pass.py:run_primary_pass`.

Implements ARCHITECTURE.md -> "Data flow -- a single review" step 16 (the
critic half): invoke `critic_model_id` from the active model policy against
the playbook, the standard-form diff, the anchored clause text, and the
primary reviewer's output (never the raw counterparty document -- see
ARCHITECTURE.md -> "Per-pass prompt manifest"). Every attempt is ledgered in
a finally path, exactly like the primary pass. On schema failure, exactly
ONE bounded structured-output retry; if the retry also fails,
`status=ERROR_MANUAL_REVIEW_REQUIRED` -- ARCHITECTURE.md -> Two-pass review:
"Critic-pass failure is terminal -- never a silent single-pass DONE."

This module deliberately reuses #81's `primary_review_pass.py` for the
system-prompt assembly (guidance + binary overlay + playbook -- identical
for both passes per the #29/#30 manifest), the critic user-prompt
assembler (`assemble_user_prompt_critic`), the output-schema validation
(`validate_model_response`), and the token-count heuristic
(`estimate_tokens`) rather than duplicating them -- there is exactly one
prompt-manifest assembler and one schema validator for both passes, per
issue #29.

MOCKED-MODEL (owner-approved, issue #81/#82 body 2026-07-10): this module
is driven entirely by an injected `model_client.BedrockModelClient`
(ordinarily `FakeBedrockClient`). No live Bedrock, no network, fully
deterministic and offline.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import config as _config  # noqa: E402
import model_client as _model_client  # noqa: E402
import model_output_schema as _mos  # noqa: E402
import primary_review_pass as pp  # noqa: E402
import replacement_text_enforcement as _rte  # noqa: E402

# Cost-model constants mirrored from primary_review_pass.py / reviews.py
# (issue #81's convention: each module owns its own copy of these small
# shared sentinels; tests/test_critic_reconciliation_82.py cross-checks
# against pp's copy, which is itself cross-checked against reviews.py).
MAX_OUTPUT_TOKENS = pp.MAX_OUTPUT_TOKENS
MAX_RETRIES_PER_PASS = pp.MAX_RETRIES_PER_PASS


def run_critic_pass(
    *,
    review_id: str,
    diff_hunks: list[dict[str, Any]],
    anchored_clauses: list[dict[str, Any]],
    primary_output: dict[str, Any],
    playbook: dict[str, Any],
    model_client: "_model_client.BedrockModelClient",
    model_id: str,
    ledger_write: Callable[["_model_client.ModelInvocationRecord"], None],
    toaster_guidance: str = "",
    instructions_text: str = "",
    notes_mode: str = "external",
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    max_retries: int = MAX_RETRIES_PER_PASS,
    system_blocks_override: list[dict[str, Any]] | None = None,
    playbook_hash_override: str | None = None,
    cancel_checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run the adversarial critic pass end-to-end (data-flow step 16, critic
    half).

    Returns one of:
      {"status": "OK", "response": {...}, "attempts": N}
        -- schema-valid critic response obtained within the retry budget.
      {"status": "ERROR_MANUAL_REVIEW_REQUIRED", "attempts": N, "last_error": ...}
        -- still schema-invalid after the one bounded retry. Per
        ARCHITECTURE.md -> Two-pass review, this is terminal: the caller
        must NOT reconcile a partial/failed critic response into a silent
        single-pass DONE result.

    `model_id` is config-checked against the single-region-native-only
    policy before any invocation is attempted (raises
    `model_client.ModelPolicyViolation` on a forbidden inference-profile
    prefix), identically to the primary pass.

    `toaster_guidance` (issue #398, default `""`) and `instructions_text`
    (issue #483, epic #481, default `""`) are threaded through to the
    SHARED `pp.assemble_system_blocks` exactly as the primary pass does, so
    the critic's self-check reasons over the same per-review guidance, the
    same standing instructions, and the same judged-NL Floor obligations the
    primary pass saw -- the critic can therefore catch a Floor violation, or
    a guidance/instructions conflict, the primary pass missed.

    `notes_mode` (issue #520, epic #519 item A, default `"external"`): the
    audience this review's footnotes are written for. Accepted here and
    DELIBERATELY not branched on -- items B/C/D make the modes differ. Item A
    exists so the mode is a pipeline INPUT rather than a post-hoc filter: the
    prompt itself changes by mode, and generating internal reasoning into a
    counterparty-bound field and stripping it on the way out is exactly the
    posture the leakage scan exists to prevent. Threading it now means B can
    be a prompt change with no plumbing attached.

    `system_blocks_override` / `playbook_hash_override` (issue #479,
    default `None`): the OPF digest-mode seam, mirroring
    `primary_review_pass.run_primary_pass`'s own params of the same names
    -- see that function's docstring. `scripts/review_spine.py::run_review`
    passes the SAME composed blocks and hash to both passes, so primary and
    critic always read identical OPF knowledge.
    """
    _model_client.enforce_single_region_native_model_id(model_id)

    # Issue #562: same plumbed capability descriptor as run_primary_pass --
    # see that function's identical comment. Issue #567 (below) is now a
    # real consumer.
    model_capabilities = (
        model_client.capabilities(model_id)
        if hasattr(model_client, "capabilities")
        else None
    )

    # Issue #418: same seam as run_primary_pass -- see that function's
    # identical comment. Resolved once, threaded into invoke() below ONLY
    # when set.
    tool_spec = _mos.model_facing_output_schema() if _config.structured_output_enabled() else None

    # Issue #567: same seam as run_primary_pass -- see that function's
    # identical comment. Resolved once from the SAME model_capabilities
    # above, threaded into invoke() below ONLY when set.
    output_schema = (
        _mos.project_output_schema_for_provider()
        if (model_capabilities or {}).get("structured_outputs")
        else None
    )

    # Issue #479/#573: `pp.resolve_pen_rules_bundle` -- see
    # primary_review_pass.run_primary_pass's own identical usage. The SAME
    # bundle also drives `pp.assemble_system_blocks`'s
    # `render_replacement_text_modes_block`, so the critic's system prompt
    # states the same per-topic modes this enforcement judges it against.
    pen_rules_bundle = pp.resolve_pen_rules_bundle(playbook)

    system_blocks = (
        system_blocks_override
        if system_blocks_override is not None
        else pp.assemble_system_blocks(
            playbook, toaster_guidance, instructions_text, notes_mode=notes_mode
        )
    )
    system_prompt_text = pp.render_system_prompt(system_blocks)
    user_prompt = pp.assemble_user_prompt_critic(
        diff_hunks=diff_hunks,
        anchored_clauses=anchored_clauses,
        primary_output=primary_output,
    )
    # Issue #267: same projection as the primary pass -- assemble_system_blocks
    # is the single shared seam, so this hash is identical to the primary
    # pass's for the same playbook.
    projected_hash = (
        playbook_hash_override
        if playbook_hash_override is not None
        else pp.projected_playbook_hash(pp.project_playbook_for_prompt(playbook))
    )

    attempts_allowed = 1 + max_retries
    last_error: Any = None
    # Issues #417 / #527 follow-up: identical reasoning to the primary pass
    # (see run_primary_pass) -- the critic runs the same bounded-retry loop
    # against the same provider, so it needs the same informed retry. It is
    # if anything the MORE truncation-prone of the two: its prompt carries the
    # primary pass's full output on top of the document, and its own answer
    # restates the issues it contests.
    correction: Any = None
    attempt_max_output_tokens = max_output_tokens

    for attempt in range(1, attempts_allowed + 1):
        # Same contract as run_primary_pass: outside the try, so a raised
        # cancellation reaches the caller instead of consuming a retry. The
        # critic is the slower of the two passes in practice (a single Kimi K3
        # attempt was measured at 205s), which makes this the checkpoint most
        # likely to be the one that actually fires.
        if cancel_checkpoint is not None:
            cancel_checkpoint()
        outcome = "failure"
        raw_response = None
        replacement_text_failures: list[str] = []
        # Issue #414: same timing seam as run_primary_pass -- see that
        # function's identical comment.
        attempt_started_monotonic = time.monotonic()
        attempt_duration_ms: int | None = None
        try:
            # Issue #418: same "only when set" kwarg-threading as
            # run_primary_pass -- see that function's identical comment.
            invoke_kwargs: dict[str, Any] = dict(
                model_id=model_id,
                system_prompt=system_prompt_text,
                user_prompt=user_prompt + pp.render_retry_correction_block(correction),
                max_output_tokens=attempt_max_output_tokens,
            )
            if tool_spec is not None:
                invoke_kwargs["tool_spec"] = tool_spec
            # Issue #567: same "only when set" kwarg-threading as tool_spec
            # above, independently resolved.
            if output_schema is not None:
                invoke_kwargs["output_schema"] = output_schema
            raw_response = model_client.invoke(**invoke_kwargs)
            attempt_duration_ms = int((time.monotonic() - attempt_started_monotonic) * 1000)
            is_valid, parsed_or_error = pp.validate_model_response(
                raw_response, issue_provenance="critic-added"
            )
            if is_valid:
                # Issue #293 scope item 6: same post-validation
                # replacement-text enforcement as the primary pass, reusing
                # the SAME bounded-retry budget -- retry once, then demote
                # the violating issue(s) to flag-only on the final attempt.
                rt_failures = _rte.check_issues_replacement_text(
                    _rte.collect_checkable_issues(parsed_or_error), pen_rules_bundle
                )
                if rt_failures and attempt < attempts_allowed:
                    replacement_text_failures = [result.failure for _issue, result in rt_failures]
                    last_error = f"replacement_text_violation: {replacement_text_failures}"
                    correction = last_error
                    outcome = "retry"
                    continue
                if rt_failures:
                    replacement_text_failures = [result.failure for _issue, result in rt_failures]
                    for issue, _result in rt_failures:
                        _rte.demote_issue_to_flag_only(issue)
                outcome = "success"
                return {
                    "status": "OK",
                    "response": parsed_or_error,
                    "attempts": attempt,
                    # Issue #514, same seam as the primary pass: what the
                    # provider served on the attempt that produced THIS
                    # result. Absent, never a null placeholder, when the
                    # client cannot report it.
                    **(
                        {"served_model_id": served}
                        if (served := getattr(model_client, "last_served_model", None))
                        else {}
                    ),
                    # Issue #562: plumbed-only, same as run_primary_pass.
                    **(
                        {"model_capabilities": model_capabilities}
                        if model_capabilities is not None
                        else {}
                    ),
                    # Issue #567: same as run_primary_pass -- always a real
                    # bool, never absent.
                    "schema_enforcement_requested": output_schema is not None,
                }
            last_error = parsed_or_error
            correction = parsed_or_error
            outcome = "retry" if attempt < attempts_allowed else "failure"
        except _model_client.ModelOutputTruncatedError:
            # Same contract as run_primary_pass's handler: widen the budget
            # and retry, re-raise on the last attempt so the review still
            # fails as `model_output_truncated` rather than as a generic
            # schema failure. No `correction` -- the critic did not get its
            # answer wrong, it ran out of room, and inviting it to shorten
            # its objections is the one thing this retry must not buy.
            if attempt >= attempts_allowed:
                outcome = "failure"
                # Issue #573 fix round 1: set even on this raising branch --
                # see run_primary_pass's identical comment for why (the
                # `finally` below still runs on the way out, and without
                # this the ledgered error_token would misattribute an
                # EARLIER attempt's error to this one).
                last_error = "model_output_truncated: the response did not fit the output budget"
                raise
            outcome = "retry"
            last_error = "model_output_truncated: the response did not fit the output budget"
            attempt_max_output_tokens = pp.widen_output_budget(attempt_max_output_tokens)
            continue
        finally:
            # Issue #414: same "only trust last_usage after a genuine
            # invoke() return" reasoning as run_primary_pass -- see that
            # function's identical comment.
            actual_usage = (
                getattr(model_client, "last_usage", None) if raw_response is not None else None
            )
            # LEDGER every attempt -- success, retry, or terminal failure
            # alike -- via this finally path, identically to the primary
            # pass (ARCHITECTURE.md step 16 / issue #82 AC "Failure
            # semantics per #16 -- no silent single-pass results").
            ledger_write(
                _model_client.ModelInvocationRecord(
                    review_id=review_id,
                    pass_name="critic",
                    model_id=model_id,
                    attempt_number=attempt,
                    outcome=outcome,
                    input_tokens_est=pp.estimate_tokens(system_prompt_text)
                    + pp.estimate_tokens(user_prompt),
                    output_tokens_est=pp.estimate_tokens(raw_response or ""),
                    projected_playbook_hash=projected_hash,
                    replacement_text_failures=replacement_text_failures,
                    # Issue #514, same seam as the primary pass. The critic
                    # matters as much or more here: it is the pass most
                    # likely to be pointed at a cheap model, so "did the
                    # model I picked actually run it" is exactly the question
                    # a reader of this ledger will have.
                    served_model_id=getattr(model_client, "last_served_model", None) or "",
                    generation_id=getattr(model_client, "last_generation_id", None) or "",
                    # Issue #414: real usage/timing, same rationale as the
                    # primary pass -- None (not 0) when unmeasured.
                    actual_input_tokens=(actual_usage or {}).get("input_tokens"),
                    actual_output_tokens=(actual_usage or {}).get("output_tokens"),
                    duration_ms=(
                        attempt_duration_ms
                        if attempt_duration_ms is not None
                        else int((time.monotonic() - attempt_started_monotonic) * 1000)
                    ),
                    # Issue #568: same seam as the primary pass -- prompt-cache
                    # usage the provider reported for THIS attempt, if any.
                    # The critic pass never sends issue #568's cached-document
                    # content itself (out of this issue's scope), but the
                    # SYSTEM-side breakpoint (issue #30) is shared with the
                    # primary pass, so a cache hit there can still show up
                    # here.
                    cache_read_input_tokens=(actual_usage or {}).get("cache_read_input_tokens"),
                    cache_creation_input_tokens=(actual_usage or {}).get(
                        "cache_creation_input_tokens"
                    ),
                    # Issue #567: same seam as the primary pass.
                    schema_enforcement_requested=output_schema is not None,
                    # Issue #573 fix round 1 (Slice A): same "only THIS
                    # attempt's own error, never a stale earlier one" guard
                    # as run_primary_pass.
                    error_token=("" if outcome == "success" else pp._error_token(last_error)),
                )
            )

    # Retry budget exhausted, still schema-invalid: terminal, distinct from
    # a pipeline ERROR (ARCHITECTURE.md step 17) and, critically, never a
    # silent single-pass DONE (ARCHITECTURE.md -> Two-pass review).
    return {
        "status": "ERROR_MANUAL_REVIEW_REQUIRED",
        "attempts": attempts_allowed,
        "last_error": last_error,
    }
