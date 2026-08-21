#!/usr/bin/env python3
"""
Slice test for issue #449: the History surface and the provenance it needs.

## Root problem this proves fixed

A user could not answer "what did you toast for me last week, and how?".
`GET /api/reviews` existed with no UI, and — more importantly — the record it
reads from could not answer the provenance question even if a UI had existed:

  * **The models were never recorded.** `pipeline_runner._bundle_with_openrouter_
    model_ids` computed the primary/critic model ids for a run and handed them
    to the spine, but nothing wrote them to the reviews row. "Which model
    reviewed this contract?" was unanswerable for every past review.
  * **The input document was not identifiable from the review row.** The
    upload pointer lived only on the `review_submissions` row and inside the
    execution-input JSON; `_create_review_row` never stored it (the ticket's
    "`upload_s3_key` is persisted on the review row (reviews.py:1280)" points
    at `_build_execution_input_json_from_parts`, which builds the EXECUTION
    INPUT — a false lead, recorded here so the next reader does not re-chase
    it). There was likewise NO routed input download: `/api/reviews/{id}/
    download` appears in `download.py`'s docstring as a usage example and is
    not registered; the only registered download is `/output`.

## The rule this file exists to enforce

**A row that predates a provenance field renders "not recorded" — never
today's configured value.** A review run last week on a different model must
not be relabelled with the model configured now (this gets sharper the moment
an admin can change models). So the historic-row assertions below compare
against `model_client.openrouter_primary_model_id()` NEGATIVELY: the
projection must not merely be falsy, it must not be today's id.

## What this test asserts

  1. THE WRITE (real runner, not a mock). Driving the REAL
     `pipeline_runner.run_real_pipeline` over a real fixture `.docx` with a
     `FakeBedrockClient` lands `primary_model_id` / `critic_model_id` on the
     reviews row, and they are the ids the spine ACTUALLY invoked — asserted
     against the fake client's own invocation ledger, not against the
     configuration the runner read.
  2. THE PROJECTION. `get_review_detail` returns both model ids plus
     `has_input`; a row that carries none returns None for each — and, per the
     rule above, NOT today's configured id.
  3. THE LIST. `list_reviews` carries the provenance the History table needs
     (`policy_version`, `posture_version`, both model ids) plus the two
     availability booleans, and NEVER the raw S3 keys.
  4. OWNER SCOPING, ASSERTED NEGATIVELY. `list_reviews(..., owner_scoped=True)`
     and `GET /api/reviews?scope=mine` return only the caller's own rows —
     for an ADMIN too, whose default listing still sees everything (that
     documented behavior is unchanged; cross-user history is out of scope for
     this ticket).
  5. THE INPUT POINTER. A real submission through `POST /api/reviews` lands
     `upload_s3_key` on the reviews row.
  6. THE INPUT ROUTE. `GET /api/reviews/{id}/input` presigns the caller's own
     input document; a non-owner gets 403; a row with no recorded pointer gets
     404; and a pointer whose OBJECT HAS BEEN PURGED gets 410 Gone — an
     explicit "no longer available", never a presigned URL to a deleted
     object (the retention AC: a purged document must render as unavailable,
     not as a broken link). The same 410 covers a purged `/output`.
  7. THE UI IS WIRED FOR EVERYONE. `ReviewHistory.tsx` exists, and `App.tsx`
     mounts its tab and always-mounted tabpanel WITHOUT an `isAdmin` gate.

## What this test deliberately does NOT prove

That the screen renders correctly. Columns, the guidance expander, the
"not recorded" copy, the unavailable state and the loading/error exclusivity
are proven in `frontend/src/__tests__/review-history.test.tsx`.

Run standalone: `python3 tests/test_review_history_449.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC_DIR = BACKEND_ROOT / "src"

for _dir in (BACKEND_ROOT, SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

# Cross-test-file imports (established convention -- see
# tests/test_review_output_endpoint_162.py importing test_review_api_84, and
# tests/test_review_progress_stage_447.py importing
# test_dts_pipeline_runner_real_review).
#
# ORDER IS LOAD-BEARING. Both modules seed bucket/table names with
# `os.environ.setdefault` at import time and they disagree on the values, so
# whichever imports FIRST wins. #84 is imported first because its names are
# the ones the route tests below assert against (its moto buckets are created
# from them); #259's fakes ignore bucket names entirely (its FakeS3 is keyed
# on the object key alone), so it is indifferent to losing the race.
import test_review_api_84 as api84  # noqa: E402
import test_dts_pipeline_runner_real_review as dts  # noqa: E402

import model_client as model_client_module  # noqa: E402
import pipeline_runner as pr  # noqa: E402
from src import download as download_module  # noqa: E402
from src import review_routes  # noqa: E402
from src import reviews as reviews_module  # noqa: E402

REVIEW_ID = dts.REVIEW_ID

FRONTEND_DIR = REPO_ROOT / "frontend" / "src"
REVIEW_HISTORY_TSX = FRONTEND_DIR / "ReviewHistory.tsx"
APP_TSX = FRONTEND_DIR / "App.tsx"


# ---------------------------------------------------------------------------
# (1) THE WRITE -- the real runner records the models that really ran.
# ---------------------------------------------------------------------------


class TestRunnerRecordsModelProvenance(unittest.TestCase):
    """Drives the REAL `run_real_pipeline` (not a stubbed spine) so the ids
    landing on the row are the ids the review genuinely used."""

    def _run(self) -> tuple[dict[str, Any], Any]:
        docx_bytes = dts._build_draft_docx({"sec-8": dts._SEC8_DRAFT_TEXT})
        client = dts._fake_client(
            dts._primary_request_change_response(), dts._critic_no_delta_response()
        )
        reviews_table = dts.FakeReviewsTable()
        s3 = dts.FakeS3({f"uploads/user-1/{REVIEW_ID}/in.docx": docx_bytes})

        with patch.object(pr, "_settle_reservation"):
            pr.run_real_pipeline(
                REVIEW_ID,
                dts._payload(),
                dynamodb_resource=dts.FakeDDB(reviews_table),
                s3_client=s3,
                model_client=client,
            )
        return reviews_table.item, client

    def test_terminal_row_records_both_model_ids(self) -> None:
        item, _client = self._run()

        self.assertEqual(item["status"], "DONE")
        self.assertEqual(item.get("primary_model_id"), model_client_module.openrouter_primary_model_id())
        self.assertEqual(item.get("critic_model_id"), model_client_module.openrouter_critic_model_id())

    def test_recorded_ids_are_the_ones_actually_invoked(self) -> None:
        """The row must name the models the spine CALLED, not the models the
        runner happened to read out of configuration. Asserted against the
        fake client's own invocation ledger."""
        item, client = self._run()

        invoked = [call["model_id"] for call in client.calls]
        self.assertIn(item.get("primary_model_id"), invoked)
        self.assertIn(item.get("critic_model_id"), invoked)


