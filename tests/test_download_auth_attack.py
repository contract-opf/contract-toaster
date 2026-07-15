#!/usr/bin/env python3
"""
Executable attack / regression tests for issue #71 AC2 + AC5, and issue #193
(three latent defects found before download.py was ever routed).

Covers the negative / security paths that the issues explicitly require:

  1. Non-owner cannot download another user's review — HTTP 403 (AC5).
  2. Key-vs-review-id binding: an s3_key not scoped to outputs/<review_id>/
     (IDOR to another review / path traversal) — HTTP 403 (AC2).
  3. Expired presigned URL fails (TTL enforcement) — URL is short-lived (AC5).
  4. Per-user daily limit rejects excess requests — HTTP 429 (AC5).
  5. (#193) _check_per_user_limits keys on the REAL users-table partition
     key (cognito_sub, per infra/lib/nested/data-stack.ts:694) — a fake that
     accepts any key name would let a `Key={"sub": ...}` bug survive, which
     is exactly how the original defect shipped undetected.
  6. (#193) The download path never touches a `concurrentReviews` counter —
     an increment-only counter with no decrement would permanently lock a
     user out after a handful of downloads.
  7. (#193) Admin privilege is derived from the caller's DynamoDB users-row
     `is_admin` flag, never from a JWT-style claim (e.g. `custom:role`) —
     a caller row carrying a `custom:role: admin`-shaped field but
     `is_admin` false/absent must NOT be treated as admin.

These are unit tests that exercise the real enforcement code in
backend/src/download.py rather than just checking documentation prose.
They import the module under test directly and drive the enforcement
functions with attack-shaped inputs (wrong owner, over-limit counters,
forged-claim-shaped rows, a schema-validating fake DynamoDB client).

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup: ensure backend/src is importable.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


# ---------------------------------------------------------------------------
# Lazily import download.py without requiring boto3 / fastapi to be installed.
# We stub the third-party modules so the tests can run in CI without deps.
# ---------------------------------------------------------------------------

def _stub_third_party() -> None:
    """Inject minimal stubs for boto3, botocore, and fastapi if absent."""
    # ---- fastapi ----
    if "fastapi" not in sys.modules:
        fastapi_mod = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class status:  # noqa: N801
            HTTP_403_FORBIDDEN = 403
            HTTP_429_TOO_MANY_REQUESTS = 429
            HTTP_503_SERVICE_UNAVAILABLE = 503

        fastapi_mod.HTTPException = HTTPException
        fastapi_mod.status = status

        responses_mod = types.ModuleType("fastapi.responses")

        class JSONResponse:
            def __init__(self, content=None, headers=None, status_code=200):
                self.content = content
                self.headers = headers or {}
                self.status_code = status_code

        responses_mod.JSONResponse = JSONResponse
        fastapi_mod.responses = responses_mod
        sys.modules["fastapi"] = fastapi_mod
        sys.modules["fastapi.responses"] = responses_mod

    # ---- botocore ----
    if "botocore" not in sys.modules:
        botocore_mod = types.ModuleType("botocore")

        config_mod = types.ModuleType("botocore.config")

        class Config:
            def __init__(self, **kwargs):
                pass

        config_mod.Config = Config

        exceptions_mod = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            def __init__(self, error_response=None, operation_name=""):
                self.response = error_response or {}
                super().__init__(str(error_response))

        exceptions_mod.ClientError = ClientError
        botocore_mod.exceptions = exceptions_mod
        sys.modules["botocore"] = botocore_mod
        sys.modules["botocore.config"] = config_mod
        sys.modules["botocore.exceptions"] = exceptions_mod

    # ---- boto3 ----
    if "boto3" not in sys.modules:
        boto3_mod = types.ModuleType("boto3")
        sys.modules["boto3"] = boto3_mod


_stub_third_party()

# Now we can safely import the module under test.
import download as _download_module  # noqa: E402


# Re-export the enforcement functions and exception class for the tests.
_check_owner_or_admin = _download_module._check_owner_or_admin
_is_admin = _download_module._is_admin
_validate_s3_key_bound_to_review = _download_module._validate_s3_key_bound_to_review
_check_per_user_limits = _download_module._check_per_user_limits
generate_presigned_download_url = _download_module.generate_presigned_download_url
HTTPException = sys.modules["fastapi"].HTTPException
MAX_DAILY_REVIEWS = _download_module.MAX_DAILY_REVIEWS
PRESIGNED_URL_TTL_SECONDS = _download_module.PRESIGNED_URL_TTL_SECONDS
ClientError = sys.modules["botocore.exceptions"].ClientError

# #193 regression: the constant must no longer exist on the module at all —
# resurrecting it would signal the increment-only concurrency counter (or an
# equivalent) has crept back into the download path.
assert not hasattr(_download_module, "MAX_CONCURRENT_REVIEWS"), (
    "MAX_CONCURRENT_REVIEWS must not exist on download.py — the per-user "
    "concurrency counter was deliberately dropped from the download path "
    "(issue #193); reintroducing it here reintroduces the permanent-lockout "
    "defect unless it is also decremented somewhere, which this module has "
    "no way to do."
)


# ---------------------------------------------------------------------------
# Load the mock review Lambda handler (issue #59 swap point), the single
# stage that stamps output_s3_key onto the reviews row. It has no third-party
# dependencies (os/time/typing only), so it loads directly. Force the
# artificial PENDING->RUNNING delay to 0 (read at import time) so the
# regression test stays fast.
# ---------------------------------------------------------------------------
MOCK_REVIEW_HANDLER_PATH = REPO_ROOT / "infra" / "lambda" / "mock_review" / "handler.py"


def _load_mock_review_handler():
    os.environ["MOCK_REVIEW_DELAY_SECONDS"] = "0"
    spec = importlib.util.spec_from_file_location(
        "mock_review_handler", MOCK_REVIEW_HANDLER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mock_review_handler = _load_mock_review_handler()


# ---------------------------------------------------------------------------
# A schema-validating fake DynamoDB client (issue #193 defect 1).
#
# The prior tests used a bare MagicMock for the DynamoDB client, which
# accepts ANY Key shape silently — that is precisely how a `Key={"sub": ...}`
# bug (the real users table's PK is `cognito_sub`) shipped without a single
# test catching it. This fake enforces the real table's key schema and
# raises the same ClientError a real DynamoDB table would (ValidationException)
# when given the wrong key name, so a regression here fails loudly again.
# ---------------------------------------------------------------------------

class _RealKeyShapeFakeDynamoDBClient:
    """Minimal DynamoDB client fake that validates the users table's real
    partition key (`cognito_sub`) and tracks item state across calls."""

    PARTITION_KEY = "cognito_sub"

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self.update_item_calls: list[dict] = []

    def update_item(self, **kwargs):
        self.update_item_calls.append(kwargs)
        key = kwargs["Key"]

        if self.PARTITION_KEY not in key:
            # Mirrors real DynamoDB's behaviour: the Key does not match the
            # table's declared key schema.
            raise ClientError(
                error_response={
                    "Error": {
                        "Code": "ValidationException",
                        "Message": (
                            "The provided key element does not match the "
                            "schema"
                        ),
                    }
                },
                operation_name="UpdateItem",
            )

        item_key = key[self.PARTITION_KEY]["S"]
        item = self._items.setdefault(item_key, {})

        update_expr = kwargs.get("UpdateExpression", "")
        expr_names = kwargs.get("ExpressionAttributeNames", {})
        expr_values = kwargs.get("ExpressionAttributeValues", {})
        cond_expr = kwargs.get("ConditionExpression")

        # Only the single attribute this module actually writes needs to be
        # modelled: dailyReviewCount_<day> via #day / :one / :zero.
        day_attr = expr_names.get("#day")
        if day_attr is None or "dailyReviewCount_#day" not in update_expr:
            raise AssertionError(
                f"Unexpected UpdateExpression for this fake: {update_expr!r}"
            )

        current = int(item.get(day_attr, 0))

        if cond_expr:
            max_daily = int(expr_values.get(":maxDaily", {}).get("N", "0"))
            allowed = (day_attr not in item) or (current < max_daily)
            if not allowed:
                raise ClientError(
                    error_response={
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "The conditional request failed",
                        }
                    },
                    operation_name="UpdateItem",
                )

        item[day_attr] = current + 1
        return {}


# ---------------------------------------------------------------------------
# Attack test 1 — Non-owner cannot download another user's review (HTTP 403)
# ---------------------------------------------------------------------------

class TestNonOwnerDenied(unittest.TestCase):
    """AC5: a non-owner must be denied access to another user's review."""

    def test_non_owner_gets_403(self) -> None:
        """Caller whose cognito_sub != review_owner_sub must raise HTTP 403."""
        owner_sub = "owner-sub-aaa111"
        attacker_row = {
            "cognito_sub": "attacker-sub-bbb222",
            "email": "attacker@teamexos.com",
            "is_admin": False,
        }
        with self.assertRaises(HTTPException) as ctx:
            _check_owner_or_admin(
                review_owner_sub=owner_sub,
                caller_user_row=attacker_row,
            )
        self.assertEqual(
            ctx.exception.status_code,
            403,
            "Non-owner must receive HTTP 403, got "
            f"{ctx.exception.status_code}: {ctx.exception.detail}",
        )

    def test_owner_is_allowed(self) -> None:
        """Caller whose cognito_sub == review_owner_sub must not raise."""
        owner_sub = "owner-sub-aaa111"
        owner_row = {
            "cognito_sub": owner_sub,
            "email": "owner@teamexos.com",
            "is_admin": False,
        }
        # Must not raise.
        _check_owner_or_admin(
            review_owner_sub=owner_sub,
            caller_user_row=owner_row,
        )

    def test_admin_is_allowed_even_for_other_owners_review(self) -> None:
        """Admin (DynamoDB users-row is_admin == True) may download any review."""
        admin_row = {
            "cognito_sub": "admin-sub-ccc333",
            "email": "admin@teamexos.com",
            "is_admin": True,
        }
        # Must not raise even though the owner_sub is different.
        _check_owner_or_admin(
            review_owner_sub="owner-sub-aaa111",
            caller_user_row=admin_row,
        )

    def test_missing_cognito_sub_gets_403(self) -> None:
        """Caller row with no cognito_sub must be treated as non-owner → HTTP 403."""
        with self.assertRaises(HTTPException) as ctx:
            _check_owner_or_admin(
                review_owner_sub="owner-sub-aaa111",
                caller_user_row={"email": "attacker@teamexos.com", "is_admin": False},
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_jwt_shaped_admin_claim_is_not_trusted(self) -> None:
        """(#193 defect 3) A row carrying a JWT-claim-shaped `custom:role:
        'admin'` field but no (or false) `is_admin` DB flag must NOT be
        treated as admin. Admin privilege is exclusively the DynamoDB
        users-row `is_admin` flag.
        """
        forged_claim_row = {
            "cognito_sub": "attacker-sub-bbb222",
            "email": "attacker@teamexos.com",
            "custom:role": "admin",  # a forged/stale JWT-style claim
            "is_admin": False,  # the actual DB row says: not an admin
        }
        with self.assertRaises(HTTPException) as ctx:
            _check_owner_or_admin(
                review_owner_sub="owner-sub-aaa111",
                caller_user_row=forged_claim_row,
            )
        self.assertEqual(
            ctx.exception.status_code,
            403,
            "A custom:role claim must never grant admin access; only the "
            "DynamoDB is_admin flag may.",
        )
        self.assertFalse(_is_admin(forged_claim_row))

    def test_is_admin_reads_only_the_db_flag(self) -> None:
        """_is_admin must key strictly on `is_admin`, ignoring any
        JWT-claim-shaped keys that might be present on the row."""
        self.assertTrue(_is_admin({"cognito_sub": "x", "is_admin": True}))
        self.assertFalse(_is_admin({"cognito_sub": "x", "is_admin": False}))
        self.assertFalse(_is_admin({"cognito_sub": "x"}))
        self.assertFalse(
            _is_admin({"cognito_sub": "x", "custom:role": "admin"})
        )


# ---------------------------------------------------------------------------
# Attack test 2 — Expired presigned URL fails (TTL check)
# ---------------------------------------------------------------------------

class TestPresignedUrlExpiry(unittest.TestCase):
    """AC5: presigned URLs must have a short TTL; this test verifies the
    TTL constant is non-zero, ≤ 300 s, and that the URL itself is returned
    with Cache-Control: no-store.
    """

    def test_presigned_url_ttl_is_short_lived(self) -> None:
        """PRESIGNED_URL_TTL_SECONDS must be > 0 and <= 300 s."""
        self.assertGreater(
            PRESIGNED_URL_TTL_SECONDS,
            0,
            "Presigned URL TTL must be positive.",
        )
        self.assertLessEqual(
            PRESIGNED_URL_TTL_SECONDS,
            300,
            "Presigned URL TTL must be ≤ 300 s (per AC: 'very short-lived').",
        )

    def test_response_carries_no_store_header(self) -> None:
        """generate_presigned_download_url must set Cache-Control: no-store."""
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = (
            "https://s3.amazonaws.com/bucket/key?X-Amz-Expires=60&sig=..."
        )
        fake_ddb = _RealKeyShapeFakeDynamoDBClient()

        owner_sub = "owner-sub-aaa111"
        caller_row = {"cognito_sub": owner_sub, "email": "owner@teamexos.com", "is_admin": False}

        with patch.dict(
            "os.environ",
            {"S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dev"},
        ):
            response = generate_presigned_download_url(
                review_id="00000000-0000-4000-a000-000000000001",
                review_owner_sub=owner_sub,
                s3_key="outputs/00000000-0000-4000-a000-000000000001/result.json",
                caller_user_row=caller_row,
                env_name="dev",
                s3_client=mock_s3,
                dynamodb_client=fake_ddb,
            )

        cache_control = response.headers.get("Cache-Control", "")
        self.assertIn(
            "no-store",
            cache_control,
            f"Response must include Cache-Control: no-store, got: '{cache_control}'",
        )

    def test_response_includes_expires_in_field(self) -> None:
        """The response body must include expires_in so callers know the TTL."""
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = (
            "https://s3.amazonaws.com/bucket/key?X-Amz-Expires=60&sig=..."
        )
        fake_ddb = _RealKeyShapeFakeDynamoDBClient()

        owner_sub = "owner-sub-aaa111"
        caller_row = {"cognito_sub": owner_sub, "email": "owner@teamexos.com", "is_admin": False}

        with patch.dict(
            "os.environ",
            {"S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dev"},
        ):
            response = generate_presigned_download_url(
                review_id="00000000-0000-4000-a000-000000000001",
                review_owner_sub=owner_sub,
                s3_key="outputs/00000000-0000-4000-a000-000000000001/result.json",
                caller_user_row=caller_row,
                env_name="dev",
                s3_client=mock_s3,
                dynamodb_client=fake_ddb,
            )

        self.assertIn(
            "expires_in",
            response.content,
            "Response body must include 'expires_in' field.",
        )
        self.assertEqual(
            response.content["expires_in"],
            PRESIGNED_URL_TTL_SECONDS,
        )

    def test_non_owner_blocked_before_presigned_url_generated(self) -> None:
        """Owner check must fire BEFORE the presigned URL is generated.

        A non-owner attack path must not even reach the S3 presign call.
        """
        mock_s3 = MagicMock()
        fake_ddb = _RealKeyShapeFakeDynamoDBClient()

        attacker_row = {
            "cognito_sub": "attacker-sub-bbb222",
            "email": "attacker@teamexos.com",
            "is_admin": False,
        }

        with patch.dict(
            "os.environ",
            {"S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dev"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                generate_presigned_download_url(
                    review_id="00000000-0000-4000-a000-000000000001",
                    review_owner_sub="owner-sub-aaa111",
                    s3_key="outputs/00000000-0000-4000-a000-000000000001/result.json",
                    caller_user_row=attacker_row,
                    env_name="dev",
                    s3_client=mock_s3,
                    dynamodb_client=fake_ddb,
                )

        self.assertEqual(ctx.exception.status_code, 403)
        # S3 presign must NOT have been called.
        mock_s3.generate_presigned_url.assert_not_called()


# ---------------------------------------------------------------------------
# Attack test 2b — Key must be bound to the authorized review_id (AC2)
#                  IDOR / path-traversal: an s3_key not scoped to
#                  outputs/<review_id>/ must be rejected with HTTP 403.
# ---------------------------------------------------------------------------

class TestKeyBoundToReviewId(unittest.TestCase):
    """AC2: the s3_key must be scoped to ``outputs/<review_id>/``.

    Even when the caller is the legitimate owner, a key that points at another
    review's outputs, at a different data class, or that uses path traversal to
    escape the prefix must be denied with HTTP 403 — there must be no IDOR or
    path-traversal path to an arbitrary object.
    """

    AUTH_REVIEW_ID = "00000000-0000-4000-a000-000000000001"
    OTHER_REVIEW_ID = "11111111-1111-4111-a111-111111111111"

    def _assert_rejected(self, s3_key: str) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_s3_key_bound_to_review(s3_key, self.AUTH_REVIEW_ID)
        self.assertEqual(
            ctx.exception.status_code,
            403,
            f"Key {s3_key!r} must be rejected with HTTP 403, got "
            f"{ctx.exception.status_code}: {ctx.exception.detail}",
        )

    def test_key_for_another_review_is_rejected(self) -> None:
        """A key scoped to a DIFFERENT review_id must be rejected (IDOR)."""
        self._assert_rejected(f"outputs/{self.OTHER_REVIEW_ID}/result.json")

    def test_key_outside_outputs_prefix_is_rejected(self) -> None:
        """A key outside the outputs/ data class must be rejected."""
        self._assert_rejected(f"uploads/{self.AUTH_REVIEW_ID}/result.json")

    def test_key_with_only_prefix_no_object_is_rejected(self) -> None:
        """The prefix alone (no object name) must be rejected."""
        self._assert_rejected(f"outputs/{self.AUTH_REVIEW_ID}/")

    def test_key_with_path_traversal_is_rejected(self) -> None:
        """A key using ``..`` to escape the prefix must be rejected."""
        self._assert_rejected(
            f"outputs/{self.AUTH_REVIEW_ID}/../{self.OTHER_REVIEW_ID}/secret.json"
        )

    def test_absolute_and_backslash_keys_are_rejected(self) -> None:
        """Leading-slash and backslash keys must be rejected."""
        self._assert_rejected(f"/outputs/{self.AUTH_REVIEW_ID}/result.json")
        self._assert_rejected(
            f"outputs/{self.AUTH_REVIEW_ID}/..\\{self.OTHER_REVIEW_ID}\\x"
        )

    def test_prefix_lookalike_is_rejected(self) -> None:
        """A review_id used as a substring (not the real prefix) is rejected.

        e.g. ``outputs/<auth_id>-evil/x`` shares a prefix string but is NOT
        under ``outputs/<auth_id>/`` and must be denied.
        """
        self._assert_rejected(f"outputs/{self.AUTH_REVIEW_ID}-evil/result.json")

    def test_valid_bound_key_is_accepted(self) -> None:
        """A correctly-scoped key must NOT raise."""
        # Must not raise.
        _validate_s3_key_bound_to_review(
            f"outputs/{self.AUTH_REVIEW_ID}/result.json",
            self.AUTH_REVIEW_ID,
        )
        # Nested object below the prefix is also fine.
        _validate_s3_key_bound_to_review(
            f"outputs/{self.AUTH_REVIEW_ID}/sub/dir/result.json",
            self.AUTH_REVIEW_ID,
        )

    def test_mismatched_key_blocked_before_presign(self) -> None:
        """End-to-end: an owner passing a key for another review gets 403 and
        the S3 presign is never reached."""
        mock_s3 = MagicMock()
        fake_ddb = _RealKeyShapeFakeDynamoDBClient()

        owner_sub = "owner-sub-aaa111"
        owner_row = {
            "cognito_sub": owner_sub,
            "email": "owner@teamexos.com",
            "is_admin": False,
        }

        with patch.dict(
            "os.environ",
            {"S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dev"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                generate_presigned_download_url(
                    review_id=self.AUTH_REVIEW_ID,
                    review_owner_sub=owner_sub,
                    # Owner is legit, but the key points at ANOTHER review.
                    s3_key=f"outputs/{self.OTHER_REVIEW_ID}/result.json",
                    caller_user_row=owner_row,
                    env_name="dev",
                    s3_client=mock_s3,
                    dynamodb_client=fake_ddb,
                )

        self.assertEqual(ctx.exception.status_code, 403)
        # Neither the per-user DDB write nor the S3 presign may be reached.
        self.assertEqual(fake_ddb.update_item_calls, [])
        mock_s3.generate_presigned_url.assert_not_called()


# ---------------------------------------------------------------------------
# Attack test 3 — Per-user daily limit rejects excess requests (issue #193
# also folds in: real key shape, and no concurrency counter).
# ---------------------------------------------------------------------------

class TestPerUserLimits(unittest.TestCase):
    """AC5: the per-user daily limit must reject excess requests with HTTP
    429. Issue #193: this must work against the REAL users-table key shape
    (cognito_sub) and must never touch a concurrentReviews counter.
    """

    def _make_conditional_check_error(self) -> ClientError:
        return ClientError(
            error_response={
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "The conditional request failed",
                }
            },
            operation_name="UpdateItem",
        )

    def test_daily_review_limit_raises_429(self) -> None:
        """Per-user daily limit exceeded must raise HTTP 429."""
        mock_ddb = MagicMock()
        mock_ddb.update_item.side_effect = self._make_conditional_check_error()

        with self.assertRaises(HTTPException) as ctx:
            _check_per_user_limits(
                user_sub="user-sub-aaa111",
                env_name="dev",
                dynamodb_client=mock_ddb,
            )

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn(
            str(MAX_DAILY_REVIEWS),
            ctx.exception.detail,
        )

    def test_keys_on_real_users_table_partition_key(self) -> None:
        """(#193 defect 1) _check_per_user_limits must key on `cognito_sub`
        — the users table's actual partition key (data-stack.ts:694) — and
        must NOT raise against a fake that validates the real key schema.

        The old `Key={"sub": ...}` bug would raise ValidationException here
        (translated to HTTP 503) because this fake, unlike a bare MagicMock,
        actually checks the key name.
        """
        fake_ddb = _RealKeyShapeFakeDynamoDBClient()

        # Must not raise — proves the Key uses cognito_sub, not sub.
        _check_per_user_limits(
            user_sub="user-sub-aaa111",
            env_name="dev",
            dynamodb_client=fake_ddb,
        )

        self.assertEqual(len(fake_ddb.update_item_calls), 1)
        key = fake_ddb.update_item_calls[0]["Key"]
        self.assertIn(
            "cognito_sub",
            key,
            f"Key must use 'cognito_sub' (the real table PK), got: {key!r}",
        )
        self.assertNotIn(
            "sub",
            key,
            "Key must not use 'sub' — that does not match the users table "
            "schema and previously caused every call to raise "
            "ValidationException -> HTTP 503 (issue #193 defect 1).",
        )

    def test_daily_limit_enforced_against_real_key_shape_fake(self) -> None:
        """End-to-end against the schema-validating fake: the (MAX_DAILY_REVIEWS
        + 1)th call in a day for the same user must raise HTTP 429, not 503."""
        fake_ddb = _RealKeyShapeFakeDynamoDBClient()

        for _ in range(MAX_DAILY_REVIEWS):
            # Must not raise.
            _check_per_user_limits(
                user_sub="user-sub-aaa111",
                env_name="dev",
                dynamodb_client=fake_ddb,
            )

        with self.assertRaises(HTTPException) as ctx:
            _check_per_user_limits(
                user_sub="user-sub-aaa111",
                env_name="dev",
                dynamodb_client=fake_ddb,
            )
        self.assertEqual(ctx.exception.status_code, 429)

    def test_download_path_never_touches_concurrent_reviews_counter(self) -> None:
        """(#193 defect 2) The download path must not reference
        `concurrentReviews` anywhere in the DynamoDB call — an increment-only
        counter with no decrement would permanently lock a user out after a
        handful of downloads."""
        mock_ddb = MagicMock()
        mock_ddb.update_item.return_value = {}

        _check_per_user_limits(
            user_sub="user-sub-aaa111",
            env_name="dev",
            dynamodb_client=mock_ddb,
        )

        call_kwargs = mock_ddb.update_item.call_args[1]
        update_expr = call_kwargs.get("UpdateExpression", "")
        cond_expr = call_kwargs.get("ConditionExpression", "")
        expr_values = call_kwargs.get("ExpressionAttributeValues", {})

        self.assertNotIn("concurrentReviews", update_expr)
        self.assertNotIn("concurrentReviews", cond_expr)
        self.assertNotIn(":maxConcurrent", expr_values)

    def test_update_expression_increments_by_one(self) -> None:
        """The UpdateExpression must increment the daily counter by 1 (not
        add 0).

        The enforcement gate works only when the counter actually grows: a
        counter that stays at 0 (+ :zero) can never reach the limit, so the
        ConditionExpression would never fire for a real user.

        This test inspects the UpdateItem kwargs to confirm ':one' (value "1")
        is used in the UpdateExpression, not ':zero'.
        """
        mock_ddb = MagicMock()
        mock_ddb.update_item.return_value = {}

        _check_per_user_limits(
            user_sub="user-sub-aaa111",
            env_name="dev",
            dynamodb_client=mock_ddb,
        )

        call_kwargs = mock_ddb.update_item.call_args[1]
        update_expr = call_kwargs.get("UpdateExpression", "")
        expr_values = call_kwargs.get("ExpressionAttributeValues", {})

        # ':one' must appear in the expression as the increment operand.
        self.assertIn(
            ":one",
            update_expr,
            "UpdateExpression must reference ':one' to increment the counter; "
            f"got: {update_expr!r}",
        )
        # ':one' must map to the numeric value 1.
        self.assertIn(
            ":one",
            expr_values,
            "ExpressionAttributeValues must define ':one'.",
        )
        self.assertEqual(
            expr_values[":one"],
            {"N": "1"},
            f"':one' must be {{\"N\": \"1\"}}, got {expr_values.get(':one')!r}",
        )
        # The increment operand must be ':one', not ':zero'.
        # ':zero' is allowed only as the seed for if_not_exists(..., :zero);
        # what must NOT happen is a tail of "+ :zero" which would leave the
        # counter stuck at 0 after every call.
        import re as _re
        bad_increment = _re.search(r"\+\s*:zero", update_expr)
        self.assertIsNone(
            bad_increment,
            "UpdateExpression must not end with '+ :zero' — that would leave "
            f"the counter at 0 and make limit enforcement non-functional. Got: {update_expr!r}",
        )

    def test_under_limit_does_not_raise(self) -> None:
        """When DynamoDB update_item succeeds, _check_per_user_limits must not raise."""
        mock_ddb = MagicMock()
        mock_ddb.update_item.return_value = {"Attributes": {}}

        # Must not raise.
        _check_per_user_limits(
            user_sub="user-sub-aaa111",
            env_name="dev",
            dynamodb_client=mock_ddb,
        )

    def test_non_owner_blocked_before_limit_check(self) -> None:
        """Owner check must run BEFORE the DynamoDB limit check.

        An attacker who is not the owner must receive HTTP 403, not 429 —
        the DynamoDB conditional write must not be reached for an unauthorised
        caller because it would consume capacity.
        """
        mock_s3 = MagicMock()
        fake_ddb = _RealKeyShapeFakeDynamoDBClient()

        attacker_row = {
            "cognito_sub": "attacker-sub-bbb222",
            "email": "attacker@teamexos.com",
            "is_admin": False,
        }

        with patch.dict(
            "os.environ",
            {"S3_OUTPUTS_BUCKET": "contract-toaster-outputs-dev"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                generate_presigned_download_url(
                    review_id="00000000-0000-4000-a000-000000000001",
                    review_owner_sub="owner-sub-aaa111",
                    s3_key="outputs/00000000-0000-4000-a000-000000000001/result.json",
                    caller_user_row=attacker_row,
                    env_name="dev",
                    s3_client=mock_s3,
                    dynamodb_client=fake_ddb,
                )

        self.assertEqual(ctx.exception.status_code, 403)
        # DynamoDB must NOT have been called (owner check fires first).
        self.assertEqual(fake_ddb.update_item_calls, [])

    def test_limit_enforcement_uses_condition_expression(self) -> None:
        """The DynamoDB call must include a ConditionExpression (atomic check).

        This test verifies the structural property that the limit check is
        an atomic conditional write (no TOCTOU), not a read-then-write.
        """
        mock_ddb = MagicMock()
        mock_ddb.update_item.return_value = {}

        _check_per_user_limits(
            user_sub="user-sub-aaa111",
            env_name="dev",
            dynamodb_client=mock_ddb,
        )

        call_kwargs = mock_ddb.update_item.call_args[1]
        self.assertIn(
            "ConditionExpression",
            call_kwargs,
            "DynamoDB UpdateItem must include ConditionExpression for atomic limit enforcement.",
        )


# ---------------------------------------------------------------------------
# Regression — the mock review pipeline's produced output_s3_key must satisfy
# download.py's key-binding contract (AC2). The mock review handler is the
# single swap point that stamps output_s3_key onto the reviews row; it once
# emitted ``outputs/<owner_sub>/<review_id>/out.docx`` while download.py
# enforces exactly ``outputs/<review_id>/...``, so every real
# GET /api/reviews/{id}/output would have 403'd once persist wrote that key.
# This binds the producer and the validator together so they cannot drift.
# ---------------------------------------------------------------------------

class TestMockPipelineKeyMatchesDownloadContract(unittest.TestCase):
    """The output_s3_key the mock review handler produces must pass
    _validate_s3_key_bound_to_review for the same review_id."""

    REVIEW_ID = "00000000-0000-4000-a000-000000000001"
    OWNER_SUB = "owner-sub-aaa111"

    def test_eiaa_output_key_passes_download_validator(self) -> None:
        """The eiaa REQUEST_CHANGE path's output key is downloadable."""
        event = {
            "review_id": self.REVIEW_ID,
            "playbook_id": "eiaa",
            "owner_sub": self.OWNER_SUB,
            "upload_s3_key": f"uploads/{self.OWNER_SUB}/{self.REVIEW_ID}/in.docx",
        }
        result = _mock_review_handler.handler(event)

        output_key = result["output_s3_key"]
        self.assertIsNotNone(output_key, "eiaa path must produce an output key")
        # Must NOT raise — the produced key is scoped to exactly this review's
        # outputs prefix, with no owner_sub segment.
        _validate_s3_key_bound_to_review(output_key, self.REVIEW_ID)
        self.assertEqual(output_key, f"outputs/{self.REVIEW_ID}/out.docx")

    def test_manual_review_paths_produce_no_output_key(self) -> None:
        """The MANUAL_REVIEW_REQUIRED paths carry no downloadable output."""
        for playbook_id in ("nda", "totally-unknown-playbook"):
            event = {
                "review_id": self.REVIEW_ID,
                "playbook_id": playbook_id,
                "owner_sub": self.OWNER_SUB,
            }
            result = _mock_review_handler.handler(event)
            self.assertIsNone(
                result["output_s3_key"],
                f"playbook {playbook_id!r} must not point at a downloadable output",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestNonOwnerDenied))
    suite.addTests(loader.loadTestsFromTestCase(TestKeyBoundToReviewId))
    suite.addTests(loader.loadTestsFromTestCase(TestPresignedUrlExpiry))
    suite.addTests(loader.loadTestsFromTestCase(TestPerUserLimits))
    suite.addTests(loader.loadTestsFromTestCase(TestMockPipelineKeyMatchesDownloadContract))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
