#!/usr/bin/env python3
"""
deploy/dts/autopull/coolify-autopull.sh: the box rolls the DTS Coolify service
forward itself (deploy/dts/README.md#automatic-roll-forward-from-the-box).

The publish workflow's `deploy` job pushes to Coolify's API from a
GitHub-hosted runner. Behind Cloudflare's Bot Fight Mode that call gets a
challenge page (HTTP 403 "Just a moment...") on every run, so the deployment
never moved on its own. The script runs on the box instead: it pulls the
watched images, and when their digests change it calls Coolify on localhost.

These tests run the real script against fake `docker` and `curl` binaries on
PATH, and pin:

  1. First run records the digests and does NOT restart.
  2. Unchanged digests do nothing.
  3. A new digest restarts through the services restart endpoint with
     latest=true, passes the token in a header FILE (never argv), and records
     the new digests.
  4. Images that disagree on org.opencontainers.image.revision (the publish
     legs are mid-flight) defer without restarting.
  5. A refused restart or a failed pull exits 1 and leaves the state alone,
     so the next run retries.
  6. The digest is read for the watched repository even when the image has
     several RepoDigests, and a registry port is not mistaken for a tag.
  7. The workflow's `deploy` job steps aside in pull mode.

Run: python3 tests/test_dts_autopull.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOPULL = REPO_ROOT / "deploy" / "dts" / "autopull"
SCRIPT = AUTOPULL / "coolify-autopull.sh"
SERVICE = AUTOPULL / "coolify-autopull.service"
ENV_EXAMPLE = AUTOPULL / "coolify-autopull.env.example"
PUBLISH = REPO_ROOT / ".github" / "workflows" / "dts-image-publish.yml"
README = REPO_ROOT / "deploy" / "dts" / "README.md"

BACKEND = "ghcr.io/example/contract-toaster-dts-backend:latest"
FRONTEND = "ghcr.io/example/contract-toaster-dts-frontend:latest"
TOKEN = "test-token-not-a-secret"
UUID = "svc-uuid-123"
BASH = shutil.which("bash")

FAKE_DOCKER = r"""#!/usr/bin/env bash
key() { printf '%s' "$1" | tr '/:' '__'; }
case "$1" in
  pull) [ -e "$FAKE/pull_fail" ] && exit 1; exit 0 ;;
  image)
    fmt="$4"; img="$5"
    case "$fmt" in
      *RepoDigests*) cat "$FAKE/$(key "$img").digests" ;;
      *Labels*) cat "$FAKE/$(key "$img").rev" 2>/dev/null; echo ;;
    esac ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE/curl.argv"
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -H) case "$2" in @*) cat "${2#@}" >> "$FAKE/curl.headerfiles" ;; esac; shift ;;
  esac
  shift