# ---------------------------------------------------------------------------
# (2) + (3) THE PROJECTION and THE LIST -- pure functions over a fake table.
# ---------------------------------------------------------------------------


class _RowsTable:
    """Scan-only reviews table stand-in (deliberately NO `query`, so
    `_list_reviews_for_owner` takes its documented scan+filter fallback)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(r) for r in rows]

    def scan(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Items": [dict(r) for r in self.rows]}

    def get_item(self, Key):  # noqa: N803 - boto3 kwarg name
        for row in self.rows:
            if row["review_id"] == Key["review_id"]:
                return {"Item": dict(row)}
        return {}


class _RowsResource:
    def __init__(self, table: _RowsTable) -> None:
        self._table = table

    def Table(self, _name: str) -> _RowsTable:  # noqa: N802 - boto3 method name
        return self._table


OWNER = "sub-owner-449"
OTHER = "sub-other-449"
ADMIN = "sub-admin-449"

MODERN_ROW: dict[str, Any] = {
    "review_id": "rev-modern",
    "owner_sub": OWNER,
    "playbook_id": "synthetic-nda-sample",
    "status": "DONE",
    "decision": "REQUEST_CHANGE",
    "created_at": "1800000200",
    "updated_at": "1800000300",
    "policy_version": 3,
    "posture_version": 2,
    "primary_model_id": "vendor/primary-model-of-that-day",
    "critic_model_id": "vendor/critic-model-of-that-day",
    "toaster_guidance": "Be lenient on payment terms.",
    "output_s3_key": "outputs/rev-modern/out.docx",
    "upload_s3_key": f"uploads/{OWNER}/rev-modern/in.docx",
}

# A review from before any of this was recorded: no model ids, no versions,
# no upload pointer.
HISTORIC_ROW: dict[str, Any] = {
    "review_id": "rev-historic",
    "owner_sub": OWNER,
    "playbook_id": "synthetic-nda-sample",
    "status": "DONE",
    "decision": "ACCEPT",
    "created_at": "1700000000",
    "updated_at": "1700000100",
}

OTHER_USERS_ROW: dict[str, Any] = {
    "review_id": "rev-someone-else",
    "owner_sub": OTHER,
    "playbook_id": "synthetic-nda-sample",
    "status": "DONE",
    "created_at": "1800000900",
}


def _resource(*rows: dict[str, Any]) -> _RowsResource:
    return _RowsResource(_RowsTable(list(rows)))


def _row(caller: str, is_admin: bool = False) -> dict[str, Any]:
    return {"cognito_sub": caller, "status": "active", "is_admin": is_admin}


class TestDetailProjectsModelProvenance(unittest.TestCase):
    def test_detail_returns_recorded_model_ids_and_input_availability(self) -> None:
        detail = reviews_module.get_review_detail(
            "rev-modern", _row(OWNER), _resource(MODERN_ROW)
        )

        self.assertEqual(detail["primary_model_id"], "vendor/primary-model-of-that-day")
        self.assertEqual(detail["critic_model_id"], "vendor/critic-model-of-that-day")
        self.assertTrue(detail["has_input"])
        self.assertTrue(detail["has_output"])
        # The instructions that governed the review stay answerable from the
        # review itself (issue #431's field, which the History expander reads).
        self.assertEqual(detail["toaster_guidance"], "Be lenient on payment terms.")

    def test_historic_row_is_not_recorded_never_todays_model(self) -> None:
        detail = reviews_module.get_review_detail(
            "rev-historic", _row(OWNER), _resource(HISTORIC_ROW)
        )

        self.assertIsNone(detail["primary_model_id"])
        self.assertIsNone(detail["critic_model_id"])
        self.assertFalse(detail["has_input"])
        # The load-bearing negative: a review run before the field existed must
        # not be relabelled with the model configured today.
        self.assertNotEqual(
            detail["primary_model_id"], model_client_module.openrouter_primary_model_id()
        )
        self.assertNotEqual(
            detail["critic_model_id"], model_client_module.openrouter_critic_model_id()
        )


class TestListCarriesHistoryProvenance(unittest.TestCase):
    def test_list_item_carries_provenance_and_availability(self) -> None:
        items = reviews_module.list_reviews(_row(OWNER), _resource(MODERN_ROW))['items']
        self.assertEqual(len(items), 1)
        item = items[0]

        for field in (
            "review_id",
            "playbook_id",
            "status",
            "decision",
            "created_at",
            "policy_version",
            "posture_version",
            "primary_model_id",
            "critic_model_id",
        ):
            self.assertIn(field, item, f"the History table needs {field!r} on the list row")

        self.assertEqual(item["policy_version"], 3)
        self.assertEqual(item["posture_version"], 2)
        self.assertEqual(item["primary_model_id"], "vendor/primary-model-of-that-day")
        self.assertEqual(item["critic_model_id"], "vendor/critic-model-of-that-day")
        self.assertTrue(item["has_output"])
        self.assertTrue(item["has_input"])

    def test_list_never_carries_raw_s3_keys(self) -> None:
        """Availability is a boolean on the list; the object keys stay
        server-side (the same discipline `get_review_detail` already applies
        with `has_output`)."""
        items = reviews_module.list_reviews(_row(OWNER), _resource(MODERN_ROW))['items']
        blob = repr(items[0])

        self.assertNotIn("output_s3_key", items[0])
        self.assertNotIn("upload_s3_key", items[0])
        self.assertNotIn("outputs/rev-modern/out.docx", blob)
        self.assertNotIn(f"uploads/{OWNER}/rev-modern/in.docx", blob)

    def test_historic_row_lists_as_not_recorded(self) -> None:
        items = reviews_module.list_reviews(_row(OWNER), _resource(HISTORIC_ROW))['items']
        item = items[0]

        self.assertIsNone(item["primary_model_id"])
        self.assertIsNone(item["critic_model_id"])
        self.assertIsNone(item["policy_version"])
        self.assertFalse(item["has_input"])
        self.assertNotEqual(
            item["primary_model_id"], model_client_module.openrouter_primary_model_id()
        )


class TestOwnerScoping(unittest.TestCase):
    def test_reviewer_never_sees_another_users_review(self) -> None:
        items = reviews_module.list_reviews(
            _row(OWNER), _resource(MODERN_ROW, OTHER_USERS_ROW)
        )['items']
        ids = {i["review_id"] for i in items}

        self.assertEqual(ids, {"rev-modern"})
        self.assertNotIn("rev-someone-else", ids)

    def test_owner_scoped_listing_holds_for_an_admin_too(self) -> None:
        """The History tab is not a cross-user surface, even for an admin
        (explicitly out of scope for this ticket)."""
        items = reviews_module.list_reviews(
            _row(ADMIN, is_admin=True),
            _resource(MODERN_ROW, OTHER_USERS_ROW),
            owner_scoped=True,
        )['items']
        ids = {i["review_id"] for i in items}

        self.assertNotIn("rev-modern", ids)
        self.assertNotIn("rev-someone-else", ids)
        self.assertEqual(ids, set())

    def test_admin_default_listing_is_unchanged(self) -> None:
        """The documented `GET /api/reviews` behavior ("admin: all reviews")
        is NOT changed by this ticket -- only opted out of."""
        items = reviews_module.list_reviews(
            _row(ADMIN, is_admin=True), _resource(MODERN_ROW, OTHER_USERS_ROW)
        )['items']
        ids = {i["review_id"] for i in items}

        self.assertEqual(ids, {"rev-modern", "rev-someone-else"})


# ---------------------------------------------------------------------------
# (4) + (5) + (6) The routes, over the real router with moto S3.
# ---------------------------------------------------------------------------


class HistoryRouteTestBase(api84.ReviewApiTestBase):
    """#84's base verbatim (real router, moto S3, fake DynamoDB, fake SFN)."""

    def _seed_review(self, review_id: str, **fields: Any) -> None:
        table = self._reviews_table()
        row: dict[str, Any] = {
            "review_id": review_id,
            "status": "DONE",
            "created_at": "1800000000",
            "updated_at": "1800000000",
            "playbook_id": "synthetic-generic",
        }
        row.update(fields)
        table.put_item(Item=row)


