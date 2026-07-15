"""
Mock review Lambda — issue #59 (mock-first MVP scope, epic #123).

This is the ONE swap-point for the real LLM pipeline (#80-#83, behind the
#62 eval gate). It stands in for the primary + adversarial review stages so
the Step Functions skeleton, idempotency, spend reservation, concurrency
control, and status polling can all be proven real end-to-end while the
"brain" is mocked.

Given the uploaded .docx pointer + playbook_id, this handler waits a few
seconds (to exercise the PENDING -> RUNNING -> DONE polling path a real
review would also exercise) and returns a canned result:

  playbook_id == "eiaa"  -> REQUEST_CHANGE, pointing at a pre-baked
                            tracked-changes redline object already staged
                            in the outputs bucket (S3 pointer only).
  playbook_id == "nda"   -> MANUAL_REVIEW_REQUIRED, reason="playbook_coming_soon",
                            with user-facing copy "playbook coming soon —
                            separate playbook later."
  any other playbook_id  -> MANUAL_REVIEW_REQUIRED, reason="unknown_playbook".

POINTER-ONLY PAYLOAD RULE (issue #19): the input and output of this Lambda
carry S3 keys, review_id, and playbook_id only -- never document text,
prompt text, or model output inline. Step Functions persists state
input/output in execution history (retained, console-visible, no
classification boundary), so nothing substantive may pass through it.

Input event shape (from the state machine, pointer-only):
  {
    "review_id": "...",
    "playbook_id": "eiaa" | "nda" | ...,
    "upload_s3_key": "uploads/<owner_sub>/<review_id>/in.docx",
    "owner_sub": "..."
  }

Output shape (pointer-only):
  {
    "review_id": "...",
    "decision": "REQUEST_CHANGE" | "MANUAL_REVIEW_REQUIRED",
    "reason": null | "playbook_coming_soon" | "unknown_playbook",
    "output_s3_key": null | "outputs/<review_id>/out.docx",
    "summary": "<short non-substantive summary string>",
    "watermark": "tool recommendation only - attorney approval required",
    "semaphore_acquired": true,
    "semaphore_wait_attempts": 0,
    "semaphore_give_up": false
  }

Note on the last three fields (issue #190, review round 1): every stage
LambdaInvoke in pipeline-stack.ts uses `outputPath: '$.Payload'` (no
`resultPath` merge), so each stage's return value wholly replaces the state
machine payload rather than merging with it. This handler therefore
explicitly carries the concurrency-semaphore acquire Lambda's
semaphore_acquired / semaphore_wait_attempts / semaphore_give_up fields
forward from its input event so ReleaseConcurrencySlot (the state after
audit) still sees them and can correctly decrement the counter for a
successful review.
"""

import os
import time
from typing import Any

# Brief delay so the state transitions PENDING -> RUNNING -> DONE are
# observable by the UI poll loop, same as a real (much longer) review would
# be. Kept short so CI / local test executions stay fast.
MOCK_REVIEW_DELAY_SECONDS = float(os.environ.get("MOCK_REVIEW_DELAY_SECONDS", "3"))

WATERMARK = "tool recommendation only - attorney approval required"

# Pre-baked tracked-changes redline staged in the outputs bucket ahead of
# time for the eiaa mock path. Pointer only -- no document text lives here.
PRE_BAKED_EIAA_REDLINE_KEY_TEMPLATE = "mock-fixtures/eiaa/pre-baked-redline.docx"


def _mock_eiaa_result(review_id: str) -> dict[str, Any]:
    """playbook_id == 'eiaa' -> REQUEST_CHANGE from a pre-baked redline."""
    return {
        "review_id": review_id,
        "decision": "REQUEST_CHANGE",
        "reason": None,
        # Pointer only: the actual redlined .docx is copied server-side from
        # the pre-baked fixture into this review's output prefix by the
        # persist stage; this handler never reads or writes document bytes.
        # The key MUST be scoped to exactly ``outputs/<review_id>/`` — this
        # is the per-review prefix backend/src/download.py's
        # _validate_s3_key_bound_to_review enforces before presigning a
        # download (issue #71 AC2). An owner_sub segment here would make
        # every GET /api/reviews/{id}/output 403.
        "output_s3_key": f"outputs/{review_id}/out.docx",
        "pre_baked_source_key": PRE_BAKED_EIAA_REDLINE_KEY_TEMPLATE,
        "summary": "Mock review: canned REQUEST_CHANGE result (issue #59 mock boundary).",
        "watermark": WATERMARK,
    }


def _mock_nda_result(review_id: str) -> dict[str, Any]:
    """playbook_id == 'nda' -> MANUAL_REVIEW_REQUIRED, 'coming soon'."""
    return {
        "review_id": review_id,
        "decision": "MANUAL_REVIEW_REQUIRED",
        "reason": "playbook_coming_soon",
        "output_s3_key": None,
        "summary": "playbook coming soon - separate playbook later.",
        "watermark": WATERMARK,
    }


def _mock_unknown_playbook_result(review_id: str, playbook_id: str) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "decision": "MANUAL_REVIEW_REQUIRED",
        "reason": "unknown_playbook",
        "output_s3_key": None,
        "summary": f"Unknown playbook_id '{playbook_id}'.",
        "watermark": WATERMARK,
    }


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Step Functions task entry point for the mock review stage.

    NOTE: this is the single, explicitly-labeled swap point. When the real
    pipeline (#80-#83) is ready behind the #62 eval gate, this Lambda target
    is replaced in pipeline-stack.ts by the real primary/adversarial review
    stages -- nothing else in the skeleton changes.
    """
    review_id = event["review_id"]
    playbook_id = event.get("playbook_id", "")

    # Exercise the PENDING -> RUNNING -> DONE polling window, same as a real
    # (much longer) review would.
    if MOCK_REVIEW_DELAY_SECONDS > 0:
        time.sleep(MOCK_REVIEW_DELAY_SECONDS)

    if playbook_id == "eiaa":
        result = _mock_eiaa_result(review_id)
    elif playbook_id == "nda":
        result = _mock_nda_result(review_id)
    else:
        result = _mock_unknown_playbook_result(review_id, playbook_id)

    # Issue #190 (review round 1): every stage LambdaInvoke in
    # pipeline-stack.ts uses `outputPath: '$.Payload'` with no `resultPath`
    # merge, so this stage's return value WHOLLY REPLACES the state
    # machine's payload -- it does not get merged with the input. The
    # concurrency-semaphore acquire Lambda threads semaphore_acquired /
    # semaphore_wait_attempts / semaphore_give_up through that payload so
    # ReleaseConcurrencySlot can tell whether this execution actually holds
    # a slot to release. Without explicitly carrying those fields forward
    # here, a successful review's payload would arrive at
    # ReleaseConcurrencySlot flag-less, causing release() to treat every
    # successful review as "never acquired" and skip the counter
    # decrement -- leaking current_count upward on every success and
    # eventually wedging the whole pipeline behind the concurrency cap.
    result["semaphore_acquired"] = event.get("semaphore_acquired")
    result["semaphore_wait_attempts"] = event.get("semaphore_wait_attempts")
    result["semaphore_give_up"] = event.get("semaphore_give_up")

    return result
