"""
Deployment-target configuration seam (Docker Compose deployment, Phase 1).

Contract-toaster runs against two deployment targets from ONE codebase:

  - `aws`  (default): App Runner + real S3 / DynamoDB / Step Functions /
    Cognito / Bedrock, selected by leaving the env vars below unset. Behavior
    is byte-identical to before this module existed.
  - `dts`: Docker Compose with S3 -> MinIO, DynamoDB -> DynamoDB-Local, and a
    direct model-provider API (OpenRouter), selected purely by environment
    variables at process start.

This module centralizes the handful of env reads that select adapters. It is
deliberately a set of **live-reading functions** (not a frozen-at-import
settings object): the existing boto3 factories read env lazily, and the test
suite selects behavior per-test with `patch.dict(os.environ, ...)`, so reading
the environment at call time keeps both working.

The ONLY behavior this module changes for the AWS target is: when no endpoint
override is configured, `boto3_client_kwargs` returns exactly
`{"region_name": ...}` -- the same kwargs the ad-hoc factories passed before.
"""

import os

# Env var name carrying a per-service endpoint override, keyed by boto3
# service name. A local emulator (MinIO for S3, DynamoDB-Local for DynamoDB)
# lives at its own host, so the endpoints are per-service, not one shared URL.
_SERVICE_ENDPOINT_ENV = {
    "s3": "S3_ENDPOINT_URL",
    "dynamodb": "DYNAMODB_ENDPOINT_URL",
    "stepfunctions": "STEPFUNCTIONS_ENDPOINT_URL",
}


def region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def deploy_target() -> str:
    """`aws` (default) or `dts`."""
    return os.environ.get("DEPLOY_TARGET", "aws").strip().lower()


def auth_mode() -> str:
    """Deployment-level auth mode selecting which verifier(s) `get_current_user`
    uses: `sso` (default -- Cognito only, the AWS target), `password` (demo
    tokens only, the Docker Compose target), or `both`.

    This is distinct from the admin-toggleable auth-mode row in
    demo_auth.py, which gates whether password *login* is currently allowed.
    This value selects which token verifiers are *wired* for the deployment
    and is fixed by env at process start, so `get_current_user` needs no
    DynamoDB read per request.
    """
    return os.environ.get("AUTH_MODE", "sso").strip().lower()


def pipeline_runner() -> str:
    """Which pipeline transport `review_routes.get_sfn_client` returns:
    `stepfunctions` (default, the AWS target -- a real boto3 Step Functions
    client) or `inprocess` (the Docker Compose target -- an in-container background-worker
    client that runs the review pipeline in-process)."""
    return os.environ.get("PIPELINE_RUNNER", "stepfunctions").strip().lower()


def model_provider() -> str:
    """Which model backend the Docker Compose in-process pipeline runner uses: `mock`
    (default -- Phase 1's canned/pre-baked review, see
    pipeline_runner.run_mock_pipeline) or `openrouter` (Phase 2 -- the real
    scripts/ review spine driven by a live OpenRouterModelClient, see
    pipeline_runner.run_real_pipeline). Anything else (including unset)
    keeps the default mock path, so existing callers/tests that never set
    this var are unaffected."""
    return os.environ.get("MODEL_PROVIDER", "mock").strip().lower()