class TestScopeQueryParam(HistoryRouteTestBase):
    def test_scope_mine_hides_other_users_rows_from_an_admin(self) -> None:
        self._seed_review("mine-1", owner_sub=ADMIN)
        self._seed_review("theirs-1", owner_sub=OTHER)
        self._authenticate_as(ADMIN, is_admin=True)

        resp = self.client.get("/api/reviews?scope=mine")

        self.assertEqual(resp.status_code, 200)
        ids = {r["review_id"] for r in resp.json()["reviews"]}
        self.assertEqual(ids, {"mine-1"})
        self.assertNotIn("theirs-1", ids)

    def test_default_scope_still_shows_an_admin_everything(self) -> None:
        self._seed_review("mine-2", owner_sub=ADMIN)
        self._seed_review("theirs-2", owner_sub=OTHER)
        self._authenticate_as(ADMIN, is_admin=True)

        resp = self.client.get("/api/reviews")

        self.assertEqual(resp.status_code, 200)
        ids = {r["review_id"] for r in resp.json()["reviews"]}
        self.assertEqual(ids, {"mine-2", "theirs-2"})

    def test_reviewer_scope_mine_is_still_owner_scoped(self) -> None:
        self._seed_review("r-own", owner_sub=OWNER)
        self._seed_review("r-theirs", owner_sub=OTHER)
        self._authenticate_as(OWNER)

        resp = self.client.get("/api/reviews?scope=mine")

        self.assertEqual(resp.status_code, 200)
        ids = {r["review_id"] for r in resp.json()["reviews"]}
        self.assertEqual(ids, {"r-own"})


