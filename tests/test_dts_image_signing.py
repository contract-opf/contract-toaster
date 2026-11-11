#!/usr/bin/env python3
"""
The DTS images are signed (cosign, keyless) by the publish workflow, and the
box's autopull script knows which signer to expect.

.github/workflows/dts-image-publish.yml signs each pushed backend and frontend
digest; deploy/dts/autopull/coolify-autopull.sh verifies a NEW digest against
that signer (warn by default, `enforce` to refuse; its behaviour is pinned in
tests/test_dts_autopull.py). This file pins the two ends that have to agree, and
the least-privilege shape of the signing:

  1. Only the `publish` job may mint an OIDC token (`id-token: write`). The
     workflow-level permissions, `publish-minio` and `deploy` must not carry it.
  2. The publish job signs the DIGEST it just pushed (not a tag), after the
     push, with `cosign sign --yes`, and installs cosign first.
  3. The signer identity autopull derives by default is that workflow file, on
     main, for the repository autopull is told to watch.

Run: python3 tests/test_dts_image_signing.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISH = REPO_ROOT / ".github" / "workflows" / "dts-image-publish.yml"
SCRIPT = REPO_ROOT / "deploy" / "dts" / "autopull" / "coolify-autopull.sh"
README = REPO_ROOT / "deploy" / "dts" / "README.md"


def workflow() -> dict:
    return yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))


def step_names(job: dict) -> list[str]:
    return [step.get("name", "") for step in job["steps"]]


class PublishWorkflowSigns(unittest.TestCase):
    def test_only_the_publish_job_may_mint_an_oidc_token(self) -> None:
        wf = workflow()
        self.assertNotIn("id-token", wf.get("permissions") or {})
        self.assertEqual(wf["jobs"]["publish"]["permissions"].get("id-token"), "write")
        for name, job in wf["jobs"].items():
            if name == "publish":
                continue
            with self.subTest(job=name):
                self.assertNotIn("id-token", job.get("permissions") or {})

    def test_the_job_block_keeps_what_the_workflow_block_granted(self) -> None:
        # A job-level `permissions:` replaces the workflow's, so losing either
        # of these would break the checkout or the push.
        job = workflow()["jobs"]["publish"]["permissions"]
        self.assertEqual(job.get("contents"), "read")
        self.assertEqual(job.get("packages"), "write")

    def test_it_signs_the_pushed_digest_after_pushing(self) -> None:
        job = workflow()["jobs"]["publish"]
        names = step_names(job)
        push = next(i for i, n in enumerate(names) if n.startswith("Push image"))
        install = next(i for i, n in enumerate(names) if n == "Install cosign")
        sign = next(i for i, n in enumerate(names) if n.startswith("Sign the pushed digest"))
        self.assertLess(push, install)
        self.assertLess(install, sign)
        steps = job["steps"]
        self.assertEqual(steps[push].get("id"), "push")
        self.assertIn("digest=${digest}", steps[push]["run"])
        self.assertTrue(steps[install]["uses"].startswith("sigstore/cosign-installer@"))
        self.assertIn("cosign sign --yes", steps[sign]["run"])
        subject = steps[sign]["env"]["SUBJECT"]
        self.assertIn("steps.meta.outputs.image", subject)
        self.assertIn("@${{ steps.push.outputs.digest }}", subject)
        self.assertNotIn("steps.meta.outputs.sha", subject, "sign the digest, never a tag")

    def test_the_minio_job_is_not_signed(self) -> None:
        # Not watched by autopull; immutable release tag. Say so if that changes.
        job = workflow()["jobs"]["publish-minio"]
        self.assertFalse([n for n in step_names(job) if "cosign" in n.lower() or "sign" in n.lower()])


class AutopullExpectsThatSigner(unittest.TestCase):
    def test_default_identity_is_this_workflow_on_main(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(PUBLISH.is_file())
        match = re.search(r'regexp="(\^https://github[^"]+)"', text)
        self.assertIsNotNone(match, "the default signer identity regexp is gone")
        identity = match.group(1)  # type: ignore[union-attr]
        self.assertIn("dts-image-publish", identity)
        self.assertIn(PUBLISH.name.replace(".", r"\\."), identity)
        self.assertIn("@refs/heads/main", identity)
        self.assertIn("${GITHUB_REPO//./", identity)

    def test_default_issuer_is_github_actions(self) -> None:
        self.assertIn(
            "https://token.actions.githubusercontent.com",
            SCRIPT.read_text(encoding="utf-8"),
        )

    def test_the_default_mode_is_warn(self) -> None:
        # The default must never stop an existing box from pulling.
        self.assertIn('COSIGN_MODE="${AUTOPULL_COSIGN:-warn}"', SCRIPT.read_text(encoding="utf-8"))

    def test_the_readme_says_how_to_flip_it(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertIn("Signature verification (cosign)", text)
        self.assertIn("AUTOPULL_COSIGN=enforce", text)
        self.assertIn("cosign verify", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
