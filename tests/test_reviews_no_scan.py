#!/usr/bin/env python3
"""
Gate for issue #52: no live path scans the `reviews` table.

`docs/audit-queries.md` §"No full-table scans, by construction" names
`reviews` and `audit` as the two tables that grow without bound and are never
scanned. `audit` was always clean (tests/test_audit_query_api_93.py). `reviews`
was scanned by six live paths -- and three of them (retention preview, purge
sweep, legal-hold list) read ONE page with no `LastEvaluatedKey` loop, so a
purge sweep silently never evaluated a row past the first megabyte.

## What is asserted, and why each assertion is the one that bites

  1. ZERO SCANS, AGAINST A REAL TABLE. The DynamoDB resource is a
     call-logging spy over **moto** (`mock_aws`), never an in-memory fake:
     moto rejects a query on an index the table does not declare exactly as
     DynamoDB does (asserted below), so a green run here means the code
     queried an index this fixture -- and infra/lib/nested/data-stack.ts,
     deploy/dts/bootstrap.py -- actually declare. `assert_no_scans` is copied
     from tests/test_audit_query_api_93.py, together with its watched-red
     negative control: a detector only ever seen green is not a detector.

  2. EVERY FULL READ FOLLOWS `LastEvaluatedKey`. The spy forces a page size
     of `SPY_PAGE_SIZE` rows on every query the code did not bound itself,
     so moto returns a `LastEvaluatedKey` on page one of every partition
     larger than that. Each read under test then has to be EXACT over a
     fixture larger than a page -- the health counts, the preview count, the
     sweep's eligible set, the hold list, the triage queue -- and the spy
     must have been asked for a continuation. A single-page implementation
     (the bug this ticket fixes) sees `SPY_PAGE_SIZE` rows per status and
     fails every one of those.

  3. THE ADMIN LISTING IS GLOBALLY NEWEST-FIRST ACROSS PAGES. Rows are seeded
     across every status; the concatenation of all pages must equal the
     whole table in `created_at` order with no duplicate and no gap. The
     scan-backed listing could only order within a page. The cursor format
     this ticket replaced is asserted to be a 400, not a silent restart.

Fixture rows carry the field names the production writers store
(`reviews._create_review_row`: review_id, owner_sub, status, created_at,
retention_window_at_creation, upload_s3_key; the terminal writers in
`pipeline_runner._write_real_terminal`: status, updated_at, output_s3_key) --
the same seeding convention tests/test_admin_dashboard_api_91.py and
tests/test_retention_purge_prefix_454.py use. The two flags the reads filter
on are written by the REAL writers: `retention.set_legal_hold` and
`disposition.record_disposition`.

MUST FAIL on the pre-implementation tree: every read under test scans.

Run standalone: `python3 tests/test_reviews_no_scan.py`
Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-noscan52-test")
os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-noscan52-test")
os.environ.setdefault("UPLOADS_BUCKET", "contract-toaster-uploads-noscan52-test")
os.environ.setdefault("OUTPUTS_BUCKET", "contract-toaster-outputs-noscan52-test")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

import src.admin_dashboard as admin_dashboard  # noqa: E402
import src.disposition as disposition  # noqa: E402
import src.retention as retention  # noqa: E402
import src.reviews as reviews  # noqa: E402

REGION = "us-east-1"
ADMIN = {"cognito_sub": "admin-1", "email": "admin-1@example.com", "is_admin": True}
OWNER = "reviewer-1"
DAY = 24 * 3600

# Rows per forced page. Every partition asserted on below is seeded LARGER
# than this, so a read that stops at page one comes up short.
SPY_PAGE_SIZE = 2


# ---------------------------------------------------------------------------
# The detector -- copied from tests/test_audit_query_api_93.py so the two
# gates enforce the identical invariant, and module-level so the negative
# control can invoke the EXACT assertion the real tests rely on.
# ---------------------------------------------------------------------------

def assert_no_scans(calls: list[tuple[str, str, Any]]) -> None:
    scanned = sorted({name for kind, name, _ in calls if kind == "scan"})
    if scanned:
        raise AssertionError(
            "a reviews read performed a full-table Scan on: " + ", ".join(scanned)
        )


class _CallLoggingTable:
    """A boto3 Table that records which access method was used, and forces a
    small page on every unbounded query so pagination has to be followed."""

    def __init__(self, inner: Any, log: list[tuple[str, str, Any]], stats: dict[str, int]):
        self._inner = inner
        self._log = log
        self._stats = stats

    def query(self, **kwargs: Any) -> Any:
        self._log.append(("query", self._inner.name, kwargs.get("IndexName")))
        self._stats["queries"] += 1
        if "ExclusiveStartKey" in kwargs:
            self._stats["continuations"] += 1
        if "Limit" not in kwargs:
            kwargs["Limit"] = SPY_PAGE_SIZE
        return self._inner.query(**kwargs)

    def scan(self, **kwargs: Any) -> Any:
        self._log.append(("scan", self._inner.name, None))
        return self._inner.scan(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CallLoggingResource:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str, Any]] = []
        self.stats: dict[str, int] = {"queries": 0, "continuations": 0}

    def Table(self, name: str) -> Any:  # noqa: N802 -- boto3's own spelling
        return _CallLoggingTable(self._inner.Table(name), self.calls, self.stats)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _reviews_table_spec(name: str, *, with_status_index: bool) -> dict[str, Any]:
    """The reviews table as infra/lib/nested/data-stack.ts declares the parts
    these reads touch: `owner_sub-index` and (unless withheld, for the
    honesty check) `status-index`, both `status`/`created_at`-keyed Strings."""
    indexes = [
        {
            "IndexName": "owner_sub-index",
            "KeySchema": [
                {"AttributeName": "owner_sub", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]
    if with_status_index:
        indexes.append(
            {
                "IndexName": "status-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        )
    key_attributes = ["review_id", "owner_sub", "created_at"]
    if with_status_index:
        key_attributes.append("status")  # DynamoDB rejects an unused definition
    return {
        "TableName": name,
        "KeySchema": [{"AttributeName": "review_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": attribute, "AttributeType": "S"} for attribute in key_attributes
        ],
        "GlobalSecondaryIndexes": indexes,
        "BillingMode": "PAY_PER_REQUEST",
    }


# (review_id, status, age_days, updated_age_days). Every status the listing
# merges over is present, and every partition a full read is asserted on holds
# MORE than SPY_PAGE_SIZE rows. Ages are distinct so newest-first is unambiguous.
SEED: tuple[tuple[str, str, int, int], ...] = (
    ("r-pending-a", "PENDING", 1, 1),
    ("r-pending-b", "PENDING", 2, 2),
    ("r-pending-stuck", "PENDING", 40, 40),
    ("r-running-a", "RUNNING", 3, 0),
    ("r-running-b", "RUNNING", 4, 4),
    ("r-running-stuck", "RUNNING", 50, 50),
    ("r-done-a", "DONE", 5, 5),
    ("r-done-b", "DONE", 6, 6),
    ("r-done-c", "DONE", 7, 7),
    ("r-done-old-a", "DONE", 100, 100),
    ("r-done-old-b", "DONE", 101, 101),
    ("r-done-old-c", "DONE", 102, 102),
    ("r-done-old-held", "DONE", 103, 103),
    ("r-error-a", "ERROR", 8, 8),
    ("r-error-b", "ERROR", 9, 9),
    ("r-error-old", "ERROR", 104, 104),
    ("r-manual-a", "MANUAL_REVIEW_REQUIRED", 10, 10),
    ("r-manual-b", "MANUAL_REVIEW_REQUIRED", 11, 11),
    ("r-manual-c", "MANUAL_REVIEW_REQUIRED", 12, 12),
    ("r-error-manual-a", "ERROR_MANUAL_REVIEW_REQUIRED", 13, 13),
    ("r-cancelled-a", "CANCELLED", 14, 14),
    ("r-cancelled-b", "CANCELLED", 15, 15),
    ("r-cancelled-c", "CANCELLED", 16, 16),
    ("r-quarantined-a", "QUARANTINED", 17, 17),
    ("r-superseded-a", "SUPERSEDED", 18, 18),
)
RETENTION_DAYS = 30
NOW = int(time.time())
TERMINAL = reviews.REVIEW_STATUSES_TERMINAL
IN_FLIGHT = reviews.REVIEW_STATUSES_NON_TERMINAL


def _created_at(age_days: int) -> str:
    return str(NOW - age_days * DAY)


def _expected_ids(predicate) -> set[str]:
    return {rid for rid, st, age, upd in SEED if predicate(rid, st, age, upd)}


class NoScanTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.ddb = boto3.resource("dynamodb", region_name=REGION)
        self.s3 = boto3.client("s3", region_name=REGION)
        self.ddb.create_table(
            **_reviews_table_spec(os.environ["REVIEWS_TABLE"], with_status_index=True)
        )
        # PK/SK match what retention.py::_write_audit_entry writes -- the
        # real `set_legal_hold` below audits its own action.
        self.ddb.create_table(
            TableName=os.environ["AUDIT_TABLE"],
            KeySchema=[
                {"AttributeName": "partition", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "partition", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.s3.create_bucket(Bucket=os.environ["UPLOADS_BUCKET"])
        self.s3.create_bucket(Bucket=os.environ["OUTPUTS_BUCKET"])
        self.table = self.ddb.Table(os.environ["REVIEWS_TABLE"])

        for review_id, review_status, age_days, updated_age_days in SEED:
            upload_key = f"uploads/{OWNER}/{review_id}/in.docx"
            row: dict[str, Any] = {
                "review_id": review_id,
                "owner_sub": OWNER,
                "playbook_id": "synthetic-nda-sample",
                "status": review_status,
                "created_at": _created_at(age_days),
                "updated_at": _created_at(updated_age_days),
                "retention_window_at_creation": RETENTION_DAYS,
                "upload_s3_key": upload_key,
            }
            self.s3.put_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=upload_key, Body=b"in")
            if review_status == "DONE":
                output_key = f"outputs/{review_id}/out.docx"
                row["output_s3_key"] = output_key
                self.s3.put_object(
                    Bucket=os.environ["OUTPUTS_BUCKET"], Key=output_key, Body=b"out"
                )
            self.table.put_item(Item=row)

        # The two flags the reads filter on, through the REAL writers.
        retention.set_legal_hold("r-done-old-held", "matter-1", ADMIN, self.ddb, self.s3)
        retention.set_legal_hold("r-running-stuck", "matter-2", ADMIN, self.ddb, self.s3)
        for review_id, outcome in (
            ("r-done-a", "EDITED"),
            ("r-done-b", "REJECTED"),
            ("r-done-c", "EDITED"),
            ("r-manual-a", "ACCEPTED"),
        ):
            disposition.record_disposition(review_id, outcome, self.ddb)

        self.spy = _CallLoggingResource(self.ddb)

    def tearDown(self) -> None:
        self._mock_aws.stop()

    def _assert_read_the_index_and_followed_pages(self) -> None:
        assert_no_scans(self.spy.calls)
        indexes = {index for kind, _, index in self.spy.calls if kind == "query"}
        self.assertEqual(indexes, {"status-index"}, self.spy.calls)
        self.assertGreater(
            self.spy.stats["continuations"],
            0,
            "no query carried ExclusiveStartKey: page two was never fetched",
        )


# ---------------------------------------------------------------------------
# 1. The reads
# ---------------------------------------------------------------------------


class AdminListingTests(NoScanTestBase):
    def test_every_page_is_index_backed_and_the_whole_is_newest_first(self) -> None:
        seen: list[str] = []
        token = None
        pages = 0
        while True:
            page = reviews.list_reviews(ADMIN, self.spy, limit=3, next_token=token)
            seen.extend(item["review_id"] for item in page["items"])
            pages += 1
            token = page["next_token"]
            if not token:
                break
            self.assertLess(pages, 50, "the cursor never terminated")

        assert_no_scans(self.spy.calls)
        self.assertEqual(
            {index for kind, _, index in self.spy.calls if kind == "query"}, {"status-index"}
        )
        expected = [rid for rid, _, _, _ in sorted(SEED, key=lambda r: r[2])]
        self.assertEqual(seen, expected, "not globally newest-first, or a gap/duplicate")
        self.assertGreater(pages, 1)

    def test_a_page_costs_at_most_one_query_per_status(self) -> None:
        reviews.list_reviews(ADMIN, self.spy, limit=3)
        assert_no_scans(self.spy.calls)
        self.assertLessEqual(self.spy.stats["queries"], len(reviews._ADMIN_LISTING_STATUSES))

    def test_the_replaced_scan_cursor_is_a_400_not_a_restart(self) -> None:
        from fastapi import HTTPException

        stale = reviews.encode_page_token({"review_id": "r-done-a"})
        with self.assertRaises(HTTPException) as ctx:
            reviews.list_reviews(ADMIN, self.spy, next_token=stale)
        self.assertEqual(ctx.exception.status_code, 400)
        assert_no_scans(self.spy.calls)


class PipelineHealthTests(NoScanTestBase):
    def test_counts_are_exact_past_the_forced_page_and_scan_free(self) -> None:
        # Six days: above every in-flight row that is merely old (<= 4 days
        # since it last moved) and below the two seeded as stuck (40 / 50).
        body = admin_dashboard.get_pipeline_health(
            ADMIN, self.spy, stale_after_seconds=6 * DAY, now_epoch=NOW
        )
        self._assert_read_the_index_and_followed_pages()

        for name in sorted(TERMINAL | IN_FLIGHT):
            self.assertEqual(
                body["status_counts"][name],
                len(_expected_ids(lambda _r, st, _a, _u: st == name)),
                name,
            )
        self.assertEqual(body["in_flight"], {"total": 6, "pending": 3, "running": 3})
        self.assertEqual(body["stale"]["total"], 2)
        # Oldest first, from the index's own order.
        self.assertEqual(
            [r["review_id"] for r in body["stale"]["reviews"]],
            ["r-running-stuck", "r-pending-stuck"],
        )


class RetentionTests(NoScanTestBase):
    ELIGIBLE = frozenset({"r-done-old-a", "r-done-old-b", "r-done-old-c", "r-error-old"})

    def test_preview_counts_every_eligible_row_on_every_page(self) -> None:
        preview = retention.preview_purge_sweep(RETENTION_DAYS, ADMIN, self.spy)
        self._assert_read_the_index_and_followed_pages()
        self.assertEqual(set(preview["review_ids"]), set(self.ELIGIBLE))
        self.assertEqual(preview["purge_count"], len(self.ELIGIBLE))

    def test_sweep_evaluates_every_row_on_every_page(self) -> None:
        summary = retention.run_purge_sweep_now(self.s3, self.spy, dry_run=True)
        self._assert_read_the_index_and_followed_pages()
        self.assertEqual(set(summary["eligible_reviews"]), set(self.ELIGIBLE))
        self.assertEqual(summary["deleted_reviews"], [])
        self.assertIn("r-done-old-held", summary["skipped_hold"])
        self.assertEqual(
            set(summary["skipped_active"]),
            _expected_ids(lambda _r, st, _a, _u: st not in retention.TERMINAL_REVIEW_STATUSES),
        )
        # Every seeded row was seen exactly once, somewhere.
        buckets = (
            summary["eligible_reviews"],
            summary["skipped_active"],
            summary["skipped_hold"],
            summary["skipped_not_yet_eligible"],
        )
        seen = [rid for bucket in buckets for rid in bucket]
        self.assertEqual(sorted(seen), sorted(rid for rid, _, _, _ in SEED))

    def test_hold_list_names_a_hold_in_any_status(self) -> None:
        holds = retention.list_legal_holds(ADMIN, self.spy)
        self._assert_read_the_index_and_followed_pages()
        self.assertEqual(
            {h["review_id"] for h in holds}, {"r-done-old-held", "r-running-stuck"}
        )
        self.assertEqual({h["status"] for h in holds}, {"DONE", "RUNNING"})


class TriageAndQueueTests(NoScanTestBase):
    def test_legal_triage_queue_is_index_backed_and_complete(self) -> None:
        queue = disposition.list_legal_triage_queue(self.spy)
        self._assert_read_the_index_and_followed_pages()
        self.assertEqual(
            {r["review_id"] for r in queue}, {"r-done-a", "r-done-b", "r-done-c"}
        )

    def test_recent_failures_are_index_backed_newest_first(self) -> None:
        failures = reviews.list_recent_failures(ADMIN, self.spy, limit=3)
        assert_no_scans(self.spy.calls)
        self.assertEqual(
            {index for kind, _, index in self.spy.calls if kind == "query"}, {"status-index"}
        )
        self.assertEqual(
            [f["review_id"] for f in failures], ["r-error-a", "r-error-b", "r-manual-a"]
        )

    def test_manual_review_queue_counts_every_page(self) -> None:
        body = admin_dashboard.list_manual_review_queue(ADMIN, self.spy, now_epoch=NOW)
        self._assert_read_the_index_and_followed_pages()
        self.assertEqual(body["counts"]["total"], 4)
        self.assertEqual(body["counts"]["MANUAL_REVIEW_REQUIRED"], 3)
        self.assertEqual(body["counts"]["ERROR_MANUAL_REVIEW_REQUIRED"], 1)


# ---------------------------------------------------------------------------
# 2. The detector and the fixture, watched red
# ---------------------------------------------------------------------------


class DetectorHonestyTests(NoScanTestBase):
    def test_the_no_scan_assertion_is_not_vacuous(self) -> None:
        """WATCHED RED. `assert_no_scans` is only evidence if it can fail, so
        the SAME assertion runs over a deliberately scan-based read of the
        same table and MUST raise -- then the shipped read, over the same
        spy, must not trip it."""

        def scan_based(dynamodb_resource: Any) -> int:
            table = dynamodb_resource.Table(os.environ["REVIEWS_TABLE"])
            return len(table.scan().get("Items", []))

        self.assertGreater(scan_based(self.spy), 0)
        with self.assertRaises(AssertionError):
            assert_no_scans(self.spy.calls)

        self.spy.calls.clear()
        retention.list_legal_holds(ADMIN, self.spy)
        assert_no_scans(self.spy.calls)

    def test_the_pagination_assertion_is_not_vacuous(self) -> None:
        """A single-page read -- the bug this ticket fixes -- must fail the
        continuation assertion, not slip past it."""
        page = self.spy.Table(os.environ["REVIEWS_TABLE"]).query(
            IndexName="status-index",
            KeyConditionExpression=boto3.dynamodb.conditions.Key("status").eq("DONE"),
        )
        self.assertIn("LastEvaluatedKey", page, "the spy did not force a short page")
        self.assertLess(len(page["Items"]), 7)
        with self.assertRaises(AssertionError):
            self._assert_read_the_index_and_followed_pages()

    def test_moto_rejects_a_query_on_an_undeclared_index(self) -> None:
        """The fixture rejects what the real table rejects: without the GSI,
        the very query the reads make is a ValidationException -- so the
        green tests above could only have passed against a declared index."""
        bare = "contract-toaster-reviews-noscan52-bare"
        self.ddb.create_table(**_reviews_table_spec(bare, with_status_index=False))
        with self.assertRaises(ClientError) as ctx:
            reviews._query_by_status(self.ddb.Table(bare), "DONE")
        # DynamoDB answers ValidationException ("The table does not have the
        # specified index"); moto spells the same refusal ResourceNotFound.
        # Either way the read is refused, and it names the index.
        error = ctx.exception.response["Error"]
        self.assertIn(error["Code"], {"ValidationException", "ResourceNotFoundException"})
        self.assertIn("index", error["Message"].lower())


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
