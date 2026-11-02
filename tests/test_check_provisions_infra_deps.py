#!/usr/bin/env python3
"""
A full `scripts/check.sh` run installs infra/node_modules BEFORE the suite
starts, the way CI GATE A's `npm ci` step does.

## The failure this locks fixed (2026-10-28)

A full gate in a fresh git worktree reported tests/test_ci_pipeline.py as
FLAKY-UNRESOLVED (exit 2): its CDK synth check failed with

    TS2882: Cannot find module or type declarations for side-effect import
    of 'source-map-support/register'.

and then passed when re-run alone. A git worktree has no infra/node_modules,
and only two infra tests (test_infra_cdk_skeleton.py, test_infra_app_stack.py)
install it -- lazily, whenever the runner reaches them. A synth test that got
there first ran `npx` against the shared `~/.npm/_npx` cache instead, which
has no aws-cdk-lib and whose ts-node now bundles TypeScript 6 (strict about
side-effect imports). By the isolated re-run the lazy install had happened, so
it passed. That is ordering, not noise, and a gate that calls it FLAKY invites
`ALLOW_FLAKY=1`.

This runs a COPY of the shipped check.sh in a throwaway repo with a stub
`npm` on PATH and a stub suite runner that records whether infra/node_modules
existed when the suite started. Copied, never re-implemented, so an edit to
check.sh changes what this sees on the next run.

NOTE FOR EDITORS: never write the lowercase CDK synth command in this file
(see tests/test_check_only_flag_68.py's note) -- SKIP_INFRA greps test
sources for it case-sensitively and would exile this pure-shell test from the
fast gate.

Run standalone: `python3 tests/test_check_provisions_infra_deps.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"

STUB_NPM = """#!/bin/sh
printf '%s %s\\n' "$(basename "$PWD")" "$*" >> "$NPM_LEDGER"
[ "${NPM_FAIL:-}" = "1" ] && exit 1
mkdir -p node_modules
exit 0
"""

STUB_SUITE = """#!/bin/sh
if [ -d infra/node_modules ]; then echo present; else echo absent; fi > suite.ran
exit 0
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TestCheckProvisionsInfraDeps(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="check-infra-deps-"))
        (self.root / "scripts").mkdir()
        shutil.copy2(CHECK_SH, self.root / "scripts" / "check.sh")
        _write_exe(self.root / "scripts" / "collect_test_failures.sh", STUB_SUITE)
        (self.root / "infra").mkdir()
        (self.root / "infra" / "package-lock.json").write_text("{}\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        _write_exe(self.bin / "npm", STUB_NPM)
        self.npm_ledger = self.root / "npm.ledger"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, *, skip_infra: bool = False, npm_fail: bool = False):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["NPM_LEDGER"] = str(self.npm_ledger)
        env["CHECK_NO_LOCK"] = "1"
        env["CHECK_LOCK_DIR"] = str(self.root / "unused.lock")
        env.pop("VIRTUAL_ENV", None)
        env.pop("CHECK_ONLY", None)
        env.pop("SKIP_INFRA", None)
        env.pop("NPM_FAIL", None)
        if skip_infra:
            env["SKIP_INFRA"] = "1"
        if npm_fail:
            env["NPM_FAIL"] = "1"
        return subprocess.run(  # noqa: S603
            ["bash", str(self.root / "scripts" / "check.sh")],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(self.root),
        )

    def _npm_calls(self) -> list[str]:
        if not self.npm_ledger.exists():
            return []
        return self.npm_ledger.read_text().splitlines()

    def _suite_saw(self) -> str | None:
        ran = self.root / "suite.ran"
        return ran.read_text().strip() if ran.exists() else None

    def test_a_full_run_installs_infra_deps_before_the_suite(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self._npm_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertTrue(calls[0].startswith("infra ci"), calls)
        self.assertEqual(self._suite_saw(), "present")

    def test_existing_infra_deps_are_left_alone(self) -> None:
        (self.root / "infra" / "node_modules").mkdir()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._npm_calls(), [])
        self.assertEqual(self._suite_saw(), "present")

    def test_skip_infra_installs_nothing(self) -> None:
        proc = self._run(skip_infra=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._npm_calls(), [])
        self.assertEqual(self._suite_saw(), "absent")

    def test_a_tree_without_an_infra_lockfile_installs_nothing(self) -> None:
        """The synthetic repo tests/test_check_only_flag_68.py builds has no infra/."""
        (self.root / "infra" / "package-lock.json").unlink()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._npm_calls(), [])

    def test_a_failed_install_is_a_red_gate_not_a_silent_skip(self) -> None:
        proc = self._run(npm_fail=True)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("ALL GREEN", proc.stdout)
        self.assertIsNone(self._suite_saw(), "the suite must not run without its infra deps")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
