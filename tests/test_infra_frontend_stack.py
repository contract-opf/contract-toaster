#!/usr/bin/env python3
"""
Structural gate for issue #54: Amplify Hosting + empty React app AC coverage.

Verifies that all acceptance criteria for issue #54 are satisfied:

  A. frontend/ directory is scaffolded with Vite + React + TypeScript.
     (package.json, vite.config, tsconfig present; dependencies include
      react, vite, typescript)

  B. AWS Amplify libraries integrated in frontend/:
     package.json lists aws-amplify and @aws-amplify/ui-react.

  C. Amplify Auth configured via aws-exports reference or Amplify.configure call.
     The app uses the Authenticator component from @aws-amplify/ui-react.

  D. Header shows signed-in user email.
     An App.tsx (or equivalent) references the user's email.

  E. Footer shows version from the authenticated /version endpoint.
     App.tsx (or equivalent) references /version and renders version info.

  F. infra/lib/nested/frontend-stack.ts defines an Amplify Hosting app.
     The FrontendStack is no longer a placeholder — it contains an Amplify app
     CDK construct or the equivalent L1/L2 resources.

  G. DEV auto-build/auto-publish is enabled; PROD requires deliberate promotion.
     The frontend-stack.ts source explicitly distinguishes dev vs. prod behavior:
     dev allows auto-build/auto-publish on push to main; prod does NOT auto-publish.

  H. CI build produces the frontend artifact.
     A CI workflow or build script exists that runs `vite build` (or npm run build).

  I. cdk synth runs cleanly with the updated frontend stack.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
FRONTEND_DIR = REPO_ROOT / "frontend"
FRONTEND_STACK_PATH = INFRA / "lib" / "nested" / "frontend-stack.ts"


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
# Check A — frontend/ scaffolded with Vite + React + TypeScript
# ---------------------------------------------------------------------------

def check_a_frontend_scaffold() -> list[str]:
    print("\nCheck A: frontend/ directory scaffolded with Vite + React + TypeScript …")
    failures: list[str] = []

    failures += _assert(
        FRONTEND_DIR.is_dir(),
        "frontend/ directory exists",
        "Per AC: 'frontend/ scaffolded with Vite + React + TypeScript.'",
    )
    if failures:
        return failures

    # package.json must exist
    pkg_json_path = FRONTEND_DIR / "package.json"
    failures += _assert(
        pkg_json_path.is_file(),
        "frontend/package.json exists",
    )

    if pkg_json_path.is_file():
        pkg = json.loads(_read(pkg_json_path))
        all_deps = {}
        all_deps.update(pkg.get("dependencies", {}))
        all_deps.update(pkg.get("devDependencies", {}))

        # React
        failures += _assert(
            "react" in all_deps,
            "frontend/package.json includes 'react' dependency",
            "Per AC: Vite + React + TypeScript scaffold.",
        )
        # TypeScript
        failures += _assert(
            "typescript" in all_deps,
            "frontend/package.json includes 'typescript' dependency",
            "Per AC: Vite + React + TypeScript scaffold.",
        )
        # Vite
        failures += _assert(
            "vite" in all_deps,
            "frontend/package.json includes 'vite' dependency",
            "Per AC: Vite + React + TypeScript scaffold.",
        )

    # tsconfig.json
    failures += _assert(
        (FRONTEND_DIR / "tsconfig.json").is_file(),
        "frontend/tsconfig.json exists",
        "Per AC: TypeScript configuration required for Vite + React + TypeScript.",
    )

    # vite.config (ts or js)
    has_vite_config = (
        (FRONTEND_DIR / "vite.config.ts").is_file()
        or (FRONTEND_DIR / "vite.config.js").is_file()
    )
    failures += _assert(
        has_vite_config,
        "frontend/vite.config.ts (or .js) exists",
        "Per AC: Vite configuration file required.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — AWS Amplify libraries in frontend/package.json
# ---------------------------------------------------------------------------

def check_b_amplify_libraries() -> list[str]:
    print("\nCheck B: AWS Amplify libraries integrated in frontend/ …")
    failures: list[str] = []

    pkg_json_path = FRONTEND_DIR / "package.json"
    if not pkg_json_path.is_file():
        return _assert(False, "frontend/package.json exists (prerequisite)")

    pkg = json.loads(_read(pkg_json_path))
    all_deps = {}
    all_deps.update(pkg.get("dependencies", {}))
    all_deps.update(pkg.get("devDependencies", {}))

    failures += _assert(
        "aws-amplify" in all_deps,
        "frontend/package.json includes 'aws-amplify' dependency",
        "Per AC: 'AWS Amplify libraries integrated (aws-amplify, @aws-amplify/ui-react)'.",
    )
    failures += _assert(
        "@aws-amplify/ui-react" in all_deps,
        "frontend/package.json includes '@aws-amplify/ui-react' dependency",
        "Per AC: 'AWS Amplify libraries integrated (aws-amplify, @aws-amplify/ui-react)'.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — Amplify Auth configured; Authenticator component used
# ---------------------------------------------------------------------------

def check_c_amplify_auth_config() -> list[str]:
    print("\nCheck C: Amplify Auth configured; Authenticator component used …")
    failures: list[str] = []

    if not FRONTEND_DIR.is_dir():
        return _assert(False, "frontend/ directory exists (prerequisite)")

    # Look in all .tsx / .ts files under frontend/src/
    src_dir = FRONTEND_DIR / "src"
    if not src_dir.is_dir():
        return _assert(False, "frontend/src/ directory exists (prerequisite)")

    all_src = ""
    for f in src_dir.rglob("*.tsx"):
        all_src += _read(f)
    for f in src_dir.rglob("*.ts"):
        all_src += _read(f)

    has_authenticator = bool(
        re.search(
            r"Authenticator|authenticator|from.*@aws-amplify/ui-react",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_authenticator,
        "Authenticator component from @aws-amplify/ui-react used in frontend/src/",
        "Per AC: 'Use @aws-amplify/ui-react Authenticator component for the sign-in flow.'",
    )

    # Amplify.configure or aws-exports reference
    has_amplify_configure = bool(
        re.search(
            r"Amplify\.configure|amplify\.configure|aws-exports|awsExports|amplifyconfig",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_amplify_configure,
        "Amplify.configure (or aws-exports reference) present in frontend/src/",
        "Per AC (Notes): 'Configure Amplify Auth via the aws-exports.js output from cdk deploy.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — Header shows signed-in user's email
# ---------------------------------------------------------------------------

def check_d_header_email() -> list[str]:
    print("\nCheck D: Header shows the signed-in user's email …")
    failures: list[str] = []

    src_dir = FRONTEND_DIR / "src"
    if not src_dir.is_dir():
        return _assert(False, "frontend/src/ directory exists (prerequisite)")

    all_src = ""
    for f in src_dir.rglob("*.tsx"):
        all_src += _read(f)

    # Look for email rendering in the App or a header component
    has_email_display = bool(
        re.search(
            r"email|Email|user.*email|email.*user|signedIn.*email|email.*signedIn",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_email_display,
        "frontend/src/ references the signed-in user's email in the header",
        "Per AC: 'Header shows the signed-in user's email.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — Footer shows version from authenticated /version endpoint
# ---------------------------------------------------------------------------

def check_e_footer_version() -> list[str]:
    print("\nCheck E: Footer shows version from the authenticated /version endpoint …")
    failures: list[str] = []

    src_dir = FRONTEND_DIR / "src"
    if not src_dir.is_dir():
        return _assert(False, "frontend/src/ directory exists (prerequisite)")

    all_src = ""
    for f in src_dir.rglob("*.tsx"):
        all_src += _read(f)

    # /version endpoint reference
    has_version_endpoint = bool(
        re.search(
            r"/version|version.*endpoint|fetch.*version|version.*fetch",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_version_endpoint,
        "frontend/src/ references the /version endpoint",
        "Per AC: 'Footer shows the version from the authenticated /version endpoint.'",
    )

    # Footer reference
    has_footer = bool(
        re.search(
            r"footer|Footer|version.*display|display.*version",
            all_src,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_footer,
        "frontend/src/ contains a footer or version display element",
        "Per AC: 'Footer shows the version from the authenticated /version endpoint.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — frontend-stack.ts defines Amplify Hosting app (no longer placeholder)
# ---------------------------------------------------------------------------

def check_f_amplify_hosting_cdk() -> list[str]:
    print("\nCheck F: infra/lib/nested/frontend-stack.ts defines Amplify Hosting app …")
    failures: list[str] = []

    if not FRONTEND_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/frontend-stack.ts exists (prerequisite)")

    frontend_ts = _read(FRONTEND_STACK_PATH)

    # Must reference Amplify CDK constructs or CfnApp
    has_amplify_cdk = bool(
        re.search(
            r"amplify|Amplify|CfnApp|aws-amplify|amplifyhosting|AmplifyApp",
            frontend_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_amplify_cdk,
        "frontend-stack.ts references Amplify CDK constructs or CfnApp",
        "Per AC: 'Amplify Hosting app defined in infra/lib/frontend-stack.ts.'",
    )

    # Must NOT still be a pure placeholder (the placeholder comment is gone)
    still_placeholder = bool(
        re.search(
            r"Placeholder:\s+Amplify Hosting resources defined in #54\.",
            frontend_ts,
        )
    )
    failures += _assert(
        not still_placeholder,
        "frontend-stack.ts is no longer a stub placeholder",
        "Per AC: The FrontendStack must define real Amplify Hosting resources.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — DEV auto-build/auto-publish enabled; PROD deliberate promotion only
# ---------------------------------------------------------------------------

def check_g_dev_vs_prod_autopublish() -> list[str]:
    print(
        "\nCheck G: DEV auto-build/auto-publish allowed; "
        "PROD requires deliberate promotion …"
    )
    failures: list[str] = []

    if not FRONTEND_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/frontend-stack.ts exists (prerequisite)")

    frontend_ts = _read(FRONTEND_STACK_PATH)

    # Must distinguish dev vs prod auto-publish behavior
    has_dev_auto = bool(
        re.search(
            r"auto.*build|auto.*publish|autoBuild|autoPublish|"
            r"enableAutoBranchCreation|branchAutoPublish|"
            r"AutoSubDomain|autoBranch",
            frontend_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_dev_auto,
        "frontend-stack.ts references auto-build/auto-publish configuration",
        "Per AC: 'Branch auto-build/auto-publish on push to main is allowed in the DEV account only.'",
    )

    # Must distinguish prod — no auto-publish in prod
    has_prod_guard = bool(
        re.search(
            r"prod.*not.*auto|not.*auto.*prod|"
            r"deliberate.*promotion|promotion.*deliberate|"
            r"prod.*manual|manual.*prod|"
            r"envName.*prod|prod.*envName|"
            r"dev.*auto|auto.*dev",
            frontend_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_prod_guard,
        "frontend-stack.ts differentiates prod (no auto-publish) from dev (auto-publish allowed)",
        "Per AC: 'The prod Amplify app does NOT auto-publish on merge — "
        "prod is advanced by a deliberate promotion of a specific built frontend artifact.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — CI build script produces frontend artifact
# ---------------------------------------------------------------------------

def check_h_ci_build_script() -> list[str]:
    print("\nCheck H: CI build script produces the frontend artifact …")
    failures: list[str] = []

    pkg_json_path = FRONTEND_DIR / "package.json"
    if not pkg_json_path.is_file():
        return _assert(False, "frontend/package.json exists (prerequisite)")

    pkg = json.loads(_read(pkg_json_path))
    scripts = pkg.get("scripts", {})

    # Must have a 'build' script
    has_build = "build" in scripts
    failures += _assert(
        has_build,
        "frontend/package.json defines a 'build' script",
        "Per AC: 'CI build produces the frontend artifact.' "
        "Add a 'build' script (e.g. 'vite build') to package.json.",
    )

    if has_build:
        build_cmd = scripts["build"]
        uses_vite = bool(re.search(r"vite|tsc", build_cmd, re.IGNORECASE))
        failures += _assert(
            uses_vite,
            "frontend 'build' script runs vite build (or tsc)",
            f"Got: '{build_cmd}'. Expected 'vite build' or similar.",
        )

    return failures


# ---------------------------------------------------------------------------
# Check I — cdk synth runs cleanly with updated frontend stack
# ---------------------------------------------------------------------------

def check_i_cdk_synth() -> list[str]:
    print("\nCheck I: cdk synth runs cleanly with updated frontend stack …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite)")

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
                f"stderr: {install.stderr[-500:]}",
            )

    with tempfile.TemporaryDirectory(prefix="contract-toaster-gate-frontend-cdk-out-") as tmp_out:
        result = subprocess.run(
            [
                "npx", "cdk", "synth",
                "--context", "env=dev",
                *NEUTRAL_CDK_CONTEXT,
                "--output", tmp_out,
                "--quiet",
            ],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        failures += _assert(
            result.returncode == 0,
            "cdk synth --context env=dev exits 0 (with Amplify Hosting frontend stack)",
            f"stdout (last 800 chars): {result.stdout[-800:]}\n"
            f"stderr (last 800 chars): {result.stderr[-800:]}",
        )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("FrontendStack structural gate (issue #54)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_frontend_scaffold()
    all_failures += check_b_amplify_libraries()
    all_failures += check_c_amplify_auth_config()
    all_failures += check_d_header_email()
    all_failures += check_e_footer_version()
    all_failures += check_f_amplify_hosting_cdk()
    all_failures += check_g_dev_vs_prod_autopublish()
    all_failures += check_h_ci_build_script()
    all_failures += check_i_cdk_synth()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all FrontendStack structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
