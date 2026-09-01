#!/usr/bin/env python3
"""
Executable tests for issue #253: the audit query API — `GET /api/audit`, the
backend the #93 audit explorer binds to.

Every row of `docs/audit-queries.md` is served as a named, parameterised,
admin-gated query by `src/audit_queries.py`. This file asserts one result per
catalogue row, plus the gate, the bounds, and the efficiency invariant.

Drives the REAL, shipped application object (`src.main.app`) through a
FastAPI `TestClient`, with DynamoDB provided by **moto** (`mock_aws`) rather
than an in-memory fake — same convention as
tests/test_admin_dashboard_api_91.py. `get_dynamodb_resource` and
`get_current_user` are dependency-overridden; no network, no AWS.

## What is asserted here

  1. ADMIN-ONLY, AND THE ROLE COMES FROM THE `users` ROW. A non-admin caller
     gets 403 from the catalogue index and from every query, and the refusal
     body carries none of the data the query would have served. The SAME
     caller, with a JWT-shaped claim set asserting `is_admin`/an admin group,
     is still refused — and is admitted the instant their DynamoDB `users`
     row flips. That pair is what proves the gate reads the row and not the
     token (ARCHITECTURE.md -> "Group-naming misnomer").

  2. ONE ASSERTION PER CATALOGUE ROW. All eleven rows of
     docs/audit-queries.md, each computed from SEEDED rows rather than
     asserted structurally. The audit rows are written by calling the REAL
     WRITERS (`review_routes._write_audit_row`, `users.update_user`,
     `playbook_versions.activate_playbook_version` /
     `rollback_playbook_version`), never hand-built, so no fixture here can
     accept a shape production never stores.

  3. THE EFFICIENCY INVARIANT, WATCHED RED FIRST. The dependency-injected
     DynamoDB resource is a call-logging spy, and `assert_no_scans` fails if
     ANY table was scanned during a request. It runs in `tearDown`, so every
     test in this file enforces it, and `test_the_no_scan_assertion_is_not_
     vacuous` replaces one handler with a deliberately Scan-based
     implementation and requires the SAME assertion to FAIL — a green
     efficiency assertion that was never seen red proves nothing (issue
     #253, "Amendment: verification bar"). `denied_audit_mutations` is
     asserted to touch the database ZERO times.

  4. WHERE THE CATALOGUE OUTRUNS THE PIPELINE, THE TEST SAYS SO RATHER THAN
     FAKING A PRODUCER (issue #253, "Amendment 2026-08-25"):
       * `review_clause_ids` — retrieval is dormant by owner decision
         2026-08-11 (docs/rag-dormant.md), so a review whose history was
         written by the CURRENT pipeline is asserted to return EMPTY with
         `dormant: true`. No synthetic `retrieved_clause_ids` is planted on
         a current-pipeline review, and no "historical" row is hand-built
         either: no code in this repo's history has ever written a
         `review_complete` audit row or a `retrieved_clause_ids` attribute
         (#27 landed as a docs-only CI gate), so a fixture of that shape
         would be a shape production never stored. The entry stays in the
         endpoint; only the populated-result claim is absent.
       * `document_access` — no writer emits `access_denied`; the download
         routes audit only a SUCCESSFUL issuance. Asserted as
         `denials_recorded: false` and an empty denial half.
       * `break_glass` / `model_recertification` — no application writer
         emits those actions; asserted empty-and-`producers_wired: false`
         under the current pipeline, then answered from historical rows.
       * `rollback_population` — `prompt_hash` / `standard_form_hash` /
         `model_policy_hash` / `corpus_snapshot_version` are release-BUNDLE
         fields that no writer puts on a `reviews` row and no index covers;
         asserted to be a 400 naming the gap, never a scan.

  5. THE KEYS_ONLY INDEX IS REAL. The moto `reviews` table declares
     `playbook_hash-index` with the CDK's KEYS_ONLY projection, so a
     rollback-population result carrying `status`/`decision` can only have
     come from the BatchGetItem the implementation performs — an ALL-
     projection fixture would have made that pass for the wrong reason.

  6. BOUNDED. Each limit fixture seeds MORE rows than the cap it tests,
     because `len(results) <= MAX` over a fixture smaller than MAX is true
     of an implementation with no clamp at all.

  7. NOTHING LEAKS, AND THE PRODUCTION DECIMAL HAZARD (issue #440). An audit
     row's free-form `detail` can carry an S3 key — one real call site
     already puts one there — and it must not reach the payload. Numbers are
     seeded as real numbers so moto hands them back as `decimal.Decimal`,
     the exact shape that 500'd GET /api/users in production.

  8. DE-BRANDED. No 'Exos'/'EXOS' in any payload.

MUST FAIL on the pre-implementation tree: `GET /api/audit` is not registered
(404) and `src.audit_queries` does not exist.

Run standalone: `python3 tests/test_audit_query_api_93.py`
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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("USERS_TABLE", "contract-toaster-users-audit253-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-audit253-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-audit253-test")
os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-audit253-test"
)

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.audit_queries as audit_queries  # noqa: E402
import src.main as backend_main  # noqa: E402
import src.playbook_versions as playbook_versions  # noqa: E402
import src.review_routes as review_routes  # noqa: E402
from src.users import update_user  # noqa: E402

AUDIT_ROUTE = "/api/audit"

ADMIN_SUB = "admin-1"
OTHER_ADMIN_SUB = "admin-2"
NON_ADMIN_SUB = "reviewer-1"
TARGET_SUB = "reviewer-2"

PLAYBOOK_ID = "synthetic-nda-sample"

DAY = 24 * 3600

# Distinctive, synthetic marker strings -- no real party names, per the
# repo's fixture rule. Presence anywhere in a response body is unambiguous.
SENTINEL_S3_KEY = "outputs/SENTINEL-S3-KEY/out.docx"


# ---------------------------------------------------------------------------
# The efficiency detector (issue #253 Scope: "queries use key/index access --
# NO full-table scans").
#
# Deliberately a module-level function rather than a TestCase method, so the
# negative control below can invoke the EXACT assertion the real tests rely
# on and require it to fail. A detector that is only ever run against a
# passing implementation is not a detector.
# ---------------------------------------------------------------------------

def assert_no_scans(calls: list[tuple[str, str, Any]]) -> None:
    scanned = sorted({name for kind, name, _ in calls if kind == "scan"})
    if scanned:
        raise AssertionError(
            "GET /api/audit performed a full-table Scan on: " + ", ".join(scanned)
        )


class _CallLoggingTable:
    """A boto3 Table that records which access method was used."""

    def __init__(self, inner: Any, log: list[tuple[str, str, Any]]) -> None:
        self._inner = inner
        self._log = log

    def query(self, **kwargs: Any) -> Any:
        self._log.append(("query", self._inner.name, kwargs.get("IndexName")))
        return self._inner.query(**kwargs)

    def scan(self, **kwargs: Any) -> Any:
        self._log.append(("scan", self._inner.name, None))
        return self._inner.scan(**kwargs)

    def get_item(self, **kwargs: Any) -> Any:
        self._log.append(("get_item", self._inner.name, None))
        return self._inner.get_item(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CallLoggingResource:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str, Any]] = []

    def Table(self, name: str) -> Any:  # noqa: N802 -- boto3's own spelling
        return _CallLoggingTable(self._inner.Table(name), self.calls)

    def batch_get_item(self, **kwargs: Any) -> Any:
        for table_name in kwargs.get("RequestItems", {}):
            self.calls.append(("batch_get_item", table_name, None))
        return self._inner.batch_get_item(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _put_user(table: Any, sub: str, is_admin: bool) -> None:
    table.put_item(
        Item={
            "cognito_sub": sub,
            "email": f"{sub}@example.com",
            "status": "active",
            "is_admin": is_admin,
        }
    )


class AuditQueryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

        self.ddb.create_table(
            TableName=os.environ["USERS_TABLE"],
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # The audit table, WITH the two GSIs infra/lib/nested/data-stack.ts
        # declares and docs/audit-queries.md keys its queries on. Without
        # them every review-scoped and actor-scoped query raises
        # ValidationException -- which is the point: the fixture must reject
        # exactly what the real table would.
        self.ddb.create_table(
            TableName=os.environ["AUDIT_TABLE"],
            KeySchema=[
                {"AttributeName": "partition", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "partition", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
                {"AttributeName": "actor", "AttributeType": "S"},
                {"AttributeName": "review_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "actor-index",
                    "KeySchema": [
                        {"AttributeName": "actor", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "review_id-index",
                    "KeySchema": [
                        {"AttributeName": "review_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # KEYS_ONLY, exactly as the CDK declares playbook_hash-index. An
        # ALL-projection fixture would let an implementation that never
        # reads the row body pass the rollback-population assertions.
        self.ddb.create_table(
            TableName=os.environ["REVIEWS_TABLE"],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "review_id", "AttributeType": "S"},
                {"AttributeName": "playbook_hash", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "playbook_hash-index",
                    "KeySchema": [
                        {"AttributeName": "playbook_hash", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.create_table(
            TableName=os.environ["PLAYBOOK_VERSIONS_TABLE"],
            KeySchema=[
                {"AttributeName": "playbook_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "playbook_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        self.users_table = self.ddb.Table(os.environ["USERS_TABLE"])
        self.audit_table = self.ddb.Table(os.environ["AUDIT_TABLE"])
        self.reviews_table = self.ddb.Table(os.environ["REVIEWS_TABLE"])
        self.versions_table = self.ddb.Table(os.environ["PLAYBOOK_VERSIONS_TABLE"])

        _put_user(self.users_table, ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, OTHER_ADMIN_SUB, is_admin=True)
        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=False)
        _put_user(self.users_table, TARGET_SUB, is_admin=False)

        # The SPY is what the app reads through, so the no-Scan invariant is
        # enforced on every request every test in this file makes.
        self.spy = _CallLoggingResource(self.ddb)

        self.client = TestClient(backend_main.app)
        backend_main.app.dependency_overrides[backend_main.get_dynamodb_resource] = (
            lambda: self.spy
        )
        self._authenticate_as(ADMIN_SUB)

    def tearDown(self) -> None:
        try:
            assert_no_scans(self.spy.calls)
        finally:
            backend_main.app.dependency_overrides.clear()
            self._mock_aws.stop()

    # -- helpers ------------------------------------------------------------

    def _authenticate_as(self, sub: str, extra_claims: dict[str, Any] | None = None) -> None:
        claims = {"sub": sub, "email": f"{sub}@example.com", "token_use": "access"}
        claims.update(extra_claims or {})
        backend_main.app.dependency_overrides[backend_main.get_current_user] = lambda: claims

    def _get(self, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.client.get(AUDIT_ROUTE, params=clean)

    def _ok(self, **params: Any) -> dict[str, Any]:
        resp = self._get(**params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _audit(
        self,
        *,
        action: str,
        target: str,
        actor: str = ADMIN_SUB,
        target_type: str = "review",
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> None:
        """Write ONE audit row through the real writer
        (`review_routes._write_audit_row`), optionally at a chosen epoch.

        Seeded through the RAW resource, not the spy, so fixture writes never
        pollute the access log the efficiency assertion reads.
        """
        def write() -> None:
            review_routes._write_audit_row(
                self.ddb,
                actor=actor,
                action=action,
                target=target,
                target_type=target_type,
                detail=detail,
            )

        if at is None:
            write()
        else:
            with mock.patch("time.time", return_value=float(at)):
                write()

    def _put_review(self, review_id: str, **fields: Any) -> None:
        row: dict[str, Any] = {"review_id": review_id, "playbook_id": PLAYBOOK_ID}
        row.update(fields)
        self.reviews_table.put_item(Item={k: v for k, v in row.items() if v is not None})

    def _seed_release_history(self) -> None:
        """Release rows built by calling the real writers, so the fixture
        cannot drift from the shape production stores."""
        for version, content_hash in (("1.0.0", "hash-one"), ("2.0.0", "hash-two")):
            self.versions_table.put_item(
                Item={
                    "playbook_id": PLAYBOOK_ID,
                    "version": version,
                    "status": "uploaded",
                    "content_hash": content_hash,
                }
            )
        playbook_versions.activate_playbook_version(PLAYBOOK_ID, "1.0.0", ADMIN_SUB, self.ddb)
        playbook_versions.activate_playbook_version(PLAYBOOK_ID, "2.0.0", ADMIN_SUB, self.ddb)
        playbook_versions.rollback_playbook_version(PLAYBOOK_ID, "1.0.0", ADMIN_SUB, self.ddb)


# ---------------------------------------------------------------------------
# 1. The admin gate
# ---------------------------------------------------------------------------


class AdminGateTests(AuditQueryTestBase):
    def test_non_admin_is_refused_by_the_index_and_every_query(self) -> None:
        self._authenticate_as(NON_ADMIN_SUB)
        self.assertEqual(self._get().status_code, 403)
        for name in audit_queries.AUDIT_QUERY_NAMES:
            with self.subTest(query=name):
                self.assertEqual(self._get(query=name).status_code, 403)

    def test_refusal_body_serves_none_of_the_data(self) -> None:
        """A 403 must not be a filtered 200 in disguise."""
        self._audit(action="review_output_downloaded", target="rev-secret")
        self._authenticate_as(NON_ADMIN_SUB)
        resp = self._get(query="review_history", review_id="rev-secret")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("rev-secret", resp.text)
        self.assertNotIn("results", resp.text)
        self.assertNotIn("retention_boundary", resp.text)

    def test_role_is_the_users_row_not_a_token_claim(self) -> None:
        self._authenticate_as(
            NON_ADMIN_SUB,
            extra_claims={
                "is_admin": True,
                "cognito:groups": ["admins"],
                "custom:is_admin": "true",
            },
        )
        self.assertEqual(self._get().status_code, 403)
        self.assertEqual(self._get(query="release_activity").status_code, 403)

        _put_user(self.users_table, NON_ADMIN_SUB, is_admin=True)
        self.assertEqual(self._get().status_code, 200)
        self.assertEqual(self._get(query="release_activity").status_code, 200)


# ---------------------------------------------------------------------------
# 2. The catalogue index
# ---------------------------------------------------------------------------


class CatalogueIndexTests(AuditQueryTestBase):
    def test_index_lists_every_catalogue_entry(self) -> None:
        body = self._ok()
        names = [entry["query"] for entry in body["catalogue"]]
        self.assertEqual(names, list(audit_queries.AUDIT_QUERY_NAMES))
        self.assertEqual(len(names), 11)
        self.assertEqual(body["source"], "docs/audit-queries.md")
        self.assertEqual(body["max_limit"], audit_queries.AUDIT_QUERY_MAX_LIMIT)

    def test_every_query_name_is_documented_in_the_catalogue_file(self) -> None:
        """Drift guard: the endpoint and docs/audit-queries.md are two
        statements of the same catalogue, and a query name that exists in
        only one of them is how they start disagreeing."""
        doc = (REPO_ROOT / "docs" / "audit-queries.md").read_text(encoding="utf-8")
        for name in audit_queries.AUDIT_QUERY_NAMES:
            with self.subTest(query=name):
                self.assertIn(f"`{name}`", doc)

    def test_unrecognised_query_is_a_400_not_a_silent_empty_result(self) -> None:
        resp = self._get(query="everything_ever")
        self.assertEqual(resp.status_code, 400, resp.text)


# ---------------------------------------------------------------------------
# 3. One assertion per catalogue row
# ---------------------------------------------------------------------------


class MonthActivityTests(AuditQueryTestBase):
    """Catalogue row: "What happened this month, in order"."""

    def test_this_month_in_order_with_a_timestamp_range(self) -> None:
        # Anchor the seeded window inside ONE calendar month. This test writes
        # rows at anchor-1d..anchor-3d and then queries the anchor's month, so
        # a bare `now` only works from the 4th onward: run on the 1st-3rd (UTC)
        # the rows land in the PREVIOUS month, the query returns nothing, and
        # the assertion fails with `[] != ['rev-c', 'rev-b', 'rev-a']`. That is
        # not a product defect -- it made main red on 2026-09-01T01:02Z while
        # the same commit's gate passed on a UTC-4 developer machine still on
        # 08-31, which is exactly the kind of failure that reads as a real
        # regression and is not one.
        anchor = int(time.time())
        while int(time.strftime("%d", time.gmtime(anchor))) < 4:
            anchor -= DAY

        now = anchor
        self._audit(action="review_output_downloaded", target="rev-a", at=now - 3 * DAY)
        self._audit(action="review_input_downloaded", target="rev-b", at=now - 2 * DAY)
        self._audit(action="review_disposition_recorded", target="rev-c", at=now - DAY)

        month = time.strftime("%Y-%m", time.gmtime(now))
        body = self._ok(query="month_activity", month=month)
        self.assertEqual(body["month"], month)
        # Newest first.
        self.assertEqual(
            [row["target"] for row in body["results"]][:3], ["rev-c", "rev-b", "rev-a"]
        )
        self.assertEqual(body["retention_boundary"]["answerable"], "always")

        # The SK range narrows it -- and excludes, rather than merely
        # reorders, the row outside the window.
        narrowed = self._ok(
            query="month_activity",
            month=month,
            since=now - 2 * DAY - 60,
            until=now - 2 * DAY + 60,
        )
        self.assertEqual([row["target"] for row in narrowed["results"]], ["rev-b"])

    def test_a_non_numeric_timestamp_bound_is_a_400(self) -> None:
        self.assertEqual(
            self._get(query="month_activity", since="last-tuesday").status_code, 400
        )


class ReviewHistoryTests(AuditQueryTestBase):
    """Catalogue row: "Full history of one review/document"."""

    def test_one_reviews_history_never_includes_another_reviews_events(self) -> None:
        now = int(time.time())
        self._audit(action="review_output_downloaded", target="rev-a", at=now - 2 * DAY)
        self._audit(action="review_disposition_recorded", target="rev-a", at=now - DAY)
        self._audit(action="review_output_downloaded", target="rev-b", at=now - DAY)

        body = self._ok(query="review_history", review_id="rev-a")
        self.assertEqual(body["review_id"], "rev-a")
        self.assertEqual(
            [row["action"] for row in body["results"]],
            ["review_disposition_recorded", "review_output_downloaded"],
        )
        for row in body["results"]:
            self.assertEqual(row["review_id"], "rev-a")
            self.assertIsInstance(row["recorded_at"], int)
        self.assertNotIn("rev-b", json.dumps(body))

    def test_an_upload_scoped_row_never_joins_a_reviews_history(self) -> None:
        """`_write_audit_row(target_type="upload")` targets an UPLOAD id.
        Indexing it as a review id would answer a review's history with
        another entity's events."""
        self._audit(action="upload_rejected", target="rev-a", target_type="upload")
        body = self._ok(query="review_history", review_id="rev-a")
        self.assertEqual(body["results"], [])

    def test_review_id_is_required(self) -> None:
        self.assertEqual(self._get(query="review_history").status_code, 400)


