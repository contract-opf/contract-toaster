#!/usr/bin/env python3
"""
CI gate for issue #102: the completion-time auto-save must not spend a daily
download slot or write a `review_output_downloaded` audit row.

The frontend's completion-time auto-save is best effort (a browser may
suppress an anchor click that has no user activation), but the backend used
to charge the per-user daily slot and write the audit row for it before any
byte moved: a suppressed click left a false audit record, and the reviewer's
own Save click then spent a second slot.

Owner decision 2026-09-14: only a user-initiated request is charged and
audited. The auto-save declares itself with `?autosave=1`, presigns through
the uncharged, unaudited path, and is usable at most ONCE per review
(`autosave_presigned_at` conditional write on the review row), so it cannot
become a free unlimited download.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_review_api_84 as api84  # noqa: E402

OWNER = "owner-sub-102"
ROUTE = "/api/reviews/{}/output"


class AutosaveBase(api84.ReviewApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.use_real_reviews_table()

    def _seed(self, review_id: str, *, put_object: bool = True, **extra: Any) -> None:
        key = f"outputs/{review_id}/out.docx"
        if put_object:
            self.s3.put_object(Bucket=os.environ["OUTPUTS_BUCKET"], Key=key, Body=b"redline")
        row: dict[str, Any] = {
            "review_id": review_id,
            "owner_sub": OWNER,
            "status": "DONE",
            "decision": "REQUEST_CHANGE",
            "output_s3_key": key,
            "created_at": "1800000000",
            "updated_at": "1800000000",
        }
        row.update(extra)
        self._reviews_table().put_item(Item=row)
        self._authenticate_as(OWNER)

    def _slots_used(self) -> int:
        return sum(int(v) for v in self.users_ddb_client._items.get(OWNER, {}).values())

    def _download_rows(self, review_id: str) -> list[dict[str, Any]]:
        return [
            i
            for i in self._audit_table().items.values()
            if i.get("action") == "review_output_downloaded" and i.get("target") == review_id
        ]


class TestAutosaveIsUncharged(AutosaveBase):
    def test_autosave_spends_no_slot_and_writes_no_audit_row(self) -> None:
        self._seed("rev-as")
        resp = self.client.get(ROUTE.format("rev-as") + "?autosave=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("url", resp.json())
        self.assertEqual(self._slots_used(), 0)
        self.assertEqual(self._download_rows("rev-as"), [])

    def test_user_click_spends_a_slot_and_writes_one_audit_row(self) -> None:
        self._seed("rev-click")
        resp = self.client.get(ROUTE.format("rev-click"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._slots_used(), 1)
        self.assertEqual(len(self._download_rows("rev-click")), 1)

    def test_autosave_then_click_costs_exactly_one_slot_and_one_row(self) -> None:
        self._seed("rev-both")
        self.assertEqual(self.client.get(ROUTE.format("rev-both") + "?autosave=1").status_code, 200)
        self.assertEqual(self.client.get(ROUTE.format("rev-both")).status_code, 200)
        self.assertEqual(self._slots_used(), 1)
        self.assertEqual(len(self._download_rows("rev-both")), 1)


class TestAutosaveIsNotAFreeUnlimitedDownload(AutosaveBase):
    def test_autosave_is_usable_at_most_once_per_review(self) -> None:
        self._seed("rev-once")
        url = ROUTE.format("rev-once") + "?autosave=1"
        self.assertEqual(self.client.get(url).status_code, 200)
        for _ in range(3):
            self.assertEqual(self.client.get(url).status_code, 409)
        self.assertEqual(self._slots_used(), 0)
        self.assertEqual(self._download_rows("rev-once"), [])

    def test_a_failed_autosave_does_not_forfeit_the_single_use(self) -> None:
        self._seed("rev-gone", put_object=False)
        url = ROUTE.format("rev-gone") + "?autosave=1"
        self.assertEqual(self.client.get(url).status_code, 410)
        self.s3.put_object(
            Bucket=os.environ["OUTPUTS_BUCKET"], Key="outputs/rev-gone/out.docx", Body=b"x"
        )
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_non_owner_cannot_use_autosave(self) -> None:
        self._seed("rev-idor")
        self._authenticate_as("someone-else")
        self.assertEqual(
            self.client.get(ROUTE.format("rev-idor") + "?autosave=1").status_code, 403
        )
        row = self._reviews_table().get_item(Key={"review_id": "rev-idor"})["Item"]
        self.assertNotIn("autosave_presigned_at", row)

    def test_an_admin_autosave_is_charged_audited_and_spends_nothing_of_the_owners(self) -> None:
        """An admin fetching someone else's redline is always charged and
        audited; the flag must not make it free, and must not burn the
        owner's single auto-save."""
        self._seed("rev-admin")
        self._authenticate_as("admin-sub", is_admin=True)
        self.assertEqual(
            self.client.get(ROUTE.format("rev-admin") + "?autosave=1").status_code, 200
        )
        self.assertEqual(len(self._download_rows("rev-admin")), 1)
        row = self._reviews_table().get_item(Key={"review_id": "rev-admin"})["Item"]
        self.assertNotIn("autosave_presigned_at", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
