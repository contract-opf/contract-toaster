#!/usr/bin/env python3
"""
deploy/dts/docker-compose.coolify.yml must never carry a literal MinIO login.

MinIO is published on its own domain in the Coolify deployment, because
presigned downloads go from the browser straight to it. Its root login used to
be the `local` / `localsecret` pair from this public repository, so anyone who
read the file could connect to that domain and read or delete every stored
contract. The Coolify compose now names Coolify magic variables, which Coolify
fills with a generated value on first save.

These tests pin:

  1. No literal `local` / `localsecret` (or any other literal) in the Coolify
     compose's AWS_* or MINIO_ROOT_* values: each is a ${VAR} reference.
  2. MinIO's root user/password and the backend's AWS key pair name the SAME
     variables, so the backend can still sign requests MinIO accepts.
  3. The variables are Coolify-generated ones (SERVICE_USER_* /
     SERVICE_PASSWORD_*), so no operator has to invent or paste a secret.

Run: python3 tests/test_dts_minio_credentials.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COOLIFY = REPO_ROOT / "deploy" / "dts" / "docker-compose.coolify.yml"
REF = re.compile(r"^\$\{(SERVICE_(?:USER|PASSWORD(?:_64)?)_[A-Z0-9_]+)\}$")


def _compose() -> dict:
    return yaml.safe_load(COOLIFY.read_text(encoding="utf-8"))


class CoolifyMinioCredentials(unittest.TestCase):
    def test_no_literal_default_pair_anywhere(self) -> None:
        text = COOLIFY.read_text(encoding="utf-8")
        self.assertNotIn("localsecret", text)
        self.assertNotRegex(text, r"(?m)^\s*(AWS_ACCESS_KEY_ID|MINIO_ROOT_USER):\s*local\s*$")

    def _pairs(self) -> dict[str, tuple[str, str]]:
        compose = _compose()
        pairs = {"minio": (
            compose["services"]["minio"]["environment"]["MINIO_ROOT_USER"],
            compose["services"]["minio"]["environment"]["MINIO_ROOT_PASSWORD"],
        )}
        for name in ("backend", "bootstrap"):
            env = compose["services"][name]["environment"]
            pairs[name] = (env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"])
        return pairs

    def test_every_pair_is_a_generated_variable(self) -> None:
        for service, pair in self._pairs().items():
            for value in pair:
                with self.subTest(service=service, value=value):
                    self.assertRegex(str(value), REF)

    def test_backend_and_minio_name_the_same_pair(self) -> None:
        pairs = self._pairs()
        self.assertEqual(pairs["backend"], pairs["minio"])
        self.assertEqual(pairs["bootstrap"], pairs["minio"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
