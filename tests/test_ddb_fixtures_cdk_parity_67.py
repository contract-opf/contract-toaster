#!/usr/bin/env python3
"""
Issue #67: the moto tables in `tests/ddb_fixtures.py` must declare EXACTLY
the GSIs `infra/lib/nested/data-stack.ts` declares -- no more, no fewer, same
key schemas, same projections.

WHY THIS GATE EXISTS
--------------------
#67 deleted the duck-typed scan fallbacks from every DynamoDB read
(`hasattr(table, "query")` and friends -- see
`tests/test_no_duck_typed_tables.py`). Every caller now issues its index
query unconditionally, so a test can only cover those paths by bringing a
real table that HAS the index. That moves the risk: a fixture that invents an
index, or misses one the stack added, would keep the suite green while the
deployed table raises ValidationException ("table does not have the specified
index") on the very first request -- the exact #446 shape that broke every
spend settle on DTS.

So the fixture is not trusted on its own. This test synthesizes the CDK app
offline and compares the synthesized Data stack's GSIs against the fixture's
declarations in both directions.

It builds the tables through `create_reviews_table` / `create_submissions_table`
rather than reading the module constants, so the assertion covers what the
fixture actually CREATES (a builder that dropped an index would otherwise pass
against intact constants), and it round-trips through moto's DescribeTable so
a declaration DynamoDB itself would reject cannot pass.

`deploy/dts/bootstrap.py::_TABLES` mirrors the same indexes for the DTS
target; `tests/test_dts_bootstrap_tables.py` (where present) covers that copy.

Run: python3 tests/test_ddb_fixtures_cdk_parity_67.py
Exit 0 = pass, 1 = fail.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"

sys.path.insert(0, str(REPO_ROOT / "tests"))

from infra_synth_helper import NEUTRAL_CDK_CONTEXT  # noqa: E402

# moto needs a region and (fake) credentials for boto3.resource("dynamodb").
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

import ddb_fixtures  # noqa: E402

#: TableName marker -> the fixture builder that must match it.
TABLE_UNDER_TEST = {
    "-reviews-": ddb_fixtures.create_reviews_table,
    "-review-submissions-": ddb_fixtures.create_submissions_table,
}


def _synth() -> None:
    """Synthesize the CDK app into infra/cdk.out. Offline: no AWS calls, no
    credentials, no network -- `cdk synth` renders templates locally."""
    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            "cdk synth failed, so the CDK side of this parity check cannot be "
            f"read.\nstdout: {result.stdout[-800:]}\nstderr: {result.stderr[-800:]}"
        )


def _synthesized_tables() -> dict[str, dict]:
    """`{name_marker: table resource}` for every marker in TABLE_UNDER_TEST,
    read out of the nested Data stack template in infra/cdk.out.

    Markers are matched longest-first and each table resource is claimed by
    at most one of them, so a future marker that is a prefix of another
    cannot silently swallow its neighbour's table.
    """
    found: dict[str, dict] = {}
    markers = sorted(TABLE_UNDER_TEST, key=len, reverse=True)
    for template_path in sorted((INFRA / "cdk.out").glob("*.nested.template.json")):
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::DynamoDB::Table":
                continue
            table_name = json.dumps(resource.get("Properties", {}).get("TableName", ""))
            for marker in markers:
                if marker in table_name:
                    found.setdefault(marker, resource)
                    break
    return found


def _normalize(gsis: list[dict]) -> dict[str, dict]:
    """`{IndexName: {"KeySchema": [...], "ProjectionType": "..."}}`.

    Only the fields both sides can state identically: CloudFormation and
    DescribeTable agree on IndexName, KeySchema and ProjectionType, and
    disagree on everything else (ARNs, status, item counts, throughput).
    """
    normalized: dict[str, dict] = {}
    for gsi in gsis:
        normalized[gsi["IndexName"]] = {
            "KeySchema": [
                {"AttributeName": k["AttributeName"], "KeyType": k["KeyType"]}
                for k in gsi.get("KeySchema", [])
            ],
            "ProjectionType": gsi.get("Projection", {}).get("ProjectionType"),
        }
    return normalized


class TestDdbFixturesCdkParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _synth()
        cls.cdk_tables = _synthesized_tables()

    def setUp(self) -> None:
        self._mock_aws = mock_aws()
        self._mock_aws.start()
        self.addCleanup(self._mock_aws.stop)
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")

    def test_every_table_under_test_was_found_in_the_synthesized_template(self) -> None:
        """A marker that matches nothing would make the per-table assertions
        vacuous, so it is its own failure."""
        self.assertEqual(
            sorted(self.cdk_tables),
            sorted(TABLE_UNDER_TEST),
            "a table this parity check covers is absent from the synthesized "
            "Data stack template -- did its TableName change?",
        )

    def test_fixture_gsis_match_the_synthesized_cdk_gsis(self) -> None:
        for marker, builder in sorted(TABLE_UNDER_TEST.items()):
            with self.subTest(table=marker):
                cdk_resource = self.cdk_tables[marker]
                cdk_gsis = _normalize(
                    cdk_resource.get("Properties", {}).get(
                        "GlobalSecondaryIndexes", []
                    )
                )

                table = builder(self.ddb, table_name=f"parity{marker}test")
                # Round-trip through DescribeTable: this is what the table
                # really has, not what the fixture asked for.
                described = table.meta.client.describe_table(TableName=table.name)
                fixture_gsis = _normalize(
                    described["Table"].get("GlobalSecondaryIndexes", [])
                )

                self.assertEqual(
                    fixture_gsis,
                    cdk_gsis,
                    f"tests/ddb_fixtures.py and infra/lib/nested/data-stack.ts "
                    f"disagree about the GSIs on the '{marker.strip('-')}' table. "
                    f"A fixture index the stack does not create makes a green "
                    f"test prove nothing; a stack index the fixture omits leaves "
                    f"a production query untested.\n"
                    f"  fixture: {json.dumps(fixture_gsis, sort_keys=True)}\n"
                    f"  cdk:     {json.dumps(cdk_gsis, sort_keys=True)}",
                )

    def test_fixture_table_key_schema_matches_the_synthesized_cdk_key_schema(self) -> None:
        for marker, builder in sorted(TABLE_UNDER_TEST.items()):
            with self.subTest(table=marker):
                cdk_keys = [
                    {"AttributeName": k["AttributeName"], "KeyType": k["KeyType"]}
                    for k in self.cdk_tables[marker]
                    .get("Properties", {})
                    .get("KeySchema", [])
                ]
                table = builder(self.ddb, table_name=f"paritykeys{marker}test")
                described = table.meta.client.describe_table(TableName=table.name)
                fixture_keys = [
                    {"AttributeName": k["AttributeName"], "KeyType": k["KeyType"]}
                    for k in described["Table"].get("KeySchema", [])
                ]
                self.assertEqual(
                    fixture_keys,
                    cdk_keys,
                    f"tests/ddb_fixtures.py and the CDK disagree about the primary "
                    f"key of the '{marker.strip('-')}' table",
                )


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestDdbFixturesCdkParity)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
