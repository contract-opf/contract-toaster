"""
Download authorization handler for the ContractToaster Review API (issue #71 AC2).

Implements the owner/admin-gated presigned URL path for secure output
file downloads.

Security controls:
  1. Owner/admin check: only the review owner (by cognito_sub) or an admin
     may request a presigned URL for a given review.  Admin privilege is
     read from the DynamoDB `users` row's `is_admin` flag (see
     src/users.py's `_is_admin` / ARCHITECTURE.md -> "Group-naming
     misnomer"), never from a JWT claim — a stale or mis-issued token must
     not be able to grant download access to another user's review output.
     Any other caller receives HTTP 403.
  2. Short-lived presigned URL: the URL expires in PRESIGNED_URL_TTL_SECONDS
     (default 60 s) so a leaked URL has a minimal blast radius.
  3. Cache-Control: no-store: the API response carrying the presigned URL
     is always sent with Cache-Control: no-store so the URL is not persisted
     in any browser or CDN cache.
  4. High-entropy non-enumerable review IDs: review IDs are UUIDs v4 and are
     never returned in list responses to callers who are not owners or admins.
  5. KMS encryption context: SSE-KMS decryption is performed by S3 on the
     server side using the bucket-default CMK.  The presigned URL authorises
     the *caller's* identity; the KMS Decrypt call is made by the S3 service
     using the runtime role that owns the presigned URL.  The encryption
     context ({contract-toaster:data-class, contract-toaster:review-id}) is enforced by the outputs
     CMK key policy (Null-deny in KmsKeysStack) against the outputs role —
     the URL itself does not carry context parameters.

Environment variables consumed:
  OUTPUTS_BUCKET         — name of the outputs S3 bucket
  AWS_REGION             — defaults to us-east-1
  S3_PUBLIC_ENDPOINT_URL — Docker Compose target only (issue #273): host-reachable
                           override for the S3 endpoint presigned URLs are
                           signed against, so a browser on the docker host
                           doesn't need a /etc/hosts entry to reach MinIO at
                           the compose-internal S3_ENDPOINT_URL host. Unset
                           on the AWS target; see config.s3_public_endpoint_url.

DynamoDB owner check:
  The per-user daily download-request limit is enforced by a DynamoDB
  conditional write on the users table (PK: cognito_sub — see
  infra/lib/nested/data-stack.ts) before any presigned URL is issued.
  See: _check_per_user_limits() below.

  Note: there is deliberately no per-user *concurrency* counter here.
  "Max in-flight reviews per user" is a review-*submission*-time concept
  (see src/reviews.py), not a download-time one; a counter incremented on
  every download-URL request with no corresponding decrement would
  permanently lock a user out after a handful of downloads of their own
  finished reviews. The daily-request limit below, together with the
  submission-time daily spend cap (src/reviews.py: reserve_spend), is
  sufficient abuse control at this volume.

Usage (FastAPI dependency injection):
  @app.get("/api/reviews/{review_id}/download")
  async def get_download_url(
      review_id: str,
      caller_row: dict = Depends(get_active_user_row),
      boto3_s3=Depends(get_s3_client),
      boto3_ddb=Depends(get_dynamodb_client),
  ) -> JSONResponse:
      # review_owner_sub and s3_key MUST be derived from the authoritative
      # review record (never from client input) by the route handler.
      return generate_presigned_download_url(
          review_id, review_owner_sub, s3_key, caller_row, env_name,
          boto3_s3, boto3_ddb,
      )
"""

import os
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

try:  # production runs `src.main` (backend/ on path); tests put backend/src on path
    from src import config
except ImportError:  # pragma: no cover
    import config  # type: ignore[no-redef]

# PresignedURL time-to-live: 60 seconds.
# Short-lived so a leaked URL expires quickly; the caller must re-authenticate
# and re-request if more time is needed.
PRESIGNED_URL_TTL_SECONDS = 60

# DynamoDB per-user daily limit (enforced via ConditionExpression on UpdateItem).
# Deliberately no concurrency counter here — see module docstring's
# "DynamoDB owner check" section for why a download-time increment-only
# counter is wrong.
MAX_DAILY_REVIEWS = 20        # max download-URL requests per user per calendar day


