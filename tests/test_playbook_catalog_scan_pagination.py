#!/usr/bin/env python3
"""
Executable tests for `playbook_versions.list_all_version_playbook_ids`
(issue #485/#490) -- the catalog's DB-only-playbook union.

## Root problem these prove fixed

That function scans the whole `playbook_versions` table and pages via
`LastEvaluatedKey`. Nothing exercised the paging loop: the only fake that
backed it (`tests/test_shipped_playbook_seed.py::FakePlaybookVersionsTable`)
returns everything in a single page and never reports `LastEvaluatedKey`,
so a `break` on the first page -- silently dropping every playbook past
page 1 -- passed the entire suite. Verified by mutation: changing
`if not last_key: break` to an unconditional `break` did not fail one test.

That is the failure mode this repo already named for itself in
`tests/test_review_api_84.py` (issue #488): "a stand-in that always returns
everything in one page cannot fail the way the real table can, so paging
bugs would be invisible here." The fake below implements real paging for
the same reason.

The consequence in production is silent and total for the affected
playbooks: a dropped page means a DB-created contract type simply never
appears on the Review dial, with no error anywhere.

Also pins `ProjectionExpression`: this scan needs only `playbook_id`, and a
scan pages on the 1 MB of data it READS. Projecting one attribute instead
of whole version rows both cuts the read and makes a second page far less
likely in the first place.

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

os.environ.setdefault(
    "PLAYBOOK_VERSIONS_TABLE", "contract-toaster-playbook-versions-scanpage-test"
)

from src import playbook_versions  # noqa: E402


class PagingScanTable:
    """A `playbook_versions` table stand-in that pages a scan the way real
    DynamoDB does -- honouring `ExclusiveStartKey` and reporting
    `LastEvaluatedKey` while rows remain (the issue #488 convention in
    `tests/test_review_api_84.py`). A one-page fake cannot fail the way the
    real table can, which is precisely why the paging bug this file covers
    stayed invisible.

    Records every scan kwarg it is handed so a test can assert what the
    production call actually requested.
    """

    def __init__(self, items: list[dict[str, Any]], page_size: int) -> None:
        self._items = items
        self._page_size = page_size
        self.scan_calls: list[dict[str, Any]] = []

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scan_calls.append(dict(kwargs))
        start = 0
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key is not None:
            for index, item in enumerate(self._items):
                if (
                    item["playbook_id"] == start_key["playbook_id"]
                    and item["version"] == start_key["version"]
                ):
                    start = index + 1
                    break
        page = self._items[start : start + self._page_size]
        projection = kwargs.get("ProjectionExpression")
        if projection:
            wanted = [name.strip() for name in projection.split(",")]
            page = [{k: v for k, v in item.items() if k in wanted} for item in page]
        resp: dict[str, Any] = {"Items": [dict(item) for item in page]}
        if start + self._page_size < len(self._items):
            last = self._items[start + self._page_size - 1]
            resp["LastEvaluatedKey"] = {
                "playbook_id": last["playbook_id"],
                "version": last["version"],
            }
        return resp


class FakeDDB:
    def __init__(self, table: PagingScanTable) -> None:
        self._table = table

    def Table(self, _name: str) -> PagingScanTable:  # noqa: N802 - boto3 API name
        return self._table


def _rows() -> list[dict[str, Any]]:
    """Six version rows across four distinct playbook_ids, deliberately
    ordered so that the duplicate ids straddle a page boundary."""
    return [
        {"playbook_id": "alpha-agreement", "version": "v1", "content_hash": "sha256:a1"},
        {"playbook_id": "alpha-agreement", "version": "v2", "content_hash": "sha256:a2"},
        {"playbook_id": "bravo-agreement", "version": "v1", "content_hash": "sha256:b1"},
        {"playbook_id": "charlie-agreement", "version": "v1", "content_hash": "sha256:c1"},
        {"playbook_id": "charlie-agreement", "version": "v2", "content_hash": "sha256:c2"},
        {"playbook_id": "delta-agreement", "version": "v1", "content_hash": "sha256:d1"},
    ]


class TestScanPaging(unittest.TestCase):
    def test_ids_beyond_the_first_page_are_returned(self) -> None:
        """The whole point: a `break` on page 1 drops charlie/delta."""
        table = PagingScanTable(_rows(), page_size=2)
        ids = playbook_versions.list_all_version_playbook_ids(FakeDDB(table))
        self.assertEqual(
            ids,
            {"alpha-agreement", "bravo-agreement", "charlie-agreement", "delta-agreement"},
        )
        self.assertGreater(
            len(table.scan_calls), 1, "expected the scan to page, not to stop at page one"
        )

    def test_exclusive_start_key_is_threaded_through(self) -> None:
        """Paging that never advances the cursor would loop forever on page
        one; assert each follow-up scan actually carries the prior page's
        LastEvaluatedKey."""
        table = PagingScanTable(_rows(), page_size=2)
        playbook_versions.list_all_version_playbook_ids(FakeDDB(table))
        self.assertIsNone(table.scan_calls[0].get("ExclusiveStartKey"))
        for call in table.scan_calls[1:]:
            self.assertIn("ExclusiveStartKey", call)
            self.assertIn("playbook_id", call["ExclusiveStartKey"])

    def test_single_page_still_works(self) -> None:
        """No LastEvaluatedKey -> exactly one scan, no spurious second call."""
        table = PagingScanTable(_rows(), page_size=100)
        ids = playbook_versions.list_all_version_playbook_ids(FakeDDB(table))
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(table.scan_calls), 1)

    def test_empty_table_yields_no_ids(self) -> None:
        table = PagingScanTable([], page_size=2)
        self.assertEqual(playbook_versions.list_all_version_playbook_ids(FakeDDB(table)), set())

    def test_scan_projects_only_playbook_id(self) -> None:
        """This scan reads one attribute; projecting it keeps whole version
        rows out of the read and makes a second page far less likely."""
        table = PagingScanTable(_rows(), page_size=2)
        ids = playbook_versions.list_all_version_playbook_ids(FakeDDB(table))
        for call in table.scan_calls:
            self.assertEqual(call.get("ProjectionExpression"), "playbook_id")
        # Still correct when the table honours that projection and returns
        # rows carrying nothing else.
        self.assertEqual(len(ids), 4)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
