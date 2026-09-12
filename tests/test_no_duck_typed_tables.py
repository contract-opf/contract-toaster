#!/usr/bin/env python3
"""
Guard for issue #67: production code must NOT select its DynamoDB access
path by duck-typing the table object it was handed.

Nine sites used to do exactly that -- `hasattr(table, "query")`,
`hasattr(table, "scan")`, `hasattr(table, "query_by_owner")` -- and fell back
to a table scan when the probe failed:

  backend/src/disposition.py        (x3)
  backend/src/pipeline_runner.py    (x1)
  backend/src/reviews.py            (x3)
  infra/lambda/persist/handler.py   (x1)
  infra/lambda/orphan_reconciler/handler.py (x1)

A real boto3 Table ALWAYS has `.query` and `.scan`, so every one of those
fallback branches was unreachable in production. They existed for hand-rolled
test fakes -- which means the tests covering them exercised code production
never runs, while the branch production DOES run (the index query) went
uncovered wherever a test took the fake path. Worse, the fallback was a full
table scan: had anything ever reached it in production it would have silently
read at most the first 1MB page.

The fix deleted the branches; this guard keeps them deleted. The fixture that
replaces the fakes is `tests/ddb_fixtures.py` (real moto tables declaring the
CDK's GSIs), whose parity with the deployed stack is asserted by
`tests/test_ddb_fixtures_cdk_parity_67.py`.

SCOPE -- deliberately narrow. This scans only `backend/src` and
`infra/lambda`, the two production trees the issue names. It does NOT scan
`tests/`: a test is allowed to probe a table object, and it does not scan
`infra/cdk.out`, which is generated build output.

There is no allowlist. If a future change genuinely needs to probe a table
for a capability (a waiter probe, say), it must be written so this string
does not appear -- and the reason recorded at the call site -- rather than
added as a silent exception here.

Run: python3 tests/test_no_duck_typed_tables.py
Exit 0 = pass, 1 = fail.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The two production trees the guard covers.
SCANNED_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "src",
    REPO_ROOT / "infra" / "lambda",
)

#: The forbidden string. Assembled at runtime rather than written as one
#: literal so this guard file does not match its own rule (the repo-wide
#: `grep -rn 'hasattr(table' backend/src infra/lambda` acceptance check in
#: issue #67 does not scan tests/, but a future widening of it should not
#: trip over the guard that enforces it).
FORBIDDEN = "hasattr(" + "table,"


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        if not root.is_dir():
            continue
        files.extend(sorted(p for p in root.rglob("*.py") if p.is_file()))
    return files


class TestNoDuckTypedTables(unittest.TestCase):
    def test_scanned_roots_exist_and_are_non_empty(self) -> None:
        """The guard is worthless if it scans nothing -- a moved directory
        would otherwise turn it permanently green."""
        for root in SCANNED_ROOTS:
            self.assertTrue(
                root.is_dir(), f"guard scans a directory that does not exist: {root}"
            )
        files = _python_files()
        self.assertGreater(
            len(files),
            10,
            "guard found suspiciously few Python files -- has the tree moved?",
        )

    def test_no_hasattr_table_probe_in_production_code(self) -> None:
        offenders: list[str] = []
        for path in _python_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover
                self.fail(f"could not read {path}: {exc}")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if FORBIDDEN in line:
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "Issue #67: production code must not duck-type a DynamoDB table to "
            "pick its access path. A real boto3 Table always has .query/.scan, "
            "so the fallback is dead in production and live only under test "
            "fakes. Call the index query unconditionally and build the test "
            "table with tests/ddb_fixtures.py instead.\n  "
            + "\n  ".join(offenders),
        )


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestNoDuckTypedTables)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
