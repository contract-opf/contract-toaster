#!/usr/bin/env python3
"""
Slice test for issue #227: WAF rule 'RateLimitUploadEndpoint' (priority 40)
must be scoped to POST so GET status-polling requests (GET /api/reviews/{id})
never match the tight (10 req/5 min) upload rate rule.

Background
----------
`WafStack` (infra/lib/nested/waf-stack.ts) rule 4 (RateLimitUploadEndpoint,
priority 40, limit 10/5min) scopes down only on URI path prefix
(`/api/reviews`, STARTS_WITH). It evaluates *before* rule 5
(RateLimitPollingEndpoint, priority 50, limit 60/5min), and it has no HTTP
method constraint — so GET /api/reviews/{id} (status polling) also matches
rule 4. A UI that polls every few seconds for a 1-3 minute review burns
through the 10-request budget in well under a minute, and the caller's IP
then gets BLOCKed for the rest of the 5-minute window even though rule 5 (the
*intended* polling rule, with headroom for polling) never gets a chance to
apply.

This test is OFFLINE and DETERMINISTIC: it runs `cdk synth` (no AWS calls),
then asserts structurally against the synthesized CloudFormation template
that:

  1. `cdk synth --context env=dev` exits 0.
  2. The synthesized `WafStack` nested template's `AWS::WAFv2::WebACL`
     resource has a rule named `RateLimitUploadEndpoint` (priority 40) whose
     `RateBasedStatement.ScopeDownStatement` is an `AndStatement` combining
     the existing URI-path-prefix `ByteMatchStatement` with a new
     `ByteMatchStatement` on `FieldToMatch.Method` that constrains the match
     to `POST` — so GET requests (status polling) no longer match rule 4 at
     all, regardless of path.

It must FAIL on the pre-fix tree (rule 4 scope-down is a bare
ByteMatchStatement on URI path, no method constraint) and PASS after the fix.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

from __future__ import annotations
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

import json
import shutil
import subprocess
import sys
from pathlib import Path

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


def _load_waf_template() -> dict | None:
    if not CDK_OUT.is_dir():
        return None
    candidates = sorted(CDK_OUT.glob("contracttoaster*Waf*.nested.template.json"))
    if not candidates:
        return None
    dev = [f for f in candidates if "dev" in f.name.lower()]
    template_file = dev[0] if dev else candidates[0]
    return json.loads(template_file.read_text(encoding="utf-8"))


def _find_web_acl(template: dict) -> tuple[str, dict] | None:
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") == "AWS::WAFv2::WebACL":
            return logical_id, resource
    return None


def _find_rule(web_acl: dict, name: str) -> dict | None:
    for rule in web_acl.get("Properties", {}).get("Rules", []):
        if rule.get("Name") == name:
            return rule
    return None


def _byte_match_statements(statement: dict) -> list[dict]:
    """
    Flatten an AndStatement (one level) into its list of ByteMatchStatements,
    or wrap a bare ByteMatchStatement in a single-element list. Statements
    that are neither are ignored (returns []).
    """
    if "AndStatement" in statement:
        out: list[dict] = []
        for sub in statement["AndStatement"].get("Statements", []):
            if "ByteMatchStatement" in sub:
                out.append(sub["ByteMatchStatement"])
        return out
    if "ByteMatchStatement" in statement:
        return [statement["ByteMatchStatement"]]
    return []


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


def check_upload_rate_rule_scoped_to_post() -> list[str]:
    print(
        "\nCheck 2: RateLimitUploadEndpoint (priority 40) scope-down constrains "
        "the HTTP method to POST …"
    )
    failures: list[str] = []

    template = _load_waf_template()
    failures += _assert(
        template is not None,
        "Synthesized WafStack nested-stack template found in infra/cdk.out",
        "No contracttoaster*Waf*.nested.template.json found — cdk synth may "
        "have failed or the WafStack nested stack is not synthesized "
        "separately.",
    )
    if template is None:
        return failures

    found_acl = _find_web_acl(template)
    failures += _assert(
        found_acl is not None,
        "AWS::WAFv2::WebACL resource found in the synthesized WafStack template",
    )
    if found_acl is None:
        return failures

    _logical_id, web_acl = found_acl

    rule = _find_rule(web_acl, "RateLimitUploadEndpoint")
    failures += _assert(
        rule is not None,
        "RateLimitUploadEndpoint rule found in the WebACL",
    )
    if rule is None:
        return failures

    failures += _assert(
        rule.get("Priority") == 40,
        "RateLimitUploadEndpoint keeps priority 40",
        f"Priority was: {rule.get('Priority')!r}",
    )

    rate_stmt = rule.get("Statement", {}).get("RateBasedStatement", {})
    scope_down = rate_stmt.get("ScopeDownStatement", {})

    print(f"  ScopeDownStatement: {json.dumps(scope_down)}")

    is_and = "AndStatement" in scope_down
    failures += _assert(
        is_and,
        "ScopeDownStatement is an AndStatement (combining path AND method)",
        "A bare ByteMatchStatement on URI path alone matches GET and POST "
        "alike — GET /api/reviews/{id} (status polling) then matches this "
        "10-req/5min rule and gets the polling IP blocked within seconds.",
    )
    if not is_and:
        return failures

    byte_matches = _byte_match_statements(scope_down)

    path_matches = [
        bm
        for bm in byte_matches
        if "uriPath" in bm.get("FieldToMatch", {})
        or "UriPath" in bm.get("FieldToMatch", {})
    ]
    method_matches = [
        bm
        for bm in byte_matches
        if "method" in bm.get("FieldToMatch", {})
        or "Method" in bm.get("FieldToMatch", {})
    ]

    failures += _assert(
        len(path_matches) >= 1,
        "AndStatement retains the URI-path-prefix ByteMatchStatement "
        "(/api/reviews)",
        f"ByteMatchStatements found: {byte_matches!r}",
    )

    failures += _assert(
        len(method_matches) >= 1,
        "AndStatement adds a ByteMatchStatement on FieldToMatch.Method",
        f"ByteMatchStatements found: {byte_matches!r}",
    )

    if method_matches:
        method_bm = method_matches[0]
        search_string = method_bm.get("SearchString", "")
        # CFN may render the literal string directly, or base64-encode it
        # depending on synth path; accept either.
        positional = method_bm.get("PositionalConstraint")
        method_value_ok = search_string in ("POST", "cG9zdA==") or (
            isinstance(search_string, str) and search_string.upper() == "POST"
        )
        failures += _assert(
            method_value_ok and positional == "EXACTLY",
            "Method ByteMatchStatement matches EXACTLY 'POST'",
            f"SearchString={search_string!r} PositionalConstraint={positional!r}",
        )

    return failures


def main() -> int:
    print("WAF upload-rate-rule method scope-down gate (issue #227)")
    print("=" * 60)

    all_failures: list[str] = []

    synth_failures, _result = check_synth_exits_zero()
    all_failures += synth_failures

    # Only meaningful to inspect the template if synth actually produced one;
    # check_upload_rate_rule_scoped_to_post() itself fails cleanly if absent.
    all_failures += check_upload_rate_rule_scoped_to_post()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all WAF upload-rate-rule method scope-down checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
