#!/usr/bin/env python3
"""
Structural gate for issue #245: Cognito username/password support for demo
auth mode (infra slice).

Issue #232 landed the BACKEND half of the demo auth feature: an
admin-configurable auth-mode toggle (`sso` / `password` / `both`) and
username/password login validated against the app's own DynamoDB store
(PBKDF2 hashes — see `backend/src/demo_auth.py`). It deferred the Cognito
side to this issue.

This is the INFRA half: `infra/lib/nested/auth-stack.ts` gains a
username/password Cognito app client, gated on an `authMode` deploy config
that uses the same three spellings as the backend, provisioned ONLY when the
mode permits password sign-in.

Read this before assuming the gate proves more than it does: the current
backend does NOT call Cognito to check a password (`backend/src/auth.py`
dispatches the password mode to `demo_auth`'s own store). This client is
optionality for a future deployment shape. The assertions below are
therefore about the SHAPE of the synthesized template, which is exactly what
issue #245 scopes ("cdk synth + structural assertions only; live deploy is
deferred to a human").

Everything here is offline: `cdk synth` + JSON assertions on the synthesized
nested-stack template. No AWS calls.

  A. Source wiring: auth-stack.ts declares the `authMode` prop and gates the
     password client on it; contract-toaster-stack.ts reads
     `--context authMode`.

  --- authMode=both ---
  B. `cdk synth --context authMode=both` exits 0.
  C. The Auth template has exactly ONE app client exposing a
     username/password flow (`ALLOW_USER_SRP_AUTH`), and it is NOT the
     Google/SSO client.
  D. That password client is Cognito-directory-only, has no client secret,
     and does NOT enable the plaintext direct flow
     (`ALLOW_USER_PASSWORD_AUTH`) — SRP keeps the password off the wire.
  E. NO REGRESSION of tests/test_infra_auth_stack.py check F: the
     Google/SSO client's own `ExplicitAuthFlows` contain neither
     `ALLOW_USER_PASSWORD_AUTH` nor `ALLOW_USER_SRP_AUTH`.
     Check F in that file is a whole-file source regex, which cannot tell
     one app client from another once a second client exists; this check
     pins the invariant per RESOURCE, in the synthesized template, where it
     is actually load-bearing.
  F. The auth-mode config reached the template (an Output carries the
     resolved mode) — wiring asserted structurally, not by grepping source.
  G. Secret hygiene: no credential plaintext in the synthesized template —
     no `ClientSecret`, no `GenerateSecret: true`, and the Google IdP client
     secret is still a CloudFormation dynamic reference. Plus the de-brand
     bar: the emitted template carries no `Exos`/`EXOS` string.

  --- authMode=password ---
  H. Password client present; the Output carries `password`.

  --- invalid mode ---
  I. `--context authMode=<bogus>` fails synth closed, naming `authMode`.

  --- default (no authMode context) ---
  J. NO app client exposes a password/SRP flow, the Output carries `sso`,
     and there is exactly one app client — a deploy that says nothing about
     auth mode is unchanged from pre-#245.

This check runs LAST and with the exact default context every other infra
test uses, so `infra/cdk.out/` is left in the state those tests expect when
they inspect it without re-synthesizing (same ordering contract as
tests/test_infra_minimal_profile_231.py).

It must FAIL on the pre-#245 tree: no `authMode` config exists there, so no
password client is ever synthesized and checks A, C, D, F, H all fail.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"
AUTH_STACK_PATH = INFRA / "lib" / "nested" / "auth-stack.ts"
PARENT_STACK_PATH = INFRA / "lib" / "contract-toaster-stack.ts"

# Cognito's two username/password sign-in flows. SRP is the one this stack
# enables (the password never leaves the client); the direct flow posts the
# plaintext password to the Cognito API and must stay off.
SRP_FLOW = "ALLOW_USER_SRP_AUTH"
PLAINTEXT_PASSWORD_FLOW = "ALLOW_USER_PASSWORD_AUTH"
PASSWORD_FLOWS = (SRP_FLOW, PLAINTEXT_PASSWORD_FLOW)


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _run_synth(context_args: list[str]) -> subprocess.CompletedProcess:
    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"], cwd=INFRA, capture_output=True, text=True
        )
        if install.returncode != 0:
            raise RuntimeError(
                f"npm install failed:\nstdout: {install.stdout[-800:]}\n"
                f"stderr: {install.stderr[-800:]}"
            )
    return subprocess.run(
        ["npx", "cdk", "synth", *context_args, *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _auth_template_path() -> Path | None:
    candidates = sorted(CDK_OUT.glob("*Auth*.nested.template.json"))
    return candidates[-1] if candidates else None


def _load_auth_template() -> dict:
    path = _auth_template_path()
    if path is None:
        raise FileNotFoundError(
            f"No *Auth*.nested.template.json found in {CDK_OUT} after cdk synth."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _app_clients(template: dict) -> dict[str, dict]:
    """Logical id → resource, for every AWS::Cognito::UserPoolClient."""
    return {
        lid: r
        for lid, r in template.get("Resources", {}).items()
        if r.get("Type") == "AWS::Cognito::UserPoolClient"
    }


def _flows(client: dict) -> list[str]:
    return list(client.get("Properties", {}).get("ExplicitAuthFlows", []) or [])


def _idps(client: dict) -> list[str]:
    return list(
        client.get("Properties", {}).get("SupportedIdentityProviders", []) or []
    )


def _clients_with_password_flow(template: dict) -> dict[str, dict]:
    return {
        lid: c
        for lid, c in _app_clients(template).items()
        if any(f in PASSWORD_FLOWS for f in _flows(c))
    }


def _google_clients(template: dict) -> dict[str, dict]:
    return {
        lid: c
        for lid, c in _app_clients(template).items()
        if any(idp.lower() == "google" for idp in _idps(c))
    }


def _render_cfn_string(value: object) -> str:
    """Flatten a CloudFormation string-valued property to its literal parts.

    `client_secret` synthesizes as an `Fn::Join` over literal chunks and an
    `AWS::Partition` Ref, so a plain `isinstance(value, str)` test would be
    reading the wrong shape. Non-literal parts are rendered as `<ref>` — they
    are resolved at deploy time and are not where plaintext could hide.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        delimiter, parts = value["Fn::Join"]
        return delimiter.join(_render_cfn_string(p) for p in parts)
    return "<ref>"


