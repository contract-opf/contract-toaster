#!/usr/bin/env python3
"""
Executable tests for issue #499: `POST /api/reviews/{review_id}/cover-note`
("Butter it").

Drafts the counterparty cover email from a FINISHED review's own analysis
artifact (the `issues[]` + `verdict_summary` already persisted on the
`reviews` row) -- never re-reads the document, never re-runs review.
Copy-only: nothing is ever sent by the toaster.

Drives the real `src.review_routes.router` end-to-end via a FastAPI
`TestClient`, reusing `test_review_api_84.ReviewApiTestBase`'s harness (real
router, moto S3, fake DynamoDB, fake Step Functions) -- same convention
`test_preflight_491.py` already established for a second model-backed
route on this same harness.

Covers the issue's Acceptance criteria:
  (1) a completed (DONE, REQUEST_CHANGE) review with known issues yields a
      <=150-word neutral draft whose bullets trace to the real applied
      edits (a hand-seeded fixture standing in for "the mock pipeline's
      known edits" -- deterministic, offline, checkable by substring).
  (2) regenerate produces a fresh draft and a NEW spend-ledger row; a
      revisit without regenerate serves the cached draft for FREE (no new
      model call, no new spend, no new ledger row).
  (3) the route refuses a non-owner caller (403) and a non-DONE review, or
      one with nothing to describe, (409); the served model is pinned to
      the policy's `cover_note` role, never primary/critic.
  (4) no send capability anywhere in this response shape; the card's cost
      is the real settled cost, priced against the cover_note role's own
      rates.
  (5) a failed generation degrades to a quiet failure (502) and leaves any
      previously cached draft untouched.
  (6) the deterministic guardrails (greeting/sign-off/legal-promise strip,
      150-word cap) actually reach the persisted, returned draft end to
      end, not just in the unit-level `cover_note_pass` tests.

This test MUST FAIL on the pre-fix tree (the route does not exist) and PASS
after the fix. Run standalone: `python tests/test_cover_note_499.py`.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

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

from fastapi.testclient import TestClient  # noqa: E402

import cover_note_pass  # noqa: E402
import leakage_scan  # noqa: E402
import src.model_client as model_client  # noqa: E402
import src.retention as retention_module  # noqa: E402
import src.review_routes as review_routes  # noqa: E402
import src.reviews as reviews_module  # noqa: E402
import test_review_api_84 as api84  # noqa: E402

# ---------------------------------------------------------------------------
# A hand-seeded pair of REQUEST_CHANGE issues standing in for "the mock
# pipeline's known edits" (issue #499 AC) -- the mock pipeline itself
# (backend/src/pipeline_runner.py::_mock_decision) never populates issues[]
# at all (it copies a pre-baked .docx and stops), so there is nothing there
# to drive this test from; a hand-seeded, deterministic, checkable fixture
# does the same job the issue's own wording asks for -- issues whose
# section/summary/rationale text can be asserted to survive, verbatim or in
# substance, into the generated bullets.
# ---------------------------------------------------------------------------

KNOWN_ISSUES = [
    {
        "section_ref": "8 Limitation on Liability",
        "section_title": "Limitation on Liability",
        "counterparty_change_summary": "Counterparty removed the liability cap entirely.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "A standard liability cap is required by policy.",
        "proposed_replacement_text": (
            "Liability shall not exceed fees paid in the preceding twelve months."
        ),
        "playbook_topic_id": "liability-cap",
        "internal_precedent_citation": None,
        "provenance": "fixture:liability-cap-removed",
    },
    {
        "section_ref": "12 Indemnification",
        "section_title": "Indemnification",
        "counterparty_change_summary": "Counterparty added a one-way indemnity in its own favor.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "Indemnification must be mutual under standard position.",
        "proposed_replacement_text": "Each party shall indemnify the other for its own breach.",
        "playbook_topic_id": "indemnification",
        "internal_precedent_citation": None,
        "provenance": "fixture:indemnity-one-way",
    },
]

# A response referencing both known edits in business English -- the
# fixture test asserts each edit's real label survives into the draft.
_KNOWN_EDITS_DRAFT = (
    "Attached is our markup of the agreement. The substantive changes are:\n"
    "- We restored a cap on liability, consistent with our standard position.\n"
    "- We made the indemnification obligation mutual rather than one-sided.\n"
    "Happy to discuss further.\n"
    "Best,\n"
    "[Your Name]"
)


class FakeCoverNoteModelClient:
    """Duck-types `model_client.OpenRouterModelClient`'s public surface the
    route reads: `.invoke(...)`, `.last_usage`, `.last_served_model`.

    `responses` is a queue popped on each `.invoke()` call -- a string is
    returned as the raw draft text, an `Exception` instance is raised --
    so one client instance can stand in for a success-then-failure or a
    two-different-drafts sequence within a single test (issue #499's
    regenerate path).
    """

    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        served_model: str | None = None,
        usage: dict[str, int] | None = None,
    ):
        self._responses = list(responses or [])
        self.last_usage = usage
        self.last_served_model = served_model
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> str:
        self.invocations.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeCoverNoteModelClient has no more seeded responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeLedgerTable:
    """A real, append-only stand-in for the MODEL_INVOCATIONS_TABLE ledger
    (mirrors test_model_invocation_ledger.py's own FakeLedgerTable) -- the
    shared `test_review_api_84.FakeDynamoDBResource`/`FakeTable` pair is
    dict-keyed with a single item per partition key, which cannot represent
    "one row per attempt" (issue #499 AC: "Spend ledger row per
    generation"). `self.items` is a plain list precisely so a second
    generation's row is a SECOND entry, never an overwrite of the first."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item: dict[str, Any]) -> None:
        self.items.append(Item)


class _RoutingDynamoDBResource:
    """Routes MODEL_INVOCATIONS_TABLE to a real `FakeLedgerTable`; every
    other table name is delegated to the underlying
    `test_review_api_84.FakeDynamoDBResource` unchanged, so every other
    route behavior (reviews row read/write, daily-spend settle, audit)
    keeps using the exact fake `ReviewApiTestBase` already sets up."""

    def __init__(self, base: Any, ledger_table: FakeLedgerTable) -> None:
        self._base = base
        self._ledger_table_name = os.environ["MODEL_INVOCATIONS_TABLE"]
        self._ledger_table = ledger_table

    def Table(self, name: str) -> Any:
        if name == self._ledger_table_name:
            return self._ledger_table
        return self._base.Table(name)


class CoverNoteRouteTestBase(api84.ReviewApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger_table = FakeLedgerTable()
        self._routing_ddb = _RoutingDynamoDBResource(self.ddb, self.ledger_table)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = (
            lambda: self._routing_ddb
        )
        self._set_cover_note_client(None)

    def _set_cover_note_client(self, client: Any) -> None:
        self.app.dependency_overrides[review_routes.get_cover_note_model_client] = (
            lambda: client
        )

    def _seed_done_review(
        self,
        review_id: str,
        owner_sub: str,
        *,
        issues: list[dict[str, Any]] | None = KNOWN_ISSUES,  # type: ignore[assignment]
        critic_delta: dict[str, Any] | None = None,
        verdict_summary: str | None = None,
        status: str = "DONE",
        decision: str = "REQUEST_CHANGE",
        cover_note_draft: str | None = None,
        # Issue #499 fix round 3 (review finding): the route now refuses a
        # review past its retention window (retention._is_past_retention),
        # the same predicate the purge sweep itself runs. `created_at` here
        # is a fixed, ancient epoch ("1000") on purpose (existing tests don't
        # care about its value) -- defaulting `retention_window_at_creation`
        # to the "forever" sentinel means that ancient timestamp never
        # trips the new gate for every OTHER test in this file. Only
        # TestRetentionGating below passes a real window to construct a
        # genuinely past-retention row.
        retention_window_at_creation: Any = retention_module.RETENTION_WINDOW_FOREVER,
        legal_hold: bool = False,
        playbook_id: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "review_id": review_id,
            "owner_sub": owner_sub,
            "status": status,
            "decision": decision,
            "verdict_summary": verdict_summary,
            "created_at": "1000",
            "updated_at": "1000",
            "retention_window_at_creation": retention_window_at_creation,
            "legal_hold": legal_hold,
            "playbook_id": playbook_id,
        }
        # `issues` is never written to the `reviews` row itself in
        # production -- it lives only in the persisted analysis artifact
        # (`outputs/{review_id}/analysis.json`, `reviews.load_analysis_
        # artifact`). Round-trip through the SAME real S3 read path the
        # route now uses, rather than hand-seeding `item["issues"]`
        # directly -- that direct seed is exactly the fixture-fidelity gap
        # (fixture accepts what the real reader would reject) that hid the
        # #416/cover-note bug for months. `issues=None` (never exercised by
        # this file today) models a review that predates the artifact:
        # no key, no object, same as `_write_real_analysis` never having run.
        if issues is not None:
            analysis_key = f"outputs/{review_id}/analysis.json"
            self.s3.put_object(
                Bucket=os.environ["OUTPUTS_BUCKET"],
                Key=analysis_key,
                Body=json.dumps({"findings": issues, "critic_delta": critic_delta}).encode(
                    "utf-8"
                ),
                ContentType="application/json",
            )
            item["analysis_s3_key"] = analysis_key
        if cover_note_draft is not None:
            item["cover_note_draft"] = cover_note_draft
            item["cover_note_generated_at"] = "1000"
            item["cover_note_cost_usd_cents"] = 1
            item["cover_note_served_model_id"] = model_client.openrouter_cover_note_model_id()
        self._reviews_table().items[review_id] = item

    def _butter(
        self, owner_sub: str, review_id: str, *, regenerate: bool | None = None
    ):
        self._authenticate_as(owner_sub)
        body: dict[str, Any] = {}
        if regenerate is not None:
            body["regenerate"] = regenerate
        return self.client.post(f"/api/reviews/{review_id}/cover-note", json=body)

    def _daily_spend_row(self) -> dict[str, Any]:
        spend_date = time.strftime("%Y-%m-%d", time.gmtime())
        table = self.ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        return table.items.get(spend_date, {})


# ---------------------------------------------------------------------------
# Route registration sanity.
# ---------------------------------------------------------------------------


class TestRouteRegistration(unittest.TestCase):
    def test_cover_note_route_registered(self):
        registered = {
            (getattr(r, "path", None), method)
            for r in review_routes.router.routes
            for method in getattr(r, "methods", set())
        }
        self.assertIn(("/api/reviews/{review_id}/cover-note", "POST"), registered)


# -- (1) traceable draft from known edits ------------------------------------


class TestKnownEditsTraceIntoTheDraft(CoverNoteRouteTestBase):
    def test_draft_traces_to_real_applied_edits_and_is_at_most_150_words(self):
        self._seed_done_review("review-butter-1", "owner-butter-1")
        fake_client = FakeCoverNoteModelClient(
            [_KNOWN_EDITS_DRAFT],
            served_model=model_client.openrouter_cover_note_model_id(),
            usage={"input_tokens": 50_000, "output_tokens": 300},
        )
        self._set_cover_note_client(fake_client)

        resp = self._butter("owner-butter-1", "review-butter-1")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        draft = body["draft"]
        self.assertIn("liability", draft.lower())
        self.assertIn("indemnif", draft.lower())
        self.assertLessEqual(len(draft.split()), cover_note_pass.COVER_NOTE_WORD_CAP)
        self.assertFalse(body["cached"])
        self.assertGreater(body["cost_usd_cents"], 0)

        # The above only proves the response text happens to contain these
        # words -- `_KNOWN_EDITS_DRAFT` is a constant `FakeCoverNoteModelClient`
        # would return regardless of what prompt the route actually built.
        # Assert on the RECORDED INVOKE KWARGS instead (mirrors the #491
        # preflight route test's own shape, tests/test_preflight_491.py) so a
        # route that sent an empty digest, the wrong field names, or another
        # review's issues would fail this test even though the fake's
        # canned response still "passes" the substring checks above.
        self.assertEqual(len(fake_client.invocations), 1)
        invocation = fake_client.invocations[0]
        self.assertEqual(
            invocation["model_id"], model_client.openrouter_cover_note_model_id()
        )
        user_prompt = invocation["user_prompt"]
        for issue in KNOWN_ISSUES:
            self.assertIn(issue["counterparty_change_summary"], user_prompt)
            self.assertIn(issue["external_rationale_for_footnote"], user_prompt)
            self.assertNotIn(issue["proposed_replacement_text"], user_prompt)
            if issue["internal_precedent_citation"]:
                self.assertNotIn(issue["internal_precedent_citation"], user_prompt)

        self.assertEqual(
            body["served_model_id"], model_client.openrouter_cover_note_model_id()
        )

        # Cached onto the row.
        row = self._reviews_table().items["review-butter-1"]
        self.assertEqual(row["cover_note_draft"], draft)
        self.assertGreater(row["cover_note_cost_usd_cents"], 0)

        # A real spend-ledger row per generation (issue #499 AC).
        self.assertEqual(len(self.ledger_table.items), 1)
        ledger_row = self.ledger_table.items[0]
        self.assertEqual(ledger_row["pass_name"], "cover_note")
        self.assertEqual(
            ledger_row["model_id"], model_client.openrouter_cover_note_model_id()
        )
        self.assertEqual(ledger_row["review_id"], "review-butter-1")

        # Daily spend aggregate settled too.
        self.assertEqual(
            self._daily_spend_row().get("settled_usd_cents"), body["cost_usd_cents"]
        )

    def test_greeting_signoff_and_legal_promise_language_are_stripped(self):
        """Guardrail proof end to end, not just in the unit-level
        cover_note_pass tests: a model that ignores the system prompt and
        emits a greeting, a signature, and an overreaching promise must
        still come out clean on the wire."""
        self._seed_done_review("review-butter-guard", "owner-guard")
        raw = (
            "Dear Sir or Madam,\n"
            "Attached is our markup of the agreement.\n"
            "- We restored the liability cap.\n"
            "We guarantee this resolves the matter permanently.\n"
            "Please let us know if you would like to discuss.\n"
            "Best regards,\n"
            "[Your Name]"
        )
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [raw],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 900, "output_tokens": 60},
            )
        )

        resp = self._butter("owner-guard", "review-butter-guard")

        self.assertEqual(resp.status_code, 200)
        draft = resp.json()["draft"]
        self.assertNotIn("Dear", draft)
        self.assertNotIn("Best regards", draft)
        self.assertNotIn("[Your Name]", draft)
        self.assertNotIn("guarantee", draft.lower())


