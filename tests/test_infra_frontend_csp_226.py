#!/usr/bin/env python3
"""
Slice test for issue #226: CSP `connect-src 'self'` blocks the Cognito
hosted-UI token exchange and every App Runner API call from the deployed SPA.

Background
----------
`FrontendStack` (infra/lib/nested/frontend-stack.ts) hard-coded
`connect-src 'self'` in the Amplify custom-headers CSP, on the premise that
"the app only calls the same-origin /api endpoints". Nothing proxies /api on
the Amplify origin — the API lives on the App Runner domain (App.tsx fetches
`${VITE_API_BASE_URL}/version`, a genuine cross-origin call), and Amplify
Auth's OAuth code->token exchange is an XHR to the Cognito hosted-UI domain
(`https://<domain>/oauth2/token`). Under `connect-src 'self'` the browser
blocks both: sign-in fails after the hosted-UI redirect, and every API call
is refused. There was also no SPA rewrite rule, so the OAuth callback
deep-link (and any client-side route) 404s on Amplify instead of serving
index.html.

This test is OFFLINE and DETERMINISTIC: it runs `cdk synth` (no AWS calls),
then asserts structurally against the synthesized CloudFormation template
that:

  1. `cdk synth --context env=dev` exits 0.
  2. The synthesized FrontendStack nested template's `AWS::Amplify::App`
     resource has a `Content-Security-Policy` header whose `connect-src`
     directive is NOT bare `'self'` — it includes the Cognito hosted-UI auth
     domain (a synth-time-known literal) and a reference to the App Runner
     service's `ServiceUrl` attribute (deploy-time-known; threaded in as a
     CDK token/cross-stack reference, not a literal string).
  3. The app's `CustomRules` contains a SPA rewrite rule: a non-asset path
     pattern targeting `/index.html`.

It must FAIL on the pre-fix tree (`connect-src 'self'` hardcoded at
frontend-stack.ts:181-ish; no SPA rewrite rule) and PASS after the fix.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _run_cdk_synth() -> subprocess.CompletedProcess:
    """
    Run `npx cdk synth --context env=dev` into the default infra/cdk.out
    directory. We clear cdk.out first so a stale artifact from a prior
    (possibly RED) run cannot cause a false pass or fail — mirrors
    scripts/check.sh's top-of-run `rm -rf infra/cdk.out`.
    """
    if CDK_OUT.exists():
        shutil.rmtree(CDK_OUT)

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
            raise RuntimeError(f"npm install failed: {install.stderr[-500:]}")

    return subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )


def _load_frontend_template() -> dict | None:
    if not CDK_OUT.is_dir():
        return None
    candidates = sorted(CDK_OUT.glob("contracttoaster*Frontend*.nested.template.json"))
    if not candidates:
        return None
    dev = [f for f in candidates if "dev" in f.name.lower()]
    template_file = dev[0] if dev else candidates[0]
    return json.loads(template_file.read_text(encoding="utf-8"))


def _find_amplify_app(template: dict) -> tuple[str, dict] | None:
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") == "AWS::Amplify::App":
            return logical_id, resource
    return None


def _flatten_strings(node: Any) -> list[str]:
    """
    Walk a (possibly Fn::Join / Ref / Fn::GetAtt-laden) CloudFormation
    property value and collect every literal string found anywhere in it,
    in order. Used to search for both literal text (e.g. the Cognito
    domain) and structural references (e.g. a Ref/Fn::GetAtt whose logical
    id mentions "ServiceUrl") without assuming exactly how CDK chose to
    render the intrinsic (Fn::Join vs Fn::Sub, Ref vs Fn::GetAtt, whether
    the App Runner origin crosses a nested-stack boundary).
    """
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_flatten_strings(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_flatten_strings(v))
    return out


def check_synth_exits_zero() -> tuple[list[str], subprocess.CompletedProcess | None]:
    print("\nCheck 1: cdk synth --context env=dev exits 0 …")
    try:
        result = _run_cdk_synth()
    except RuntimeError as exc:
        return _assert(False, "cdk synth prerequisite (npm install) succeeded", str(exc)), None

    failures = _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0",
        f"stdout (last 800 chars): {result.stdout[-800:]}\n"
        f"stderr (last 800 chars): {result.stderr[-800:]}",
    )
    return failures, result


def check_connect_src_allows_cognito_and_api() -> list[str]:
    print(
        "\nCheck 2: CSP connect-src includes the Cognito hosted-UI domain and "
        "the App Runner API origin (not bare 'self') …"
    )
    failures: list[str] = []

    template = _load_frontend_template()
    failures += _assert(
        template is not None,
        "Synthesized FrontendStack nested-stack template found in infra/cdk.out",
        "No contracttoaster*Frontend*.nested.template.json found — cdk synth "
        "may have failed or FrontendStack is not synthesized separately.",
    )
    if template is None:
        return failures

    found_app = _find_amplify_app(template)
    failures += _assert(
        found_app is not None,
        "AWS::Amplify::App resource found in the synthesized FrontendStack template",
    )
    if found_app is None:
        return failures

    _logical_id, amplify_app = found_app
    custom_headers = amplify_app.get("Properties", {}).get("CustomHeaders")
    failures += _assert(
        custom_headers is not None,
        "AWS::Amplify::App has a CustomHeaders property",
    )
    if custom_headers is None:
        return failures

    parts = _flatten_strings(custom_headers)
    rendered = "".join(parts)
    print(f"  CustomHeaders literal text (joined): {rendered[:400]}…")

    has_csp = "Content-Security-Policy" in rendered
    failures += _assert(has_csp, "CustomHeaders sets a Content-Security-Policy header")
    if not has_csp:
        return failures

    # The bare, pre-fix directive was exactly "connect-src 'self';" (nothing
    # else before the next directive's semicolon). Assert that exact bare
    # form is gone.
    bare_connect_src = "connect-src 'self';" in rendered.replace('\\"', '"')
    failures += _assert(
        not bare_connect_src,
        "connect-src is no longer bare 'self' (no allowed cross-origin targets)",
        "Found literal \"connect-src 'self';\" — the app cannot call the "
        "cross-origin App Runner API nor complete the Cognito hosted-UI "
        "token exchange under this policy (issue #226).",
    )

    # The literal fragment immediately after 'self' must lead with
    # "connect-src 'self' https://" — i.e. at least one additional origin is
    # present in the same directive.
    has_extra_origin_prefix = "connect-src 'self' https://" in rendered
    failures += _assert(
        has_extra_origin_prefix,
        "connect-src directive lists at least one https:// origin after 'self'",
    )

    # Cognito hosted-UI domain: synth-time-known literal, must appear
    # verbatim (host only, e.g. contract-toaster-dev.auth.us-east-1.amazoncognito.com).
    has_cognito_domain = bool(
        __import__("re").search(r"https://[\w.-]+\.auth\.[\w-]+\.amazoncognito\.com", rendered)
    )
    failures += _assert(
        has_cognito_domain,
        "connect-src includes the Cognito hosted-UI auth domain "
        "(https://<prefix>.auth.<region>.amazoncognito.com)",
        "Per issue #226: the OAuth code->token exchange after the hosted-UI "
        "redirect is an XHR to this domain's /oauth2/token endpoint.",
    )

    # App Runner API origin: deploy-time-known (CFN attribute/cross-stack
    # ref), not a synth-time literal — assert it is threaded in structurally
    # by searching every string anywhere in the CustomHeaders intrinsic tree
    # (Ref logical ids, Fn::GetAtt attribute names, parameter names) for a
    # reference to the App Runner service's ServiceUrl attribute.
    has_service_url_ref = any("ServiceUrl" in p for p in parts)
    failures += _assert(
        has_service_url_ref,
        "CustomHeaders structurally references the App Runner service's "
        "ServiceUrl attribute (App Runner API origin threaded in as a prop, "
        "not a hardcoded literal)",
        f"No string containing 'ServiceUrl' found among the CustomHeaders "
        f"intrinsic parts: {parts!r}",
    )

    return failures


def check_spa_rewrite_rule_present() -> list[str]:
    print("\nCheck 3: Amplify app has a SPA rewrite rule (404 → /index.html) …")
    failures: list[str] = []

    template = _load_frontend_template()
    if template is None:
        return _assert(False, "Synthesized FrontendStack template available (prerequisite)")

    found_app = _find_amplify_app(template)
    if found_app is None:
        return _assert(False, "AWS::Amplify::App resource found (prerequisite)")

    _logical_id, amplify_app = found_app
    custom_rules = amplify_app.get("Properties", {}).get("CustomRules")

    failures += _assert(
        bool(custom_rules),
        "AWS::Amplify::App has a non-empty CustomRules property",
        "Without a SPA rewrite rule, any deep-link path (including the "
        "Cognito OAuth callback, e.g. /?code=...&state=...) that isn't a "
        "literal built file 404s on Amplify Hosting instead of serving "
        "index.html for the client-side app to handle.",
    )
    if not custom_rules:
        return failures

    rewrite_rules = [
        r
        for r in custom_rules
        if isinstance(r, dict) and r.get("Target") == "/index.html"
    ]
    failures += _assert(
        len(rewrite_rules) >= 1,
        "A CustomRules entry targets /index.html",
        f"CustomRules found: {custom_rules!r}",
    )
    if not rewrite_rules:
        return failures

    rule = rewrite_rules[0]
    status = rule.get("Status")
    failures += _assert(
        status in ("200", "404-200"),
        "The /index.html rewrite rule uses a rewrite status (200 or 404-200), "
        "not a redirect",
        f"Status was: {status!r}",
    )

    source = rule.get("Source", "")
    # Should not be a literal single-file match — it must apply broadly to
    # non-asset paths (the standard Amplify SPA pattern excludes common
    # static-asset extensions via a negative lookahead, or is a catch-all).
    is_broad_match = source in ("/<*>",) or "[^.]" in source or ".*" in source
    failures += _assert(
        is_broad_match,
        "The rewrite rule's Source pattern is a broad (non-asset-path) match, "
        "not a single literal path",
        f"Source was: {source!r}",
    )

    return failures


def main() -> int:
    print("FrontendStack CSP connect-src + SPA rewrite gate (issue #226)")
    print("=" * 60)

    all_failures: list[str] = []

    synth_failures, _result = check_synth_exits_zero()
    all_failures += synth_failures

    # Only meaningful to inspect the template if synth actually produced one;
    # the checks below fail cleanly if it's absent.
    all_failures += check_connect_src_allows_cognito_and_api()
    all_failures += check_spa_rewrite_rule_present()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all FrontendStack CSP connect-src + SPA rewrite checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
