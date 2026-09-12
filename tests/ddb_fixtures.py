#!/usr/bin/env python3
"""Shared moto DynamoDB table builders for the two tables whose GSIs
production code queries by name (issue #67).

WHY THIS EXISTS
---------------
Production used to choose its DynamoDB access path by duck-typing the table
object it was handed -- probing it for `.query` / `.scan` / `query_by_owner`
and falling back to a scan when the probe failed. A real boto3 Table always
has `.query` and `.scan`, so every fallback branch was dead in production and
live only under hand-rolled test fakes: the tests exercised code production
never runs, and the code production DOES run (the index query) was covered
only where a test happened to bring a real table.

Issue #67 deleted those branches. Every caller now issues the index query
unconditionally, which means a test must bring a table that actually HAS the
index -- i.e. a real (moto) table declared exactly as the CDK declares it.
This module is the single place those declarations live, so a test cannot
quietly invent an index the deployed table does not have.

SOURCE OF RECORD
----------------
`infra/lib/nested/data-stack.ts` (mirrored for the DTS target by
`deploy/dts/bootstrap.py::_TABLES`). `tests/test_ddb_fixtures_cdk_parity_67.py`
synthesizes the CDK template and asserts the GSIs below are exactly the GSIs
the stack creates, so drift in either direction is caught rather than
discovered in production.

NOT FOR PRODUCTION USE -- this module is imported only by tests.
"""

from __future__ import annotations

import os
from typing import Any

# ---------------------------------------------------------------------------
# reviews -- PK review_id (infra/lib/nested/data-stack.ts `ReviewsTable`)
# ---------------------------------------------------------------------------

#: `owner_sub-index` backs the owner-scoped reads (reviews.py
#: `_list_reviews_for_owner` / `_page_for_owner`, disposition.py
#: `_query_by_owner`); `status-index` backs every admin-wide read
#: (reviews.py `_query_status_page`, issue #52); `playbook_hash-index` backs
#: the audit query API's rollback/quarantine population (issue #253) and is
#: KEYS_ONLY in the CDK -- declaring it ALL here would let an implementation
#: that never re-reads the row body pass against a fixture it would fail
#: against the deployed table.
REVIEWS_TABLE_KEY_SCHEMA: list[dict[str, str]] = [
    {"AttributeName": "review_id", "KeyType": "HASH"},
]

REVIEWS_GSIS: list[dict[str, Any]] = [
    {
        "IndexName": "owner_sub-index",
        "KeySchema": [
            {"AttributeName": "owner_sub", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
    {
        "IndexName": "status-index",
        "KeySchema": [
            {"AttributeName": "status", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
    {
        "IndexName": "playbook_hash-index",
        "KeySchema": [
            {"AttributeName": "playbook_hash", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "KEYS_ONLY"},
    },
]

# Every attribute that is a key of the table or of one of its indexes, and
# nothing else -- DynamoDB rejects an AttributeDefinition that no key uses.
REVIEWS_ATTRIBUTE_DEFINITIONS: list[dict[str, str]] = [
    {"AttributeName": "review_id", "AttributeType": "S"},
    {"AttributeName": "owner_sub", "AttributeType": "S"},
    {"AttributeName": "status", "AttributeType": "S"},
    {"AttributeName": "playbook_hash", "AttributeType": "S"},
    {"AttributeName": "created_at", "AttributeType": "S"},
]

# ---------------------------------------------------------------------------
# review_submissions -- PK idempotency_key
# (infra/lib/nested/data-stack.ts `ReviewSubmissionsTable`)
# ---------------------------------------------------------------------------

#: `review_id-index` is the keyed lookup the spend settle and the orphan
#: reconciler need: the table is keyed on idempotency_key, but the pointer-only
#: event those paths receive carries only review_id (issue #262). HASH only --
#: the CDK declares no sort key on this index.
SUBMISSIONS_TABLE_KEY_SCHEMA: list[dict[str, str]] = [
    {"AttributeName": "idempotency_key", "KeyType": "HASH"},
]

SUBMISSIONS_GSIS: list[dict[str, Any]] = [
    {
        "IndexName": "review_id-index",
        "KeySchema": [
            {"AttributeName": "review_id", "KeyType": "HASH"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    },
]

SUBMISSIONS_ATTRIBUTE_DEFINITIONS: list[dict[str, str]] = [
    {"AttributeName": "idempotency_key", "AttributeType": "S"},
    {"AttributeName": "review_id", "AttributeType": "S"},
]


def _resolve_name(explicit: str | None, env_var: str, default: str) -> str:
    if explicit:
        return explicit
    return os.environ.get(env_var, "").strip() or default


def create_reviews_table(dynamodb: Any, table_name: str | None = None) -> Any:
    """Create the `reviews` table on an ACTIVE moto mock and return the
    `Table` resource.

    `table_name` defaults to `$REVIEWS_TABLE` (the env var every caller
    already sets before importing the module under test), then to a neutral
    test name.
    """
    name = _resolve_name(table_name, "REVIEWS_TABLE", "contract-toaster-reviews-test")
    dynamodb.create_table(
        TableName=name,
        KeySchema=REVIEWS_TABLE_KEY_SCHEMA,
        AttributeDefinitions=REVIEWS_ATTRIBUTE_DEFINITIONS,
        GlobalSecondaryIndexes=REVIEWS_GSIS,
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table(name)


def create_submissions_table(dynamodb: Any, table_name: str | None = None) -> Any:
    """Create the `review_submissions` table on an ACTIVE moto mock and
    return the `Table` resource.

    `table_name` defaults to `$REVIEW_SUBMISSIONS_TABLE`, then to a neutral
    test name.
    """
    name = _resolve_name(
        table_name,
        "REVIEW_SUBMISSIONS_TABLE",
        "contract-toaster-review-submissions-test",
    )
    dynamodb.create_table(
        TableName=name,
        KeySchema=SUBMISSIONS_TABLE_KEY_SCHEMA,
        AttributeDefinitions=SUBMISSIONS_ATTRIBUTE_DEFINITIONS,
        GlobalSecondaryIndexes=SUBMISSIONS_GSIS,
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table(name)
