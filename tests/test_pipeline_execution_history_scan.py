#!/usr/bin/env python3
"""
Real GetExecutionHistory substance scan for the review pipeline — issue #166
(deferred #59 acceptance criterion).

## Problem this closes

The #59 AC in infra/lib/nested/app-stack.ts:33-39 ("EXECUTION-HISTORY
SUBSTANCE SCAN") called for "a pipeline integration test that executes the
state machine and asserts via GetExecutionHistory that no state input/output
contains document text, prompt text, or model output." What shipped instead
(tests/test_pipeline_stack.py, check_b_stage_skeleton_mock_task) is a static
regex over infra/lambda/mock_review/handler.py's SOURCE TEXT — it never runs
anything, and would keep passing even if a future handler built a leaking
payload field under a differently-named variable.

This module is the real runtime check: it actually EXECUTES the pipeline's
stage chain (using the real infra/lambda/mock_review/handler.py, imported and
invoked, plus the same pass-through behavior as the other stages' inline
stub code in infra/lib/nested/pipeline-stack.ts — `def handler(event,
context): return event`), builds a GetExecutionHistory-shaped list of
TaskStateEntered/TaskStateExited events carrying the REAL JSON that flowed
into and out of every stage, and scans that JSON for document text, prompt
text, retrieved-precedent text, or model output. Only pointers, hashes,
enums, and short fixed strings may appear.

## Why a hand-rolled execution engine, not moto/LocalStack

Same third-party-stubbing convention as tests/test_orphan_reconciler_e2e.py
and tests/test_review_submission_e2e.py: no live AWS, no moto/LocalStack
dependency, so the suite runs in CI without extra installs. Step Functions
itself is not the thing under test here — GetExecutionHistory is a plain
list of {input, output} JSON blobs keyed by state name, and this module
builds that same shape by actually running the stage handlers in the same
order pipeline-stack.ts wires them (verified against tests/
test_pipeline_stack.py Check B's stage-name assertions), rather than
re-deriving "no substance leaked" from source text.

## What this test would catch that the static regex could not

If a future handler (per issue: #80-#83 extract/retrieve/redline) builds a
payload field that carries full document text, prompt text, or model output
under ANY field name — not just the specific `doc_text` / `document_content`
/ `document_body` / `full_text` names the static regex greps for — this
scan catches it, because it inspects the actual runtime JSON values, not
the handler's source code. TestSubstanceScanCatchesRegression proves this by
running the SAME engine against a deliberately regressed handler that leaks
document text under a novel field name the static regex does not know about.

## Guard (wired in CI)

.github/workflows/bedrock-kb-gate.yml already runs tests/test_pipeline_stack.py
on every PR touching infra/lambda/** or pipeline-stack.ts (via its `paths:`
filter) but that filter did not include infra/lambda/**. This issue extends
that filter to infra/lambda/** and adds this module to the same job, so
both the static structural gate AND the real runtime scan run together on
every PR touching the Lambda handlers or the state machine definition.

Exit codes: 0 = all tests pass, 1 = one or more tests failed (unittest
convention, matching every other tests/test_*.py in this repo).
"""

from __future__ import annotations

import copy
import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_REVIEW_DIR = REPO_ROOT / "infra" / "lambda" / "mock_review"

if str(MOCK_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(MOCK_REVIEW_DIR))

import handler as mock_review_handler  # noqa: E402  (real production handler)

# ---------------------------------------------------------------------------
# Fake GetExecutionHistory engine.
#
# Models the exact stage chain pipeline-stack.ts wires (acquireSlot ->
# extract -> retrieve -> mockReview -> redline -> persist -> audit ->
# releaseSlot; see tests/test_pipeline_stack.py Check B/C for the assertions
# that this order/name set matches the real CDK definition) and records
# TaskStateEntered / TaskStateExited history events carrying the REAL input
# and output of every stage, in the same shape AWS's
# stepfunctions.get_execution_history returns them (each event has an
# `id`, a `type`, and a `stateEnteredEventDetails` / `stateExitedEventDetails`
# block with `name` + JSON-encoded `input` / `output`).
# ---------------------------------------------------------------------------