class TestSubmissionRecordsUploadPointer(HistoryRouteTestBase):
    def test_review_row_records_the_input_document_pointer(self) -> None:
        resp = self._submit("owner-449-upload", idempotency_key="key-449-upload")
        self.assertEqual(resp.status_code, 202)
        review_id = resp.json()["review_id"]

        row = self._reviews_table().items[review_id]
        self.assertEqual(
            row.get("upload_s3_key"), f"uploads/owner-449-upload/{review_id}/in.docx"
        )


class TestInputDownloadRoute(HistoryRouteTestBase):
    ROUTE_TEMPLATE = "/api/reviews/{}/input"

    def _put_input_object(self, key: str) -> None:
        self.s3.put_object(
            Bucket=os.environ["UPLOADS_BUCKET"], Key=key, Body=b"the counterparty draft"
        )

    def test_route_is_registered(self) -> None:
        registered = {
            (getattr(r, "path", None), method)
            for r in self.app.routes
            for method in getattr(r, "methods", set())
        }
        self.assertIn(("/api/reviews/{review_id}/input", "GET"), registered)

    def test_owner_gets_a_presigned_url(self) -> None:
        key = f"uploads/{OWNER}/in-ok/in.docx"
        self._put_input_object(key)
        self._seed_review("in-ok", owner_sub=OWNER, upload_s3_key=key)
        self._authenticate_as(OWNER)

        resp = self.client.get(self.ROUTE_TEMPLATE.format("in-ok"))

        self.assertEqual(resp.status_code, 200)
        self.assertIn("url", resp.json())
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

    def test_non_owner_is_forbidden(self) -> None:
        key = f"uploads/{OWNER}/in-403/in.docx"
        self._put_input_object(key)
        self._seed_review("in-403", owner_sub=OWNER, upload_s3_key=key)
        self._authenticate_as(OTHER)

        resp = self.client.get(self.ROUTE_TEMPLATE.format("in-403"))

        self.assertEqual(resp.status_code, 403)

    def test_row_with_no_recorded_pointer_is_404(self) -> None:
        self._seed_review("in-404", owner_sub=OWNER)
        self._authenticate_as(OWNER)

        resp = self.client.get(self.ROUTE_TEMPLATE.format("in-404"))

        self.assertEqual(resp.status_code, 404)

    def test_purged_object_is_410_gone_not_a_dead_url(self) -> None:
        """Retention deletes the OBJECT and leaves the row's pointer in place.
        A presigned URL to a deleted object is exactly the broken link the AC
        forbids, so the route must say Gone instead of handing one out."""
        key = f"uploads/{OWNER}/in-410/in.docx"  # deliberately never PUT
        self._seed_review("in-410", owner_sub=OWNER, upload_s3_key=key)
        self._authenticate_as(OWNER)

        resp = self.client.get(self.ROUTE_TEMPLATE.format("in-410"))

        self.assertEqual(resp.status_code, 410)

    def test_key_not_bound_to_this_review_is_refused(self) -> None:
        """download.py's independent key-binding gate must still apply on this
        route: a stored pointer belonging to ANOTHER review is never presigned
        (issue #71 AC2's IDOR defence, now parameterised by prefix)."""
        cross_key = f"uploads/{OWNER}/some-other-review/in.docx"
        self._put_input_object(cross_key)
        self._seed_review("in-idor", owner_sub=OWNER, upload_s3_key=cross_key)
        self._authenticate_as(OWNER)

        resp = self.client.get(self.ROUTE_TEMPLATE.format("in-idor"))

        self.assertEqual(resp.status_code, 403)


