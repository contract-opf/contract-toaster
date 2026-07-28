#!/usr/bin/env python3
"""CI gate (issue #404): the engine's PUBLIC SURFACE must carry no tenant brand.

## What this guards

The toaster is a brand-free empty shell that is being developed in public
(epic #408). Issues #403/#412/#413/#422 removed the tenant brand from the
engine; nothing stopped a future edit from putting it back. This lint fails
loudly on any regression, so "brand-free" is a property the build enforces
rather than a state someone remembers to maintain.

## The public surface

Scanned files = `git ls-files` MINUS every path in `public-cut-exclude.txt`.
That manifest is the authoritative list of what never ships publicly (the real
contract corpus under `docs/planning/`, the internal AWS access request), so
scanning exactly the complement means this gate covers precisely what a public
cut would publish -- no more, no less.

## Tiers

1. HARD, zero tolerance: `Exos` / `EXOS`. The tenant brand itself. As of
   issue #422 there are ZERO occurrences on the public surface, so this tier
   is a true zero-tolerance gate.
2. HARD, allowlisted: `teamexos` (the corporate domain). Every remaining
   occurrence is either a de-brand scanner that must name the token as its
   search pattern, or an auth-domain behavior test that uses it as its sample
   allowed-domain. Each allowlist entry carries a justification below.
3. REPORTED, pending the flip: `exos-legal` (the private GitHub org). These
   are REPO IDENTITY -- org URLs, the GHCR image path, CodeBuild's source
   repo, CODEOWNERS teams -- which are functional today and get repointed as
   part of flipping the public repo primary (issue #406). Repointing them
   early would break CODEOWNERS against teams that do not exist yet. The gate
   PRINTS the count every run so the debt cannot be silently forgotten, and
   `--strict-org` (used after #406) promotes this tier to a hard failure.
4. HARD: any `.docx` under `tests/` lacking a SYNTHETIC content marker --
   promoting the public-cut SOFT scan into a blocking check.

## Self-test

Before trusting the real scan, this plants each violation class in temp files
and asserts the scanner catches it, and plants a clean file and asserts it
does not -- so a scanner that silently matches nothing cannot pass.

Run standalone: `python3 tests/lint-brand-free.py [--strict-org]`
Exit codes: 0 = pass, 1 = fail.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "public-cut-exclude.txt"

EXOS_RE = re.compile(r"\bExos\b|\bEXOS\b")
TEAMEXOS_RE = re.compile(r"teamexos", re.IGNORECASE)
ORG_RE = re.compile(r"exos-legal", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Allowlist. The ONLY place a brand token may legitimately live.
# Each entry states WHY. Adding one is a reviewed decision, not a convenience.
# ---------------------------------------------------------------------------

# Files that must contain a token because the token IS their search pattern
# (de-brand scanners) or their assertion (tests pinning the token's absence).
# Without these the enforcement mechanism could not name what it forbids.
GUARD_FILES = {
    "tests/lint-brand-free.py": "this gate itself -- defines the patterns",
    "tests/lint-public-cut-debrand.py": "public-cut de-brand scanner",
    "tests/lint-issue-349-debrand.py": "issue-349 de-brand scanner",
    "tests/lint-public-cut-exclude.py": "exclusion-manifest gate; names the private org",
    "tests/lint-acceptable-variations.py": "references the org in its provenance docstring",
    "tests/test_no_hardcoded_tenant_literals_274.py": "asserts tenant literals are ABSENT",
    "tests/test_policy_document.py": "asserts a policy carries no tenant literal",
    "tests/test_repo_bootstrap.py": "asserts bootstrap docs/config identity",
    "tests/test_codeowners_coverage.py": "asserts CODEOWNERS team ownership",
    "tests/test_phase0_ac_coverage.py": "quotes historical phase-0 acceptance criteria",
    "tests/test_schema_hardening.py": "quotes historical schema URLs",
    "scripts/docs-lint.py": "docs linter; the stale address is its search pattern",
    "scripts/public-cut.sh": "the cut tool; names the private origin by design",
    # De-brand ASSERTIONS: these tests prove the brand does NOT reach rendered
    # output / audit rows / emitted documents. The literal IS the thing under
    # test -- reword it and the test silently stops testing anything. Issue
    # #422/#404 reworded every PROSE mention repo-wide; what survives here is
    # only the assertion (and, in one case, a deliberately brand-bearing
    # fixture that keeps its assertion non-vacuous).
    "tests/redline/test_inplace_tracked_changes.py": "asserts emitted .docx/report carry no brand",
    "tests/test_bundle_activate_rollback_79.py": "asserts rendered output/trail carry no brand",
    "tests/test_example_playbook_registry.py": "asserts sample playbook content carries no brand",
    "tests/test_form_match_router.py": "banned-token tuple for router-emitted strings",
    "tests/test_me_capability_route.py": "asserts the capability payload carries no brand",
    "tests/test_opf_prompt.py": "asserts composed prompt output carries no brand",
    "tests/test_playbook_version_audit_9.py": "asserts the audit trail carries no brand",
    "tests/test_playbook_version_notes.py": "brand-bearing note fixture + non-leak assertion",
    "tests/test_review_api_84.py": "asserts user-facing error copy carries no brand",
    "tests/test_sample_playbook_activation.py": "asserts sample playbook content carries no brand",
    "tests/test_retention_window_config_34.py": "asserts rendered option labels carry no brand",
    "public-cut-exclude.txt": "the exclusion manifest; naming what it excludes is its job",
    "tests/test_infra_appname_prefix_233.py": "asserts CodeBuild no longer hard-codes the org",
}

# Auth/domain behavior tests that use the corporate domain as their SAMPLE
# allowed-domain. Post-#274 the domain is env-driven, so these are arbitrary
# fixture values -- they are allowlisted rather than rewritten because the
# rewrite must stay consistent with the semantics each test encodes.
TEAMEXOS_BEHAVIOR_FILES = {
    "tests/test_auth_jwt.py": "sample allowed-domain in JWT domain-matching tests",
    "tests/test_pre_token_lambda_deny_paths.py": "sample domain in pre-token deny-path tests",
    "tests/test_infra_auth_stack.py": "asserts the hd= pin and group-check wiring",
    "tests/test_infra_app_stack.py": "comments recording the removed hard-coded literal",
    "tests/test_infra_appname_prefix_233.py": "asserts the literal is ABSENT under custom context",
    "tests/test_reviewer_admission.py": "asserts the RUNBOOK admission procedure text",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _excluded_prefixes() -> tuple[set[str], set[str]]:
    """(directory prefixes, exact paths) from the public-cut exclusion manifest."""
    dirs: set[str] = set()
    exact: set[str] = set()
    if not MANIFEST.exists():
        return dirs, exact
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        p = raw.split("#", 1)[0].strip()
        if not p:
            continue
        if p.endswith("/"):
            dirs.add(p)
        else:
            exact.add(p)
    return dirs, exact


def public_surface() -> list[str]:
    dirs, exact = _excluded_prefixes()
    surface = []
    for rel in _tracked_files():
        if rel in exact or any(rel.startswith(d) for d in dirs):
            continue
        surface.append(rel)
    return surface


def _read(rel: str) -> str | None:
    p = REPO_ROOT / rel
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable -- .docx handled separately


def scan(text: str, pattern: re.Pattern) -> list[int]:
    return [i for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


def docx_missing_synthetic_marker(rel: str) -> bool:
    """A tests/ .docx must declare itself synthetic EITHER in its filename
    (`*.SYNTHETIC.docx`, the convention this repo already uses) OR in its
    body text. Filename counts: these fixtures are generated by
    `_generate.py` scripts that name the output rather than embedding a
    marker paragraph, and a marker paragraph would perturb the very
    paragraph/anchor offsets several fixtures exist to pin."""
    if ".SYNTHETIC." in rel.upper():
        return False
    try:
        with zipfile.ZipFile(REPO_ROOT / rel) as z:
            body = z.read("word/document.xml").decode("utf8", "ignore")
        return "SYNTHETIC" not in body.upper()
    except Exception:
        return True  # unreadable .docx is a failure, not a pass


# ---------------------------------------------------------------------------
# Self-test: prove the scanner catches each class before trusting a clean run.
# ---------------------------------------------------------------------------


def self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        dirty = tmp / "dirty.md"
        dirty.write_text("The Exos standard form.\nContact a@teamexos.com\nrepo exos-legal/x\n")
        text = dirty.read_text()
        if not scan(text, EXOS_RE):
            raise AssertionError("self-test failed: did not flag 'Exos'")
        if not scan(text, TEAMEXOS_RE):
            raise AssertionError("self-test failed: did not flag 'teamexos'")
        if not scan(text, ORG_RE):
            raise AssertionError("self-test failed: did not flag 'exos-legal'")

        caps = tmp / "caps.md"
        caps.write_text("EXOS OWNS THIS\n")
        if not scan(caps.read_text(), EXOS_RE):
            raise AssertionError("self-test failed: did not flag 'EXOS'")

        clean = tmp / "clean.md"
        clean.write_text("Contract Toaster is a trademark of Athletes' Performance, Inc.\n")
        ct = clean.read_text()
        if scan(ct, EXOS_RE) or scan(ct, TEAMEXOS_RE) or scan(ct, ORG_RE):
            raise AssertionError("self-test failed: flagged a clean, brand-free line")

        # A non-SYNTHETIC .docx must be flagged; a marked one must not.
        for marker, expect_flag in (("nothing here", True), ("SYNTHETIC sample", False)):
            d = tmp / f"f{expect_flag}.docx"
            with zipfile.ZipFile(d, "w") as z:
                z.writestr("word/document.xml", f"<w:t>{marker}</w:t>")
            rel = str(d.relative_to(REPO_ROOT)) if str(d).startswith(str(REPO_ROOT)) else None
            # scan directly (path is outside the repo)
            with zipfile.ZipFile(d) as z:
                body = z.read("word/document.xml").decode("utf8", "ignore")
            flagged = "SYNTHETIC" not in body.upper()
            if flagged != expect_flag:
                raise AssertionError(
                    f"self-test failed: .docx marker detection wrong for {marker!r}"
                )

    print("Self-test OK: scanner catches Exos/EXOS/teamexos/exos-legal and unmarked .docx,")
    print("              and does not flag brand-free text.")


def main(argv: list[str]) -> int:
    strict_org = "--strict-org" in argv
    self_test()

    surface = public_surface()
    print(f"\nPublic surface: {len(surface)} tracked files (excl. public-cut-exclude.txt paths)")

    exos_hits: list[str] = []
    teamexos_hits: list[str] = []
    org_files: list[str] = []
    docx_hits: list[str] = []

    for rel in surface:
        if rel.endswith(".docx") and rel.startswith("tests/"):
            if docx_missing_synthetic_marker(rel):
                docx_hits.append(rel)
            continue
        text = _read(rel)
        if text is None:
            continue
        if rel not in GUARD_FILES:
            for lineno in scan(text, EXOS_RE):
                exos_hits.append(f"{rel}:{lineno}")
        if rel not in GUARD_FILES and rel not in TEAMEXOS_BEHAVIOR_FILES:
            for lineno in scan(text, TEAMEXOS_RE):
                teamexos_hits.append(f"{rel}:{lineno}")
        if rel not in GUARD_FILES and scan(text, ORG_RE):
            org_files.append(rel)

    failures = 0

    print("\nCheck 1: zero 'Exos'/'EXOS' on the public surface …")
    if exos_hits:
        failures += 1
        print(f"  FAIL — {len(exos_hits)} occurrence(s):")
        for h in exos_hits[:40]:
            print(f"    {h}")
    else:
        print("  OK — none.")

    print("\nCheck 2: zero 'teamexos' outside the reviewed allowlist …")
    if teamexos_hits:
        failures += 1
        print(f"  FAIL — {len(teamexos_hits)} occurrence(s) in non-allowlisted files:")
        for h in teamexos_hits[:40]:
            print(f"    {h}")
        print("  Use a neutral example domain (example.com), or add a justified allowlist entry.")
    else:
        print("  OK — only allowlisted guards/behavior tests carry it.")

    print("\nCheck 3: no unmarked .docx under tests/ …")
    if docx_hits:
        failures += 1
        print(f"  FAIL — {len(docx_hits)} .docx without a SYNTHETIC marker:")
        for h in docx_hits:
            print(f"    {h}")
    else:
        print("  OK — every tests/ .docx carries a SYNTHETIC marker.")

    # Issue #406 landed (the public repo is primary as of 2026-07-26), so this
    # tier is HARD by default now. `--strict-org` is retained as an accepted
    # no-op so any existing caller keeps working.
    print("\nCheck 4: zero 'exos-legal' repo-identity references …")
    if org_files:
        failures += 1
        print(f"  FAIL — {len(org_files)} file(s) still name the private org:")
        for f in org_files[:40]:
            print(f"    {f}")
        print("  Derive the owner from context (e.g. github.repository_owner, a CDK")
        print("  context value, or a host-supplied ${VAR}) instead of hard-coding it.")
    else:
        print("  OK — none.")

    if failures:
        print(f"\nBRAND-FREE LINT: FAIL ({failures} check(s) failed)")
        return 1
    print("\nBRAND-FREE LINT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
