#!/usr/bin/env python3
"""
The hardened WAF lets a .docx upload through and actually enforces its
request-size cap (QA 2026-10-31 report item 14).

WHY THIS GATE EXISTS
--------------------
`infra/lib/nested/waf-stack.ts` had two body-size defects:

  1. `AWSManagedRulesCommonRuleSet`'s `SizeRestrictions_BODY` rule blocks any
     request body over 8 KB. Every .docx upload is larger, so on the hardened
     profile every review submission and playbook upload was refused at the
     WAF.
  2. The custom 50 MiB `SizeConstraintStatement` on the body could never
     match: a regional WAF inspects at most the first 8 KB of a body (64 KB
     with an association config), and with `oversizeHandling` unset only the
     inspected part is measured -- never more than 50 MiB.

The fix, checked here against the synthesized WebACL by a small evaluator of
the statements the template carries (so the assertion is about what the
rules match, not how they are spelled):

  A. `SizeRestrictions_BODY` is overridden to COUNT inside the managed group,
     and a separate rule BLOCKS on its label everywhere EXCEPT a POST to one
     of the upload routes the backend actually serves
     (`POST /api/reviews`, `POST /api/reviews/preflight`,
     `POST /api/admin/playbooks`, `POST /api/admin/playbooks/{id}/versions`).
  B. The size cap reads the `Content-Length` header, which states the whole
     body's size, and blocks exactly when it exceeds 50 MiB; no body
     `SizeConstraintStatement` without `oversizeHandling` remains.

Run: python3 tests/test_infra_waf_upload_body.py
Exit 0 = pass, 1 = fail.
"""

import json
import random
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
sys.path.insert(0, str(REPO_ROOT / "tests"))

from infra_synth_helper import NEUTRAL_CDK_CONTEXT  # noqa: E402

SIZE_LABEL = "awswaf:managed:aws:core-rule-set:SizeRestrictions_Body"
CAP_BYTES = 50 * 1024 * 1024

UPLOAD_ROUTES = [
    "/api/reviews",
    "/api/reviews/preflight",
    "/api/admin/playbooks",
    "/api/admin/playbooks/synthetic-generic/versions",
]
NOT_UPLOAD_ROUTES = [
    "/api/reviews/abc123/cancel",
    "/api/audit",
    "/api/admin/settings",
    "/api/reviewsx",
]


def _synth() -> None:
    result = subprocess.run(  # noqa: S603
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],  # noqa: S607
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"cdk synth failed.\n{result.stderr[-1200:]}")


def _web_acl() -> dict:
    for template_path in sorted((INFRA / "cdk.out").glob("*.nested.template.json")):
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") == "AWS::WAFv2::WebACL":
                return resource["Properties"]
    raise AssertionError("no AWS::WAFv2::WebACL in the synthesized templates (hardened profile)")


# --- a minimal evaluator for the statement kinds these rules use -------------

def _transform(value: str, transformations: list[dict]) -> str:
    for t in sorted(transformations, key=lambda t: t["Priority"]):
        if t["Type"] == "LOWERCASE":
            value = value.lower()
        elif t["Type"] != "NONE":
            raise AssertionError(f"evaluator does not model transformation {t['Type']}")
    return value


def _field(request: dict, field: dict) -> str:
    if "UriPath" in field:
        return request["path"]
    if "Method" in field:
        return request["method"]
    if "SingleHeader" in field:
        return request["headers"].get(field["SingleHeader"]["Name"].lower(), "")
    raise AssertionError(f"evaluator does not model field {field}")


def _matches(statement: dict, request: dict) -> bool:
    if "AndStatement" in statement:
        return all(_matches(s, request) for s in statement["AndStatement"]["Statements"])
    if "OrStatement" in statement:
        return any(_matches(s, request) for s in statement["OrStatement"]["Statements"])
    if "NotStatement" in statement:
        return not _matches(statement["NotStatement"]["Statement"], request)
    if "LabelMatchStatement" in statement:
        return statement["LabelMatchStatement"]["Key"] in request["labels"]
    if "ByteMatchStatement" in statement:
        s = statement["ByteMatchStatement"]
        value = _transform(_field(request, s["FieldToMatch"]), s["TextTransformations"])
        needle = s["SearchString"]
        return {
            "EXACTLY": value == needle,
            "STARTS_WITH": value.startswith(needle),
            "ENDS_WITH": value.endswith(needle),
            "CONTAINS": needle in value,
        }[s["PositionalConstraint"]]
    if "RegexMatchStatement" in statement:
        s = statement["RegexMatchStatement"]
        value = _transform(_field(request, s["FieldToMatch"]), s["TextTransformations"])
        return re.search(s["RegexString"], value) is not None
    raise AssertionError(f"evaluator does not model statement {list(statement)}")