class TestPurgedOutputIsGone(HistoryRouteTestBase):
    def test_purged_output_object_is_410_not_a_presigned_dead_link(self) -> None:
        key = "outputs/out-410/out.docx"  # row points at it; object was purged
        self._seed_review("out-410", owner_sub=OWNER, output_s3_key=key)
        self._authenticate_as(OWNER)

        resp = self.client.get("/api/reviews/out-410/output")

        self.assertEqual(resp.status_code, 410)


class TestGoneIsNotChargedAgainstTheDailyLimit(HistoryRouteTestBase):
    """A Gone answer delivers nothing, so it must cost nothing.

    `generate_presigned_download_url` runs the per-user daily-limit
    conditional-write increment before it checks whether the object is still
    there. Every click on a purged document therefore burned one of the
    caller's 20 daily slots and returned 410 having handed over no bytes --
    so a user with a handful of purged rows could exhaust the quota that
    exists to protect their still-downloadable redlines. The existence check
    sits after the owner/admin and key-binding gates either way, so moving it
    ahead of the counter discloses nothing to an unauthorized caller.
    """

    def _slots_used(self, sub: str) -> int:
        return sum(int(v) for v in self.users_ddb_client._items.get(sub, {}).values())

    def test_a_purged_input_does_not_consume_a_download_slot(self) -> None:
        key = f"uploads/{OWNER}/quota-in/in.docx"  # deliberately never PUT
        self._seed_review("quota-in", owner_sub=OWNER, upload_s3_key=key)
        self._authenticate_as(OWNER)

        resp = self.client.get("/api/reviews/quota-in/input")

        self.assertEqual(resp.status_code, 410)
        self.assertEqual(self._slots_used(OWNER), 0)

    def test_a_purged_output_does_not_consume_a_download_slot(self) -> None:
        self._seed_review("quota-out", owner_sub=OWNER, output_s3_key="outputs/quota-out/out.docx")
        self._authenticate_as(OWNER)

        resp = self.client.get("/api/reviews/quota-out/output")

        self.assertEqual(resp.status_code, 410)
        self.assertEqual(self._slots_used(OWNER), 0)

    def test_purged_rows_cannot_starve_a_still_downloadable_redline(self) -> None:
        """The concrete harm: clicking through a page of purged rows must not
        lock the user out of the one document that IS still there."""
        live_key = "outputs/quota-live/out.docx"
        self.s3.put_object(
            Bucket=os.environ["OUTPUTS_BUCKET"], Key=live_key, Body=b"the redline"
        )
        self._seed_review("quota-live", owner_sub=OWNER, output_s3_key=live_key)
        self._authenticate_as(OWNER)

        for index in range(download_module.MAX_DAILY_REVIEWS):
            review_id = f"quota-purged-{index}"
            self._seed_review(
                review_id, owner_sub=OWNER, output_s3_key=f"outputs/{review_id}/out.docx"
            )
            self.assertEqual(
                self.client.get(f"/api/reviews/{review_id}/output").status_code, 410
            )

        resp = self.client.get("/api/reviews/quota-live/output")

        self.assertEqual(resp.status_code, 200, "purged rows must not spend the daily quota")

    def test_a_real_download_still_costs_a_slot(self) -> None:
        """Regression guard on the reorder: the limit must still be enforced
        for downloads that actually deliver a URL."""
        key = f"uploads/{OWNER}/quota-ok/in.docx"
        self.s3.put_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=key, Body=b"draft")
        self._seed_review("quota-ok", owner_sub=OWNER, upload_s3_key=key)
        self._authenticate_as(OWNER)

        self.assertEqual(self.client.get("/api/reviews/quota-ok/input").status_code, 200)

        self.assertEqual(self._slots_used(OWNER), 1)