# -- (2) caching + regenerate -------------------------------------------------


class TestCachingAndRegenerate(CoverNoteRouteTestBase):
    def test_revisit_without_regenerate_is_served_from_cache_for_free(self):
        self._seed_done_review("review-cache-1", "owner-cache-1")
        fake_client = FakeCoverNoteModelClient(
            [_KNOWN_EDITS_DRAFT],
            served_model=model_client.openrouter_cover_note_model_id(),
            usage={"input_tokens": 50_000, "output_tokens": 300},
        )
        self._set_cover_note_client(fake_client)

        first = self._butter("owner-cache-1", "review-cache-1")
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["cached"])

        second = self._butter("owner-cache-1", "review-cache-1")
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body["cached"])
        self.assertEqual(body["cost_usd_cents"], 0)
        self.assertEqual(body["draft"], first.json()["draft"])

        # No second model call, no second ledger row, no additional spend.
        self.assertEqual(len(fake_client.invocations), 1)
        self.assertEqual(len(self.ledger_table.items), 1)
        self.assertEqual(
            self._daily_spend_row().get("settled_usd_cents"), first.json()["cost_usd_cents"]
        )

    def test_regenerate_forces_a_fresh_call_and_a_new_ledger_row(self):
        self._seed_done_review("review-regen-1", "owner-regen-1")
        second_draft = (
            "Attached is our updated markup. The substantive changes remain the "
            "liability cap and the mutual indemnification obligation. Happy to "
            "discuss."
        )
        fake_client = FakeCoverNoteModelClient(
            [_KNOWN_EDITS_DRAFT, second_draft],
            served_model=model_client.openrouter_cover_note_model_id(),
            usage={"input_tokens": 50_000, "output_tokens": 300},
        )
        self._set_cover_note_client(fake_client)

        first = self._butter("owner-regen-1", "review-regen-1")
        self.assertEqual(first.status_code, 200)

        second = self._butter("owner-regen-1", "review-regen-1", regenerate=True)
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertFalse(body["cached"])
        self.assertGreater(body["cost_usd_cents"], 0)
        self.assertNotEqual(body["draft"], first.json()["draft"])

        self.assertEqual(len(fake_client.invocations), 2)
        self.assertEqual(len(self.ledger_table.items), 2)

        row = self._reviews_table().items["review-regen-1"]
        self.assertEqual(row["cover_note_draft"], body["draft"])


