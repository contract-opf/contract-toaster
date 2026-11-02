#!/usr/bin/env python3
"""
The AWS audit table's primary key is the key every audit writer uses.

WHY THIS GATE EXISTS
--------------------
`infra/lib/nested/data-stack.ts` declared the audit table's partition key as
`pk`, while the one backend writer (`backend/src/audit_log.py`), every audit
read in `backend/src/audit_queries.py` and the DTS table
(`deploy/dts/bootstrap.py::_TABLES`) all use `partition` / `timestamp`. A
PutItem whose item lacks the table's key attribute is a ValidationException,
so on AWS every backend audit write failed while every offline suite (moto
and DTS tables built from the backend's own shape) stayed green. The two
promotion writers (`.github/workflows/ci-pipeline.yml` STEP I and its
CodeBuild twin in `infra/lib/nested/cicd-stack.ts`) wrote the CDK's `pk`
shape, so they worked on AWS and nowhere else.

This gate pins all of them to one shape:

  A. The SYNTHESIZED audit table's KeySchema is HASH `partition`, RANGE
     `timestamp` (read from `cdk synth`, not a regex over the source).
  B. The DTS bootstrap declares the same key.
  C. `build_audit_item` writes exactly those key attributes and
     `audit_put_condition` guards on them.
  D. Both promotion writers put `partition` + `timestamp` in the backend's
     format (`YYYY-MM`, `"{epoch}#{event_id}"`), guard on
     `attribute_not_exists(partition)`, and never write a `pk`.

The table replacement this implies on AWS is described in RUNBOOK.md
("Audit table key change").

Run: python3 tests/test_infra_audit_table_key.py
Exit 0 = pass, 1 = fail.
"""

import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from infra_synth_helper import NEUTRAL_CDK_CONTEXT  # noqa: E402

EXPECTED_KEYS = [
    {"AttributeName": "partition", "KeyType": "HASH"},
    {"AttributeName": "timestamp", "KeyType": "RANGE"},
]

CI_PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci-pipeline.yml"
CICD_STACK = INFRA / "lib" / "nested" / "cicd-stack.ts"
BOOTSTRAP = REPO_ROOT / "deploy" / "dts" / "bootstrap.py"


def _synth() -> None:
    result = subprocess.run(  # noqa: S603
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],  # noqa: S607
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"cdk synth failed.\nstdout: {result.stdout[-800:]}\nstderr: {result.stderr[-800:]}"
        )


def _synthesized_audit_table() -> dict | None:
    for template_path in sorted((INFRA / "cdk.out").glob("*.nested.template.json")):
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::DynamoDB::Table":
                continue
            name = json.dumps(resource.get("Properties", {}).get("TableName", ""))
            if "-audit-" in name:
                return resource
    return None


def _bootstrap_audit_entry() -> tuple:
    """The AUDIT_TABLE row of deploy/dts/bootstrap.py::_TABLES, read with ast
    so the bootstrap's imports (boto3, the playbook registry) never run."""
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_TABLES" for t in node.targets
        ):
            for entry in ast.literal_eval(node.value):
                if entry[0] == "AUDIT_TABLE":
                    return entry
    raise AssertionError("deploy/dts/bootstrap.py::_TABLES has no AUDIT_TABLE entry")


def _ci_promotion_step() -> str:
    text = CI_PIPELINE.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^      - name: Write promotion audit row \(STEP I\)\n(.*?)(?=^      - )", text)
    if not m:
        raise AssertionError("ci-pipeline.yml has no 'Write promotion audit row (STEP I)' step")
    return m.group(1)


def _cicd_promotion_commands() -> str:
    text = CICD_STACK.read_text(encoding="utf-8")
    m = re.search(r"(?s)--- STEP I:(.*?)--- STEP J:", text)
    if not m:
        raise AssertionError("cicd-stack.ts has no STEP I block")
    return m.group(1)


class TestAuditTableKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _synth()
        cls.table = _synthesized_audit_table()

    def test_a_synthesized_audit_table_key_is_partition_timestamp(self) -> None:
        self.assertIsNotNone(self.table, "no '-audit-' table in the synthesized templates")
        keys = [
            {"AttributeName": k["AttributeName"], "KeyType": k["KeyType"]}
            for k in self.table["Properties"]["KeySchema"]
        ]
        self.assertEqual(keys, EXPECTED_KEYS)

    def test_b_dts_bootstrap_declares_the_same_key(self) -> None:
        _, hash_key, range_key, _ = _bootstrap_audit_entry()
        self.assertEqual((hash_key, range_key), ("partition", "timestamp"))

    def test_c_backend_writer_uses_the_table_key(self) -> None:
        from src import audit_log  # noqa: PLC0415

        item = audit_log.build_audit_item(actor="a", action="x", now=1_700_000_000, event_id="e1")
        self.assertEqual(item["partition"], "2023-11")
        self.assertEqual(item["timestamp"], "1700000000#e1")
        self.assertNotIn("pk", item)

    def test_d_promotion_writers_use_the_table_key(self) -> None:
        for label, body in (
            ("ci-pipeline.yml STEP I", _ci_promotion_step()),
            ("cicd-stack.ts STEP I", _cicd_promotion_commands()),
        ):
            with self.subTest(writer=label):
                self.assertIn("attribute_not_exists(partition)", body)
                self.assertNotRegex(body, r"\bpk\b", f"{label} still writes a `pk` attribute")
                self.assertRegex(body, r"partition\\*\"\s*:\s*\{\\*\"S\\*\"\s*:\s*\\*\"\$\{?AUDIT_PARTITION")
                self.assertRegex(body, r"AUDIT_PARTITION=\$\(date -u \+%Y-%m\)")
                # Sort key in the backend's "{epoch}#{event_id}" format, which
                # audit_queries parses back into recorded_at.
                self.assertRegex(body, r"AUDIT_EPOCH=\$\(date -u \+%s\)")
                self.assertRegex(body, r"timestamp\\*\"\s*:\s*\{\\*\"S\\*\"\s*:\s*\\*\"\$\{?AUDIT_EPOCH\}?#")
                self.assertRegex(body, r"\\*\"action\\*\"\s*:\s*\{\\*\"S\\*\"\s*:\s*\\*\"image-promoted")


def _run_tests() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(TestAuditTableKey)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