class ActorActivityTests(AuditQueryTestBase):
    """Catalogue row: "Everything a given user did"."""

    def _admin_row(self, sub: str) -> dict[str, Any]:
        return self.users_table.get_item(Key={"cognito_sub": sub})["Item"]

    def test_everything_one_actor_did(self) -> None:
        # Real writer: users.update_user appends the lifecycle audit row.
        update_user(TARGET_SUB, {"is_admin": True}, self._admin_row(ADMIN_SUB), self.ddb)
        update_user(TARGET_SUB, {"is_admin": False}, self._admin_row(OTHER_ADMIN_SUB), self.ddb)

        body = self._ok(query="actor_activity", actor=ADMIN_SUB)
        self.assertEqual(body["actor"], ADMIN_SUB)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["actor"], ADMIN_SUB)
        self.assertEqual(body["results"][0]["action"], "user_lifecycle_update")
        self.assertNotIn(OTHER_ADMIN_SUB, json.dumps(body))

    def test_actor_activity_accepts_a_timestamp_range(self) -> None:
        now = int(time.time())
        self._audit(action="review_output_downloaded", target="rev-a", at=now - 5 * DAY)
        self._audit(action="review_output_downloaded", target="rev-b", at=now - DAY)
        body = self._ok(query="actor_activity", actor=ADMIN_SUB, since=now - 2 * DAY)
        self.assertEqual([row["target"] for row in body["results"]], ["rev-b"])

    def test_actor_is_required(self) -> None:
        self.assertEqual(self._get(query="actor_activity").status_code, 400)