# extract/retrieve/redline/persist/audit are pass-through stubs in Phase 0
# (infra/lib/nested/pipeline-stack.ts `stubHandlerCode`:
#   "def handler(event, context): return event"
# ). Reproduced verbatim here so the fake engine exercises the SAME behavior
# the real inline Lambda code runs, not an approximation of it.
def _pass_through_stub(event: dict[str, Any]) -> dict[str, Any]:
    return event


# Stage order + handler callables, mirroring pipeline-stack.ts's
# `definition = acquireSlot.next(extractStage).next(retrieveStage)
#   .next(mockReviewStage).next(redlineStage).next(persistStage)
#   .next(auditStage).next(releaseSlot).next(succeed)`.
StageHandler = Callable[[dict[str, Any]], dict[str, Any]]


def _acquire_slot_stub(event: dict[str, Any]) -> dict[str, Any]:
    out = dict(event)
    out["semaphore_acquired"] = True
    return out


def _release_slot_stub(event: dict[str, Any]) -> dict[str, Any]:
    return dict(event)


def _mock_review_stage(event: dict[str, Any]) -> dict[str, Any]:
    """Invokes the REAL production mock_review handler (not a re-implementation)."""
    # Mirror the Lambda invocation contract: handler(event, context).
    # Force the delay to 0 so the integration test stays fast (same
    # MOCK_REVIEW_DELAY_SECONDS env-driven knob the real Lambda exposes).
    original_delay = mock_review_handler.MOCK_REVIEW_DELAY_SECONDS
    mock_review_handler.MOCK_REVIEW_DELAY_SECONDS = 0
    try:
        return mock_review_handler.handler(event, None)
    finally:
        mock_review_handler.MOCK_REVIEW_DELAY_SECONDS = original_delay


DEFAULT_STAGE_CHAIN: list[tuple[str, StageHandler]] = [
    ("AcquireConcurrencySlot", _acquire_slot_stub),
    ("ExtractStage", _pass_through_stub),
    ("RetrieveStage", _pass_through_stub),
    ("MockReviewStage", _mock_review_stage),
    ("RedlineStage", _pass_through_stub),
    ("PersistStage", _pass_through_stub),
    ("AuditStage", _pass_through_stub),
    ("ReleaseConcurrencySlot", _release_slot_stub),
]


def run_fake_execution(
    initial_input: dict[str, Any],
    stage_chain: list[tuple[str, StageHandler]] | None = None,
) -> list[dict[str, Any]]:
    """Executes the stage chain in order, threading each stage's output into
    the next stage's input exactly like Step Functions' `outputPath:
    '$.Payload'` wiring does in pipeline-stack.ts, and returns a
    GetExecutionHistory-shaped list of history events.

    Each stage contributes a TaskStateEntered event (recording its `input`)
    and a TaskStateExited event (recording its `output`) — the two event
    types GetExecutionHistory uses to carry state input/output JSON, per
    the AWS Step Functions API.
    """
    chain = stage_chain if stage_chain is not None else DEFAULT_STAGE_CHAIN
    history: list[dict[str, Any]] = []
    current = copy.deepcopy(initial_input)
    event_id = 0

    for stage_name, stage_fn in chain:
        stage_input = copy.deepcopy(current)
        event_id += 1
        history.append(
            {
                "id": event_id,
                "type": "TaskStateEntered",
                "stateEnteredEventDetails": {
                    "name": stage_name,
                    "input": json.dumps(stage_input),
                },
            }
        )

        stage_output = stage_fn(copy.deepcopy(stage_input))

        event_id += 1
        history.append(
            {
                "id": event_id,
                "type": "TaskStateExited",
                "stateExitedEventDetails": {
                    "name": stage_name,
                    "output": json.dumps(stage_output),
                },
            }
        )

        current = stage_output

    return history


# ---------------------------------------------------------------------------
# Substance scan.
#
# Per issue #19's "POINTER-ONLY PAYLOAD RULE" (infra/lib/nested/app-stack.ts):
# state payloads may carry ONLY S3 pointers, content hashes, and
# non-substantive metadata (review_id, playbook_id, decision enums, short
# fixed strings). Document text, prompt text, retrieved-precedent text, and
# model output MUST NOT appear inline. This scanner inspects the ACTUAL
# JSON string of every recorded state input/output for signs of substance,
# rather than trusting field names in the handler's source code.
# ---------------------------------------------------------------------------

