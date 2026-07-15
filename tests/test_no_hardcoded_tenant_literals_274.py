#!/usr/bin/env python3
"""
Structural gate for issue #274: no internal tenant-identity literals baked
into backend/src or frontend/src.

Problem (issue #274): the codebase hard-codes the internal tenant's email
domain (teamexos.com) and GitHub org / alarms mailbox strings directly in
source, so an adopter must patch source rather than configure these via
env vars.

Verifies:
  A. None of the literal strings 'teamexos.com', 'exos-legal', or
     'legal-eng@' appear anywhere under backend/src/ or frontend/src/.

Scope note (2026-07-13 descope): the infra portion of #274 (removing the
CDK context defaults in infra/lib/contract-toaster-stack.ts) was split out
to #316 after the tree showed it breaks ~21 unrelated infra structural-gate
tests. This gate therefore checks backend/src/ and frontend/src/ ONLY —
infra/ is explicitly out of scope here.

This file MUST fail on the pre-fix tree (auth.py hard-codes
TEAMEXOS_DOMAIN = "teamexos.com" and App.tsx hard-codes the product name)
and pass once the fix lands.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

FORBIDDEN_LITERALS = ("teamexos.com", "exos-legal", "legal-eng@")

# Source-file extensions to scan under backend/src and frontend/src.
_SCAN_EXTENSIONS = {".py", ".ts", ".tsx", ".html"}


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _iter_source_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in _SCAN_EXTENSIONS:
            yield path


def check_a_no_literals_in_src_trees() -> list[str]:
    print("\nCheck A: no tenant-identity literals under backend/src/ or frontend/src/ …")
    failures: list[str] = []

    for root in (BACKEND_SRC, FRONTEND_SRC):
        failures += _assert(root.is_dir(), f"{root.relative_to(REPO_ROOT)} exists")

    if failures:
        return failures

    hits: list[str] = []
    for root in (BACKEND_SRC, FRONTEND_SRC):
        for path in _iter_source_files(root):
            text = path.read_text(encoding="utf-8", errors="replace")
            for literal in FORBIDDEN_LITERALS:
                if literal in text:
                    for lineno, line in enumerate(text.splitlines(), start=1):
                        if literal in line:
                            hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {literal!r} in {line.strip()!r}")

    failures += _assert(
        not hits,
        "no occurrences of 'teamexos.com' / 'exos-legal' / 'legal-eng@' under backend/src/ or frontend/src/",
        "occurrences found:\n         " + "\n         ".join(hits) if hits else "",
    )
    return failures


def main() -> int:
    print("No hard-coded tenant-identity literals — structural gate (issue #274)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_no_literals_in_src_trees()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: no hard-coded tenant-identity literals remain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
