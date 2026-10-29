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
  8. An accepted restart is recorded only once the service's containers run
     the new revision and are healthy (or have no healthcheck); an exited
     one-shot container and another service's containers are not counted.
  9. A rollout that does not come up is rolled back: the watched refs are
     re-tagged to the previous digests, Coolify restarts WITHOUT pulling, and
     the bad digests are held off until a newer image is published.
 10. With AUTOPULL_HEARTBEAT_URL set, every run reports up or down.
 11. A run with nothing to roll out still checks the containers, and reports
     down (without restarting anything) when one is not running and healthy.

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
  ps) cat "$FAKE/ps" 2>/dev/null; exit 0 ;;
  inspect) cat "$FAKE/inspect.${*: -1}" 2>/dev/null; echo ;;
  tag)
    printf '%s %s\n' "$2" "$3" >> "$FAKE/tag.log"
    [ -e "$FAKE/tag_fail" ] && exit 1; exit 0 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE/curl.argv"
out=""
fmt=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) fmt="$2"; shift ;;
    -H) case "$2" in @*) cat "${2#@}" >> "$FAKE/curl.headerfiles" ;; esac; shift ;;
  esac
  shift
done
[ -n "$out" ] && [ "$out" != /dev/null ] && printf 'body from coolify' > "$out"
[ -n "$fmt" ] && printf '%s' "$(cat "$FAKE/http_code" 2>/dev/null || echo 200)"
exit 0
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

    def serve(self, revision: str, health: str = "healthy", state: str = "running") -> None:
        """What `docker ps`/`docker inspect` show for the service's containers."""
        rows = [(f"backend-{UUID}", BACKEND), (f"frontend-{UUID}", FRONTEND)]
        (self.fake / "ps").write_text(
            "".join(f"{name}\t{image}\n" for name, image in rows), encoding="utf-8"
        )
        for name, _ in rows:
            self.container(name, revision, health, state)

    def container(self, name: str, revision: str, health: str, state: str) -> None:
        (self.fake / f"inspect.{name}").write_text(
            f"{revision}|{health}|{state}", encoding="utf-8"
        )

    def add_ps(self, name: str, image: str) -> None:
        with (self.fake / "ps").open("a", encoding="utf-8") as fh:
            fh.write(f"{name}\t{image}\n")

    def run(
        self, images: str = f"{BACKEND} {FRONTEND}", **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "AUTOPULL_IMAGES": images,
            "COOLIFY_SERVICE_UUID": UUID,
            "COOLIFY_API_TOKEN": TOKEN,
            "AUTOPULL_STATE_FILE": str(self.state),
            "AUTOPULL_VERIFY_SECONDS": "1",
            "AUTOPULL_VERIFY_INTERVAL": "0",
            **extra,
        }
        return subprocess.run(  # noqa: S603 (the repo's own script)
            [BASH or "bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
        )

    def curl_calls(self) -> list[str]:
        """The Coolify restart calls (heartbeat pings are left out)."""
        return [c for c in self.all_curl() if "/restart?" in c]

    def all_curl(self) -> list[str]:
        path = self.fake / "curl.argv"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def tags(self) -> list[str]:
        path = self.fake / "tag.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    @property
    def bad(self) -> Path:
        return self.state.with_name(self.state.name + ".bad")


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
        self.box.serve("a" * 40)
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.box.curl_calls(), [])

    def test_new_digest_restarts_with_latest_and_records(self) -> None:
        self.initialise()
        self.box.publish(BACKEND, "sha256:b2", "b" * 40)
        self.box.publish(FRONTEND, "sha256:f2", "b" * 40)
        self.box.serve("b" * 40)
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
        self.box.serve("b" * 40)
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


    def publish_b(self) -> None:
        self.box.publish(BACKEND, "sha256:b2", "b" * 40)
        self.box.publish(FRONTEND, "sha256:f2", "b" * 40)

    def assert_rolled_back(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rolling back", result.stdout)
        calls = self.box.curl_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("restart?latest=true", calls[0])
        self.assertIn("restart?latest=false", calls[1], "a rollback must not pull")
        repo_b, repo_f = BACKEND.rsplit(":", 1)[0], FRONTEND.rsplit(":", 1)[0]
        self.assertEqual(
            self.box.tags(), [f"{repo_b}@sha256:b1 {BACKEND}", f"{repo_f}@sha256:f1 {FRONTEND}"]
        )
        self.assertEqual(
            self.box.state.read_text(encoding="utf-8"),
            f"{BACKEND} sha256:b1\n{FRONTEND} sha256:f1\n",
        )
        self.assertEqual(
            self.box.bad.read_text(encoding="utf-8"),
            f"{BACKEND} sha256:b2\n{FRONTEND} sha256:f2\n",
        )

    def test_containers_still_on_the_old_revision_roll_back(self) -> None:
        self.box.serve("a" * 40)
        self.initialise()
        self.publish_b()
        self.assert_rolled_back(self.box.run())

    def test_unhealthy_rollout_rolls_back(self) -> None:
        self.initialise()
        self.publish_b()
        self.box.serve("b" * 40, health="unhealthy")
        self.assert_rolled_back(self.box.run())

    def test_still_starting_counts_as_not_up(self) -> None:
        self.initialise()
        self.publish_b()
        self.box.serve("b" * 40, health="starting")
        self.assert_rolled_back(self.box.run())

    def test_missing_container_counts_as_not_up(self) -> None:
        self.initialise()
        self.publish_b()
        (self.box.fake / "ps").write_text(f"backend-{UUID}\t{BACKEND}\n", encoding="utf-8")
        self.box.container(f"backend-{UUID}", "b" * 40, "healthy", "running")
        self.assert_rolled_back(self.box.run())

    def test_no_healthcheck_and_exited_one_shot_and_other_services_are_fine(self) -> None:
        self.initialise()
        self.publish_b()
        self.box.serve("b" * 40, health="none")
        self.box.add_ps(f"bootstrap-{UUID}", BACKEND)
        self.box.container(f"bootstrap-{UUID}", "a" * 40, "none", "exited")
        self.box.add_ps("backend-some-other-service", BACKEND)
        self.box.container("backend-some-other-service", "a" * 40, "unhealthy", "running")
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("up and healthy", result.stdout)
        self.assertIn("sha256:b2", self.box.state.read_text(encoding="utf-8"))

    def test_known_bad_revision_is_held_and_retagged(self) -> None:
        self.initialise()
        self.publish_b()
        self.box.serve("b" * 40, health="unhealthy")
        self.box.run()
        result = self.box.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("holding at the previous revision", result.stdout)
        self.assertEqual(len(self.box.curl_calls()), 2, "no new restart while held")
        self.assertEqual(len(self.box.tags()), 4, "re-tagged after the pull")
        self.assertIn("sha256:b1", self.box.state.read_text(encoding="utf-8"))

    def test_a_newer_image_after_a_bad_one_rolls_out_and_clears_it(self) -> None:
        self.initialise()
        self.publish_b()
        self.box.serve("b" * 40, health="unhealthy")
        self.box.run()
        self.box.publish(BACKEND, "sha256:b3", "c" * 40)
        self.box.publish(FRONTEND, "sha256:f3", "c" * 40)
        self.box.serve("c" * 40)
        result = self.box.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sha256:b3", self.box.state.read_text(encoding="utf-8"))
        self.assertFalse(self.box.bad.exists())

    def test_rollback_without_the_previous_image_says_so(self) -> None:
        self.initialise()
        self.publish_b()
        (self.box.fake / "tag_fail").touch()
        result = self.box.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ROLLBACK FAILED", result.stdout)
        self.assertEqual(len(self.box.curl_calls()), 1, "no restart without the old image")

    def test_verification_can_be_turned_off(self) -> None:
        self.initialise()
        self.publish_b()
        result = self.box.run(AUTOPULL_VERIFY_SECONDS="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not verified", result.stdout)
        self.assertIn("sha256:b2", self.box.state.read_text(encoding="utf-8"))

    def test_heartbeat_reports_up_and_down(self) -> None:
        url = "https://kuma.example/api/push/abc"
        self.assertEqual(self.box.run(AUTOPULL_HEARTBEAT_URL=url).returncode, 0)
        up = [c for c in self.box.all_curl() if url in c]
        self.assertEqual(len(up), 1)
        self.assertIn("status=up", up[0])
        (self.box.fake / "pull_fail").touch()
        self.assertEqual(self.box.run(AUTOPULL_HEARTBEAT_URL=url).returncode, 1)
        down = [c for c in self.box.all_curl() if url in c]
        self.assertEqual(len(down), 2)
        self.assertIn("status=down", down[1])
        self.assertIn("could not pull", down[1])

    def test_unchanged_but_unhealthy_reports_down_without_restarting(self) -> None:
        url = "https://kuma.example/api/push/abc"
        self.initialise()
        self.box.serve("a" * 40, health="unhealthy")
        result = self.box.run(AUTOPULL_HEARTBEAT_URL=url)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not running and healthy", result.stdout)
        self.assertEqual(self.box.curl_calls(), [], "a liveness check never restarts")
        self.assertIn("status=down", [c for c in self.box.all_curl() if url in c][-1])
        self.box.serve("a" * 40)
        self.assertEqual(self.box.run(AUTOPULL_HEARTBEAT_URL=url).returncode, 0)
        self.assertIn("status=up", [c for c in self.box.all_curl() if url in c][-1])

    def test_no_heartbeat_without_a_url(self) -> None:
        self.initialise()
        self.assertEqual(self.box.all_curl(), [])


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
