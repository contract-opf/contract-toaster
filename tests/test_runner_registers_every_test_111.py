#!/usr/bin/env python3
r"""
Gate-shaped twin of #93 (issue #111): "0 of the new tests ran" must not be
indistinguishable from "all passed".

## The gap this closes

`scripts/collect_test_failures.sh` (shared by `scripts/check.sh` and CI GATE
A) runs each discovered `tests/**/test_*.py` file as `python file.py` and
reads only its process exit status -- nothing else about the file reaches the
gate. About 84% of files enumerate their tests from a hand-maintained list
(`TESTS = [...]`, `for test in TESTS: ...`, the style `tests/README.md` calls
out as the convention for every NEW test). A `def test_new(...)` that is
written but never appended to that list is syntactically fine, imports fine,
and simply never runs -- the file still exits 0 and the gate stays green.

`pytest.ini`'s "pass under both runners" rule does not help: no gate here
invokes pytest (`grep pytest .github/workflows/*.yml` hits only a comment),
so a file that would fail pytest collection (or that pytest would happily
collect and run) never gets that chance in CI.

## What this does NOT check

Files that hand test discovery to `unittest` itself --
`if __name__ == "__main__": unittest.main()`, or a runner that scans
`globals()` for `test_*` callables -- register every matching definition
automatically; there is no hand-maintained list to fall out of sync. This
file skips those (see `uses_known_safe_runner`) rather than re-litigating a
registration mechanism that cannot silently drop an entry. In practice this
also makes the check a no-op on the ~90 class-based files in this tree
(`unittest.TestLoader().loadTestsFromTestCase(SomeCase)` for an explicit list
of *classes*): their `test_*` methods are indented inside a class body, so
the module-level `^def test_\w+\(` pattern below never matches them at all --
forgetting to list a whole `TestCase` subclass is a real, different bug this
file does not claim to catch.

## Evidence (from the issue)

A harness that runs each discovered non-infra file via `runpy` with an
injected failing top-level `test_zz_injected_deliberate_failure()` found 251
of 291 non-infra files exit 0 without ever having seen the injected test --
including `tests/test_review_api_84.py`, whose own `main()` builds a
`unittest.TestSuite` from an explicit tuple of `TestCase` classes rather than
calling `unittest.main()`. That file has zero *module-level* `def test_`
definitions (everything lives in class methods), so it is correctly out of
this file's scope too -- the orphan risk on that file is a forgotten
`TestCase` class in that tuple, not a forgotten module-level function.

## Required verification (from the issue)

    .venv/bin/python tests/test_runner_registers_every_test_111.py

Dual-mode: every check below is a no-argument `test_*` function that raises
`AssertionError` on failure, so this also collects and passes under
`.venv/bin/python -m pytest tests/test_runner_registers_every_test_111.py`
(see tests/README.md, "pytest style -- every NEW test").

Exit codes: 0 = pass, 1 = fail.
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# A MODULE-LEVEL (column-0) `def test_whatever(` -- deliberately anchored so
# an indented method inside a class body never matches; those are registered
# by unittest's own class-based discovery, not by a hand-maintained list.
TEST_DEF_RE = re.compile(r"^def (test_\w+)\(", re.MULTILINE)


def _assert(condition: bool, label: str, detail: str = "") -> None:
    if not condition:
        raise AssertionError(label + (f"\n         {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def uses_known_safe_runner(text: str) -> bool:
    """True for a file whose test registration cannot silently drop an
    entry: `unittest.main()` (module-level `unittest` discovery) or a runner
    that scans `globals()` for its tests. Both discover what is DEFINED, not
    what is LISTED, so there is no hand-maintained list to fall out of sync.

    Classified by parsing `text` with `ast` and looking for an actual `Call`
    node shaped like `unittest.main(...)` or `globals(...)` -- never by
    grepping the raw source. A docstring, comment, or fixture string literal
    that merely MENTIONS either spelling becomes an `ast.Constant`, not a
    `Call`, so a file that only discusses the mechanism is never mistaken
    for one that uses it (issue #111 fix-round-1, finding 1: a raw substring
    match let this very file -- whose docstring and fixtures discuss both
    names in prose -- exempt itself from its own scan).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "main":
            value = func.value
            if isinstance(value, ast.Name) and value.id == "unittest":
                return True
        elif isinstance(func, ast.Name) and func.id == "globals":
            return True
    return False


