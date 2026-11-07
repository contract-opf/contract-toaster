#!/usr/bin/env python3
"""
CI gate for issue #45 (owner decision D8): the daily download limit takes an
EXPLICIT clock, and "daily" is the UTC calendar day.

Before this, `download._check_per_user_limits` read bare `time.gmtime()`, so a
test that froze `time.time()` never froze what it saw and every "boundary"
test passed identically at any instant. These tests drive the module through
its `clock` seam only; nothing here patches `time`.

The rule under test: the day a download is charged to is the UTC calendar day
of the instant the clock returns -- whatever zone that instant is expressed
in. Counters are the per-day attributes `dailyReviewCount_<YYYY-MM-DD>`.

Checks (exit 1 on any failure):
  1. Immediately before / after 00:00:00 UTC land in different day buckets.
  2. A full counter before midnight does not bind the first request after it,
     and the old day's counter is untouched.
  3. Requests straddling the boundary (interleaved, one user) are counted per
     day, never merged.
  4. Leap day (2028-02-29) is its own bucket; a non-leap year has no Feb 29.
  5. Month rollover (01-31 -> 02-01) and year rollover (12-31 -> 01-01).
  6. A non-UTC aware instant is bucketed by its UTC day (no local-calendar
     reset): 23:30 at UTC-5 is already the NEXT UTC day; a DST spring-forward
     in New York does not move the reset.
  7. A naive datetime from a clock is refused, not guessed at.
  8. The production clock returns an aware UTC instant, and the default path
     (no clock passed) still works end to end.
  9. `generate_presigned_download_url` forwards its clock to the limiter.
"""
import datetime as dt
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402, I001
from moto import mock_aws  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import download as download_module  # noqa: E402

MAX = download_module.MAX_DAILY_REVIEWS
USERS_TABLE = "contract-toaster-users-test45"
ENV_NAME = "test45"
UTC = dt.timezone.utc  # noqa: UP017
PREFIX = "dailyReviewCount_"


def at(*args, tz=UTC):
    """A fixed clock at the given instant."""
    instant = dt.datetime(*args, tzinfo=tz)
    return lambda: instant


