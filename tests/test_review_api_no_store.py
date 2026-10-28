#!/usr/bin/env python3
"""
The review READ routes answer `Cache-Control: no-store`.

## The production report this guards (2026-10-28, build c35b5e1)

The Review screen read "Stage not yet reported" for a whole 153 s review and
stayed on "Toasting..." after the server had marked it DONE, until a reload.
A poll response that any cache along the way keeps and replays -- the browser,
or the edge proxy in front of the DTS target -- is a status that can never
change, which freezes the screen the same way a hung poll does. The frontend
now asks with `cache: 'no-store'` and abandons a hung attempt
(frontend/src/__tests__/poll-hung-request.test.tsx); this is the half an
intermediary obeys: `GET /api/reviews/{id}` and `GET /api/reviews` both say
no-store, on every answer the poller and History act on.

Run standalone: `python3 tests/test_review_api_no_store.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Cross-test-file import (established convention -- see
# tests/test_review_history_449.py): the #84 harness owns the app, the fakes
# and the environment these routes read.
import test_review_api_84 as api84  # noqa: E402


class TestReviewReadRoutesAreNoStore(api84.ReviewApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        # `scope=mine` reads through `owner_sub-index` (issue #67).
        self.use_real_reviews_table()
        submit = self._submit("owner-no-store", idempotency_key="no-store-key")
        self.assertEqual(submit.status_code, 202)
        self.review_id = submit.json()["review_id"]

    def test_detail_is_no_store(self) -> None:
        resp = self.client.get(f"/api/reviews/{self.review_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "no-store")

    def test_listing_is_no_store(self) -> None:
        resp = self.client.get("/api/reviews?scope=mine")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.review_id, [row["review_id"] for row in resp.json()["reviews"]])
        self.assertEqual(resp.headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