def find_top_level_test_defs(text: str) -> list[tuple[str, int]]:
    """Every module-level `def test_xxx(` in `text`, as (name, 1-based line)."""
    out: list[tuple[str, int]] = []
    for m in TEST_DEF_RE.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        out.append((m.group(1), line_no))
    return out


def unregistered_tests(text: str) -> list[str]:
    """Names of module-level test functions never referenced anywhere in
    `text` OUTSIDE their own `def` line -- the orphan a hand-maintained
    `TESTS = [...]` list can silently drop (issue #111)."""
    lines = text.splitlines()
    orphans: list[str] = []
    for name, def_line in find_top_level_test_defs(text):
        name_re = re.compile(r"\b" + re.escape(name) + r"\b")
        referenced = any(
            name_re.search(line)
            for i, line in enumerate(lines, start=1)
            if i != def_line
        )
        if not referenced:
            orphans.append(name)
    return orphans


def discover_test_files() -> list[Path]:
    """`tests/**/test_*.py`, recursively -- the file shape the issue scopes
    this to. Excludes `__pycache__`; there is nothing else to exclude."""
    return sorted(
        p for p in TESTS_DIR.rglob("test_*.py") if "__pycache__" not in p.parts
    )


def orphans_in_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if uses_known_safe_runner(text):
        return []
    return unregistered_tests(text)


# ---------------------------------------------------------------------------
# Self-test: prove the scanner catches the class before trusting a real run,
# and that it correctly stays silent on the two known-safe runner shapes.
# ---------------------------------------------------------------------------