# -- (3) auth + status gating + model pin -------------------------------------


class TestAuthAndStatusGating(CoverNoteRouteTestBase):
    def test_missing_review_is_404(self):
        resp = self._butter("someone", "no-such-review")
        self.assertEqual(resp.status_code, 404)

    def test_non_owner_non_admin_is_403(self):
        self._seed_done_review("review-403", "owner-real")
        resp = self._butter("someone-else", "review-403")
        self.assertEqual(resp.status_code, 403)

    def test_admin_may_butter_someone_elses_review(self):
        self._seed_done_review("review-admin-ok", "owner-real")
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )
        self._authenticate_as("admin-sub", is_admin=True)
        resp = self.client.post("/api/reviews/review-admin-ok/cover-note", json={})
        self.assertEqual(resp.status_code, 200)

    def test_non_done_review_is_409(self):
        self._seed_done_review("review-running", "owner-running", status="RUNNING")
        resp = self._butter("owner-running", "review-running")
        self.assertEqual(resp.status_code, 409)

    def test_no_issues_is_409(self):
        self._seed_done_review(
            "review-accept", "owner-accept", issues=[], decision="ACCEPT"
        )
        resp = self._butter("owner-accept", "review-accept")
        self.assertEqual(resp.status_code, 409)

    def test_served_model_is_the_cover_note_pin_never_primary_or_critic(self):
        pinned_cover_note = model_client.openrouter_cover_note_model_id()
        pinned_primary = model_client.openrouter_primary_model_id()
        pinned_critic = model_client.openrouter_critic_model_id()
        self.assertNotEqual(pinned_cover_note, pinned_primary)
        self.assertNotEqual(pinned_cover_note, pinned_critic)

        self._seed_done_review("review-pin", "owner-pin")
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=pinned_cover_note,
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )
        resp = self._butter("owner-pin", "review-pin")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["served_model_id"], pinned_cover_note)
        self.assertNotEqual(body["served_model_id"], pinned_primary)
        self.assertNotEqual(body["served_model_id"], pinned_critic)

        ledger_row = self.ledger_table.items[0]
        self.assertEqual(ledger_row["model_id"], pinned_cover_note)