def _output_values(template: dict) -> dict[str, object]:
    return {k: v.get("Value") for k, v in template.get("Outputs", {}).items()}


def _auth_mode_output(template: dict) -> str | None:
    """The resolved auth mode carried out of the stack as a plain-string
    Output value. Returns None if no Output carries one of the three modes."""
    for value in _output_values(template).values():
        if isinstance(value, str) and value in ("sso", "password", "both"):
            return value
    return None


# ---------------------------------------------------------------------------
# Check A — source wiring
# ---------------------------------------------------------------------------

def check_a_source_wiring() -> list[str]:
    print("\nCheck A: authMode config is declared and gates the password client …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")
    if not PARENT_STACK_PATH.is_file():
        return _assert(False, "contract-toaster-stack.ts exists (prerequisite)")

    auth_ts = AUTH_STACK_PATH.read_text(encoding="utf-8")
    parent_ts = PARENT_STACK_PATH.read_text(encoding="utf-8")

    failures += _assert(
        bool(re.search(r"authMode\s*\??\s*:\s*AuthMode", auth_ts)),
        "auth-stack.ts declares an `authMode` prop typed to the auth-mode union",
        "Per issue #245: the password surface must be wired to the auth-mode config.",
    )
    failures += _assert(
        bool(re.search(r"CfnUserPoolClient\b", auth_ts)),
        "auth-stack.ts defines a SEPARATE app client construct for password auth",
        "Per issue #245: the password path must be additive, not a flag flipped "
        "on the Google/SSO client.",
    )
    failures += _assert(
        bool(re.search(r"tryGetContext\(\s*['\"]authMode['\"]\s*\)", parent_ts)),
        "contract-toaster-stack.ts reads `--context authMode`",
        "Per issue #245: an operator selects the mode at deploy time.",
    )
    return failures


# ---------------------------------------------------------------------------
# Checks B–G — authMode=both
# ---------------------------------------------------------------------------

