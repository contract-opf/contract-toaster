#!/usr/bin/env python3
"""
Gate for issue #488: `GET /api/reviews` is paged, and the cap cannot be
opted out of.

## What was wrong

The listing was unbounded on BOTH axes. Every row ever written came back in
one response, sorted in memory -- and for an admin without `scope=mine` that
meant a full table scan on every load. At beta usage that grows linearly
forever: History's first fetch gets heavier every week and never gets lighter.

## What is asserted, and why each assertion is the one that bites

1. **The page boundary.** 60 rows, page size 25: the boundary is where an
   off-by-one lives, so the test walks all three pages and asserts the
   concatenation has no duplicate and no gap. Asserting only "page one has 25
   rows" would pass with a cursor that repeats the same page forever.

2. **Newest-first ACROSS pages, not within one.** The old code sorted in
   memory, which can only order what it already fetched -- i.e. everything.
   The owner path now reads the `owner_sub-index` GSI backwards, so the order
   comes from the index. The test checks the order of the FULL concatenation,
   which is the property the in-memory sort could not have.

3. **The cap is not advisory.** `limit=100000` is clamped, so there is no
   request that turns this back into a full dump. That is the entire point of
   the ticket, and a clamp is trivially removable, so it gets its own test.

4. **A bad token is a 400, not a restart.** Silently starting over would look
   to a paging client like an endless stream of page one -- the failure mode
   is worse than the error.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")

from fastapi import HTTPException  # noqa: E402

from src import reviews as reviews_module  # noqa: E402

OWNER = "user-owner"
OTHER = "user-other"


class _PagingTable:
    """A reviews-table stand-in that pages the way the real one does.

    Deliberately implements `query` on `owner_sub-index` INCLUDING
    `ScanIndexForward` and `ExclusiveStartKey`, because the production path
    depends on all three. A fake that ignored them would return everything in
    one page and every assertion below would pass no matter what the code did
    -- which is the specific way a pagination test can be worthless.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(r) for r in rows]
        self.query_calls = 0
        self.scan_calls = 0

    def _page(self, ordered: list[dict[str, Any]], limit, start_key):
        start = 0
        if start_key is not None:
            ids = [r["review_id"] for r in ordered]
            after = start_key["review_id"]
            start = ids.index(after) + 1 if after in ids else 0
        page = ordered[start : start + limit] if limit else ordered[start:]
        resp: dict[str, Any] = {"Items": [dict(r) for r in page]}
        if page and (start + len(page)) < len(ordered):
            resp["LastEvaluatedKey"] = {"review_id": page[-1]["review_id"]}
        return resp

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls += 1
        assert kwargs["IndexName"] == "owner_sub-index"
        owner = kwargs["KeyConditionExpression"]._values[1]
        rows = [r for r in self.rows if r.get("owner_sub") == owner]
        rows.sort(
            key=lambda r: r.get("created_at") or "",
            reverse=not kwargs.get("ScanIndexForward", True),
        )
        return self._page(rows, kwargs.get("Limit"), kwargs.get("ExclusiveStartKey"))

    def scan(self, Limit=None, ExclusiveStartKey=None):  # noqa: N803 - boto3 kwarg names
        self.scan_calls += 1
        rows = sorted(self.rows, key=lambda r: r.get("created_at") or "", reverse=True)
        return self._page(rows, Limit, ExclusiveStartKey)


class _Resource:
    def __init__(self, table: _PagingTable) -> None:
        self._table = table

    def Table(self, _name: str) -> _PagingTable:  # noqa: N802 - boto3 method name
        return self._table


def _rows(count: int, owner: str = OWNER) -> list[dict[str, Any]]:
    # Descending timestamps so "newest first" has a single unambiguous answer.
    return [
        {
            "review_id": f"rev-{index:03d}",
            "owner_sub": owner,
            "status": "DONE",
            "created_at": str(2_000_000_000 - index),
        }
        for index in range(count)
    ]


def _caller(sub: str = OWNER, is_admin: bool = False) -> dict[str, Any]:
    return {"cognito_sub": sub, "status": "active", "is_admin": is_admin}


class TestPageBoundary(unittest.TestCase):
    def test_sixty_reviews_page_cleanly_with_no_duplicate_and_no_gap(self) -> None:
        table = _PagingTable(_rows(60))
        resource = _Resource(table)

        seen: list[str] = []
        token = None
        pages = 0
        while True:
            page = reviews_module.list_reviews(_caller(), resource, next_token=token)
            seen.extend(item["review_id"] for item in page["items"])
            pages += 1
            token = page["next_token"]
            if not token:
                break
            self.assertLess(pages, 10, "paging did not terminate")

        self.assertEqual(pages, 3, "60 rows at the default page size is three pages")
        self.assertEqual(len(seen), 60, "every row appears exactly once across the pages")
        self.assertEqual(len(set(seen)), 60, "a row appeared on two pages")
        # The boundary itself: rows 24/25 and 49/50 are adjacent in the
        # concatenation, in order, with nothing dropped between them.
        self.assertEqual(seen[24:26], ["rev-024", "rev-025"])
        self.assertEqual(seen[49:51], ["rev-049", "rev-050"])

    def test_the_order_is_newest_first_across_the_whole_listing(self) -> None:
        """Not merely within a page. The old in-memory sort could only order
        rows it had already fetched, which is the same as fetching them all --
        so ordering across pages is exactly the property that proves the index
        is doing the work."""
        resource = _Resource(_PagingTable(_rows(60)))
        seen: list[str] = []
        token = None
        while True:
            page = reviews_module.list_reviews(_caller(), resource, next_token=token)
            seen.extend(item["created_at"] for item in page["items"])
            token = page["next_token"]
            if not token:
                break
        self.assertEqual(seen, sorted(seen, reverse=True))

    def test_the_last_page_carries_no_token(self) -> None:
        page = reviews_module.list_reviews(_caller(), _Resource(_PagingTable(_rows(5))))
        self.assertEqual(len(page["items"]), 5)
        self.assertIsNone(page["next_token"], "a complete listing must not offer a next page")

    def test_an_empty_listing_is_a_page_not_an_error(self) -> None:
        page = reviews_module.list_reviews(_caller(), _Resource(_PagingTable([])))
        self.assertEqual(page["items"], [])
        self.assertIsNone(page["next_token"])