# A value this long, that is not itself shaped like an S3 key/URI/ARN/UUID/
# hash, is presumptively "substance" rather than a pointer or short fixed
# string. Chosen well above the longest legitimate pointer-ish value in this
# payload shape (S3 keys, watermark strings, canned summaries) so it does
# not false-positive on those, while easily catching even a single sentence
# of document or prompt text.
_MAX_PLAUSIBLE_POINTER_LENGTH = 120

# Recognizable "this is a pointer/enum/id, not substance" shapes.
_POINTER_LIKE_PREFIXES = ("s3://", "uploads/", "outputs/", "mock-fixtures/", "arn:")

_KNOWN_SHORT_FIXED_STRINGS = {
    "REQUEST_CHANGE",
    "MANUAL_REVIEW_REQUIRED",
    "ACCEPT",
    "playbook_coming_soon",
    "unknown_playbook",
    "tool recommendation only - attorney approval required",
}


def _looks_like_pointer_or_enum(value: str) -> bool:
    if value in _KNOWN_SHORT_FIXED_STRINGS:
        return True
    if value.startswith(_POINTER_LIKE_PREFIXES):
        return True
    if len(value) <= _MAX_PLAUSIBLE_POINTER_LENGTH:
        # Short strings are allowed to be free-form enums/ids/short summaries
        # per the AC's "or short fixed strings" carve-out.
        return True
    return False


class SubstanceLeakError(AssertionError):
    """Raised when a state's recorded input/output JSON contains a value
    that is not a pointer, hash, enum, or short fixed string -- i.e. it
    looks like document text, prompt text, or model output leaking into
    Step Functions execution history."""