def test_synthetic_orphan_fixture_is_detected() -> None:
    """A checked-in-shaped fixture file with one registered and one
    unregistered module-level test -- the exact defect #111 exists to catch."""
    src = (
        "import sys\n\n"
        "def test_registered(failures):\n"
        "    pass\n\n"
        "def test_orphaned(failures):\n"
        "    pass\n\n"
        "TESTS = [test_registered]\n\n"
        "def main():\n"
        "    failures = []\n"
        "    for t in TESTS:\n"
        "        t(failures)\n"
        "    return 0 if not failures else 1\n\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "test_orphan_fixture.py"
        fixture.write_text(src)
        text = fixture.read_text()

        _assert(
            not uses_known_safe_runner(text),
            "fixture wrongly classified as a known-safe runner (unittest.main/globals())",
        )
        found = orphans_in_file(fixture)
        _assert(
            found == ["test_orphaned"],
            "scanner did not isolate exactly the one unregistered test",
            f"got {found!r}",
        )


def test_fully_registered_fixture_has_no_orphans() -> None:
    """The mirror positive case: every module-level test IS in the list, so
    nothing should be flagged -- a scanner that always fires is as useless as
    one that never does."""
    src = (
        "def test_a(failures):\n    pass\n\n"
        "def test_b(failures):\n    pass\n\n"
        "TESTS = [test_a, test_b]\n"
    )
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "test_clean_fixture.py"
        fixture.write_text(src)
        found = orphans_in_file(fixture)
        _assert(found == [], "clean fixture wrongly flagged", f"got {found!r}")


def test_unittest_main_file_is_not_checked_even_with_a_real_orphan() -> None:
    """A file that hands discovery to `unittest.main()` is skipped outright
    -- it has no hand-maintained list to forget an entry in, so even a
    module-level `test_*` def that nothing else names must not be flagged."""
    src = (
        "import unittest\n\n"
        "def test_orphan_helper():\n"
        "    pass\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_something(self):\n"
        "        pass\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    )
    _assert(
        uses_known_safe_runner(src),
        "a file calling unittest.main() must be classified as a known-safe runner",
    )


def test_globals_scan_file_is_not_checked_even_with_a_real_orphan() -> None:
    """Same shape, the other known-safe mechanism: a runner that discovers
    its tests by scanning `globals()` rather than a hand-typed list."""
    src = (
        "def test_orphan():\n"
        "    pass\n\n"
        "def main():\n"
        "    for name, obj in globals().items():\n"
        "        if name.startswith('test_') and callable(obj):\n"
        "            obj()\n"
    )
    _assert(
        uses_known_safe_runner(src),
        "a file scanning globals() must be classified as a known-safe runner",
    )


def test_docstring_mentioning_the_mechanism_is_not_misclassified() -> None:
    """The misclassification branch finding 1 (#111 fix round 1) found
    unexercised: a file whose DOCSTRING and a string-literal constant merely
    discuss `unittest.main()` and `globals()` in prose, but whose actual
    runner is the hand-maintained `TESTS = [...]` list -- exactly the
    mechanism #111 exists to guard -- must still be SCANNED, and its real
    orphan must still be caught. A raw substring match would wrongly exempt
    this fixture the same way it wrongly exempted this guard file itself."""
    src = (
        '"""This module talks about unittest.main() and globals() in\n'
        'prose only -- it does not call either."""\n\n'
        "import sys\n\n"
        'NOTE = "see unittest.main() and globals() for background"\n\n'
        "def test_registered(failures):\n"
        "    pass\n\n"
        "def test_orphaned_but_only_mentioned(failures):\n"
        "    pass\n\n"
        "TESTS = [test_registered]\n\n"
        "def main():\n"
        "    failures = []\n"
        "    for t in TESTS:\n"
        "        t(failures)\n"
        "    return 0 if not failures else 1\n\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    _assert(
        not uses_known_safe_runner(src),
        "a file that only MENTIONS unittest.main()/globals() in a docstring "
        "or string literal must not be classified as a known-safe runner",
    )
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "test_mentions_only_fixture.py"
        fixture.write_text(src)
        found = orphans_in_file(fixture)
        _assert(
            found == ["test_orphaned_but_only_mentioned"],
            "a file that merely mentions the mechanism must still be scanned "
            "for real orphans",
            f"got {found!r}",
        )


def test_this_guard_file_is_itself_scanned_not_exempted() -> None:
    """Regression guard for finding 1 (#111 fix round 1): this file's own
    docstring and fixture string literals mention `unittest.main(` and
    `globals()` repeatedly, but its own runner is the hand-maintained
    `TESTS = [...]` list at the bottom of this file -- exactly the mechanism
    #111 exists to check. It must land in the SCANNED set, never the
    skipped one, so it can never again silently exempt itself."""
    self_path = Path(__file__).resolve()
    self_text = self_path.read_text(encoding="utf-8")
    _assert(
        not uses_known_safe_runner(self_text),
        "this guard file must never classify itself as a known-safe runner",
    )
    _assert(
        self_path in discover_test_files(),
        "this guard file must be part of the discovered/scanned set",
    )


def test_class_based_test_methods_are_never_flagged() -> None:
    """A `def test_x(self):` indented inside a class body is not a
    module-level definition and must never be reported as an orphan by this
    file -- that class of bug (a forgotten `TestCase` in an explicit tuple,
    the tests/test_review_api_84.py shape) is out of scope here."""
    src = (
        "import unittest\n\n"
        "class Forgotten(unittest.TestCase):\n"
        "    def test_never_listed_anywhere_else(self):\n"
        "        pass\n\n"
        "def main():\n"
        "    loader = unittest.TestLoader()\n"
        "    suite = unittest.TestSuite()\n"
        "    return 0\n"
    )
    _assert(
        not uses_known_safe_runner(src),
        "fixture should not accidentally read as unittest.main()/globals()",
    )
    _assert(
        find_top_level_test_defs(src) == [],
        "an indented class method must not be picked up as a module-level test def",
    )


def test_name_that_is_a_prefix_of_another_does_not_false_positive() -> None:
    """`test_foo` referenced only via `test_foo_bar` must still count as
    orphaned -- word-boundary matching, not substring matching."""
    src = (
        "def test_foo(failures):\n    pass\n\n"
        "def test_foo_bar(failures):\n    pass\n\n"
        "TESTS = [test_foo_bar]\n"
    )
    found = unregistered_tests(src)
    _assert(
        found == ["test_foo"],
        "prefix-name collision let an orphan hide behind a similarly-named sibling",
        f"got {found!r}",
    )


# ---------------------------------------------------------------------------
# The real check: the discovered tree, today, has zero orphans.
# ---------------------------------------------------------------------------


def test_discovery_matches_the_gates_own_globs() -> None:
    """`scripts/collect_test_failures.sh` discovers files via THREE globs:
    `tests/test_*.py`, `tests/*/test_*.py` (one level of subdirectory), and
    `tests/lint-*.py`. This file scans `tests/**/test_*.py` (any depth) --
    the shape issue #111 scopes it to -- which is the union of only the
    first two of those; `tests/lint-*.py` is deliberately out of THIS
    file's scope (the ticket scopes this check to `tests/**/test_*.py`, and
    a `tests/lint-*.py` file is not a `test_*.py` file), not a gap the gate
    fails to discover. Assert this file's discovery set agrees with the
    gate's first two globs on the CURRENT tree so "wider recursive glob"
    never quietly means "checking files the gate does not run" or vice
    versa -- if a test file ever moves two directories deep this assertion
    is the tripwire that says so, not a silent gap between what is checked
    and what is gated."""
    gate_discovered = {
        p.relative_to(REPO_ROOT)
        for p in list(TESTS_DIR.glob("test_*.py")) + list(TESTS_DIR.glob("*/test_*.py"))
        if "__pycache__" not in p.parts
    }
    this_file_discovered = {p.relative_to(REPO_ROOT) for p in discover_test_files()}
    _assert(
        this_file_discovered == gate_discovered,
        "this file's discovery set no longer matches the gate's first two "
        "globs (tests/test_*.py, tests/*/test_*.py) -- the gate also has a "
        "third glob, tests/lint-*.py, which is out of this file's scope",
        f"only here: {sorted(this_file_discovered - gate_discovered)}\n"
        f"only in gate: {sorted(gate_discovered - this_file_discovered)}",
    )


def test_no_orphaned_top_level_test_in_the_real_tree() -> None:
    """The actual gate: every module-level `test_*` def in every discovered
    file (outside the unittest.main()/globals() runners) is referenced
    somewhere else in its own file -- i.e. a hand-maintained TESTS list
    cannot have silently dropped it."""
    violations: list[str] = []
    for path in discover_test_files():
        for name in orphans_in_file(path):
            violations.append(f"{path.relative_to(REPO_ROOT)}: {name}")
    _assert(
        violations == [],
        f"{len(violations)} module-level test function(s) defined but never "
        "referenced by their file's own runner (issue #111) -- each one is "
        "silently skipped by every gate that runs this repo:",
        "\n".join(violations),
    )


TESTS = [
    test_synthetic_orphan_fixture_is_detected,
    test_fully_registered_fixture_has_no_orphans,
    test_unittest_main_file_is_not_checked_even_with_a_real_orphan,
    test_globals_scan_file_is_not_checked_even_with_a_real_orphan,
    test_docstring_mentioning_the_mechanism_is_not_misclassified,
    test_this_guard_file_is_itself_scanned_not_exempted,
    test_class_based_test_methods_are_never_flagged,
    test_name_that_is_a_prefix_of_another_does_not_false_positive,
    test_discovery_matches_the_gates_own_globs,
    test_no_orphaned_top_level_test_in_the_real_tree,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {name}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [PASS] {name}")

    print("\n" + "=" * 60)
    print("ALL GREEN" if failures == 0 else f"FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