class TestTheCapIsNotAdvisory(unittest.TestCase):
    def test_an_enormous_limit_is_clamped(self) -> None:
        table = _PagingTable(_rows(300))
        page = reviews_module.list_reviews(_caller(), _Resource(table), limit=100_000)
        self.assertEqual(
            len(page["items"]),
            reviews_module.REVIEWS_PAGE_MAX_LIMIT,
            "a caller must not be able to ask for the whole table",
        )
        self.assertIsNotNone(page["next_token"])

    def test_a_nonsense_limit_still_returns_a_page(self) -> None:
        for bad in (0, -1, -100):
            page = reviews_module.list_reviews(_caller(), _Resource(_PagingTable(_rows(5))), limit=bad)
            self.assertEqual(len(page["items"]), 1, f"limit={bad} must clamp to 1, not to empty")

    def test_the_default_is_bounded_when_no_limit_is_asked_for(self) -> None:
        page = reviews_module.list_reviews(_caller(), _Resource(_PagingTable(_rows(300))))
        self.assertEqual(len(page["items"]), reviews_module.REVIEWS_PAGE_DEFAULT_LIMIT)


class TestTokenHandling(unittest.TestCase):
    def test_a_tampered_token_is_a_400_not_a_silent_restart(self) -> None:
        """Silently starting from page one would look, to a paging client,
        like an endless stream of the same first page -- a worse failure than
        an error."""
        # Whitespace is included on purpose: it is not "no token", it is a
        # token that is not one, and it must be answered the same way.
        for bad in ("not-base64!!", "  ", "eyJub3QiOiAiYSBrZXkifQ", "bnVsbA==", "e30="):
            resource = _Resource(_PagingTable(_rows(60)))
            with self.assertRaises(HTTPException) as caught:
                reviews_module.list_reviews(_caller(), resource, next_token=bad)
            self.assertEqual(caught.exception.status_code, 400, f"token {bad!r}")

    def test_no_token_at_all_is_page_one_not_an_error(self) -> None:
        for absent in (None, ""):
            page = reviews_module.list_reviews(
                _caller(), _Resource(_PagingTable(_rows(60))), next_token=absent
            )
            self.assertEqual(len(page["items"]), 25)

    def test_a_token_round_trips(self) -> None:
        key = {"review_id": "rev-024", "owner_sub": OWNER}
        token = reviews_module.encode_page_token(key)
        self.assertIsInstance(token, str)
        self.assertEqual(reviews_module.decode_page_token(token), key)
        self.assertIsNone(reviews_module.encode_page_token(None))
        self.assertIsNone(reviews_module.decode_page_token(None))


class TestScoping(unittest.TestCase):
    def test_paging_never_widens_what_a_reviewer_may_see(self) -> None:
        """The scoping is re-applied on EVERY page, not just the first. A
        cursor that fell back to the unscoped path on page two would leak
        another user's contract activity, and would look exactly like a
        working paginator."""
        rows = _rows(30, OWNER) + _rows(30, OTHER)
        for row in rows[30:]:
            row["review_id"] = row["review_id"].replace("rev-", "other-")
        resource = _Resource(_PagingTable(rows))

        seen: list[str] = []
        token = None
        while True:
            page = reviews_module.list_reviews(_caller(OWNER), resource, next_token=token)
            seen.extend(item["review_id"] for item in page["items"])
            token = page["next_token"]
            if not token:
                break
        self.assertEqual(len(seen), 30)
        self.assertTrue(all(rid.startswith("rev-") for rid in seen), seen)

    def test_an_admin_default_scope_no_longer_materializes_the_table(self) -> None:
        table = _PagingTable(_rows(300))
        page = reviews_module.list_reviews(_caller("admin", is_admin=True), _Resource(table))
        self.assertEqual(len(page["items"]), reviews_module.REVIEWS_PAGE_DEFAULT_LIMIT)
        self.assertIsNotNone(page["next_token"])
        # One bounded scan call, not a drain loop.
        self.assertEqual(table.scan_calls, 1)

    def test_an_owner_scoped_listing_uses_the_index_not_a_scan(self) -> None:
        """The whole cost argument depends on this. A scan+filter that
        happened to return the right rows would pass every ordering assertion
        above and still burn read capacity across the table."""
        table = _PagingTable(_rows(60))
        reviews_module.list_reviews(_caller(OWNER), _Resource(table))
        self.assertGreater(table.query_calls, 0, "the owner path must query the GSI")
        self.assertEqual(table.scan_calls, 0, "the owner path must not scan")


if __name__ == "__main__":
    unittest.main(verbosity=2)