def _get_outputs_bucket() -> str:
    """The outputs bucket, for the redline-output download.

    Named after the SAME env var `src/pipeline_runner.py`'s
    `_copy_output_object` / `_write_real_output` write through, so the
    download reads from exactly the bucket the pipeline wrote to; a
    deployment that configured one and not the other fails loudly here
    rather than presigning against the wrong data class (issue #465: this
    used to read an S3-prefixed variant of this name that no writer or
    deploy target ever set, while the pipeline and every deploy target used
    this un-prefixed name — do not re-split the pair).
    """
    bucket = os.environ.get("OUTPUTS_BUCKET", "")
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OUTPUTS_BUCKET not configured.",
        )
    return bucket


def get_uploads_bucket() -> str:
    """The uploads bucket, for the issue-#449 input-document download.

    Named after the SAME env var `src/review_routes.py::_put_upload_object`
    writes through, so the download reads from exactly the bucket the upload
    wrote to; a deployment that configured one and not the other fails loudly
    here rather than presigning against the wrong data class.
    """
    bucket = os.environ.get("UPLOADS_BUCKET", "")
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UPLOADS_BUCKET not configured.",
        )
    return bucket


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """Return True if the caller's users row (looked up by cognito_sub) is an admin.

    NOTE: `is_admin` is a DynamoDB `users`-row flag, never a JWT claim
    (ARCHITECTURE.md -> "Group-naming misnomer"; see src/users.py's
    identically-shaped `_is_admin`). Callers of this module pass in the
    caller's own users row (already fetched by `require_active_user` /
    `get_active_user_row`) rather than trusting a token claim, so admin
    privilege cannot be forged by a stale or crafted JWT.
    """
    return bool(caller_user_row.get("is_admin", False))


def _check_owner_or_admin(
    review_owner_sub: str,
    caller_user_row: dict[str, Any],
) -> None:
    """Raise HTTP 403 if the caller is neither the owner nor an admin.

    Attack path covered (AC5 test scenario):
      A non-owner calling GET /api/reviews/{other_id}/download must receive
      HTTP 403 Forbidden.  This function is the enforcement point.

    Args:
        review_owner_sub: the `cognito_sub` of the user who created the review.
        caller_user_row: the calling user's DynamoDB `users` row (fetched by
            `require_active_user` / `get_active_user_row` — never a raw JWT
            claims dict), used both for identity (`cognito_sub`) and for the
            `is_admin` flag.

    Raises:
        HTTPException(403) if the caller is not the owner and not an admin.
    """
    caller_sub = caller_user_row.get("cognito_sub", "")
    if caller_sub != review_owner_sub and not _is_admin(caller_user_row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Download denied: you are not the owner of this review and "
                "do not have admin privileges."
            ),
        )


def _validate_s3_key_bound_to_review(
    s3_key: str, review_id: str, expected_prefix: str | None = None
) -> None:
    """Reject any S3 key that is not bound to the authorized review (AC2).

    Defence-in-depth for issue #71 AC2 ("scoped download authorization, no path
    traversal / IDOR").  The authoritative caller is expected to *derive* the
    S3 key from the stored review record (never from client input), but this
    function is a second, independent gate so that a key which is not scoped to
    ``outputs/<review_id>/`` can never be presigned even if a future caller is
    wired up incorrectly.

    Enforcement:
      - The key MUST be exactly under the per-review prefix ``outputs/<review_id>/``.
        This binds the object to the authorized review (blocks IDOR to another
        review's outputs and blocks reads outside the outputs data class).
      - The key MUST name a non-empty object below that prefix.
      - The key MUST NOT contain path-traversal (``..``) segments, a leading
        ``/``, or backslashes — so a crafted key cannot escape the prefix.

    Note: S3 treats object keys literally (it does not resolve ``..`` like a
    filesystem), so the strict prefix binding is the primary control; the
    traversal/absolute checks are belt-and-suspenders to keep the contract
    unambiguous and to satisfy the AC2 "no path traversal" criterion.

    Args:
        s3_key: the candidate S3 object key.
        review_id: the UUID v4 review identifier the caller is authorized for.
        expected_prefix: the per-review prefix the key must sit under.
            Defaults to ``outputs/<review_id>/`` — the only data class this
            gate served before issue #449 added the input-document download,
            whose keys are ``uploads/<owner_sub>/<review_id>/`` (the layout
            `review_routes._put_upload_object` writes) and therefore cannot be
            derived from ``review_id`` alone. Whatever prefix is supplied,
            ``review_id`` MUST appear in it as a whole path segment: that is
            what keeps this an independent review-binding gate rather than a
            caller-supplied allowlist, so a mis-wired future caller still
            cannot widen it to another review or another data class.

    Raises:
        HTTPException(403) if the key is not scoped to `expected_prefix`, or
            if `expected_prefix` does not itself bind to `review_id`.
    """
    expected_prefix = expected_prefix or f"outputs/{review_id}/"

    def _reject() -> None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Download denied: the requested object key is not scoped to "
                "this review."
            ),
        )

    # The prefix must genuinely name THIS review. A caller that passed a
    # review-agnostic prefix (say "uploads/") would otherwise turn this gate
    # into a data-class check only, and every one of that class's objects
    # would satisfy it.
    if review_id not in expected_prefix.split("/"):
        _reject()

    if not s3_key.startswith(expected_prefix):
        _reject()

    # Must name a non-empty object below the prefix.
    if len(s3_key) <= len(expected_prefix):
        _reject()

    # Block traversal / absolute / backslash tricks that could escape the prefix.
    if s3_key.startswith("/") or "\\" in s3_key or ".." in s3_key.split("/"):
        _reject()


