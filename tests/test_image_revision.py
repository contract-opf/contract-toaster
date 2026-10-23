#!/usr/bin/env python3
"""
Issue #42 (private-tracker #613): the source revision is an explicit cache
input of a final metadata layer, every image carries the OCI revision
annotation, and the artifact revision stays separate from any release or
deployment identifier.

The defect: `GET /version` reported a stale stamp after a deploy that really
shipped new code. The publish job passed the right `--build-arg`, and the
build reused cached layers anyway, so the image never baked the new value in.
Docker cache reuse follows instruction inputs, and build args take part in
invalidation only where an instruction actually consumes them. The fix is
structural, not "turn caching off":

  * the revision ARG (`COMMIT_SHA`) is declared and consumed only AFTER every
    dependency-install and copy step of its stage, so a new revision can
    never invalidate the expensive layers above it, and no cached layer
    below the install/copy steps can carry an older revision forward;
  * the final stage ends in a cheap metadata block that takes the revision
    as a direct input: `LABEL org.opencontainers.image.revision=${COMMIT_SHA}`
    (the OCI pre-defined annotation for "source control revision identifier
    for the packaged software"), the `ENV` the API reads, and a `RUN` that
    writes the revision into a file. RUN is the instruction both the legacy
    builder and BuildKit fold consumed build args into the cache key of, and
    it produces a FILESYSTEM layer, so a warm-cache rebuild with a different
    revision demonstrably produces a different layer, not just a different
    config blob;
  * the revision is the full 40-hex commit SHA, passed by every producer:
    `ci-pipeline.yml` and the CDK CodeBuild buildspec in
    `infra/lib/nested/cicd-stack.ts` for the AWS image,
    `dts-image-publish.yml` for the two DTS images. A short SHA is a prefix,
    not an immutable name;
  * `VERSION` (`<short-sha>-<UTC build time>`) is the release/build
    identifier and travels in its own ARG, ENV and
    `org.opencontainers.image.version` label. Nothing bakes a deployment
    identifier: which deployment event launched an image is a runtime fact,
    not an artifact fact.

The frontend image needs one carve-out, and it is deliberate. Vite inlines
`import.meta.env.VITE_*` at BUILD time (issue #652), so the revision must
reach the build stage before `RUN npm run build`. The rule still holds there
in the form that matters: in that stage too it arrives after `npm ci` and
after the last COPY, so the only layers it invalidates are the two that are
supposed to change with the commit (the ENV and the bundle build).

The image digest is retained as before: `IMAGE_DIGEST` stays a declared
ARG/ENV on the backend images (it is only knowable after push, so the deploy
supplies it at runtime), and the AWS pipeline still signs and promotes by
digest.

`test_warm_cache_builds_report_different_revisions` is the end-to-end
regression check. It needs Docker and a few minutes, so it runs only with
CT_IMAGE_BUILD_TEST=1 and is reported as skipped otherwise.

Run: python3 -m pytest tests/test_image_revision.py -q
     python3 tests/test_image_revision.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

AWS_BACKEND = REPO_ROOT / "backend" / "Dockerfile"
DTS_BACKEND = REPO_ROOT / "deploy" / "dts" / "backend.Dockerfile"
DTS_FRONTEND = REPO_ROOT / "deploy" / "dts" / "frontend.Dockerfile"
DOCKERFILES = (AWS_BACKEND, DTS_BACKEND, DTS_FRONTEND)

CI_PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci-pipeline.yml"
DTS_PUBLISH = REPO_ROOT / ".github" / "workflows" / "dts-image-publish.yml"
# The AWS backend image's second producer: the CDK CodeBuild buildspec builds
# the same backend/Dockerfile on every push to main.
CICD_STACK = REPO_ROOT / "infra" / "lib" / "nested" / "cicd-stack.ts"

REVISION_ARG = "COMMIT_SHA"
RELEASE_ARG = "VERSION"
OCI_REVISION = "org.opencontainers.image.revision"
OCI_VERSION = "org.opencontainers.image.version"

# The file the final RUN writes. Asserted by path so the warm-cache test can
# read it back out of the built image.
REVISION_FILE = "/etc/image-revision"

# RUN commands that install dependencies. A revision consumed above one of
# these would re-run it on every commit; a cached one below it would hide it.
DEPENDENCY_INSTALL = re.compile(
    r"\b(pip3?\s+install|npm\s+(ci|install)|yarn\s+install|pnpm\s+install|"
    r"apt-get\s+install|apk\s+add|uv\s+(pip|sync))\b"
)


@dataclass
class Instruction:
    keyword: str
    args: str
    line: int


@dataclass
class Stage:
    base: str
    instructions: list[Instruction] = field(default_factory=list)


@dataclass
class Dockerfile:
    path: Path
    global_args: list[Instruction]
    stages: list[Stage]

    @property
    def final(self) -> Stage:
        return self.stages[-1]

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(REPO_ROOT))


def parse_dockerfile(path: Path) -> Dockerfile:
    """Instructions grouped by stage, with continuations joined and comments
    dropped. Only as much of the grammar as these three files use."""
    global_args: list[Instruction] = []
    stages: list[Stage] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if buffer and stripped.startswith("#"):
            continue  # a comment line inside a continuation
        if not buffer:
            start = number
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        buffer += stripped
        keyword, _, args = buffer.partition(" ")
        instruction = Instruction(keyword.upper(), args.strip(), start)
        buffer = ""
        if instruction.keyword == "FROM":
            stages.append(Stage(base=instruction.args))
        elif not stages:
            global_args.append(instruction)
        else:
            stages[-1].instructions.append(instruction)
    return Dockerfile(path, global_args, stages)


def references(text: str, name: str) -> bool:
    return re.search(rf"\$\{{?{name}\b", text) is not None


def consumes(instruction: Instruction, name: str) -> bool:
    """True when the instruction declares or reads the build arg."""
    if instruction.keyword == "ARG":
        return re.match(rf"{name}\b", instruction.args) is not None
    return references(instruction.args, name)


def is_install_or_copy(instruction: Instruction) -> bool:
    if instruction.keyword in ("COPY", "ADD"):
        return True
    return instruction.keyword == "RUN" and DEPENDENCY_INSTALL.search(instruction.args) is not None


def label_pairs(stage: Stage) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for instruction in stage.instructions:
        if instruction.keyword != "LABEL":
            continue
        for key, value in re.findall(r'([\w.\-]+)=("[^"]*"|\S+)', instruction.args):
            pairs[key] = value.strip('"')
    return pairs


def env_pairs(stage: Stage) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for instruction in stage.instructions:
        if instruction.keyword != "ENV":
            continue
        for key, value in re.findall(r'(\w+)=("[^"]*"|\S+)', instruction.args):
            pairs[key] = value.strip('"')
    return pairs


class RevisionLayerPlacement(unittest.TestCase):
    """The revision is consumed only after every install and copy step."""

    def test_no_revision_arg_before_the_first_stage(self) -> None:
        # A pre-FROM ARG is in scope for every FROM line; the old AWS
        # Dockerfile declared all three metadata ARGs there and consumed them
        # straight after FROM, ABOVE the pip install.
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            offenders = [i.line for i in parsed.global_args if consumes(i, REVISION_ARG)]
            self.assertEqual(
                offenders, [],
                f"{parsed.rel}: {REVISION_ARG} declared before the first FROM "
                f"(line {offenders}); declare it in the final metadata block",
            )

    def test_revision_follows_every_install_and_copy_step(self) -> None:
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            for index, stage in enumerate(parsed.stages):
                first_use = next(
                    (i for i in stage.instructions if consumes(i, REVISION_ARG)), None
                )
                if first_use is None:
                    continue
                late = [
                    i for i in stage.instructions
                    if i.line > first_use.line and is_install_or_copy(i)
                ]
                self.assertEqual(
                    late, [],
                    f"{parsed.rel} stage {index} ({stage.base}): {REVISION_ARG} is "
                    f"consumed at line {first_use.line}, above install/copy step(s) "
                    f"at line(s) {[i.line for i in late]} - a new revision then "
                    f"re-runs them, and their cached copies can carry an older one",
                )

    def test_only_the_frontend_build_stage_consumes_it_before_the_final_stage(self) -> None:
        # Vite inlines VITE_COMMIT_SHA at build time (#652), so the frontend
        # build stage must see it before `npm run build`. Nothing else may.
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            for index, stage in enumerate(parsed.stages[:-1]):
                used = [i for i in stage.instructions if consumes(i, REVISION_ARG)]
                if not used:
                    continue
                self.assertEqual(
                    path, DTS_FRONTEND,
                    f"{parsed.rel} stage {index}: only the final stage may consume "
                    f"{REVISION_ARG}",
                )
                after = [
                    i for i in stage.instructions if i.line > used[0].line
                ]
                kinds = {(i.keyword, i.args) for i in after}
                self.assertTrue(
                    all(k == "ENV" or (k == "RUN" and a == "npm run build")
                        or (k == "ARG") for k, a in kinds),
                    f"{parsed.rel} stage {index}: after {REVISION_ARG} only the "
                    f"VITE_* ENV and `RUN npm run build` may follow, got {kinds}",
                )

    def test_final_stage_ends_in_the_metadata_block(self) -> None:
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            final = parsed.final
            arg = next(
                (i for i in final.instructions
                 if i.keyword == "ARG" and consumes(i, REVISION_ARG)),
                None,
            )
            self.assertIsNotNone(
                arg, f"{parsed.rel}: final stage never declares ARG {REVISION_ARG}"
            )
            assert arg is not None
            self.assertRegex(
                arg.args, rf"^{REVISION_ARG}=unknown$",
                f"{parsed.rel}: ARG {REVISION_ARG} must default to `unknown`, the "
                f"/version fallback, so an un-parameterised build degrades cleanly",
            )
            after = [i.keyword for i in final.instructions if i.line > arg.line]
            allowed = {"ARG", "ENV", "LABEL", "RUN", "WORKDIR", "EXPOSE", "CMD",
                       "ENTRYPOINT", "USER"}
            self.assertTrue(
                set(after) <= allowed,
                f"{parsed.rel}: only metadata may follow ARG {REVISION_ARG}, got "
                f"{sorted(set(after) - allowed)}",
            )

    def test_final_stage_writes_the_revision_into_a_layer(self) -> None:
        # ENV and LABEL change only the image config. The RUN makes the
        # revision a direct input of a real filesystem layer, whose cache
        # key carries the consumed build arg on both builders.
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            runs = [
                i for i in parsed.final.instructions
                if i.keyword == "RUN" and references(i.args, REVISION_ARG)
                and REVISION_FILE in i.args
            ]
            self.assertEqual(
                len(runs), 1,
                f"{parsed.rel}: the final stage must have exactly one RUN that "
                f"writes ${{{REVISION_ARG}}} to {REVISION_FILE}",
            )


class OciAnnotations(unittest.TestCase):
    def test_final_stage_labels_the_oci_revision(self) -> None:
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            labels = label_pairs(parsed.final)
            self.assertIn(
                OCI_REVISION, labels,
                f"{parsed.rel}: final stage carries no {OCI_REVISION} label",
            )
            self.assertRegex(
                labels[OCI_REVISION], rf"^\$\{{{REVISION_ARG}\}}$",
                f"{parsed.rel}: {OCI_REVISION} must be bound to the "
                f"{REVISION_ARG} build arg and nothing else",
            )
            label_line = next(
                i.line for i in parsed.final.instructions
                if i.keyword == "LABEL" and OCI_REVISION in i.args
            )
            arg_line = next(
                i.line for i in parsed.final.instructions
                if i.keyword == "ARG" and consumes(i, REVISION_ARG)
            )
            self.assertGreater(
                label_line, arg_line,
                f"{parsed.rel}: the label must follow ARG {REVISION_ARG} in the "
                f"final stage, or it expands to an empty string",
            )


class RevisionAndReleaseAreSeparate(unittest.TestCase):
    """Which artifact is this? vs which build/release/deployment made it?"""

    def test_revision_fields_carry_only_the_revision(self) -> None:
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            labels = label_pairs(parsed.final)
            self.assertFalse(references(labels.get(OCI_REVISION, ""), RELEASE_ARG))
            if OCI_VERSION in labels:
                self.assertRegex(
                    labels[OCI_VERSION], rf"^\$\{{{RELEASE_ARG}\}}$",
                    f"{parsed.rel}: {OCI_VERSION} must carry the release "
                    f"identifier ({RELEASE_ARG}) alone",
                )
            for stage in parsed.stages:
                for key, value in env_pairs(stage).items():
                    revision = references(value, REVISION_ARG)
                    release = references(value, RELEASE_ARG)
                    self.assertFalse(
                        revision and release,
                        f"{parsed.rel}: ENV {key}={value} mixes the artifact "
                        f"revision with the release identifier",
                    )

    def test_release_identifier_has_its_own_label(self) -> None:
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            self.assertIn(
                OCI_VERSION, label_pairs(parsed.final),
                f"{parsed.rel}: the release identifier needs its own "
                f"{OCI_VERSION} label, distinct from {OCI_REVISION}",
            )

    def test_no_deployment_identifier_is_baked_in(self) -> None:
        # The deployment event that launched an image is a runtime fact.
        # Baking one in would make "which artifact" answer "which deploy".
        for path in DOCKERFILES:
            parsed = parse_dockerfile(path)
            names = [
                i.args.split("=")[0]
                for stage in parsed.stages for i in stage.instructions
                if i.keyword in ("ARG", "ENV")
            ] + [i.args.split("=")[0] for i in parsed.global_args]
            baked = [n for n in names if re.search(r"DEPLOY|RELEASE", n)]
            self.assertEqual(
                baked, [], f"{parsed.rel}: deployment identity baked in: {baked}"
            )

    def test_image_digest_is_retained_on_the_backend_images(self) -> None:
        for path in (AWS_BACKEND, DTS_BACKEND):
            parsed = parse_dockerfile(path)
            env = env_pairs(parsed.final)
            self.assertEqual(env.get("IMAGE_DIGEST"), "${IMAGE_DIGEST}", parsed.rel)
            self.assertEqual(env.get(REVISION_ARG), f"${{{REVISION_ARG}}}", parsed.rel)
            self.assertEqual(env.get(RELEASE_ARG), f"${{{RELEASE_ARG}}}", parsed.rel)


class WorkflowsPassTheImmutableRevision(unittest.TestCase):
    def test_ci_pipeline_passes_the_full_sha(self) -> None:
        text = CI_PIPELINE.read_text(encoding="utf-8")
        build = re.search(
            r"- name: Build container image \(STEP E\)\n(.*?)(?=\n      - name:)",
            text, re.DOTALL,
        )
        self.assertIsNotNone(build, "ci-pipeline.yml: STEP E build step not found")
        assert build is not None
        body = build.group(1)
        self.assertRegex(body, r'--build-arg COMMIT_SHA="\$\{COMMIT_SHA\}"')
        assignment = re.search(r"^\s*COMMIT_SHA=(.+)$", body, re.MULTILINE)
        self.assertIsNotNone(assignment, "COMMIT_SHA is never assigned in STEP E")
        assert assignment is not None
        self.assertEqual(
            assignment.group(1).strip(), "$(git rev-parse HEAD)",
            "ci-pipeline.yml: the revision build arg must be the full commit "
            "SHA, not `--short` (a prefix is not an immutable name)",
        )

    def test_dts_publish_passes_the_full_sha(self) -> None:
        text = DTS_PUBLISH.read_text(encoding="utf-8")
        arg = re.search(r'--build-arg "COMMIT_SHA=\$\{\{ steps\.meta\.outputs\.(\w+) \}\}"', text)
        self.assertIsNotNone(arg, "dts-image-publish.yml: COMMIT_SHA build arg not found")
        assert arg is not None
        output = arg.group(1)
        producer = re.search(rf'echo "{output}=(.+?)" >> "\$GITHUB_OUTPUT"', text)
        self.assertIsNotNone(producer, f"no step writes the `{output}` output")
        assert producer is not None
        self.assertEqual(
            producer.group(1), "${TARGET_SHA}",
            "dts-image-publish.yml: the revision build arg must be the full "
            "TARGET_SHA (the commit that passed CI), not a truncated one",
        )

    def test_codebuild_buildspec_passes_the_full_sha(self) -> None:
        text = CICD_STACK.read_text(encoding="utf-8")
        build = re.search(
            r"docker build [^'\n]*--build-arg COMMIT_SHA=\$COMMIT_SHA[^'\n]*backend/",
            text,
        )
        self.assertIsNotNone(
            build, "cicd-stack.ts: buildspec docker build of backend/ with "
            "--build-arg COMMIT_SHA=$COMMIT_SHA not found",
        )
        assignments = re.findall(r"^\s*'COMMIT_SHA=(.+)',\s*$", text, re.MULTILINE)
        self.assertEqual(
            assignments, ["$(git rev-parse HEAD)"],
            "cicd-stack.ts: the CodeBuild buildspec's revision build arg must "
            "be the full commit SHA, not `--short` (a prefix is not an "
            "immutable name)",
        )


DOCKER = shutil.which("docker")


def docker(*args: str, check: bool = True) -> str:
    """Run the resolved docker binary; the arguments are this file's own."""
    assert DOCKER is not None
    return subprocess.run(  # noqa: S603
        [DOCKER, *args], check=check, capture_output=True, text=True
    ).stdout.strip()


