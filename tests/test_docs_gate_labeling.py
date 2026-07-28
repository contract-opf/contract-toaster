#!/usr/bin/env python3
"""
Doc-lint labeling gate for issue #196: a large share of the test suite
asserts documentation prose, not behavior, inflating apparent completeness.

This is the meta-fix's own slice test. It does NOT re-implement or
re-verify the underlying doc-prose checks in the three named gate files —
it asserts that each of them now declares an explicit, machine-readable
`GATE_KIND = "documentation-lint"` module attribute, so a reader (or a CI
summary) can distinguish "this gate proves docs say X" from "this gate
proves code does X" without reading every regex by hand.

Files asserted (named in issue #196's Evidence):
  - tests/test_no_active_bundle.py
  - tests/test_prompt_manifest.py
  - tests/test_ci_pipeline.py

It additionally asserts the load-bearing behavioral conversion required by
issue #196: tests/test_no_active_bundle.py's Gate 1a must no longer be a
pure ARCHITECTURE.md prose scan for "POST /api/reviews refuses with 503" —
it must either exercise the real route or explicitly document a skip
reason when the route is unimplemented.

GATE_KIND (issue #196): this module is itself a documentation/structure
lint over other test files' source text (a meta-fix), not a scan of
project docs — it is intentionally NOT given the
GATE_KIND = "documentation-lint" marker, since it partially IS the
behavioral enforcement mechanism for that marker's presence.

Run with: python3 tests/test_docs_gate_labeling.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# The three prose-asserting docs-gate tests named in issue #196's Evidence.
DOCS_GATE_FILES = [
    "test_no_active_bundle.py",
    "test_prompt_manifest.py",
    "test_ci_pipeline.py",
]

EXPECTED_GATE_KIND = "documentation-lint"

GATE_KIND_PATTERN = re.compile(
    r'^GATE_KIND\s*=\s*["\']documentation-lint["\']\s*$',
    re.MULTILINE,
)


def _read(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


def _load_module(path: Path):
    """Import a tests/test_*.py file as a module without executing its
    __main__ block (the module-level GATE_KIND assignment runs at import
    time regardless, which is all we need)."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Check 1: each named docs-gate file declares GATE_KIND via source scan ────


def check_gate_kind_marker_in_source() -> list[str]:
    """Static (regex) check that each named docs-gate file's source text
    declares a module-level GATE_KIND = "documentation-lint" line. This is
    the check that must FAIL before the marker is added (red state)."""
    failures = []
    for filename in DOCS_GATE_FILES:
        path = TESTS_DIR / filename
        try:
            text = _read(path)
        except FileNotFoundError as e:
            failures.append(f"  {e}")
            continue

        if not GATE_KIND_PATTERN.search(text):
            failures.append(
                f"  tests/{filename} does not declare a module-level\n"
                f"  GATE_KIND = \"documentation-lint\" line. Every prose-asserting\n"
                f"  docs-gate named in issue #196's Evidence must carry this\n"
                f"  machine-readable marker so a green suite does not imply an\n"
                f"  enforced runtime invariant for gates that are actually prose\n"
                f"  scans. (issue #196)"
            )
    return failures


# ── Check 2: each named docs-gate file's GATE_KIND is importable and correct ─


def check_gate_kind_marker_importable() -> list[str]:
    """Dynamic check: actually import each file and read the attribute,
    guarding against a marker that's present as a comment/docstring
    mention but not a real assignment (or assigned a wrong value)."""
    failures = []
    for filename in DOCS_GATE_FILES:
        path = TESTS_DIR / filename
        if not path.exists():
            failures.append(f"  tests/{filename} does not exist.")
            continue
        try:
            module = _load_module(path)
        except Exception as e:  # pragma: no cover - environment-dependent
            failures.append(
                f"  tests/{filename} raised {e!r} on import; cannot verify\n"
                f"  GATE_KIND at runtime. (issue #196)"
            )
            continue

        gate_kind = getattr(module, "GATE_KIND", None)
        if gate_kind != EXPECTED_GATE_KIND:
            failures.append(
                f"  tests/{filename}.GATE_KIND is {gate_kind!r}, expected\n"
                f"  {EXPECTED_GATE_KIND!r}. (issue #196)"
            )
    return failures