# ---------------------------------------------------------------------------
# (7) The UI is wired, for everyone.
# ---------------------------------------------------------------------------


class TestHistoryTabIsWiredForEveryone(unittest.TestCase):
    def test_review_history_component_exists(self) -> None:
        self.assertTrue(
            REVIEW_HISTORY_TSX.is_file(),
            "frontend/src/ReviewHistory.tsx must exist -- the History screen",
        )

    def test_app_registers_the_history_tab(self) -> None:
        source = APP_TSX.read_text(encoding="utf-8")

        self.assertIn("ReviewHistory", source, "App.tsx must import ReviewHistory")
        self.assertIn("'history'", source, "App.tsx must carry a 'history' TabId")
        self.assertIn('id="panel-history"', source)

    def test_history_tab_is_not_admin_gated(self) -> None:
        """The tab must sit in the ALWAYS-present part of the tab array, not
        inside the `isAdmin ? [...] : []` block -- a reviewer sees their own
        history."""
        source = APP_TSX.read_text(encoding="utf-8")

        admin_block = re.search(r"\.\.\.\(isAdmin\s*\n?\s*\?\s*\(\[(.*?)\]\s*as TabDef\[\]\)", source, re.S)
        self.assertIsNotNone(admin_block, "App.tsx's admin-only tab block was not found")
        self.assertNotIn(
            "id: 'history'",
            admin_block.group(1),
            "the History tab must not be inside the admin-only tab block",
        )
        self.assertIn("{ id: 'history', label:", source)

    def test_history_panel_stays_mounted(self) -> None:
        """Every tabpanel stays MOUNTED; only `hidden` toggles (App.tsx's
        stated invariant) -- so the History panel must be a plain
        `hidden={activeTab !== 'history'}` section, never conditionally
        rendered."""
        source = APP_TSX.read_text(encoding="utf-8")
        self.assertIn("hidden={activeTab !== 'history'}", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