def _request(method: str, path: str, labels=(), content_length: str | None = None) -> dict:
    headers = {} if content_length is None else {"content-length": content_length}
    return {"method": method, "path": path, "labels": set(labels), "headers": headers}


def _blocking_rules(acl: dict) -> list[dict]:
    return [r for r in acl["Rules"] if "Block" in r.get("Action", {})]


def _blocked(acl: dict, request: dict, exclude_rate_rules: bool = True) -> bool:
    for rule in _blocking_rules(acl):
        statement = rule["Statement"]
        if "RateBasedStatement" in statement and exclude_rate_rules:
            continue
        if _matches(statement, request):
            return True
    return False


class TestWafUploadBody(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _synth()
        cls.acl = _web_acl()

    def _common_rule_set(self) -> dict:
        rule = next(r for r in self.acl["Rules"] if r["Name"] == "AWSManagedRulesCommonRuleSet")
        return rule

    def test_a_size_restrictions_body_is_counted_in_the_managed_group(self) -> None:
        group = self._common_rule_set()["Statement"]["ManagedRuleGroupStatement"]
        overrides = {o["Name"]: o["ActionToUse"] for o in group.get("RuleActionOverrides", [])}
        self.assertEqual(overrides.get("SizeRestrictions_BODY"), {"Count": {}})

    def test_a_label_rule_runs_after_the_managed_group(self) -> None:
        common_priority = self._common_rule_set()["Priority"]
        label_rules = [
            r for r in _blocking_rules(self.acl)
            if SIZE_LABEL in json.dumps(r["Statement"])
        ]
        self.assertEqual(len(label_rules), 1, "exactly one rule acts on the SizeRestrictions_Body label")
        self.assertGreater(label_rules[0]["Priority"], common_priority)

    def test_a_upload_posts_with_a_large_body_pass(self) -> None:
        for path in UPLOAD_ROUTES:
            with self.subTest(path=path):
                req = _request("POST", path, labels=[SIZE_LABEL], content_length="400000")
                self.assertFalse(_blocked(self.acl, req))

    def test_a_large_bodies_elsewhere_are_still_blocked(self) -> None:
        cases = [("POST", p) for p in NOT_UPLOAD_ROUTES] + [("PUT", p) for p in UPLOAD_ROUTES]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                req = _request(method, path, labels=[SIZE_LABEL], content_length="400000")
                self.assertTrue(_blocked(self.acl, req))

    def test_a_small_bodies_are_not_blocked(self) -> None:
        for path in UPLOAD_ROUTES + NOT_UPLOAD_ROUTES:
            with self.subTest(path=path):
                self.assertFalse(_blocked(self.acl, _request("POST", path, content_length="512")))

    def test_b_no_unenforceable_body_size_constraint(self) -> None:
        for rule in self.acl["Rules"]:
            dumped = json.dumps(rule["Statement"])
            if '"SizeConstraintStatement"' not in dumped:
                continue
            with self.subTest(rule=rule["Name"]):
                s = rule["Statement"].get("SizeConstraintStatement", {})
                if "Body" in s.get("FieldToMatch", {}):
                    self.assertIn("OversizeHandling", s["FieldToMatch"]["Body"])

    def test_b_content_length_over_the_cap_is_blocked_exactly(self) -> None:
        rng = random.Random(1405)  # noqa: S311 -- reproducible test samples, not crypto
        samples = [0, 1, 999, CAP_BYTES - 1, CAP_BYTES, CAP_BYTES + 1, CAP_BYTES + 9,
                   CAP_BYTES + 10, CAP_BYTES + 99, CAP_BYTES + 100, CAP_BYTES + 999,
                   CAP_BYTES * 2, 99_999_999, 100_000_000, 10**12]
        samples += [rng.randrange(0, 4 * CAP_BYTES) for _ in range(5000)]
        samples += [CAP_BYTES + rng.randrange(-5000, 5000) for _ in range(5000)]
        for n in samples:
            req = _request("POST", "/api/reviews", content_length=str(n))
            if _blocked(self.acl, req) != (n > CAP_BYTES):
                self.fail(f"Content-Length {n}: blocked={_blocked(self.acl, req)}, cap={CAP_BYTES}")
        # A zero-padded length is still the same number to the server.
        self.assertTrue(_blocked(self.acl, _request("POST", "/api/reviews", content_length="0" + str(CAP_BYTES + 1))))


def _run_tests() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(TestWafUploadBody)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