done
[ -n "$out" ] && printf 'body from coolify' > "$out"
printf '%s' "$(cat "$FAKE/http_code" 2>/dev/null || echo 200)"
"""


class Box:
    """A fake box: PATH with fake docker/curl, a state dir and fixtures."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.fake = root / "fake"
        self.state = root / "state" / "deployed"
        self.bin.mkdir()
        self.fake.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

    @staticmethod
    def key(image: str) -> str:
        return image.replace("/", "_").replace(":", "_")

    def publish(self, image: str, digest: str, revision: str, extra: str = "") -> None:
        repo = image.rsplit(":", 1)[0]
        lines = (extra + "\n" if extra else "") + f"{repo}@{digest}\n"
        (self.fake / f"{self.key(image)}.digests").write_text(lines, encoding="utf-8")
        (self.fake / f"{self.key(image)}.rev").write_text(revision, encoding="utf-8")

    def run(self, images: str = f"{BACKEND} {FRONTEND}") -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "AUTOPULL_IMAGES": images,
            "COOLIFY_SERVICE_UUID": UUID,
            "COOLIFY_API_TOKEN": TOKEN,
            "AUTOPULL_STATE_FILE": str(self.state),
        }
        return subprocess.run(  # noqa: S603 (the repo's own script)
            [BASH or "bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
        )

    def curl_calls(self) -> list[str]:
        path = self.fake / "curl.argv"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@unittest.skipUnless(BASH, "needs bash")
class AutopullBehaviour(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.box = Box(Path(self._tmp.name))
        self.box.publish(BACKEND, "sha256:b1", "a" * 40)
        self.box.publish(FRONTEND, "sha256:f1", "a" * 40)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def initialise(self) -> None:
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_first_run_records_and_does_not_restart(self) -> None:
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("initialised", result.stdout)
        self.assertEqual(self.box.curl_calls(), [])
        self.assertEqual(
            self.box.state.read_text(encoding="utf-8"),
            f"{BACKEND} sha256:b1\n{FRONTEND} sha256:f1\n",
        )

    def test_unchanged_does_nothing(self) -> None:
        self.initialise()
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.box.curl_calls(), [])

    def test_new_digest_restarts_with_latest_and_records(self) -> None:
        self.initialise()
        self.box.publish(BACKEND, "sha256:b2", "b" * 40)
        self.box.publish(FRONTEND, "sha256:f2", "b" * 40)
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.box.curl_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("-X POST", calls[0])
        self.assertIn(
            f"http://127.0.0.1:8000/api/v1/services/{UUID}/restart?latest=true", calls[0]
        )
        self.assertNotIn(TOKEN, calls[0], "the token must not be on the command line")
        self.assertIn(
            f"Authorization: Bearer {TOKEN}",
            (self.box.fake / "curl.headerfiles").read_text(encoding="utf-8"),
        )
        self.assertIn("rolled forward to " + "b" * 40, result.stdout)
        self.assertEqual(
            self.box.state.read_text(encoding="utf-8"),
            f"{BACKEND} sha256:b2\n{FRONTEND} sha256:f2\n",
        )

    def test_mixed_revisions_defer(self) -> None:
        self.initialise()
        before = self.box.state.read_text(encoding="utf-8")
        self.box.publish(BACKEND, "sha256:b2", "b" * 40)  # frontend leg not pushed yet
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disagree", result.stdout)
        self.assertEqual(self.box.curl_calls(), [])
        self.assertEqual(self.box.state.read_text(encoding="utf-8"), before)

    def test_refused_restart_is_retried(self) -> None:
        self.initialise()
        before = self.box.state.read_text(encoding="utf-8")
        self.box.publish(BACKEND, "sha256:b2", "b" * 40)
        self.box.publish(FRONTEND, "sha256:f2", "b" * 40)
        (self.box.fake / "http_code").write_text("401", encoding="utf-8")
        result = self.box.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("HTTP 401", result.stdout)
        self.assertEqual(self.box.state.read_text(encoding="utf-8"), before)
        (self.box.fake / "http_code").write_text("200", encoding="utf-8")
        self.assertEqual(self.box.run().returncode, 0)
        self.assertEqual(len(self.box.curl_calls()), 2)
        self.assertIn("sha256:b2", self.box.state.read_text(encoding="utf-8"))

    def test_failed_pull_exits_nonzero_without_state(self) -> None:
        (self.box.fake / "pull_fail").touch()
        result = self.box.run()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.box.state.exists())

    def test_digest_matches_the_watched_repo_and_registry_port(self) -> None:
        image = "localhost:5000/team/contract-toaster-dts-backend:latest"
        self.box.publish(
            image, "sha256:mine", "c" * 40,
            extra="localhost:5000/team/other-image@sha256:not-mine",
        )
        result = self.box.run(images=image)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.box.state.read_text(encoding="utf-8"), f"{image} sha256:mine\n"
        )


class Wiring(unittest.TestCase):
    def test_workflow_deploy_job_steps_aside_in_pull_mode(self) -> None:
        text = PUBLISH.read_text(encoding="utf-8")
        deploy = text.split("\n  deploy:\n", 1)[1]
        self.assertRegex(deploy, r"(?m)^    if: vars\.DTS_DEPLOY_MODE != 'pull'$")

    def test_service_runs_the_installed_script_from_the_env_file(self) -> None:
        unit = SERVICE.read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=/etc/coolify-autopull.env", unit)
        self.assertIn("ExecStart=/usr/local/bin/coolify-autopull", unit)
        self.assertIn("StateDirectory=coolify-autopull", unit)
        readme = README.read_text(encoding="utf-8")
        self.assertIn("/usr/local/bin/coolify-autopull", readme)
        self.assertIn("/etc/coolify-autopull.env", readme)

    def test_env_example_carries_placeholders_only(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("COOLIFY_API_TOKEN=<token>", text)
        self.assertIn("COOLIFY_SERVICE_UUID=<service-uuid>", text)
        self.assertIn("ghcr.io/<owner>/", text)

    def test_script_is_executable(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
