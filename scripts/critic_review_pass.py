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
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client as _model_client  # noqa: E402
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
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    max_retries: int = MAX_RETRIES_PER_PASS,
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
    """
    _model_client.enforce_single_region_native_model_id(model_id)

    system_blocks = pp.assemble_system_blocks(playbook)
    system_prompt_text = pp.render_system_prompt(system_blocks)
    user_prompt = pp.assemble_user_prompt_critic(
        diff_hunks=diff_hunks,
        anchored_clauses=anchored_clauses,
        primary_output=primary_output,
    )
    # Issue #267: same projection as the primary pass -- assemble_system_blocks
    # is the single shared seam, so this hash is identical to the primary
    # pass's for the same playbook.
    projected_hash = pp.projected_playbook_hash(pp.project_playbook_for_prompt(playbook))

    attempts_allowed = 1 + max_retries
    last_error: Any = None

    for attempt in range(1, attempts_allowed + 1):
        outcome = "failure"
        raw_response = None
        replacement_text_failures: list[str] = []
        try:
            raw_response = model_client.invoke(
                model_id=model_id,
                system_prompt=system_prompt_text,
                user_prompt=user_prompt,
                max_output_tokens=max_output_tokens,
            )
            is_valid, parsed_or_error = pp.validate_model_response(raw_response)
            if is_valid:
                # Issue #293 scope item 6: same post-validation
                # replacement-text enforcement as the primary pass, reusing
                # the SAME bounded-retry budget -- retry once, then demote
                # the violating issue(s) to flag-only on the final attempt.
                rt_failures = _rte.check_issues_replacement_text(
                    _rte.collect_checkable_issues(parsed_or_error), playbook
                )
                if rt_failures and attempt < attempts_allowed:
                    replacement_text_failures = [result.failure for _issue, result in rt_failures]
                    last_error = f"replacement_text_violation: {replacement_text_failures}"
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
                }
            last_error = parsed_or_error
            outcome = "retry" if attempt < attempts_allowed else "failure"
        finally:
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
