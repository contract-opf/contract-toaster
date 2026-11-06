#!/usr/bin/env python3
"""The tracked confidentiality scanner, its hook wiring and its CI workflow (QA 2026-11-03 #3).

Only INVENTED terms appear here ("Zorblax Holdings", "Quillfeather"): the real
list is private and never tracked. Every case builds a throwaway git repository.
"""

# ruff: noqa: S603, S607  -- throwaway git repositories driven through subprocess
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "scripts" / "pre-push-confidentiality.py"
HOOK = REPO_ROOT / ".githooks" / "pre-push"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "confidential-terms-gate.yml"
ZERO = "0" * 40
TERMS = "# invented terms for this test\nZorblax Holdings\nQuillfeather\n"


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "LAND_TO_MAIN", "CONTRACT_TOASTER_LOOP", "GIT_DIR")}
    env.update({"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
    env.update(extra)
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=_env()).stdout.strip()


def _repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "pre-push-confidentiality.py").write_text(SCANNER.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / ".githooks").mkdir()
    (repo / ".githooks" / "pre-push").write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _scan(repo: Path, lsha: str, rsha: str, names_dir: Path | None, **env_extra: str) -> subprocess.CompletedProcess[str]:
    extra = dict(env_extra)
    if names_dir is not None:
        extra["PLAYBOOK_CONFIDENTIAL_NAMES_DIR"] = str(names_dir)
    return subprocess.run(
        [sys.executable, "scripts/pre-push-confidentiality.py", "origin"],
        cwd=repo, input=f"refs/heads/x {lsha} refs/heads/x {rsha}\n", capture_output=True, text=True, env=_env(**extra),
    )


def _terms(tmp: Path) -> Path:
    d = tmp / "terms"
    d.mkdir()
    (d / "terms.txt").write_text(TERMS, encoding="utf-8")
    return d