class DocumentAccessTests(AuditQueryTestBase):
    """Catalogue row: "Who viewed/downloaded a document (and who was denied)"."""

    def test_only_access_events_for_that_document(self) -> None:
        now = int(time.time())
        self._audit(
            action="review_output_downloaded",
            target="rev-a",
            actor=TARGET_SUB,
            detail={"s3_key": SENTINEL_S3_KEY},
            at=now - 2 * DAY,
        )
        self._audit(action="review_input_downloaded", target="rev-a", at=now - DAY)
        # Same review, NOT an access event.
        self._audit(action="review_disposition_recorded", target="rev-a", at=now)

        body = self._ok(query="document_access", review_id="rev-a")
        self.assertEqual(
            [row["action"] for row in body["results"]],
            ["review_input_downloaded", "review_output_downloaded"],
        )
        actors = {row["actor"] for row in body["results"]}
        self.assertIn(TARGET_SUB, actors)

    def test_the_s3_key_a_real_call_site_audits_never_reaches_the_payload(self) -> None:
        """`review_routes`'s download route passes `detail={"s3_key": ...}`.
        A deployment internal must not reach an operator's browser through an
        audit explorer."""
        self._audit(
            action="review_output_downloaded",
            target="rev-a",
            detail={"s3_key": SENTINEL_S3_KEY},
        )
        for query in ("document_access", "review_history"):
            with self.subTest(query=query):
                resp = self._get(query=query, review_id="rev-a")
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertNotIn("SENTINEL-S3-KEY", resp.text)
                self.assertNotIn("s3_key", resp.text)

    def test_the_denial_half_is_reported_as_unwired_not_as_an_all_clear(self) -> None:
        """No writer emits `access_denied` -- the download routes audit only
        a SUCCESSFUL issuance. An empty list alone would read as "nobody was
        denied"."""
        self._audit(action="review_output_downloaded", target="rev-a")
        body = self._ok(query="document_access", review_id="rev-a")
        self.assertIs(body["denials_recorded"], False)
        self.assertIn("access_denied", body["actions"])
        self.assertEqual(
            [r for r in body["results"] if r["action"] == "access_denied"], []
        )


