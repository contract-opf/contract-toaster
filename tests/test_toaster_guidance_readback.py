#!/usr/bin/env python3
"""
Slice test for issue #431's read path: the per-review `toaster_guidance` a
review was submitted with must be answerable FROM THE REVIEW ITSELF.

## Root problem this proves fixed

Issue #398 wired `toaster_guidance` end to end on the WRITE side: `POST
/api/reviews` accepts it, `backend/src/reviews.py::submit_review` threads it
into the execution-input JSON, and the pipeline hands it to the primary +
critic passes. But the value landed only in the submission record's
`execution_input` blob -- a dedup/idempotency artifact keyed by
idempotency_key, which the read path never touches. `get_review_detail`
(GET /api/reviews/{id}, keyed by review_id) therefore had no way to say what
instructions governed a given review, so the Review tab's read-only readback
had nothing to read. "Which rules applied to this review?" was answerable
only from whoever happened to remember typing them.

This test FAILS on a tree where `_create_review_row` does not record
`toaster_guidance` / `get_review_detail` does not project it, and PASSES
once both do.

## What this test asserts

  1. Submitting with guidance records it on the REVIEWS row (the row the
     read path reads), and `get_review_detail` hands it back verbatim to
     the review's owner.
  2. Submitting WITHOUT guidance leaves the reviews row byte-identical to
     the row written before this issue -- the key is ABSENT, never a null
     or empty-string placeholder (the same "absent, not null" convention
     the OPF-lineage fields already follow) -- and the detail projects
     None, so the UI renders no readback rather than an empty one.
  3. Whitespace-only guidance is no guidance: not recorded, exactly as
     `scripts/primary_review_pass.py::render_toaster_guidance_block`
     already treats it when building the prompt block.
  4. The recorded value is stored VERBATIM (leading/trailing whitespace
     around real content preserved) -- the row is a record of what was
     submitted, not a normalized copy of it.
  5. A RESUMED submission (same owner + file + bundle inside the
     idempotency window, guidance added on the retry -- guidance is not
     part of the key) leaves the original row's value untouched, so the
     detail keeps projecting what actually governed the running execution.
  6. The readback inherits the detail route's existing owner-or-admin
     scoping and its enumeration-proof 404: a stranger asking for the
     review gets the same 404 as for a review_id that does not exist, and
     never the guidance text.

## What this test deliberately does NOT prove

That the model obeys the guidance -- that is issue #398's prompt-plumbing
gate (tests/test_llm_native_overlay.py) and, beyond it, a live-model check.
This file is strictly about the submit -> row -> detail round trip.

Run standalone: `python3 tests/test_toaster_guidance_readback.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Cross-test-file import (established convention -- see
# tests/test_llm_native_overlay.py importing the same two names): reuse the
# in-memory DynamoDB fake and the already-imported `reviews` module from the
# #59 submission slice test, whose module-level third-party stubs and
# env-var setdefaults happen once, on first import.
from test_review_submission_e2e import FakeDynamoDBResource  # noqa: E402
from test_review_submission_e2e import _reviews_module as reviews_module  # noqa: E402

HTTPException = sys.modules["fastapi"].HTTPException

GUIDANCE = "Hold the notice period at 30 days regardless of the playbook's position."


class _StubSfnClient:
    """Enough Step Functions surface for submit_review's happy path."""

    class exceptions:  # noqa: N801 - mirrors boto3 client's `.exceptions` attribute shape
        class ExecutionAlreadyExists(Exception):
            pass

    def __init__(self) -> None:
        self.started_names: set[str] = set()

    def start_execution(self, stateMachineArn, name, input):  # noqa: A002,N803
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"inprocess:{name}"}


def _submit_result(
    ddb: Any, owner_sub: str, suffix: str, sfn: Any | None = None, **kwargs: Any
) -> dict[str, Any]:
    return reviews_module.submit_review(
        owner_sub=owner_sub,
        playbook_id="eiaa",
        file_sha256=f"filehash-{suffix}",
        upload_pointer=f"uploads/{owner_sub}/review-{suffix}/in.docx",
        active_release_bundle_hash=f"bundle-hash-{suffix}",
        dynamodb_resource=ddb,
        sfn_client=sfn or _StubSfnClient(),
        **kwargs,
    )


