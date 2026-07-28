#!/usr/bin/env python3
"""
CI gate (issue #348): public-cut de-brand lint for the four docs cleared for
publication by the issue #348 GRIND SPEC — ARCHITECTURE.md, RUNBOOK.md,
docs/threat-model.md, docs/output-contract.md.

## Problem this guards against

Issue #348 straight-de-branded these four docs for the public cut: real
tenant identity (`teamexos.com` / `@teamexos.com` / `hd=teamexos.com`), the
real dev AWS account ID, and prose references to the tenant name "Exos" all
had to be reworded or replaced before these docs ship in the public repo.
Nothing enforced that going forward, so a future edit could silently
reintroduce a `teamexos.com` example, paste a real account ID into a runbook
snippet, or slip "Exos" back into prose. This lint fails loudly on any of
those three regressions in the four cleared docs.

## What counts as a violation

1. The literal substring `teamexos` (case-insensitive) anywhere in the file
   — catches `teamexos.com`, `@teamexos.com`, and `hd=teamexos.com` alike.
2. Any standalone 12-digit number that is not the generic placeholder
   `123456789012` — a real AWS account ID is always 12 digits, and the one
   permitted 12-digit string in these docs is the placeholder itself.
3. The word "Exos" (case-sensitive, so it does not false-positive on the
   unrelated lowercase code identifier `no-exos-indemnity` — a real rule
   name used across the codebase and out of scope for a docs-only rename)
   on any line that does not itself mention "trademark" or "NOTICE" — the
   GRIND SPEC's carve-out for a future trademark/attribution notice.

## Self-test

Before trusting the real scan, this proves the scanner actually catches each
violation class (and does not false-positive on the `no-exos-indemnity`
code-identifier case) against disposable temp files, never real source.

Run: python3 tests/lint-public-cut-debrand.py
Exit 0 = pass, 1 = fail.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TARGET_FILES = (
    "ARCHITECTURE.md",
    "RUNBOOK.md",
    "docs/threat-model.md",
    "docs/output-contract.md",
)

_TEAMEXOS_RE = re.compile(r"teamexos", re.IGNORECASE)
_TWELVE_DIGIT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_EXOS_WORD_RE = re.compile(r"\bExos\b")
_ALLOWED_ACCOUNT_ID_PLACEHOLDER = "123456789012"
_NOTICE_CARVEOUT_RE = re.compile(r"trademark|NOTICE", re.IGNORECASE)


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return [(lineno, kind, line), ...] for every violation found in text."""
    violations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _TEAMEXOS_RE.search(line):
            violations.append((lineno, "teamexos", line))
        for match in _TWELVE_DIGIT_RE.finditer(line):
            if match.group(0) != _ALLOWED_ACCOUNT_ID_PLACEHOLDER:
                violations.append((lineno, f"12-digit-id:{match.group(0)}", line))
        if _EXOS_WORD_RE.search(line) and not _NOTICE_CARVEOUT_RE.search(line):
            violations.append((lineno, "Exos", line))
    return violations


# ---------------------------------------------------------------------------
# Self-test: prove the scanner catches each violation class, and does not
# false-positive on the no-exos-indemnity code identifier.
# ---------------------------------------------------------------------------


def _self_test_scanner_catches_each_violation_class() -> None:
    dirty = (
        "Google Workspace domain: teamexos.com\n"
        "Dev account: 464817648595\n"
        "Placeholder account: 123456789012\n"
        "Exos already has a Word add-in.\n"
        "The no-exos-indemnity rule is fine.\n"
        "Contract Toaster is a trademark of Exos.\n"
    )
    violations = scan_text(dirty)
    kinds = [kind for _, kind, _ in violations]

    if not any(k == "teamexos" for k in kinds):
        raise AssertionError("self-test failed: did not flag 'teamexos.com'")
    if not any(k == "12-digit-id:464817648595" for k in kinds):
        raise AssertionError("self-test failed: did not flag the real 12-digit account ID")
    if any("123456789012" in k for k in kinds):
        raise AssertionError("self-test failed: flagged the allowed placeholder account ID")
    exos_lines = [line for lineno, kind, line in violations if kind == "Exos"]
    if not any("Word add-in" in line for line in exos_lines):
        raise AssertionError("self-test failed: did not flag prose 'Exos' outside a NOTICE/trademark line")
    if any("trademark" in line for line in exos_lines):
        raise AssertionError("self-test failed: flagged 'Exos' on a trademark-carveout line")
    if any("no-exos-indemnity" in line for line in exos_lines):
        raise AssertionError(
            "self-test failed: false-positived on the no-exos-indemnity "
            "lowercase code identifier"
        )

    clean = (
        "Google Workspace domain: company.com\n"
        "Dev account: 123456789012\n"
        "Your organization already has a Word add-in.\n"
        "The no-exos-indemnity rule is fine.\n"
    )
    clean_violations = scan_text(clean)
    if clean_violations:
        raise AssertionError(f"self-test failed: clean text flagged as dirty: {clean_violations!r}")


# ---------------------------------------------------------------------------
# The real gate.
# ---------------------------------------------------------------------------


def scan_repo(repo_root: Path, target_files: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    for rel in target_files:
        path = repo_root / rel
        if not path.exists():
            failures.append(f"{rel}: MISSING (expected one of the issue #348 public-cut docs)")
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, kind, line in scan_text(text):
            failures.append(f"{rel}:{lineno}: [{kind}] {line.strip()}")
    return failures


def main() -> int:
    try:
        _self_test_scanner_catches_each_violation_class()
    except AssertionError as exc:
        print(f"FAIL (lint self-test): {exc}", file=sys.stderr)
        return 1
    print("Self-test OK: scanner catches teamexos/account-id/Exos violations, skips code identifiers.")

    failures = scan_repo(REPO_ROOT, TARGET_FILES)

    if failures:
        print("\nFAIL: public-cut de-brand violation(s) found:\n")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nIssue #348: these four docs are cleared for the public cut and must "
            "not carry teamexos.com/@teamexos.com identity, a real 12-digit AWS "
            "account ID, or the tenant name 'Exos' in prose. Reword using 'your' / "
            "'your organization' / 'the company' as reads naturally, or use the "
            "123456789012 placeholder for an account ID."
        )
        return 1

    print(f"PASS: no public-cut de-brand violations in {list(TARGET_FILES)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