class ReviewClauseIdTests(AuditQueryTestBase):
    """Catalogue row: "Which clauses informed review X" (#27)."""

    def test_a_current_pipeline_review_records_no_clause_ids(self) -> None:
        """Issue #253 amendment: retrieval was retired 2026-08-11
        (docs/rag-dormant.md) and `review_spine.py` passes
        `retrieved_precedent=[]`, so a review whose audit history was
        written by the CURRENT pipeline has no clause ids. Asserted as
        EMPTY -- no synthetic `retrieved_clause_ids` is planted here to make
        the entry look live."""
        self._audit(action="review_output_downloaded", target="rev-live")
        self._audit(action="review_disposition_recorded", target="rev-live")
        body = self._ok(query="review_clause_ids", review_id="rev-live")
        self.assertEqual(body["results"], [])
        self.assertIs(body["dormant"], True)
        self.assertIs(body["producers_wired"], False)
        self.assertIn("rag-dormant.md", body["note"])


class RollbackPopulationTests(AuditQueryTestBase):
    """Catalogue row: "Rollback/quarantine population"."""

    def _seed_population(self) -> None:
        self._put_review(
            "rev-bad-1",
            playbook_hash="hash-bad",
            created_at="1750000001",
            status="DONE",
            decision="REQUEST_CHANGE",
            summary="SENTINEL-SUMMARY-indemnity is uncapped",
            toaster_guidance="SENTINEL-GUIDANCE-be lenient",
            upload_s3_key=SENTINEL_S3_KEY,
        )
        self._put_review(
            "rev-bad-2",
            playbook_hash="hash-bad",
            created_at="1750000002",
            status="DONE",
            decision="ACCEPT",
        )
        self._put_review(
            "rev-good", playbook_hash="hash-good", created_at="1750000003", status="DONE"
        )

    def test_every_review_run_under_a_given_bundle_hash(self) -> None:
        self._seed_population()
        body = self._ok(query="rollback_population", component="playbook_hash", value="hash-bad")
        self.assertEqual(
            sorted(row["review_id"] for row in body["results"]), ["rev-bad-1", "rev-bad-2"]
        )
        self.assertEqual(body["index"], "playbook_hash-index")
        self.assertNotIn("rev-good", json.dumps(body))

    def test_row_bodies_come_from_a_keyed_read_not_the_keys_only_index(self) -> None:
        """`playbook_hash-index` is KEYS_ONLY (see the CDK), so `status` and
        `decision` cannot have come off the index -- only from the
        BatchGetItem by primary key."""
        self._seed_population()
        body = self._ok(query="rollback_population", component="playbook_hash", value="hash-bad")
        by_id = {row["review_id"]: row for row in body["results"]}
        self.assertEqual(by_id["rev-bad-1"]["decision"], "REQUEST_CHANGE")
        self.assertEqual(by_id["rev-bad-2"]["decision"], "ACCEPT")
        self.assertTrue(
            any(kind == "batch_get_item" for kind, _, _ in self.spy.calls),
            "expected a BatchGetItem for the row bodies",
        )

    def test_document_substance_on_the_row_never_reaches_the_payload(self) -> None:
        self._seed_population()
        resp = self._get(query="rollback_population", component="playbook_hash", value="hash-bad")
        self.assertEqual(resp.status_code, 200, resp.text)
        for marker in ("SENTINEL-SUMMARY", "SENTINEL-GUIDANCE", "SENTINEL-S3-KEY"):
            self.assertNotIn(marker, resp.text)

    def test_an_unindexed_component_is_refused_never_scanned(self) -> None:
        """The other four catalogue components are release-BUNDLE fields
        (scripts/bind_bundle.py): no writer puts them on a `reviews` row and
        no GSI covers them, so answering them would mean a full-table scan.
        400, naming the gap."""
        self._seed_population()
        for component in audit_queries.ROLLBACK_UNINDEXED_COMPONENTS:
            with self.subTest(component=component):
                resp = self._get(
                    query="rollback_population", component=component, value="whatever"
                )
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertIn(component, resp.text)
        # And the refusals really did leave the table alone.
        assert_no_scans(self.spy.calls)

    def test_an_unknown_component_is_a_400(self) -> None:
        resp = self._get(query="rollback_population", component="vibes", value="x")
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_component_and_value_are_required(self) -> None:
        self.assertEqual(self._get(query="rollback_population").status_code, 400)
        self.assertEqual(
            self._get(query="rollback_population", component="playbook_hash").status_code, 400
        )