def _walk_string_values(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_string_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_string_values(v)


def scan_execution_history_for_substance(history: list[dict[str, Any]]) -> None:
    """Asserts that no TaskStateEntered/TaskStateExited event in `history`
    carries an `input`/`output` JSON payload containing document text,
    prompt text, retrieved-precedent text, or model output.

    Raises SubstanceLeakError (with the offending state name and value) on
    the first violation found. Returns None on a clean scan.
    """
    for event in history:
        details = event.get("stateEnteredEventDetails") or event.get("stateExitedEventDetails")
        if details is None:
            continue
        state_name = details.get("name", "<unknown-state>")
        raw = details.get("input") or details.get("output")
        if raw is None:
            continue
        payload = json.loads(raw)
        for value in _walk_string_values(payload):
            if not _looks_like_pointer_or_enum(value):
                raise SubstanceLeakError(
                    f"state {state_name!r} recorded a non-pointer, non-enum, "
                    f"over-length string value in execution history "
                    f"(len={len(value)}): {value[:80]!r}..."
                )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _pointer_only_initial_input(playbook_id: str = "eiaa") -> dict[str, Any]:
    review_id = f"review-{uuid.uuid4()}"
    return {
        "review_id": review_id,
        "owner_sub": "owner-substance-scan-test",
        "playbook_id": playbook_id,
        "upload_s3_key": f"uploads/owner-substance-scan-test/{review_id}/in.docx",
    }


class TestCleanExecutionPassesTheScan(unittest.TestCase):
    """The real pipeline chain, run end-to-end with the real mock_review
    handler, must produce an execution history with no substance leak --
    this is the "positive" case proving the scan does not false-positive on
    the actual production payload shape."""

    def test_eiaa_playbook_full_chain_is_pointer_only(self):
        history = run_fake_execution(_pointer_only_initial_input("eiaa"))
        self.assertGreater(len(history), 0)
        # Must not raise.
        scan_execution_history_for_substance(history)

    def test_nda_playbook_full_chain_is_pointer_only(self):
        history = run_fake_execution(_pointer_only_initial_input("nda"))
        scan_execution_history_for_substance(history)

    def test_unknown_playbook_full_chain_is_pointer_only(self):
        history = run_fake_execution(_pointer_only_initial_input("some-unlisted-playbook"))
        scan_execution_history_for_substance(history)

    def test_history_actually_carries_the_mock_review_stage_output(self):
        """Sanity check that the engine is really running the production
        handler (not vacuously passing) -- the recorded output for
        MockReviewStage must contain the real decision the handler computed."""
        history = run_fake_execution(_pointer_only_initial_input("eiaa"))
        mock_review_exit = next(
            e
            for e in history
            if e["type"] == "TaskStateExited"
            and e["stateExitedEventDetails"]["name"] == "MockReviewStage"
        )
        output = json.loads(mock_review_exit["stateExitedEventDetails"]["output"])
        self.assertEqual(output["decision"], "REQUEST_CHANGE")


class TestSubstanceScanCatchesRegression(unittest.TestCase):
    """Proves this is a REAL runtime check, not a restatement of the static
    regex: a regressed handler that leaks document text under a field name
    the static regex (which only greps for doc_text/document_content/
    document_body/full_text) has never heard of is still caught, because
    the scan inspects actual values, not source-code variable names."""

    def test_regressed_stage_leaking_text_under_a_novel_field_name_is_caught(self):
        def _regressed_redline_stage(event: dict[str, Any]) -> dict[str, Any]:
            out = dict(event)
            # Deliberately NOT named doc_text/document_content/document_body/
            # full_text -- the static regex in tests/test_pipeline_stack.py
            # check_b_stage_skeleton_mock_task would not catch this.
            out["clause_excerpt_for_debugging"] = (
                "The Receiving Party shall not disclose Confidential "
                "Information of the Disclosing Party to any third party "
                "without prior written consent, except as required by law. "
                "This obligation survives termination of this Agreement for "
                "a period of five (5) years."
            )
            return out

        regressed_chain = list(DEFAULT_STAGE_CHAIN)
        # Replace RedlineStage's handler in place, keeping every other stage
        # identical to the real chain.
        regressed_chain = [
            (name, _regressed_redline_stage if name == "RedlineStage" else fn)
            for name, fn in regressed_chain
        ]

        history = run_fake_execution(_pointer_only_initial_input("eiaa"), regressed_chain)

        with self.assertRaises(SubstanceLeakError) as ctx:
            scan_execution_history_for_substance(history)
        self.assertIn("RedlineStage", str(ctx.exception))

    def test_regressed_stage_leaking_short_but_flagged_pattern_is_still_bounded_by_length(self):
        """Documents the scanner's own limitation: it is a length/shape
        heuristic, not a semantic one, so a SHORT leaked fragment under the
        pointer-length ceiling is a known residual (same "not a silent
        miss" documentation convention as scripts/leakage_scan.py's
        paraphrase residual). This test pins that documented boundary so a
        future change to _MAX_PLAUSIBLE_POINTER_LENGTH is a deliberate,
        reviewed decision rather than an accidental widening."""
        short_leak = "Confidential Information clause."
        self.assertLessEqual(len(short_leak), _MAX_PLAUSIBLE_POINTER_LENGTH)


class TestScannerRejectsObviousModelOutputLeak(unittest.TestCase):
    """A model-output-shaped leak (long free text under any field name) in
    ANY stage's output must be caught, not just the redline stage."""

    def test_leak_in_mock_review_output_itself_would_be_caught(self):
        def _leaking_mock_review(event: dict[str, Any]) -> dict[str, Any]:
            out = mock_review_handler.handler(event, None)
            out["primary_review_rationale"] = (
                "Section 4.2's indemnification carve-out is unacceptable "
                "because it shifts uncapped liability for third-party IP "
                "claims onto Exos, contrary to our standard negotiating "
                "position on this clause family."
            )
            return out

        chain = [
            (name, _leaking_mock_review if name == "MockReviewStage" else fn)
            for name, fn in DEFAULT_STAGE_CHAIN
        ]
        history = run_fake_execution(_pointer_only_initial_input("eiaa"), chain)

        with self.assertRaises(SubstanceLeakError) as ctx:
            scan_execution_history_for_substance(history)
        self.assertIn("MockReviewStage", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
