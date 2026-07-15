#!/usr/bin/env python3
"""
CI gate (issue #289): AST lint that fails on any hard-coded "eiaa" string
literal in the required core (backend/src/, scripts/) outside a checked-in
allowlist.

## Problem this guards against

"Type-blindness" is the property that lets a second contract type (e.g. the
NDA engine, issue #184) land as pure data -- author a playbook.json +
anchor-map + section-config + fixtures dir, add one playbooks/registry.json
entry, no code edit. A hard-coded `playbook_id == "eiaa"` comparison (or a
literal `"playbooks/eiaa-v1.0.0.json"` path) anywhere in the required core
breaks that property silently: the code keeps working for eiaa and nothing
fails until someone tries to add a second playbook and discovers the
special-casing. Issue #289 swept the five call sites that had drifted into
this pattern (playbook_registry.DEFAULT_PLAYBOOK_ID,
backend/src/corpus.py's PLAYBOOK_PATH/DEFAULT_PLAYBOOK_ID,
scripts/diff_standard_form.py's _SYNTHETIC_TEXT_SUPPLEMENTS,
backend/src/pipeline_runner.py's _mock_decision,
backend/src/review_routes.py's Form default) so they resolve through
playbooks/registry.json instead. This lint is what stops the pattern from
coming back.

## What counts as a violation

AST-walks every `.py` file under `backend/src/` and `scripts/`, collecting
every `ast.Constant` string value that contains "eiaa" (case-insensitive)
-- EXCEPT:
  1. Docstrings (the first statement of a module/class/function body, as an
     `ast.Expr` wrapping the Constant) -- prose explaining the eiaa playbook
     by name is fine; a runtime comparison or hard-coded path is not. This
     is a structural check (body[0] is an Expr(Constant(str))), not a
     heuristic, so it cannot be fooled by a docstring-shaped string used as
     a real value, nor does it accidentally exempt a real second-statement
     string constant that merely looks documentation-y.
  2. Files listed in the checked-in allowlist
     (tests/lint-no-eiaa-literals.allowlist.json) -- data-tooling / fixture
     generators and standalone doc-audit scripts that legitimately name the
     eiaa playbook as the specific artifact they generate or audit, not as
     a playbook_id resolution/comparison. Each entry carries a one-line
     reason. A file NOT in the allowlist with a real (non-docstring) "eiaa"
     string constant FAILS with file:line.

## Self-test

Before running the real gate, this script proves its own walker actually
catches a reintroduced literal: it compiles a TEMPORARY file (never real
source) containing a `playbook_id == "eiaa"`-shaped comparison through the
exact same `scan_file()` function the gate below calls, and asserts it is
flagged (while a same-file docstring mention of "eiaa" is not). This is the
issue #289 acceptance criterion "the lint FAILS if you re-add
playbook_id == 'eiaa' anywhere in backend/src/" -- proven by mutation of a
disposable temp file, never by editing real source.

Run: python3 tests/lint-no-eiaa-literals.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = ("backend/src", "scripts")
ALLOWLIST_PATH = Path(__file__).resolve().parent / "lint-no-eiaa-literals.allowlist.json"

_NEEDLE = "eiaa"


def load_allowlist() -> dict[str, str]:
    with open(ALLOWLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """id() of every ast.Constant node that is a genuine docstring: the
    first statement of a Module/ClassDef/FunctionDef/AsyncFunctionDef body,
    expressed as a bare `ast.Expr(ast.Constant(str))`."""
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    return docstring_ids


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, value), ...] for every non-docstring string
    Constant in `path` that contains "eiaa" (case-insensitive)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstring_ids = _docstring_constant_ids(tree)

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstring_ids:
            continue
        if _NEEDLE in node.value.lower():
            violations.append((node.lineno, node.value))
    return violations


# ---------------------------------------------------------------------------
# Self-test: prove the walker itself works before trusting its verdict on
# the real tree.
# ---------------------------------------------------------------------------

_SELF_TEST_SNIPPET = (
    '"""Module docstring mentioning eiaa is fine -- docstrings are exempt."""\n'
    "\n"
    "\n"
    "def _mock_decision(playbook_id):\n"
    '    """Per-function docstring mentioning eiaa is also fine."""\n'
    '    if playbook_id == "eiaa":\n'
    '        return "DONE"\n'
    '    return "MANUAL_REVIEW_REQUIRED"\n'
)


def _self_test_walker_catches_reintroduced_literal() -> None:
    fd, tmp_name = tempfile.mkstemp(suffix=".py", text=True)
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(_SELF_TEST_SNIPPET)

        violations = scan_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if len(violations) != 1:
        raise AssertionError(
            "lint self-test failed: expected exactly 1 violation (the "
            f"playbook_id == 'eiaa' comparison), got {violations!r}. The walker "
            "is either not catching a reintroduced literal or is wrongly "
            "flagging a docstring."
        )
    lineno, value = violations[0]
    if value != "eiaa" or lineno != 6:
        raise AssertionError(f"lint self-test failed: unexpected violation {violations[0]!r}")


# ---------------------------------------------------------------------------
# The real gate.
# ---------------------------------------------------------------------------


def scan_repo(repo_root: Path, target_dirs: tuple[str, ...], allowlist: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for base in target_dirs:
        for path in sorted((repo_root / base).rglob("*.py")):
            rel = str(path.relative_to(repo_root))
            if rel in allowlist:
                continue
            for lineno, value in scan_file(path):
                failures.append(f"{rel}:{lineno}: {value!r}")
    return failures


def main() -> int:
    try:
        _self_test_walker_catches_reintroduced_literal()
    except AssertionError as exc:
        print(f"FAIL (lint self-test): {exc}", file=sys.stderr)
        return 1
    print("Self-test OK: walker catches a reintroduced eiaa literal, skips docstrings.")

    allowlist = load_allowlist()
    failures = scan_repo(REPO_ROOT, TARGET_DIRS, allowlist)

    if failures:
        print("\nFAIL: eiaa string literal(s) found outside the allowlist:\n")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nType-blindness (issue #289): the required core must resolve "
            "playbook_id through playbooks/registry.json "
            "(scripts/playbook_registry.py), never compare against a "
            "hard-coded 'eiaa' string. If this is genuinely data-tooling (a "
            "fixture generator, a docs-consistency checker) rather than core "
            "resolution logic, add the file to "
            "tests/lint-no-eiaa-literals.allowlist.json with a one-line reason."
        )
        return 1

    print(f"PASS: no eiaa string literal outside the allowlist in {list(TARGET_DIRS)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