class PlaybookRequestChangeTests(AuditQueryTestBase):
    """Catalogue row: "Every REQUEST_CHANGE under a given playbook version"."""

    def _seed(self) -> None:
        self.versions_table.put_item(
            Item={
                "playbook_id": PLAYBOOK_ID,
                "version": "1.0.0",
                "status": "active",
                "content_hash": "hash-one",
            }
        )
        self._put_review(
            "rev-rc-1",
            playbook_hash="hash-one",
            created_at="1750000001",
            decision="REQUEST_CHANGE",
            playbook_version="1.0.0",
        )
        self._put_review(
            "rev-accept",
            playbook_hash="hash-one",
            created_at="1750000002",
            decision="ACCEPT",
            playbook_version="1.0.0",
        )
        self._put_review(
            "rev-other-version",
            playbook_hash="hash-two",
            created_at="1750000003",
            decision="REQUEST_CHANGE",
            playbook_version="2.0.0",
        )

    def test_only_request_change_under_that_version(self) -> None:
        self._seed()
        body = self._ok(
            query="playbook_request_changes",
            playbook_id=PLAYBOOK_ID,
            playbook_version="1.0.0",
        )
        self.assertEqual([row["review_id"] for row in body["results"]], ["rev-rc-1"])
        self.assertEqual(body["playbook_hash"], "hash-one")
        self.assertEqual(body["decision"], "REQUEST_CHANGE")
        self.assertNotIn("rev-accept", json.dumps(body))
        self.assertNotIn("rev-other-version", json.dumps(body))
        # An UNtruncated population must say so, not merely omit the field.
        self.assertIs(body["population_truncated"], False)
        self.assertEqual(body["reviews_examined"], 2)

    def test_a_capped_population_is_disclosed_not_served_as_a_short_answer(self) -> None:
        """`decision` is on no index, so the filter runs AFTER a capped read:
        on a version with more than AUDIT_QUERY_MAX_LIMIT reviews, older
        REQUEST_CHANGEs fall outside the window and `count` sits far below
        `limit` anyway -- a short answer that reads as a complete one. Seeded
        ABOVE the cap, with the ONLY REQUEST_CHANGE oldest, so a payload that
        failed to disclose the cap would report a clean version."""
        cap = audit_queries.AUDIT_QUERY_MAX_LIMIT
        self.versions_table.put_item(
            Item={
                "playbook_id": PLAYBOOK_ID,
                "version": "1.0.0",
                "status": "active",
                "content_hash": "hash-one",
            }
        )
        self._put_review(
            "rev-rc-oldest",
            playbook_hash="hash-one",
            created_at="1750000000",
            decision="REQUEST_CHANGE",
            playbook_version="1.0.0",
        )
        for index in range(cap + 5):
            self._put_review(
                f"rev-newer-{index:04d}",
                playbook_hash="hash-one",
                created_at=str(1750001000 + index),
                decision="ACCEPT",
                playbook_version="1.0.0",
            )
        body = self._ok(
            query="playbook_request_changes",
            playbook_id=PLAYBOOK_ID,
            playbook_version="1.0.0",
        )
        # The bare answer really is the misleading one: empty, well under limit.
        self.assertEqual(body["results"], [])
        self.assertLess(body["count"], body["limit"])
        # ...and the envelope says why, rather than letting it read as clean.
        self.assertIs(body["population_truncated"], True)
        self.assertEqual(body["reviews_examined"], cap)
        self.assertEqual(body["population_examined_cap"], cap)
        self.assertIn("older", body["note"].lower())

    def test_an_unknown_version_is_a_404(self) -> None:
        self._seed()
        resp = self._get(
            query="playbook_request_changes", playbook_id=PLAYBOOK_ID, playbook_version="9.9.9"
        )
        self.assertEqual(resp.status_code, 404, resp.text)


