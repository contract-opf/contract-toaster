#!/usr/bin/env python3
"""
Executable tests for issue #59 AC:

  "A trivial test execution completes end-to-end through the stubbed stages,
   and a duplicate-submission test proves the retry collides rather than
   double-running."

These are unit tests that exercise the real enforcement code in
backend/src/reviews.py against in-memory fakes for DynamoDB and Step
Functions (no live AWS needed, no moto/boto3 dependency required — follows
the same third-party-stubbing convention as tests/test_download_auth_attack.py
so the suite runs in CI without extra installs).

Covers:
  1. Idempotency key derivation: client-supplied key wins; derived key
     checks current AND previous bucket.
  2. Worst-case, retry-inclusive spend reservation formula.
  3. Atomic daily-cap enforcement (fails closed).
  4. "Trivial end-to-end" — a first submission creates exactly one
     submission record, one reviews row, and starts exactly one execution.
  5. Duplicate-submission — a second call with the same idempotency key (or
     the same owner/file/bundle within the same time bucket) resumes the
     existing review/execution rather than creating a second one
     (ExecutionAlreadyExists is handled, not re-raised).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def _stub_third_party() -> None:
    """Inject minimal stubs for boto3, botocore, and fastapi if absent."""
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_202_ACCEPTED = 202
            HTTP_404_NOT_FOUND = 404
            HTTP_409_CONFLICT = 409
            HTTP_429_TOO_MANY_REQUESTS = 429
            HTTP_503_SERVICE_UNAVAILABLE = 503

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status
        sys.modules["fastapi"] = fastapi_mod

    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")
        exceptions_mod = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            def __init__(self, error_response=None, operation_name=""):
                self.response = error_response or {}
                super().__init__(str(error_response))

        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.exceptions"] = exceptions_mod

    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

import os  # noqa: E402

os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test")

import reviews as _reviews_module  # noqa: E402

ClientError = sys.modules["botocore.exceptions"].ClientError
HTTPException = sys.modules["fastapi"].HTTPException


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------

class FakeTable:
    """A tiny in-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        key = Key[self.key_name]
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        if ConditionExpression == "attribute_not_exists(idempotency_key)" and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ConditionExpression=None, ExpressionAttributeNames=None):
        key = Key[self.key_name]
        item = self.items.setdefault(key, dict(Key))

        # Very small, purpose-built interpreter for the specific
        # UpdateExpressions used by reviews.py in this test file — not a
        # general DynamoDB expression engine.
        vals = ExpressionAttributeValues or {}

        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            current = item.get("reserved_usd_cents", 0)
            cap = item.get("daily_cap_usd_cents", vals.get(":cap"))
            amount = vals[":amount"]
            if ConditionExpression and current + amount > cap:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            item["reserved_usd_cents"] = current + amount
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return

        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return

        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return

        # Generic fallback: no-op for anything else exercised indirectly.


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            key_name = {
                os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ["DAILY_SPEND_TABLE"]: "spend_date",
            }.get(name, "id")
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class ExecutionAlreadyExists(Exception):
    pass


class FakeSfnExceptions:
    ExecutionAlreadyExists = ExecutionAlreadyExists