class ClockSeam(unittest.TestCase):
    def setUp(self) -> None:
        self._mock = mock_aws()
        self._mock.start()
        self.client = boto3.client("dynamodb", region_name="us-east-1")
        self.client.create_table(
            TableName=USERS_TABLE,
            KeySchema=[{"AttributeName": "cognito_sub", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "cognito_sub", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self._prev = os.environ.get("USERS_TABLE")
        os.environ["USERS_TABLE"] = USERS_TABLE

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("USERS_TABLE", None)
        else:
            os.environ["USERS_TABLE"] = self._prev
        self._mock.stop()

    def charge(self, clock, sub: str = "u") -> None:
        download_module._check_per_user_limits(
            user_sub=sub, env_name=ENV_NAME, dynamodb_client=self.client, clock=clock
        )

    def counters(self, sub: str = "u") -> dict:
        item = self.client.get_item(
            TableName=USERS_TABLE, Key={"cognito_sub": {"S": sub}}
        ).get("Item", {})
        return {
            k[len(PREFIX):]: int(v["N"]) for k, v in item.items() if k.startswith(PREFIX)
        }

    def fill(self, day: str, sub: str = "u") -> None:
        self.client.put_item(
            TableName=USERS_TABLE,
            Item={"cognito_sub": {"S": sub}, PREFIX + day: {"N": str(MAX)}},
        )

    # 1
    def test_1_just_before_and_after_midnight_utc_are_different_days(self) -> None:
        self.charge(at(2027, 3, 9, 23, 59, 59))
        self.charge(at(2027, 3, 10, 0, 0, 0))
        self.assertEqual(self.counters(), {"2027-03-09": 1, "2027-03-10": 1})

    def test_1b_last_microsecond_is_still_the_old_day(self) -> None:
        self.charge(at(2027, 3, 9, 23, 59, 59, 999999))
        self.assertEqual(self.counters(), {"2027-03-09": 1})

    # 2
    def test_2_full_counter_does_not_bind_after_reset(self) -> None:
        self.fill("2027-03-09")
        with self.assertRaises(HTTPException) as ctx:
            self.charge(at(2027, 3, 9, 23, 59, 59))
        self.assertEqual(ctx.exception.status_code, 429)
        self.charge(at(2027, 3, 10, 0, 0, 0))
        self.assertEqual(self.counters(), {"2027-03-09": MAX, "2027-03-10": 1})

    def test_2b_limit_holds_until_the_last_second_of_the_day(self) -> None:
        for _ in range(MAX):
            self.charge(at(2027, 3, 9, 0, 0, 0))
        with self.assertRaises(HTTPException) as ctx:
            self.charge(at(2027, 3, 9, 23, 59, 59))
        self.assertEqual(ctx.exception.status_code, 429)

    # 3
    def test_3_requests_spanning_the_boundary_are_counted_per_day(self) -> None:
        before, after = at(2027, 3, 9, 23, 59, 59), at(2027, 3, 10, 0, 0, 1)
        for clock in (before, after, before, after, after, before):
            self.charge(clock)
        self.assertEqual(self.counters(), {"2027-03-09": 3, "2027-03-10": 3})

    def test_3b_exhausted_old_day_blocks_late_arrivals_not_new_day(self) -> None:
        self.fill("2027-03-09")
        with self.assertRaises(HTTPException):
            self.charge(at(2027, 3, 9, 23, 59, 59))
        self.charge(at(2027, 3, 10, 0, 0, 0))

    # 4
    def test_4_leap_day(self) -> None:
        self.charge(at(2028, 2, 28, 23, 59, 59))
        self.charge(at(2028, 2, 29, 0, 0, 0))
        self.charge(at(2028, 2, 29, 23, 59, 59))
        self.charge(at(2028, 3, 1, 0, 0, 0))
        self.assertEqual(
            self.counters(), {"2028-02-28": 1, "2028-02-29": 2, "2028-03-01": 1}
        )

    def test_4b_non_leap_year_has_no_feb_29(self) -> None:
        self.charge(at(2027, 2, 28, 23, 59, 59))
        self.charge(at(2027, 3, 1, 0, 0, 0))
        self.assertEqual(self.counters(), {"2027-02-28": 1, "2027-03-01": 1})

    # 5
    def test_5_month_rollover(self) -> None:
        self.charge(at(2027, 1, 31, 23, 59, 59))
        self.charge(at(2027, 2, 1, 0, 0, 0))
        self.assertEqual(self.counters(), {"2027-01-31": 1, "2027-02-01": 1})

    def test_5b_year_rollover(self) -> None:
        self.charge(at(2027, 12, 31, 23, 59, 59))
        self.charge(at(2028, 1, 1, 0, 0, 0))
        self.assertEqual(self.counters(), {"2027-12-31": 1, "2028-01-01": 1})

    # 6
    def test_6_non_utc_instant_is_bucketed_by_its_utc_day(self) -> None:
        ny = ZoneInfo("America/New_York")
        # 23:30 EST on 03-09 is 04:30 UTC on 03-10.
        self.charge(at(2027, 3, 9, 23, 30, tz=ny))
        self.assertEqual(self.counters(), {"2027-03-10": 1})

    def test_6b_dst_spring_forward_does_not_move_the_reset(self) -> None:
        """US spring-forward is 2027-03-14 at 02:00 local. Neither local
        midnight nor the 02:00 gap is a reset; only 00:00 UTC is."""
        ny = ZoneInfo("America/New_York")
        self.charge(at(2027, 3, 13, 23, 0, 0))  # 18:00 EST, UTC day 03-13
        self.charge(at(2027, 3, 14, 1, 30, 0, tz=ny))  # 06:30 UTC 03-14
        self.charge(at(2027, 3, 14, 23, 59, 59))
        self.charge(at(2027, 3, 15, 0, 0, 0))
        self.assertEqual(
            self.counters(), {"2027-03-13": 1, "2027-03-14": 2, "2027-03-15": 1}
        )

    # 7
    def test_7_naive_clock_value_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.charge(lambda: dt.datetime(2027, 3, 9, 12, 0, 0))
        self.assertEqual(self.counters(), {})

    # 8
    def test_8_system_clock_is_aware_utc_and_default_path_works(self) -> None:
        now = download_module.system_clock()
        self.assertEqual(now.utcoffset(), dt.timedelta(0))
        download_module._check_per_user_limits(
            user_sub="u", env_name=ENV_NAME, dynamodb_client=self.client
        )
        today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
        self.assertIn(today, self.counters())

    def test_8b_day_key_function(self) -> None:
        self.assertEqual(
            download_module.utc_day_key(dt.datetime(2028, 2, 29, 23, 59, 59, tzinfo=UTC)),
            "2028-02-29",
        )

    # 9
    def test_9_presign_forwards_its_clock(self) -> None:
        sentinel = at(2027, 3, 9, 12, 0, 0)
        s3 = mock.MagicMock()
        s3.generate_presigned_url.return_value = "https://example.invalid/x"
        with mock.patch.object(download_module, "_check_per_user_limits") as chk, \
                mock.patch.object(download_module, "_validate_s3_key_bound_to_review"), \
                mock.patch.object(download_module, "_check_owner_or_admin"), \
                mock.patch.object(download_module, "_get_outputs_bucket", return_value="b"):
            download_module.generate_presigned_download_url(
                review_id="r",
                review_owner_sub="o",
                s3_key="reviews/r/out.docx",
                caller_user_row={"cognito_sub": "o"},
                env_name=ENV_NAME,
                s3_client=s3,
                dynamodb_client=self.client,
                clock=sentinel,
            )
        self.assertIs(chk.call_args.kwargs["clock"], sentinel)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(ClockSeam)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        print("\nFAIL: issue #45 clock-seam checks failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
