#!/usr/bin/env python3
"""
Executable tests for issue #104: a *raised* playbook-lookup failure while
resolving the cover-note leakage-scan corpus (a DynamoDB throttle, an S3
outage, a credential failure) must not silently degrade to an EMPTY,
never-blocking corpus -- `backend/src/review_routes.py
::_default_cover_note_leakage_corpus` caught every exception the SAME way a
routine registry miss is handled, so the counterparty-bound cover-note
draft was returned "clean" after being scanned against nothing, with no
log line, no audit row, and no response marker distinguishing "scanned
clean" from "scanned against nothing".

## Owner decision (2026-09-14): fail closed on lookup error

A RAISED playbook lookup (throttle, S3 outage, credential failure) now
returns 503 with a stable detail token and writes an audit row. A registry
miss or a review with no `playbook_id` keeps the existing empty-corpus
fail-open (#479's decision stands) -- matching the main pipeline's
`run_real_pipeline`/`run_leakage_gate` posture of refusing rather than
guessing when the bundle cannot be resolved at all.

Drives the real `src.review_routes.router` end-to-end, reusing
`tests.test_cover_note_499.CoverNoteRouteTestBase`'s harness (real router,
moto S3, fake DynamoDB) -- same convention `test_preflight_491.py` already
established for reusing `test_review_api_84.ReviewApiTestBase`.

This test MUST FAIL on the pre-fix tree (200, no WARNING log, no audit
row) and PASS after the fix. Run standalone:
`python tests/test_cover_note_corpus_lookup_failure_104.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("MODEL_INVOCATIONS_TABLE", "contract-toaster-model-invocations-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test",
)
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENV_NAME", "dev")
# Deliberately NOT setting PLAYBOOK_VERSIONS_TABLE at module scope: the
# registry-miss tests below need it ABSENT (matching
# tests/test_cover_note_499.py's own "no active OPF bundle" coverage --
# `_load_opf_bundle_if_active` returns None immediately, touching no
# table, when this var is unset), so it is set, and cleaned up again, only
# inside the raised-lookup-failure tests that need
# `dynamodb_resource.Table(...)` reached at all.
_PLAYBOOK_VERSIONS_TABLE_NAME = "contract-toaster-playbook-versions-104-test"

import src.review_routes as review_routes  # noqa: E402
import test_cover_note_499 as cover_note_499  # noqa: E402

_KNOWN_EDITS_DRAFT = (
    "Attached is our markup. We restored the liability cap and made the "
    "indemnification obligation mutual. Please let us know if you would "
    "like to discuss."
)


class _RaisingTableDynamoDBResource:
    """Delegates every table to `base` EXCEPT `raising_table_name`, whose
    `.Table()` call itself raises -- standing in for a DynamoDB throttle or
    a credential failure surfaced at lookup time (issue #104's Evidence
    section reproduces this with a resource whose every attribute raises
    `ProvisionedThroughputExceededException`; a plain `RuntimeError` here
    is enough to prove the route no longer treats ANY exception the same
    as a registry miss -- the two are asserted as DIFFERENT branches
    below, not conflated by construction)."""

    def __init__(self, base: Any, raising_table_name: str) -> None:
        self._base = base
        self._raising_table_name = raising_table_name

    def Table(self, name: str) -> Any:  # noqa: N802 - boto3-shaped API
        if name == self._raising_table_name:
            raise RuntimeError("ProvisionedThroughputExceededException (simulated)")
        return self._base.Table(name)


class CorpusLookupFailureTestBase(cover_note_499.CoverNoteRouteTestBase):
    def _install_raising_dynamodb(self) -> None:
        """Sets `PLAYBOOK_VERSIONS_TABLE` (cleaned up on test teardown) so
        `_load_playbook_bundle` actually reaches `dynamodb_resource
        .Table(...)` on this route's corpus-resolution path, then replaces
        the base class's `get_dynamodb_resource` override with one that
        raises ONLY for that table -- wrapping (not replacing)
        `self._routing_ddb` so REVIEWS_TABLE, AUDIT_TABLE and the
        MODEL_INVOCATIONS_TABLE ledger keep working exactly as
        `CoverNoteRouteTestBase.setUp` already wired them (the audit-row
        assertion below depends on AUDIT_TABLE writes still succeeding)."""
        os.environ["PLAYBOOK_VERSIONS_TABLE"] = _PLAYBOOK_VERSIONS_TABLE_NAME
        self.addCleanup(os.environ.pop, "PLAYBOOK_VERSIONS_TABLE", None)
        raising = _RaisingTableDynamoDBResource(
            self._routing_ddb, _PLAYBOOK_VERSIONS_TABLE_NAME
        )
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = lambda: raising


# -- A raised lookup failure is refused, not silently scanned against -------
# nothing (the bug this issue closes).


class TestRaisedLookupFailureFailsClosed(CorpusLookupFailureTestBase):
    def test_a_raised_playbook_lookup_failure_returns_503_with_an_audit_row(self):
        self._install_raising_dynamodb()
        self._seed_done_review(
            "review-corpus-throttle",
            "owner-corpus-throttle",
            playbook_id="any-playbook-id",
        )
        self._set_cover_note_client(
            cover_note_499.FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        )

        with self.assertLogs("src.review_routes", level="WARNING") as captured:
            resp = self._butter("owner-corpus-throttle", "review-corpus-throttle")

        # (a) the response: a 503 carrying a stable, non-substantive
        # token -- never the raw `RuntimeError` text, never a 200.
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(
            resp.json()["detail"], review_routes.COVER_NOTE_SCAN_UNAVAILABLE_TOKEN
        )
        self.assertNotIn("ProvisionedThroughputExceededException", resp.text)

        # (b) the log: a WARNING naming the review, distinguishing this
        # from an undifferentiated generation failure.
        self.assertTrue(
            any("review-corpus-throttle" in line for line in captured.output),
            captured.output,
        )

        # (c) the audit row: the scan_corpus marker, matching the
        # response token exactly (the two must never drift apart).
        audit_items = list(self._audit_table().items.values())
        matching = [
            item for item in audit_items if item.get("target") == "review-corpus-throttle"
        ]
        self.assertEqual(len(matching), 1, audit_items)
        self.assertEqual(matching[0]["action"], "leakage_scan_corpus_unavailable")
        self.assertEqual(
            matching[0]["scan_corpus"], review_routes.COVER_NOTE_SCAN_UNAVAILABLE_TOKEN
        )

        # (d) nothing was persisted or billed from the un-scanned draft.
        row = self._reviews_table().items["review-corpus-throttle"]
        self.assertNotIn("cover_note_draft", row)

    def test_the_draft_text_itself_never_reaches_the_response_or_the_row(self):
        """Belt-and-suspenders on the escaped-text-only rule: even though
        the model call succeeded and produced a real draft, a corpus-
        lookup failure downstream of it must not let that draft leak into
        either the 503 response body or the reviews row."""
        self._install_raising_dynamodb()
        self._seed_done_review(
            "review-corpus-throttle-2",
            "owner-corpus-throttle-2",
            playbook_id="any-playbook-id",
        )
        self._set_cover_note_client(
            cover_note_499.FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        )

        resp = self._butter("owner-corpus-throttle-2", "review-corpus-throttle-2")

        self.assertEqual(resp.status_code, 503)
        self.assertNotIn(_KNOWN_EDITS_DRAFT, resp.text)
        row = self._reviews_table().items["review-corpus-throttle-2"]
        self.assertNotIn("cover_note_draft", row)


# -- A registry miss / no playbook_id keeps the existing fail-open ----------
# posture (#479's decision stands) -- proves the fix did not collapse the
# two cases into one.


class TestRegistryMissStaysFailOpen(CorpusLookupFailureTestBase):
    def test_an_unregistered_playbook_id_with_no_active_version_still_answers_200(self):
        """`_install_raising_dynamodb` is deliberately NOT called here --
        `PLAYBOOK_VERSIONS_TABLE` stays unset (as most deployments have it,
        per `_load_opf_bundle_if_active`'s own docstring), so
        `_load_playbook_bundle` falls straight through to the registry,
        which raises `PlaybookNotRegisteredError` for this unknown id.
        That specific exception must still degrade to an empty,
        never-blocking corpus -- same coverage
        `test_cover_note_499.TestDefaultLeakageCorpusIsOpfAware
        .test_a_v1_playbook_id_with_no_active_opf_bundle_is_unaffected`
        already has, kept here for a self-contained positive/negative
        pair against the SAME lookup-failure fix."""
        self._seed_done_review(
            "review-registry-miss",
            "owner-registry-miss",
            playbook_id="does-not-exist-in-registry",
        )
        self._set_cover_note_client(
            cover_note_499.FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        )

        resp = self._butter("owner-registry-miss", "review-registry-miss")

        self.assertEqual(resp.status_code, 200)
        row = self._reviews_table().items["review-registry-miss"]
        self.assertEqual(row.get("cover_note_draft"), _KNOWN_EDITS_DRAFT)

    def test_no_playbook_id_at_all_still_answers_200(self):
        self._seed_done_review(
            "review-no-playbook-id", "owner-no-playbook-id", playbook_id=None
        )
        self._set_cover_note_client(
            cover_note_499.FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        )

        resp = self._butter("owner-no-playbook-id", "review-no-playbook-id")

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