class ReleaseActivityTests(AuditQueryTestBase):
    """Catalogue row: "Release-bundle activations / rollbacks"."""

    def test_activations_and_rollbacks_with_their_bundle_hashes(self) -> None:
        self._seed_release_history()
        body = self._ok(query="release_activity")
        actions = sorted(row["action"] for row in body["results"])
        self.assertEqual(
            actions,
            ["release_bundle_activate", "release_bundle_activate", "release_bundle_rollback"],
        )
        rollback = next(
            row for row in body["results"] if row["action"] == "release_bundle_rollback"
        )
        self.assertEqual(rollback["playbook_id"], PLAYBOOK_ID)
        self.assertEqual(rollback["version"], "1.0.0")
        self.assertEqual(rollback["content_hash"], "hash-one")
        self.assertEqual(rollback["prior_active_version"], "2.0.0")
        self.assertEqual(body["months_searched"], audit_queries.AUDIT_QUERY_MONTHS_BACK)

    def test_a_non_release_audit_row_never_joins_the_feed(self) -> None:
        self._seed_release_history()
        update_user(
            TARGET_SUB,
            {"is_admin": True},
            self.users_table.get_item(Key={"cognito_sub": ADMIN_SUB})["Item"],
            self.ddb,
        )
        body = self._ok(query="release_activity")
        self.assertEqual(len(body["results"]), 3)
        self.assertNotIn("user_lifecycle_update", json.dumps(body))


