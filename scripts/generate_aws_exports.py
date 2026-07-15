#!/usr/bin/env python3
"""
generate_aws_exports.py -- turn CDK stack outputs into the frontend's
`aws-exports.ts` (issue #241).

## Problem this solves

`frontend/src/aws-exports.ts` is committed as a placeholder stub (a
`us-east-1_PLACEHOLDER` user-pool ID, a `PLACEHOLDER_CLIENT_ID`, ...) so the
SPA type-checks and builds without a live AWS deploy. Its own docstring, and
a comment in `frontend/src/main.tsx`, have long described a script that turns
a real `cdk deploy` into a real `aws-exports.ts` -- but that script never
existed. This is it.

## Input format

`--outputs` is the JSON file `cdk deploy --outputs-file <path>` writes:

    {"<StackName>": {"<OutputKey>": "<value>", ...}, ...}

If the file has more than one top-level stack key, pass `--stack <name>` to
disambiguate (there is no reasonable default when a deploy touched multiple
stacks).

## Output-key contract

Looked up (with a couple of historically-used aliases) from the selected
stack's outputs:

  - User pool ID:        `UserPoolId`
  - User pool client ID: `UserPoolClientId`
  - Hosted-UI domain:    `UserPoolDomainPrefix` (combined with `--region` into
                          `<prefix>.auth.<region>.amazoncognito.com`), or a
                          full domain under `UserPoolDomain` / `CognitoHostedUiDomain`.
  - API base URL:        `ApiUrl`, falling back to `AppRunnerServiceUrl` or
                          `ApiOrigin` (all three names have been used at
                          different points for the App Runner service URL;
                          see `infra/lib/nested/app-stack.ts`). A bare host
                          (no `http(s)://` prefix -- CfnOutput's
                          `attrServiceUrl` is host-only) is normalized to
                          `https://`.

NOTE: as of this issue, `infra/lib/contract-toaster-stack.ts` (the CDK root
stack) does not yet re-export these values as top-level `CfnOutput`s itself
-- they currently live only on the nested `AuthStack` / `AppStack` and are
consumed internally (see contract-toaster-stack.ts:270-306), so a real
`cdk deploy --outputs-file` will not yet contain them. Promoting them to
root-stack outputs is deploy-wiring (issue #225's territory, explicitly
out of scope here); this script defines the consuming contract each output
key must satisfy once that lands. Until then, run this script against a
manually-assembled outputs JSON (documented above) -- e.g. from
`aws cloudformation describe-stacks` against the nested stacks directly.

## Usage

    python3 scripts/generate_aws_exports.py \\
        --outputs cdk-outputs.json \\
        --output frontend/src/aws-exports.ts \\
        --region us-east-1 \\
        [--stack ContractToasterStack-dev]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = REPO_ROOT / "frontend" / "src" / "aws-exports.ts"
DEFAULT_REGION = "us-east-1"

USER_POOL_ID_KEYS = ("UserPoolId",)
USER_POOL_CLIENT_ID_KEYS = ("UserPoolClientId",)
USER_POOL_DOMAIN_PREFIX_KEYS = ("UserPoolDomainPrefix",)
USER_POOL_FULL_DOMAIN_KEYS = ("UserPoolDomain", "CognitoHostedUiDomain")
API_BASE_URL_KEYS = ("ApiUrl", "AppRunnerServiceUrl", "ApiOrigin")


class GenerateAwsExportsError(Exception):
    """A clean, user-facing error (missing file, ambiguous stack, missing key)."""


@dataclass(frozen=True)
class AwsExportsConfig:
    user_pool_id: str
    user_pool_client_id: str
    cognito_domain: str
    api_base_url: str


def _first_present(outputs: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = outputs.get(key)
        if value:
            return str(value)
    return None


def _normalize_api_base_url(value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{value}"


def extract_config(outputs: dict, *, region: str = DEFAULT_REGION) -> AwsExportsConfig:
    """Pull the four aws-exports values out of one stack's CDK outputs dict."""
    user_pool_id = _first_present(outputs, USER_POOL_ID_KEYS)
    if not user_pool_id:
        raise GenerateAwsExportsError(
            f"outputs are missing a Cognito user pool ID (expected one of {USER_POOL_ID_KEYS})"
        )

    user_pool_client_id = _first_present(outputs, USER_POOL_CLIENT_ID_KEYS)
    if not user_pool_client_id:
        raise GenerateAwsExportsError(
            f"outputs are missing a Cognito user pool client ID (expected one of {USER_POOL_CLIENT_ID_KEYS})"
        )

    full_domain = _first_present(outputs, USER_POOL_FULL_DOMAIN_KEYS)
    if full_domain:
        cognito_domain = full_domain
    else:
        domain_prefix = _first_present(outputs, USER_POOL_DOMAIN_PREFIX_KEYS)
        if not domain_prefix:
            raise GenerateAwsExportsError(
                "outputs are missing a Cognito hosted-UI domain (expected one of "
                f"{USER_POOL_DOMAIN_PREFIX_KEYS + USER_POOL_FULL_DOMAIN_KEYS})"
            )
        cognito_domain = f"{domain_prefix}.auth.{region}.amazoncognito.com"

    api_base_url_raw = _first_present(outputs, API_BASE_URL_KEYS)
    if not api_base_url_raw:
        raise GenerateAwsExportsError(
            f"outputs are missing an API base URL (expected one of {API_BASE_URL_KEYS})"
        )
    api_base_url = _normalize_api_base_url(api_base_url_raw)

    return AwsExportsConfig(
        user_pool_id=user_pool_id,
        user_pool_client_id=user_pool_client_id,
        cognito_domain=cognito_domain,
        api_base_url=api_base_url,
    )