# -- Issue #499 fix round 3 (review finding): a purged review is refused ----
#
# Neither purge implementation clears `issues`/`status` (only
# verdict_summary/issue_rationale_text/original_filename/normalization_notes/
# attorney_disposition_note/cover_note_draft -- see retention.py's own REMOVE
# clause), so a purged review previously still read status=DONE with `issues`
# intact and this route had no gate of its own: an owner/admin could keep
# generating fresh, billed drafts from a review the retention policy believed
# it had erased. The route now reuses retention._is_past_retention /
# _is_legal_held -- the EXACT predicate the purge sweep itself runs -- so it
# can never drift from what the sweep actually purges.
# -----------------------------------------------------------------------


class TestRetentionGating(CoverNoteRouteTestBase):
    def test_a_review_past_its_retention_window_is_refused(self):
        self._seed_done_review(
            "review-past-retention",
            "owner-past-retention",
            retention_window_at_creation=90,
        )
        resp = self._butter("owner-past-retention", "review-past-retention")
        self.assertEqual(resp.status_code, 409)

        # No spend, no ledger row, no draft -- refused before any model call.
        row = self._reviews_table().items["review-past-retention"]
        self.assertNotIn("cover_note_draft", row)
        self.assertEqual(len(self.ledger_table.items), 0)

    def test_a_legal_held_review_past_its_window_is_still_butterable(self):
        """Legal hold overrides purge eligibility for every substance field
        (docs/data-handling.md) -- it must override this gate too, the same
        way it overrides the sweep itself."""
        self._seed_done_review(
            "review-held-past-window",
            "owner-held",
            retention_window_at_creation=90,
            legal_hold=True,
        )
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )
        resp = self._butter("owner-held", "review-held-past-window")
        self.assertEqual(resp.status_code, 200)

    def test_a_review_within_its_retention_window_is_unaffected(self):
        self._seed_done_review(
            "review-fresh",
            "owner-fresh",
            retention_window_at_creation=retention_module.RETENTION_WINDOW_FOREVER,
        )
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )
        resp = self._butter("owner-fresh", "review-fresh")
        self.assertEqual(resp.status_code, 200)

    def test_a_malformed_created_at_fails_closed_not_500(self):
        """`_is_past_retention` does `float(review.get("created_at", ...))` --
        a corrupted row must degrade to a refusal (409), never an unhandled
        500 (review finding: the gate itself had no guard against this).

        `retention_window_at_creation` must be a real number, not "forever"
        -- `_is_past_retention` short-circuits to `False` for the forever
        sentinel BEFORE ever touching `created_at`, which would never
        exercise the `float()` call this test targets."""
        self._seed_done_review(
            "review-corrupt-created-at", "owner-corrupt", retention_window_at_creation=90
        )
        self._reviews_table().items["review-corrupt-created-at"]["created_at"] = (
            "not-a-number"
        )
        resp = self._butter("owner-corrupt", "review-corrupt-created-at")
        self.assertEqual(resp.status_code, 409)

    def test_a_row_carrying_purged_at_is_refused_even_when_the_prediction_would_allow_it(
        self,
    ):
        """`purged_at` (stamped by both purge implementations -- see
        tests/test_summary_attribute_roundtrip.py::TestPurgedAtMarker) is a
        definitive signal: this row demonstrably WAS purged. It must win
        even in the realistic case where the fallback PREDICTION
        (`retention._is_past_retention`) would now say "not past retention"
        -- e.g. the window was raised back up after a purge already ran
        under a shorter one."""
        self._seed_done_review(
            "review-purged-but-fresh",
            "owner-purged-but-fresh",
            retention_window_at_creation=90,
        )
        row = self._reviews_table().items["review-purged-but-fresh"]
        row["created_at"] = str(int(time.time()))
        row["purged_at"] = str(int(time.time()))
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )

        resp = self._butter("owner-purged-but-fresh", "review-purged-but-fresh")

        self.assertEqual(resp.status_code, 409)

    def test_purged_at_short_circuits_before_any_retention_arithmetic(self):
        """purged_at is checked FIRST, with NO timestamp arithmetic at all --
        a corrupt `created_at` on an already-purged row must never even
        reach `_is_past_retention`. Patches `_is_past_retention` to explode
        if called at all, proving the gate never gets there once
        `purged_at` is present."""
        self._seed_done_review(
            "review-purged-marker",
            "owner-purged-marker",
            retention_window_at_creation=90,
        )
        row = self._reviews_table().items["review-purged-marker"]
        row["created_at"] = "not-a-number"
        row["purged_at"] = "1700000000"

        with patch.object(
            retention_module,
            "_is_past_retention",
            side_effect=AssertionError(
                "_is_past_retention must not be called once purged_at is set"
            ),
        ):
            resp = self._butter("owner-purged-marker", "review-purged-marker")

        self.assertEqual(resp.status_code, 409)


# -- (4) failure degrades quietly and never destroys a good cached draft -----


class TestFailureDegradesQuietly(CoverNoteRouteTestBase):
    def test_no_model_client_available_is_a_quiet_502_no_side_effects(self):
        self._seed_done_review("review-no-client", "owner-no-client")
        resp = self._butter("owner-no-client", "review-no-client")
        self.assertEqual(resp.status_code, 502)

        row = self._reviews_table().items["review-no-client"]
        self.assertNotIn("cover_note_draft", row)
        self.assertEqual(len(self.ledger_table.items), 0)
        self.assertEqual(self._daily_spend_row(), {})

    def test_generation_exception_is_a_quiet_502_no_side_effects(self):
        self._seed_done_review("review-raises", "owner-raises")
        self._set_cover_note_client(
            FakeCoverNoteModelClient([RuntimeError("simulated provider outage")])
        )
        resp = self._butter("owner-raises", "review-raises")
        self.assertEqual(resp.status_code, 502)

        row = self._reviews_table().items["review-raises"]
        self.assertNotIn("cover_note_draft", row)
        self.assertEqual(len(self.ledger_table.items), 0)

    def test_failed_regenerate_leaves_the_previously_cached_draft_untouched(self):
        self._seed_done_review(
            "review-keep-cache",
            "owner-keep-cache",
            cover_note_draft="A previously generated, perfectly good draft.",
        )
        self._set_cover_note_client(
            FakeCoverNoteModelClient([RuntimeError("simulated provider outage")])
        )

        resp = self._butter(
            "owner-keep-cache", "review-keep-cache", regenerate=True
        )
        self.assertEqual(resp.status_code, 502)

        row = self._reviews_table().items["review-keep-cache"]
        self.assertEqual(
            row["cover_note_draft"], "A previously generated, perfectly good draft."
        )
        # The failed attempt is never ledgered or billed either -- nothing
        # was actually spent.
        self.assertEqual(len(self.ledger_table.items), 0)


