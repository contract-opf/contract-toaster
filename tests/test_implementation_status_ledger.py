#!/usr/bin/env python3
"""
Red gate for issue #230: RUNBOOK/ARCHITECTURE document an extensive admin UI
and workflows that don't exist — systemic doc-ahead-of-code drift.

RUNBOOK.md is written as if the product were live: playbook upload/
version-history/rollback (RUNBOOK.md:253-272), release-bundle deactivation
(:286-299), Admin UI -> Corpus -> Upload (:339-346), an audit-log viewer with
CSV export (:349-353), cost-ledger reconcile actions (:400-411), and the
disposition-capture / manual-review filter (:615-617). None of these are
reachable end-to-end today (frontend/src/App.tsx:135-147 ships a sign-in
header plus exactly two admin panels — AdminUsers, AdminRetention).

This test asserts the meta-fix: an implementation-status ledger
(docs/implementation-status.md) exists, marks every one of those RUNBOOK
capabilities SHIPPED / STUBBED / PLANNED, and that the two demo-critical
fictional steps named in the issue (playbook seed, corpus upload) are each
either backed by a real, present CLI script or explicitly marked PLANNED or
STUBBED — never silently left to imply they are operator-usable. It also
asserts the same check is wired into scripts/docs-lint.py as a new Check so
CI enforces it.

This test must FAIL on the pre-fix tree (no ledger file existed) and PASS
once docs/implementation-status.md is added and scripts/docs-lint.py wires
the equivalent check.

Run with: python3 tests/test_implementation_status_ledger.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_CANDIDATES = [
    REPO_ROOT / "docs" / "implementation-status.md",
    REPO_ROOT / "README.md",
]
DOCS_LINT = REPO_ROOT / "scripts" / "docs-lint.py"

STATUS_TOKENS = ("SHIPPED", "STUBBED", "PLANNED")

# (label, required RUNBOOK line-range substring) — the fictional capabilities
# named verbatim in the issue's Evidence section.
REQUIRED_CAPABILITIES = [
    ("playbook upload / version-history / rollback", "253-272"),
    ("release-bundle deactivation", "286-299"),
    ("admin UI corpus upload", "339-346"),
    ("audit-log viewer / CSV export", "349-353"),
    ("cost-ledger reconcile", "400-411"),
    ("disposition capture / manual-review filter", "615-617"),
]

# The two demo-critical fictional steps called out by name in the issue body
# ("Prioritize converting the handful of demo-critical fictional steps
# (playbook seed, corpus upload) into CLI scripts ... or explicitly marked
# PLANNED or STUBBED").  Mapped to the REQUIRED_CAPABILITIES row that covers
# each.
DEMO_CRITICAL_LINE_REFS = ["253-272", "339-346"]


def _find_ledger_file() -> Path | None:
    for path in LEDGER_CANDIDATES:
        if path.exists() and "SHIPPED" in path.read_text(encoding="utf-8"):
            return path
    return None


def _ledger_text() -> str | None:
    path = _find_ledger_file()
    if path is None:
        return None
    return path.read_text(encoding="utf-8")


def check_ledger_exists() -> list[str]:
    failures = []
    path = _find_ledger_file()
    if path is None:
        failures.append(
            "  No implementation-status ledger found. Expected a Markdown "
            "table (in docs/implementation-status.md or README.md) marking "
            "RUNBOOK capabilities SHIPPED / STUBBED / PLANNED."
        )
    return failures


def check_legend_defines_all_statuses() -> list[str]:
    failures = []
    text = _ledger_text()
    if text is None:
        return ["  (skipped — no ledger file)"]
    for token in STATUS_TOKENS:
        if token not in text:
            failures.append(
                f"  Ledger does not use the status token '{token}' anywhere."
            )
    return failures


def _capability_row(text: str, line_ref: str) -> str | None:
    """Return the Markdown table row (line) that cites the given RUNBOOK
    line-range substring, or None if no line in the ledger cites it."""
    for line in text.splitlines():
        if line_ref in line:
            return line
    return None


def check_capabilities_covered_with_status() -> list[str]:
    failures = []
    text = _ledger_text()
    if text is None:
        return ["  (skipped — no ledger file)"]

    for label, line_ref in REQUIRED_CAPABILITIES:
        row = _capability_row(text, line_ref)
        if row is None:
            failures.append(
                f"  Ledger does not cover '{label}' (expected a row citing "
                f"RUNBOOK.md:{line_ref})."
            )
            continue
        if not any(token in row for token in STATUS_TOKENS):
            failures.append(
                f"  Ledger row for '{label}' (RUNBOOK.md:{line_ref}) does "
                f"not carry a SHIPPED/STUBBED/PLANNED status.\n"
                f"    > {row.strip()}"
            )

    return failures


def _script_paths_in_row(row: str) -> list[Path]:
    """Extract candidate scripts/*.py paths mentioned in a ledger row."""
    return [REPO_ROOT / m for m in re.findall(r"scripts/[A-Za-z0-9_\-./]+\.py", row)]


def _script_has_cli_entrypoint(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return '__main__' in text and ('argparse' in text or 'sys.argv' in text)


def check_demo_critical_steps_cli_or_planned() -> list[str]:
    """The issue's bounded scope: 'playbook seed' and 'corpus upload' must
    each be either backed by a real, present CLI script, or explicitly
    marked PLANNED or STUBBED in the ledger — never silently implied as
    operator-usable."""
    failures = []
    text = _ledger_text()
    if text is None:
        return ["  (skipped — no ledger file)"]

    for line_ref in DEMO_CRITICAL_LINE_REFS:
        row = _capability_row(text, line_ref)
        if row is None:
            failures.append(
                f"  No ledger row for demo-critical RUNBOOK.md:{line_ref}."
            )
            continue

        if "PLANNED" in row or "STUBBED" in row:
            continue  # explicitly marked PLANNED or STUBBED — satisfies the bound

        scripts = _script_paths_in_row(row)
        backed = scripts and all(_script_has_cli_entrypoint(p) for p in scripts)
        if not backed:
            failures.append(
                f"  Demo-critical RUNBOOK.md:{line_ref} row is not marked "
                f"PLANNED or STUBBED and does not cite a real CLI script "
                f"with a '__main__' entrypoint.\n"
                f"    > {row.strip()}"
            )

    return failures


def check_docs_lint_wires_ledger_check() -> list[str]:
    """scripts/docs-lint.py must gain a new Check enforcing the same ledger
    presence/coverage in CI (per the issue's Required verification)."""
    failures = []
    if not DOCS_LINT.exists():
        return [f"  {DOCS_LINT.relative_to(REPO_ROOT)}: file not found"]

    text = DOCS_LINT.read_text(encoding="utf-8")

    if "implementation" not in text.lower() or "ledger" not in text.lower():
        failures.append(
            "  scripts/docs-lint.py does not appear to reference an "
            "'implementation status ledger' check anywhere."
        )

    # main() must register a new lettered Check whose name mentions the
    # ledger, alongside the existing Check A-F registrations.
    checks_block_match = re.search(r"checks\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if checks_block_match is None:
        failures.append("  Could not find the checks=[...] registration list in docs-lint.py")
    else:
        checks_block = checks_block_match.group(1)
        if "ledger" not in checks_block.lower():
            failures.append(
                "  docs-lint.py's checks=[...] list does not register a "
                "ledger-related Check (expected a new lettered Check "
                "alongside A-F)."
            )

    return failures


def main() -> int:
    checks = [
        ("1", "Implementation-status ledger file exists", check_ledger_exists),
        ("2", "Legend defines SHIPPED / STUBBED / PLANNED", check_legend_defines_all_statuses),
        ("3", "All six fictional RUNBOOK capabilities covered with a status",
         check_capabilities_covered_with_status),
        ("4", "Demo-critical steps (playbook seed, corpus upload) are CLI-backed, PLANNED, or STUBBED",
         check_demo_critical_steps_cli_or_planned),
        ("5", "scripts/docs-lint.py wires an equivalent CI check",
         check_docs_lint_wires_ledger_check),
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
        print("All implementation-status ledger checks passed.")
        return 0
    else:
        print("One or more implementation-status ledger checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
