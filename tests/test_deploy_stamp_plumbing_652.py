#!/usr/bin/env python3
"""
CI gate for issue #652: the FRONTEND image carries a build stamp too.

Issue #424 plumbed VERSION/COMMIT_SHA into the backend image, which is what
`GET /version` serves. That is only half of "what is actually deployed": issue
#613 root-caused, from the build log, a builder cache-key bug that leaves the
backend image's ARG-derived ENV layers pinned to an OLDER commit whenever a
change touches no backend files -- so `/version` reports a plausible, wrong
answer while the shipped bundle is genuinely new (observed twice 2026-08-23).

A screen cannot flag that condition unless the SPA can say which commit IT was
built from. Vite inlines `import.meta.env.VITE_*` at BUILD time, so the stamp
has to be in the frontend image build's environment before `npm run build`
runs; a runtime `environment:` entry in compose could never reach the bundle.
`frontend/src/deployIdentity.ts` reads the two values this file gates.

Lives in its own file rather than inside tests/test_dts_version_metadata_424.py
because that file gates a different contract (the backend's runtime ENV), and
because -- like it -- this check touches no infrastructure synthesis and must
run in BOTH the fast and full gates. Keep this file free of the phrase the
fast gate filters on (see scripts/collect_test_failures.sh) or it silently
stops running there.

Checks (all must pass; exit 1 on any failure):

  1. deploy/dts/frontend.Dockerfile declares ARG VERSION/COMMIT_SHA with the
     same graceful-degrade defaults the backend image uses, forwards each into
     a VITE_-prefixed ENV, and does so BEFORE `RUN npm run build` -- an ENV
     after the build is inlined into nothing.
  2. .github/workflows/dts-image-publish.yml passes --build-arg VERSION and
     COMMIT_SHA to EVERY matrix leg, not only the backend one. A leg-gated
     `if [ ... = "backend" ]` here is precisely the bug this check exists to
     prevent regressing to: the frontend would then always report the ARG
     default and the Settings tab would answer "cannot be checked" forever.
  3. deploy/dts/docker-compose.yml forwards VERSION/COMMIT_SHA to the frontend
     service as build `args:`, so a locally built image is stamped too.
  4. Neither compose file gives the frontend service a VERSION/COMMIT_SHA/
     VITE_* `environment:` entry. The stamp is compiled INTO the bundle; a
     runtime env var on an nginx container cannot change what was compiled,
     so such an entry would look like configuration while doing nothing --
     and it is the same shape as the #469/#613 landmine that check 3 of
     tests/test_dts_version_metadata_424.py guards on the backend side.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DTS_DIR = REPO_ROOT / "deploy" / "dts"
FRONTEND_DOCKERFILE = DTS_DIR / "frontend.Dockerfile"
COMPOSE_LOCAL = DTS_DIR / "docker-compose.yml"
COMPOSE_COOLIFY = DTS_DIR / "docker-compose.coolify.yml"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dts-image-publish.yml"
DEPLOY_IDENTITY = REPO_ROOT / "frontend" / "src" / "deployIdentity.ts"

# ARG name -> (default, the VITE_ ENV it must be forwarded into).
STAMP_VARS = {
    "VERSION": ("dev", "VITE_BUILD_VERSION"),
    "COMMIT_SHA": ("unknown", "VITE_COMMIT_SHA"),
}


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
# Check 1 — the frontend Dockerfile bakes the stamp into the bundle
# ---------------------------------------------------------------------------

def check_1_frontend_dockerfile() -> list[str]:
    print("\nCheck 1: deploy/dts/frontend.Dockerfile stamps the bundle …")
    failures: list[str] = []

    if not FRONTEND_DOCKERFILE.is_file():
        return fail(f"{FRONTEND_DOCKERFILE.relative_to(REPO_ROOT)} does not exist")

    text = read(FRONTEND_DOCKERFILE)

    build_match = re.search(r"^\s*RUN\s+npm\s+run\s+build\s*$", text, re.MULTILINE)
    failures += check(
        build_match is not None,
        "1a: the image still builds the SPA with `RUN npm run build`",
        "1a: deploy/dts/frontend.Dockerfile has no `RUN npm run build` — this "
        "gate can no longer tell whether the stamp is set before the build",
    )
    build_at = build_match.start() if build_match else len(text)

    for var, (default, vite_var) in STAMP_VARS.items():
        arg = re.search(rf"^\s*ARG\s+{var}\s*=\s*(\S+)\s*$", text, re.MULTILINE)
        failures += check(
            arg is not None,
            f"1b[{var}]: ARG {var}=<default> declared",
            f"1b[{var}]: deploy/dts/frontend.Dockerfile does not declare "
            f"`ARG {var}=<default>` — the bundle then carries no build stamp "
            f"and the Settings tab cannot tell agreement from #613's stale "
            f"backend stamp",
        )
        if arg is not None:
            failures += check(
                arg.group(1) == default,
                f"1c[{var}]: ARG default is {default!r} (matches the backend "
                f"image, so one pair of --build-arg values stamps both)",
                f"1c[{var}]: ARG default is {arg.group(1)!r}, expected "
                f"{default!r} — the defaults must match deploy/dts/"
                f"backend.Dockerfile or an un-parameterised build reports two "
                f"different kinds of 'no stamp'",
            )

        env = re.search(
            rf"^\s*ENV\s+{vite_var}\s*=\s*\$\{{{var}\}}\s*$", text, re.MULTILINE
        )
        failures += check(
            env is not None,
            f"1d[{var}]: ENV {vite_var}=${{{var}}} forwards the build arg to Vite",
            f"1d[{var}]: no `ENV {vite_var}=${{{var}}}` — Vite only inlines "
            f"variables named VITE_*, so an ARG alone never reaches the "
            f"bundle (read by frontend/src/deployIdentity.ts)",
        )
        if env is not None:
            failures += check(
                env.start() < build_at,
                f"1e[{var}]: ENV {vite_var} is set BEFORE `RUN npm run build`",
                f"1e[{var}]: ENV {vite_var} is set AFTER `RUN npm run build` — "
                f"Vite inlines import.meta.env at build time, so a stamp set "
                f"afterwards is compiled into nothing and the bundle ships "
                f"unstamped while the Dockerfile looks correct",
            )

    return failures


# ---------------------------------------------------------------------------
# Check 2 — the publish workflow stamps BOTH images
# ---------------------------------------------------------------------------

def check_2_workflow_stamps_both_legs() -> list[str]:
    print("\nCheck 2: dts-image-publish.yml stamps every matrix leg …")
    failures: list[str] = []

    if not PUBLISH_WORKFLOW.is_file():
        return fail(f"{PUBLISH_WORKFLOW.relative_to(REPO_ROOT)} does not exist")

    text = read(PUBLISH_WORKFLOW)

    build_step = re.search(
        r"^      - name: Build image\n(.*?)(?=^      - name: |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    failures += check(
        build_step is not None,
        "2a: the `Build image` step is present",
        "2a: dts-image-publish.yml has no `Build image` step — this gate "
        "cannot tell which legs receive the stamp",
    )
    if build_step is None:
        return failures

    body = build_step.group(1)

    # A component conditional inside the build step is the exact regression:
    # it is how the frontend leg went unstamped before #652.
    component_gate = re.search(r"^\s*if\s+\[.*matrix\.component.*\]", body, re.MULTILINE)
    failures += check(
        component_gate is None,
        "2b: no per-component gate around the build flags — both legs are stamped",
        "2b: the Build image step gates its flags on matrix.component. Both "
        "the backend and the frontend Dockerfile now consume "
        "VERSION/COMMIT_SHA, so a gated leg ships an unstamped image and the "
        "Settings tab reports 'cannot be checked on this build' forever "
        "(issue #652)",
    )

    for var in STAMP_VARS:
        m = re.search(rf"--build-arg\s+\"?{var}=([^\"\n\\]+)", body)
        failures += check(
            m is not None,
            f"2c[{var}]: docker build receives --build-arg {var}=…",
            f"2c[{var}]: the Build image step never passes --build-arg {var}",
        )
        if m is None:
            continue
        value = m.group(1).strip()
        failures += check(
            "steps." in value and "outputs." in value,
            f"2d[{var}]: value is wired to a workflow step output ({value})",
            f"2d[{var}]: --build-arg {var} is set to {value!r}, which is not a "
            f"computed step output — it must derive from the real ref/SHA",
        )

    return failures


# ---------------------------------------------------------------------------
# Check 3 — the build-from-source compose stamps the frontend too
# ---------------------------------------------------------------------------

def _service_block(text: str, service: str) -> str | None:
    """The raw text of one top-level service's body."""
    m = re.search(
        rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL
    )
    return m.group(1) if m else None