# -- Issue #499 fix round 3 (review finding): the DEFAULT corpus resolver ---
# is OPF-aware
#
# `_default_cover_note_leakage_corpus` previously resolved the corpus via
# `playbook_registry.resolve_playbook(playbook_id)` -> the registry's static
# on-disk `playbook_path` -> `ConfidentialCorpus.from_playbook(...)`, with no
# check for an activated OPF bundle -- reopening issue #479 (an OPF bundle
# carries neither `topics` nor `hard_rejections`, so `from_playbook` on one
# silently yields an EMPTY, never-blocking corpus) for this one route. The
# fix reuses `pipeline_runner._load_playbook_bundle` (the same resolver
# `scripts/review_spine.py` uses for the main pipeline) so the two can never
# drift. Patches `pipeline_runner._load_playbook_bundle` directly (the
# established convention -- see tests/test_review_failure_reason_442.py,
# tests/test_runtime_playbook_loading.py) rather than standing up a full
# moto-backed upload+activate flow: what's under test here is the corpus
# resolver's OWN branching logic, not the activation flow itself (that is
# tests/test_review_opf_digest_mode_479.py's job).


class TestDefaultLeakageCorpusIsOpfAware(CoverNoteRouteTestBase):
    _PLANTED_POSTURE_PHRASE = "internal strategy: concede indemnity only after liability holds"

    def test_an_opf_governed_reviews_draft_is_scanned_against_the_opf_document(self):
        """Proves the fix: given an ACTIVE OPF bundle for this playbook_id,
        a draft reproducing that bundle's posture prose is blocked -- using
        the REAL default resolver (`get_cover_note_leakage_corpus_resolver`
        is NOT overridden here), not a hand-built test double."""
        self._seed_done_review(
            "review-opf-leak", "owner-opf-leak", playbook_id="synthetic-opf-playbook"
        )
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [
                    "Attached is our markup. Per "
                    f"{self._PLANTED_POSTURE_PHRASE}, we held the line here."
                ]
            )
        )

        with patch.object(
            review_routes.pipeline_runner,
            "_load_playbook_bundle",
            return_value={
                "opf_bundle_v2": {
                    "opf": {"posture": {"system_prompt": self._PLANTED_POSTURE_PHRASE}},
                    "overrides": None,
                },
                "playbook": {"metadata": {}},
            },
        ):
            resp = self._butter("owner-opf-leak", "review-opf-leak")

        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(self._PLANTED_POSTURE_PHRASE, resp.text)
        row = self._reviews_table().items["review-opf-leak"]
        self.assertNotIn("cover_note_draft", row)

    def test_the_same_bundle_read_via_from_playbook_would_have_missed_it(self):
        """Documents the exact bug this fix closes: the SAME OPF-shaped
        bundle, run through the pre-fix `from_playbook` path instead of
        `from_opf_document`, produces an EMPTY corpus -- the planted posture
        phrase is NOT `topics`/`hard_rejections` shaped, so it is invisible
        to `from_playbook`."""
        opf_bundle = {
            "opf_bundle_v2": {
                "opf": {"posture": {"system_prompt": self._PLANTED_POSTURE_PHRASE}},
                "overrides": None,
            },
            "playbook": {"metadata": {}},
        }
        pre_fix_corpus = leakage_scan.ConfidentialCorpus.from_playbook(opf_bundle)
        scan_result = leakage_scan.LeakageScanner(pre_fix_corpus).scan(
            f"Per {self._PLANTED_POSTURE_PHRASE}, we held the line here.",
            field_name="cover_note_draft",
        )
        self.assertFalse(
            scan_result.blocked,
            "from_playbook on an OPF-shaped bundle should miss the planted "
            "term -- if this now fails, from_playbook's behavior changed "
            "and the OPF-awareness fix above may no longer be necessary.",
        )

    def test_a_v1_playbook_id_with_no_active_opf_bundle_is_unaffected(self):
        """`_load_playbook_bundle` falls through to the registry disk read
        exactly as before when no OPF bundle is active -- byte-identical to
        pre-fix behavior for every v1 playbook_id."""
        self._seed_done_review(
            "review-v1-unaffected", "owner-v1", playbook_id="does-not-exist-in-registry"
        )
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )
        # No patch on `_load_playbook_bundle`: an unregistered playbook_id
        # raises inside the real registry lookup, caught by
        # `_default_cover_note_leakage_corpus`'s own fail-open `except`,
        # degrading to an empty corpus -- same as the pre-fix behavior for
        # this exact case.
        resp = self._butter("owner-v1", "review-v1-unaffected")
        self.assertEqual(resp.status_code, 200)


# -- Issue #499 fix round 2, finding 1/3: the leakage scan gates the draft ---
#
# The route previously had NO leakage-scan call anywhere between
# `cover_note_pass.sanitize_cover_note_text` and either
# `reviews.record_cover_note_draft` or the 200 response -- `sanitize_cover_
# note_text` is a greeting/sign-off/promise/word-cap text filter and does no
# corpus matching at all. These tests plant a corpus n-gram in a
# `FakeCoverNoteModelClient`'s seeded response and assert the route fails
# closed: no 200 with the planted text, and no `cover_note_draft` written to
# the row. This MUST fail against the pre-fix-round-2 tree (a planted term
# sailed through as a 200, verbatim, and got persisted).
#
# Overrides `get_cover_note_leakage_corpus_resolver` (rather than engineering
# a real on-disk playbook fixture to carry a planted n-gram) with a resolver
# that hands back a small `ConfidentialCorpus` carrying a planted
# hard-rejection-style rule id and a planted precedent-counterparty name --
# the two categories `LeakageScanner.scan` checks via `playbook_ngrams` and
# `counterparty_names` respectively.


