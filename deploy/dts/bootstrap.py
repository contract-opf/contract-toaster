#!/usr/bin/env python3
"""
Docker Compose bootstrap — one-shot job that provisions the local emulators before the
backend starts (docker-compose `bootstrap` service).

Idempotent: creating a table/bucket that already exists is a no-op, so
`docker compose up` can be re-run safely.

Does four things against DynamoDB-Local + MinIO (endpoints from env, via
config.boto3_client_kwargs):
  1. Create the DynamoDB tables the backend reads, with the GSIs it queries
     (reviews.owner_sub-index, review_submissions.review_id-index) -- and, for
     a table that ALREADY exists, converge it onto that declared index set
     (issue #446). Creating is not enough: a table provisioned before an index
     was declared here would otherwise be skipped forever, which is how a live
     deployment ran without review_id-index and failed every spend settle.
  2. Create the uploads/outputs S3 buckets.
  3. Seed every registry-declared mock-pipeline redline fixture into the
     outputs bucket (the keys `_mock_decision` copies from).
  4. Seed the demo users (admin/admin, user/user). No playbook row is seeded
     here: step 5 installs the registry-backed sample, which is what
     submit_review's active-bundle check actually resolves (issue #515
     removed a hardcoded tenant-named orphan row that nothing served).
  5. Install and activate the playbook the image ships with (issue #433), so a
     fresh deployment never comes up with an empty catalog. This is the ONLY
     way the shipped playbook gets installed -- there is no bespoke
     activate-the-sample route or button any more; it goes through the same
     src.playbook_versions upload/activate functions an admin-uploaded
     version does, and is an ordinary playbook from then on.
  6. Backfill `activated_at` (issue #462) onto any `playbook_versions` row
     left over from before that attribute existed. `activate_playbook_
     version` has stamped it on every row it activates since #462 landed,
     but step 5 above only writes on a FRESH deployment -- a table carried
     forward from an earlier deploy never gets a stamping pass otherwise,
     which would make rollback (which now requires `activated_at`) refuse
     every pre-existing version forever. See `backfill_activated_at_462`.

Run: python3 deploy/dts/bootstrap.py   (PYTHONPATH must include backend/)
"""

import os
import sys
import time
from pathlib import Path
from typing import Optional

import boto3

# backend/src on the path so we can reuse config + the demo-user seeder.
_APP_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _APP_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from src import config, demo_auth, playbook_versions, sample_playbooks  # noqa: E402

# scripts/ for the registry that names WHICH playbook a deployment installs --
# a registry field, never a playbook_id literal here (issue #289's
# type-blindness convention). backend/src/sample_playbooks.py already inserts
# this path; doing it explicitly keeps the dependency visible.
_SCRIPTS = _APP_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import playbook_registry  # noqa: E402

# Issue #515: the mock-pipeline fixture key is resolved from the REGISTRY, not
# spelled out here. It used to be two hardcoded tenant-named literals, in the
# most public-facing path in the repo -- exactly what an adopter copies and
# runs -- and neither of the two guards could see them: the brand-free gate
# does not know that term, and the tenant-literal lint did not scan `deploy/`.
# Two guards, one blind spot each, and the leak sat in the overlap.
#
# `backend/src/pipeline_runner.py::_mock_decision` already resolves this key
# the same way, so the seeder and the consumer now read one source instead of
# agreeing by hand.
_MOCK_OUTPUT_ROOT = _APP_ROOT / "infra" / "fixtures" / "mock-outputs"