def check_3_local_compose_frontend_build_args() -> list[str]:
    print("\nCheck 3: docker-compose.yml stamps the frontend build …")
    failures: list[str] = []

    if not COMPOSE_LOCAL.is_file():
        return fail(f"{COMPOSE_LOCAL.relative_to(REPO_ROOT)} does not exist")

    block = _service_block(read(COMPOSE_LOCAL), "frontend")
    if block is None:
        return fail("3: could not locate the `frontend:` service in docker-compose.yml")

    args = re.search(r"^      args:\n(.*?)(?=^      \S|^    \S|\Z)", block, re.MULTILINE | re.DOTALL)
    failures += check(
        args is not None,
        "3a: the frontend build declares an `args:` block",
        "3a: deploy/dts/docker-compose.yml's frontend service declares no "
        "build `args:` — a locally built image can then never carry a stamp, "
        "so the Settings tab is blind on every non-published deployment",
    )
    if args is None:
        return failures

    body = args.group(1)
    for var, (default, _vite) in STAMP_VARS.items():
        failures += check(
            re.search(rf"^\s*{var}:\s*\$\{{{var}:-{default}\}}\s*$", body, re.MULTILINE)
            is not None,
            f"3b[{var}]: build arg {var} forwards ${{{var}:-{default}}}",
            f"3b[{var}]: the frontend build arg {var} is missing or does not "
            f"use the ${{{var}:-{default}}} host-override convention that the "
            f"backend anchor uses",
        )

    return failures