class BreakGlassTests(AuditQueryTestBase):
    """Catalogue row: "Break-glass / governance-bypass uses"."""

    def test_empty_under_the_current_pipeline_and_it_says_so(self) -> None:
        self._seed_release_history()
        body = self._ok(query="break_glass")
        self.assertEqual(body["results"], [])
        self.assertIs(body["producers_wired"], False)

    def test_both_documented_shapes_are_matched(self) -> None:
        """The catalogue defines this as `reason = emergency-override` OR
        `action = governance_bypass`; a filter that checked only one would
        miss half of every investigation. Both rows here are explicitly
        historical/out-of-band shapes -- no application writer emits them."""
        self._audit(action="governance_bypass", target="rev-a")
        self._audit(
            action="review_output_downloaded",
            target="rev-b",
            detail={"reason": audit_queries.BREAK_GLASS_REASON},
        )
        self._audit(action="review_output_downloaded", target="rev-c")

        body = self._ok(query="break_glass")
        self.assertEqual(
            sorted(row["target"] for row in body["results"]), ["rev-a", "rev-b"]
        )


class ModelRecertificationTests(AuditQueryTestBase):
    """Catalogue row: "Model recertification record"."""

    def test_empty_under_the_current_pipeline_and_it_says_so(self) -> None:
        body = self._ok(query="model_recertification")
        self.assertEqual(body["results"], [])
        self.assertIs(body["producers_wired"], False)

    def test_a_recorded_recertification_is_found(self) -> None:
        self._audit(
            action="model_recertification",
            target="model-policy",
            target_type="model_settings",
        )
        self._audit(action="review_output_downloaded", target="rev-a")
        body = self._ok(query="model_recertification")
        self.assertEqual([row["action"] for row in body["results"]], ["model_recertification"])


class DeniedAuditMutationTests(AuditQueryTestBase):
    """Catalogue row: "Denied audit-table mutation attempts"."""

    def test_it_refuses_to_manufacture_an_all_clear_and_reads_nothing(self) -> None:
        """A denied mutation is refused by IAM before it reaches the table,
        so it leaves no audit row. Returning an empty result set would tell
        an investigator "no attempts" from a query that could never have
        found one."""
        before = len(self.spy.calls)
        body = self._ok(query="denied_audit_mutations")
        self.assertIs(body["answerable_via_api"], False)
        self.assertEqual(body["results"], [])
        self.assertIn("CloudTrail", body["source"])
        # The auth gate's users-row GetItem is the ONLY table access this
        # request may perform.
        after = [c for c in self.spy.calls[before:] if c[1] != os.environ["USERS_TABLE"]]
        self.assertEqual(after, [])


# ---------------------------------------------------------------------------
# 4. The efficiency invariant
# ---------------------------------------------------------------------------


