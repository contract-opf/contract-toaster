#!/usr/bin/env python3
"""
The Docker Compose target's MinIO image is built from upstream source and
published under its release tag only (deploy/dts/README.md#minio-image).

MinIO stopped publishing community images: quay.io and Docker Hub both answer
401 for `minio/minio:RELEASE.2025-04-08T15-41-24Z`, so a fresh install could
not start, and a pull-latest restart aborted on that one image before the new
backend and frontend could roll out. deploy/dts/minio/Dockerfile rebuilds the
same release, and .github/workflows/dts-image-publish.yml publishes it as
`contract-toaster-dts-minio:<release>`.

What this file pins:

  1. The Dockerfile names one upstream release and the commit its tag points
     at, and the build verifies both (a moved tag cannot slip in).
  2. Both compose files use THAT release, and neither still names a
     discontinued upstream image.
  3. The publish job reads the tag from the Dockerfile, pushes only that tag
     (no :latest), refuses to overwrite it, and the deploy job waits for it.
  4. The AGPL source location written in the Dockerfile and the README is the
     release actually built.

`test_built_image_reports_the_release` builds the image and runs
`minio --version`. It needs Docker and a few minutes, so it runs only with
CT_IMAGE_BUILD_TEST=1 and is reported as skipped otherwise.

Run: python3 tests/test_dts_minio_image.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DTS = REPO_ROOT / "deploy" / "dts"
DOCKERFILE = DTS / "minio" / "Dockerfile"
LOCAL_COMPOSE = DTS / "docker-compose.yml"
COOLIFY_COMPOSE = DTS / "docker-compose.coolify.yml"
README = DTS / "README.md"
PUBLISH = REPO_ROOT / ".github" / "workflows" / "dts-image-publish.yml"

TAG_RE = r"RELEASE\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def global_arg(name: str) -> str:
    m = re.search(rf"^ARG {name}=(\S+)$", read(DOCKERFILE), re.MULTILINE)
    assert m is not None, f"{DOCKERFILE.relative_to(REPO_ROOT)}: no ARG {name}=…"
    return m.group(1)


def service_block(compose: Path, name: str) -> str:
    """The text of one top-level service, up to the next one."""
    m = re.search(
        rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", read(compose), re.MULTILINE | re.DOTALL
    )
    assert m is not None, f"{compose.name}: no `{name}` service"
    return m.group(1)


def job_block(name: str) -> str:
    m = re.search(
        rf"^  {name}:\n(.*?)(?=^  \S|\Z)", read(PUBLISH), re.MULTILINE | re.DOTALL
    )
    assert m is not None, f"dts-image-publish.yml: no `{name}` job"
    return m.group(1)


class DockerfilePinsOneRelease(unittest.TestCase):
    def test_tag_and_commit_are_pinned(self) -> None:
        self.assertRegex(global_arg("MINIO_TAG"), rf"^{TAG_RE}$")
        self.assertRegex(global_arg("MINIO_COMMIT"), r"^[0-9a-f]{40}$")

    def test_build_verifies_commit_and_version(self) -> None:
        text = read(DOCKERFILE)
        self.assertIn('test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"', text)
        self.assertIn("MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go", text)
        self.assertIn(
            'grep -qF "minio version ${MINIO_TAG} (commit-id=${MINIO_COMMIT})"', text
        )

    def test_no_prebuilt_binary_download(self) -> None:
        # dl.min.io binaries can be withdrawn the same way the images were.
        instructions = [
            line for line in read(DOCKERFILE).splitlines()
            if not line.lstrip().startswith("#")
        ]
        self.assertFalse([line for line in instructions if "dl.min.io" in line])


class ComposeFilesUseThatRelease(unittest.TestCase):
    def test_local_compose_builds_it(self) -> None:
        block = service_block(LOCAL_COMPOSE, "minio")
        self.assertRegex(block, r"(?m)^    build:\n      context: minio$")
        self.assertRegex(
            block,
            rf"(?m)^    image: contract-toaster-dts-minio:{re.escape(global_arg('MINIO_TAG'))}$",
        )

    def test_coolify_compose_pulls_the_published_tag(self) -> None:
        block = service_block(COOLIFY_COMPOSE, "minio")
        want = (
            "ghcr.io/${GHCR_OWNER}/contract-toaster-dts-minio:"
            + global_arg("MINIO_TAG")
        )
        self.assertRegex(block, rf"(?m)^    image: {re.escape(want)}$")

    def test_no_discontinued_upstream_image(self) -> None:
        for compose in (LOCAL_COMPOSE, COOLIFY_COMPOSE):
            self.assertNotRegex(
                read(compose),
                r"(?m)^\s*image:\s*(quay\.io/|docker\.io/)?minio/",
                f"{compose.name} still pulls an upstream MinIO image",
            )


class PublishJobTreatsTheTagAsImmutable(unittest.TestCase):
    def test_tag_is_read_from_the_dockerfile(self) -> None:
        job = job_block("publish-minio")
        self.assertIn("DOCKERFILE: deploy/dts/minio/Dockerfile", job)
        self.assertIn("sed -n 's/^ARG MINIO_TAG=//p'", job)

    def test_pushes_only_the_release_tag(self) -> None:
        job = job_block("publish-minio")
        pushes = re.findall(r"(?m)docker push (.+)$", job)
        self.assertEqual(
            pushes, ['"${{ steps.meta.outputs.image }}:${{ steps.meta.outputs.tag }}"']
        )
        tags = re.findall(r"(?m)^\s+-t (.+?)(?: \\)?$", job)
        self.assertEqual(
            tags, ['"${{ steps.meta.outputs.image }}:${{ steps.meta.outputs.tag }}"']
        )

    def test_build_and_push_only_when_absent(self) -> None:
        job = job_block("publish-minio")
        for step in ("Build MinIO image", "Push MinIO image"):
            m = re.search(
                rf"- name: {step}[^\n]*\n\s+if: steps\.existing\.outputs\.publish == 'true'",
                job,
            )
            self.assertIsNotNone(m, f"`{step}` is not gated on the existence check")
        self.assertIn("title=MinIO tag is immutable", job)

    def test_build_is_stamped_like_the_app_images(self) -> None:
        job = job_block("publish-minio")
        self.assertIn('--build-arg "COMMIT_SHA=${{ steps.meta.outputs.revision }}"', job)
        self.assertIn('echo "revision=${TARGET_SHA}" >> "$GITHUB_OUTPUT"', job)
        self.assertIn('--build-arg "VERSION=${{ steps.meta.outputs.tag }}"', job)

    def test_deploy_waits_for_it(self) -> None:
        self.assertRegex(job_block("deploy"), r"(?m)^    needs: \[publish, publish-minio\]$")


class SourceLocationIsTheReleaseBuilt(unittest.TestCase):
    def test_agpl_source_url_names_the_pinned_tag_and_commit(self) -> None:
        tag, commit = global_arg("MINIO_TAG"), global_arg("MINIO_COMMIT")
        url = f"https://github.com/minio/minio/tree/{tag}"
        for path in (DOCKERFILE, README):
            text = read(path)
            self.assertIn(url, text, f"{path.name}: AGPL source URL is stale")
            self.assertIn(commit, text, f"{path.name}: upstream commit is stale")
        self.assertIn("COPY --from=build /src/LICENSE /src/CREDITS /licenses/", read(DOCKERFILE))


DOCKER = shutil.which("docker")


@unittest.skipUnless(
    os.environ.get("CT_IMAGE_BUILD_TEST") == "1" and DOCKER,
    "set CT_IMAGE_BUILD_TEST=1 with a running Docker daemon to build the image",
)
class BuiltImage(unittest.TestCase):
    def test_built_image_reports_the_release(self) -> None:
        assert DOCKER is not None
        tag = f"ct-dts-minio-{uuid.uuid4().hex[:8]}:test"
        try:
            subprocess.run(  # noqa: S603
                [DOCKER, "build", "-q", "-t", tag, str(DOCKERFILE.parent)],
                check=True, capture_output=True, text=True,
            )
            out = subprocess.run(  # noqa: S603
                [DOCKER, "run", "--rm", tag, "--version"],
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn(
                f"minio version {global_arg('MINIO_TAG')} "
                f"(commit-id={global_arg('MINIO_COMMIT')})",
                out,
            )
        finally:
            subprocess.run([DOCKER, "rmi", "-f", tag], check=False, capture_output=True)  # noqa: S603


if __name__ == "__main__":
    unittest.main(verbosity=2)