def select_stack_outputs(all_outputs: dict, *, stack_name: str | None) -> dict:
    if not all_outputs:
        raise GenerateAwsExportsError("outputs file contains no stacks")

    if stack_name is not None:
        if stack_name not in all_outputs:
            raise GenerateAwsExportsError(
                f"stack {stack_name!r} not found in outputs file "
                f"(available: {sorted(all_outputs)})"
            )
        return all_outputs[stack_name]

    if len(all_outputs) > 1:
        raise GenerateAwsExportsError(
            "outputs file contains more than one stack "
            f"({sorted(all_outputs)}) -- pass --stack to disambiguate"
        )

    return next(iter(all_outputs.values()))


def build_aws_exports_source(config: AwsExportsConfig) -> str:
    """Render aws-exports.ts source with real values baked in as the
    `import.meta.env.VITE_*` fallback defaults, so the same env-var override
    mechanism the committed stub uses (and the DTS Docker target relies on)
    keeps working, while a deploy that doesn't set those env vars still gets
    the real deployed values instead of PLACEHOLDER stubs.
    """
    return f"""/**
 * aws-exports.ts -- Amplify configuration.
 *
 * GENERATED by `python3 scripts/generate_aws_exports.py` from CDK stack
 * outputs after `cdk deploy`. Do not hand-edit: re-run the script to
 * regenerate. See that script's module docstring for the full CDK-outputs
 * key contract.
 *
 * Every value below still resolves through `import.meta.env.VITE_*` first,
 * so it can be overridden at build/run time (e.g. the DTS Docker target,
 * which has no Cognito and sets these via compose env vars instead) --
 * the literal here is only the fallback default, now set to this
 * deploy's real values instead of a placeholder.
 */

const awsExports = {{
  Auth: {{
    Cognito: {{
      // Cognito User Pool ID.
      userPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID ?? '{config.user_pool_id}',
      // Cognito App Client ID.
      userPoolClientId: import.meta.env.VITE_COGNITO_CLIENT_ID ?? '{config.user_pool_client_id}',
      // Cognito hosted UI domain.
      loginWith: {{
        oauth: {{
          domain: import.meta.env.VITE_COGNITO_DOMAIN ?? '{config.cognito_domain}',
          scopes: ['email', 'openid', 'profile'],
          redirectSignIn: [import.meta.env.VITE_REDIRECT_SIGN_IN ?? 'http://localhost:3000'],
          redirectSignOut: [import.meta.env.VITE_REDIRECT_SIGN_OUT ?? 'http://localhost:3000'],
          responseType: 'code' as const,
        }},
      }},
    }},
  }},
}};

// API base URL for the deployed App Runner service. Components read this
// via `import.meta.env.VITE_API_BASE_URL` directly (see PasswordLogin.tsx,
// ReviewSubmission.tsx, AdminUsers.tsx, AdminRetention.tsx, App.tsx); this
// export exists so the value is also discoverable from this generated file.
export const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? '{config.api_base_url}';

export default awsExports;
"""


def generate(*, outputs_path: Path, output_path: Path, region: str, stack_name: str | None) -> None:
    if not outputs_path.exists():
        raise GenerateAwsExportsError(f"outputs file not found: {outputs_path}")

    try:
        all_outputs = json.loads(outputs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GenerateAwsExportsError(f"outputs file is not valid JSON: {outputs_path} ({exc})") from exc

    if not isinstance(all_outputs, dict):
        raise GenerateAwsExportsError(
            f"outputs file must be a JSON object of {{stackName: {{outputKey: value}}}}, got {type(all_outputs).__name__}"
        )

    stack_outputs = select_stack_outputs(all_outputs, stack_name=stack_name)
    config = extract_config(stack_outputs, region=region)
    source = build_aws_exports_source(config)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outputs", required=True, type=Path, help="Path to a cdk deploy --outputs-file JSON.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Path to write the generated aws-exports.ts (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION}).")
    parser.add_argument(
        "--stack",
        default=None,
        help="Stack name to read from the outputs file, if it contains more than one.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        generate(
            outputs_path=args.outputs,
            output_path=args.output,
            region=args.region,
            stack_name=args.stack,
        )
    except GenerateAwsExportsError as exc:
        print(f"generate_aws_exports: error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