# Table name (env var) -> key schema. `gsis` is a list of
# (index_name, hash_attr, range_attr) tuples.
_TABLES = [
    ("USERS_TABLE", "cognito_sub", None, []),
    ("REVIEWS_TABLE", "review_id", None, [("owner_sub-index", "owner_sub", "created_at")]),
    # review_id-index mirrors infra/lib/nested/data-stack.ts: pipeline_runner's
    # _find_submission_by_review_id queries it to settle a review's spend
    # reservation. Without it every settle raises ValidationException ("table
    # does not have the specified index") -- DTS-only, since the CDK stack has
    # always had the index.
    ("REVIEW_SUBMISSIONS_TABLE", "idempotency_key", None, [("review_id-index", "review_id", None)]),
    ("DAILY_SPEND_TABLE", "spend_date", None, []),
    ("AUDIT_TABLE", "partition", "timestamp", []),
    ("AUTH_SETTINGS_TABLE", "setting_id", None, []),
    ("PLAYBOOKS_TABLE", "playbook_id", None, []),
    ("PLAYBOOK_VERSIONS_TABLE", "playbook_id", "version", []),
    # Standing instructions (issue #482, epic #481) -- append-only,
    # monotonically-versioned per-playbook free-text overrides. Unlike
    # playbook_versions' admin-supplied string `version` (e.g. "1.0.0"),
    # this table's `version` is a plain monotonic Number (see
    # src/playbook_instructions.py) -- _RANGE_KEY_NUMERIC_TABLES below picks
    # that up so this table's range key is provisioned as type N, matching
    # infra/lib/nested/data-stack.ts's `dynamodb.AttributeType.NUMBER`.
    ("PLAYBOOK_INSTRUCTIONS_TABLE", "playbook_id", "version", []),
    ("RETENTION_SETTINGS_TABLE", "setting_id", None, []),
    ("MODEL_SETTINGS_TABLE", "setting_id", None, []),
    ("SYNC_STATUS_TABLE", "sync_type", None, []),
    # Model-invocation ledger (issue #414) -- metadata-only record of every
    # model-invocation attempt the primary/critic passes make. Sort key is a
    # plain String (src/invocation_ledger.py's "{pass_name}#{attempt:02d}#
    # {timestamp}"), so this needs no entry in _RANGE_KEY_NUMERIC_TABLES.
    ("MODEL_INVOCATIONS_TABLE", "review_id", "record_id", []),
]


