#!/usr/bin/env python3
"""
DTS bootstrap — one-shot job that provisions the local emulators before the
backend starts (docker-compose `bootstrap` service).

Idempotent: creating a table/bucket that already exists is a no-op, so
`docker compose up` can be re-run safely.

Does four things against DynamoDB-Local + MinIO (endpoints from env, via
config.boto3_client_kwargs):
  1. Create the DynamoDB tables the backend reads (with the one GSI the backend
     queries: reviews.owner_sub-index).
  2. Create the uploads/outputs S3 buckets.
  3. Seed the mock eiaa redline fixture into the outputs bucket at
     mock-fixtures/eiaa/pre-baked-redline.docx (what the mock pipeline copies).
  4. Seed the demo users (admin/admin, user/user) and a minimal active eiaa
     playbook bundle so submit_review's active-bundle check passes.

Run: python3 deploy/dts/bootstrap.py   (PYTHONPATH must include backend/)
"""

import os
import sys
import time
from pathlib import Path

import boto3

# backend/src on the path so we can reuse config + the demo-user seeder.
_APP_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _APP_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from src import config, demo_auth  # noqa: E402

FIXTURE_PATH = _APP_ROOT / "infra" / "fixtures" / "mock-outputs" / "eiaa" / "pre-baked-redline.docx"
FIXTURE_KEY = "mock-fixtures/eiaa/pre-baked-redline.docx"

# Table name (env var) -> key schema. `gsis` is a list of
# (index_name, hash_attr, range_attr) tuples.
_TABLES = [
    ("USERS_TABLE", "cognito_sub", None, []),
    ("REVIEWS_TABLE", "review_id", None, [("owner_sub-index", "owner_sub", "created_at")]),
    ("REVIEW_SUBMISSIONS_TABLE", "idempotency_key", None, []),
    ("DAILY_SPEND_TABLE", "spend_date", None, []),
    ("AUDIT_TABLE", "partition", "timestamp", []),
    ("AUTH_SETTINGS_TABLE", "setting_id", None, []),
    ("PLAYBOOKS_TABLE", "playbook_id", None, []),
    ("PLAYBOOK_VERSIONS_TABLE", "playbook_id", "version", []),
    ("RETENTION_SETTINGS_TABLE", "setting_id", None, []),
    ("SYNC_STATUS_TABLE", "sync_type", None, []),
]


def _ddb_client():
    return boto3.client("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def _ddb_resource():
    return boto3.resource("dynamodb", **config.boto3_client_kwargs("dynamodb"))


def _s3_client():
    return boto3.client("s3", **config.boto3_client_kwargs("s3"))


def _attr_defs(hash_attr, range_attr, gsis):
    names = {hash_attr}
    if range_attr:
        names.add(range_attr)
    for _, gh, gr in gsis:
        names.add(gh)
        if gr:
            names.add(gr)
    return [{"AttributeName": n, "AttributeType": "S"} for n in sorted(names)]


def _key_schema(hash_attr, range_attr):
    schema = [{"AttributeName": hash_attr, "KeyType": "HASH"}]
    if range_attr:
        schema.append({"AttributeName": range_attr, "KeyType": "RANGE"})
    return schema


def create_tables() -> None:
    client = _ddb_client()
    for env_var, hash_attr, range_attr, gsis in _TABLES:
        name = os.environ.get(env_var)
        if not name:
            print(f"  skip {env_var} (unset)")
            continue
        kwargs = {
            "TableName": name,
            "AttributeDefinitions": _attr_defs(hash_attr, range_attr, gsis),
            "KeySchema": _key_schema(hash_attr, range_attr),
            "BillingMode": "PAY_PER_REQUEST",
        }
        if gsis:
            kwargs["GlobalSecondaryIndexes"] = [
                {
                    "IndexName": idx,
                    "KeySchema": _key_schema(gh, gr),
                    "Projection": {"ProjectionType": "ALL"},
                }
                for idx, gh, gr in gsis
            ]
        try:
            client.create_table(**kwargs)
            print(f"  created table {name}")
        except client.exceptions.ResourceInUseException:
            print(f"  table {name} already exists")


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
    bucket = os.environ.get("OUTPUTS_BUCKET")
    if not bucket:
        return
    if not FIXTURE_PATH.exists():
        print(f"  WARNING: fixture not found at {FIXTURE_PATH}; eiaa downloads will 404")
        return
    _s3_client().put_object(Bucket=bucket, Key=FIXTURE_KEY, Body=FIXTURE_PATH.read_bytes())
    print(f"  seeded {FIXTURE_KEY} into {bucket}")


def seed_users_and_playbook() -> None:
    ddb = _ddb_resource()
    demo_auth.seed_demo_users(ddb)
    print("  seeded demo users (admin/admin, user/user)")

    # Enable password sign-in in the ADMIN-TOGGLEABLE auth-mode row that
    # demo_auth.login_with_password gates on. This is distinct from the
    # deployment-level AUTH_MODE env (which get_current_user's verifier
    # dispatch uses): without seeding this DynamoDB row, login_with_password
    # defaults to sso-only and rejects password sign-in. Seed it to the
    # deployment's AUTH_MODE (password/both); default to password for DTS.
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

    # Minimal active eiaa bundle so resolve_active_release_bundle_hash passes.
    playbooks_table = os.environ.get("PLAYBOOKS_TABLE")
    if playbooks_table:
        ddb.Table(playbooks_table).put_item(
            Item={
                "playbook_id": "eiaa",
                "active_release_bundle_hash": "dts-mock-bundle-v1",
                "updated_at": str(int(time.time())),
            }
        )
        print("  seeded active eiaa playbook bundle")


def wait_for_services(timeout_seconds: int = 60) -> None:
    """Block until DynamoDB-Local and MinIO accept connections (the compose
    `depends_on: service_started` only waits for the container to start, not for
    the service inside it to be ready)."""
    deadline = time.time() + timeout_seconds
    for label, probe in (("DynamoDB-Local", _probe_ddb), ("MinIO", _probe_s3)):
        while True:
            try:
                probe()
                print(f"  {label} is ready")
                break
            except Exception as exc:  # noqa: BLE001
                if time.time() > deadline:
                    raise RuntimeError(f"{label} not ready within {timeout_seconds}s") from exc
                time.sleep(1)


def _probe_ddb() -> None:
    _ddb_client().list_tables()


def _probe_s3() -> None:
    _s3_client().list_buckets()


def main() -> int:
    print("DTS bootstrap: provisioning DynamoDB-Local + MinIO …")
    wait_for_services()
    create_tables()
    create_buckets()
    seed_fixture()
    seed_users_and_playbook()
    print("DTS bootstrap: done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
