#!/usr/bin/env python3
"""
Structural gate for issue #53: Cognito + Google IdP AC coverage.

Verifies that all acceptance criteria for issue #53 are satisfied:

  A. AuthStack TypeScript source defines a Cognito UserPool (username = email).

  B. Google OAuth credentials are fetched from Secrets Manager at runtime
     (dynamic reference / runtime secret resolution); CDK does NOT read the
     secret value at synth time.  The secret path 'contract-toaster/cognito/google-oauth'
     must appear in the auth-stack source.

  C. Google IdP attribute mapping covers email, name, and sub.

  D. User pool client is configured with callback and logout URLs (Amplify +
     localhost).

  E. Hosted UI is enabled with a domain prefix.

  F. App client does NOT allow USER_PASSWORD_AUTH (Google IdP only).

  G. Hosted-domain enforcement is in TWO layers:
       (a) hd=teamexos.com pinned in the OAuth authorize scope/attributes, AND
       (b) a pre-sign-up or pre-token-generation Lambda rejects non-@teamexos.com.

  H. Pre-token-generation Lambda:
       - checks legal-admin@teamexos.com group membership via Directory API
       - JIT-creates an active users row in DynamoDB on first sign-in
       - fails closed (denies on Directory API error)

  I. Directory API service-account credentials live in Secrets Manager and are
     least-privilege (read-only).

  J. Break-glass: NO dedicated CDK-managed IAM role for v1 (issue #229 —
     removed a placeholder whose trust policy used an invalid managed-policy
     ARN as its FederatedPrincipal, which would have failed deploy). Emergency
     admin recovery is documented in RUNBOOK.md as the AdministratorAccess
     SSO permission set already granted for this account — not a dedicated
     role/principal — and this is verified structurally (no BreakGlassRole
     resource in the synthesized template), not by grepping source comments.

  K. CDK admin_bootstrap seed: CDK writes the initial admin bootstrap row keyed
     by email into the admin_bootstrap table (not into the sub-keyed users table).
     References to 'admin_bootstrap' and the GC email seed must appear in the
     auth-stack source.

  L. RUNBOOK.md documents the break-glass procedure.

  M. cdk synth runs cleanly with the auth resources present.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from infra_synth_helper import NEUTRAL_CDK_CONTEXT, NEUTRAL_HOSTED_DOMAIN

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
AUTH_STACK_PATH = INFRA / "lib" / "nested" / "auth-stack.ts"
RUNBOOK_PATH = REPO_ROOT / "RUNBOOK.md"

# Module-level cache: check_m populates this with a freshly synthesized template
# so that check_g can assert against it without depending on a pre-existing
# (potentially stale) cdk.out/.  check_g will FAIL if this is None (i.e. synth
# has not been run yet in this gate invocation) — the WARN fallback was replaced
# because it allowed a source-text regex to satisfy the strong template assertion.
_synth_template_cache: dict | None = None


def _load_synthesized_auth_template(cdk_out_dir: Path | None = None) -> dict | None:
    """
    Load the synthesized auth nested stack template from cdk_out_dir (or the
    default infra/cdk.out/ if not specified).
    Returns None if no template is found (synth not yet run).
    We look for a file matching contracttoaster*Auth*.nested.template.json.
    """
    cdk_out = cdk_out_dir if cdk_out_dir is not None else (INFRA / "cdk.out")
    if not cdk_out.is_dir():
        return None
    candidates = sorted(cdk_out.glob("contracttoaster*Auth*.nested.template.json"))
    if not candidates:
        return None
    # Prefer the dev template if present
    dev = [f for f in candidates if "dev" in f.name.lower()]
    template_file = dev[0] if dev else candidates[0]
    return json.loads(template_file.read_text(encoding="utf-8"))


def _find_google_idp_resource(template: dict) -> dict | None:
    """Return the first AWS::Cognito::UserPoolIdentityProvider resource."""
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") == "AWS::Cognito::UserPoolIdentityProvider":
            return resource
    return None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_ts_sources() -> list[Path]:
    sources: list[Path] = []
    for subdir in ("lib", "bin"):
        p = INFRA / subdir
        if p.is_dir():
            sources.extend(p.rglob("*.ts"))
    return sources


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
# Check A — Cognito UserPool with username = email
# ---------------------------------------------------------------------------

def check_a_user_pool() -> list[str]:
    print("\nCheck A: Cognito UserPool defined with username = email …")
    failures: list[str] = []

    failures += _assert(
        AUTH_STACK_PATH.is_file(),
        "infra/lib/nested/auth-stack.ts exists",
    )
    if failures:
        return failures

    auth_ts = _read(AUTH_STACK_PATH)

    has_user_pool = bool(
        re.search(
            r"new\s+cognito\.UserPool|new\s+UserPool\s*\(|aws-cdk-lib/aws-cognito",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_user_pool,
        "Cognito UserPool instantiated in auth-stack.ts",
        "Per AC: 'Cognito user pool created with username = email.'"
        " Use 'new cognito.UserPool(...)' from aws-cdk-lib/aws-cognito.",
    )

    # Username should be set to EMAIL
    has_email_username = bool(
        re.search(
            r"signInAliases.*email|email.*signInAliases|"
            r"USERNAME_EMAIL|signIn.*[Ee]mail|[Ee]mail.*signIn",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_email_username,
        "UserPool configured with email as sign-in alias (username = email)",
        "Per AC: 'username = email'. Set signInAliases: { email: true } on the UserPool.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B — Google OAuth credentials from Secrets Manager (dynamic reference)
# ---------------------------------------------------------------------------

def check_b_secrets_manager_dynamic_ref() -> list[str]:
    print("\nCheck B: Google OAuth credentials from Secrets Manager (dynamic reference) …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    # Secret path must appear
    has_secret_path = bool(
        re.search(
            r"contract-toaster/cognito/google.oauth|contract-toaster-cognito-google-oauth",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_secret_path,
        "Secret path 'contract-toaster/cognito/google-oauth' referenced in auth-stack.ts",
        "Per AC: 'Google OAuth client credentials live in AWS Secrets Manager at "
        "contract-toaster/cognito/google-oauth'.",
    )

    # Must use dynamic reference (SecretValue.secretsManager or fromSecretsManager
    # or SecretsManager.secret or similar), NOT fromPlainText or hard-coded creds
    has_dynamic_ref = bool(
        re.search(
            r"SecretValue\.secretsManager|"
            r"secretsmanager\.Secret|"
            r"fromSecretsManager|"
            r"SecretsManager|"
            r"secrets\.Secret|"
            r"secretComplete|"
            r"secretString|"
            r"SecretValue\.unsafePlainText\s*\(\s*\)|"  # allowed only as placeholder
            r"fromSecretsManagerVersion",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_dynamic_ref,
        "Secret retrieved via dynamic reference / SecretValue.secretsManager (not hard-coded)",
        "Per AC: 'CDK does not read the secret value at synth time — wires a dynamic "
        "reference'. Use SecretValue.secretsManager(...) or secretsmanager.Secret.",
    )

    # Must NOT resolve the OAuth client secret to plaintext at synth time.
    # Specifically: secretValueFromJson(...).unsafeUnwrap() or .toString() on
    # the CLIENT SECRET field is forbidden.  The public clientId may be resolved
    # (it is not sensitive), but the clientSecret must remain a SecretValue.
    # Pattern: 'clientSecret' field resolved to string at synth time
    resolves_client_secret_at_synth = bool(
        re.search(
            r"secretValueFromJson\(['\"]clientSecret['\"].*unsafeUnwrap|"
            r"secretValueFromJson\(['\"]clientSecret['\"].*\.toString\(\)|"
            r"process\.env\.['\"]?(?:GOOGLE_CLIENT_SECRET|CLIENT_SECRET)['\"]?",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        not resolves_client_secret_at_synth,
        "CDK does NOT read OAuth plaintext secret at synth time",
        "Per AC: 'Reading the secret into CDK code at synth is explicitly prohibited.' "
        "The OAuth clientSecret must remain a SecretValue (dynamic reference); "
        "do not call unsafeUnwrap() or toString() on it.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C — Google IdP attribute mapping: email, name, sub
# ---------------------------------------------------------------------------

def check_c_google_idp_attribute_mapping() -> list[str]:
    print("\nCheck C: Google IdP attribute mapping (email, name, sub) …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    # Google identity provider defined
    has_google_idp = bool(
        re.search(
            r"cognito\.UserPoolIdentityProviderGoogle|"
            r"UserPoolIdentityProviderGoogle|"
            r"google.*provider|provider.*google|"
            r"IdentityProvider.*Google|Google.*IdentityProvider",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_google_idp,
        "Google IdP (UserPoolIdentityProviderGoogle) defined in auth-stack.ts",
        "Per AC: 'Google IdP attribute mapping: email, name, sub.'",
    )

    # Attribute mappings: email
    has_email_mapping = bool(
        re.search(r"email.*[Aa]ttribute|[Aa]ttribute.*email", auth_ts, re.IGNORECASE)
    )
    failures += _assert(
        has_email_mapping,
        "Google IdP attribute mapping includes 'email'",
        "Per AC: 'Google IdP attribute mapping: email, name, sub.'",
    )

    # Attribute mapping: name
    has_name_mapping = bool(
        re.search(r"\bname\b.*[Aa]ttribute|[Aa]ttribute.*\bname\b", auth_ts, re.IGNORECASE)
    )
    failures += _assert(
        has_name_mapping,
        "Google IdP attribute mapping includes 'name'",
        "Per AC: 'Google IdP attribute mapping: email, name, sub.'",
    )

    # Attribute mapping: sub (Google sub / subject)
    has_sub_mapping = bool(
        re.search(r"\bsub\b.*[Aa]ttribute|[Aa]ttribute.*\bsub\b", auth_ts, re.IGNORECASE)
    )
    failures += _assert(
        has_sub_mapping,
        "Google IdP attribute mapping includes 'sub'",
        "Per AC: 'Google IdP attribute mapping: email, name, sub.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D — User pool client with callback and logout URLs
# ---------------------------------------------------------------------------

def check_d_app_client_urls() -> list[str]:
    print("\nCheck D: User pool client with callback and logout URLs …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    has_app_client = bool(
        re.search(
            r"UserPoolClient|userPoolClient|addClient|"
            r"cognito\.UserPoolClient",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_app_client,
        "UserPoolClient defined in auth-stack.ts",
        "Per AC: 'User pool client configured with appropriate callback and logout URLs'.",
    )

    # Callback URLs
    has_callback_urls = bool(
        re.search(
            r"callbackUrl|callback_url|oAuthCallbackUrl|"
            r"callbackUrls|redirectSignIn",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_callback_urls,
        "Callback URLs configured on user pool client",
        "Per AC: 'User pool client configured with appropriate callback and logout URLs "
        "(Amplify URL + localhost for dev)'.",
    )

    # Logout URLs
    has_logout_urls = bool(
        re.search(
            r"logoutUrl|logout_url|signOutUrl|logoutUrls|redirectSignOut",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_logout_urls,
        "Logout URLs configured on user pool client",
        "Per AC: 'User pool client configured with appropriate callback and logout URLs'.",
    )

    # localhost for dev
    has_localhost = bool(re.search(r"localhost", auth_ts, re.IGNORECASE))
    failures += _assert(
        has_localhost,
        "localhost URL included (for dev)",
        "Per AC: '(Amplify URL + localhost for dev)'.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E — Hosted UI enabled with a domain prefix
# ---------------------------------------------------------------------------

def check_e_hosted_ui_domain() -> list[str]:
    print("\nCheck E: Hosted UI enabled with a domain prefix …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    has_domain = bool(
        re.search(
            r"addDomain|UserPoolDomain|userPoolDomain|"
            r"cognito\.UserPoolDomain|"
            r"domainPrefix|domain_prefix|hostedZone.*cognito|cognito.*domain",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_domain,
        "Hosted UI domain prefix defined in auth-stack.ts",
        "Per AC: 'Hosted UI enabled with a domain prefix.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check F — App client does NOT allow user-password sign-in
# ---------------------------------------------------------------------------

def check_f_no_user_password_auth() -> list[str]:
    print("\nCheck F: App client does NOT allow user-password sign-in (Google IdP only) …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    # Must NOT enable USER_PASSWORD_AUTH (or ALLOW_USER_PASSWORD_AUTH).
    # Note: `userPassword: false` or `userSrp: false` in the authFlows object is
    # acceptable (explicitly disabling); we only fail if it is set to `true`.
    enables_user_password_auth = bool(
        re.search(
            r"userPassword\s*:\s*true|"
            r"USER_PASSWORD_AUTH\s*(?:=|:)\s*true|"
            r"ALLOW_USER_PASSWORD_AUTH|"
            r"userSrp\s*:\s*true",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        not enables_user_password_auth,
        "App client does NOT enable USER_PASSWORD_AUTH (Google IdP only)",
        "Per AC: 'App client does not allow user-password sign-in. Google IdP only.' "
        "Set userPassword: false and userSrp: false in authFlows, or omit authFlows entirely.",
    )

    # Must specify only ALLOW_REFRESH_TOKEN_AUTH or code flow (no direct auth flows)
    # Alternatively, just verify the authFlows block omits password flows
    # We'll check that the identity providers are set to Google only
    has_google_only_idp = bool(
        re.search(
            r"supportedIdentityProviders.*[Gg]oogle|"
            r"identityProviders.*[Gg]oogle|"
            r"[Gg]oogle.*supportedIdentityProviders|"
            r"UserPoolClientIdentityProvider\.GOOGLE|"
            r"GOOGLE",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_google_only_idp,
        "UserPoolClient configured with Google as identity provider",
        "Per AC: 'Google IdP only.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G — Hosted-domain enforcement in TWO layers
#
# Layer (a) is asserted against the SYNTHESIZED CloudFormation template
# (cdk.out/), not against source text.  This is required because comment
# strings in the source can satisfy a regex check without any real wired
# behavior.  The gate must fail if hd=teamexos.com is absent from the
# ProviderDetails in the synthesized UserPoolIdentityProvider resource.
# ---------------------------------------------------------------------------

def check_g_two_layer_domain_enforcement() -> list[str]:
    print("\nCheck G: Hosted-domain enforcement in TWO layers (hd= + Lambda) …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    # --- Layer (a): assert hd=teamexos.com in the SYNTHESIZED template ---
    # We do NOT use a source-text regex here; only the synthesized template
    # proves that the wired behavior (not just a comment) is present.
    #
    # We use the module-level cache populated by check_m_cdk_synth() (which runs
    # BEFORE check_g in main()).  check_m synthesizes into a fresh temp directory
    # so that pre-existing stale cdk.out artifacts cannot cause false-passes or
    # false-failures — the result is deterministic regardless of the checkout state.
    #
    # If the cache is empty (i.e. check_m was not run first or synth failed), we
    # FAIL rather than WARN.  The old WARN fallback was removed because a source-text
    # regex (which a comment satisfies) is an insufficient gate for a security
    # invariant (hd= wired in the actual CloudFormation template).
    template = _synth_template_cache
    if template is None:
        failures += _assert(
            False,
            f"hd={NEUTRAL_HOSTED_DOMAIN} present in synthesized ProviderDetails (layer a — wired behavior)",
            "No synthesized template available for Check G.\n"
            "         check_m_cdk_synth() must run BEFORE check_g_two_layer_domain_enforcement()\n"
            "         and must succeed so that _synth_template_cache is populated.\n"
            "         The WARN fallback has been removed: a source-text regex is insufficient\n"
            "         because a comment can satisfy it without any wired behavior.\n"
            "         Per AC layer (a): 'the Google OAuth request pins hd={hostedDomain}'.\n"
            "         Use cfnIdp.addPropertyOverride('ProviderDetails.hd', hostedDomain)\n"
            "         to inject hd into the synthesized ProviderDetails.",
        )
    else:
        idp_resource = _find_google_idp_resource(template)
        if idp_resource is None:
            failures += _assert(
                False,
                "AWS::Cognito::UserPoolIdentityProvider resource found in synthesized auth template",
                "No Google IdP resource found in the synthesized nested auth stack template.\n"
                "         Ensure the AuthStack creates a UserPoolIdentityProviderGoogle construct.",
            )
        else:
            provider_details = idp_resource.get("Properties", {}).get("ProviderDetails", {})
            hd_value = provider_details.get("hd", "")
            failures += _assert(
                hd_value == NEUTRAL_HOSTED_DOMAIN,
                f"hd={NEUTRAL_HOSTED_DOMAIN} present in synthesized ProviderDetails (layer a — wired behavior; "
                "issue #349: hostedDomain is required CDK context, no internal tenant-domain default)",
                f"Per AC: 'the Google OAuth request pins hd={{hostedDomain}} (layer a)'.\n"
                f"         Synthesized ProviderDetails.hd={hd_value!r} — expected {NEUTRAL_HOSTED_DOMAIN!r}\n"
                f"         (from NEUTRAL_CDK_CONTEXT's --context hostedDomain={NEUTRAL_HOSTED_DOMAIN}).\n"
                f"         Source comments are insufficient; the hd field must appear in the\n"
                f"         synthesized CloudFormation template.  Use the CDK escape hatch:\n"
                f"         const cfnIdp = googleIdp.node.findChild('Resource');\n"
                f"         (cfnIdp as cdk.CfnResource).addPropertyOverride('ProviderDetails.hd', hostedDomain);",
            )

    # --- Layer (b): pre-sign-up or pre-token-generation Lambda (source check) ---
    has_lambda_trigger = bool(
        re.search(
            r"preSignUp|pre_sign_up|preTokenGeneration|pre_token_generation|"
            r"Lambda.*trigger|trigger.*Lambda|"
            r"lambdaTriggers|lambda_triggers|"
            r"cognito\.UserPoolOperation",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_lambda_trigger,
        "Pre-sign-up or pre-token-generation Lambda trigger defined (layer b of two-layer enforcement)",
        "Per AC: 'a Cognito pre-sign-up / pre-token-generation Lambda rejects any "
        "non-@teamexos.com verified email'.",
    )

    # Lambda must be defined or imported
    has_lambda_construct = bool(
        re.search(
            r"new\s+lambda\.Function|lambda\.Function\.fromFunctionArn|"
            r"aws-cdk-lib/aws-lambda|NodejsFunction|PythonFunction",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_lambda_construct,
        "Lambda Function construct defined in auth-stack.ts",
        "Per AC: the pre-sign-up/pre-token Lambda must be defined as a CDK Lambda construct.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H — Pre-token Lambda: group check, JIT-create row, fail-closed
# ---------------------------------------------------------------------------

def check_h_pre_token_lambda_behavior() -> list[str]:
    print("\nCheck H: Pre-token Lambda behavior (group check + JIT-create + fail-closed) …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    # Group membership check
    has_group_check = bool(
        re.search(
            r"legal.admin@teamexos\.com|"
            r"legal.admin.*group|group.*legal.admin|"
            r"directory.*api|Directory.*API|"
            r"group.*membership|membership.*group",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_group_check,
        "Pre-token Lambda references legal-admin@teamexos.com group check / Directory API",
        "Per AC: 'the pre-token Lambda checks legal-admin@teamexos.com group membership "
        "via the Directory API'.",
    )

    # JIT-creates active users row
    has_jit_create = bool(
        re.search(
            r"JIT.creat|jit.creat|"
            r"active.*users.*row|users.*row.*active|"
            r"first.*sign.in.*creat|creat.*first.*sign.in|"
            r"on.*sign.in.*creat|creat.*on.*sign.in",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_jit_create,
        "Pre-token Lambda documented to JIT-create active users row on first sign-in",
        "Per AC (and reconciliation): 'the pre-token Lambda checks group membership and "
        "JIT-creates an active users row in DynamoDB on first sign-in'.",
    )

    # Fail-closed behavior documented
    has_fail_closed = bool(
        re.search(
            r"fail.closed|failClosed|fail_closed|"
            r"deny.*error|error.*deny|"
            r"directory.*unavailable.*deny|deny.*directory.*unavailable|"
            r"directory.*unreachable.*deny|deny.*directory.*unreachable",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_fail_closed,
        "Pre-token Lambda documented as fail-closed (deny on Directory API error)",
        "Per AC: 'fail closed when the Directory API is unreachable (deny; never fail open)'.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check I — Directory API service account credentials in Secrets Manager
# ---------------------------------------------------------------------------

def check_i_directory_api_credentials() -> list[str]:
    print("\nCheck I: Directory API service account credentials in Secrets Manager …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    has_directory_creds = bool(
        re.search(
            r"directory.*secret|secret.*directory|"
            r"contract-toaster/cognito/directory|directory.api.*secret|"
            r"google.*directory.*api|Directory.*API.*secret",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_directory_creds,
        "Directory API service account credentials stored in Secrets Manager",
        "Per AC: 'Google Directory API service account credentials live in Secrets Manager "
        "and are rotated'.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check J — Break-glass IAM role intentionally absent for v1 (issue #229)
# ---------------------------------------------------------------------------

def check_j_break_glass_role() -> list[str]:
    print(
        "\nCheck J: Break-glass IAM role intentionally ABSENT for v1 (issue #229); "
        "documented as SSO AdministratorAccess permission set …"
    )
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    # Per issue #229: the dedicated CDK-managed break-glass IAM role was
    # removed because its trust policy used a managed-policy ARN
    # ('arn:aws:iam::aws:policy/AdministratorAccess') as a FederatedPrincipal
    # — an invalid IAM principal that would have failed CreateRole at deploy
    # time. v1 does not wire ANY dedicated role/principal for break-glass.
    # Assert there is no `new iam.Role(...'BreakGlassRole'...)` construct in
    # the source. A regex that merely matches the words "break-glass" / "MFA"
    # / "SSO" would also match the explanatory comment block describing the
    # removal (and the unrelated KMS 'BreakGlassMfa' SID) — that is NOT
    # evidence of a real resource, so it is deliberately not used here.
    has_break_glass_role_construct = bool(
        re.search(r"new\s+iam\.Role\([^;]*?BreakGlassRole", auth_ts, re.DOTALL)
    )
    failures += _assert(
        not has_break_glass_role_construct,
        "No dedicated CDK-managed BreakGlassRole IAM role construct in auth-stack.ts "
        "source (issue #229: removed — invalid managed-policy-ARN-as-FederatedPrincipal)",
        "v1 does not wire a dedicated break-glass role/principal at all. If a "
        "'new iam.Role(this, \\'BreakGlassRole\\', ...)' construct has been (re)added, it "
        "must use a valid Federated/SAML/service principal — see "
        "tests/test_infra_break_glass_principal_229.py.",
    )

    # Confirm structurally, via the template synthesized by Check M, that no
    # BreakGlassRole resource actually exists — mirrors the slice test in
    # tests/test_infra_break_glass_principal_229.py rather than trusting
    # source-comment prose.
    template = _synth_template_cache
    if template is None:
        failures += _assert(
            False,
            "Synthesized AuthStack template available to confirm BreakGlassRole absence",
            "No _synth_template_cache — Check M (cdk synth) must run before Check J and "
            "must succeed.",
        )
    else:
        break_glass_resources = [
            logical_id
            for logical_id, resource in template.get("Resources", {}).items()
            if resource.get("Type") == "AWS::IAM::Role"
            and (
                "BreakGlass" in logical_id
                or "break-glass" in str(resource.get("Properties", {}).get("RoleName", ""))
            )
        ]
        failures += _assert(
            not break_glass_resources,
            "No BreakGlassRole AWS::IAM::Role resource in the synthesized AuthStack template",
            f"Found unexpected BreakGlassRole resource(s): {break_glass_resources!r}. v1 "
            "intentionally drops the dedicated role (issue #229) in favor of the documented "
            "SSO AdministratorAccess permission set.",
        )

    # RUNBOOK.md must document break-glass as the v1 reality — the SSO
    # AdministratorAccess permission set already granted for this account —
    # and must NOT claim a dedicated break-glass IAM role exists.
    if not RUNBOOK_PATH.exists():
        return failures + _assert(False, "RUNBOOK.md exists (prerequisite)")

    runbook = _read(RUNBOOK_PATH)

    documents_sso_admin_access = bool(
        re.search(
            r"AdministratorAccess.{0,80}SSO permission set|"
            r"SSO permission set.{0,80}AdministratorAccess",
            runbook,
            re.IGNORECASE | re.DOTALL,
        )
    )
    failures += _assert(
        documents_sso_admin_access,
        "RUNBOOK.md documents break-glass as the SSO AdministratorAccess permission set "
        "(v1 reality per issue #229), not a dedicated IAM role",
        "Emergency admin recovery = the AdministratorAccess SSO permission set already "
        "granted for this account (see aws-access-request.md), MFA-enforced by the SSO "
        "identity provider — not a dedicated CDK-managed IAM role.",
    )

    claims_dedicated_role_exists = bool(
        re.search(
            r"dedicated\s+\*{0,2}break-glass IAM role\*{0,2}\s+exists",
            runbook,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        not claims_dedicated_role_exists,
        "RUNBOOK.md does not claim a dedicated break-glass IAM role 'exists' "
        "(stale claim contradicted by issue #229's removal)",
        "Found language asserting a dedicated break-glass IAM role exists; this must "
        "describe the v1 SSO AdministratorAccess permission-set path instead.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check K — admin_bootstrap CDK seed row
# ---------------------------------------------------------------------------

def check_k_admin_bootstrap_seed() -> list[str]:
    print("\nCheck K: admin_bootstrap seed row (email-keyed, CDK writes) …")
    failures: list[str] = []

    if not AUTH_STACK_PATH.is_file():
        return _assert(False, "auth-stack.ts exists (prerequisite)")

    auth_ts = _read(AUTH_STACK_PATH)

    has_bootstrap_ref = bool(
        re.search(
            r"admin.bootstrap|adminBootstrap|admin_bootstrap",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_bootstrap_ref,
        "admin_bootstrap table referenced in auth-stack.ts for first-admin seed",
        "Per AC: 'CDK writes a single row into the admin_bootstrap table keyed by the "
        "configured GC email'.",
    )

    # First-admin bootstrap should reference email (not sub)
    has_email_seed = bool(
        re.search(
            r"adminEmail|admin_email|gc.*email|email.*gc|"
            r"firstAdmin.*email|email.*firstAdmin|"
            r"bootstrap.*email|email.*bootstrap",
            auth_ts,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_email_seed,
        "admin_bootstrap seed uses email key (not cognito_sub)",
        "Per AC: 'CDK writes a single row … keyed by the configured GC email "
        "(not into the cognito_sub-keyed users table)'.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check L — RUNBOOK.md documents break-glass procedure
# ---------------------------------------------------------------------------

def check_l_runbook_break_glass() -> list[str]:
    print("\nCheck L: RUNBOOK.md documents the break-glass procedure …")
    failures: list[str] = []

    if not RUNBOOK_PATH.exists():
        return _assert(False, "RUNBOOK.md exists", str(RUNBOOK_PATH))

    runbook = _read(RUNBOOK_PATH)

    has_break_glass_procedure = bool(
        re.search(
            r"break.glass|break_glass|breakGlass|emergency.admin",
            runbook,
            re.IGNORECASE,
        )
    )
    failures += _assert(
        has_break_glass_procedure,
        "RUNBOOK.md references break-glass procedure",
        "Per AC: 'Procedure documented in RUNBOOK.md.'",
    )

    return failures


# ---------------------------------------------------------------------------
# Check M — cdk synth runs cleanly with auth resources present
# ---------------------------------------------------------------------------

def check_m_cdk_synth() -> list[str]:
    """
    Run cdk synth into a FRESH temporary directory so that:
      (a) Pre-existing stale cdk.out artifacts cannot cause false-passes or
          false-failures in Check G (finding 5: gate was non-deterministic with
          respect to a pre-existing cdk.out from a RED-commit synth).
      (b) The synthesized template is loaded into _synth_template_cache so that
          check_g_two_layer_domain_enforcement() can assert against it without
          depending on the on-disk cdk.out path (finding 4: check_g ran before
          check_m and hit the WARN fallback when cdk.out was absent).
    """
    global _synth_template_cache
    print("\nCheck M: cdk synth runs cleanly with auth resources …")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite)", "")

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

    # Synthesize into a fresh temp directory to guarantee determinism.
    # This avoids stale cdk.out artifacts from a previous (possibly RED-commit)
    # synth run causing check_g to see the wrong template.
    with tempfile.TemporaryDirectory(prefix="contract-toaster-gate-cdk-out-") as tmp_out:
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
            "cdk synth --context env=dev exits 0 (with Cognito auth resources)",
            f"stdout (last 800 chars): {result.stdout[-800:]}\n"
            f"stderr (last 800 chars): {result.stderr[-800:]}",
        )

        if result.returncode == 0:
            # Populate the module-level cache so check_g can use it.
            # We read the template files while tmp_out still exists (within this
            # with-block).  The cache holds the parsed dict in memory.
            _synth_template_cache = _load_synthesized_auth_template(Path(tmp_out))
            if _synth_template_cache is None:
                failures += _assert(
                    False,
                    "Synthesized auth nested-stack template found in fresh cdk.out",
                    f"cdk synth exited 0 but no contracttoaster*Auth*.nested.template.json\n"
                    f"         was found in the output directory: {tmp_out}\n"
                    f"         Check that the AuthStack nested stack is synthesized as\n"
                    f"         a separate nested template (NestedStack, not inline).",
                )
        # tmp_out is cleaned up here; _synth_template_cache holds the data in memory.

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("AuthStack structural gate (issue #53)")
    print("=" * 60)

    all_failures: list[str] = []
    all_failures += check_a_user_pool()
    all_failures += check_b_secrets_manager_dynamic_ref()
    all_failures += check_c_google_idp_attribute_mapping()
    all_failures += check_d_app_client_urls()
    all_failures += check_e_hosted_ui_domain()
    all_failures += check_f_no_user_password_auth()
    # Check M (synth) MUST run before Check G so that _synth_template_cache is
    # populated and Check G can assert against the freshly synthesized template.
    # Running synth first also makes the gate deterministic: it synthesizes into
    # a fresh temp directory, so a pre-existing stale cdk.out cannot affect the
    # result (finding 4 and 5 from the issue #53 fix round 2 review).
    all_failures += check_m_cdk_synth()
    all_failures += check_g_two_layer_domain_enforcement()
    all_failures += check_h_pre_token_lambda_behavior()
    all_failures += check_i_directory_api_credentials()
    all_failures += check_j_break_glass_role()
    all_failures += check_k_admin_bootstrap_seed()
    all_failures += check_l_runbook_break_glass()

    print("\n" + "=" * 60)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all AuthStack structural checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
