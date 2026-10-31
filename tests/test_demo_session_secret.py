#!/usr/bin/env python3
"""
DEMO_TOKEN_SECRET must be a real secret before a password-mode backend boots
(QA 2026-10-31, report #3).

Every password-mode session is an HS256 token signed with DEMO_TOKEN_SECRET.
`deploy/dts/.env.example` used to ship a usable value, and nothing checked
what reached the container, so a deployment could run on a secret printed in
this public repository.

These tests pin:

  1. `startup_checks.verify_demo_token_secret` refuses an unset, short,
     placeholder or single-character secret when AUTH_MODE is `password` or
     `both`, accepts 64 hex characters, and does nothing under `sso`.
  2. Its message says how to generate a secret and never contains the value.
  3. `demo_auth._demo_token_secret` applies the same rule at the point of use
     (503, value not echoed), so a path that skipped the lifespan still
     cannot sign or accept a token with a weak secret.
  4. The check is wired into `main._lifespan`, at the top level, right after
     `verify_required_env`.
  5. The shipped deploy files carry no usable value: `.env.example` is blank
     with a generate comment, the Coolify compose names a Coolify-generated
     variable, and the local compose still requires the operator to set it.

Run: python3 tests/test_demo_session_secret.py
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import src.demo_auth as demo_auth  # noqa: E402
import src.startup_checks as startup_checks  # noqa: E402
from fastapi import HTTPException  # noqa: E402

GOOD = "3f9c2a7be41d08c6a5f2e9b7d1c04a8e6b3f5d2c9e0a7b1d4c8f6e2a9b5d3c07"
ENV_EXAMPLE_PLACEHOLDER = "change-me-to-a-long-random-string"
DTS = REPO_ROOT / "deploy" / "dts"

WEAK = {
    "unset": None,
    "blank": "   ",
    "short": "unit-test-demo-secret",
    "env_example_placeholder": ENV_EXAMPLE_PLACEHOLDER,
    "placeholder_case_and_punctuation": "CHANGE_ME_TO_A_LONG_RANDOM_STRING",
    "change_me_prefix_long": "change-me-please-to-something-long-and-random",
    "repeated_character": "a" * 64,
}


def _env(mode: str, secret: str | None) -> dict[str, str]:
    env = {"AUTH_MODE": mode}
    if secret is not None:
        env["DEMO_TOKEN_SECRET"] = secret
    return env


class TestStartupCheck(unittest.TestCase):
    def test_weak_secrets_refuse_to_boot_in_password_and_both(self) -> None:
        for mode in ("password", "both", " Password "):
            for label, secret in WEAK.items():
                with self.subTest(mode=mode, case=label):
                    with self.assertRaises(SystemExit) as caught:
                        startup_checks.verify_demo_token_secret(_env(mode, secret))
                    message = str(caught.exception)
                    self.assertIn("DEMO_TOKEN_SECRET", message)
                    self.assertIn("openssl rand -hex 32", message)
                    if secret and secret.strip():
                        self.assertNotIn(secret, message)

    def test_sixty_four_hex_characters_boot(self) -> None:
        for mode in ("password", "both"):
            with self.subTest(mode=mode):
                self.assertIsNone(startup_checks.verify_demo_token_secret(_env(mode, GOOD)))

    def test_sso_mode_does_not_need_a_secret(self) -> None:
        self.assertIsNone(startup_checks.verify_demo_token_secret({"AUTH_MODE": "sso"}))
        self.assertIsNone(startup_checks.verify_demo_token_secret({}))

    def test_message_never_echoes_the_value_even_for_the_placeholder(self) -> None:
        with (
            self.assertLogs(startup_checks.logger, level="ERROR") as logs,
            self.assertRaises(SystemExit),
        ):
            startup_checks.verify_demo_token_secret(_env("password", ENV_EXAMPLE_PLACEHOLDER))
        self.assertNotIn(ENV_EXAMPLE_PLACEHOLDER, "\n".join(logs.output))


class TestPointOfUseGuard(unittest.TestCase):
    def test_weak_secret_cannot_sign_or_verify(self) -> None:
        row = {"cognito_sub": "local:alice", "username": "alice", "is_admin": False}
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": GOOD}, clear=True):
            token = demo_auth.issue_demo_token(row)
        for label, secret in WEAK.items():
            env = {} if secret is None else {"DEMO_TOKEN_SECRET": secret}
            with self.subTest(case=label), patch.dict("os.environ", env, clear=True):
                for call in (lambda: demo_auth.issue_demo_token(row),
                             lambda: demo_auth.verify_demo_token(token)):
                    with self.assertRaises(HTTPException) as caught:
                        call()
                    self.assertEqual(caught.exception.status_code, 503)
                    if secret and secret.strip():
                        self.assertNotIn(secret, str(caught.exception.detail))

    def test_strong_secret_round_trips(self) -> None:
        row = {"cognito_sub": "local:alice", "username": "alice", "is_admin": False}
        with patch.dict("os.environ", {"DEMO_TOKEN_SECRET": GOOD}, clear=True):
            claims = demo_auth.verify_demo_token(demo_auth.issue_demo_token(row))
        self.assertEqual(claims["sub"], "local:alice")


class TestWiredIntoLifespan(unittest.TestCase):
    def test_runs_at_top_level_right_after_the_required_env_check(self) -> None:
        source = (BACKEND_ROOT / "src" / "main.py").read_text(encoding="utf-8")
        function = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lifespan"
        )
        body = [
            ast.unparse(s.value) for s in function.body
            if isinstance(s, ast.Expr) and not isinstance(s.value, ast.Constant)
        ]
        self.assertEqual(body[0], "startup_checks.verify_required_env()")
        self.assertEqual(body[1], "startup_checks.verify_demo_token_secret()")


class TestShippedDeployFiles(unittest.TestCase):
    def test_env_example_ships_no_value_and_says_how_to_generate(self) -> None:
        text = (DTS / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn(ENV_EXAMPLE_PLACEHOLDER, text)
        lines = text.splitlines()
        self.assertIn("DEMO_TOKEN_SECRET=", lines)
        self.assertIn("openssl rand -hex 32", text)

    def test_coolify_compose_uses_a_generated_value(self) -> None:
        compose = yaml.safe_load(
            (DTS / "docker-compose.coolify.yml").read_text(encoding="utf-8")
        )
        value = compose["services"]["backend"]["environment"]["DEMO_TOKEN_SECRET"]
        self.assertEqual(value, "${SERVICE_PASSWORD_64_DEMOTOKEN}")

    def test_local_compose_still_requires_the_operator_to_set_it(self) -> None:
        compose = yaml.safe_load(
            (DTS / "docker-compose.yml").read_text(encoding="utf-8")
        )
        value = compose["services"]["backend"]["environment"]["DEMO_TOKEN_SECRET"]
        self.assertTrue(str(value).startswith("${DEMO_TOKEN_SECRET:?"), value)


def _run_tests() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (TestStartupCheck, TestPointOfUseGuard, TestWiredIntoLifespan,
                 TestShippedDeployFiles):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