def _check_per_user_limits(
    user_sub: str,
    env_name: str,
    dynamodb_client: Any,
) -> None:
    """Enforce the per-user daily download-request limit via DynamoDB
    conditional write.

    Uses a DynamoDB UpdateItem with ConditionExpression so that the check and
    the increment are atomic — there is no TOCTOU race.

    Limit enforced:
      - daily_review_count <= MAX_DAILY_REVIEWS (per user, per UTC day)

    Deliberately NOT enforced here: a per-user *concurrency* counter. See
    the module docstring's "DynamoDB owner check" section — "max in-flight
    reviews" belongs to review *submission* (src/reviews.py), and an
    increment-only counter fired on every download request would
    permanently lock a user out with no way to ever decrement it.

    Keys on `cognito_sub` — the users table's actual partition key (see
    infra/lib/nested/data-stack.ts: `partitionKey: { name: 'cognito_sub', ...
    }`). A prior version of this function keyed on `sub`, which does not
    match the table schema and made every call fail with
    ValidationException -> HTTP 503 (all downloads blocked).

    Attack path covered (AC5 test scenario):
      A caller who has reached MAX_DAILY_REVIEWS must receive HTTP 429 Too
      Many Requests.

    Args:
        user_sub: the caller's `cognito_sub`.
        env_name: the deployment environment name (dev/staging/prod).
        dynamodb_client: a boto3 DynamoDB client (injected for testability).

    Raises:
        HTTPException(429) if the daily limit is exceeded.
        HTTPException(503) if the DynamoDB check itself fails.
    """
    # Same resource every other module names through USERS_TABLE
    # (users.py, demo_auth.py). This used to be spelled out as
    # f"contract-toaster-users-{env_name}"; the two happened to agree, but
    # two names for one resource is exactly the drift that made the outputs
    # bucket unreadable for every deployment (issue #465) -- so read the
    # env var and keep the derived name only as the fallback for a caller
    # that has not set it.
    table_name = os.environ.get("USERS_TABLE") or f"contract-toaster-users-{env_name}"
    # The counter is one attribute PER DAY, literally named
    # "dailyReviewCount_2026-08-03". That whole string has to go in the
    # ExpressionAttributeNames placeholder: a DynamoDB "#name" placeholder
    # is a complete path component, never a suffix glued onto a literal
    # prefix, so "dailyReviewCount_#day" is a syntax error the service
    # rejects with ValidationException (issue #528 -- it 503'd every
    # authenticated download, sitting directly behind the #465 bucket 503
    # that used to fire first and hide it).
    today_attr = "dailyReviewCount_" + time.strftime("%Y-%m-%d", time.gmtime())

    try:
        dynamodb_client.update_item(
            TableName=table_name,
            Key={"cognito_sub": {"S": user_sub}},
            # Atomic conditional increment: only proceed when the daily
            # limit is below its threshold.  The ConditionExpression is the
            # actual enforcement; the UpdateExpression increments the
            # counter so subsequent requests see the updated value.
            UpdateExpression="SET #day = if_not_exists(#day, :zero) + :one",
            ExpressionAttributeNames={"#day": today_attr},
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":one": {"N": "1"},
                ":maxDaily": {"N": str(MAX_DAILY_REVIEWS)},
            },
            # If the condition fails, DynamoDB raises ConditionalCheckFailedException.
            ConditionExpression="attribute_not_exists(#day) OR #day < :maxDaily",
        )
    except ClientError as exc:
        err_code = exc.response.get("Error", {}).get("Code", "")
        if err_code == "ConditionalCheckFailedException":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Per-user limit exceeded: max {MAX_DAILY_REVIEWS} download requests "
                    "per day."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to check per-user limits: {exc!r}",
        ) from exc