class TestLeakageScanGatesTheDraft(CoverNoteRouteTestBase):
    _PLANTED_RULE_ID = "PB-LIAB-007"
    _PLANTED_COUNTERPARTY = "Riverside Community College"

    def setUp(self) -> None:
        super().setUp()
        planted_corpus = leakage_scan.ConfidentialCorpus(
            playbook_ngrams=[self._PLANTED_RULE_ID],
            counterparty_names=[self._PLANTED_COUNTERPARTY],
        )
        self.app.dependency_overrides[
            review_routes.get_cover_note_leakage_corpus_resolver
        ] = lambda: (lambda playbook_id: planted_corpus)

    def test_a_planted_playbook_rule_id_fails_closed(self):
        self._seed_done_review("review-leak-rule", "owner-leak-rule")
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [
                    "Attached is our markup of the agreement. The change "
                    f"follows internal rule {self._PLANTED_RULE_ID} on liability."
                ]
            )
        )

        resp = self._butter("owner-leak-rule", "review-leak-rule")

        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(self._PLANTED_RULE_ID, resp.text)

        row = self._reviews_table().items["review-leak-rule"]
        self.assertNotIn("cover_note_draft", row)
        self.assertEqual(len(self.ledger_table.items), 0)
        self.assertEqual(self._daily_spend_row(), {})

    def test_a_planted_precedent_counterparty_name_fails_closed(self):
        self._seed_done_review("review-leak-name", "owner-leak-name")
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [
                    "Attached is our markup of the agreement. As we agreed "
                    f"with {self._PLANTED_COUNTERPARTY}, we restored the cap."
                ]
            )
        )

        resp = self._butter("owner-leak-name", "review-leak-name")

        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(self._PLANTED_COUNTERPARTY, resp.text)

        row = self._reviews_table().items["review-leak-name"]
        self.assertNotIn("cover_note_draft", row)
        self.assertEqual(len(self.ledger_table.items), 0)

    def test_a_previously_cached_clean_draft_is_untouched_by_a_blocked_regenerate(self):
        # Same "failed attempt leaves the cache alone" contract the generic
        # failure-degrades-quietly tests assert, specifically for a leakage
        # block rather than a provider exception.
        self._seed_done_review(
            "review-leak-keep-cache",
            "owner-leak-keep-cache",
            cover_note_draft="A previously generated, perfectly good draft.",
        )
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [f"This references {self._PLANTED_RULE_ID} directly."]
            )
        )

        resp = self._butter(
            "owner-leak-keep-cache", "review-leak-keep-cache", regenerate=True
        )

        self.assertEqual(resp.status_code, 502)
        row = self._reviews_table().items["review-leak-keep-cache"]
        self.assertEqual(
            row["cover_note_draft"], "A previously generated, perfectly good draft."
        )

    def test_a_clean_draft_is_unaffected_by_the_scan(self):
        # Sanity check the override does not over-block: a draft that
        # never mentions either planted term still gets through as a 200
        # and is persisted, same as every other clean-draft test above.
        self._seed_done_review("review-leak-clean", "owner-leak-clean")
        self._set_cover_note_client(FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT]))

        resp = self._butter("owner-leak-clean", "review-leak-clean")

        self.assertEqual(resp.status_code, 200)
        row = self._reviews_table().items["review-leak-clean"]
        self.assertIn("cover_note_draft", row)


# -- (5) no send capability anywhere in this response shape ------------------


class TestNoSendCapability(CoverNoteRouteTestBase):
    def test_response_carries_no_signature_or_send_affordance(self):
        self._seed_done_review("review-no-send", "owner-no-send")
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )
        resp = self._butter("owner-no-send", "review-no-send")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(
            set(body.keys()),
            {
                "review_id",
                "draft",
                "cost_usd_cents",
                "cached",
                "generated_at",
                "served_model_id",
            },
        )


# -- post-landing review round 4, finding 1: cover-note spend is unbounded --
#
# `record_cover_note_spend` only ever SETTLES (never reserves), so cover-note
# spend never enters `reserved_usd_cents` -- `reserve_spend`'s own
# conditional cap check reads ONLY that attribute and never sees a cover-note
# dollar. Unlike preflight (advisory, never blocks a submission, fires on
# every file selection including abandoned ones -- see
# `reviews.record_preflight_spend`'s module comment), a cover note is a
# user-initiated, explicitly priced, repeatable action with no rate limit
# anywhere on the route: an authenticated owner could loop
# `{"regenerate": true}` forever. The fix is a pre-flight refusal (429) keyed
# on the day's TOTAL committed spend (`reserved_usd_cents +
# settled_usd_cents`) reaching the cap, checked before the model is ever
# invoked -- `record_cover_note_spend` itself stays settle-only.
# -----------------------------------------------------------------------


