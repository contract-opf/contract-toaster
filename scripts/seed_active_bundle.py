#!/usr/bin/env python3
"""
Seed the `playbooks` table's `active_release_bundle_hash` -- issue #194.

## Why this exists

`backend/src/reviews.py`'s `resolve_active_release_bundle_hash` (and the
pipeline's `verify_submission_time_bundle`) read
`playbooks.active_release_bundle_hash` from DynamoDB, but nothing wrote
that attribute for the demo's one playbook (`eiaa`) -- the resolver would
correctly refuse every submission with 503 "no active playbook" against an
empty table, which is CORRECT behavior for a genuinely-empty table but
useless for a demo/dev environment that needs a real, non-fabricated
active bundle to submit against.

This script computes the REAL content hash (`scripts/canonicalize.py`'s
`content_hash()` -- the same canonicalization used everywhere else in the
repo, e.g. `playbook_versions.content_hash`, the golden-hash CI fixture)
and writes it as `active_release_bundle_hash` on the `playbooks` row for
the given `playbook_id`. It never fabricates or hard-codes a hash: the
seeded value always matches what `scripts/canonicalize.py` computes from
the actual playbook JSON at the time this script runs, so the two can
never drift silently.

## Scope (v1, issue #194)

This is intentionally a seed/bootstrap script, not the activation admin
API (#41/#67/#68 -- the full activation/rollback/deactivate lifecycle is
explicitly deferred past this slice; see `backend/src/playbook_versions.py`
"Explicitly deferred"). It unconditionally sets `active_release_bundle_hash`
on the `playbooks` row -- no draft/approval workflow, no audit entry, no
demotion of a prior bundle. Use it to bootstrap a fresh environment (or a
test/moto table) with a real, resolvable active bundle.

## Usage

    # Print the hash that would be seeded (no writes):
    python3 scripts/seed_active_bundle.py --dry-run

    # Seed the real DynamoDB table for an environment:
    python3 scripts/seed_active_bundle.py --table-name contract-toaster-playbooks-dev

    # Programmatic (e.g. from a test with a moto-mocked table):
    from seed_active_bundle import compute_seed_hash, seed_active_bundle
    seeded_hash = seed_active_bundle("eiaa", dynamodb_resource, table_name="...")
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import canonicalize  # noqa: E402
import playbook_registry  # noqa: E402


def compute_seed_hash(playbook_id: str = playbook_registry.DEFAULT_PLAYBOOK_ID) -> str:
    """The real content_hash for `playbook_id`'s current playbook JSON,
    computed the exact same way as everywhere else in the repo
    (`scripts/canonicalize.py::content_hash`). Never a placeholder."""
    playbook_path = canonicalize.resolve_playbook_path(playbook_id)
    with open(playbook_path) as f:
        doc = json.load(f)
    return canonicalize.content_hash(doc)


def seed_active_bundle(
    playbook_id: str,
    dynamodb_resource: Any,
    table_name: str | None = None,
) -> str:
    """Write `active_release_bundle_hash` = compute_seed_hash(playbook_id)
    onto the `playbooks` row for `playbook_id`. Returns the seeded hash.

    `table_name` defaults to the `PLAYBOOKS_TABLE` environment variable
    (the same name `backend/src/reviews.py` reads), matching every other
    table-name convention in this repo.
    """
    seeded_hash = compute_seed_hash(playbook_id)
    resolved_table_name = table_name or os.environ["PLAYBOOKS_TABLE"]
    table = dynamodb_resource.Table(resolved_table_name)
    table.put_item(
        Item={
            "playbook_id": playbook_id,
            "active_release_bundle_hash": seeded_hash,
        }
    )
    return seeded_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--playbook-id",
        default=playbook_registry.DEFAULT_PLAYBOOK_ID,
        help=f"playbook_id to seed (default: {playbook_registry.DEFAULT_PLAYBOOK_ID!r}).",
    )
    parser.add_argument(
        "--table-name",
        default=None,
        help="playbooks table name. Defaults to the PLAYBOOKS_TABLE env var.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the hash that would be seeded; do not write to DynamoDB.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(compute_seed_hash(args.playbook_id))
        return 0

    import boto3  # local import: not needed for --dry-run / programmatic use

    table_name = args.table_name or os.environ.get("PLAYBOOKS_TABLE")
    if not table_name:
        print(
            "ERROR: --table-name not given and PLAYBOOKS_TABLE is not set.",
            file=sys.stderr,
        )
        return 1

    dynamodb_resource = boto3.resource("dynamodb")
    seeded_hash = seed_active_bundle(args.playbook_id, dynamodb_resource, table_name=table_name)
    print(f"Seeded playbook_id={args.playbook_id!r} active_release_bundle_hash={seeded_hash!r}")
    print(f"  table: {table_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
