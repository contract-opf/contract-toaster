#!/usr/bin/env python3
"""
Both Docker Compose files cap each long-running container's CPU, generously
and overridably (deploy/dts/README.md#container-cpu-limits).

The cap is a CFS quota (`cpu_quota`, against Docker's default 100000us period),
NOT `cpus:`. Docker
refuses a `cpus` value above the host's core count, so a "generous" `cpus: 4`
would stop the whole stack starting on a 2-core box; a quota above the core
count never throttles and never fails a start. This file pins:

  1. backend, frontend, dynamodb-local and minio each carry a `cpu_quota` of
     the form `${NAME:-default}`, in BOTH files, with the same variable name
     and default in each, and NO `cpu_period`: with the quota at 0 a service
     then sets no CPU key at all, which is what a host that refuses CPU limits
     (rootless Docker without cgroup CPU delegation) needs.
  2. The defaults are generous: the backend (which runs the review pipeline
     in-process) at least 2 CPUs, every other service at least 1. A tighter
     default throttles the pipeline and can make its healthcheck miss its
     4-second timeout, which autopull reads as a bad rollout.
  3. No `cpus:` key anywhere, and nothing under `deploy.resources` (which sets
     the same refusing limit).
  4. Every override variable is documented in the README.

Run: python3 tests/test_dts_compose_cpu_limits.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DTS = REPO_ROOT / "deploy" / "dts"
FILES = {
    "docker-compose.yml": DTS / "docker-compose.yml",
    "docker-compose.coolify.yml": DTS / "docker-compose.coolify.yml",
}
README = DTS / "README.md"
PERIOD = 100000
MIN_CPUS = {"backend": 2, "frontend": 1, "dynamodb-local": 1, "minio": 1}
QUOTA_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*):-(\d+)\}$")


def services(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]


class CpuLimits(unittest.TestCase):
    def quota(self, file: str, service: str) -> tuple[str, int]:
        value = services(FILES[file])[service].get("cpu_quota")
        self.assertIsInstance(value, str, f"{file}: {service} has no cpu_quota")
        match = QUOTA_RE.match(value)
        self.assertIsNotNone(match, f"{file}: {service} cpu_quota must be ${{NAME:-default}}")
        return match.group(1), int(match.group(2))  # type: ignore[union-attr]

    def test_every_long_running_service_has_an_overridable_quota_and_no_period(self) -> None:
        for file in FILES:
            for service in MIN_CPUS:
                with self.subTest(file=file, service=service):
                    self.assertNotIn("cpu_period", services(FILES[file])[service])
                    self.quota(file, service)

    def test_both_files_agree_on_the_names_and_defaults(self) -> None:
        for service in MIN_CPUS:
            with self.subTest(service=service):
                self.assertEqual(
                    self.quota("docker-compose.yml", service),
                    self.quota("docker-compose.coolify.yml", service),
                )

    def test_the_defaults_are_generous(self) -> None:
        for file in FILES:
            for service, cpus in MIN_CPUS.items():
                with self.subTest(file=file, service=service):
                    _, default = self.quota(file, service)
                    self.assertGreaterEqual(default, cpus * PERIOD)

    def test_the_variable_names_are_distinct(self) -> None:
        names = [self.quota("docker-compose.yml", s)[0] for s in MIN_CPUS]
        self.assertEqual(len(set(names)), len(names), names)

    def test_nothing_uses_the_limit_that_refuses_to_start_on_a_small_host(self) -> None:
        for file, path in FILES.items():
            for name, service in services(path).items():
                with self.subTest(file=file, service=name):
                    self.assertNotIn("cpus", service)
                    self.assertNotIn("cpu_count", service)
                    self.assertNotIn("cpu_period", service)
                    resources = (service.get("deploy") or {}).get("resources") or {}
                    self.assertNotIn("cpus", resources.get("limits") or {})

    def test_the_one_shot_bootstrap_is_left_alone(self) -> None:
        # It runs once at start-up and exits; capping it only slows a boot.
        for file in FILES:
            self.assertNotIn("cpu_quota", services(FILES[file])["bootstrap"], file)


class Documented(unittest.TestCase):
    def test_the_readme_says_how_to_run_where_cpu_limits_are_refused(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertIn("Rootless Docker without cgroup CPU delegation", text)

    def test_every_override_is_in_the_readme(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertIn("Container CPU limits", text)
        for service in MIN_CPUS:
            name, _ = re.match(  # type: ignore[union-attr]
                r"^\$\{([A-Z0-9_]+):-(\d+)\}$",
                services(FILES["docker-compose.yml"])[service]["cpu_quota"],
            ).groups()
            with self.subTest(variable=name):
                self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