class TestDailySpendCapRefusesGeneration(CoverNoteRouteTestBase):
    def _seed_daily_spend(
        self, *, reserved: int = 0, settled: int = 0, cap: int | None = None
    ) -> None:
        spend_date = time.strftime("%Y-%m-%d", time.gmtime())
        row: dict[str, Any] = {
            "spend_date": spend_date,
            "reserved_usd_cents": reserved,
            "settled_usd_cents": settled,
        }
        if cap is not None:
            row["daily_cap_usd_cents"] = cap
        self.ddb.Table(os.environ["DAILY_SPEND_TABLE"]).items[spend_date] = row

    def test_cap_already_reached_via_reservations_refuses_with_429_before_any_model_call(
        self,
    ):
        self._seed_daily_spend(reserved=reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT)
        self._seed_done_review("review-cap-reserved", "owner-cap-reserved")
        fake_client = FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        self._set_cover_note_client(fake_client)

        resp = self._butter("owner-cap-reserved", "review-cap-reserved")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(len(fake_client.invocations), 0)
        row = self._reviews_table().items["review-cap-reserved"]
        self.assertNotIn("cover_note_draft", row)
        self.assertEqual(len(self.ledger_table.items), 0)

    def test_cap_reached_via_settled_spend_alone_still_refuses(self):
        """The bug's specific shape: nothing anywhere previously read
        `settled_usd_cents` in a condition, so a day whose entire committed
        spend sits in `settled_usd_cents` (e.g. from preflight calls, or
        from a prior cover-note generation settled with no reservation)
        never tripped any existing cap check at all."""
        self._seed_daily_spend(settled=reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT)
        self._seed_done_review("review-cap-settled", "owner-cap-settled")
        fake_client = FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        self._set_cover_note_client(fake_client)

        resp = self._butter("owner-cap-settled", "review-cap-settled")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(len(fake_client.invocations), 0)

    def test_cap_reached_via_split_reserved_and_settled_still_refuses(self):
        half = reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT // 2
        self._seed_daily_spend(reserved=half, settled=half)
        self._seed_done_review("review-cap-split", "owner-cap-split")
        fake_client = FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        self._set_cover_note_client(fake_client)

        resp = self._butter("owner-cap-split", "review-cap-split")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(len(fake_client.invocations), 0)

    def test_cap_not_yet_reached_is_unaffected(self):
        self._seed_daily_spend(
            reserved=reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT - 100
        )
        self._seed_done_review("review-cap-headroom", "owner-cap-headroom")
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [_KNOWN_EDITS_DRAFT],
                served_model=model_client.openrouter_cover_note_model_id(),
                usage={"input_tokens": 50_000, "output_tokens": 300},
            )
        )

        resp = self._butter("owner-cap-headroom", "review-cap-headroom")

        self.assertEqual(resp.status_code, 200)

    def test_a_cached_draft_is_still_served_free_even_when_the_cap_is_reached(self):
        """The cap guards the PAID path only -- a revisit that never calls
        the model (no `regenerate`, a draft already cached) costs nothing
        and must not be refused just because unrelated spend elsewhere
        today has exhausted the cap."""
        self._seed_daily_spend(reserved=reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT)
        self._seed_done_review(
            "review-cap-cached",
            "owner-cap-cached",
            cover_note_draft="A previously generated, perfectly good draft.",
        )

        resp = self._butter("owner-cap-cached", "review-cap-cached")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["cached"])

    def test_regenerate_is_refused_once_the_cap_is_reached_even_with_a_cached_draft(self):
        self._seed_daily_spend(reserved=reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT)
        self._seed_done_review(
            "review-cap-regen",
            "owner-cap-regen",
            cover_note_draft="A previously generated, perfectly good draft.",
        )
        fake_client = FakeCoverNoteModelClient([_KNOWN_EDITS_DRAFT])
        self._set_cover_note_client(fake_client)

        resp = self._butter(
            "owner-cap-regen", "review-cap-regen", regenerate=True
        )

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(len(fake_client.invocations), 0)
        row = self._reviews_table().items["review-cap-regen"]
        self.assertEqual(
            row["cover_note_draft"], "A previously generated, perfectly good draft."
        )


# -- post-landing review round 4, finding 2: a transient audit failure turns
# a quiet 502 into an unhandled 500
#
# `_write_audit_row`'s `table.put_item(...)` was completely unguarded. Its
# only defensive branch was a missing `AUDIT_TABLE` env var ("best-effort:
# never gate the request itself on audit-table config") -- a DynamoDB
# throttle/outage DURING a leakage block raised out of the
# `except leakage_scan.LeakageDetectedError` clause uncaught (Python does
# not route an exception raised inside one `except` clause to a sibling
# `except Exception` of the same `try`), turning the route's promised quiet
# 502 into an unhandled 500 -- exactly the scary red-banner path
# `frontend/src/coverNote.ts` special-cases 502 to avoid. The fix guards the
# `put_item` inside `_write_audit_row` itself (covering all six call sites,
# not just this one) with a warning log as the durable record of the
# failure.
# -----------------------------------------------------------------------


class _RaisingAuditTable:
    """Stands in for a DynamoDB audit table mid-throttle/outage: every
    `put_item` raises, exactly the failure mode `_write_audit_row` never
    guarded against."""

    def put_item(self, Item: dict[str, Any]) -> None:
        raise RuntimeError("simulated audit-table put_item failure")


class _AuditFailingDynamoDBResource:
    """Routes `AUDIT_TABLE` to `_RaisingAuditTable`; every other table name
    delegates to the underlying fake unchanged -- same routing convention
    `_RoutingDynamoDBResource` above uses for the ledger table."""

    def __init__(self, base: Any) -> None:
        self._base = base
        self._audit_table_name = os.environ["AUDIT_TABLE"]

    def Table(self, name: str) -> Any:
        if name == self._audit_table_name:
            return _RaisingAuditTable()
        return self._base.Table(name)


class TestAuditWriteFailureDoesNotBreakTheResponse(CoverNoteRouteTestBase):
    _PLANTED_RULE_ID = "PB-LIAB-007"

    def setUp(self) -> None:
        super().setUp()
        # A non-raising TestClient: the bug this covers is an UNHANDLED
        # exception escaping the route, which the default TestClient
        # (raise_server_exceptions=True) would re-raise into the test
        # itself rather than deliver as a 500 response. Asserting the
        # actual status code (500 pre-fix, 502 post-fix) requires seeing
        # the response the way a real deployed server would produce it.
        self.client = TestClient(self.app, raise_server_exceptions=False)
        planted_corpus = leakage_scan.ConfidentialCorpus(
            playbook_ngrams=[self._PLANTED_RULE_ID], counterparty_names=[]
        )
        self.app.dependency_overrides[
            review_routes.get_cover_note_leakage_corpus_resolver
        ] = lambda: (lambda playbook_id: planted_corpus)
        self.app.dependency_overrides[review_routes.get_dynamodb_resource] = (
            lambda: _AuditFailingDynamoDBResource(self._routing_ddb)
        )

    def test_a_leakage_block_survives_an_audit_write_failure_as_a_quiet_502(self):
        self._seed_done_review("review-audit-fail", "owner-audit-fail")
        self._set_cover_note_client(
            FakeCoverNoteModelClient(
                [
                    "Attached is our markup of the agreement. The change "
                    f"follows internal rule {self._PLANTED_RULE_ID} on liability."
                ]
            )
        )

        resp = self._butter("owner-audit-fail", "review-audit-fail")

        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(self._PLANTED_RULE_ID, resp.text)


# -- pure cover_note_pass unit coverage (word cap + digest bound) -----------