def check_mode_both() -> list[str]:
    print("\n--- authMode=both ---")
    failures: list[str] = []

    print("Check B: cdk synth --context authMode=both exits 0 …")
    result = _run_synth(["--context", "env=dev", "--context", "authMode=both"])
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev --context authMode=both exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if result.returncode != 0:
        return failures  # nothing further to inspect

    template = _load_auth_template()
    clients = _app_clients(template)
    password_clients = _clients_with_password_flow(template)
    google_clients = _google_clients(template)

    print("\nCheck C: exactly one app client exposes a username/password flow …")
    failures += _assert(
        len(password_clients) == 1,
        "Exactly one AWS::Cognito::UserPoolClient exposes a username/password flow",
        f"Found {len(password_clients)} of {len(clients)} app client(s) with a "
        f"flow in {PASSWORD_FLOWS}: {sorted(password_clients)}. Per issue #245 the "
        "password capability is ONE additive client.",
    )
    failures += _assert(
        bool(google_clients) and not (set(password_clients) & set(google_clients)),
        "The password client is NOT the Google/SSO client",
        f"password-flow clients: {sorted(password_clients)}; "
        f"Google-IdP clients: {sorted(google_clients)}. Per issue #245 these must "
        "be distinct resources.",
    )
    if not password_clients:
        return failures

    print("\nCheck D: the password client is SRP-only, Cognito-only, secretless …")
    for lid, client in password_clients.items():
        flows = _flows(client)
        props = client.get("Properties", {})
        failures += _assert(
            SRP_FLOW in flows,
            f"{lid}: enables {SRP_FLOW} (username/password sign-in)",
            f"ExplicitAuthFlows = {flows}",
        )
        failures += _assert(
            PLAINTEXT_PASSWORD_FLOW not in flows,
            f"{lid}: does NOT enable the plaintext flow {PLAINTEXT_PASSWORD_FLOW}",
            f"ExplicitAuthFlows = {flows}. SRP keeps the password off the wire; the "
            "direct flow posts it to the Cognito API in plaintext.",
        )
        failures += _assert(
            _idps(client) == ["COGNITO"],
            f"{lid}: SupportedIdentityProviders is Cognito-directory-only",
            f"SupportedIdentityProviders = {_idps(client)}. This client must not be "
            "able to start a federated Google sign-in — that is the SSO client's "
            "job, and it is where hosted-domain enforcement lives.",
        )
        failures += _assert(
            props.get("GenerateSecret") is not True,
            f"{lid}: no client secret generated (public browser client)",
            f"GenerateSecret = {props.get('GenerateSecret')!r}",
        )

    print("\nCheck E: Google/SSO client's no-direct-auth invariant NOT regressed …")
    for lid, client in google_clients.items():
        flows = _flows(client)
        offending = [f for f in flows if f in PASSWORD_FLOWS]
        failures += _assert(
            not offending,
            f"{lid} (Google/SSO client): no username/password flow enabled",
            f"ExplicitAuthFlows = {flows}; offending: {offending}. This is "
            "tests/test_infra_auth_stack.py check F, pinned per-resource: a "
            "password flow here would bypass the two-layer hosted-domain "
            "enforcement the SSO client exists to impose.",
        )

    print("\nCheck F: the resolved auth mode reached the synthesized template …")
    failures += _assert(
        _auth_mode_output(template) == "both",
        "An Output carries the resolved auth mode 'both'",
        f"Output values: {_output_values(template)}. Per issue #245 the auth-mode "
        "wiring must be assertable against the template, not just source text.",
    )

    print("\nCheck G: no credential plaintext, and no de-branded strings, emitted …")
    raw = _auth_template_path().read_text(encoding="utf-8")
    failures += _assert(
        "ClientSecret" not in json.dumps(
            {lid: c for lid, c in clients.items()}
        ),
        "No app client carries a ClientSecret property",
        "Per issue #245: passwords/secrets are never written into synthesized "
        "templates.",
    )
    failures += _assert(
        '"GenerateSecret": true' not in raw and '"GenerateSecret":true' not in raw,
        "No app client sets GenerateSecret: true",
        "A generated client secret would land in the deployed stack's outputs "
        "surface; both clients here are public browser clients.",
    )
    idp_resources = [
        r
        for r in template.get("Resources", {}).values()
        if r.get("Type") == "AWS::Cognito::UserPoolIdentityProvider"
    ]
    failures += _assert(
        bool(idp_resources),
        "Google IdP resource still present in the template",
    )
    for idp in idp_resources:
        secret = idp.get("Properties", {}).get("ProviderDetails", {}).get(
            "client_secret"
        )
        rendered = _render_cfn_string(secret)
        failures += _assert(
            rendered.startswith("{{resolve:secretsmanager:")
            and rendered.endswith("}}"),
            "Google IdP client secret is a CloudFormation dynamic reference",
            f"client_secret renders to {rendered!r}. The plaintext must never be "
            "read at synth time (pre-existing auth-stack.ts invariant, unchanged "
            "by #245).",
        )
    branded = re.findall(r"exos", raw, re.IGNORECASE)
    failures += _assert(
        not branded,
        "Synthesized auth template emits no Exos/EXOS string",
        f"Found {len(branded)} occurrence(s). Per issue #245's de-brand bar, use "
        "'your' voicing in emitted labels and descriptions.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — authMode=password
# ---------------------------------------------------------------------------

def check_mode_password() -> list[str]:
    print("\n--- authMode=password ---")
    failures: list[str] = []

    result = _run_synth(["--context", "env=dev", "--context", "authMode=password"])
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev --context authMode=password exits 0",
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if result.returncode != 0:
        return failures

    template = _load_auth_template()
    password_clients = _clients_with_password_flow(template)
    failures += _assert(
        len(password_clients) == 1,
        "authMode=password also provisions the username/password app client",
        f"Found {len(password_clients)}; expected exactly 1. Per issue #245 the "
        "surface is provisioned when the mode PERMITS password sign-in — which is "
        "'password' as well as 'both'.",
    )
    failures += _assert(
        _auth_mode_output(template) == "password",
        "An Output carries the resolved auth mode 'password'",
        f"Output values: {_output_values(template)}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check I — invalid mode fails closed
# ---------------------------------------------------------------------------

def check_invalid_mode_fails_closed() -> list[str]:
    print("\n--- authMode=<invalid> ---")
    failures: list[str] = []

    result = _run_synth(
        ["--context", "env=dev", "--context", "authMode=not-a-real-mode"]
    )
    failures += _assert(
        result.returncode != 0,
        "cdk synth rejects an unrecognized authMode value",
        "An unrecognized mode must fail closed rather than silently falling back "
        "to a mode the operator did not choose.",
    )
    combined = f"{result.stdout}\n{result.stderr}"
    failures += _assert(
        "authMode" in combined,
        "The failure message names `authMode`",
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check J — default context (no authMode) is unchanged from pre-#245
#
# Runs LAST and with the exact default context every other infra test uses,
# so cdk.out/ is left in the state downstream tests in the
# scripts/collect_test_failures.sh loop expect when they inspect it without
# re-synthesizing first.
# ---------------------------------------------------------------------------

def check_default_mode_unchanged() -> list[str]:
    print("\n--- default context (no authMode) ---")
    failures: list[str] = []

    result = _run_synth(["--context", "env=dev"])
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev (no authMode) exits 0",
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    if result.returncode != 0:
        return failures

    template = _load_auth_template()
    clients = _app_clients(template)
    password_clients = _clients_with_password_flow(template)

    failures += _assert(
        len(password_clients) == 0,
        "Default mode provisions NO username/password app client",
        f"Found {sorted(password_clients)}. Per issue #245 the default is 'sso' "
        "and must be fail-closed: a deploy that says nothing about auth mode gets "
        "no password surface.",
    )
    failures += _assert(
        len(clients) == 1,
        "Default mode still synthesizes exactly one app client (pre-#245 shape)",
        f"Found {len(clients)}: {sorted(clients)}.",
    )
    failures += _assert(
        _auth_mode_output(template) == "sso",
        "An Output carries the default resolved auth mode 'sso'",
        f"Output values: {_output_values(template)}",
    )
    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Cognito username/password demo-auth structural gate (issue #245)")
    print("=" * 60)

    if not INFRA.is_dir():
        print("[FAIL] infra/ directory does not exist")
        return 1

    all_failures: list[str] = []

    # Clean slate: a stale cdk.out from a previous run must not be able to
    # satisfy (or poison) the per-mode assertions below.
    if CDK_OUT.is_dir():
        shutil.rmtree(CDK_OUT)

    all_failures += check_a_source_wiring()
    all_failures += check_mode_both()
    all_failures += check_mode_password()
    all_failures += check_invalid_mode_fails_closed()
    # LAST — leaves cdk.out/ in the default-context state (see note above).
    all_failures += check_default_mode_unchanged()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all Cognito demo-auth structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