def _submit(ddb: Any, owner_sub: str, suffix: str, **kwargs: Any) -> str:
    return _submit_result(ddb, owner_sub, suffix, **kwargs)["review_id"]


def _review_row(ddb: Any, review_id: str) -> dict[str, Any]:
    table = ddb.Table(os.environ["REVIEWS_TABLE"])
    return table.get_item(Key={"review_id": review_id}).get("Item") or {}


# ---------------------------------------------------------------------------
# 1. Guidance recorded on the reviews row and projected by get_review_detail.
# ---------------------------------------------------------------------------


def test_guidance_round_trips_from_submission_to_detail(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    owner = "owner-431"
    review_id = _submit(ddb, owner, "431a", toaster_guidance=GUIDANCE)

    row = _review_row(ddb, review_id)
    if row.get("toaster_guidance") != GUIDANCE:
        failures.append(
            f"[1a] The reviews row -- the row the read path reads -- must record the "
            f"submitted toaster_guidance; got {row.get('toaster_guidance')!r}"
        )

    detail = reviews_module.get_review_detail(review_id, {"cognito_sub": owner}, ddb)
    if detail.get("toaster_guidance") != GUIDANCE:
        failures.append(
            f"[1b] get_review_detail must project toaster_guidance verbatim; "
            f"got {detail.get('toaster_guidance')!r}"
        )


def test_guidance_is_stored_verbatim_not_normalized(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    owner = "owner-431-verbatim"
    padded = f"  {GUIDANCE}  "
    review_id = _submit(ddb, owner, "431verbatim", toaster_guidance=padded)

    detail = reviews_module.get_review_detail(review_id, {"cognito_sub": owner}, ddb)
    if detail.get("toaster_guidance") != padded:
        failures.append(
            f"[2a] The row is a record of what was submitted, not a normalized copy: "
            f"expected {padded!r}, got {detail.get('toaster_guidance')!r}"
        )


# ---------------------------------------------------------------------------
# 2. No guidance -> the key is ABSENT from the row (never a null/empty
#    placeholder) and the detail projects None.
# ---------------------------------------------------------------------------


def test_no_guidance_leaves_the_row_untouched(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    owner = "owner-431b"
    review_id = _submit(ddb, owner, "431b")

    row = _review_row(ddb, review_id)
    if "toaster_guidance" in row:
        failures.append(
            f"[3a] A review submitted with no guidance must leave the row byte-identical "
            f"to before this issue -- key ABSENT, never a placeholder; got "
            f"{row.get('toaster_guidance')!r}"
        )

    detail = reviews_module.get_review_detail(review_id, {"cognito_sub": owner}, ddb)
    if detail.get("toaster_guidance") is not None:
        failures.append(
            f"[3b] get_review_detail must project None when no guidance was submitted "
            f"(so the UI renders no readback at all); got {detail.get('toaster_guidance')!r}"
        )


def test_whitespace_only_guidance_is_no_guidance(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    owner = "owner-431c"
    review_id = _submit(ddb, owner, "431c", toaster_guidance="   \n\t  ")

    row = _review_row(ddb, review_id)
    if "toaster_guidance" in row:
        failures.append(
            "[4a] Whitespace-only guidance is no guidance (same treatment as "
            "primary_review_pass.render_toaster_guidance_block) -- it must not be "
            f"recorded on the row; got {row.get('toaster_guidance')!r}"
        )


# ---------------------------------------------------------------------------
# 2b. The resumed/idempotent path does NOT rewrite the original row -- so the
#     readback keeps naming what actually governed, and the UI must not
#     substitute the text typed into the resumed submit.
# ---------------------------------------------------------------------------


def test_resumed_submit_does_not_overwrite_the_original_row(failures: list[str]) -> None:
    """The "oops, I forgot my instructions" re-drop.

    derive_idempotency_key keys only on owner_sub + file_sha256 +
    release_bundle_hash + time bucket -- `toaster_guidance` is deliberately
    NOT part of it. So re-dropping the same file inside the same bucket with
    guidance added resumes the ORIGINAL review, whose execution already ran
    (or is running) under no guidance at all. The row must keep saying so:
    anything else is a false record of which rules applied.
    """
    ddb = FakeDynamoDBResource()
    owner = "owner-431-resumed"
    sfn = _StubSfnClient()

    first = _submit_result(ddb, owner, "431resumed", sfn=sfn)
    second = _submit_result(ddb, owner, "431resumed", sfn=sfn, toaster_guidance=GUIDANCE)

    if not second.get("resumed"):
        failures.append(
            "[7a] Re-submitting the same file/bundle for the same owner within the "
            "idempotency window must resume the existing review (guidance is not part "
            f"of the key); got resumed={second.get('resumed')!r}"
        )
    if second.get("review_id") != first.get("review_id"):
        failures.append(
            f"[7b] The resumed submit must return the ORIGINAL review_id; got "
            f"{second.get('review_id')!r} vs {first.get('review_id')!r}"
        )

    row = _review_row(ddb, first["review_id"])
    if "toaster_guidance" in row:
        failures.append(
            "[7c] A resumed submit must leave the original reviews row's guidance "
            "untouched -- the running execution never saw this text, so recording it "
            f"would be a false record of what governed; got {row.get('toaster_guidance')!r}"
        )

    detail = reviews_module.get_review_detail(first["review_id"], {"cognito_sub": owner}, ddb)
    if detail.get("toaster_guidance") is not None:
        failures.append(
            "[7d] get_review_detail must still project None for the resumed review, so "
            "the UI renders no readback rather than the text of the second submit; got "
            f"{detail.get('toaster_guidance')!r}"
        )


# ---------------------------------------------------------------------------
# 3. The readback inherits the detail route's owner-or-admin scoping and its
#    enumeration-proof 404 -- guidance is per-review confidential substance,
#    not a field that widens who may read a review.
# ---------------------------------------------------------------------------


def test_stranger_gets_404_never_the_guidance(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    owner = "owner-431d"
    review_id = _submit(ddb, owner, "431d", toaster_guidance=GUIDANCE)

    try:
        detail = reviews_module.get_review_detail(review_id, {"cognito_sub": "stranger"}, ddb)
    except HTTPException as exc:
        if getattr(exc, "status_code", None) != 404:
            failures.append(
                f"[5a] A non-owner, non-admin caller must get 404 (never 403, which would "
                f"confirm the review exists); got {getattr(exc, 'status_code', None)}"
            )
    else:
        failures.append(
            f"[5b] A non-owner, non-admin caller must not receive the review detail at all; "
            f"got a payload carrying toaster_guidance={detail.get('toaster_guidance')!r}"
        )


def test_admin_may_read_the_guidance(failures: list[str]) -> None:
    ddb = FakeDynamoDBResource()
    owner = "owner-431e"
    review_id = _submit(ddb, owner, "431e", toaster_guidance=GUIDANCE)

    detail = reviews_module.get_review_detail(
        review_id, {"cognito_sub": "an-admin", "is_admin": True}, ddb
    )
    if detail.get("toaster_guidance") != GUIDANCE:
        failures.append(
            f"[6a] An admin caller reads the same detail payload as the owner, guidance "
            f"included; got {detail.get('toaster_guidance')!r}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_TESTS = [
    test_guidance_round_trips_from_submission_to_detail,
    test_guidance_is_stored_verbatim_not_normalized,
    test_no_guidance_leaves_the_row_untouched,
    test_whitespace_only_guidance_is_no_guidance,
    test_resumed_submit_does_not_overwrite_the_original_row,
    test_stranger_gets_404_never_the_guidance,
    test_admin_may_read_the_guidance,
]


def main() -> int:
    failures: list[str] = []
    for test_fn in _ALL_TESTS:
        before = len(failures)
        test_fn(failures)
        status_word = "PASS" if len(failures) == before else "FAIL"
        print(f"{status_word}: {test_fn.__name__}")

    if failures:
        print("\nFAIL: toaster-guidance readback gate (issue #431).\n")
        for f in failures:
            print(f)
        print(f"\nTotal failures: {len(failures)}")
        return 1

    print("\nPASS: all toaster-guidance readback (issue #431) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