def generate_presigned_download_url(
    review_id: str,
    review_owner_sub: str,
    s3_key: str,
    caller_user_row: dict[str, Any],
    env_name: str,
    s3_client: Any,
    dynamodb_client: Any,
    *,
    expected_key_prefix: str | None = None,
    bucket_name: str | None = None,
    require_object_exists: bool = False,
) -> JSONResponse:
    """Generate a short-lived presigned URL for an output file download.

    Steps:
      1. Owner/admin check (raises HTTP 403 for non-owner/non-admin callers).
      1b. Key-vs-review-id binding check: the s3_key must be scoped to
         ``outputs/<review_id>/`` — or to `expected_key_prefix` when the
         caller supplies one — with no path traversal (raises HTTP 403
         otherwise — AC2 IDOR / path-traversal defence).
      2. (opt-in) Object-existence check — see `require_object_exists`.
      3. Per-user daily limit check via DynamoDB conditional write (raises
         HTTP 429 when the limit is exceeded).
      4. Generate a presigned GetObject URL with a 60-second TTL, embedding
         the per-review KMS encryption context so the key policy is satisfied.
      5. Return the URL with Cache-Control: no-store so it is not cached.

    The existence check sits between the authorization gates and the quota
    deliberately. After the gates, because an unauthorized or mis-scoped key
    must be refused with 403 without ever having its existence probed. Before
    the quota, because a 410 delivers nothing and so must not be charged a
    download slot — otherwise clicking through purged rows exhausts the very
    limit that protects a user's still-downloadable redlines.

    Args:
        review_id: the UUID v4 review identifier (non-enumerable).
        review_owner_sub: the `cognito_sub` of the user who owns the review.
        s3_key: the S3 object key.  MUST be derived from the authoritative
            review record (never from client input) and MUST be scoped to
            ``outputs/<review_id>/``; this is re-validated here regardless.
        caller_user_row: the calling user's DynamoDB `users` row (e.g. from
            `require_active_user` / `get_active_user_row` — never a raw JWT
            claims dict), used for identity (`cognito_sub`) and the
            `is_admin` flag.
        env_name: the deployment environment name (dev/staging/prod).
        s3_client: a boto3 S3 client (injected for testability).
        dynamodb_client: a boto3 DynamoDB client (injected for testability).
        expected_key_prefix: overrides the per-review prefix `s3_key` is
            validated against (issue #449's input-document download lives
            under ``uploads/<owner_sub>/<review_id>/``).  The prefix must
            itself name `review_id` — see `_validate_s3_key_bound_to_review`.
        bucket_name: overrides the bucket the URL is signed against.  Defaults
            to the outputs bucket, unchanged.
        require_object_exists: when True, HEAD the object first and raise HTTP
            410 Gone if it is not there.  Opt-in rather than always-on because
            this function's contract is "presign a key", and the extra S3 round
            trip is only worth paying where a MISSING object is an expected,
            meaningful state rather than an error — which is exactly the case
            once retention starts purging documents out from under review rows
            that still carry their pointers (issue #449).  Without it, a purged
            review hands the browser a valid-looking URL that 404s on click; a
            dead link is a worse answer than "no longer available".

    Returns:
        JSONResponse with {"url": "<presigned-url>", "expires_in": 60} and
        Cache-Control: no-store header.

    Raises:
        HTTPException(403) if the caller is not the owner or admin, or if the
            s3_key is not scoped to the expected per-review prefix (IDOR /
            traversal).
        HTTPException(410) if `require_object_exists` and the object is gone.
        HTTPException(429) if the per-user daily limit is exceeded.
        HTTPException(503) if S3 or DynamoDB operations fail.
    """
    # Step 1: owner/admin check.
    _check_owner_or_admin(review_owner_sub, caller_user_row)

    # Step 1b: key-vs-review-id binding (AC2 — no path traversal / IDOR).
    # The s3_key MUST belong to this review's own prefix.  Callers are
    # required to derive s3_key from the authoritative review record, not from
    # client input; this is an independent gate so an incorrectly-wired caller
    # still cannot presign a key scoped to another review or data class.
    _validate_s3_key_bound_to_review(s3_key, review_id, expected_key_prefix)

    bucket = bucket_name or _get_outputs_bucket()

    # Step 2 (issue #449): does the object still exist?
    #
    # Ordered AFTER the owner/admin and key-binding gates, so an unauthorized
    # or mis-scoped key is still refused with 403 and never gets so far as to
    # have its existence probed — nothing is disclosed here that those gates
    # had not already let through.
    #
    # Ordered BEFORE the per-user daily limit, which is the other half of the
    # ordering argument and was got wrong first time round: a Gone answer
    # delivers no bytes, so it must not spend one of the caller's 20 daily
    # download slots. It previously did, which meant a user with a page of
    # purged rows could exhaust — by clicking on documents that no longer
    # exist — the very quota that protects their still-downloadable redlines.
    # (Proven by tests/test_review_history_449.py::
    # TestGoneIsNotChargedAgainstTheDailyLimit.)
    #
    # The reorder does not weaken the limit: HEAD is a cheap, read-only,
    # already-authorized probe of one specific key the caller owns, and every
    # request that actually yields a URL still passes through the counter
    # below.
    if require_object_exists:
        try:
            s3_client.head_object(Bucket=bucket, Key=s3_key)
        except ClientError as exc:
            # Only a genuine "it isn't there" is Gone.  Anything else (a
            # throttle, an IAM problem, an outage) is a 503: reporting a
            # transient failure as a permanent deletion would tell a user their
            # document had been purged when it had not.
            err_code = exc.response.get("Error", {}).get("Code", "")
            http_status = (
                exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            )
            if err_code in ("404", "NoSuchKey", "NotFound") or http_status == 404:
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail=(
                        "This document is no longer available. Files are removed "
                        "once their retention window has passed."
                    ),
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Unable to check whether the document is still available: {exc!r}",
            ) from exc

    # Step 3: per-user daily limit (DynamoDB conditional write with ConditionExpression).
    _check_per_user_limits(
        user_sub=caller_user_row["cognito_sub"],
        env_name=env_name,
        dynamodb_client=dynamodb_client,
    )

    # Step 4: generate presigned URL.
    # For SSE-KMS: the S3 service performs the KMS Decrypt call server-side
    # using the outputs role (the role that signed the presigned URL).  The
    # KMS encryption context enforcement ({contract-toaster:data-class, contract-toaster:review-id})
    # is carried by the CMK key policy DENY applied to the outputs role in
    # KmsKeysStack — not by parameters embedded in the presigned URL itself.
    # The presigned URL authorises the caller's GET; the key policy enforces
    # that the outputs role was granted only for the correct context.
    #
    # Docker Compose target only (issue #273): a presigned URL is host-bound (the SigV4
    # signature commits to the endpoint host used at generation time). The
    # injected s3_client above reaches MinIO at the compose-internal
    # S3_ENDPOINT_URL=http://minio:9000 -- a browser on the docker host
    # cannot resolve `minio` there. When S3_PUBLIC_ENDPOINT_URL is set,
    # presign with a dedicated client pointed at the host-reachable endpoint
    # instead, so the returned URL is followable with zero /etc/hosts edits.
    # Every other S3 call in this deployment (upload, bootstrap) is
    # unaffected -- only THIS presigning step swaps endpoints.
    # AWS target: the var is unset, no second client is built, and
    # presigning uses s3_client exactly as before -- byte-identical.
    presigning_client = s3_client
    if config.s3_public_endpoint_url():
        presigning_client = boto3.client("s3", **config.presigning_s3_client_kwargs())

    try:
        presigned_url = presigning_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": s3_key,
            },
            ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
        )
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to generate presigned URL: {exc!r}",
        ) from exc

    # Step 4: return with Cache-Control: no-store.
    return JSONResponse(
        content={
            "url": presigned_url,
            "expires_in": PRESIGNED_URL_TTL_SECONDS,
            "review_id": review_id,
        },
        headers={
            # Prevent the presigned URL from being cached by any intermediary.
            "Cache-Control": "no-store",
        },
    )