def _ddb_client():
    return boto3.client("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def _ddb_resource():
    return boto3.resource("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def _s3_client():
    return boto3.client("s3", **config.boto3_client_kwargs("s3"))


# Tables whose RANGE key is a Number rather than this helper's default
# String -- every other table's range key (e.g. playbook_versions' admin-
# supplied "1.0.0" string, audit's "epoch#event_id" string) is a String, so
# this stays a short, explicit exception list rather than a new column on
# every `_TABLES` tuple.
_RANGE_KEY_NUMERIC_TABLES = {"PLAYBOOK_INSTRUCTIONS_TABLE"}


def _attr_defs(hash_attr, range_attr, gsis, range_type="S"):
    types = {hash_attr: "S"}
    if range_attr:
        types[range_attr] = range_type
    for _, gh, gr in gsis:
        types.setdefault(gh, "S")
        if gr:
            types.setdefault(gr, "S")
    return [{"AttributeName": n, "AttributeType": t} for n, t in sorted(types.items())]


def _key_schema(hash_attr, range_attr):
    schema = [{"AttributeName": hash_attr, "KeyType": "HASH"}]
    if range_attr:
        schema.append({"AttributeName": range_attr, "KeyType": "RANGE"})
    return schema


def _gsi_definition(idx, hash_attr, range_attr):
    return {
        "IndexName": idx,
        "KeySchema": _key_schema(hash_attr, range_attr),
        "Projection": {"ProjectionType": "ALL"},
    }


def _existing_gsi_names(client, name) -> set:
    table = client.describe_table(TableName=name)["Table"]
    return {idx["IndexName"] for idx in table.get("GlobalSecondaryIndexes") or []}


def _wait_for_gsi_active(client, name, index_name) -> None:
    """Block until a just-created GSI reports ACTIVE.

    Real DynamoDB backfills a new index asynchronously and refuses a second
    index creation while one is still building, so this is what makes adding
    several indexes in one pass safe. DynamoDB-Local reports ACTIVE
    immediately, which is why the status is checked BEFORE the first sleep.
    """
    timeout_seconds = int(os.environ.get("BOOTSTRAP_GSI_TIMEOUT_SECONDS", "600"))
    deadline = time.time() + timeout_seconds
    while True:
        table = client.describe_table(TableName=name)["Table"]
        for idx in table.get("GlobalSecondaryIndexes") or []:
            # DynamoDB-Local omits IndexStatus entirely; an index it reports at
            # all is usable, so a missing status counts as ACTIVE.
            if idx["IndexName"] == index_name and idx.get("IndexStatus", "ACTIVE") == "ACTIVE":
                return
        if time.time() > deadline:
            raise RuntimeError(
                f"index {index_name} on table {name} did not become ACTIVE within "
                f"{timeout_seconds}s (override with BOOTSTRAP_GSI_TIMEOUT_SECONDS)."
            )
        time.sleep(2)


def _converge_gsis(client, name, hash_attr, range_attr, gsis) -> None:
    """Bring an EXISTING table's indexes up to the declared set (issue #446).

    `create_table` only ever provisions indexes for a table that does not yet
    exist. A table created before an index was declared here is reported
    "already exists" and skipped -- so the declaration only ever helped a
    FRESH deployment, and an existing one could never converge no matter how
    many times it was redeployed. That is exactly how a live deployment ran
    without `review_id-index`, where every spend settle raised
    ValidationException.

    Since the bootstrap runs on every `docker compose up`, doing the diff here
    is the one mechanism that can repair a deployment in place. Adding an
    index is the only convergence performed: it is additive and safe. Indexes
    present on the table but no longer declared are left ALONE -- dropping
    data-bearing infrastructure is not something a boot script should do
    unattended. Once converged, re-running is a plain no-op.
    """
    if not gsis:
        return
    for idx, gh, gr in gsis:
        if idx in _existing_gsi_names(client, name):
            continue
        print(f"    adding missing index {idx} to {name} …")
        client.update_table(
            TableName=name,
            # UpdateTable requires the key attributes of the index being
            # created, and only those -- the table's own key attributes are
            # already defined.
            AttributeDefinitions=_attr_defs(gh, gr, []),
            GlobalSecondaryIndexUpdates=[{"Create": _gsi_definition(idx, gh, gr)}],
        )
        _wait_for_gsi_active(client, name, idx)
        print(f"    index {idx} on {name} is ACTIVE")


def create_tables() -> None:
    client = _ddb_client()
    for env_var, hash_attr, range_attr, gsis in _TABLES:
        name = os.environ.get(env_var)
        if not name:
            print(f"  skip {env_var} (unset)")
            continue
        range_type = "N" if env_var in _RANGE_KEY_NUMERIC_TABLES else "S"
        kwargs = {
            "TableName": name,
            "AttributeDefinitions": _attr_defs(hash_attr, range_attr, gsis, range_type),
            "KeySchema": _key_schema(hash_attr, range_attr),
            "BillingMode": "PAY_PER_REQUEST",
        }
        if gsis:
            kwargs["GlobalSecondaryIndexes"] = [
                _gsi_definition(idx, gh, gr) for idx, gh, gr in gsis
            ]
        try:
            client.create_table(**kwargs)
            print(f"  created table {name}")
        except client.exceptions.ResourceInUseException:
            print(f"  table {name} already exists")
            _converge_gsis(client, name, hash_attr, range_attr, gsis)


def create_buckets() -> None:
    client = _s3_client()
    for env_var in ("UPLOADS_BUCKET", "OUTPUTS_BUCKET"):
        name = os.environ.get(env_var)
        if not name:
            print(f"  skip {env_var} (unset)")
            continue
        try:
            client.create_bucket(Bucket=name)
            print(f"  created bucket {name}")
        except Exception as exc:  # BucketAlreadyOwnedByYou / BucketAlreadyExists
            if "AlreadyExists" in type(exc).__name__ or "AlreadyOwned" in type(exc).__name__:
                print(f"  bucket {name} already exists")
            else:
                # MinIO returns these as ClientError; treat idempotently.
                print(f"  bucket {name}: {type(exc).__name__} (assuming exists)")


def seed_fixture() -> None:
    """Seed every registry-declared mock-pipeline fixture into the outputs
    bucket (issue #515: registry-derived, never a playbook_id literal here).

    Only entries that actually declare a `mock_output_key` are seeded, so a
    playbook with no canned fixture is silently skipped rather than seeded
    under a guessed key -- which is the same thing `_mock_decision` does with
    the same field, from the other side.

    The local path is derived from the key's own middle segment: the S3 layout
    (`mock-fixtures/<name>/…`) and the on-disk layout
    (`infra/fixtures/mock-outputs/<name>/…`) are two spellings of one
    arrangement, and reading the name out of the key is what stops this file
    needing to know any playbook's name.
    """
    bucket = os.environ.get("OUTPUTS_BUCKET")
    if not bucket:
        return
    try:
        import playbook_registry
    except ImportError:
        print("  WARNING: playbook_registry unavailable; no mock fixtures seeded")
        return

    seeded = 0
    for playbook_id in playbook_registry.list_playbook_ids():
        key = playbook_registry.resolve_playbook(playbook_id).mock_output_key
        if not key:
            continue
        parts = key.split("/")
        if len(parts) < 3:
            print(f"  WARNING: unrecognised mock_output_key layout: {key}")
            continue
        source = _MOCK_OUTPUT_ROOT / parts[-2] / parts[-1]
        if not source.exists():
            print(f"  WARNING: fixture not found at {source}; mock downloads will 404")
            continue
        _s3_client().put_object(Bucket=bucket, Key=key, Body=source.read_bytes())
        print(f"  seeded {key} into {bucket}")
        seeded += 1
    if seeded == 0:
        print("  no registry entry declares a mock fixture; nothing seeded")


def seed_users_and_playbook() -> None:
    ddb = _ddb_resource()
    demo_auth.seed_demo_users(ddb)
    print("  seeded demo users (admin/admin, user/user)")

    # Enable password sign-in in the ADMIN-TOGGLEABLE auth-mode row that
    # demo_auth.login_with_password gates on. This is distinct from the
    # deployment-level AUTH_MODE env (which get_current_user's verifier
    # dispatch uses): without seeding this DynamoDB row, login_with_password
    # defaults to sso-only and rejects password sign-in. Seed it to the
    # deployment's AUTH_MODE (password/both); default to password for Docker Compose.
    settings_table = os.environ.get("AUTH_SETTINGS_TABLE")
    deploy_mode = config.auth_mode()
    login_mode = deploy_mode if deploy_mode in ("password", "both") else "password"
    if settings_table:
        ddb.Table(settings_table).put_item(
            Item={
                "setting_id": demo_auth.AUTH_MODE_SETTING_ID,
                "auth_mode": login_mode,
                "updated_at": str(int(time.time())),
            }
        )
        print(f"  set auth-mode setting to '{login_mode}' (password login enabled)")

    # Issue #515: the hardcoded tenant-named playbook row that used to be
    # seeded here is GONE, with no replacement.
    #
    # It was vestigial. The catalog is registry-filtered, so `GET /api/playbooks`
    # never served it -- the row simply sat in PLAYBOOKS_TABLE of every fresh
    # deployment, tenant-named, in the path a public adopter copies and runs.
    # `seed_shipped_playbook()` below installs and activates the real sample,
    # which is what `resolve_active_release_bundle_hash` actually resolves.
    #
    # Owner decision 2026-08-03: the tenant's own playbook is a bonus that
    # arrives through the ordinary install/upload path (#478/#485), from the
    # playbook-engine repo. Nothing about it needs a bootstrap seed here, and
    # the empty-shell architecture assumes it does not have one.


def seed_shipped_playbook() -> None:
    """Install + activate the playbook the image ships with (issue #433).

    Idempotent in the strong sense: `sample_playbooks.seed_shipped_playbook`
    installs only into a FRESH deployment and skips once the playbook has
    version rows, or once an admin has removed it -- so re-running the
    bootstrap on every `docker compose up` can never stomp admin state or
    resurrect a removed playbook. Which playbook_id gets installed comes
    from the registry's `default_playbook_id`, never a literal here.

    The two documented refusals (nothing shippable registered; on-disk
    content that fails runtime validation) are reported and survived rather
    than crashing the stack -- the app's own empty-shell state is a
    supported, documented condition (reviews are refused with 503 "no active
    playbook" rather than silently mis-served). Anything else -- a table
    that isn't there, a DynamoDB error -- propagates and fails the bootstrap
    loudly, because that is a broken deployment, not an empty one.
    """
    playbook_id = playbook_registry.default_playbook_id()
    try:
        result = sample_playbooks.seed_shipped_playbook(playbook_id, _ddb_resource())
    except (
        sample_playbooks.SampleNotAvailableError,
        sample_playbooks.SampleInvalidError,
    ) as exc:
        print(f"  WARNING: could not install the shipped playbook: {type(exc).__name__}: {exc}")
        return
    if result["status"] == "active":
        print(f"  installed and activated shipped playbook {playbook_id!r}")
    else:
        print(f"  shipped playbook {playbook_id!r} not installed ({result['reason']})")


def backfill_activated_at_462() -> None:
    """One-time convergence for issue #462: stamp a durable `activated_at`
    fact onto any `playbook_versions` row written before that attribute
    existed.

    `activate_playbook_version` has stamped `activated_at` on every row it
    activates since issue #462 landed, and `rollback_playbook_version` now
    refuses a rollback target that lacks one (see docs/playbook-governance.md
    "Gate 7 on rollback"). But `sample_playbooks.seed_shipped_playbook` only
    ever writes on a FRESH deployment -- it returns
    `_skip(..., "already_installed")` on every subsequent bootstrap -- so
    nothing re-stamps a row that already existed before this change
    deployed. Without this backfill, every pre-existing `active`/`retired`
    row (the shipped v1.0.0, any admin-uploaded version activated before
    today) would be permanently ineligible for rollback: rollback would go
    from "silently ineffective" (the #462 bug) to "impossible" for every
    version already on a live deployment.

    `status in (active, retired)` is treated as "was previously active" for
    a row with no `activated_at` -- both statuses are only ever reached via
    `activate_playbook_version`, so this recovers the fact that attribute
    would already record had it existed at the time, rather than guessing.
    The stamped value is the time of this backfill (the true original
    activation time was never recorded) -- only the attribute's presence,
    not its exact value, is what rollback-eligibility and issue #476's
    "show Roll back only when previously-activated" UI flag key off of.

    Runs on every bootstrap. Idempotent: a row that already carries
    `activated_at` (including one just stamped by step 5's fresh-install
    path) is left untouched, and a race against a concurrent bootstrap is
    resolved by the update's own conditional check, so re-running -- or
    running against a table restored from an older backup -- is always
    safe.
    """
    table_name = os.environ.get("PLAYBOOK_VERSIONS_TABLE")
    if not table_name:
        return
    table = _ddb_resource().Table(table_name)

    eligible_statuses = (playbook_versions.STATUS_ACTIVE, playbook_versions.STATUS_RETIRED)
    now = int(time.time())
    stamped = 0
    scan_kwargs: dict = {}
    while True:
        page = table.scan(**scan_kwargs)
        for item in page.get("Items", []):
            if item.get("activated_at") is not None:
                continue
            if item.get("status") not in eligible_statuses:
                continue
            try:
                table.update_item(
                    Key={"playbook_id": item["playbook_id"], "version": item["version"]},
                    UpdateExpression="SET activated_at = :now",
                    ConditionExpression="attribute_not_exists(activated_at)",
                    ExpressionAttributeValues={":now": now},
                )
                stamped += 1
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                # Another bootstrap (or this one, on a later loop) already
                # stamped it between the scan and this write -- fine.
                pass
        if "LastEvaluatedKey" not in page:
            break
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    if stamped:
        print(
            f"  backfilled activated_at onto {stamped} legacy playbook_versions "
            "row(s) (issue #462)"
        )


def wait_for_services(timeout_seconds: Optional[int] = None) -> None:
    """Block until DynamoDB-Local and MinIO accept connections (the compose
    `depends_on: service_started` only waits for the container to start, not for
    the service inside it to be ready).

    These are deliberately APPLICATION-level probes (ListTables / ListBuckets),
    not HTTP liveness checks, and that distinction matters: DynamoDB-Local keeps
    answering on its port even when its storage engine is dead (a bare GET still
    returns 400 while every real request hangs), so an HTTP-level container
    healthcheck reports "healthy" through exactly the failures this needs to
    catch. Only a real API call proves readiness.

    The budget is PER SERVICE. It used to be one shared deadline computed once
    for the whole loop, so a slow DynamoDB-Local silently ate MinIO's budget and
    then blamed MinIO -- "MinIO not ready within 60s" after MinIO had been given
    five.

    Default 120s (override with BOOTSTRAP_WAIT_TIMEOUT_SECONDS): DynamoDB-Local
    is a JVM starting from cold, and 60s is not enough on a loaded machine --
    e.g. while docker is still building sibling images.
    """
    if timeout_seconds is None:
        timeout_seconds = int(os.environ.get("BOOTSTRAP_WAIT_TIMEOUT_SECONDS", "120"))

    for label, probe in (("DynamoDB-Local", _probe_ddb), ("MinIO", _probe_s3)):
        deadline = time.time() + timeout_seconds
        while True:
            try:
                probe()
                print(f"  {label} is ready")
                break
            except Exception as exc:  # noqa: BLE001
                if time.time() > deadline:
                    raise RuntimeError(
                        f"{label} not ready within {timeout_seconds}s. It is probably "
                        f"not merely slow: check `docker compose logs {label.lower()}` "
                        f"for a service that is up but broken. A known one is "
                        f"DynamoDB-Local looping `SQLiteException: [14] unable to open "
                        f"database file` when its data volume is root-owned but the "
                        f"image runs as uid 1000 (see the `user: root` note in "
                        f"docker-compose.yml)."
                    ) from exc
                time.sleep(1)


def _probe_ddb() -> None:
    _ddb_client().list_tables()


def _probe_s3() -> None:
    _s3_client().list_buckets()


def main() -> int:
    print("Docker Compose bootstrap: provisioning DynamoDB-Local + MinIO …")
    wait_for_services()
    create_tables()
    create_buckets()
    seed_fixture()
    seed_users_and_playbook()
    seed_shipped_playbook()
    backfill_activated_at_462()
    print("Docker Compose bootstrap: done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
