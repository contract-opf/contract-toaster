#!/usr/bin/env python3
"""
CI gate for issue #424: DTS deploy target plumbs real VERSION/COMMIT_SHA.

The AWS target bakes build metadata into its image correctly
(backend/Dockerfile:1-15 declares ARG/ENV for VERSION, COMMIT_SHA and
IMAGE_DIGEST; .github/workflows/ci-pipeline.yml passes real --build-arg
values), and tests/test_infra_app_stack.py check A locks that in. The DTS
(Docker Compose) target had no equivalent plumbing at any link of the chain,
so `GET /version` always hit its os.environ defaults and the deployed footer
read the literal "Version dev (unknown)"
(docs/planning/frontend-release-audit-2026-07-27.md §A2).

This test is the parallel gate for the DTS path. It deliberately lives in its
own file rather than inside tests/test_infra_app_stack.py, because that file
synthesizes the CDK templates and is therefore skipped by the SKIP_INFRA fast
gate (scripts/collect_test_failures.sh matches any file naming that command) —
this check touches no infra and must run in BOTH gates. Keep this file free of
that phrase, or it silently stops running in the fast gate.

Checks (all must pass; exit 1 on any failure):

  1. deploy/dts/backend.Dockerfile declares ARG *and* ENV for all three of
     VERSION, COMMIT_SHA, IMAGE_DIGEST, with the same graceful-degrade
     defaults the AWS Dockerfile uses (dev / unknown / unknown).
  2. .github/workflows/dts-image-publish.yml passes real --build-arg VERSION
     and --build-arg COMMIT_SHA to the backend `docker build`, wired to the
     workflow's own computed step outputs (not hard-coded literals).
  3. Neither deploy/dts compose file CLOBBERS the baked-in image metadata.
     This is the subtle half of the bug: a compose `environment:` entry
     ALWAYS overrides the image's ENV, so `VERSION: ${VERSION:-dev}` would
     re-break the footer on every deploy where the host has no VERSION set.
     The pass-through form (`VERSION:` with no value) resolves from the host
     environment and, when unset, leaves the image's baked ENV intact.
  4. deploy/dts/docker-compose.yml (the build-from-source variant) forwards
     VERSION/COMMIT_SHA as build `args:`, so a locally built image can carry
     real values too.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DTS_DIR = REPO_ROOT / "deploy" / "dts"
DTS_DOCKERFILE = DTS_DIR / "backend.Dockerfile"
COMPOSE_LOCAL = DTS_DIR / "docker-compose.yml"
COMPOSE_COOLIFY = DTS_DIR / "docker-compose.coolify.yml"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dts-image-publish.yml"

METADATA_VARS = ("VERSION", "COMMIT_SHA", "IMAGE_DIGEST")
DEFAULTS = {"VERSION": "dev", "COMMIT_SHA": "unknown", "IMAGE_DIGEST": "unknown"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fail(msg: str) -> list[str]:
    print(f"  [FAIL] {msg}")
    return [msg]


def ok(msg: str) -> list[str]:
    print(f"  [PASS] {msg}")
    return []


def check(condition: bool, pass_msg: str, fail_msg: str) -> list[str]:
    return ok(pass_msg) if condition else fail(fail_msg)


# ---------------------------------------------------------------------------
# Check 1 — deploy/dts/backend.Dockerfile ARG/ENV contract
# ---------------------------------------------------------------------------

def check_1_dockerfile_arg_env() -> list[str]:
    print("\nCheck 1: deploy/dts/backend.Dockerfile declares ARG + ENV metadata …")
    failures: list[str] = []

    if not DTS_DOCKERFILE.is_file():
        return fail(f"{DTS_DOCKERFILE.relative_to(REPO_ROOT)} does not exist")

    text = read(DTS_DOCKERFILE)

    for var in METADATA_VARS:
        # ARG must be declared with the graceful-degrade default.
        arg_default = re.search(
            rf"^\s*ARG\s+{var}\s*=\s*(\S+)\s*$", text, re.MULTILINE
        )
        failures += check(
            arg_default is not None,
            f"1a[{var}]: ARG {var}=<default> declared",
            f"1a[{var}]: deploy/dts/backend.Dockerfile does not declare "
            f"`ARG {var}=<default>` — mirror backend/Dockerfile:1-15, or "
            f"GET /version silently reports its os.environ fallback (issue #424)",
        )
        if arg_default is not None:
            failures += check(
                arg_default.group(1) == DEFAULTS[var],
                f"1b[{var}]: ARG default is {DEFAULTS[var]!r} (matches the "
                f"backend/src/main.py os.environ.get fallback)",
                f"1b[{var}]: ARG default is {arg_default.group(1)!r}, expected "
                f"{DEFAULTS[var]!r} — the default must match the /version "
                f"fallback so an un-parameterised build degrades identically",
            )

        # ENV must forward the ARG (ENV VERSION=${VERSION}).
        env_line = re.search(
            rf"^\s*ENV\s+{var}\s*=\s*\$\{{{var}\}}\s*$", text, re.MULTILINE
        )
        failures += check(
            env_line is not None,
            f"1c[{var}]: ENV {var}=${{{var}}} forwards the build arg to runtime",
            f"1c[{var}]: deploy/dts/backend.Dockerfile has no "
            f"`ENV {var}=${{{var}}}` — an ARG alone is build-time only and "
            f"never reaches os.environ in the running container",
        )

    return failures


# ---------------------------------------------------------------------------
# Check 2 — the publish workflow passes real --build-arg values
# ---------------------------------------------------------------------------

def check_2_workflow_build_args() -> list[str]:
    print("\nCheck 2: dts-image-publish.yml passes --build-arg VERSION/COMMIT_SHA …")
    failures: list[str] = []

    if not PUBLISH_WORKFLOW.is_file():
        return fail(f"{PUBLISH_WORKFLOW.relative_to(REPO_ROOT)} does not exist")

    text = read(PUBLISH_WORKFLOW)

    for var in ("VERSION", "COMMIT_SHA"):
        m = re.search(rf"--build-arg\s+\"?{var}=([^\"\n\\]+)", text)
        failures += check(
            m is not None,
            f"2a[{var}]: docker build receives --build-arg {var}=…",
            f"2a[{var}]: the DTS publish workflow never passes "
            f"--build-arg {var} — the Dockerfile ARG then keeps its default "
            f"and every published image reports {DEFAULTS[var]!r} (issue #424)",
        )
        if m is None:
            continue

        value = m.group(1).strip()
        failures += check(
            "steps." in value and "outputs." in value,
            f"2b[{var}]: value is wired to a workflow step output ({value})",
            f"2b[{var}]: --build-arg {var} is set to {value!r}, which is not a "
            f"computed step output — it must derive from the real ref/SHA "
            f"(mirroring .github/workflows/ci-pipeline.yml:320-326)",
        )

    return failures


# ---------------------------------------------------------------------------
# Check 3 — compose must not clobber the baked-in image ENV
# ---------------------------------------------------------------------------

def _backend_environment_block(text: str) -> str | None:
    """Return the raw text of the `backend:` service's `environment:` block."""
    svc = re.search(r"^  backend:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    if svc is None:
        return None
    env = re.search(
        r"^    environment:\n(.*?)(?=^    \S|\Z)", svc.group(1), re.MULTILINE | re.DOTALL
    )
    return env.group(1) if env else None


def check_3_compose_does_not_clobber() -> list[str]:
    print("\nCheck 3: compose does not override the baked-in metadata …")
    failures: list[str] = []

    for path in (COMPOSE_LOCAL, COMPOSE_COOLIFY):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file():
            failures += fail(f"3[{rel}]: file does not exist")
            continue

        block = _backend_environment_block(read(path))
        if block is None:
            failures += fail(f"3[{rel}]: could not locate backend.environment block")
            continue

        for var in METADATA_VARS:
            entry = re.search(rf"^\s*{var}:(.*)$", block, re.MULTILINE)
            if entry is None:
                # Absent is safe: the image ENV simply survives untouched.
                failures += ok(
                    f"3[{rel}/{var}]: not set in compose (image ENV survives)"
                )
                continue
            value = entry.group(1).strip()
            failures += check(
                value == "",
                f"3[{rel}/{var}]: declared in the pass-through form "
                f"(`{var}:`), so an unset host value leaves the image ENV intact",
                f"3[{rel}/{var}]: sets `{var}: {value}` — a compose environment "
                f"entry ALWAYS overrides the image's ENV, so this replaces the "
                f"value baked in at build time and re-breaks the footer. Use "
                f"the pass-through form (`{var}:`, no value) instead.",
            )

    return failures


# ---------------------------------------------------------------------------
# Check 4 — the build-from-source compose forwards the values as build args
# ---------------------------------------------------------------------------

def check_4_local_compose_build_args() -> list[str]:
    print("\nCheck 4: docker-compose.yml forwards VERSION/COMMIT_SHA as build args …")
    failures: list[str] = []

    if not COMPOSE_LOCAL.is_file():
        return fail(f"{COMPOSE_LOCAL.relative_to(REPO_ROOT)} does not exist")

    text = read(COMPOSE_LOCAL)

    args_block = re.search(
        r"^(\s*)args:\n(.*?)(?=^\1\S|\Z)", text, re.MULTILINE | re.DOTALL
    )
    failures += check(
        args_block is not None,
        "4a: a build `args:` block is present",
        "4a: deploy/dts/docker-compose.yml declares no build `args:` — a "
        "locally built image can then never carry real version metadata",
    )
    if args_block is None:
        return failures

    body = args_block.group(2)
    for var in ("VERSION", "COMMIT_SHA"):
        failures += check(
            re.search(rf"^\s*{var}:\s*\$\{{{var}:-\S+\}}\s*$", body, re.MULTILINE)
            is not None,
            f"4b[{var}]: build arg {var} forwards ${{{var}:-{DEFAULTS[var]}}}",
            f"4b[{var}]: build arg {var} is missing or does not use the "
            f"${{{var}:-<default>}} host-override convention",
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("DTS version-metadata plumbing gate (issue #424)")
    print("=" * 70)

    all_failures: list[str] = []
    all_failures += check_1_dockerfile_arg_env()
    all_failures += check_2_workflow_build_args()
    all_failures += check_3_compose_does_not_clobber()
    all_failures += check_4_local_compose_build_args()

    print("\n" + "=" * 70)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all DTS version-metadata checks passed (issue #424).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