# ---------------------------------------------------------------------------
# Check 4 — the stamp is never faked at runtime
# ---------------------------------------------------------------------------

RUNTIME_FAKE = re.compile(
    r"^\s*(VERSION|COMMIT_SHA|IMAGE_DIGEST|VITE_[A-Z0-9_]+):", re.MULTILINE
)


def check_4_no_runtime_frontend_stamp() -> list[str]:
    print("\nCheck 4: no compose file fakes the bundle's stamp at runtime …")
    failures: list[str] = []

    for path in (COMPOSE_LOCAL, COMPOSE_COOLIFY):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file():
            failures += fail(f"4[{rel}]: file does not exist")
            continue

        block = _service_block(read(path), "frontend")
        if block is None:
            failures += fail(f"4[{rel}]: could not locate the `frontend:` service")
            continue

        env = re.search(
            r"^    environment:\n(.*?)(?=^    \S|\Z)", block, re.MULTILINE | re.DOTALL
        )
        if env is None:
            failures += ok(
                f"4[{rel}]: the frontend service declares no `environment:` block"
            )
            continue

        offenders = sorted({m.group(1) for m in RUNTIME_FAKE.finditer(env.group(1))})
        failures += check(
            not offenders,
            f"4[{rel}]: the frontend `environment:` block sets no build-stamp keys",
            f"4[{rel}]: the frontend service sets {', '.join(offenders)} in its "
            f"`environment:` block. The frontend image is nginx serving a "
            f"bundle that was compiled at BUILD time — a runtime variable "
            f"cannot change what Vite already inlined, so this looks like "
            f"configuration while doing nothing. Pass it as a build `arg:` "
            f"instead (issues #652, #613/#469).",
        )

    return failures


# ---------------------------------------------------------------------------
# Check 5 — the reader the whole chain exists to feed
# ---------------------------------------------------------------------------

def check_5_reader_consumes_the_stamp() -> list[str]:
    print("\nCheck 5: the SPA actually reads the baked stamp …")
    failures: list[str] = []

    if not DEPLOY_IDENTITY.is_file():
        return fail(
            f"{DEPLOY_IDENTITY.relative_to(REPO_ROOT)} does not exist — the "
            f"Dockerfile would then be baking values nothing reads"
        )

    text = read(DEPLOY_IDENTITY)
    for _var, (_default, vite_var) in STAMP_VARS.items():
        failures += check(
            vite_var in text,
            f"5[{vite_var}]: frontend/src/deployIdentity.ts reads {vite_var}",
            f"5[{vite_var}]: nothing in frontend/src/deployIdentity.ts reads "
            f"{vite_var} — the build plumbing above would be inert",
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Frontend build-stamp plumbing gate (issue #652)")
    print("=" * 70)

    all_failures: list[str] = []
    all_failures += check_1_frontend_dockerfile()
    all_failures += check_2_workflow_stamps_both_legs()
    all_failures += check_3_local_compose_frontend_build_args()
    all_failures += check_4_no_runtime_frontend_stamp()
    all_failures += check_5_reader_consumes_the_stamp()

    print("\n" + "=" * 70)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all frontend build-stamp checks passed (issue #652).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
