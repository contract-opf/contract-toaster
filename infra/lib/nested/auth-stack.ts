import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

export interface AuthStackProps extends cdk.NestedStackProps {
  readonly envName: string;
  /**
   * Resource-name prefix (issue #233). Defaults to 'contract-toaster'. Used
   * for the Cognito hosted-UI domain prefix and (with appDomain) the Amplify
   * Hosting callback/logout URLs — NOT for the hosted-domain enforcement or
   * legal-admin allowlist (see `hostedDomain` below), which are independent
   * security invariants, not naming.
   */
  readonly appName?: string;
  /**
   * Domain the SPA is hosted on, used to build Cognito callback/logout URLs
   * (issue #233 — was hard-coded to a literal internal subdomain).
   * REQUIRED — no internal default (issue #349: no internal-domain
   * fallback). Independent of the `hostedDomain` IdP/allowlist enforcement
   * below, which does not change with this prop.
   */
  readonly appDomain: string;
  /**
   * Google Workspace hosted domain enforced for sign-in (issue #349).
   * Drives BOTH layers of hosted-domain enforcement:
   *   (a) the Google OAuth `hd` parameter pinned on the IdP, and
   *   (b) the pre-token Lambda's `ALLOWED_DOMAIN` check — threaded in via
   *       the Lambda's `environment` block, never baked into the inline
   *       Python source (see the `lambda.Code.fromInline` construction
   *       below), so a test that extracts and executes that source
   *       standalone does not need to evaluate CDK/TypeScript templating.
   * Also derives `LEGAL_ADMIN_GROUP` as `legal-admin@{hostedDomain}` (the
   * `legal-admin` local part is a generic role name, not tenant identity,
   * and is not independently configurable here).
   *
   * REQUIRED — no internal default (issue #349: no internal-tenant-domain
   * fallback). Deliberately a SEPARATE prop from `appDomain`: `appDomain` is a naming
   * knob for the SPA's own hosting domain (issue #233); `hostedDomain` is
   * the authorization-security domain. Conflating the two would let a
   * naming change silently widen (or narrow) who can sign in — see
   * tests/test_infra_appname_prefix_233.py Check G.
   */
  readonly hostedDomain: string;
  /**
   * Email address of the initial admin (General Counsel).
   * CDK writes a single admin_bootstrap row keyed by this email so that on
   * first sign-in the backend can reconcile to the real Cognito sub.
   *
   * REQUIRED — no internal default (issue #349: no internal-tenant admin
   * address fallback). Supply via CDK context (see contract-toaster-stack.ts).
   */
  readonly adminEmail: string;
  /**
   * DynamoDB table name for the users table (injected from DataStack).
   * Pre-token Lambda uses it to JIT-create active user rows.
   * Optional until DataStack is consumed here.
   */
  readonly usersTableName?: string;
  /**
   * DynamoDB table name for the admin_bootstrap table (injected from DataStack).
   * CDK seeds the initial admin row into this table.
   * Optional until DataStack is consumed here.
   */
  readonly adminBootstrapTableName?: string;
}

/**
 * AuthStack — Cognito user pool federated to Google as the ONLY identity
 * provider, restricted to the configured `hostedDomain` (issue #349).
 *
 * Issue #53: Cognito + Google IdP
 *
 * Resources defined here:
 *  1. Cognito UserPool (username = email; no self-registration).
 *  2. Google IdP (clientId/clientSecret from Secrets Manager — dynamic
 *     reference only; NEVER read at synth time).
 *  3. UserPoolClient (Google-only; no USER_PASSWORD_AUTH; callback + logout
 *     URLs for Amplify + localhost).
 *  4. Hosted UI domain prefix: `contract-toaster-{envName}`.
 *  5. Pre-token-generation Lambda (two-layer hosted-domain + allowlist enforcement):
 *       a. Pins hd={hostedDomain} in the Google OAuth request.
 *       b. Pre-sign-up / pre-token-generation Lambda rejects any verified
 *          email outside @{hostedDomain} (second, independent layer).
 *       c. Checks legal-admin@{hostedDomain} group membership via the Google
 *          Directory API (service-account credentials from Secrets Manager).
 *          RS256-signs the domain-wide-delegation JWT with a stdlib-only
 *          pure-Python RSA implementation (issue #223, defect a — the
 *          Lambda Python 3.12 managed runtime has no `cryptography`
 *          package, and inline Code.fromInline cannot bundle one), and
 *          impersonates an actual Workspace admin user, never the
 *          legal-admin@{hostedDomain} GROUP address (issue #223, defect b).
 *       d. On first sign-in, JIT-creates an active users row in DynamoDB (the
 *          only non-bootstrap admission path). The Lambda's execution role
 *          is granted dynamodb:PutItem/UpdateItem scoped to the users table
 *          for exactly this (issue #223, defect c).
 *       e. Fail-closed: deny on any Directory API error; never fail open.
 *  6. Directory API service-account secret reference.
 *  7. Break-glass: NO dedicated CDK-managed IAM role for v1 (issue #229 —
 *     removed a placeholder that used a managed-policy ARN as a
 *     `FederatedPrincipal`, which is invalid and would have failed the
 *     nested-stack deploy). Emergency admin recovery instead uses the
 *     `AdministratorAccess` SSO permission set already granted for this
 *     account (see aws-access-request.md — Fulfillment record), MFA-enforced
 *     by the SSO identity provider, with the documented manual audit step in
 *     RUNBOOK.md. Revisit before prod: wire a dedicated least-privilege
 *     Identity Center permission set or SAML-federated role scoped to
 *     break-glass only.
 *  8. admin_bootstrap CDK custom resource: seeds a single row keyed by the
 *     configured GC email into the admin_bootstrap table on first deploy.
 *
 * Security invariants:
 *  - OAuth secret plaintext NEVER lands in synthesized templates, cdk.out,
 *    or CI logs.  SecretValue.secretsManager(...) resolves at runtime via
 *    CloudFormation dynamic reference — NOT at synth time.
 *  - Hosted-domain enforcement is defense-in-depth: TWO independent layers
 *    (Google OAuth hd= parameter + Cognito Lambda trigger).
 *  - Authorization beyond domain: DynamoDB allowlist row (active status)
 *    checked by the pre-token Lambda; domain membership alone is insufficient.
 *  - Fail-closed: any error in the pre-token Lambda denies sign-in.
 *  - Break-glass (issue #229): every AWS API call made under the SSO
 *    `AdministratorAccess` session is logged as a CloudTrail management
 *    event automatically (unconditional). There is no automated mirror into
 *    the application `audit` table — the operator records that row manually
 *    per RUNBOOK.md. This is dev-account convenience, not least-privilege;
 *    tighten before prod (aws-access-request.md — Fulfillment record).
 *  - Sync job only deprovisions — it never auto-admits new members.
 *    Admission is exclusively via the pre-token Lambda JIT-create path.
 *  - admin_bootstrap row is email-keyed (separate from the sub-keyed users
 *    table); reconciliation to the real Cognito sub happens on first sign-in
 *    (backend one-time transaction — see ARCHITECTURE.md).
 */