class TestCoverNotePassPureFunctions(unittest.TestCase):
    def test_word_cap_truncates_at_a_sentence_boundary(self):
        sentences = [f"This is sentence number {i}." for i in range(40)]
        long_text = " ".join(sentences)
        self.assertGreater(len(long_text.split()), cover_note_pass.COVER_NOTE_WORD_CAP)

        capped = cover_note_pass.sanitize_cover_note_text(long_text)

        self.assertLessEqual(len(capped.split()), cover_note_pass.COVER_NOTE_WORD_CAP)
        # Cut at a sentence boundary -- ends on a period, not mid-clause.
        self.assertTrue(capped.endswith("."))

    def test_a_single_sentence_longer_than_the_cap_is_hard_truncated(self):
        long_sentence = " ".join(f"word{i}" for i in range(200)) + "."
        capped = cover_note_pass.sanitize_cover_note_text(long_sentence)
        self.assertEqual(len(capped.split()), cover_note_pass.COVER_NOTE_WORD_CAP)

    def test_bullet_lines_survive_sanitizing_intact(self):
        """`_strip_legal_promise_sentences` and `_cap_to_word_limit` must
        preserve line structure (issue #499 fix round 1): `_SENTENCE_SPLIT_RE`
        consumes whitespace -- including newlines -- between sentences, so
        filtering across the WHOLE text and rejoining with a single space
        collapses the Design/AC1-required 3-6 bullet-line structure into one
        run-on paragraph. A compliant model response with three bullet
        lines, one of which trips the legal-promise filter and is dropped
        entirely, must still reach the wire as separate lines -- with the
        render sites' `white-space: pre-wrap` actually doing something."""
        raw = (
            "Attached is our markup of the agreement.\n"
            "- We restored a cap on liability.\n"
            "- We guarantee this resolves the matter permanently.\n"
            "- We made the indemnification obligation mutual.\n"
            "Happy to discuss further."
        )
        sanitized = cover_note_pass.sanitize_cover_note_text(raw)
        lines = sanitized.split("\n")

        # The legal-promise line is gone entirely, not just its wording --
        # and it did not drag its neighboring bullets into one paragraph.
        self.assertNotIn("guarantee", sanitized.lower())
        self.assertIn("- We restored a cap on liability.", lines)
        self.assertIn("- We made the indemnification obligation mutual.", lines)
        # Still separate lines -- the fix under test is exactly this: no
        # single line holds more than one of the three original sentences
        # glued together with " - ".
        self.assertEqual(
            sum(1 for line in lines if line.strip().startswith("-")), 2
        )

    def test_digest_is_bounded_to_max_issues_and_excludes_internal_fields(self):
        many_issues = [
            {
                "section_title": f"Section {i}",
                "counterparty_change_summary": f"Change {i}.",
                "external_rationale_for_footnote": f"Rationale {i}.",
                "proposed_replacement_text": f"SECRET_REPLACEMENT_TEXT_{i}",
                "internal_precedent_citation": f"SECRET_PRECEDENT_{i}",
                "playbook_topic_id": f"secret-topic-{i}",
            }
            for i in range(20)
        ]
        digest = cover_note_pass.build_edit_digest(many_issues)
        self.assertEqual(len(digest), cover_note_pass.MAX_ISSUES_IN_DIGEST)

        prompt = cover_note_pass.render_cover_note_user_prompt(many_issues)
        self.assertNotIn("SECRET_REPLACEMENT_TEXT", prompt)
        self.assertNotIn("SECRET_PRECEDENT", prompt)
        self.assertNotIn("secret-topic", prompt)

    def test_an_issue_with_no_summary_or_rationale_is_skipped(self):
        digest = cover_note_pass.build_edit_digest(
            [{"section_title": "Empty section"}, "not-a-dict"]
        )
        self.assertEqual(digest, [])

    def test_skippable_entries_do_not_count_against_the_cap(self):
        """Post-#499-landing review fix: MAX_ISSUES_IN_DIGEST must bound
        the number of SUMMARIZABLE issues, not raw slice position. 13
        skippable entries (each missing both `counterparty_change_summary`
        and `external_rationale_for_footnote`) followed by one real issue
        must still surface that real issue -- the pre-fix
        `issues[:MAX_ISSUES_IN_DIGEST]` slice truncates BEFORE the
        skip-filter ever runs, so 12+ skippable entries alone silently
        swallow every real issue behind them, and the route ends up
        sending the model (and billing the reviewer for) a prompt whose
        digest body is literally '(no substantive edits recorded)' on a
        review that already passed its own `if not issues: 409` gate."""
        skippable = [{"section_title": f"Skip {i}"} for i in range(13)]
        real_issue = {
            "section_title": "Indemnification",
            "counterparty_change_summary": "Indemnification obligation made mutual.",
            "external_rationale_for_footnote": "Playbook fallback position.",
        }
        issues = skippable + [real_issue]

        digest = cover_note_pass.build_edit_digest(issues)

        self.assertEqual(len(digest), 1)
        self.assertEqual(
            digest[0]["summary"], real_issue["counterparty_change_summary"]
        )

        prompt = cover_note_pass.render_cover_note_user_prompt(issues)
        self.assertNotIn("no substantive edits recorded", prompt)
        self.assertIn("Indemnification obligation made mutual.", prompt)

    def test_mid_draft_signoff_lookalike_does_not_truncate_the_rest(self):
        """Post-#499-landing review fix: `_strip_greeting_and_signoff`
        must cut only the TRAILING contiguous run of sign-off/placeholder
        lines, not everything from the FIRST such line onward (the
        docstring says "first trailing", which is self-contradictory --
        the code followed the "first" half). A standalone mid-draft line
        that happens to match `_SIGNOFF_LINE_RE` (a bare "Thanks." on its
        own line, here describing a business fact partway through the
        note, not a signature) must not swallow the real closing
        "offer to discuss" line the system prompt requires."""
        raw = (
            "Attached is our markup of the agreement.\n"
            "- We restored the liability cap.\n"
            "Thanks.\n"
            "- We also revised the indemnification clause.\n"
            "Please let us know if you would like to discuss."
        )
        stripped = cover_note_pass._strip_greeting_and_signoff(raw)
        self.assertIn("We also revised the indemnification clause.", stripped)
        self.assertIn(
            "Please let us know if you would like to discuss.", stripped
        )


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
