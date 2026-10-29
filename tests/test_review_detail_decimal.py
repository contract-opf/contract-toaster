#!/usr/bin/env python3
"""
A review row carrying numbers reads back through the routes instead of 500ing.

## The production failure this locks fixed (2026-10-28, build c35b5e1)

The first successful real review on the OPF 0.6 playbook: every
`GET /api/reviews/{id}` poll, from the review's first second, answered
HTTP 500 with

    TypeError: Object of type Decimal is not JSON serializable

out of `JSONResponse(content=detail)`. The Review screen sat on "Stage not
yet reported" through the whole run and on "Toasting..." past DONE. The
listing kept answering 200.

`get_review_detail` projected the row verbatim, and boto3's DynamoDB
resource API hands every stored number back as `decimal.Decimal`.
`instructions_version` is stamped as an int whenever admin instructions are
active; the OPF `posture_version`/`policy_version` and `playbook_version` are
ints too. The #84 harness's in-memory `FakeTable` stores plain ints, which is
why the suite never saw it -- the same blind spot as GET /api/users (#440).
So this drives a REAL (moto) table, which round-trips numbers to `Decimal`
exactly as DynamoDB does, and asserts the values come back as JSON numbers.

Run standalone: `python3 tests/test_review_detail_decimal.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import test_review_api_84 as api84  # noqa: E402

OWNER = "owner-decimal"

# The numeric fields a production row carries. The detail route projects all
# four; the listing projects the last three.
NUMERIC_FIELDS = {
    "instructions_version": 3,
    "posture_version": 2,
    "policy_version": 1,
    "playbook_version": 7,
}


class TestNumericRowFieldsSerialize(api84.ReviewApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.table = self.use_real_reviews_table()
        submit = self._submit(OWNER, idempotency_key="decimal-key")
        self.assertEqual(submit.status_code, 202)
        self.review_id = submit.json()["review_id"]
        names = {f"#f{i}": field for i, field in enumerate(NUMERIC_FIELDS)}
        values = {f":v{i}": value for i, value in enumerate(NUMERIC_FIELDS.values())}
        self.table.update_item(
            Key={"review_id": self.review_id},
            UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(names))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def test_the_real_table_hands_numbers_back_as_decimal(self) -> None:
        """The precondition: without it this suite would prove nothing."""
        item = self.table.get_item(Key={"review_id": self.review_id})["Item"]
        self.assertIsInstance(item["instructions_version"], Decimal)

    def test_detail_answers_200_with_plain_numbers(self) -> None:
        resp = self.client.get(f"/api/reviews/{self.review_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        for field, value in NUMERIC_FIELDS.items():
            self.assertEqual(body[field], value, field)
            # An integral Decimal must come back an int, not 3.0.
            self.assertIsInstance(body[field], int, field)

    def test_listing_answers_200_with_plain_numbers(self) -> None:
        resp = self.client.get("/api/reviews?scope=mine")
        self.assertEqual(resp.status_code, 200, resp.text)
        (row,) = [r for r in resp.json()["reviews"] if r["review_id"] == self.review_id]
        for field in ("posture_version", "policy_version", "playbook_version"):
            self.assertEqual(row[field], NUMERIC_FIELDS[field], field)
            self.assertIsInstance(row[field], int, field)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