# ── Check 3: test_no_active_bundle.py Gate 1a is no longer prose-only ────────


def check_no_active_bundle_gate_1a_behavioral() -> list[str]:
    """The load-bearing conversion (issue #196): Gate 1a in
    test_no_active_bundle.py must no longer be a pure regex scan for
    "does ARCHITECTURE.md SAY POST /api/reviews refuses with 503" — it
    must either exercise the real route (a TestClient call asserting a
    real status code) or explicitly document a skip reason when the route
    is unimplemented. We assert this both structurally (source contains a
    real HTTP call and an explicit, documented skip path) and dynamically
    (running the converted gate function actually returns a skip or a
    real assertion outcome, not silent success)."""
    failures = []
    path = TESTS_DIR / "test_no_active_bundle.py"
    text = _read(path)

    behavioral_call_present = re.search(
        r"TestClient|\.post\(\s*[\"']\/api\/reviews[\"']", text
    )
    if not behavioral_call_present:
        failures.append(
            "  tests/test_no_active_bundle.py has no TestClient / real HTTP\n"
            "  call against POST /api/reviews. Gate 1a must exercise the route\n"
            "  when it is wired, not just read docs. (issue #196)"
        )

    documented_skip_present = re.search(
        r"SKIP \(documented reason\)", text
    )
    if not documented_skip_present:
        failures.append(
            "  tests/test_no_active_bundle.py has no documented-skip path for\n"
            "  Gate 1a. Per issue #196, when the route is unimplemented the\n"
            "  gate must explicitly assert-skip with a documented reason rather\n"
            "  than silently pass or fall back to prose-only assertions.\n"
            "  (issue #196)"
        )

    # Dynamic: import the module and run the actual behavioral gate function,
    # confirming it returns a (failures, skips) pair where at least one of
    # them is populated (i.e. it did something observable) rather than
    # silently returning ([], []).
    try:
        module = _load_module(path)
        fn = getattr(module, "gate_1a_route_refusal_behavioral", None)
        if fn is None:
            failures.append(
                "  tests/test_no_active_bundle.py has no\n"
                "  gate_1a_route_refusal_behavioral function. Gate 1a's\n"
                "  behavioral logic must be a separately callable, testable\n"
                "  unit. (issue #196)"
            )
        else:
            gate_failures, gate_skips = fn()
            if not gate_failures and not gate_skips:
                failures.append(
                    "  tests/test_no_active_bundle.py's\n"
                    "  gate_1a_route_refusal_behavioral() returned no failures\n"
                    "  and no skips — it must either assert a real outcome or\n"
                    "  explicitly skip with a documented reason, never silently\n"
                    "  no-op. (issue #196)"
                )
    except Exception as e:  # pragma: no cover - environment-dependent
        failures.append(
            f"  tests/test_no_active_bundle.py raised {e!r} while running\n"
            f"  gate_1a_route_refusal_behavioral(). (issue #196)"
        )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    checks = [
        (
            "1",
            "Each named docs-gate file declares GATE_KIND = \"documentation-lint\" (source scan)",
            check_gate_kind_marker_in_source,
        ),
        (
            "2",
            "Each named docs-gate file's GATE_KIND is importable and correct",
            check_gate_kind_marker_importable,
        ),
        (
            "3",
            "test_no_active_bundle.py Gate 1a converted to behavioral-or-documented-skip",
            check_no_active_bundle_gate_1a_behavioral,
        ),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All docs-gate labeling checks passed.")
        return 0
    else:
        print("One or more docs-gate labeling checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
