#!/usr/bin/env python3
"""
Slice test for issue #233: extract an `appName` construct-level prefix
parameter so NEW deployments can pick their own resource-name prefix and
GitHub source / alarm destination / callback domain, while the CURRENT
contract-toaster dev/prod deployments keep their exact existing names by
default (stateful resources — S3 buckets, DynamoDB tables — can't be
renamed in place).

Verifies (per the issue's "Required verification" section):

  A. `cdk synth --context env=dev` (no appName) still exits 0 and every
     synthesized S3 bucket / DynamoDB table / KMS key alias name is
     unchanged — prefixed with the literal 'contract-toaster-' — and the
     CodeBuild source owner/repo, alarms-topic subscriber email, and
     Cognito callback/logout URLs are unchanged from their current
     baked-in literals.

  B. `cdk synth --context env=dev --context appName=acmecorp
     --context githubOwner=acme-org --context githubRepo=acme-toaster
     --context alarmsEmail=alerts@acme.example --context appDomain=acme.example`
     exits 0 and:
       - every synthesized S3 bucket / DynamoDB table name is prefixed
         with 'acmecorp-' (none with 'contract-toaster-');
       - every KMS key alias is prefixed with 'alias/acmecorp-';
       - the CodeBuild project's GitHub source resolves to
         acme-org/acme-toaster (cicd-stack.ts:382-390 — was hard-coded to
         contract-opf/contract-toaster);
       - the alarms SNS topic subscription endpoint resolves to
         alerts@acme.example (observability-stack.ts — was hard-coded
         to an internal tenant mailbox);
       - the Cognito UserPoolClient callback/logout URLs resolve to the
         acmecorp.acme.example domain (auth-stack.ts:540-553 — was
         hard-coded to an internal tenant subdomain).

  C. Security-invariant regression guard (issue #349): the two-layer
     hosted-domain enforcement (Google OAuth `hd=` parameter AND the
     pre-token Lambda's ALLOWED_DOMAIN check) tracks the dedicated
     `--context hostedDomain`, NOT `--context appDomain` — supplying a
     custom appDomain for the callback/logout URLs must NOT change hd=.
     appDomain is a naming knob for the SPA's own hosting domain, NOT an
     authorization control.

This file MUST fail on the pre-fix tree (no `appName` context parameter
exists; owner/repo, alarm email, and callback URLs are string literals in
source) and pass once the fix lands.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
CDK_OUT = INFRA / "cdk.out"

STACK_TS = INFRA / "lib" / "contract-toaster-stack.ts"

# hostedDomain is deliberately a DIFFERENT domain than appDomain here (issue
# #349 Check G below): the two are independent security/naming concerns, and
# using distinct values proves hostedDomain does not silently derive from
# (or get overridden by) a custom appDomain.
CUSTOM_HOSTED_DOMAIN = "acme-legal.example"
CUSTOM_ADMIN_EMAIL = "gc@acme-legal.example"

CUSTOM_CONTEXT = [
    "--context", "appName=acmecorp",
    "--context", "githubOwner=acme-org",
    "--context", "githubRepo=acme-toaster",
    "--context", "alarmsEmail=alerts@acme.example",
    "--context", "appDomain=acme.example",
    "--context", f"hostedDomain={CUSTOM_HOSTED_DOMAIN}",
    "--context", f"adminEmail={CUSTOM_ADMIN_EMAIL}",
]


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _ensure_npm_install() -> list[str]:
    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)")
    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent — running npm install first …)")
        install = subprocess.run(
            ["npm", "install"], cwd=INFRA, capture_output=True, text=True,
        )
        if install.returncode != 0:
            return _assert(
                False, "npm install succeeded in infra/",
                f"stderr: {install.stderr[-500:]}",
            )
    return []


def _synth(extra_context: list[str]) -> subprocess.CompletedProcess:
    shutil.rmtree(CDK_OUT, ignore_errors=True)
    return subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", "--quiet", *extra_context],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )


def _collect_templates() -> list[dict]:
    if not CDK_OUT.is_dir():
        return []
    templates = []
    for p in CDK_OUT.glob("*.template.json"):
        templates.append(json.loads(p.read_text(encoding="utf-8")))
    return templates


def _resources_of_type(templates: list[dict], *rtypes: str):
    for tmpl in templates:
        for resource in tmpl.get("Resources", {}).values():
            if resource.get("Type") in rtypes:
                yield resource


def _bucket_and_table_names(templates: list[dict]) -> set[str]:
    names: set[str] = set()
    for r in _resources_of_type(templates, "AWS::S3::Bucket"):
        n = r.get("Properties", {}).get("BucketName")
        if n:
            names.add(n)
    for r in _resources_of_type(templates, "AWS::DynamoDB::Table", "AWS::DynamoDB::GlobalTable"):
        n = r.get("Properties", {}).get("TableName")
        if n:
            names.add(n)
    return names


def _kms_aliases(templates: list[dict]) -> set[str]:
    return {
        r.get("Properties", {}).get("AliasName")
        for r in _resources_of_type(templates, "AWS::KMS::Alias")
        if r.get("Properties", {}).get("AliasName")
    }


def _codebuild_source_locations(templates: list[dict]) -> set[str]:
    return {
        r.get("Properties", {}).get("Source", {}).get("Location")
        for r in _resources_of_type(templates, "AWS::CodeBuild::Project")
        if r.get("Properties", {}).get("Source", {}).get("Location")
    }


def _sns_subscription_endpoints(templates: list[dict]) -> set[str]:
    return {
        r.get("Properties", {}).get("Endpoint")
        for r in _resources_of_type(templates, "AWS::SNS::Subscription")
        if r.get("Properties", {}).get("Endpoint")
    }


def _cognito_callback_urls(templates: list[dict]) -> set[str]:
    urls: set[str] = set()
    for r in _resources_of_type(templates, "AWS::Cognito::UserPoolClient"):
        urls.update(r.get("Properties", {}).get("CallbackURLs") or [])
        urls.update(r.get("Properties", {}).get("LogoutURLs") or [])
    return urls


def _cognito_hd_values(templates: list[dict]) -> set[str]:
    return {
        r.get("Properties", {}).get("ProviderDetails", {}).get("hd")
        for r in _resources_of_type(templates, "AWS::Cognito::UserPoolIdentityProvider")
        if r.get("Properties", {}).get("ProviderDetails", {}).get("hd")
    }


def check_a_appname_param_declared_in_source() -> list[str]:
    print("\nCheck A: contract-toaster-stack.ts declares an appName context parameter …")
    failures: list[str] = []

    failures += _assert(STACK_TS.is_file(), "infra/lib/contract-toaster-stack.ts exists")
    if failures:
        return failures

    src = STACK_TS.read_text(encoding="utf-8")
    failures += _assert(
        "tryGetContext('appName')" in src or 'tryGetContext("appName")' in src,
        "contract-toaster-stack.ts reads 'appName' from CDK context",
        "Expected: this.node.tryGetContext('appName') per the issue's suggested direction "
        "(extract an appName construct-level prefix parameter).",
    )
    return failures


def check_b_default_context_preserves_current_names() -> list[str]:
    print(
        "\nCheck B: synth without githubOwner/alarmsEmail/appDomain/adminEmail/hostedDomain "
        "context fails closed naming the missing keys; explicit context resolves verbatim "
        "(per issues #316, #349) …"
    )
    failures: list[str] = []

    failures += _ensure_npm_install()
    if failures:
        return failures

    # (a) No identity context at all: synth must fail closed, naming every
    # missing key in the error message.
    no_context_result = _synth([])
    failures += _assert(
        no_context_result.returncode != 0,
        "cdk synth --context env=dev (no identity context) exits non-zero",
        f"stdout (last 800): {no_context_result.stdout[-800:]}\n"
        f"stderr (last 800): {no_context_result.stderr[-800:]}",
    )
    combined_output = no_context_result.stdout + no_context_result.stderr
    for missing_key in ("githubOwner", "alarmsEmail", "appDomain", "adminEmail", "hostedDomain"):
        failures += _assert(
            missing_key in combined_output,
            f"missing-context error message names '{missing_key}'",
            f"stdout+stderr (last 1200): {combined_output[-1200:]}",
        )

    # (b) Explicit context (no appName override) resolves the supplied
    # githubOwner/alarmsEmail/appDomain verbatim, and the appName default
    # ('contract-toaster') still prefixes buckets/tables/aliases — the
    # structural naming coverage this check has always provided.
    result = _synth(NEUTRAL_CDK_CONTEXT)
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev with explicit githubOwner/alarmsEmail/appDomain exits 0",
        f"stdout (last 800): {result.stdout[-800:]}\nstderr (last 800): {result.stderr[-800:]}",
    )
    if failures:
        return failures

    templates = _collect_templates()
    failures += _assert(len(templates) > 0, "at least one synthesized template found in cdk.out")
    if failures:
        return failures

    names = _bucket_and_table_names(templates)
    failures += _assert(
        len(names) >= 15,
        f"at least 15 buckets/tables synthesized (found {len(names)})",
    )
    non_default = sorted(n for n in names if not n.startswith("contract-toaster-"))
    failures += _assert(
        not non_default,
        "every bucket/table name is prefixed with the default 'contract-toaster-' (appName unset)",
        f"unexpected names: {non_default}",
    )

    aliases = _kms_aliases(templates)
    non_default_aliases = sorted(a for a in aliases if not a.startswith("alias/contract-toaster-"))
    failures += _assert(
        not non_default_aliases,
        "every KMS key alias is prefixed with the default 'alias/contract-toaster-' (appName unset)",
        f"unexpected aliases: {non_default_aliases}",
    )

    sources = _codebuild_source_locations(templates)
    failures += _assert(
        any("example-org/contract-toaster" in s for s in sources),
        "CodeBuild source resolves to example-org/contract-toaster from --context githubOwner (githubRepo unset)",
        f"sources found: {sources}",
    )

    endpoints = _sns_subscription_endpoints(templates)
    failures += _assert(
        "alarms@example.com" in endpoints,
        "alarms SNS subscription resolves to alarms@example.com from --context alarmsEmail",
        f"endpoints found: {endpoints}",
    )

    urls = _cognito_callback_urls(templates)
    failures += _assert(
        any(u == "https://contract-toaster.example.com" for u in urls),
        "Cognito callback/logout URLs resolve to contract-toaster.example.com from --context appDomain",
        f"urls found: {sorted(urls)}",
    )

    return failures


def check_c_custom_appname_prefixes_buckets_tables_aliases() -> list[str]:
    print("\nCheck C: custom appName prefixes buckets/tables/key aliases …")
    failures: list[str] = []

    result = _synth(CUSTOM_CONTEXT)
    failures += _assert(
        result.returncode == 0,
        "cdk synth with --context appName=acmecorp (+ overrides) exits 0",
        f"stdout (last 800): {result.stdout[-800:]}\nstderr (last 800): {result.stderr[-800:]}",
    )
    if failures:
        return failures

    templates = _collect_templates()
    failures += _assert(len(templates) > 0, "at least one synthesized template found in cdk.out")
    if failures:
        return failures

    names = _bucket_and_table_names(templates)
    failures += _assert(
        len(names) >= 15,
        f"at least 15 buckets/tables synthesized (found {len(names)})",
    )
    stale = sorted(n for n in names if n.startswith("contract-toaster-"))
    failures += _assert(
        not stale,
        "no bucket/table name retains the 'contract-toaster-' literal under a custom appName",
        f"stale names: {stale}",
    )
    not_prefixed = sorted(n for n in names if not n.startswith("acmecorp-"))
    failures += _assert(
        not not_prefixed,
        "every bucket/table name is prefixed with the custom appName 'acmecorp-'",
        f"unprefixed names: {not_prefixed}",
    )

    aliases = _kms_aliases(templates)
    stale_aliases = sorted(a for a in aliases if a.startswith("alias/contract-toaster-"))
    failures += _assert(
        not stale_aliases,
        "no KMS key alias retains the 'alias/contract-toaster-' literal under a custom appName",
        f"stale aliases: {stale_aliases}",
    )
    not_prefixed_aliases = sorted(a for a in aliases if not a.startswith("alias/acmecorp-"))
    failures += _assert(
        not not_prefixed_aliases,
        "every KMS key alias is prefixed with 'alias/acmecorp-'",
        f"unprefixed aliases: {not_prefixed_aliases}",
    )

    return failures


def check_d_codebuild_owner_repo_from_context() -> list[str]:
    print("\nCheck D: CodeBuild source owner/repo resolve from context …")
    failures: list[str] = []

    templates = _collect_templates()
    failures += _assert(len(templates) > 0, "reusing custom-context synth output (run Check C first)")
    if failures:
        return failures

    sources = _codebuild_source_locations(templates)
    failures += _assert(
        any("acme-org/acme-toaster" in s for s in sources),
        "CodeBuild source resolves to acme-org/acme-toaster from --context githubOwner/githubRepo",
        f"sources found: {sources}",
    )
    failures += _assert(
        not any("contract-opf/contract-toaster" in s for s in sources),
        "CodeBuild source no longer hard-codes contract-opf/contract-toaster under custom context",
        f"sources found: {sources}",
    )

    return failures


def check_e_alarm_email_from_context() -> list[str]:
    print("\nCheck E: alarms SNS subscription email resolves from context …")
    failures: list[str] = []

    templates = _collect_templates()
    failures += _assert(len(templates) > 0, "reusing custom-context synth output (run Check C first)")
    if failures:
        return failures

    endpoints = _sns_subscription_endpoints(templates)
    failures += _assert(
        "alerts@acme.example" in endpoints,
        "alarms SNS subscription endpoint resolves to alerts@acme.example from --context alarmsEmail",
        f"endpoints found: {endpoints}",
    )
    failures += _assert(
        "legal-eng@teamexos.com" not in endpoints,
        "alarms SNS subscription no longer hard-codes legal-eng@teamexos.com under custom context",
        f"endpoints found: {endpoints}",
    )

    return failures


def check_f_callback_urls_from_context() -> list[str]:
    print("\nCheck F: Cognito callback/logout URLs resolve from context …")
    failures: list[str] = []

    templates = _collect_templates()
    failures += _assert(len(templates) > 0, "reusing custom-context synth output (run Check C first)")
    if failures:
        return failures

    urls = _cognito_callback_urls(templates)
    failures += _assert(
        any(u == "https://acmecorp.acme.example" for u in urls),
        "Cognito callback/logout URLs include https://acmecorp.acme.example "
        "from --context appName/appDomain",
        f"urls found: {sorted(urls)}",
    )
    stale = [u for u in urls if "teamexos.com" in u]
    failures += _assert(
        not stale,
        "no Cognito callback/logout URL retains the teamexos.com literal under custom context",
        f"stale urls: {stale}",
    )

    return failures


def check_g_hosted_domain_enforcement_unaffected() -> list[str]:
    print(
        "\nCheck G: hosted-domain enforcement tracks --context hostedDomain, "
        "independent of --context appDomain (issue #349) …"
    )
    failures: list[str] = []

    templates = _collect_templates()
    failures += _assert(len(templates) > 0, "reusing custom-context synth output (run Check C first)")
    if failures:
        return failures

    hd_values = _cognito_hd_values(templates)
    failures += _assert(
        hd_values == {CUSTOM_HOSTED_DOMAIN},
        f"Google IdP hd= hosted-domain override is pinned to --context hostedDomain "
        f"({CUSTOM_HOSTED_DOMAIN!r}), not derived from --context appDomain",
        f"hd values found: {hd_values}",
    )
    failures += _assert(
        "acme.example" not in hd_values,
        "hd= does NOT silently pick up --context appDomain's value "
        "(appDomain is a naming knob, not an authorization control)",
        f"hd values found: {hd_values}",
    )

    return failures


def main() -> int:
    print("appName construct-level prefix parameter — structural gate (issue #233)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_appname_param_declared_in_source()
    all_failures += check_b_default_context_preserves_current_names()
    all_failures += check_c_custom_appname_prefixes_buckets_tables_aliases()
    all_failures += check_d_codebuild_owner_repo_from_context()
    all_failures += check_e_alarm_email_from_context()
    all_failures += check_f_callback_urls_from_context()
    all_failures += check_g_hosted_domain_enforcement_unaffected()

    print("\n" + "=" * 60)
    if all_failures:
        print(f"\nFAIL: {len(all_failures)} check(s) failed.\nSee output above for details.")
        return 1

    print("\nPASS: all appName prefix structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
