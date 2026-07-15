#!/usr/bin/env python3
"""
Structural gate for issue #49: repo bootstrap AC coverage.

Verifies that all file-system-checkable acceptance criteria for the repo
bootstrap (issue #49) are satisfied:

  A. Required root files exist (README.md, ARCHITECTURE.md, RUNBOOK.md, LICENSE).
  B. .github/ contains issue templates, PR template, CODEOWNERS, labels.yml.
  C. .gitignore covers Python, Node, CDK, IDE, AWS, and OS artefacts.
  D. playbooks/schema.json and playbooks/eiaa-v1.0.0.json are committed.
  E. labels.yml declares the legal-review-required label.
  F. PR template checklist references the legal-review-required label.
  G. CODEOWNERS gates eval/** under @exos-legal/gc (architecture-review
     reconciliation #10 — eval/ is a future path that must be gated before
     any content lands there).
  H. standard-forms/ directory exists (architecture-review reconciliation #3).

Branch-protection and GitHub-API checks (milestones with target dates, live
label sync, and CODEOWNERS-enforcement via "Require review from Code Owners")
are not testable in CI without admin credentials and are tracked as deferred
manual controls.

Exit codes: 0 = all checks pass, 1 = one or more failures.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GC_TEAM = "@exos-legal/gc"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    """Return a list of failure messages (empty == pass)."""
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


# ---------------------------------------------------------------------------
# Check A — required root files
# ---------------------------------------------------------------------------

def check_a_root_files() -> list[str]:
    print("\nCheck A: required root files …")
    failures = []
    for name in ("README.md", "ARCHITECTURE.md", "RUNBOOK.md", "LICENSE"):
        path = REPO_ROOT / name
        failures += _assert(
            path.is_file(),
            f"{name} exists at repo root",
            f"Expected: {path}",
        )
    return failures


# ---------------------------------------------------------------------------
# Check B — .github/ structure
# ---------------------------------------------------------------------------

def check_b_github_structure() -> list[str]:
    print("\nCheck B: .github/ structure …")
    failures = []
    github = REPO_ROOT / ".github"
    required = [
        ("CODEOWNERS", github / "CODEOWNERS"),
        ("labels.yml", github / "labels.yml"),
        ("PULL_REQUEST_TEMPLATE.md", github / "PULL_REQUEST_TEMPLATE.md"),
        ("ISSUE_TEMPLATE/ directory", github / "ISSUE_TEMPLATE"),
    ]
    for label, path in required:
        failures += _assert(
            path.exists(),
            f".github/{label} exists",
            f"Expected: {path}",
        )
    return failures


# ---------------------------------------------------------------------------
# Check C — .gitignore coverage
# ---------------------------------------------------------------------------

_GITIGNORE_SENTINELS = [
    # Python
    ("Python: __pycache__/", r"__pycache__"),
    ("Python: *.py[cod]", r"\*\.py\[cod\]"),
    ("Python: .venv/ or venv/", r"\.?venv/"),
    # Node
    ("Node: node_modules/", r"node_modules/"),
    # CDK
    ("CDK: cdk.out/", r"cdk\.out/"),
    # IDE
    ("IDE: .vscode/", r"\.vscode/"),
    ("IDE: .idea/", r"\.idea/"),
    # AWS
    ("AWS: .aws-sam/", r"\.aws-sam/"),
    # OS
    ("OS: .DS_Store", r"\.DS_Store"),
    ("OS: Thumbs.db", r"Thumbs\.db"),
]


def check_c_gitignore() -> list[str]:
    print("\nCheck C: .gitignore coverage …")
    path = REPO_ROOT / ".gitignore"
    failures = _assert(path.is_file(), ".gitignore exists")
    if failures:
        return failures
    text = _read(path)
    for label, pattern in _GITIGNORE_SENTINELS:
        failures += _assert(
            bool(re.search(pattern, text)),
            f".gitignore covers {label}",
        )
    return failures


# ---------------------------------------------------------------------------
# Check D — playbooks/ artefacts
# ---------------------------------------------------------------------------

def check_d_playbooks() -> list[str]:
    print("\nCheck D: playbooks/ artefacts …")
    failures = []
    for name in ("schema.json", "eiaa-v1.0.0.json"):
        path = REPO_ROOT / "playbooks" / name
        failures += _assert(
            path.is_file(),
            f"playbooks/{name} exists",
            f"Expected: {path}",
        )
    return failures


# ---------------------------------------------------------------------------
# Check E — labels.yml declares legal-review-required
# ---------------------------------------------------------------------------

def check_e_labels_yml() -> list[str]:
    print("\nCheck E: labels.yml declares legal-review-required …")
    path = REPO_ROOT / ".github" / "labels.yml"
    failures = _assert(path.is_file(), "labels.yml exists")
    if failures:
        return failures
    text = _read(path)
    failures += _assert(
        "legal-review-required" in text,
        'labels.yml contains "legal-review-required" entry',
        "The label must exist so GitHub label sync creates it.",
    )
    return failures


# ---------------------------------------------------------------------------
# Check F — PR template references legal-review-required
# ---------------------------------------------------------------------------

def check_f_pr_template() -> list[str]:
    print("\nCheck F: PR template references legal-review-required …")
    path = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
    failures = _assert(path.is_file(), "PULL_REQUEST_TEMPLATE.md exists")
    if failures:
        return failures
    text = _read(path)
    failures += _assert(
        "legal-review-required" in text,
        'PR template mentions "legal-review-required"',
        "The template checklist must reference the label so authors know to apply it.",
    )
    return failures


# ---------------------------------------------------------------------------
# Check G — CODEOWNERS gates eval/** under @exos-legal/gc
#
# Architecture-review reconciliation #10 (issue #49 body):
# "CODEOWNERS must additionally gate … eval/**"
# eval/ is a future directory for the evaluation harness.  The CODEOWNERS entry
# must exist before any content lands there so engineering cannot approve an
# eval-fixture change without GC sign-off.
# ---------------------------------------------------------------------------

def _parse_gc_patterns(codeowners_text: str) -> list[str]:
    """Return path-patterns from CODEOWNERS lines that list @exos-legal/gc."""
    patterns = []
    for raw in codeowners_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and GC_TEAM in parts[1:]:
            patterns.append(parts[0])
    return patterns


def check_g_codeowners_eval() -> list[str]:
    print("\nCheck G: CODEOWNERS gates eval/** under @exos-legal/gc …")
    path = REPO_ROOT / ".github" / "CODEOWNERS"
    failures = _assert(path.is_file(), "CODEOWNERS exists")
    if failures:
        return failures

    gc_patterns = _parse_gc_patterns(_read(path))

    # Accept any pattern that unambiguously covers the eval tree:
    # /eval/, /eval/**, eval/, eval/** are all valid.
    eval_pattern = re.compile(r"^/?eval(/(\*\*)?)?$")
    covered = any(eval_pattern.match(p) for p in gc_patterns)

    failures += _assert(
        covered,
        f"CODEOWNERS has an eval/** rule owned by {GC_TEAM}",
        f"GC-gated patterns found: {gc_patterns}\n"
        "Fix: add '/eval/**  @exos-legal/gc @exos-legal/engineering' to "
        ".github/CODEOWNERS before eval/ content is committed.",
    )
    return failures


# ---------------------------------------------------------------------------
# Check H — standard-forms/ directory exists
#
# Architecture-review reconciliation #3 (issue #49 body):
# "Repo layout must include standard-forms/ for the content-addressed canonical .docx"
# ---------------------------------------------------------------------------

def check_h_standard_forms() -> list[str]:
    print("\nCheck H: standard-forms/ directory exists …")
    path = REPO_ROOT / "standard-forms"
    failures = _assert(
        path.is_dir(),
        "standard-forms/ directory exists",
        f"Expected: {path}",
    )
    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Repo bootstrap structural gate (issue #49)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_root_files()
    all_failures += check_b_github_structure()
    all_failures += check_c_gitignore()
    all_failures += check_d_playbooks()
    all_failures += check_e_labels_yml()
    all_failures += check_f_pr_template()
    all_failures += check_g_codeowners_eval()
    all_failures += check_h_standard_forms()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print(
        f"\nPASS: all repo-bootstrap structural checks passed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