@unittest.skipUnless(
    os.environ.get("CT_IMAGE_BUILD_TEST") == "1" and DOCKER,
    "set CT_IMAGE_BUILD_TEST=1 with a running Docker daemon to build the image",
)
class WarmCacheBuild(unittest.TestCase):
    """Two warm-cache builds with different revisions must report different
    revisions in every place the revision is recorded."""

    def _build(self, tag: str, revision: str) -> None:
        docker(
            "build", "-q", "-f", str(DTS_BACKEND),
            "--build-arg", f"VERSION={revision[:7]}-20261023000000",
            "--build-arg", f"COMMIT_SHA={revision}",
            "-t", tag, str(REPO_ROOT),
        )

    def test_warm_cache_builds_report_different_revisions(self) -> None:
        run = uuid.uuid4().hex[:8]
        first, second = "a" * 40, "b" * 40
        tags = [f"ct-image-revision-{run}:{n}" for n in ("first", "second")]
        try:
            self._build(tags[0], first)  # warms the cache
            self._build(tags[1], second)
            for tag, revision in zip(tags, (first, second), strict=True):
                label = docker(
                    "inspect", "--format",
                    f'{{{{index .Config.Labels "{OCI_REVISION}"}}}}', tag,
                )
                self.assertEqual(label, revision)
                env = docker("inspect", "--format", "{{json .Config.Env}}", tag)
                self.assertIn(f'"COMMIT_SHA={revision}"', env)
                written = docker("run", "--rm", "--entrypoint", "cat", tag, REVISION_FILE)
                self.assertEqual(written, revision)
            layers = [docker("inspect", "--format", "{{json .RootFS.Layers}}", t) for t in tags]
            self.assertNotEqual(layers[0], layers[1], "the revision layer did not change")
        finally:
            docker("rmi", "-f", *tags, check=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