class EfficiencyTests(AuditQueryTestBase):
    def _run_every_query(self) -> None:
        self._seed_release_history()
        self._audit(action="review_output_downloaded", target="rev-a")
        self._put_review("rev-a", playbook_hash="hash-one", created_at="1750000001")
        for name, params in (
            ("month_activity", {}),
            ("review_history", {"review_id": "rev-a"}),
            ("actor_activity", {"actor": ADMIN_SUB}),
            ("document_access", {"review_id": "rev-a"}),
            ("review_clause_ids", {"review_id": "rev-a"}),
            ("rollback_population", {"component": "playbook_hash", "value": "hash-one"}),
            (
                "playbook_request_changes",
                {"playbook_id": PLAYBOOK_ID, "playbook_version": "1.0.0"},
            ),
            ("release_activity", {}),
            ("break_glass", {}),
            ("denied_audit_mutations", {}),
            ("model_recertification", {}),
        ):
            with self.subTest(query=name):
                resp = self._get(query=name, **params)
                self.assertEqual(resp.status_code, 200, resp.text)

    def test_no_catalogue_query_scans_a_table(self) -> None:
        self._run_every_query()
        assert_no_scans(self.spy.calls)
        # Guard against a vacuous pass: the requests really did read.
        self.assertTrue(any(kind == "query" for kind, _, _ in self.spy.calls))

    def test_the_no_scan_assertion_is_not_vacuous(self) -> None:
        """WATCHED RED. `assert_no_scans` above is only evidence if it can
        fail, so here the SAME assertion runs against a deliberately
        Scan-based implementation of one catalogue entry and MUST raise."""

        def scan_based(dynamodb_resource: Any, params: dict[str, Any], now: float) -> dict:
            table = dynamodb_resource.Table(os.environ["AUDIT_TABLE"])
            items = table.scan().get("Items", [])
            return {"query": "month_activity", "count": len(items), "limit": 1, "results": []}

        with mock.patch.dict(audit_queries._HANDLERS, {"month_activity": scan_based}):
            self.assertEqual(self._get(query="month_activity").status_code, 200)
        with self.assertRaises(AssertionError):
            assert_no_scans(self.spy.calls)

        # ...and the SHIPPED implementation, over the same request, does not
        # trip it. (Clearing the log also lets tearDown's blanket assertion
        # judge the real implementation rather than the stand-in.)
        self.spy.calls.clear()
        self.assertEqual(self._get(query="month_activity").status_code, 200)
        assert_no_scans(self.spy.calls)


# ---------------------------------------------------------------------------
# 5. Bounds, Decimals, de-branding
# ---------------------------------------------------------------------------


class BoundsTests(AuditQueryTestBase):
    def test_limit_is_clamped_over_a_fixture_larger_than_the_cap(self) -> None:
        """Seeded ABOVE the cap: with fewer rows than MAX,
        `len(results) <= MAX` holds for an implementation with no clamp at
        all. The rows are written by the real writer, not hand-built."""
        for index in range(audit_queries.AUDIT_QUERY_MAX_LIMIT + 5):
            self._audit(action="review_output_downloaded", target=f"rev-{index:04d}")
        body = self._ok(query="month_activity", limit=100000)
        self.assertEqual(body["limit"], audit_queries.AUDIT_QUERY_MAX_LIMIT)
        self.assertEqual(len(body["results"]), audit_queries.AUDIT_QUERY_MAX_LIMIT)

    def test_a_small_limit_is_honoured(self) -> None:
        for index in range(5):
            self._audit(action="review_output_downloaded", target=f"rev-{index}")
        body = self._ok(query="month_activity", limit=2)
        self.assertEqual(len(body["results"]), 2)


class SerializationAndBrandTests(AuditQueryTestBase):
    def test_a_numeric_audit_attribute_does_not_500_the_route(self) -> None:
        """boto3's resource API hands stored numbers back as
        `decimal.Decimal`, which `JSONResponse` cannot encode -- the exact
        shape that took GET /api/users down in production (issue #440)."""
        self._audit(
            action="review_cover_note_generated",
            target="rev-a",
            detail={"cost_usd_cents": 42, "attempt": 1},
        )
        self._put_review(
            "rev-a", playbook_hash="hash-one", created_at="1750000001", cost_usd_cents=42
        )
        self.assertEqual(self._get(query="review_history", review_id="rev-a").status_code, 200)
        self.assertEqual(
            self._get(
                query="rollback_population", component="playbook_hash", value="hash-one"
            ).status_code,
            200,
        )

    def test_no_brand_name_appears_in_any_payload(self) -> None:
        self._seed_release_history()
        self._audit(action="review_output_downloaded", target="rev-a")
        self._put_review("rev-a", playbook_hash="hash-one", created_at="1750000001")
        for name, params in (
            ("", {}),
            ("month_activity", {}),
            ("review_history", {"review_id": "rev-a"}),
            ("actor_activity", {"actor": ADMIN_SUB}),
            ("document_access", {"review_id": "rev-a"}),
            ("review_clause_ids", {"review_id": "rev-a"}),
            ("rollback_population", {"component": "playbook_hash", "value": "hash-one"}),
            (
                "playbook_request_changes",
                {"playbook_id": PLAYBOOK_ID, "playbook_version": "1.0.0"},
            ),
            ("release_activity", {}),
            ("break_glass", {}),
            ("denied_audit_mutations", {}),
            ("model_recertification", {}),
        ):
            with self.subTest(query=name or "<catalogue index>"):
                resp = self._get(query=name or None, **params)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertNotIn("Exos", resp.text)
                self.assertNotIn("EXOS", resp.text)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