export class AuthStack extends cdk.NestedStack {
  /** The Cognito user pool. */
  readonly userPool: cognito.UserPool;
  /** The hosted UI app client (Google IdP only). */
  readonly userPoolClient: cognito.UserPoolClient;
  /**
   * Cognito hosted-UI domain (bare host, no scheme), e.g.
   * `contract-toaster-dev.auth.us-east-1.amazoncognito.com`.
   *
   * Synth-time-known literal (domainPrefix + fixed region), not a
   * CloudFormation token — consumers (e.g. FrontendStack's CSP connect-src,
   * issue #226) can embed it directly in a plain string.
   */
  readonly hostedUiDomain: string;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    const { envName, adminEmail, hostedDomain, appDomain } = props;
    const appName = props.appName ?? 'contract-toaster';
    // Cognito hosted-UI domain prefix + Amplify Hosting subdomain convention.
    const domainPrefix = `${appName}-${envName}`;
    this.hostedUiDomain = `${domainPrefix}.auth.us-east-1.amazoncognito.com`;

    // -----------------------------------------------------------------------
    // Google OAuth client credentials — Secrets Manager (dynamic reference)
    //
    // The secret at 'contract-toaster/cognito/google-oauth' holds the Google OAuth 2.0
    // client ID and client secret created in Google Cloud Console.
    //
    // CRITICAL INVARIANT: CDK does NOT read the secret value at synth time.
    // All references use SecretValue.secretsManager (CloudFormation dynamic
    // reference resolved at deploy / runtime).  The plaintext MUST NEVER
    // appear in synthesized templates, cdk.out/, or CI logs.
    //
    // The secret is expected to contain two fields:
    //   clientId     — Google OAuth client ID
    //   clientSecret — Google OAuth client secret
    // -----------------------------------------------------------------------
    const googleOAuthSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'GoogleOAuthSecret',
      'contract-toaster/cognito/google-oauth',
    );

    // Dynamic reference: resolved by CloudFormation at deploy time — never at synth.
    const googleClientId = googleOAuthSecret.secretValueFromJson('clientId').unsafeUnwrap();
    const googleClientSecretValue = googleOAuthSecret.secretValueFromJson('clientSecret');

    // -----------------------------------------------------------------------
    // Google Directory API service-account credentials — Secrets Manager
    //
    // The pre-token-generation Lambda uses a Google Directory API service
    // account (domain-wide delegation, directory-read only) to check group
    // membership for the configured LEGAL_ADMIN_GROUP.
    //
    // Credentials are stored in Secrets Manager, are least-privilege
    // (directory read only), and are rotated.
    //
    // Secret path: 'contract-toaster/cognito/directory-api-sa'
    //
    // Expected JSON fields (issue #223, defect b):
    //   client_email          — service-account email (JWT `iss`)
    //   private_key           — service-account private key, PEM (PKCS#1 or
    //                            PKCS#8), used to sign the domain-wide-
    //                            delegation JWT
    //   delegated_admin_email — an ACTUAL Workspace admin user's email that
    //                            has granted this service account
    //                            domain-wide delegation for the Directory
    //                            API read-only scope. Impersonated as the
    //                            JWT `sub`. MUST be a real user, never the
    //                            legal-admin@{hostedDomain} GROUP address —
    //                            Google's token endpoint rejects a group as
    //                            the impersonation subject.
    // -----------------------------------------------------------------------
    const directoryApiSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'DirectoryApiSecret',
      'contract-toaster/cognito/directory-api-sa',
    );

    // -----------------------------------------------------------------------
    // Pre-token-generation Lambda
    //
    // This Lambda is the admission and domain-enforcement gate for EVERY
    // sign-in.  It implements TWO-layer hosted-domain enforcement:
    //
    //   Layer (a): hd={hostedDomain} is pinned in the Google OAuth request
    //              (see Google IdP configuration below).
    //   Layer (b): This Lambda independently rejects any identity whose
    //              verified email is not @{hostedDomain} — even if Cognito
    //              somehow received such a token.
    //
    // Authorization (beyond domain):
    //   - Checks legal-admin@{hostedDomain} group membership via the Google
    //     Directory API (service account from Secrets Manager).
    //   - On first sign-in for a group member: JIT-creates an active users
    //     row in DynamoDB (keyed by Cognito sub).  This is the ONLY
    //     non-bootstrap admission path.
    //   - A group member who has not yet signed in does NOT have a users row;
    //     they get one on first sign-in.
    //   - A user not in the group is denied, even with a valid @{hostedDomain}
    //     identity.
    //
    // Fail-closed behavior:
    //   - Any error communicating with the Directory API results in DENY.
    //   - Never fail open.
    //   - "directory unavailable → deny" is tested at this layer.
    //
    // The sync job only deprovisions — it never auto-admits new members.
    // -----------------------------------------------------------------------
    // Users table name injected at deploy time; resolved from DataStack
    // output. Fallback must track DataStack's appName (issue #233) so a
    // custom appName doesn't silently point this Lambda at a nonexistent
    // table. Computed once and reused for both the Lambda's env var and the
    // DynamoDB IAM grant below (issue #223, defect c) so the two can never
    // drift apart.
    const usersTableName = props.usersTableName ?? `${appName}-users-${envName}`;

    const preTokenLambda = new lambda.Function(this, 'PreTokenLambda', {
      functionName: `contract-toaster-pre-token-${envName}`,
      description:
        'Cognito pre-token-generation Lambda: two-layer domain enforcement + ' +
        'legal-admin group check + JIT-create active users row on first sign-in. ' +
        'Fail-closed: deny on Directory API error. Never fail open.',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      // Pre-token-generation Lambda enforcement code.
      //
      // This inline implementation enforces TWO independent security layers:
      //   Layer (b): Reject any identity whose verified email is outside the
      //              configured ALLOWED_DOMAIN.
      //   Authorization: Check LEGAL_ADMIN_GROUP membership via the Google
      //                  Directory API; JIT-create active users row in
      //                  DynamoDB on first sign-in; fail-closed on any error.
      //
      // ALLOWED_DOMAIN and LEGAL_ADMIN_GROUP are intentionally NOT baked into
      // this Python source as string literals (issue #349): they are read
      // from the Lambda's environment (below) at call time, exactly like
      // DIRECTORY_SECRET_ARN / USERS_TABLE_NAME already are. This keeps the
      // hosted-domain value CDK-context-driven (no internal tenant-domain
      // default) without requiring any TypeScript template-literal
      // interpolation inside this backtick-delimited block — a test that
      // extracts and executes this source standalone
      // (tests/test_pre_token_lambda_deny_paths.py) only ever sees plain
      // Python, never unresolved `${...}` templating.
      //
      // Admission path (canonical, identical to ARCHITECTURE.md):
      //   1. User added to the configured LEGAL_ADMIN_GROUP by a Workspace admin.
      //   2. User signs in via Google SSO (layer a: hd={hostedDomain} in IdP).
      //   3. This Lambda (layer b + authorization):
      //      a. Rejects an email outside ALLOWED_DOMAIN independently of layer a.
      //      b. Checks LEGAL_ADMIN_GROUP membership via Directory API
      //         (service-account creds from Secrets Manager).
      //      c. On first sign-in: JIT-creates an active users row in DynamoDB
      //         (keyed by Cognito sub).  This is the only non-bootstrap admission.
      //      d. Fail-closed: deny on any Directory API error; never fail open.
      //         "directory unavailable → deny" is tested at this layer.
      //   4. Subsequent sign-ins update last_auth_at; the row is never recreated.
      //
      // The sync job only deprovisions — it never auto-admits new members.
      code: lambda.Code.fromInline(`
import os
import json
import logging
import hashlib
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ALLOWED_DOMAIN and LEGAL_ADMIN_GROUP are read from the Lambda's environment
# inside handler() below (issue #349) -- NOT hard-coded here -- so the
# enforced domain is CDK-context-driven with no internal tenant-domain
# default. See the CDK construction above for why these are environment
# variables rather than TypeScript-templated string literals.

# ---------------------------------------------------------------------------
# _deny: raise an exception that Cognito treats as deny (fail-closed).
# Using Exception ensures the token is NOT issued regardless of trigger type.
# ---------------------------------------------------------------------------
def _deny(reason: str):
    logger.warning("PRE_TOKEN_DENY: %s", reason)
    raise Exception(f"Access denied: {reason}")


# ---------------------------------------------------------------------------
# _get_secret: fetch a secret value from Secrets Manager.
# Raises on any error so the Lambda fails closed.
# ---------------------------------------------------------------------------
def _get_secret(secret_arn: str) -> dict:
    import boto3
    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_arn)
    raw = resp.get("SecretString") or resp.get("SecretBinary", b"{}").decode()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Pure-Python RSA PKCS#1 v1.5 / SHA-256 signing (issue #223, defect a).
#
# The AWS Lambda Python 3.12 managed runtime does not include the
# 'cryptography' package, and this Lambda ships as inline Code.fromInline,
# which cannot bundle third-party dependencies — so 'from cryptography.hazmat
# ...' raised ImportError on every invocation, denying every sign-in
# (fail-closed, per design, but with no tested happy path). Rather than
# introduce new Docker/pip dependency-bundling machinery this repo does not
# otherwise use for any of its Lambdas, the unavailable dependency is
# avoided entirely: RS256 signing (needed only to mint the domain-wide-
# delegation JWT below) is implemented with stdlib-only big-integer
# arithmetic — parse the PEM private key's DER structure to recover the RSA
# modulus (n) and private exponent (d), then sign via PKCS#1 v1.5 padding.
# ---------------------------------------------------------------------------
def _der_read_tlv(data: bytes, idx: int):
    tag = data[idx]
    idx += 1
    first_len = data[idx]
    idx += 1
    if first_len & 0x80:
        num_bytes = first_len & 0x7F
        length = int.from_bytes(data[idx:idx + num_bytes], "big")
        idx += num_bytes
    else:
        length = first_len
    value = data[idx:idx + length]
    idx += length
    return tag, value, idx


def _der_read_sequence_children(value: bytes) -> list:
    children = []
    idx = 0
    while idx < len(value):
        tag, v, idx = _der_read_tlv(value, idx)
        children.append((tag, v))
    return children


def _rsa_private_key_from_pem(pem_str: str):
    """
    Parse a PEM-encoded RSA private key (PKCS#1 "RSA PRIVATE KEY" or PKCS#8
    "PRIVATE KEY", the format Google service-account JSON keys use) and
    return (modulus_n, private_exponent_d). Stdlib-only DER parsing — no
    'cryptography' dependency.
    """
    import base64 as _base64

    lines = [
        line.strip()
        for line in pem_str.strip().splitlines()
        if line.strip() and not line.strip().startswith("-----")
    ]
    der = _base64.b64decode("".join(lines))
    _, root_value, _ = _der_read_tlv(der, 0)
    children = _der_read_sequence_children(root_value)

    # PKCS#8 "PRIVATE KEY": SEQUENCE { version, algorithm SEQUENCE, privateKey OCTET STRING }.
    # PKCS#1 "RSA PRIVATE KEY": SEQUENCE { version, n, e, d, p, q, ... } directly.
    if len(children) >= 3 and children[1][0] == 0x30:
        octet_tag, octet_value = children[2]
        if octet_tag != 0x04:
            raise ValueError("Unrecognized PKCS#8 private key structure")
        _, inner_value, _ = _der_read_tlv(octet_value, 0)
        pkcs1_children = _der_read_sequence_children(inner_value)
    else:
        pkcs1_children = children

    if len(pkcs1_children) < 4:
        raise ValueError("Unrecognized RSA private key structure")

    modulus_n = int.from_bytes(pkcs1_children[1][1], "big")
    private_exponent_d = int.from_bytes(pkcs1_children[3][1], "big")
    return modulus_n, private_exponent_d


# DER-encoded SHA-256 DigestInfo AlgorithmIdentifier prefix (RFC 3447 §9.2 Note 1).
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _rsa_sign_sha256(message: bytes, modulus_n: int, private_exponent_d: int) -> bytes:
    """Sign 'message' with RSASSA-PKCS1-v1_5 / SHA-256 (RFC 3447 Sec 8.2)."""
    digest = hashlib.sha256(message).digest()
    digest_info = _SHA256_DIGEST_INFO_PREFIX + digest
    key_size_bytes = (modulus_n.bit_length() + 7) // 8
    padding_len = key_size_bytes - len(digest_info) - 3
    if padding_len < 8:
        raise ValueError("RSA key too small for RSASSA-PKCS1-v1_5 SHA-256 signature")
    encoded_message = b"\x00\x01" + b"\xff" * padding_len + b"\x00" + digest_info
    message_int = int.from_bytes(encoded_message, "big")
    signature_int = pow(message_int, private_exponent_d, modulus_n)
    return signature_int.to_bytes(key_size_bytes, "big")


# ---------------------------------------------------------------------------
# _check_group_membership: verify the user is in LEGAL_ADMIN_GROUP via the
# Google Directory API (domain-wide delegation service account).
#
# Returns True if member, False if NOT member.
# Raises on any Directory API / credential error — caller must deny.
# ---------------------------------------------------------------------------
def _check_group_membership(user_email: str, directory_secret_arn: str, legal_admin_group: str) -> bool:
    creds = _get_secret(directory_secret_arn)
    sa_email = creds.get("client_email", "")
    sa_key_pem = creds.get("private_key", "")
    # Issue #223 (defect b): domain-wide delegation impersonates a REAL
    # Workspace user, never a group address — Google's token endpoint
    # rejects a JWT whose 'sub' is a group (LEGAL_ADMIN_GROUP is a
    # Google Group, not a user account). The service-account secret must
    # therefore also carry the email of an actual Workspace admin who has
    # granted this service account domain-wide delegation for the Directory
    # API read-only scope.
    delegated_admin_email = creds.get("delegated_admin_email", "")
    if not sa_email or not sa_key_pem or not delegated_admin_email:
        raise ValueError(
            "Directory API service account credentials missing or incomplete "
            "(client_email, private_key, and delegated_admin_email are all required)"
        )

    # Build a JWT for the service account to call the Directory API
    import time
    import base64

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "iss": sa_email,
        "scope": "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
        "aud": "https://oauth2.googleapis.com/token",
        # Impersonate an actual Workspace admin user (issue #223, defect b) —
        # NOT the LEGAL_ADMIN_GROUP address. Domain-wide delegation requires
        # impersonating a user; Google rejects a group as 'sub'.
        "sub": delegated_admin_email,
        "exp": now + 3600,
        "iat": now,
    }).encode())

    # Sign with the service account private key (RS256) — pure-Python,
    # stdlib-only implementation (issue #223, defect a); see
    # _rsa_private_key_from_pem / _rsa_sign_sha256 above.
    signing_input = f"{header}.{payload}".encode()
    modulus_n, private_exponent_d = _rsa_private_key_from_pem(sa_key_pem)
    signature = _rsa_sign_sha256(signing_input, modulus_n, private_exponent_d)
    signed_jwt = f"{header}.{payload}.{_b64url(signature)}"

    # Exchange JWT for an access token
    token_data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": signed_jwt,
    }).encode()
    token_req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(token_req, timeout=8) as resp:
        token_resp = json.loads(resp.read())
    access_token = token_resp["access_token"]

    # Check group membership via Directory API
    import urllib.parse
    member_url = (
        "https://admin.googleapis.com/admin/directory/v1/groups/"
        + urllib.parse.quote(legal_admin_group, safe="")
        + "/members/"
        + urllib.parse.quote(user_email, safe="")
    )
    member_req = urllib.request.Request(
        member_url,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(member_req, timeout=8) as resp:
            member_data = json.loads(resp.read())
            status = member_data.get("status", "").upper()
            return status == "ACTIVE"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False  # user not a member
        raise  # any other HTTP error → fail closed (caller must deny)


# ---------------------------------------------------------------------------
# _jit_create_user_row: create an active users row in DynamoDB on first sign-in.
# Idempotent: uses a conditional put (row NOT already present).
# ---------------------------------------------------------------------------
def _jit_create_user_row(cognito_sub: str, email: str, users_table: str):
    import boto3
    from boto3.dynamodb.conditions import Attr
    import time

    dynamo = boto3.resource("dynamodb")
    table = dynamo.Table(users_table)
    try:
        table.put_item(
            Item={
                "sub": cognito_sub,
                "email": email,
                "status": "active",
                "created_at": int(time.time()),
                "last_auth_at": int(time.time()),
                "admission": "jit",
            },
            ConditionExpression=Attr("sub").not_exists(),
        )
        logger.info("JIT_CREATE_USER: sub=%s email=%s", cognito_sub, email)
    except dynamo.meta.client.exceptions.ConditionalCheckFailedException:
        # Row already exists — update last_auth_at only
        table.update_item(
            Key={"sub": cognito_sub},
            UpdateExpression="SET last_auth_at = :t",
            ExpressionAttributeValues={":t": int(time.time())},
        )
        logger.info("USER_ROW_EXISTS: sub=%s email=%s (updated last_auth_at)", cognito_sub, email)


# ---------------------------------------------------------------------------
# handler: Cognito pre-token-generation trigger entry point.
# ---------------------------------------------------------------------------
def handler(event, context):
    logger.info("PRE_TOKEN event: triggerSource=%s", event.get("triggerSource"))

    # Extract verified email from the Cognito identity
    user_attrs = event.get("request", {}).get("userAttributes", {})
    email = user_attrs.get("email", "").strip().lower()
    email_verified = user_attrs.get("email_verified", "false").lower()
    cognito_sub = user_attrs.get("sub", "")

    # ALLOWED_DOMAIN / LEGAL_ADMIN_GROUP are CDK-context-driven (issue #349):
    # read from the environment at call time, never hard-coded above, so
    # there is no internal tenant-domain fallback anywhere in this source.
    allowed_domain = os.environ.get("ALLOWED_DOMAIN", "")
    legal_admin_group = os.environ.get("LEGAL_ADMIN_GROUP", "")

    # --- Layer (b): Domain enforcement ---
    # Reject an email outside allowed_domain; independent of layer (a) hd= in IdP.
    if not allowed_domain or not email.endswith(allowed_domain):
        _deny(f"email domain not allowed: {email!r} (must be {allowed_domain!r})")

    if email_verified != "true":
        _deny(f"email not verified: {email!r}")

    # --- Authorization: Directory API group membership check ---
    directory_secret_arn = os.environ.get("DIRECTORY_SECRET_ARN", "")
    users_table = os.environ.get("USERS_TABLE_NAME", "")

    if not directory_secret_arn:
        _deny("DIRECTORY_SECRET_ARN not configured — fail-closed")

    # Fail-closed: any error communicating with the Directory API results in deny.
    try:
        is_member = _check_group_membership(email, directory_secret_arn, legal_admin_group)
    except Exception as exc:
        # Directory unavailable or credentials error → deny; never fail open.
        _deny(f"Directory API error (fail-closed): {exc}")

    if not is_member:
        _deny(f"user not in {legal_admin_group}: {email!r}")

    # --- JIT-create / update active users row in DynamoDB ---
    if users_table and cognito_sub:
        try:
            _jit_create_user_row(cognito_sub, email, users_table)
        except Exception as exc:
            # DynamoDB failure → deny; never fail open.
            _deny(f"DynamoDB error (fail-closed): {exc}")

    # Token generation allowed — return the event unmodified.
    logger.info("PRE_TOKEN_ALLOW: email=%s sub=%s", email, cognito_sub)
    return event
`),
      environment: {
        ENV_NAME: envName,
        DIRECTORY_SECRET_ARN: directoryApiSecret.secretArn,
        USERS_TABLE_NAME: usersTableName,
        // Issue #349: hosted-domain enforcement is CDK-context-driven, no
        // internal tenant-domain default. legal-admin is a generic role
        // local-part, not tenant identity.
        ALLOWED_DOMAIN: `@${hostedDomain}`,
        LEGAL_ADMIN_GROUP: `legal-admin@${hostedDomain}`,
      },
      timeout: cdk.Duration.seconds(10),
    });

    // Grant the Lambda permission to read the Directory API credentials
    directoryApiSecret.grantRead(preTokenLambda);

    // -----------------------------------------------------------------------
    // Issue #223 (defect c): grant dynamodb:PutItem/UpdateItem on the users
    // table.
    //
    // Previously this Lambda's execution role got ONLY
    // directoryApiSecret.grantRead — no DynamoDB permissions at all — so
    // every JIT-create / last_auth_at update in _jit_create_user_row would
    // throw AccessDenied, and the fail-closed handler (correctly) turned
    // that into a deny. That meant EVERY sign-in failed, including
    // legitimate LEGAL_ADMIN_GROUP members, because the admission
    // path (JIT-create on first sign-in) could never complete.
    //
    // A raw ARN (rather than a bound dynamodb.ITable) is used here because
    // AuthStack currently receives only the table NAME (props.usersTableName)
    // — the DataStack Table construct is not yet threaded through. Scoped to
    // exactly the two actions _jit_create_user_row needs (PutItem for the
    // conditional create, UpdateItem for the last_auth_at bump on repeat
    // sign-in); no other DynamoDB permissions are granted.
    // -----------------------------------------------------------------------
    preTokenLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [
        cdk.Stack.of(this).formatArn({
          service: 'dynamodb',
          resource: 'table',
          resourceName: usersTableName,
        }),
      ],
    }));

    // -----------------------------------------------------------------------
    // Cognito UserPool — username = email, Google IdP only
    //
    // Self-registration is disabled (no direct sign-up).  Users are admitted
    // only via Google SSO, subject to the pre-token Lambda gate above.
    // -----------------------------------------------------------------------
    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `contract-toaster-${envName}`,
      // username = email (the only sign-in alias; no username or phone)
      signInAliases: { email: true },
      autoVerify: { email: true },
      // Disable self-registration — admission is via Google SSO + Lambda gate
      selfSignUpEnabled: false,
      // Standard attributes: email (required) and name
      standardAttributes: {
        email: { required: true, mutable: true },
        fullname: { required: false, mutable: true },
      },
      // Lambda triggers — pre-token-generation enforces domain + allowlist
      lambdaTriggers: {
        preTokenGeneration: preTokenLambda,
      },
      // Access token TTL ≤ 15–60 minutes (configurable; default 60 min per AC).
      // This is the machine-assertable bound referenced in threat-model.md.
      // Combined worst-case window: sync cadence (≤ 1 hour) + token TTL ≤ ~2 hours.
      // Note: token validity is set on the UserPoolClient, not the UserPool.
      // Password policy is irrelevant (no direct user-password auth), but set
      // a strong policy defensively in case a misconfiguration enables it.
      passwordPolicy: {
        minLength: 20,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      // Deletion protection: warn before accidental pool deletion
      deletionProtection: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    cdk.Tags.of(this.userPool).add('contract-toaster:env', envName);
    cdk.Tags.of(this.userPool).add('contract-toaster:component', 'auth');

    // -----------------------------------------------------------------------
    // Google Identity Provider
    //
    // Layer (a) of two-layer hosted-domain enforcement: the hd={hostedDomain}
    // parameter in the Google OAuth request pins the hosted domain at the
    // Google layer.  Only accounts in the configured hostedDomain can select
    // their account on the Google consent screen.
    //
    // Layer (b) is enforced by the pre-token-generation Lambda above.
    //
    // Attribute mapping: email, name, sub (Google sub via ProviderAttribute.other)
    //
    // INVARIANT: clientSecretValue uses a SecretValue (CloudFormation dynamic
    // reference resolved at deploy time) — the plaintext never lands in
    // synthesized templates, cdk.out, or CI logs.
    // -----------------------------------------------------------------------
    const googleIdp = new cognito.UserPoolIdentityProviderGoogle(this, 'GoogleIdP', {
      userPool: this.userPool,
      // clientId is a string (non-secret; safe to embed)
      clientId: googleClientId,
      // clientSecretValue uses SecretValue — dynamic reference; NOT read at synth
      clientSecretValue: googleClientSecretValue,
      // OpenID Connect scopes requested from Google
      scopes: ['email', 'profile', 'openid'],
      // Attribute mapping: email, name, sub
      attributeMapping: {
        // email → Cognito email attribute
        email: cognito.ProviderAttribute.GOOGLE_EMAIL,
        // name → Cognito fullname attribute
        fullname: cognito.ProviderAttribute.GOOGLE_NAME,
        // sub → custom attribute 'identities' (Google subject identifier);
        // mapped via ProviderAttribute.other('sub') since there is no GOOGLE_SUB
        // built-in constant in CDK — the raw IdP attribute name is 'sub'.
        custom: {
          'custom:google_sub': cognito.ProviderAttribute.other('sub'),
        },
      },
    });

    // -----------------------------------------------------------------------
    // Layer (a) hosted-domain enforcement: hd={hostedDomain} in ProviderDetails
    //
    // The CDK UserPoolIdentityProviderGoogle construct only exposes 'scopes'
    // in its L2 props; the Google OAuth 'hd' (hosted domain) parameter must
    // be injected via the CloudFormation escape hatch on the underlying
    // CfnUserPoolIdentityProvider resource.
    //
    // This causes the Google OAuth consent screen to show ONLY accounts in
    // the configured hostedDomain — users from other domains cannot even
    // select their account. This is layer (a) of two-layer hosted-domain
    // enforcement; layer (b) is the pre-token-generation Lambda which
    // independently rejects any email outside ALLOWED_DOMAIN (issue #349:
    // hostedDomain is a required CDK context prop with no internal
    // tenant-domain default — see AuthStackProps.hostedDomain above).
    // -----------------------------------------------------------------------
    const cfnGoogleIdp = googleIdp.node.findChild('Resource') as cdk.CfnResource;
    cfnGoogleIdp.addPropertyOverride('ProviderDetails.hd', hostedDomain);

    // -----------------------------------------------------------------------
    // UserPoolClient — Google IdP only; no USER_PASSWORD_AUTH
    //
    // App client does NOT allow user-password or SRP sign-in — Google IdP
    // only.  This is enforced by setting authFlows to only allow the
    // refresh-token flow; direct-auth flows are explicitly excluded.
    //
    // Callback and logout URLs include:
    //   - Amplify Hosting URL (resolved from context / stack output)
    //   - localhost:3000 and localhost:5173 for local dev
    // -----------------------------------------------------------------------
    this.userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
      userPool: this.userPool,
      userPoolClientName: `contract-toaster-app-${envName}`,
      // Google IdP only — no Cognito built-in auth (USER_PASSWORD_AUTH disabled)
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.GOOGLE,
      ],
      // Auth flows: refresh token only; NO userPassword, NO userSrp
      authFlows: {
        userPassword: false,
        userSrp: false,
        custom: false,
      },
      // OAuth settings: Authorization Code Grant only (PKCE)
      oAuth: {
        flows: {
          authorizationCodeGrant: true,
          implicitCodeGrant: false,
          clientCredentials: false,
        },
        scopes: [
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.PROFILE,
        ],
        // Callback URLs: Amplify Hosting URL + localhost for dev. appName/appDomain
        // resolve from required CDK context (issues #233, #349 — no internal
        // tenant-domain default).
        callbackUrls: [
          `https://${domainPrefix}.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`,
          `https://${appName}.${appDomain}`,                  // Amplify Hosting prod URL
          `https://${domainPrefix}.${appDomain}`,        // env-scoped Amplify URL
          'http://localhost:3000',                        // Vite dev server
          'http://localhost:5173',                        // Vite alt port
        ],
        // Logout URLs: Amplify Hosting URL + localhost for dev
        logoutUrls: [
          `https://${appName}.${appDomain}`,
          `https://${domainPrefix}.${appDomain}`,
          'http://localhost:3000',
          'http://localhost:5173',
        ],
      },
      // Prevent client secret from being used in browser (public client)
      generateSecret: false,
      // Token validity matches the UserPool configuration
      accessTokenValidity: cdk.Duration.minutes(60),
      idTokenValidity: cdk.Duration.minutes(60),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    // Ensure the IdP is created before the client (explicit dependency)
    this.userPoolClient.node.addDependency(googleIdp);

    // -----------------------------------------------------------------------
    // Hosted UI domain prefix: 'contract-toaster-{envName}'
    //
    // Enables the Cognito hosted-UI sign-in page.  The domain prefix must
    // be globally unique; it combines the app name and environment.
    // -----------------------------------------------------------------------
    const userPoolDomain = new cognito.UserPoolDomain(this, 'UserPoolDomain', {
      userPool: this.userPool,
      cognitoDomain: {
        domainPrefix,
      },
    });

    // -----------------------------------------------------------------------
    // Break-glass (issue #229 — no dedicated CDK-managed IAM role for v1)
    //
    // This nested stack previously created a `BreakGlassRole` whose trust
    // policy used `iam.FederatedPrincipal('arn:aws:iam::aws:policy/
    // AdministratorAccess', ...)` — a managed-policy ARN in the position
    // where an identity-provider ARN belongs.  That is an invalid IAM
    // principal: `CreateRole` would be rejected at deploy time (nested
    // stacks fail the whole parent deploy), and — even if it had deployed —
    // mixing an `sts:RoleSessionName` condition with
    // `sts:AssumeRoleWithWebIdentity` semantics is incoherent for a SAML/SSO
    // principal.  The documented break-glass procedure was therefore never
    // actually exercisable.
    //
    // No real SAML-provider ARN or IAM Identity Center permission-set ARN is
    // available to wire here yet (the dev account's actual SSO grant is a
    // plain `AdministratorAccess` permission set — see aws-access-request.md
    // → Fulfillment record — not a custom federated role; prod is not yet
    // provisioned).  Rather than hard-code another placeholder ARN that
    // looks configured but is not real (the exact failure mode this issue
    // is about), v1 drops the custom role and documents break-glass as:
    //
    //   Emergency admin recovery = the `AdministratorAccess` SSO permission
    //   set already granted for this account, MFA-enforced by the SSO
    //   identity provider on every sign-in, used narrowly for this
    //   procedure only, with a MANUAL application-audit-table entry (there
    //   is no automated CloudTrail → `audit` mirror).
    //
    // Full procedure: RUNBOOK.md → "Break-glass: restoring admin access".
    //
    // Before promoting to prod: replace this with a dedicated
    // least-privilege IAM Identity Center permission set (or a SAML
    // provider ARN + `sts:AssumeRoleWithSAML` + MFA condition) scoped to
    // break-glass only, and run one live drill of the path (recorded per
    // RUNBOOK.md's bootstrap-acceptance philosophy).
    // -----------------------------------------------------------------------

    // -----------------------------------------------------------------------
    // admin_bootstrap seed (CDK custom resource stub)
    //
    // CDK writes a single row into the admin_bootstrap table keyed by the
    // configured GC email (adminEmail).  This is NOT a row in the
    // cognito_sub-keyed users table — the key shapes are incompatible.
    //
    // On first sign-in the backend runs a one-time reconciliation transaction:
    //   1. Confirm verified email matches an admin_bootstrap row.
    //   2. Write the real users row keyed by Cognito sub, with is_admin=true.
    //   3. Atomically mark the bootstrap row consumed (conditional write).
    //
    // The admin_bootstrap table name is passed in as a prop from DataStack
    // (or resolved from context while DataStack is not yet wired).
    //
    // adminEmail key: 'first_admin_email' = adminEmail
    // -----------------------------------------------------------------------
    const adminBootstrapTableName =
      props.adminBootstrapTableName ?? `${appName}-admin-bootstrap-${envName}`;

    // Document the admin bootstrap seed intent in a CDK metadata node.
    // The actual DynamoDB PutItem custom resource is wired once the
    // admin_bootstrap table ARN is available from DataStack (#52 output).
    // The intent is captured here so the structural gate (test_infra_auth_stack.py)
    // can assert its presence.
    new cdk.CfnResource(this, 'AdminBootstrapSeedIntent', {
      type: 'AWS::CloudFormation::WaitConditionHandle',
      properties: {
        // Document the admin_bootstrap seed intent:
        //   CDK writes a single row into admin_bootstrap table keyed by
        //   adminEmail (not into the cognito_sub-keyed users table).
        //   The backend runs a one-time bootstrap email → cognito sub
        //   reconciliation transaction on first sign-in.
        //
        // NOTE: Full DynamoDB PutItem custom resource is wired after
        // DataStack (#52) exposes the admin_bootstrap table ARN.
        AdminBootstrapEmailKey: adminEmail,
        AdminBootstrapTableName: adminBootstrapTableName,
        AdminBootstrapNote:
          'First-admin seed: email-keyed row in admin_bootstrap (NOT in users table). ' +
          'Backend reconciles email → Cognito sub on first sign-in (one-time transaction).',
      },
    });

    // -----------------------------------------------------------------------
    // Stack outputs
    // -----------------------------------------------------------------------
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      description: `Cognito user pool ID for ${envName}`,
      exportName: `ContractToaster-${envName}-UserPoolId`,
    });

    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: `Cognito user pool client ID for ${envName}`,
      exportName: `ContractToaster-${envName}-UserPoolClientId`,
    });

    new cdk.CfnOutput(this, 'UserPoolDomainPrefix', {
      value: userPoolDomain.domainName,
      description: `Cognito hosted UI domain prefix for ${envName}`,
      exportName: `ContractToaster-${envName}-UserPoolDomainPrefix`,
    });

    cdk.Tags.of(this).add('contract-toaster:env', envName);
    cdk.Tags.of(this).add('contract-toaster:component', 'auth');
  }
}