class Scanner(unittest.TestCase):
    def test_scanner_file_carries_no_term(self) -> None:
        text = SCANNER.read_text(encoding="utf-8").casefold()
        for term in ("zorblax", "quillfeather"):
            self.assertNotIn(term, text)

    def test_every_commit_in_range_is_scanned_not_just_the_tip(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, names = _repo(tmp), _terms(tmp)
            base = _git(repo, "rev-parse", "HEAD")
            _commit(repo, "b.txt", "an intermediate commit mentions Zorblax Holdings\n", "step one")
            tip = _commit(repo, "b.txt", "clean again\n", "step two")
            result = _scan(repo, tip, base, names)
        self.assertEqual(result.returncode, 1)
        self.assertIn("added content", result.stderr)
        self.assertNotIn("Zorblax", result.stdout + result.stderr)

    def test_message_path_and_slug_forms_are_caught(self) -> None:
        for label, name, content, message in (
            ("message", "c.txt", "ok\n", "notes on quillfeather deal"),
            ("path", "zorblax-holdings-notes.txt", "ok\n", "add notes"),
            ("slug", "d.txt", "zorblax_holdings and ZORBLAX-HOLDINGS\n", "add"),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                repo, names = _repo(tmp), _terms(tmp)
                base = _git(repo, "rev-parse", "HEAD")
                tip = _commit(repo, name, content, message)
                result = _scan(repo, tip, base, names)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("Zorblax", result.stderr)
                self.assertNotIn("Quillfeather", result.stderr)

    def test_a_clean_push_passes(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, names = _repo(tmp), _terms(tmp)
            base = _git(repo, "rev-parse", "HEAD")
            tip = _commit(repo, "e.txt", "nothing to see\n", "tidy")
            self.assertEqual(_scan(repo, tip, base, names).returncode, 0)

    def test_the_scanner_itself_fails_closed_with_no_term_list(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo = _repo(tmp)
            base = _git(repo, "rev-parse", "HEAD")
            tip = _commit(repo, "e.txt", "x\n", "tidy")
            empty = tmp / "none"
            empty.mkdir()
            self.assertEqual(_scan(repo, tip, base, empty).returncode, 1)


class HookWiring(unittest.TestCase):
    def _run_hook(self, repo: Path, lsha: str, rsha: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", ".githooks/pre-push", "origin", "git@example.invalid:o/r.git"],
            cwd=repo, input=f"refs/heads/feat {lsha} refs/heads/feat {rsha}\n", capture_output=True, text=True, env=_env(**env),
        )

    def test_hook_blocks_a_hit_and_allows_a_clean_push(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, names = _repo(tmp), _terms(tmp)
            base = _git(repo, "rev-parse", "HEAD")
            bad = _commit(repo, "f.txt", "Quillfeather\n", "add")
            blocked = self._run_hook(repo, bad, base, PLAYBOOK_CONFIDENTIAL_NAMES_DIR=str(names))
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("pre-push BLOCKED", blocked.stderr)
            good = _commit(repo, "g.txt", "fine\n", "add more")
            clean = self._run_hook(repo, good, bad, PLAYBOOK_CONFIDENTIAL_NAMES_DIR=str(names))
            self.assertEqual(clean.returncode, 0, clean.stderr)
            whole_range = self._run_hook(repo, good, base, PLAYBOOK_CONFIDENTIAL_NAMES_DIR=str(names))
            self.assertNotEqual(whole_range.returncode, 0, "the earlier commit of the range still hits")

    def test_hook_scans_even_when_the_main_guard_is_lifted(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, names = _repo(tmp), _terms(tmp)
            base = _git(repo, "rev-parse", "HEAD")
            bad = _commit(repo, "f.txt", "Zorblax Holdings\n", "add")
            result = subprocess.run(
                ["bash", ".githooks/pre-push", "origin", "u"], cwd=repo, capture_output=True, text=True,
                input=f"refs/heads/main {bad} refs/heads/main {base}\n",
                env=_env(LAND_TO_MAIN="1", CONTRACT_TOASTER_LOOP="1", PLAYBOOK_CONFIDENTIAL_NAMES_DIR=str(names)),
            )
        self.assertNotEqual(result.returncode, 0)

    def test_hook_skips_with_a_notice_when_there_is_no_term_list(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo = _repo(tmp)
            base = _git(repo, "rev-parse", "HEAD")
            tip = _commit(repo, "f.txt", "Zorblax Holdings\n", "add")
            empty = tmp / "none"
            empty.mkdir()
            result = self._run_hook(repo, tip, base, PLAYBOOK_CONFIDENTIAL_NAMES_DIR=str(empty))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("confidentiality scan SKIPPED", result.stderr)

    def test_hook_does_not_scan_on_a_ci_runner(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo, names = _repo(tmp), _terms(tmp)
            base = _git(repo, "rev-parse", "HEAD")
            tip = _commit(repo, "f.txt", "Zorblax Holdings\n", "add")
            result = self._run_hook(repo, tip, base, CI="1", PLAYBOOK_CONFIDENTIAL_NAMES_DIR=str(names))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_main_guard_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = _repo(Path(t))
            sha = _git(repo, "rev-parse", "HEAD")
            result = subprocess.run(
                ["bash", ".githooks/pre-push", "origin", "u"], cwd=repo, capture_output=True, text=True,
                input=f"refs/heads/x {sha} refs/heads/main {ZERO}\n", env=_env(),
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LAND_TO_MAIN=1", result.stderr)


class Workflow(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_it_skips_with_a_notice_when_the_secret_is_absent(self) -> None:
        self.assertIn("secrets.CONFIDENTIAL_TERMS", self.text)
        self.assertIn("::notice::confidential-terms-gate SKIPPED", self.text)
        self.assertIn('[ -z "${CONFIDENTIAL_TERMS:-}" ]', self.text)

    def test_it_never_echoes_the_terms_and_does_not_trace(self) -> None:
        self.assertNotIn("set -x", self.text)
        for line in self.text.splitlines():
            if "echo" in line and "CONFIDENTIAL_TERMS" in line and "::notice::" not in line:
                self.fail(f"echoes the secret: {line!r}")
        self.assertIn("umask 077", self.text)
        self.assertIn("RUNNER_TEMP", self.text)
        self.assertIn("rm -rf", self.text)

    def test_it_scans_the_pr_range_with_full_history(self) -> None:
        self.assertIn("fetch-depth: 0", self.text)
        self.assertIn("github.event.pull_request.base.sha", self.text)
        self.assertIn("github.event.pull_request.head.sha", self.text)
        self.assertIn("scripts/pre-push-confidentiality.py", self.text)


if __name__ == "__main__":
    unittest.main()
