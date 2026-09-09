#!/usr/bin/env python3
"""CI gate (issue #404, rescoped by #591): the public surface must carry no
FUNCTIONAL reference to the private org, and no corporate-domain leak.

## What this guards

The toaster is a brand-free empty shell that is being developed in public
(epic #408). Owner ruling, 2026-08-21: "exos or exos legal is always ok to
publish" -- the tenant brand string is NOT a leak and this gate no longer
polices it (issue #591 supersedes the brand-string half of #405). What DID
survive is narrower and functional: anything published publicly must point
at `contract-opf`, not `exos-legal`, in any position that is actually
ACTED ON -- an image path a deploy pulls, a CODEOWNERS team that routes
review, a workflow org ref, a repo URL a clone/checkout would follow. A prose
mention of the org's name (a changelog note, a historical issue link quoted
in a test) is not that, and no longer fails the gate.

## The public surface

Scanned files = `git ls-files` MINUS every path in `public-cut-exclude.txt`.
That manifest is the authoritative list of what never ships publicly (the real
contract corpus under `docs/planning/`, the internal AWS access request), so
scanning exactly the complement means this gate covers precisely what a public
cut would publish -- no more, no less.

## Tiers

1. HARD, allowlisted: `teamexos` (the corporate domain). Every remaining
   occurrence is either a de-brand scanner that must name the token as its
   search pattern, or an auth-domain behavior test that uses it as its sample
   allowed-domain. Each allowlist entry carries a justification below.
2. HARD: `exos-legal` in a FUNCTIONAL position -- a GHCR/container image
   path, a `github.com`/SSH repo URL, a CODEOWNERS-style `@exos-legal/team`
   reference, or a workflow `uses:`/`repository:` org ref. A file that merely
   NAMES the org in prose (a historical issue link quoted by a test, a
   comment explaining repo history) does not trip this tier.
3. HARD: any `.docx` under `tests/` lacking a SYNTHETIC content marker --
   promoting the public-cut SOFT scan into a blocking check.

`--strict-org` is accepted as a no-op for backward compatibility with any
existing caller; the org tier has been HARD-by-default (not opt-in) since
issue #406 landed and rescoping it in #591 did not change that.

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

TEAMEXOS_RE = re.compile(r"teamexos", re.IGNORECASE)

# `exos-legal` in a FUNCTIONAL position only -- the shapes issue #591 names:
# an image path, a repo URL (https or SSH), a CODEOWNERS/scoped-team
# reference, or a workflow org ref. A bare prose mention of "exos-legal" that
# matches none of these shapes (e.g. "the repo used to live under exos-legal")
# is not functional and does not trip this gate.
ORG_FUNCTIONAL_RE = re.compile(
    r"ghcr\.io/exos-legal\b"           # GHCR/container image path
    r"|github\.com[:/]exos-legal/"     # repo URL, https:// or git@ SSH form
    r"|@exos-legal/[\w.-]+"            # CODEOWNERS team / scoped package ref
    r"|\bexos-legal/[\w.-]+@"          # workflow `uses: org/repo@ref`
    r"|\brepository:\s*['\"]?exos-legal/"  # workflow `repository:` field
    r"|\bowner:\s*['\"]?exos-legal\b",     # CDK/workflow `owner:` context field
    re.IGNORECASE,
)

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
    "tests/lint-acceptable-variations.py": "references the org in its provenance docstring",
    "tests/test_no_hardcoded_tenant_literals_274.py": "asserts tenant literals are ABSENT",
    "tests/test_policy_document.py": "asserts a policy carries no tenant literal",
    "tests/test_repo_bootstrap.py": "asserts bootstrap docs/config identity",
    "tests/test_codeowners_coverage.py": "asserts CODEOWNERS team ownership",
    "tests/test_phase0_ac_coverage.py": "quotes historical phase-0 acceptance criteria",
    "tests/test_schema_hardening.py": "quotes historical schema URLs",
    "scripts/docs-lint.py": "docs linter; the stale address is its search pattern",
    "public-cut-exclude.txt": "the exclusion manifest; naming what it excludes is its job",
    # (Removed 2026-09-09: entries for scripts/public-cut.sh and
    # tests/lint-public-cut-exclude.py. Both belonged to the retired cut
    # workflow and neither file exists in this repository, so the entries
    # were inert config describing a tool nobody can run here.)
    "tests/test_infra_appname_prefix_233.py": "asserts CodeBuild no longer hard-codes the org",
}
# NOTE (issue #591): eleven entries were removed from this dict here -- tests
# that quoted/asserted the tenant brand string alone (redline/tracked-changes,
# bundle-rollback, playbook-registry/version-audit/version-notes, form-match
# router, me-capability, opf-prompt, review-api, shipped-playbook-seed,
# retention-window-config) and had no teamexos/org content of their own. Now
# that tier 1 (Exos/EXOS) no longer exists, they needed no guard to begin
# with; leaving them allowlisted would have been dead, misleading
# documentation for a check that no longer runs.

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
        dirty.write_text(
            "Contact a@teamexos.com\nimage: ghcr.io/exos-legal/toaster:latest\n"
            "owner: @exos-legal/gc\n"
        )
        text = dirty.read_text()
        if not scan(text, TEAMEXOS_RE):
            raise AssertionError("self-test failed: did not flag 'teamexos'")
        if not scan(text, ORG_FUNCTIONAL_RE):
            raise AssertionError("self-test failed: did not flag a functional 'exos-legal' ref")

        # Issue #591: a plain brand string, and a bare PROSE mention of the
        # private org's name (no image path / URL / team-scope / workflow
        # ref shape), are both publishable now -- neither may flag.
        clean = tmp / "clean.md"
        clean.write_text(
            "The Exos standard form is a trademark of Athletes' Performance, Inc.\n"
            "EXOS OWNS THIS.\n"
            "This project used to live under exos-legal before the public cut.\n"
        )
        ct = clean.read_text()
        if scan(ct, TEAMEXOS_RE) or scan(ct, ORG_FUNCTIONAL_RE):
            raise AssertionError("self-test failed: flagged brand-string / prose-org text")

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

    print("Self-test OK: scanner catches teamexos + functional exos-legal refs + unmarked .docx,")
    print("              and does not flag the brand string or a bare prose org mention.")


def main(argv: list[str]) -> int:
    # Retained as an accepted no-op: the org tier has been HARD-by-default
    # (not opt-in) since issue #406, and rescoping it in #591 to functional-
    # position-only did not reintroduce an opt-in mode.
    _ = "--strict-org" in argv
    self_test()

    surface = public_surface()
    print(f"\nPublic surface: {len(surface)} tracked files (excl. public-cut-exclude.txt paths)")

    teamexos_hits: list[str] = []
    org_functional_hits: list[str] = []
    docx_hits: list[str] = []

    for rel in surface:
        if rel.endswith(".docx") and rel.startswith("tests/"):
            if docx_missing_synthetic_marker(rel):
                docx_hits.append(rel)
            continue
        text = _read(rel)
        if text is None:
            continue
        if rel not in GUARD_FILES and rel not in TEAMEXOS_BEHAVIOR_FILES:
            for lineno in scan(text, TEAMEXOS_RE):
                teamexos_hits.append(f"{rel}:{lineno}")
        if rel not in GUARD_FILES:
            for lineno in scan(text, ORG_FUNCTIONAL_RE):
                org_functional_hits.append(f"{rel}:{lineno}")

    failures = 0

    print("\nCheck 1: zero 'teamexos' outside the reviewed allowlist …")
    if teamexos_hits:
        failures += 1
        print(f"  FAIL — {len(teamexos_hits)} occurrence(s) in non-allowlisted files:")
        for h in teamexos_hits[:40]:
            print(f"    {h}")
        print("  Use a neutral example domain (example.com), or add a justified allowlist entry.")
    else:
        print("  OK — only allowlisted guards/behavior tests carry it.")

    print("\nCheck 2: zero 'exos-legal' in a FUNCTIONAL position (image path / repo URL /")
    print("         CODEOWNERS-style team / workflow org ref) …")
    if org_functional_hits:
        failures += 1
        print(f"  FAIL — {len(org_functional_hits)} occurrence(s):")
        for h in org_functional_hits[:40]:
            print(f"    {h}")
        print("  Point at contract-opf instead (e.g. github.repository_owner, a CDK context")
        print("  value, or a host-supplied ${VAR}) -- this is a routing/pull-path leak, not")
        print("  a brand mention. Naming the private org in PROSE is fine and not flagged.")
    else:
        print("  OK — none.")

    print("\nCheck 3: no unmarked .docx under tests/ …")
    if docx_hits:
        failures += 1
        print(f"  FAIL — {len(docx_hits)} .docx without a SYNTHETIC marker:")
        for h in docx_hits:
            print(f"    {h}")
    else:
        print("  OK — every tests/ .docx carries a SYNTHETIC marker.")

    if failures:
        print(f"\nBRAND-FREE LINT: FAIL ({failures} check(s) failed)")
        return 1
    print("\nBRAND-FREE LINT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
