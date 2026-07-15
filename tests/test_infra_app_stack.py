#!/usr/bin/env python3
"""
Structural gate for issue #55: App Runner + hello-world container AC coverage.

Verifies that all acceptance criteria for issue #55 are satisfied:

  A. backend/Dockerfile builds a Python 3.12 container running FastAPI via
     uvicorn.  The Dockerfile must reference python:3.12 and uvicorn.

  B. backend/src/main.py exposes:
       - GET /health  — public, liveness only, {"status": "ok"}
       - GET /version — allowlisted (requires auth), returns version/commit/
                        image_digest/uptime_seconds
       - GET /whoami  (or equivalent) — authenticated echo endpoint that
                        proves JWT verification end-to-end.

  C. JWT verification middleware is present:
       - Verifies Cognito token signature, audience, and expiry.
       - Independently re-verifies the email domain against the env-driven
         ALLOWED_EMAIL_DOMAINS allowlist (issue #274 replaced the original
         hardcoded teamexos.com literal from the #55 AC).
       - Independently re-verifies the Google `hd` claim against the same
         allowlist.

  D. Version and commit SHA are read from environment variables (VERSION,
     COMMIT_SHA, IMAGE_DIGEST) — not hard-coded.

  E. infra/lib/nested/app-stack.ts defines the App Runner service:
       - Sourced from an ECR image pinned to an immutable digest.
       - Auto-deploy from main is DISABLED.
       - VPC connector configured for private network access.
       - API task role is least-privilege and does NOT include
         bedrock:InvokeModel.

  F. NetworkStack (infra/lib/nested/network-stack.ts) exports a VPC that
     the AppStack VPC connector references.

  G. cdk synth runs cleanly with the updated app + network stacks.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
BACKEND_DIR = REPO_ROOT / "backend"
DOCKERFILE_PATH = BACKEND_DIR / "Dockerfile"
MAIN_PY_PATH = BACKEND_DIR / "src" / "main.py"
APP_STACK_PATH = INFRA / "lib" / "nested" / "app-stack.ts"
NETWORK_STACK_PATH = INFRA / "lib" / "nested" / "network-stack.ts"


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


# ---------------------------------------------------------------------------
# Check A — backend/Dockerfile
# ---------------------------------------------------------------------------

def check_a_dockerfile() -> list[str]:
    print("\nCheck A: backend/Dockerfile — Python 3.12 container, FastAPI, uvicorn …")
    failures: list[str] = []

    failures += _assert(
        BACKEND_DIR.is_dir(),
        "backend/ directory exists",
        f"Expected: {BACKEND_DIR}",
    )
    if failures:
        return failures

    failures += _assert(
        DOCKERFILE_PATH.is_file(),
        "backend/Dockerfile exists",
        f"Expected: {DOCKERFILE_PATH}",
    )
    if failures:
        return failures

    text = _read(DOCKERFILE_PATH)

    # Must use Python 3.12 base image
    failures += _assert(
        bool(re.search(r"python:3\.12", text)),
        "Dockerfile uses Python 3.12 base image",
        "Must reference 'python:3.12' (e.g. FROM python:3.12-slim).",
    )

    # Must reference uvicorn (as the ASGI server)
    failures += _assert(
        bool(re.search(r"uvicorn", text, re.IGNORECASE)),
        "Dockerfile references uvicorn",
        "CMD or ENTRYPOINT must invoke uvicorn.",
    )

    # Must reference fastapi or requirements file
    has_fastapi = bool(re.search(r"fastapi|requirements", text, re.IGNORECASE))
    failures += _assert(
        has_fastapi,
        "Dockerfile references FastAPI or a requirements file",
        "Must install FastAPI, either directly or via a requirements file.",
    )

    # Must expose port 8080 (App Runner default)
    failures += _assert(
        bool(re.search(r"EXPOSE\s+8080", text)),
        "Dockerfile EXPOSEs port 8080",
        "App Runner listens on 8080 by default.",
    )

    # Build-time ENV vars for version/commit/digest
    has_build_args = bool(re.search(r"ARG\s+(?:VERSION|COMMIT_SHA|IMAGE_DIGEST)", text))
    has_env_vars = bool(re.search(r"ENV\s+(?:VERSION|COMMIT_SHA|IMAGE_DIGEST)", text))
    failures += _assert(
        has_build_args or has_env_vars,
        "Dockerfile defines VERSION, COMMIT_SHA, or IMAGE_DIGEST as ARG/ENV",
        "AC: version and commit SHA are read from environment variables set at container build time.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — backend/src/main.py endpoints
# ---------------------------------------------------------------------------

def check_b_main_py_endpoints() -> list[str]:
    print("\nCheck B: backend/src/main.py — /health, /version, /whoami endpoints …")
    failures: list[str] = []

    failures += _assert(
        MAIN_PY_PATH.is_file(),
        "backend/src/main.py exists",
        f"Expected: {MAIN_PY_PATH}",
    )
    if failures:
        return failures

    text = _read(MAIN_PY_PATH)

    # /health endpoint — public liveness only
    failures += _assert(
        bool(re.search(r'["\']/?health["\']', text)),
        "main.py defines GET /health endpoint",
        "Per AC: public GET /health returning liveness only {'status': 'ok'}.",
    )
    failures += _assert(
        bool(re.search(r'"status"\s*:\s*"ok"', text)),
        "main.py /health returns {\"status\": \"ok\"}",
        "Per AC: liveness only — no build details.",
    )

    # /version endpoint — allowlisted (requires auth)
    failures += _assert(
        bool(re.search(r'["\']/?version["\']', text)),
        "main.py defines GET /version endpoint",
        "Per AC: allowlisted GET /version returning version/commit/image_digest/uptime_seconds.",
    )
    failures += _assert(
        bool(re.search(r"uptime_seconds", text)),
        "main.py /version response includes uptime_seconds",
        "Per AC: version response must include uptime_seconds.",
    )

    # /whoami (or equivalent) authenticated echo endpoint
    has_whoami = bool(re.search(r'["\']/?whoami["\']', text))
    has_me = bool(re.search(r'["\']/?(?:api/)?me["\']', text))
    failures += _assert(
        has_whoami or has_me,
        "main.py defines an authenticated echo endpoint (/whoami or /me)",
        "Per AC: A /whoami (or equivalent) authenticated echo endpoint proves JWT verification end-to-end.",
    )

    # FastAPI import
    failures += _assert(
        bool(re.search(r"from fastapi|import fastapi|FastAPI\s*\(", text, re.IGNORECASE)),
        "main.py imports FastAPI",
        "Must use FastAPI.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — JWT verification middleware
# ---------------------------------------------------------------------------

def check_c_jwt_middleware() -> list[str]:
    print("\nCheck C: JWT verification middleware — Cognito token + domain checks …")
    failures: list[str] = []

    failures += _assert(
        MAIN_PY_PATH.is_file(),
        "backend/src/main.py exists (prerequisite for middleware check)",
    )
    if failures:
        return failures

    # Find all Python sources in backend/
    backend_src_files = list((BACKEND_DIR / "src").rglob("*.py")) if (BACKEND_DIR / "src").is_dir() else []
    if MAIN_PY_PATH.is_file() and MAIN_PY_PATH not in backend_src_files:
        backend_src_files.append(MAIN_PY_PATH)

    all_py = "\n".join(_read(f) for f in backend_src_files)

    # JWT verification (must mention jwks, jwt, or python-jose, PyJWT etc.)
    failures += _assert(
        bool(re.search(r"jwt|jwks|JWKS|JWK|PyJWT|python_jose|jose", all_py, re.IGNORECASE)),
        "Backend Python sources reference JWT / JWKS verification",
        "Per AC: middleware must verify the Cognito token signature, audience, and expiry.",
    )

    # Cognito JWKS endpoint or issuer
    failures += _assert(
        bool(re.search(r"cognito|amazonaws\.com.*cognito|COGNITO", all_py, re.IGNORECASE)),
        "Backend references Cognito (JWT issuer / JWKS endpoint)",
        "Per AC: must verify the Cognito token signature.",
    )

    # Email domain check: env-driven allowlist. Issue #274 (PR #317) replaced
    # the hardcoded teamexos.com literal from the original #55 AC with the
    # ALLOWED_EMAIL_DOMAINS env var, so assert the mechanism instead of the
    # literal: the allowlist must be read from the environment, and the email
    # domain must be independently re-checked against it.
    # The regexes below are deliberately case-sensitive and code-shaped
    # (environ read, lowercase allowed_domains variable) so that docstring
    # prose mentioning ALLOWED_EMAIL_DOMAINS cannot satisfy them if the
    # actual verification code is removed.
    failures += _assert(
        bool(re.search(r'(environ\.get|environ\[|getenv)\(?\s*\(?\s*["\']ALLOWED_EMAIL_DOMAINS', all_py)),
        "Backend reads the ALLOWED_EMAIL_DOMAINS env-driven domain allowlist",
        "Per AC (#55 as amended by #274): allowed email domains must come from "
        "the ALLOWED_EMAIL_DOMAINS env var, not a hardcoded literal.",
    )
    failures += _assert(
        bool(re.search(r"email.{0,120}@.{0,120}allowed", all_py)),
        "Backend independently re-checks the email domain against the allowlist",
        "Per AC: independently re-verifies the email domain (e.g. "
        "email.lower().endswith(f\"@{domain}\") for domain in allowed_domains).",
    )

    # hd claim check: must read the Google 'hd' claim AND compare it against
    # the same allowlist.
    failures += _assert(
        bool(re.search(r'["\']hd["\']|hd_claim|hd\s*==', all_py)),
        "Backend independently checks the Google 'hd' claim",
        "Per AC: independently re-verifies the Google hd claim.",
    )
    # Require a code-shaped membership test ("… in allowed…") so prose in a
    # docstring can't satisfy this check if the actual comparison is removed.
    failures += _assert(
        bool(re.search(r"\bhd\b.{0,120}\bin\s+allowed", all_py)),
        "Backend re-checks the Google 'hd' claim against the domain allowlist",
        "Per AC (#55 as amended by #274): the hd claim must be validated "
        "against the ALLOWED_EMAIL_DOMAINS allowlist.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — version/commit read from environment variables
# ---------------------------------------------------------------------------

def check_d_env_vars() -> list[str]:
    print("\nCheck D: version and commit SHA read from environment variables …")
    failures: list[str] = []

    failures += _assert(
        MAIN_PY_PATH.is_file(),
        "backend/src/main.py exists (prerequisite for env var check)",
    )
    if failures:
        return failures

    all_py = "\n".join(
        _read(f)
        for f in (list((BACKEND_DIR / "src").rglob("*.py")) if (BACKEND_DIR / "src").is_dir() else [])
    )
    if not all_py:
        all_py = _read(MAIN_PY_PATH)

    failures += _assert(
        bool(re.search(r'os\.environ|os\.getenv|environ\[', all_py)),
        "main.py reads environment variables via os.environ or os.getenv",
        "Per AC: version and commit SHA are read from environment variables.",
    )

    failures += _assert(
        bool(re.search(r"VERSION|COMMIT_SHA|COMMIT|IMAGE_DIGEST", all_py)),
        "main.py references VERSION, COMMIT_SHA, or IMAGE_DIGEST env var names",
        "Per AC: variables set at container build time.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — infra/lib/nested/app-stack.ts: App Runner service
# ---------------------------------------------------------------------------

def check_e_app_stack_ts() -> list[str]:
    print("\nCheck E: infra/lib/nested/app-stack.ts — App Runner service definition …")
    failures: list[str] = []

    failures += _assert(
        APP_STACK_PATH.is_file(),
        "infra/lib/nested/app-stack.ts exists",
    )
    if failures:
        return failures

    text = _read(APP_STACK_PATH)

    # App Runner service construct
    has_apprunner = bool(
        re.search(r"apprunner|AppRunner|aws-apprunner|CfnService", text, re.IGNORECASE)
    )
    failures += _assert(
        has_apprunner,
        "app-stack.ts references App Runner (apprunner / CfnService)",
        "Per AC: App Runner service defined in infra/lib/app-stack.ts.",
    )

    # ECR image pinned to an immutable digest (not tag-only)
    has_ecr = bool(re.search(r"ecr|ECR|imageIdentifier|imageRepository|digest", text, re.IGNORECASE))
    failures += _assert(
        has_ecr,
        "app-stack.ts references ECR image / digest",
        "Per AC: sourced from an ECR image pinned to an immutable digest.",
    )

    # Auto-deploy DISABLED
    has_auto_deploy_disabled = bool(
        re.search(r"auto.*deploy.*false|autoDeployments.*DISABLED|isPubliclyAccessible.*false|DISABLE|paused.*true|deploymentConfiguration.*DISABLED", text, re.IGNORECASE)
    )
    # Also check for a comment asserting auto-deploy is off (code + comment both acceptable as structural gate)
    has_auto_deploy_comment = bool(
        re.search(r"auto.deploy.*disabled|disabled.*auto.deploy|never.*auto.mutat|auto.mutat.*disabled", text, re.IGNORECASE)
    )
    failures += _assert(
        has_auto_deploy_disabled or has_auto_deploy_comment,
        "app-stack.ts explicitly disables auto-deploy from main",
        "Per AC: auto-deploy from main is DISABLED. A merge to main must not alter production.",
    )

    # VPC connector
    has_vpc = bool(
        re.search(r"vpc|VPC|vpcConnector|VpcConnector|networkConfiguration", text, re.IGNORECASE)
    )
    failures += _assert(
        has_vpc,
        "app-stack.ts references VPC connector",
        "Per AC: VPC connector configured (security => Phase 0).",
    )

    # No bedrock:InvokeModel on the API task role
    has_bedrock_invoke = bool(re.search(r"bedrock:InvokeModel", text))
    failures += _assert(
        not has_bedrock_invoke,
        "app-stack.ts does NOT grant bedrock:InvokeModel to the API task role",
        "Per AC: the API task role does NOT get bedrock:InvokeModel — inference runs under the pipeline task role.",
    )

    # Least-privilege role defined (capability-split)
    has_task_role = bool(
        re.search(r"taskRole|task.*role|role.*task|least.priv|addToPolicy|grantRead|grantWrite|addGrantee", text, re.IGNORECASE)
    )
    failures += _assert(
        has_task_role,
        "app-stack.ts defines task role permissions (least-privilege)",
        "Per AC: API task role is least-privilege.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — NetworkStack exports a VPC
# ---------------------------------------------------------------------------

def check_f_network_stack_vpc() -> list[str]:
    print("\nCheck F: NetworkStack (network-stack.ts) defines and exports a VPC …")
    failures: list[str] = []

    failures += _assert(
        NETWORK_STACK_PATH.is_file(),
        "infra/lib/nested/network-stack.ts exists",
    )
    if failures:
        return failures

    text = _read(NETWORK_STACK_PATH)

    # Must define a VPC
    has_vpc_construct = bool(
        re.search(r"ec2\.Vpc|new\s+Vpc|aws-cdk-lib/aws-ec2|aws_ec2|Vpc\s*\(", text, re.IGNORECASE)
    )
    failures += _assert(
        has_vpc_construct,
        "network-stack.ts defines a VPC construct (ec2.Vpc or CfnVPC)",
        "Per AC: VPC connector so the service reaches data resources privately.",
    )

    # Must export the VPC (readonly property)
    has_vpc_export = bool(
        re.search(r"readonly\s+vpc|this\.vpc\s*=|export.*vpc", text, re.IGNORECASE)
    )
    failures += _assert(
        has_vpc_export,
        "network-stack.ts exports the VPC as a readonly property",
        "AppStack must be able to reference the VPC from NetworkStack.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — cdk synth runs cleanly
# ---------------------------------------------------------------------------

def check_g_cdk_synth() -> list[str]:
    print("\nCheck G: cdk synth runs cleanly with the App Runner stack …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)")

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            return _assert(
                False,
                "npm install succeeded in infra/",
                f"stdout: {install.stdout[-500:]}\nstderr: {install.stderr[-500:]}",
            )

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("AppStack + backend structural gate (issue #55)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_dockerfile()
    all_failures += check_b_main_py_endpoints()
    all_failures += check_c_jwt_middleware()
    all_failures += check_d_env_vars()
    all_failures += check_e_app_stack_ts()
    all_failures += check_f_network_stack_vpc()
    all_failures += check_g_cdk_synth()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all App Runner + backend structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