def structured_output_enabled() -> bool:
    """Issue #418: forces the OpenRouter request to carry the review object
    as a tool call (forced `tool_choice`) instead of prose JSON, making the
    prose-preamble / markdown-fence failure mode `_extract_json_object`
    exists to paper over (#382) structurally impossible.

    Default ON since issue #673. #418 shipped this default OFF pending the
    live measurement its own Notes demanded; #673 is that measurement --
    same synthetic document, same pinned models, same bound playbook, one
    variable, run through `scripts/live_smoke_eval.py --structured-output
    both` against real OpenRouter traffic. With the flag OFF the critic
    pass failed schema validation on two consecutive attempts and the
    review terminated `ERROR_MANUAL_REVIEW_REQUIRED` (`reason: "critic"` --
    the same token production recorded); with it ON both passes validated
    on their FIRST attempt, at 60.9s / $0.55 against 128.5s / $0.74. The
    OFF-path critic invented a `grounding` property that appears nowhere in
    the critic prompt or in `output-schema-v3.json`, against an
    `additionalProperties: false` schema -- exactly the failure class
    forced tool-use makes structurally impossible.

    On #418's open question -- whether OpenRouter's Anthropic pass-through
    actually honours `tool_choice` or silently drops to the prose path --
    the evidence is BEHAVIOURAL, not a recorded provider assertion: nothing
    in the request/response plumbing reports "the tool was honoured", and a
    tool-mode call whose response comes back as plain `content` still
    parses via the fallback (`OpenRouterModelClient.invoke`). What was
    measured is that both passes validated first-try with the flag on and
    the critic failed twice with it off, on the same document. That is why
    the flag stays.

    The flag survives as the ROLLBACK, and as #418's A/B seam: setting
    `OPENROUTER_STRUCTURED_OUTPUT` to `0`/`false`/`no`/`off` (any case)
    restores the prose-JSON request byte-for-byte -- no `tools`/
    `tool_choice` fields, no `tool_spec` kwarg reaching the client at all
    (see `scripts/primary_review_pass.py::run_primary_pass` /
    `scripts/critic_review_pass.py::run_critic_pass`, which thread it only
    when this is True). Unset, empty, or any other value is ON. The
    explicit-off spelling set follows the same default-ON convention
    `backend/src/purge_scheduler.py::scheduler_enabled` already uses.

    Read live per call (never cached), so `live_smoke_eval.py`'s A/B and a
    test's `patch.dict(os.environ, ...)` both flip it the same way
    production's deployment env does.
    """
    return os.environ.get("OPENROUTER_STRUCTURED_OUTPUT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def notes_mode_enabled() -> bool:
    """Issue #572: the kill switch that makes epic #519's "ship all of A-G
    together, or none" deploy gate a real mechanism instead of a human
    promise. Gates ACCEPTANCE of the `internal` / `both` notes modes at
    `src.reviews.resolve_notes_mode` -- while this is off, a submission
    requesting either is refused with a `ValueError` (routed to a 400 by
    `src.review_routes`), never silently downgraded to `external`.

    Default OFF: `NOTES_MODE_ENABLED` unset (or set to anything other than
    `1`/`true`/`yes`) keeps `resolve_notes_mode`'s behavior byte-identical to
    today -- `none` and `external` are unaffected (they cannot surface
    internal reasoning, so they carry none of the risk this gate exists
    for), and `internal`/`both` are refused exactly as an unrecognized value
    already is. Read once at this module seam (the same live-env-read
    convention as `structured_output_enabled` above), never cached, so a test can flip it per-case with
    `patch.dict(os.environ, ...)`.

    Flipping the default ON is a deployment-config decision made AFTER
    every one of #516, #521, #522, #523, #513 and #524 has landed and been
    verified -- landing any subset while this stays off is exactly the
    incremental-and-safe shape the epic calls for; flipping it early would
    let internal reasoning reach counterparty-facing footnotes before the
    audience-aware leakage scan (#521) exists to stop it. This function's
    default must not change without that evidence, exactly like
    `structured_output_enabled` above -- whose default DID move, ON, in
    issue #673, but only once a live A/B against real OpenRouter traffic
    had been run and written down. The convention borrowed from it is the
    live per-call env read and that evidence bar, not the direction of the
    default.
    """
    return os.environ.get("NOTES_MODE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def endpoint_url(service: str) -> str | None:
    """The endpoint override for a boto3 service, or None for the real AWS
    endpoint. Checks the per-service var first (e.g. `S3_ENDPOINT_URL`), then
    a shared `AWS_ENDPOINT_URL` fallback. Empty string counts as unset."""
    specific = os.environ.get(_SERVICE_ENDPOINT_ENV.get(service, ""), "").strip()
    if specific:
        return specific
    shared = os.environ.get("AWS_ENDPOINT_URL", "").strip()
    return shared or None


def s3_public_endpoint_url() -> str | None:
    """Host-reachable override for the S3 *presigning* endpoint (Docker Compose target
    only), or None when unset. Empty/whitespace-only counts as unset.

    Presigned URLs are host-bound: the SigV4 signature commits to the
    endpoint host used at generation time. The Docker Compose target's backend reaches
    MinIO at `S3_ENDPOINT_URL=http://minio:9000` (the compose-internal DNS
    name) for every other S3 call, but a browser on the docker host cannot
    resolve `minio` -- without this seam, downloading required a manual
    `/etc/hosts` entry (issue #273). When set, `S3_PUBLIC_ENDPOINT_URL`
    overrides ONLY the endpoint used to presign download URLs (see
    `download.generate_presigned_download_url` /
    `presigning_s3_client_kwargs` below); every other S3 call (upload,
    bootstrap) keeps using `S3_ENDPOINT_URL`/`endpoint_url("s3")` unchanged.
    Unset (the AWS target, and any deployment that doesn't need the split) ->
    presigning uses the same client as every other S3 call; behavior is
    byte-identical to before this var existed.
    """
    return os.environ.get("S3_PUBLIC_ENDPOINT_URL", "").strip() or None


def presigning_s3_client_kwargs() -> dict[str, str]:
    """Like `boto3_client_kwargs("s3")`, but `endpoint_url` is overridden by
    `s3_public_endpoint_url()` when set. Used only to build the dedicated
    client `download.generate_presigned_download_url` presigns with; the
    client used for every other S3 call is unaffected.

    When `S3_PUBLIC_ENDPOINT_URL` is unset, returns exactly
    `boto3_client_kwargs("s3")` -- byte-identical to the AWS path.
    """
    kwargs = boto3_client_kwargs("s3")
    public_override = s3_public_endpoint_url()
    if public_override:
        kwargs["endpoint_url"] = public_override
        if "aws_access_key_id" not in kwargs and not os.environ.get("AWS_ACCESS_KEY_ID"):
            # Local emulators accept any non-empty credentials.
            kwargs["aws_access_key_id"] = "local"
            kwargs["aws_secret_access_key"] = "local"  # noqa: S105 (not a real secret)
    return kwargs


def boto3_client_kwargs(service: str) -> dict[str, str]:
    """Build the kwargs for `boto3.client(service, ...)` /
    `boto3.resource(service, ...)`.

    - Always sets `region_name`.
    - When an endpoint override is configured for the service (Docker Compose: MinIO /
      DynamoDB-Local), also sets `endpoint_url` and -- unless real credentials
      are already present in the environment -- dummy static credentials
      (local emulators require *some* credentials but do not validate them).

    With no override configured (the AWS target), returns exactly
    `{"region_name": region()}` -- unchanged from the previous ad-hoc
    factories, so the AWS path and every AWS-asserting test are unaffected.
    """
    kwargs: dict[str, str] = {"region_name": region()}
    override = endpoint_url(service)
    if override:
        kwargs["endpoint_url"] = override
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            # Local emulators accept any non-empty credentials.
            kwargs["aws_access_key_id"] = "local"
            kwargs["aws_secret_access_key"] = "local"  # noqa: S105 (not a real secret)
    return kwargs
