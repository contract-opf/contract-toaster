#!/usr/bin/env python3
"""
Executable tests for the single, append-only audit writer
(`backend/src/audit_log.py`, QA remediation W6.1).

WHAT THIS GUARDS
----------------
Ten modules each carried their own "build an audit row and `put_item` it"
copy, and every copy wrote an UNCONDITIONAL `put_item`. A PutItem on an
existing key replaces the row, so the audit table's "append-only" claim
rested only on the AWS resource policy denying Update/Delete -- and the DTS
target has no resource policy at all.

  1. A second write with the same key raises (moto, so the condition is
     evaluated by a real DynamoDB implementation, not a fake that ignores
     it), and the first row survives unchanged.
  2. `detail` cannot move the key.
  3. The AST rule: no module under `backend/src` other than `audit_log.py`
     `put_item`s to the audit table or defines `_write_audit_entry`, and
     nothing under `infra/lambda`, `scripts` or `deploy` writes to it except
     the purge worker's own copy.
  4. Parity: the purge worker Lambda (which cannot import backend/src)
     builds the identical row and carries the identical condition.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = BACKEND_ROOT / "src"
PURGE_WORKER = REPO_ROOT / "infra" / "lambda" / "purge_worker" / "handler.py"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("AUDIT_TABLE", "contract-toaster-audit-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402, I001
from boto3.dynamodb.conditions import ConditionExpressionBuilder  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

from src import audit_log  # noqa: E402

FIXED_NOW = 1_790_000_000.0
FIXED_EVENT = "0" * 32


def _load_purge_worker():
    """Load the Lambda under a private module name: every infra/lambda bundle
    has a module called `handler` (tests/README.md -> "The `handler`
    collision"), so a bare import could bind to the wrong one."""
    spec = importlib.util.spec_from_file_location("_purge_worker_for_parity", PURGE_WORKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_audit_table(ddb):
    ddb.create_table(
        TableName=os.environ["AUDIT_TABLE"],
        KeySchema=[
            {"AttributeName": "partition", "KeyType": "HASH"},
            {"AttributeName": "timestamp", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "partition", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(os.environ["AUDIT_TABLE"])


class TestAppendOnly(unittest.TestCase):
    def test_a_second_write_with_the_same_key_raises_and_the_first_survives(self) -> None:
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = _create_audit_table(ddb)
            audit_log.write_audit_entry(
                ddb, actor="admin-1", action="first", target="t", target_type="x",
                now=FIXED_NOW, event_id=FIXED_EVENT,
            )
            with self.assertRaises(ClientError) as ctx:
                audit_log.write_audit_entry(
                    ddb, actor="attacker", action="overwrite", target="t", target_type="x",
                    now=FIXED_NOW, event_id=FIXED_EVENT,
                )
            self.assertEqual(
                ctx.exception.response["Error"]["Code"], "ConditionalCheckFailedException"
            )
            rows = table.scan()["Items"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action"], "first")
            self.assertEqual(rows[0]["actor"], "admin-1")

    def test_best_effort_swallows_the_collision_and_still_does_not_overwrite(self) -> None:
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = _create_audit_table(ddb)
            audit_log.write_audit_entry(
                ddb, actor="a", action="first", now=FIXED_NOW, event_id=FIXED_EVENT,
            )
            result = audit_log.write_audit_entry(
                ddb, actor="b", action="second", now=FIXED_NOW, event_id=FIXED_EVENT,
                best_effort=True,
            )
            self.assertIsNone(result)
            self.assertEqual(table.scan()["Items"][0]["action"], "first")

    def test_two_writes_in_the_same_second_get_different_keys(self) -> None:
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = _create_audit_table(ddb)
            for _ in range(5):
                audit_log.write_audit_entry(ddb, actor="a", action="burst", now=FIXED_NOW)
            rows = table.scan()["Items"]
            self.assertEqual(len(rows), 5)
            for row in rows:
                self.assertRegex(row["timestamp"], r"^1790000000#[0-9a-f]{32}$")

    def test_detail_cannot_move_the_key(self) -> None:
        item = audit_log.build_audit_item(
            actor="a", action="x", now=FIXED_NOW, event_id=FIXED_EVENT,
            detail={"partition": "1999-01", "timestamp": "0#forged", "event_id": "forged",
                    "outcome": "rejected"},
        )
        self.assertEqual(item["partition"], "2026-09")
        self.assertEqual(item["timestamp"], f"1790000000#{FIXED_EVENT}")
        self.assertEqual(item["event_id"], FIXED_EVENT)
        # `outcome` stays overridable: a rejected upload records it.
        self.assertEqual(item["outcome"], "rejected")

    def test_review_scoped_rows_are_stamped_for_the_review_id_index(self) -> None:
        item = audit_log.build_audit_item(actor="a", action="x", target="r-1", target_type="review")
        self.assertEqual(item["review_id"], "r-1")
        other = audit_log.build_audit_item(actor="a", action="x", target="u-1", target_type="upload")
        self.assertNotIn("review_id", other)


# ---------------------------------------------------------------------------
# The AST single-writer rule
# ---------------------------------------------------------------------------

_AUDIT_RE = re.compile(r"audit", re.IGNORECASE)


def _audit_put_items(tree: ast.AST) -> list[int]:
    """Line numbers of every `.put_item(...)` whose receiver is the audit
    table: the receiver expression mentions `audit`, or it is a local name
    assigned from an expression that does (`table = _audit_table(...)`,
    `table = ddb.Table(os.environ["AUDIT_TABLE"])`)."""
    hits: list[int] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        audit_names: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and _AUDIT_RE.search(ast.unparse(node.value)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        audit_names.add(target.id)
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "put_item"
            ):
                receiver = node.func.value
                text = ast.unparse(receiver)
                names_in_receiver = {
                    n.id for n in ast.walk(receiver) if isinstance(n, ast.Name)
                }
                if _AUDIT_RE.search(text) or names_in_receiver & audit_names:
                    hits.append(node.lineno)
    return sorted(set(hits))


class TestSingleWriter(unittest.TestCase):
    def test_no_backend_module_but_audit_log_writes_the_audit_table(self) -> None:
        offenders: list[str] = []
        for path in sorted(BACKEND_SRC.rglob("*.py")):
            if path.name == "audit_log.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno in _audit_put_items(tree):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    node.name in {"_write_audit_entry", "write_audit_entry"}
                ):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} defines {node.name}"
                    )
        self.assertEqual(offenders, [], "audit writes outside backend/src/audit_log.py")

    def test_only_the_purge_worker_writes_audit_outside_backend(self) -> None:
        offenders: list[str] = []
        for root in ("infra/lambda", "scripts", "deploy"):
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                if "node_modules" in path.parts or path == PURGE_WORKER:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for lineno in _audit_put_items(tree):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual(offenders, [])

    def test_the_purge_worker_writes_only_through_its_own_writer(self) -> None:
        tree = ast.parse(PURGE_WORKER.read_text(encoding="utf-8"))
        writer_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_write_audit_entry":
                writer_lines = {n.lineno for n in ast.walk(node) if hasattr(n, "lineno")}
        self.assertTrue(writer_lines, "purge worker has no _write_audit_entry")
        stray = [line for line in _audit_put_items(tree) if line not in writer_lines]
        self.assertEqual(stray, [])


class TestLambdaParity(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = _load_purge_worker()

    def test_the_lambda_builds_the_same_row(self) -> None:
        cases = [
            {"actor": "system:retention-purge-worker", "action": "retention_purge_sweep",
                 "target": "sweep-1", "target_type": "retention_sweep",
                 "detail": {"deleted_count": 3, "purged_review_ids": ["a", "b"]}},
            {"actor": "a", "action": "x", "target": "r-1", "target_type": "review",
                 "detail": {"outcome": "rejected", "partition": "forged"}},
            {"actor": "a", "action": "x"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                backend = audit_log.build_audit_item(
                    now=FIXED_NOW, event_id=FIXED_EVENT, **kwargs
                )
                lam = self.worker._build_audit_item(
                    now=FIXED_NOW, event_id=FIXED_EVENT, **kwargs
                )
                self.assertEqual(backend, lam)

    def test_the_lambda_carries_the_same_append_only_condition(self) -> None:
        built = ConditionExpressionBuilder().build_expression(
            audit_log.append_only_condition()
        )
        backend = built.condition_expression
        for placeholder, name in built.attribute_name_placeholders.items():
            backend = backend.replace(placeholder, name)
        lam = self.worker.AUDIT_APPEND_ONLY_CONDITION
        for placeholder, name in self.worker.AUDIT_APPEND_ONLY_NAMES.items():
            lam = lam.replace(placeholder, name)

        def norm(s: str) -> str:
            return re.sub(r"[()\s]", "", s.replace("attribute_not_exists", "ane:"))

        self.assertEqual(norm(backend), norm(lam))
        self.assertEqual(
            norm(lam), norm("attribute_not_exists(partition) AND attribute_not_exists(timestamp)")
        )


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestAppendOnly))
    suite.addTests(loader.loadTestsFromTestCase(TestSingleWriter))
    suite.addTests(loader.loadTestsFromTestCase(TestLambdaParity))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
