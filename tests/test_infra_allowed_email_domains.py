#!/usr/bin/env python3
"""
The AWS App Runner service is given ALLOWED_EMAIL_DOMAINS from CDK context,
and synth fails closed without it.

WHY THIS GATE EXISTS
--------------------
`backend/src/auth.py` refuses every Cognito-authenticated request with 503
while `ALLOWED_EMAIL_DOMAINS` is unset (issue #274, fail-closed). AppStack
never injected it, so a stack that synthesized and deployed cleanly served
nothing but 503s to signed-in users, and the README, infra README and review
guide each carried a "the CDK stack does not set it yet" caveat.

  A. With `--context allowedEmailDomains=…` the synthesized App Runner
     service carries ALLOWED_EMAIL_DOMAINS with exactly that value.
  B. Without it, `cdk synth` exits non-zero and names `allowedEmailDomains`.
  C. A value the backend would read as an empty or wildcard allowlist
     (blank entries, `*`, a non-domain) is refused at synth too.
  D. No doc still says the stack does not set it.

Run: python3 tests/test_infra_allowed_email_domains.py
Exit 0 = pass, 1 = fail.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
sys.path.insert(0, str(REPO_ROOT / "tests"))

from infra_synth_helper import NEUTRAL_CDK_CONTEXT  # noqa: E402

DOMAINS = "example.com,subsidiary.example"


def _context_without_allowed_domains() -> list[str]:
    """NEUTRAL_CDK_CONTEXT minus any allowedEmailDomains pair."""
    out: list[str] = []
    args = list(NEUTRAL_CDK_CONTEXT)
    for flag, value in zip(args[::2], args[1::2], strict=True):
        if value.startswith("allowedEmailDomains="):
            continue
        out += [flag, value]
    return out


def _synth(extra: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["npx", "cdk", "synth", "--context", "env=dev", *extra, "--quiet"],  # noqa: S607
        cwd=INFRA,
        capture_output=True,
        text=True,
    )


def _app_runner_env() -> dict[str, object]:
    for template_path in sorted((INFRA / "cdk.out").glob("*.nested.template.json")):
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::AppRunner::Service":
                continue
            image = (
                resource["Properties"]["SourceConfiguration"]
                .get("ImageRepository", {})
                .get("ImageConfiguration", {})
            )
            return {v["Name"]: v["Value"] for v in image.get("RuntimeEnvironmentVariables", [])}
    return {}


class TestAllowedEmailDomains(unittest.TestCase):
    def test_a_context_value_reaches_app_runner(self) -> None:
        result = _synth([*_context_without_allowed_domains(), "--context", f"allowedEmailDomains={DOMAINS}"])
        self.assertEqual(result.returncode, 0, result.stderr[-1200:])
        self.assertEqual(_app_runner_env().get("ALLOWED_EMAIL_DOMAINS"), DOMAINS)

    def test_b_synth_fails_closed_without_it(self) -> None:
        result = _synth(_context_without_allowed_domains())
        self.assertNotEqual(result.returncode, 0, "synth succeeded without allowedEmailDomains")
        self.assertIn("allowedEmailDomains", result.stdout + result.stderr)

    def test_c_unusable_values_are_refused(self) -> None:
        for bad in ("*", "example.com,,other.example", " , ", "not a domain", "@example.com"):
            with self.subTest(value=bad):
                result = _synth([*_context_without_allowed_domains(), "--context", f"allowedEmailDomains={bad}"])
                self.assertNotEqual(result.returncode, 0, f"synth accepted allowedEmailDomains={bad!r}")
                self.assertIn("allowedEmailDomains", result.stdout + result.stderr)

    def test_d_no_doc_says_the_stack_does_not_set_it(self) -> None:
        for rel in ("README.md", "infra/README.md", "docs/REVIEW-GUIDE.md", "deploy/dts/README.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(doc=rel):
                for stale in (
                    "the stack does not set for you yet",
                    "the CDK stack does not set it yet",
                    "the CDK stack does\n   not yet",
                    "Runtime variables the stack does not set\n\n`AppStack` injects the table and bucket names the API needs. Three",
                ):
                    self.assertNotIn(stale, text)


def _run_tests() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(TestAllowedEmailDomains)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