class FakeSfnClient:
    """Fake Step Functions client — first StartExecution for a name succeeds;
    any subsequent call with the same name raises ExecutionAlreadyExists,
    exactly like the real service."""

    def __init__(self):
        self.exceptions = FakeSfnExceptions()
        self.started_names: set[str] = set()
        self.start_execution_call_count = 0

    def start_execution(self, stateMachineArn, name, input):
        self.start_execution_call_count += 1
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists()
        self.started_names.add(name)
        return {"executionArn": f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIdempotencyKeyDerivation(unittest.TestCase):
    def test_client_supplied_key_preferred(self):
        key = _reviews_module.resolve_idempotency_key(
            "client-key-123", "owner-1", "filehash", "bundlehash"
        )
        self.assertEqual(key, "client-key-123")

    def test_derived_key_is_deterministic_within_bucket(self):
        now = 1_000_000.0
        k1 = _reviews_module.derive_idempotency_key("owner-1", "filehash", "bundlehash", now)
        k2 = _reviews_module.derive_idempotency_key("owner-1", "filehash", "bundlehash", now + 5)
        self.assertEqual(k1, k2, "Same bucket must derive the same key.")

    def test_candidate_keys_include_current_and_previous_bucket(self):
        now = 1_000_000.0
        candidates = _reviews_module.candidate_idempotency_keys(
            "owner-1", "filehash", "bundlehash", now
        )
        self.assertEqual(len(candidates), 2, "Must check exactly current + previous bucket.")
        self.assertNotEqual(candidates[0], candidates[1])

    def test_boundary_straddling_retry_finds_previous_bucket_submission(self):
        """A submission created just before a bucket edge must be found by a
        retry that lands just after the edge (AC: check current AND previous
        bucket)."""
        bucket_seconds = _reviews_module.BUCKET_WIDTH_MINUTES * 60
        just_before_edge = 10 * bucket_seconds - 1
        just_after_edge = 10 * bucket_seconds + 1

        original_key = _reviews_module.derive_idempotency_key(
            "owner-1", "filehash", "bundlehash", just_before_edge
        )
        candidates_after = _reviews_module.candidate_idempotency_keys(
            "owner-1", "filehash", "bundlehash", just_after_edge
        )
        self.assertIn(
            original_key,
            candidates_after,
            "The previous-bucket key must be among the candidates checked just after the edge.",
        )


class TestSpendReservation(unittest.TestCase):
    def test_reservation_is_retry_inclusive_and_per_model(self):
        """reservation = (1 + max_retries) * sum over {primary, critic} of
        (max_in * that model's input rate + max_out * that model's output
        rate) -- issue #189: each pass priced at its OWN model's rate, not a
        single blended rate applied to both passes."""
        attempts_per_pass = 1 + _reviews_module.MAX_RETRIES_PER_PASS
        primary_usd = _reviews_module.MAX_INPUT_TOKENS * (
            _reviews_module.PRIMARY_INPUT_RATE_USD_PER_MILLION / 1_000_000
        ) + _reviews_module.MAX_OUTPUT_TOKENS * (
            _reviews_module.PRIMARY_OUTPUT_RATE_USD_PER_MILLION / 1_000_000
        )
        critic_usd = _reviews_module.MAX_INPUT_TOKENS * (
            _reviews_module.CRITIC_INPUT_RATE_USD_PER_MILLION / 1_000_000
        ) + _reviews_module.MAX_OUTPUT_TOKENS * (
            _reviews_module.CRITIC_OUTPUT_RATE_USD_PER_MILLION / 1_000_000
        )
        expected_usd = attempts_per_pass * (primary_usd + critic_usd)
        expected_cents = int(round(expected_usd * 100))
        self.assertEqual(
            _reviews_module.compute_worst_case_reservation_usd_cents(), expected_cents
        )

    def test_reservation_matches_architecture_md_worst_case(self):
        """The reservation must match ARCHITECTURE.md's documented $2.11
        worst-case/review, not the pre-fix $9.68 (issue #189: applying a
        single blended 'Opus output' rate to ALL tokens overshot by 4.6x
        and 429'd the third review of any day against the $20/day cap)."""
        self.assertEqual(_reviews_module.compute_worst_case_reservation_usd_cents(), 211)

    def test_reservation_fails_closed_over_daily_cap(self):
        ddb = FakeDynamoDBResource()
        table = ddb.Table(os.environ["DAILY_SPEND_TABLE"])
        # Pre-seed the day's reservation right at the cap boundary.
        cap_cents = _reviews_module.DAILY_SPEND_CAP_USD_CENTS_DEFAULT
        table.items["2026-06-30"] = {
            "spend_date": "2026-06-30",
            "reserved_usd_cents": cap_cents,
            "daily_cap_usd_cents": cap_cents,
        }
        import time as _time

        now = _time.mktime(_time.strptime("2026-06-30", "%Y-%m-%d"))
        with self.assertRaises(HTTPException) as ctx:
            _reviews_module.reserve_spend("review-x", ddb, now_epoch=now)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_reservation_succeeds_under_cap(self):
        ddb = FakeDynamoDBResource()
        reservation_id = _reviews_module.reserve_spend("review-y", ddb)
        self.assertTrue(reservation_id)


class TestTrivialEndToEndSubmission(unittest.TestCase):
    """AC: 'A trivial test execution completes end-to-end through the
    stubbed stages' — proven here at the API-orchestration layer: exactly
    one submission record, one reviews row, and one StartExecution call."""

    def test_first_submission_creates_exactly_one_of_each(self):
        ddb = FakeDynamoDBResource()
        sfn = FakeSfnClient()

        result = _reviews_module.submit_review(
            owner_sub="owner-1",
            playbook_id="eiaa",
            file_sha256="filehash-aaa",
            upload_pointer="uploads/owner-1/review-1/in.docx",
            active_release_bundle_hash="bundle-hash-v1",
            dynamodb_resource=ddb,
            sfn_client=sfn,
        )

        self.assertEqual(result["status_code"], 202)
        self.assertFalse(result["resumed"])

        submissions_table = ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        reviews_table = ddb.Table(os.environ["REVIEWS_TABLE"])

        self.assertEqual(len(submissions_table.items), 1, "Exactly one submission record.")
        self.assertEqual(len(reviews_table.items), 1, "Exactly one reviews row.")
        self.assertEqual(sfn.start_execution_call_count, 1, "Exactly one StartExecution call.")

        review_row = next(iter(reviews_table.items.values()))
        self.assertEqual(review_row["status"], "PENDING")
        self.assertEqual(review_row["playbook_id"], "eiaa")


class TestDuplicateSubmissionCollides(unittest.TestCase):
    """AC: 'a duplicate-submission test proves the retry collides rather
    than double-running.'"""

    def test_same_client_key_retry_resumes_not_duplicates(self):
        ddb = FakeDynamoDBResource()
        sfn = FakeSfnClient()

        first = _reviews_module.submit_review(
            owner_sub="owner-2",
            playbook_id="eiaa",
            file_sha256="filehash-bbb",
            upload_pointer="uploads/owner-2/review-2/in.docx",
            active_release_bundle_hash="bundle-hash-v1",
            dynamodb_resource=ddb,
            sfn_client=sfn,
            client_supplied_idempotency_key="client-key-fixed",
        )

        second = _reviews_module.submit_review(
            owner_sub="owner-2",
            playbook_id="eiaa",
            file_sha256="filehash-bbb",
            upload_pointer="uploads/owner-2/review-2/in.docx",
            active_release_bundle_hash="bundle-hash-v1",
            dynamodb_resource=ddb,
            sfn_client=sfn,
            client_supplied_idempotency_key="client-key-fixed",
        )

        self.assertEqual(
            first["review_id"], second["review_id"], "Retry must return the SAME review_id."
        )
        self.assertTrue(second["resumed"], "Second call must be recognized as a resume/retry.")

        submissions_table = ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"])
        reviews_table = ddb.Table(os.environ["REVIEWS_TABLE"])
        self.assertEqual(
            len(submissions_table.items), 1, "Still exactly one submission record after retry."
        )
        self.assertEqual(len(reviews_table.items), 1, "Still exactly one reviews row after retry.")
        # StartExecution called twice (initial + retry), but the second call
        # collides via the deterministic name -- ensure_execution_started
        # must handle ExecutionAlreadyExists without raising and without
        # recording a second ARN/execution.
        self.assertEqual(
            sfn.start_execution_call_count,
            1,
            "The retry's ensure_execution_started must be a no-op once an "
            "execution_arn is already recorded on the submission -- it must "
            "not call StartExecution again.",
        )

    def test_derived_key_retry_within_same_bucket_resumes(self):
        """No client key: two submissions with identical owner/file/bundle in
        the same timestamp bucket must collide on the derived key."""
        ddb = FakeDynamoDBResource()
        sfn = FakeSfnClient()

        first = _reviews_module.submit_review(
            owner_sub="owner-3",
            playbook_id="eiaa",
            file_sha256="filehash-ccc",
            upload_pointer="uploads/owner-3/review-3/in.docx",
            active_release_bundle_hash="bundle-hash-v1",
            dynamodb_resource=ddb,
            sfn_client=sfn,
        )
        second = _reviews_module.submit_review(
            owner_sub="owner-3",
            playbook_id="eiaa",
            file_sha256="filehash-ccc",
            upload_pointer="uploads/owner-3/review-3/in.docx",
            active_release_bundle_hash="bundle-hash-v1",
            dynamodb_resource=ddb,
            sfn_client=sfn,
        )

        self.assertEqual(first["review_id"], second["review_id"])
        reviews_table = ddb.Table(os.environ["REVIEWS_TABLE"])
        self.assertEqual(len(reviews_table.items), 1, "No double-run: still one review.")


class TestEnsureExecutionStartedHandlesRace(unittest.TestCase):
    def test_execution_already_exists_is_handled_not_raised(self):
        ddb = FakeDynamoDBResource()
        sfn = FakeSfnClient()
        submission = {
            "idempotency_key": "k1",
            "review_id": "review-z",
            "execution_name": "review-review-z",
            "execution_arn": None,
        }
        ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"]).items["k1"] = dict(submission)

        # First call starts the execution for real.
        _reviews_module.ensure_execution_started(submission, "{}", ddb, sfn)
        self.assertIsNotNone(submission["execution_arn"])

        # Simulate a second, independent submission dict (as if a retry
        # re-read the row before the ARN was persisted) hitting the same
        # deterministic name -- must not raise.
        racing_submission = {
            "idempotency_key": "k1",
            "review_id": "review-z",
            "execution_name": "review-review-z",
            "execution_arn": None,
        }
        try:
            _reviews_module.ensure_execution_started(racing_submission, "{}", ddb, sfn)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"ensure_execution_started must swallow ExecutionAlreadyExists, raised {exc!r}")


if __name__ == "__main__":
    unittest.main()
