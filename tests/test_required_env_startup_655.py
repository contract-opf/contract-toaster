#!/usr/bin/env python3
"""
Gate for issue #655: a missing required environment variable must kill the
container at START, not 500 one endpoint later.

## The incident

`USER_PREFERENCES_TABLE` was absent from the live compose (#654). The backend
started perfectly happily with a configuration it could not serve:
`GET /api/me/preferences` 500'd on every request and the only user-visible
symptom was `Internal (unavailable)` on the Review tab -- indistinguishable
from an unbuilt feature. It cost a full day of debugging aimed at the wrong
thing, and neither the repo diff nor CI could see it, because the repo
TEMPLATES were correct all along; the live compose lives in Coolify's
database.

That is why the assertion below is a real process, not a template check. A
test that read the compose file and found the key would have passed
throughout the incident.

## What is asserted

  A. The required set is DERIVED FROM SOURCE by an AST pass -- adding a new
     `os.environ["X"]` to a module is picked up with no list to edit
     (`TestTheRequiredSetIsDerivedFromSource`, which points the same pass at
     a fixture tree it writes).
  B. `os.environ.get("Y", default)` is NOT promoted to required. A boot check
     that demanded every optional flag would be a worse footgun than the bug
     it fixes.
  C. A real `uvicorn src.main:app` -- the exact command
     `deploy/dts/backend.Dockerfile` runs -- EXITS NON-ZERO when a required
     variable is missing, naming it, and starts normally when they are all
     present (`TestTheContainerRefusesToStart`).
  D. The failure output carries NAMES ONLY. It lands in container logs; an
     env VALUE must not.
  E. Both deploy-target compose files supply every derived name, so a future
     `os.environ[...]` added without a matching compose key fails this gate
     rather than the deployment (`TestBothComposeTargetsSupplyEveryName`).

## Not asserted, deliberately

The AWS App Runner target does NOT supply the full set today
(`infra/lib/nested/app-stack.ts` injects eight of the sixteen names and no
`STATE_MACHINE_ARN`, so `POST /api/reviews` has always raised `KeyError`
there). Pinning that here would encode the gap as expected. See
`backend/src/startup_checks.py`'s "Deployment-target consequence" note.

Offline: no AWS, no network. The subprocess binds an ephemeral port and is
torn down.

Exit codes: 0 = all tests pass, 1 = one or more failed.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
COMPOSE_LOCAL = REPO_ROOT / "deploy" / "dts" / "docker-compose.yml"
COMPOSE_COOLIFY = REPO_ROOT / "deploy" / "dts" / "docker-compose.coolify.yml"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src import startup_checks  # noqa: E402

# Seconds to wait for the subprocess to either exit or announce startup.
# Generous on purpose: a slow importer must not be reported as a failure to
# start (that would be a race dressed up as a finding).
BOOT_TIMEOUT_SECONDS = 180.0

# Names read with `os.environ.get(..., default)` somewhere in backend/src.
# Each has a documented default; promoting any of them to required turns a
# working deployment into a boot loop.
OPTIONAL_NAMES = (
    "AWS_REGION",          # src/config.py::region
    "DEPLOY_TARGET",       # src/config.py::deploy_target
    "AUTH_MODE",           # src/config.py::auth_mode
    "MODEL_PROVIDER",      # src/config.py::model_provider
    "NOTES_MODE_ENABLED",  # src/config.py::notes_mode_enabled
    "VERSION",             # src/main.py, FastAPI(version=...)
)


# ---------------------------------------------------------------------------
# A/B -- the required set comes out of the source, not out of a list
# ---------------------------------------------------------------------------

class TestTheRequiredSetIsDerivedFromSource(unittest.TestCase):
    """A hand-maintained list would drift away from the code exactly the way
    the live compose drifted away from the template. These point the SAME
    pass production uses at a throwaway source tree."""

    def _derive(self, module_source: str) -> tuple[str, ...]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "fixture_module.py").write_text(
                module_source, encoding="utf-8"
            )
            return startup_checks.required_env_names(root)

    def test_a_new_required_deref_is_picked_up_with_no_list_edited(self):
        derived = self._derive(
            "import os\n"
            "def f():\n"
            "    return os.environ['FIXTURE_NEWLY_ADDED_TABLE']\n"
        )
        self.assertEqual(derived, ("FIXTURE_NEWLY_ADDED_TABLE",))

    def test_an_optional_get_with_a_default_is_not_promoted_to_required(self):
        """The branch that matters: both forms in ONE module, so a pass that
        simply matched the word `environ` would fail here."""
        derived = self._derive(
            "import os\n"
            "REQUIRED = os.environ['FIXTURE_REQUIRED_TABLE']\n"
            "OPTIONAL = os.environ.get('FIXTURE_OPTIONAL_FLAG', 'off')\n"
            "BARE_GET = os.environ.get('FIXTURE_OPTIONAL_NO_DEFAULT')\n"
        )
        self.assertEqual(derived, ("FIXTURE_REQUIRED_TABLE",))

    def test_a_name_only_mentioned_in_a_docstring_is_not_required(self):
        """`src/reviews.py` documents its bucket read in prose. A grep cannot
        tell that apart from a real dereference; an AST pass can."""
        derived = self._derive(
            'import os\n'
            'def f(key):\n'
            '    """Bucket is `os.environ["FIXTURE_DOCSTRING_ONLY"]`."""\n'
            "    return os.environ['FIXTURE_REAL_DEREF']\n"
        )
        self.assertEqual(derived, ("FIXTURE_REAL_DEREF",))

    def test_a_non_literal_subscript_is_skipped_rather_than_guessed(self):
        derived = self._derive(
            "import os\n"
            "def f(name):\n"
            "    return os.environ[name]\n"
        )
        self.assertEqual(derived, ())

    def test_the_from_os_import_environ_form_is_also_caught(self):
        derived = self._derive(
            "from os import environ\n"
            "TABLE = environ['FIXTURE_FROM_IMPORT_TABLE']\n"
        )
        self.assertEqual(derived, ("FIXTURE_FROM_IMPORT_TABLE",))

    def test_the_real_backend_set_holds_the_variable_the_incident_was_about(self):
        names = startup_checks.required_env_names()
        self.assertIn("USER_PREFERENCES_TABLE", names)
        # The AWS submit path's ARN -- required, and the loudest example of a
        # name a deployment can be missing while the container looks healthy.
        self.assertIn("STATE_MACHINE_ARN", names)

    def test_the_real_backend_set_holds_no_optional_name(self):
        names = startup_checks.required_env_names()
        for optional in OPTIONAL_NAMES:
            self.assertNotIn(optional, names, f"{optional} has a default")


# ---------------------------------------------------------------------------
# The missing-name computation
# ---------------------------------------------------------------------------

class TestMissingNames(unittest.TestCase):

    def setUp(self):
        self.full = {name: f"value-for-{name}" for name in
                     startup_checks.required_env_names()}

    def test_a_complete_environment_reports_nothing_missing(self):
        self.assertEqual(startup_checks.missing_required_env(self.full), ())

    def test_one_absent_name_is_reported(self):
        env = dict(self.full)
        del env["USER_PREFERENCES_TABLE"]
        self.assertEqual(
            startup_checks.missing_required_env(env), ("USER_PREFERENCES_TABLE",)
        )

    def test_an_empty_value_counts_as_absent(self):
        """A table name of "" is not a table name; DynamoDB rejects it on the
        first call -- the same half-working deployment, one restart later."""
        env = dict(self.full, REVIEWS_TABLE="   ")
        self.assertEqual(startup_checks.missing_required_env(env), ("REVIEWS_TABLE",))

    def test_every_missing_name_is_reported_at_once(self):
        """An operator fixing one variable per restart is why this is a
        single line and not the first KeyError that happens to fire."""
        env = dict(self.full)
        del env["AUDIT_TABLE"]
        del env["UPLOADS_BUCKET"]
        missing = startup_checks.missing_required_env(env)
        self.assertEqual(missing, ("AUDIT_TABLE", "UPLOADS_BUCKET"))
        message = startup_checks.format_missing_env_message(missing)
        self.assertIn("AUDIT_TABLE", message)
        self.assertIn("UPLOADS_BUCKET", message)

    def test_verify_raises_system_exit_naming_the_variable(self):
        env = dict(self.full)
        del env["DAILY_SPEND_TABLE"]
        with self.assertRaises(SystemExit) as caught:
            startup_checks.verify_required_env(env)
        self.assertIn("DAILY_SPEND_TABLE", str(caught.exception))

    def test_verify_returns_quietly_when_the_environment_is_complete(self):
        self.assertIsNone(startup_checks.verify_required_env(self.full))


# ---------------------------------------------------------------------------
# Compose parsing shared by the process tests and the coverage test
# ---------------------------------------------------------------------------

# `KEY: value` at any indentation. Values carrying a `${...}` interpolation
# (DEMO_TOKEN_SECRET, OPENROUTER_API_KEY) resolve from the operator's .env,
# not from the file, so they are not readable here.
_COMPOSE_ENTRY = re.compile(r"^\s+([A-Z][A-Z0-9_]*):\s*(\S.*?)\s*$")


def compose_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _COMPOSE_ENTRY.match(line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2)
        if raw.startswith("#") or "${" in raw:
            continue
        values[name] = raw.strip('"').strip("'")
    return values


class TestBothComposeTargetsSupplyEveryName(unittest.TestCase):
    """Not a substitute for the process test above -- the live compose is
    NOT this file, which is the whole reason #654 was invisible. What this
    pins is the other direction: a new `os.environ[...]` landing in
    backend/src without a matching compose key now fails the gate instead of
    the next deployment."""

    def test_local_compose_supplies_every_required_name(self):
        supplied = compose_env(COMPOSE_LOCAL)
        for name in startup_checks.required_env_names():
            self.assertIn(name, supplied, f"{name} missing from {COMPOSE_LOCAL.name}")

    def test_coolify_compose_supplies_every_required_name(self):
        supplied = compose_env(COMPOSE_COOLIFY)
        for name in startup_checks.required_env_names():
            self.assertIn(name, supplied, f"{name} missing from {COMPOSE_COOLIFY.name}")


# ---------------------------------------------------------------------------
# C/D -- the real entrypoint, in a real process
# ---------------------------------------------------------------------------

def _base_env() -> dict[str, str]:
    """The table/bucket/state-machine half of the DTS backend container's
    environment, read out of `deploy/dts/docker-compose.yml` rather than out
    of the module under test -- deriving the "complete" environment from
    `required_env_names()` would make the started/not-started assertions
    circular.

    Only that half: the compose also sets DEPLOY_TARGET, AUTH_MODE and
    MODEL_PROVIDER, which select adapters (purge cadence, verifiers, live
    OpenRouter). Those are optional-with-a-default by construction, and
    leaving them out keeps this test's subprocess on the same inert defaults
    every other test runs under while still proving the required half is
    sufficient to boot."""
    supplied = compose_env(COMPOSE_LOCAL)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": f"{BACKEND_ROOT}{os.pathsep}{SCRIPTS_ROOT}",
        "PYTHONUNBUFFERED": "1",
    }
    for name, value in supplied.items():
        if name.endswith(("_TABLE", "_BUCKET")) or name == "STATE_MACHINE_ARN":
            env[name] = value
    return env


def _run_uvicorn(env: dict[str, str], wait_for: str) -> tuple[int | None, str]:
    """Run the container's own command. Returns (exit code, output) if the
    process died, or (None, output) once `wait_for` appears in its output --
    in which case the process is torn down."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "uvicorn.log"
        with open(log_path, "w", encoding="utf-8") as sink:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "src.main:app",
                 "--host", "127.0.0.1", "--port", "0"],
                cwd=str(BACKEND_ROOT),
                env=env,
                stdout=sink,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
                while time.monotonic() < deadline:
                    code = process.poll()
                    output = log_path.read_text(encoding="utf-8", errors="replace")
                    if code is not None:
                        return code, output
                    if wait_for in output:
                        return None, output
                    time.sleep(0.2)
                raise AssertionError(
                    f"uvicorn neither exited nor printed {wait_for!r} within "
                    f"{BOOT_TIMEOUT_SECONDS}s:\n"
                    + log_path.read_text(encoding="utf-8", errors="replace")
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:  # pragma: no cover - teardown
                    process.kill()
                    process.wait(timeout=30)


class TestTheContainerRefusesToStart(unittest.TestCase):
    """`uvicorn src.main:app` is verbatim the CMD in
    deploy/dts/backend.Dockerfile. Anything short of running it would be
    asserting about a startup path the container does not take."""

    def test_a_complete_environment_still_starts_normally(self):
        env = _base_env()
        # An OPTIONAL variable being absent must not block startup -- none of
        # these is set here, and the app must come up anyway.
        for optional in OPTIONAL_NAMES:
            self.assertNotIn(optional, env)
        code, output = _run_uvicorn(env, wait_for="Application startup complete")
        self.assertIsNone(code, f"exited instead of starting:\n{output}")
        self.assertIn("Application startup complete", output)

    def test_one_missing_variable_exits_non_zero_naming_it(self):
        env = _base_env()
        removed_value = env.pop("USER_PREFERENCES_TABLE")
        code, output = _run_uvicorn(env, wait_for="Application startup complete")
        self.assertIsNotNone(
            code, f"started with a configuration it cannot serve:\n{output}"
        )
        self.assertNotEqual(code, 0, f"exited zero:\n{output}")
        self.assertIn("USER_PREFERENCES_TABLE", output)
        self.assertNotIn("Application startup complete", output)
        # D -- names only. The value is a real table name; it must not be in
        # a log line an operator pastes into a ticket.
        self.assertNotIn(removed_value, output)

    def test_several_missing_variables_are_all_named_in_one_run(self):
        env = _base_env()
        for name in ("AUDIT_TABLE", "OUTPUTS_BUCKET", "STATE_MACHINE_ARN"):
            env.pop(name)
        code, output = _run_uvicorn(env, wait_for="Application startup complete")
        self.assertIsNotNone(code, f"started anyway:\n{output}")
        self.assertNotEqual(code, 0, f"exited zero:\n{output}")
        for name in ("AUDIT_TABLE", "OUTPUTS_BUCKET", "STATE_MACHINE_ARN"):
            self.assertIn(name, output)


class TestItIsActuallyWiredIn(unittest.TestCase):
    """A check nothing calls reproduces the bug exactly, and every unit test
    above would still pass."""

    def _lifespan_body(self) -> list:
        source = (BACKEND_ROOT / "src" / "main.py").read_text(encoding="utf-8")
        function = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lifespan"
        )
        return [
            statement for statement in function.body
            if not (isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant))
        ]

    def test_the_check_is_the_lifespans_first_statement(self):
        """First, and at the top level of the body -- `_lifespan` wraps the
        purge cadence in a bare `except Exception` so a cadence can never
        stop the API booting. Inside that block this check would be
        swallowed by the same handler and the process would boot anyway."""
        first = self._lifespan_body()[0]
        self.assertIsInstance(first, ast.Expr)
        self.assertEqual(
            ast.unparse(first.value), "startup_checks.verify_required_env()"
        )

    def test_the_lifespan_is_attached_to_the_app(self):
        """Writing the hook and forgetting to hand it to FastAPI is the same
        class of mistake as writing the check and never calling it."""
        source = (BACKEND_ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("lifespan=_lifespan", source)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("\nPASS: a missing required env var kills startup (issue #655).")
        return 0
    print(f"\nFAIL: {len(result.failures)} failure(s), {len(result.errors)} error(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
