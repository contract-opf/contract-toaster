#!/usr/bin/env python3
"""Unit test for tests/lint-publication-markers.py (QA remediation W2).

Copies the lint into a throwaway git repository (it computes its scope from
`git ls-files` under its own parent's parent) and runs it end to end:

- a tracked file carrying a self-declared internal marker -> exit 1;
- a tracked file carrying a macOS session-temp or home-directory path -> exit 1;
- clean look-alike text only -> exit 0.

The planted strings are assembled from pieces so this file does not itself
carry a marker and needs no allowlist entry in the lint.

pytest style (see tests/README.md): no-argument test functions plus a
`__main__` runner, so it runs under scripts/check.sh and under pytest.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LINT = Path(__file__).resolve().parent / "lint-publication-markers.py"

MARKER = "Internal. Not for " + "publication."
SHOUTED_MARKER = "NEVER " + "PUBLISH this file."
TMP_PATH = "bash /private/" + "tmp/claude-9/scratchpad/roll.sh"
HOME_PATH = "/" + "Users/someone/.claude/skills/x.js"
CLEAN = (
    "GET /users/{id} returns the record.\n"
    "Never write plans to `/private/tmp` -- macOS purges it.\n"
    "Publication is a separate step.\n"
)


def _run_in_repo(files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "tests").mkdir()
        shutil.copy(LINT, root / "tests" / LINT.name)
        for rel, body in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)  # noqa: S607
        return subprocess.run(  # noqa: S603 -- fixed argv: this interpreter + the copied lint
            [sys.executable, str(root / "tests" / LINT.name)],
            cwd=root,
            capture_output=True,
            text=True,
        )


def test_fails_on_planted_marker() -> None:
    r = _run_in_repo({"docs/plans/x.md": f"# X\n\n**{MARKER}**\n"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "docs/plans/x.md:3" in r.stdout, r.stdout


def test_fails_on_marker_any_case() -> None:
    r = _run_in_repo({"notes.md": SHOUTED_MARKER + "\n"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "notes.md:1" in r.stdout, r.stdout


def test_fails_on_private_paths() -> None:
    r = _run_in_repo({"a.md": TMP_PATH + "\n", "b.js": f'const p = "{HOME_PATH}";\n'})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "a.md:1" in r.stdout and "b.js:1" in r.stdout, r.stdout


def test_passes_on_clean_input() -> None:
    r = _run_in_repo({"docs/ok.md": CLEAN})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PUBLICATION-MARKERS LINT: PASS" in r.stdout, r.stdout


def test_lint_alone_passes() -> None:
    """The lint's own source names every pattern; its allowlist entry is
    what keeps it from flagging itself."""
    r = _run_in_repo({})
    assert r.returncode == 0, r.stdout + r.stderr


TESTS = [
    test_fails_on_planted_marker,
    test_fails_on_marker_any_case,
    test_fails_on_private_paths,
    test_passes_on_clean_input,
    test_lint_alone_passes,
]


def main() -> int:
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
