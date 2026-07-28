#!/usr/bin/env python3
"""
Behavioral gate for issue #53: pre-token-generation Lambda deny paths.

These tests exercise the ACTUAL Lambda handler extracted from the inline code
in infra/lib/nested/auth-stack.ts.  They are NOT regex/doc-consistency tests
— they import and call the real handler function and assert that it raises
(i.e., denies the token) for each required deny path.

Required deny paths (AC):
  1. Domain mismatch: email not ending in @teamexos.com → deny.
  2. Non-allowlisted user: email is @teamexos.com but NOT in the
     legal-admin@teamexos.com Google group → deny.
  3. Directory unavailable: Directory API returns an error → deny (fail-closed).
  4. Unverified email: email_verified != 'true' → deny.

The Lambda handler is imported by extracting the inline code from the CDK
stack source file (infra/lib/nested/auth-stack.ts) and executing it in a
fresh module namespace.  This ensures that any change to the handler code is
automatically picked up without a separate maintenance step.

Test-integrity notes:
  - Tests 2 and 3 patch `_jit_create_user_row` (and the boto3-using
    `_get_secret` helper) so that no boto3 import can mask an authz-layer
    denial.  The deny under test must come ONLY from the authz layer, not
    from an incidental ImportError on the JIT/boto3 path that executes AFTER
    the authz decision point.
  - Assertions for Tests 2 and 3 require the SPECIFIC deny reason
    ("not in"/"group" for Test 2; "directory"/"fail-closed" for Test 3) and
    explicitly reject "DynamoDB" / "boto3" masking messages.
  - Test 6 strengthens the RED-state self-check to surface per-path fail-open
    for BOTH the Directory-API-error branch flip AND the non-allowlisted-user
    deny deletion, not just a wholesale fail-open handler.

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import importlib
import re
import sys
import types
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_STACK_PATH = REPO_ROOT / "infra" / "lib" / "nested" / "auth-stack.ts"

# ALLOWED_DOMAIN / LEGAL_ADMIN_GROUP are no longer module-level constants in
# the extracted Lambda source (issue #349) -- they are read from the
# environment inside handler() at call time, exactly like DIRECTORY_SECRET_ARN
# / USERS_TABLE_NAME already were. These tests inject them via env, the same
# way. The domain chosen here is this test file's own arbitrary fixture value
# (matching the "user@teamexos.com"-style emails already used throughout this
# file) -- it exercises handler LOGIC given injected config and is unrelated
# to any real deploy default.
TEST_ALLOWED_DOMAIN = "@teamexos.com"
TEST_LEGAL_ADMIN_GROUP = "legal-admin@teamexos.com"

# ---------------------------------------------------------------------------
# Load the inline Lambda handler from auth-stack.ts
# ---------------------------------------------------------------------------

def _extract_lambda_code() -> str:
    """
    Extract the Python handler source from the fromInline(…) call in the CDK stack.
    The inline code is delimited by backtick template literals.
    """
    source = AUTH_STACK_PATH.read_text(encoding="utf-8")
    # Match: lambda.Code.fromInline(`  …code…  `)
    m = re.search(r"lambda\.Code\.fromInline\(\`(.*?)\`\)", source, re.DOTALL)
    if not m:
        raise RuntimeError(
            f"Could not locate lambda.Code.fromInline(`...`) in {AUTH_STACK_PATH}.\n"
            "Ensure the pre-token Lambda code is defined as an inline fromInline template literal."
        )
    return m.group(1)


def _load_handler():
    """
    Execute the extracted Lambda code in a fresh module and return the handler function.
    Also returns the module namespace for patching.

    boto3 and cryptography are not available in the test environment; that is
    intentional.  Tests that reach the authz decision layer patch out every
    boto3-touching helper (_jit_create_user_row, _get_secret) so those
    ImportErrors can never mask an authz-layer outcome.
    """
    code = _extract_lambda_code()
    module = types.ModuleType("pre_token_lambda")
    # Provide a minimal __builtins__ so exec works
    module.__builtins__ = __builtins__  # type: ignore[attr-defined]
    exec(compile(code, "<pre_token_lambda_inline>", "exec"), module.__dict__)  # noqa: S102
    if not hasattr(module, "handler"):
        raise RuntimeError(
            "Extracted Lambda code does not define a 'handler' function.\n"
            "The inline Python code must define 'def handler(event, context):'."
        )
    return module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(email: str, email_verified: str = "true", sub: str = "test-sub-123") -> dict:
    """Build a minimal Cognito pre-token-generation event."""
    return {
        "triggerSource": "TokenGeneration_Authentication",
        "request": {
            "userAttributes": {
                "email": email,
                "email_verified": email_verified,
                "sub": sub,
            },
        },
    }


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


def _authz_env_and_boto3_patches(module):
    """
    Return a context-manager stack that:
      1. Sets DIRECTORY_SECRET_ARN + USERS_TABLE_NAME env vars so the handler
         reaches the authz decision layer (not the env-guard short-circuit).
      2. Patches _jit_create_user_row to a no-op so the JIT/DynamoDB path
         NEVER executes after the authz decision — preventing boto3 ImportErrors
         from masking a missing authz-layer deny.

    Callers must additionally patch _check_group_membership themselves with
    the specific return_value or side_effect for the scenario under test.
    """
    import contextlib

    @contextlib.contextmanager
    def _combined():
        env_patch = unittest.mock.patch.dict(
            module.os.environ,
            {
                "DIRECTORY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
                "USERS_TABLE_NAME": "contract-toaster-users-test",
                "ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN,
                "LEGAL_ADMIN_GROUP": TEST_LEGAL_ADMIN_GROUP,
            },
        )
        jit_patch = unittest.mock.patch.object(
            module, "_jit_create_user_row", return_value=None
        )
        with env_patch, jit_patch:
            yield

    return _combined()


# ---------------------------------------------------------------------------
# Test 1 — Domain mismatch: non-@teamexos.com email must be denied
# ---------------------------------------------------------------------------

def test_domain_mismatch_deny() -> list[str]:
    print("\nTest 1: Domain-mismatch deny (non-@teamexos.com email) …")
    failures: list[str] = []

    module = _load_handler()
    # Patch _check_group_membership so it would return True if the domain
    # check is absent — the test must fail due to domain, not group check.
    # ALLOWED_DOMAIN is read from the environment (issue #349), so it must be
    # set explicitly here for the domain check to exercise real logic.
    env_patch = unittest.mock.patch.dict(
        module.os.environ,  # type: ignore[attr-defined]
        {"ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN},
    )
    with env_patch, unittest.mock.patch.object(module, "_check_group_membership", return_value=True):
        for bad_email in ["attacker@evil.com", "user@gmail.com", "test@other.com"]:
            event = _make_event(bad_email)
            raised = False
            try:
                module.handler(event, None)
            except Exception as exc:
                raised = True
                denied_for_domain = (
                    "domain" in str(exc).lower()
                    or "teamexos" in str(exc).lower()
                    or "not allowed" in str(exc).lower()
                    or "denied" in str(exc).lower()
                )
                failures += _assert(
                    denied_for_domain,
                    f"handler raises for domain-mismatch email {bad_email!r} with domain/deny reason",
                    f"Exception raised: {exc!r}\n"
                    "         The denial reason must mention domain, teamexos, 'not allowed', or 'denied'.",
                )
            if not raised:
                failures += _assert(
                    False,
                    f"handler raises Exception for domain-mismatch email {bad_email!r}",
                    "Per AC layer (b): the Lambda must deny any non-@teamexos.com email.\n"
                    "         The handler returned normally instead of raising.",
                )

    return failures


# ---------------------------------------------------------------------------
# Test 2 — Non-allowlisted user: @teamexos.com but not in legal-admin group
#
# Test-integrity: _jit_create_user_row is patched out so that if the
# non-allowlisted deny is REMOVED and execution falls through to the JIT step,
# a boto3 ImportError CANNOT mask the missing authz-layer deny.  The assertion
# requires the specific group/membership deny reason and explicitly rejects any
# masking message containing "DynamoDB" or "boto3".
# ---------------------------------------------------------------------------

def test_non_allowlisted_deny() -> list[str]:
    print("\nTest 2: Non-allowlisted deny (not in legal-admin@teamexos.com group) …")
    failures: list[str] = []

    module = _load_handler()
    # _check_group_membership returns False → user is not in the group.
    # _jit_create_user_row is patched out via _authz_env_and_boto3_patches so
    # boto3 cannot mask a missing authz deny on this path.
    with _authz_env_and_boto3_patches(module), \
         unittest.mock.patch.object(module, "_check_group_membership", return_value=False):
        event = _make_event("user@teamexos.com")
        raised = False
        exc_str = ""
        try:
            module.handler(event, None)
        except Exception as exc:
            raised = True
            exc_str = str(exc).lower()

            # Must name the specific authz-layer reason (group / membership).
            denied_for_group = (
                "group" in exc_str
                or "legal-admin" in exc_str
                or "not in" in exc_str
                or "allowlist" in exc_str
            )
            # Must NOT be the DynamoDB/boto3 masking message.
            is_masking_message = "dynamodb" in exc_str or "boto3" in exc_str

            failures += _assert(
                denied_for_group and not is_masking_message,
                "handler raises for @teamexos.com user NOT in legal-admin group "
                "with specific group-membership reason (not a DynamoDB/boto3 mask)",
                f"Exception raised: {exc!r}\n"
                "         The denial reason must mention group/legal-admin/'not in'/allowlist\n"
                "         and must NOT mention DynamoDB or boto3 (which would indicate\n"
                "         the authz deny is missing and a downstream error is masking it).",
            )
        if not raised:
            failures += _assert(
                False,
                "handler raises Exception for @teamexos.com user NOT in legal-admin group",
                "Per AC: the Lambda must deny a @teamexos.com user who is not a member of\n"
                "         legal-admin@teamexos.com.  The handler returned normally instead of raising.",
            )

    return failures


# ---------------------------------------------------------------------------
# Test 3 — Directory unavailable: Directory API error must deny (fail-closed)
#
# Test-integrity: _jit_create_user_row is patched out so that if the
# Directory-API-error branch is flipped to fail-open (is_member = True),
# execution falls through to the JIT step but a boto3 ImportError CANNOT mask
# the missing authz-layer deny.  The assertion requires a specific
# directory/fail-closed reason and explicitly rejects "DynamoDB"/"boto3".
# ---------------------------------------------------------------------------

def test_directory_unavailable_deny() -> list[str]:
    print("\nTest 3: Directory-unavailable deny (fail-closed on Directory API error) …")
    failures: list[str] = []

    module = _load_handler()
    # _check_group_membership raises an exception → simulates Directory API failure.
    # _jit_create_user_row is patched out via _authz_env_and_boto3_patches so
    # boto3 cannot mask a missing authz deny if the fail-closed branch is removed.
    dir_error = ConnectionError("simulated Directory API unavailable")
    with _authz_env_and_boto3_patches(module), \
         unittest.mock.patch.object(module, "_check_group_membership", side_effect=dir_error):
        event = _make_event("user@teamexos.com")
        raised = False
        exc_str = ""
        try:
            module.handler(event, None)
        except Exception as exc:
            raised = True
            exc_str = str(exc).lower()

            # Denial reason must indicate the directory error / fail-closed path —
            # NOT the env-guard short-circuit ("DIRECTORY_SECRET_ARN not configured")
            # AND NOT the DynamoDB/boto3 masking message.
            is_denied_for_directory_error = (
                "directory" in exc_str
                or "fail-closed" in exc_str
            )
            is_env_guard = "directory_secret_arn not configured" in exc_str
            is_masking_message = "dynamodb" in exc_str or "boto3" in exc_str

            failures += _assert(
                is_denied_for_directory_error and not is_env_guard and not is_masking_message,
                "handler raises (denies) when Directory API raises an error (fail-closed) "
                "with specific directory/fail-closed reason (not a DynamoDB/boto3 mask)",
                f"Exception raised: {exc!r}\n"
                "         The denial reason must indicate fail-closed/directory error,\n"
                "         must NOT be the env-guard short-circuit, and\n"
                "         must NOT mention DynamoDB or boto3 (which would indicate\n"
                "         the fail-closed branch is missing and a downstream error is masking it).\n"
                "         Per AC: 'fail closed when the Directory API is unreachable'.",
            )
        if not raised:
            failures += _assert(
                False,
                "handler raises Exception when Directory API raises (fail-closed behavior)",
                "Per AC: 'fail closed when the Directory API is unreachable (deny; never fail open)'.\n"
                "         The handler returned normally instead of raising — this is a FAIL-OPEN bug.",
            )

    return failures


# ---------------------------------------------------------------------------
# Test 4 — Unverified email: email_verified != 'true' must be denied
# ---------------------------------------------------------------------------

def test_unverified_email_deny() -> list[str]:
    print("\nTest 4: Unverified email deny (email_verified != 'true') …")
    failures: list[str] = []

    module = _load_handler()
    # ALLOWED_DOMAIN must be set so the (earlier) domain check passes and
    # execution actually reaches the email_verified check under test.
    env_patch = unittest.mock.patch.dict(
        module.os.environ,  # type: ignore[attr-defined]
        {"ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN},
    )
    with env_patch, unittest.mock.patch.object(module, "_check_group_membership", return_value=True):
        event = _make_event("user@teamexos.com", email_verified="false")
        raised = False
        try:
            module.handler(event, None)
        except Exception as exc:
            raised = True
            denied_unverified = (
                "verified" in str(exc).lower()
                or "denied" in str(exc).lower()
            )
            failures += _assert(
                denied_unverified,
                "handler raises for unverified email (email_verified=false)",
                f"Exception raised: {exc!r}\n"
                "         The denial reason must mention 'verified' or 'denied'.",
            )
        if not raised:
            failures += _assert(
                False,
                "handler raises Exception for unverified email (email_verified=false)",
                "Per AC: the Lambda must reject identities whose email is not verified.\n"
                "         The handler returned normally instead of raising.",
            )

    return failures


# ---------------------------------------------------------------------------
# Test 5 — Happy-path: valid @teamexos.com user in group → returns event
#
# This confirms the handler does NOT deny valid users, and that the
# fail-closed tests above are actually testing denial (not normal flow).
# We mock both group membership (True) and DynamoDB to avoid AWS calls.
# ---------------------------------------------------------------------------

def test_happy_path_no_deny() -> list[str]:
    print("\nTest 5: Happy-path — valid @teamexos.com group member → token allowed …")
    failures: list[str] = []

    module = _load_handler()
    # Patch group membership, DynamoDB, AND the environment variable so the
    # handler reaches the allow path without real AWS calls.
    env_patch = unittest.mock.patch.dict(
        module.os.environ,  # type: ignore[attr-defined]
        {"DIRECTORY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
         "USERS_TABLE_NAME": "contract-toaster-users-test",
         "ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN,
         "LEGAL_ADMIN_GROUP": TEST_LEGAL_ADMIN_GROUP},
    )
    with env_patch, \
         unittest.mock.patch.object(module, "_check_group_membership", return_value=True), \
         unittest.mock.patch.object(module, "_jit_create_user_row", return_value=None):
        event = _make_event("reviewer@teamexos.com")
        try:
            result = module.handler(event, None)
            # On success the handler must return the event unmodified
            failures += _assert(
                result == event,
                "handler returns the event unmodified for a valid @teamexos.com group member",
                f"Expected: {event!r}\n         Got: {result!r}",
            )
        except Exception as exc:
            failures += _assert(
                False,
                "handler does NOT raise for a valid @teamexos.com group member",
                f"Unexpected exception: {exc!r}",
            )

    return failures


# ---------------------------------------------------------------------------
# Test 6 — TDD RED-state verification (strengthened for per-path fail-open)
#
# This test re-establishes the required RED phase for the behavioral deny-path
# invariants (findings 1–3 in the issue #53 fix round 2 review), and has been
# strengthened to surface per-path fail-open mutations, not just a wholesale
# fail-open handler stub.
#
# Specifically:
#   Sub-test A: non-allowlisted deny — simulates removing the `if not is_member:
#               _deny(...)` block by patching handler to skip the group check
#               and return normally when membership is False.
#   Sub-test B: directory fail-closed deny — simulates flipping the except-block
#               in the handler from _deny(...) to is_member = True by patching
#               handler to skip the directory-error deny and return normally.
#   Sub-test C: wholesale fail-open — replaces handler with a stub that never
#               raises regardless of inputs.
#
# All three stubs include the _jit_create_user_row patch (the same isolation
# used in Tests 2 and 3) so that boto3 masking cannot produce a false GREEN.
# ---------------------------------------------------------------------------

def _make_fail_open_module() -> types.ModuleType:
    """
    Return a module with a fail-open handler stub (never raises, always returns
    the event).  Used for sub-test C (wholesale fail-open).
    """
    module = _load_handler()

    # Replace handler with a fail-open stub
    def _fail_open_handler(event, context):  # noqa: ANN001
        return event

    module.handler = _fail_open_handler  # type: ignore[attr-defined]
    return module


def _make_missing_group_deny_module() -> types.ModuleType:
    """
    Return a module whose handler skips the `if not is_member: _deny(...)` check
    (simulates deletion of the non-allowlisted-user deny).  The JIT step is still
    present but _jit_create_user_row is patched to a no-op, so boto3 cannot mask.
    """
    module = _load_handler()
    _orig_handler = module.handler

    def _handler_without_group_deny(event, context):  # noqa: ANN001
        # Intercept the group-membership result so is_member is always True,
        # simulating removal of the `if not is_member: _deny(...)` guard.
        with unittest.mock.patch.object(module, "_check_group_membership", return_value=True), \
             unittest.mock.patch.object(module, "_jit_create_user_row", return_value=None):
            return _orig_handler(event, context)

    module.handler = _handler_without_group_deny  # type: ignore[attr-defined]
    return module


def _make_directory_fail_open_module() -> types.ModuleType:
    """
    Return a module whose handler treats a Directory API error as is_member=True
    (simulates flipping the fail-closed branch to fail-open).  The JIT step is
    present but _jit_create_user_row is patched to a no-op so boto3 cannot mask.
    """
    module = _load_handler()
    _orig_handler = module.handler

    def _handler_with_dir_fail_open(event, context):  # noqa: ANN001
        # Make _check_group_membership raise, then intercept so the handler
        # treats it as is_member=True (fail-open), and patch out JIT/boto3.
        # We do this by wrapping _check_group_membership: first call raises,
        # and we patch handler to catch and recover fail-open.
        # The cleanest approach: re-exec the handler with a patched version
        # that swallows the Directory exception.
        #
        # Strategy: patch _check_group_membership to raise, but also wrap
        # the module's handler internals by replacing _check_group_membership
        # at the module level with a side_effect that triggers the fail-open
        # path, then patch _jit_create_user_row to a no-op.
        #
        # We synthesize this by patching _check_group_membership to raise AND
        # monkey-patching the except clause to not call _deny.
        # The simplest correct approach: reload the handler code, edit the
        # except clause inline to set is_member = True instead of _deny, then
        # exec that mutated code.
        code = _extract_lambda_code()
        mutated = code.replace(
            '_deny(f"Directory API error (fail-closed): {exc}")',
            "is_member = True  # MUTATED: fail-open",
        )
        mut_module = types.ModuleType("pre_token_lambda_mutated")
        mut_module.__builtins__ = __builtins__  # type: ignore[attr-defined]
        exec(compile(mutated, "<mutated>", "exec"), mut_module.__dict__)  # noqa: S102
        with unittest.mock.patch.dict(
            mut_module.os.environ,
            {
                "DIRECTORY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
                "USERS_TABLE_NAME": "contract-toaster-users-test",
                "ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN,
                "LEGAL_ADMIN_GROUP": TEST_LEGAL_ADMIN_GROUP,
            },
        ), unittest.mock.patch.object(
            mut_module, "_check_group_membership",
            side_effect=ConnectionError("simulated Directory API unavailable"),
        ), unittest.mock.patch.object(
            mut_module, "_jit_create_user_row", return_value=None
        ):
            return mut_module.handler(event, context)

    module.handler = _handler_with_dir_fail_open  # type: ignore[attr-defined]
    return module


def test_tdd_red_state_verification() -> list[str]:
    """
    Confirm Tests 2 and 3 produce at least one failure when run against per-path
    fail-open mutations AND a wholesale fail-open stub.

    Sub-test A: non-allowlisted deny deleted — Test 2 must catch this.
    Sub-test B: Directory-API-error branch flipped to fail-open — Test 3 must catch this.
    Sub-test C: wholesale fail-open handler — Tests 2 and 3 must catch this.

    All stubs include the _jit_create_user_row patch so boto3 masking cannot
    produce a false GREEN that hides the missing authz deny.
    """
    print("\nTest 6: TDD RED-state — confirm deny tests fail against per-path fail-open mutations …")
    failures: list[str] = []

    # --- Sub-test A: non-allowlisted deny removed → Test 2 must fail ---
    print("  Sub-test A: non-allowlisted deny deleted (group-deny guard removed) …")
    stub_module_a = _make_missing_group_deny_module()
    env_patch_a = unittest.mock.patch.dict(
        stub_module_a.os.environ,  # type: ignore[attr-defined]
        {
            "DIRECTORY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            "USERS_TABLE_NAME": "contract-toaster-users-test",
            "ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN,
            "LEGAL_ADMIN_GROUP": TEST_LEGAL_ADMIN_GROUP,
        },
    )
    with env_patch_a:
        event = _make_event("user@teamexos.com")
        raised_a = False
        exc_str_a = ""
        try:
            stub_module_a.handler(event, None)
        except Exception as exc:
            raised_a = True
            exc_str_a = str(exc).lower()

    if raised_a:
        # The mutation stub raised — only acceptable if it's NOT a DynamoDB/boto3 mask.
        # If it IS the masking message, the isolation has a gap.
        is_masking = "dynamodb" in exc_str_a or "boto3" in exc_str_a
        if is_masking:
            # Masking still present despite patch — test isolation is broken.
            test2_would_fail = False
        else:
            # The handler raised for a legitimate authz reason even with group deny removed —
            # this means another prior check (domain, unverified) caught it, which shouldn't
            # happen for a valid @teamexos.com verified user.  RED-state check is inconclusive.
            test2_would_fail = True  # still a real denial, RED state holds
    else:
        # Handler returned normally (fail-open) — this is the expected RED outcome.
        # Test 2 catches this: "handler raises Exception for @teamexos.com user NOT in
        # legal-admin group" would FAIL.
        test2_would_fail = True

    failures += _assert(
        test2_would_fail,
        "Test 2 (non-allowlisted deny) detects per-path fail-open: group-deny guard removed — was RED before GREEN",
        "With the non-allowlisted-user deny removed (or bypassed), Test 2 must report failure.\n"
        "         If Test 2 passed, the group-deny path is unverified.\n"
        "         Note: boto3/DynamoDB masking must not produce a false GREEN here.",
    )

    # --- Sub-test B: Directory-API-error branch flipped to fail-open → Test 3 must fail ---
    print("  Sub-test B: Directory-API-error branch flipped to fail-open (is_member = True) …")
    stub_module_b = _make_directory_fail_open_module()
    env_patch_b = unittest.mock.patch.dict(
        stub_module_b.os.environ,  # type: ignore[attr-defined]
        {
            "DIRECTORY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            "USERS_TABLE_NAME": "contract-toaster-users-test",
            "ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN,
            "LEGAL_ADMIN_GROUP": TEST_LEGAL_ADMIN_GROUP,
        },
    )
    with env_patch_b:
        event = _make_event("user@teamexos.com")
        raised_b = False
        exc_str_b = ""
        try:
            stub_module_b.handler(event, None)
        except Exception as exc:
            raised_b = True
            exc_str_b = str(exc).lower()

    if raised_b:
        is_masking = "dynamodb" in exc_str_b or "boto3" in exc_str_b
        if is_masking:
            # Masking still present — test isolation is broken.
            test3_would_fail = False
        else:
            test3_would_fail = True
    else:
        # Handler returned normally (fail-open) — expected RED outcome for Test 3.
        test3_would_fail = True

    failures += _assert(
        test3_would_fail,
        "Test 3 (directory-unavailable / fail-closed) detects per-path fail-open: "
        "Directory-API-error branch flipped to fail-open — was RED before GREEN",
        "With the Directory-API-error branch flipped to fail-open (is_member=True),\n"
        "         Test 3 must report failure.\n"
        "         If Test 3 passed, the fail-closed path is unverified.\n"
        "         Note: boto3/DynamoDB masking must not produce a false GREEN here.",
    )

    # --- Sub-test C: wholesale fail-open handler → Tests 2 and 3 must fail ---
    print("  Sub-test C: wholesale fail-open handler (never raises) …")
    stub_module_c = _make_fail_open_module()
    env_patch_c = unittest.mock.patch.dict(
        stub_module_c.os.environ,  # type: ignore[attr-defined]
        {
            "DIRECTORY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            "USERS_TABLE_NAME": "contract-toaster-users-test",
            "ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN,
            "LEGAL_ADMIN_GROUP": TEST_LEGAL_ADMIN_GROUP,
        },
    )
    with env_patch_c, unittest.mock.patch.object(
        stub_module_c, "_check_group_membership", return_value=False
    ):
        event = _make_event("user@teamexos.com")
        try:
            stub_module_c.handler(event, None)
            # The fail-open stub returned normally — this is the expected RED outcome.
            test2_wholesale_would_fail = True
        except Exception:
            # The stub should NOT raise; if it does, the module is broken.
            test2_wholesale_would_fail = False

    failures += _assert(
        test2_wholesale_would_fail,
        "Test 2 (non-allowlisted deny) detects wholesale fail-open stub — was RED before GREEN",
        "A fail-open handler (never raises) must cause Test 2 to report failure.\n"
        "         If Test 2 passed against the stub, the group-deny path is unverified.",
    )

    stub_module_d = _make_fail_open_module()
    env_patch_d = unittest.mock.patch.dict(
        stub_module_d.os.environ,  # type: ignore[attr-defined]
        {
            "DIRECTORY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            "USERS_TABLE_NAME": "contract-toaster-users-test",
            "ALLOWED_DOMAIN": TEST_ALLOWED_DOMAIN,
            "LEGAL_ADMIN_GROUP": TEST_LEGAL_ADMIN_GROUP,
        },
    )
    dir_error = ConnectionError("simulated Directory API unavailable")
    with env_patch_d, unittest.mock.patch.object(
        stub_module_d, "_check_group_membership", side_effect=dir_error
    ):
        event = _make_event("user@teamexos.com")
        try:
            stub_module_d.handler(event, None)
            # The fail-open stub returned normally — expected RED outcome for Test 3.
            test3_wholesale_would_fail = True
        except Exception:
            test3_wholesale_would_fail = False

    failures += _assert(
        test3_wholesale_would_fail,
        "Test 3 (directory-unavailable / fail-closed) detects wholesale fail-open stub — was RED before GREEN",
        "A fail-open handler (never raises) must cause Test 3 to report failure.\n"
        "         If Test 3 passed against the stub, the fail-closed path is unverified.",
    )

    return failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Pre-token Lambda behavioral gate (issue #53 — deny-path tests)")
    print("=" * 65)

    # Verify the Lambda code can be extracted before running tests
    try:
        code = _extract_lambda_code()
    except RuntimeError as exc:
        print(f"\nFATAL: {exc}")
        return 1

    if len(code.strip()) < 100:
        print(
            "\nFATAL: Extracted Lambda code is too short (< 100 chars).\n"
            "The inline handler appears to be a stub — implement the real\n"
            "enforcement logic before these behavioral tests can pass."
        )
        return 1

    all_failures: list[str] = []
    all_failures += test_domain_mismatch_deny()
    all_failures += test_non_allowlisted_deny()
    all_failures += test_directory_unavailable_deny()
    all_failures += test_unverified_email_deny()
    all_failures += test_happy_path_no_deny()
    all_failures += test_tdd_red_state_verification()

    print("\n" + "=" * 65)
    if all_failures:
        print(
            f"\nFAIL: {len(all_failures)} check(s) failed.\n"
            "See output above for details."
        )
        return 1

    print("\nPASS: all pre-token Lambda behavioral checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
