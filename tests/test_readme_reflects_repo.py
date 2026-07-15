#!/usr/bin/env python3
"""
Structural gate for issue #275: README/LICENSE must describe the real tree.

Architecture review (2026-07-12) found that README.md's repository-layout
block and LICENSE's third-party attribution paragraph both name components
that do not exist in the tree (`prompts/`, `backend/vendor/` with
`claude-for-legal/` and `docx/` forks), and that the standard-forms entry
still says the canonical `.docx` is "not yet committed" when a synthetic
placeholder has in fact been committed. Separately, the README quickstart
still leads with a `cdk deploy` AWS flow instead of the DTS Docker Compose
stack described in docs/REVIEW-GUIDE.md.

  A. Every path in README's fenced repository-layout block exists on disk
     (files as files, directories as directories). This catches phantom
     entries like `prompts/` and `backend/vendor/claude-for-legal/` and
     `backend/vendor/docx/`.
  B. The layout block does not list a `standard-forms/eiaa-v1.0.0.docx`
     entry annotated "not yet committed" — the committed file is
     `eiaa-v1.0.0.SYNTHETIC.docx`.
  C. README's Redlining bullet describes `scripts/redline_docx_writer.py`
     as owned/original code, not a library "vendored" from a third party.
  D. README's review-prompt bullet points at the real prompt-assembly
     script (`scripts/primary_review_pass.py`), not a `prompts/` directory.
  E. README's local-development instructions lead with the DTS Docker
     Compose quickstart (linking docs/REVIEW-GUIDE.md) before the AWS CDK
     deploy flow.
  F. LICENSE's third-party attribution paragraph no longer claims
     `anthropics/skills` / `anthropics/claude-for-legal` components are
     incorporated into the tree (no vendored directory exists to back
     that claim).

Exit codes: 0 = all checks pass, 1 = one or more failures.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
LICENSE = REPO_ROOT / "LICENSE"

TREE_LINE_RE = re.compile(r"^((?:│   |    )*)(├── |└── )(.+)$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _extract_layout_block(readme_text: str) -> str:
    match = re.search(r"## Repository layout\s*\n```\n(.*?)```", readme_text, re.DOTALL)
    if not match:
        raise AssertionError("README.md has no fenced '## Repository layout' code block")
    return match.group(1)


def _parse_layout_paths(block: str) -> list[tuple[str, bool, str]]:
    """Return [(relative_path, is_dir, raw_line), ...] for every entry below the root."""
    entries: list[tuple[str, bool, str]] = []
    stack: list[str] = []
    for raw in block.splitlines():
        if not raw.strip():
            continue
        m = TREE_LINE_RE.match(raw)
        if not m:
            continue  # the root line, e.g. "contract-toaster/"
        prefix, _marker, rest = m.groups()
        depth = len(prefix) // 4
        name_full = rest.split("#", 1)[0].rstrip()
        is_dir = name_full.endswith("/")
        name = name_full.rstrip("/")
        stack = stack[:depth]
        stack.append(name)
        rel_path = "/".join(stack)
        entries.append((rel_path, is_dir, raw))
    return entries


# ---------------------------------------------------------------------------
# Check A / B — repository-layout block matches the real tree
# ---------------------------------------------------------------------------

def check_ab_layout_matches_tree() -> list[str]:
    print("\nCheck A/B: README repository-layout block matches the real tree …")
    failures = []
    readme_text = _read(README)
    block = _extract_layout_block(readme_text)
    entries = _parse_layout_paths(block)

    failures += _assert(
        len(entries) > 0,
        "repository-layout block has parseable entries",
    )

    for rel_path, is_dir, raw in entries:
        path = REPO_ROOT / rel_path
        if is_dir:
            failures += _assert(
                path.is_dir(),
                f"{rel_path}/ exists as a directory",
                f"README line: {raw.strip()!r}",
            )
        else:
            failures += _assert(
                path.is_file(),
                f"{rel_path} exists as a file",
                f"README line: {raw.strip()!r}",
            )

    failures += _assert(
        "not yet committed" not in block,
        "layout block does not claim a file is 'not yet committed'",
        "The tree has standard-forms/eiaa-v1.0.0.SYNTHETIC.docx; describe that "
        "reality instead of a phantom pending file.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — redline writer described as owned, not vendored
# ---------------------------------------------------------------------------

def check_c_redline_writer_owned() -> list[str]:
    print("\nCheck C: README describes the redline writer as owned stdlib code …")
    text = _read(README)
    failures = []
    failures += _assert(
        "scripts/redline_docx_writer.py" in text,
        "README references scripts/redline_docx_writer.py",
    )
    failures += _assert(
        "vendored into our own tree" not in text,
        "README does not claim the docx writer was 'vendored' from a third party",
    )
    return failures


# ---------------------------------------------------------------------------
# Check D — prompts described as code-assembled, not a prompts/ directory
# ---------------------------------------------------------------------------

def check_d_prompts_code_assembled() -> list[str]:
    print("\nCheck D: README describes prompts as code-assembled …")
    text = _read(README)
    failures = []
    failures += _assert(
        "scripts/primary_review_pass.py" in text,
        "README references scripts/primary_review_pass.py for prompt assembly",
    )
    return failures


# ---------------------------------------------------------------------------
# Check E — DTS compose quickstart is primary, before the AWS CDK flow
# ---------------------------------------------------------------------------

def check_e_dts_quickstart_primary() -> list[str]:
    print("\nCheck E: DTS compose quickstart precedes the AWS CDK deploy flow …")
    text = _read(README)
    failures = []

    local_dev_idx = text.find("## Local development")
    failures += _assert(
        local_dev_idx != -1,
        "README has a '## Local development' section",
    )
    quickstart_text = text[local_dev_idx:] if local_dev_idx != -1 else text

    dts_idx = quickstart_text.find("docker compose")
    cdk_idx = quickstart_text.find("cdk deploy")

    failures += _assert(
        dts_idx != -1,
        "README's local-development section shows a 'docker compose' command",
    )
    failures += _assert(
        cdk_idx == -1 or (dts_idx != -1 and dts_idx < cdk_idx),
        "the DTS 'docker compose' quickstart appears before the AWS 'cdk deploy' flow",
        f"docker compose at char {dts_idx}, cdk deploy at char {cdk_idx}",
    )
    failures += _assert(
        "docs/REVIEW-GUIDE.md" in text,
        "README links to docs/REVIEW-GUIDE.md",
    )
    return failures


# ---------------------------------------------------------------------------
# Check F — LICENSE third-party attribution reconciled with reality
# ---------------------------------------------------------------------------

def check_f_license_attribution() -> list[str]:
    print("\nCheck F: LICENSE third-party attribution matches reality …")
    text = _read(LICENSE)
    failures = []
    failures += _assert(
        "anthropics/skills" not in text and "anthropics/claude-for-legal" not in text,
        "LICENSE does not claim anthropics/skills or anthropics/claude-for-legal "
        "components are incorporated (no vendored directory exists in the tree)",
    )
    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("README/LICENSE-reflects-repo structural gate (issue #275)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_ab_layout_matches_tree()
    all_failures += check_c_redline_writer_owned()
    all_failures += check_d_prompts_code_assembled()
    all_failures += check_e_dts_quickstart_primary()
    all_failures += check_f_license_attribution()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: README and LICENSE reflect the real tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
