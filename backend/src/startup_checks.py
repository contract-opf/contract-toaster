"""
Boot-time environment check — issue #655.

## The incident this exists for

`USER_PREFERENCES_TABLE` was absent from the live compose (#654). The
container started perfectly happily with a configuration it could not
actually serve: the table was never created, `GET /api/me/preferences` 500'd
on every request, and the only user-visible symptom was `Internal
(unavailable)` on the Review tab — indistinguishable from an unbuilt feature.
It survived a full day of debugging aimed at the wrong thing, with no repo
diff, no failing test and no CI signal, because the repo templates were
correct all along and the live compose lives in Coolify's database.

So the check has to run in the *running container*, at start, and it has to
be fatal. One restart loop is far cheaper than a silent half-working
deployment.

## Where the required set comes from

From the source, by an AST pass — never a hand-maintained list. A hand-picked
list is the exact failure mode that left `main` red for two days in the
playbook-lint incident, and it would drift away from the code the same way
the live compose drifted away from the template.

`required_env_names` walks every `.py` file under `backend/src` and collects
the string literal of every `os.environ[<literal>]` **subscript**:

  - `os.environ["REVIEWS_TABLE"]`  -> required. No default exists; the first
    caller that reaches it raises `KeyError` -> HTTP 500.
  - `os.environ.get("AWS_REGION", "us-east-1")` -> NOT required. That is a
    `Call`, not a `Subscript`, so this pass never sees it. Every such read
    has a documented default (see `src/config.py`) and promoting one to
    required would turn a working deployment into a boot loop.

Being an AST pass rather than a grep, it also ignores the name inside a
docstring that merely *describes* a read (e.g. `src/reviews.py`'s
"Bucket is `os.environ[\"OUTPUTS_BUCKET\"]`"), which a regex cannot tell
apart from the real thing.

## Names only, never values

This runs at boot and its output lands in container logs, which are read by
more people than the environment is. `format_missing_env_message` is given
only the *names* it found missing; no value ever reaches it.

## Deployment-target consequence (deliberate, read before "fixing" it)

The Docker Compose target — the one this project actually deploys — supplies
every name this pass derives (`deploy/dts/docker-compose.yml` and
`docker-compose.coolify.yml`; `tests/test_required_env_startup_655.py` pins
that, so a new `os.environ[...]` added without a matching compose key fails
the gate instead of the deployment).

The AWS App Runner target does not: `infra/lib/nested/app-stack.ts` injects
eight of these names and no `STATE_MACHINE_ARN`, so on that target
`POST /api/reviews` — the central route — has always raised `KeyError` at
`src/reviews.py`'s `os.environ["STATE_MACHINE_ARN"]`. That container will now
refuse to start rather than serve a review API that cannot start a review.
That is this ticket's thesis, not a regression introduced by it: completing
the App Runner environment (and the IAM grants behind it) is a separate
infra change, not a reason to soften the check into a list of the names one
target happens to set.
"""

import ast
import logging
import os
from pathlib import Path
from typing import Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# The tree the required set is derived from: this package. Passed explicitly
# by the tests so they can point the same pass at a fixture tree.
BACKEND_SRC = Path(__file__).resolve().parent


def _environ_subscript_name(node: ast.Subscript) -> Optional[str]:
    """The literal env var name of an `os.environ[...]`/`environ[...]`
    subscript, or None for anything else (including a non-literal subscript
    like `os.environ[name]`, which no static pass can resolve)."""
    target = node.value
    is_environ = (
        # os.environ[...] — the form every module in this package uses.
        (isinstance(target, ast.Attribute) and target.attr == "environ"
         and isinstance(target.value, ast.Name) and target.value.id == "os")
        # environ[...] — `from os import environ`.
        or (isinstance(target, ast.Name) and target.id == "environ")
    )
    if not is_environ:
        return None
    key = node.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return None


def required_env_names_in_source(source: str) -> set[str]:
    """Every env var name dereferenced with `os.environ[<literal>]` in one
    module's source. Raises SyntaxError on unparseable source — a module in
    this package that does not parse is a problem worth failing on, not one
    to skip past silently."""
    tree = ast.parse(source)
    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        for name in (_environ_subscript_name(node),)
        if name is not None
    }


def required_env_names(source_root: Optional[Path] = None) -> tuple[str, ...]:
    """The sorted set of env var names the code under `source_root`
    (default: `backend/src`) treats as REQUIRED — i.e. dereferences with
    `os.environ[<literal>]`, which has no default and raises `KeyError`.

    Derived from the source on every call. Nothing here is a list to keep in
    step by hand.
    """
    root = Path(source_root) if source_root is not None else BACKEND_SRC
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        names |= required_env_names_in_source(path.read_text(encoding="utf-8"))
    return tuple(sorted(names))


def missing_required_env(
    environ: Optional[Mapping[str, str]] = None,
    source_root: Optional[Path] = None,
) -> tuple[str, ...]:
    """The required names absent from `environ` (default: the real one), in
    sorted order. An empty value counts as absent: a table name of `""` is
    not a table name, and DynamoDB would reject it on the first call — the
    same silent half-working deployment one restart earlier."""
    env = os.environ if environ is None else environ
    return tuple(
        name
        for name in required_env_names(source_root)
        if not (env.get(name) or "").strip()
    )


def format_missing_env_message(missing: Iterable[str]) -> str:
    """The single log line naming every missing variable at once — NAMES
    ONLY. Callers pass names; no value is ever available to this function to
    leak."""
    return (
        "STARTUP: refusing to start — required environment variable(s) not set: "
        + ", ".join(sorted(missing))
        + ". Set them in the deployment's environment (see "
        "deploy/dts/docker-compose.yml) and restart."
    )


def verify_required_env(
    environ: Optional[Mapping[str, str]] = None,
    source_root: Optional[Path] = None,
) -> None:
    """Fail the process if any required environment variable is missing.

    Reports EVERY missing name in one line — an operator fixing a broken
    deployment one restart at a time per variable is the reason this is a
    single line and not the first `KeyError` that happens to fire.

    Raises `SystemExit` carrying the message. Raised from the ASGI lifespan
    startup, uvicorn logs "Application startup failed. Exiting." and the
    process exits non-zero, so an orchestrator restarts (and keeps
    restarting) instead of routing traffic at a container that cannot serve
    it.

    The message is BOTH logged and carried on the exception on purpose. The
    log line is the one an operator reads; the exception payload is the one
    that survives a logging configuration this module does not control —
    uvicorn's own logger prints the lifespan traceback, so the names reach
    the container log even if this module's logger has no handler. Losing
    the names to a logging detail would leave "Application startup failed"
    as the only signal, which is the #654 debugging experience again.
    """
    missing = missing_required_env(environ, source_root)
    if not missing:
        return
    message = format_missing_env_message(missing)
    logger.error(message)
    raise SystemExit(message)


# ---------------------------------------------------------------------------
# Entity-roster table provisioning — issue #59 (audit finding F9 / action A9)
# ---------------------------------------------------------------------------
#
# `src/entity_roster.py` used to call `dynamodb.create_table` every time it
# wanted a table handle and swallow `ResourceInUseException`. That put a
# write-class API call on a read path (`GET /api/admin/entity-roster` and
# every review's prompt assembly via `resolve_entity_roster`), required
# `dynamodb:CreateTable` on the API role, and surfaced as log noise under
# throttling. Provisioning belongs here: once, at boot, with the deployment
# target deciding whether creating it is even legitimate.
#
# Target split (`src/config.py::deploy_target`, NOT `auth_mode`):
#   dts — Docker Compose. `deploy/dts/bootstrap.py` already creates this
#         table; this helper is belt-and-braces for a compose that skipped
#         bootstrap, so it tolerates losing the race with it.
#   aws — the table is CDK-managed (`infra/lib/nested/data-stack.ts`
#         `EntityRosterTable`). A table created at runtime there would carry
#         no CMK, no PITR and no removal policy, so a missing one is a
#         deployment fault and refuses the boot by name.
#
# The boto3 resource is INJECTED rather than built here so this module keeps
# the property the rest of it has: importable with no boto3 present and with
# no client constructed as an import side effect. `src/main.py`'s lifespan
# passes the resource it already built. `entity_roster` and `config` are
# imported lazily inside the function for the same reason.

#: Named in the refusal message so an operator reads the variable to fix, not
#: a stack trace. The value is never logged — this module's "names only,
#: never values" rule covers this path too.
ENTITY_ROSTER_TABLE_ENV = "ENTITY_ROSTER_TABLE"


def _error_code(exc: BaseException) -> str:
    """The DynamoDB error code on a botocore ClientError, or `""`. Duck-typed
    rather than `except ClientError` so this module needs no botocore import
    (see the module note above)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
        if isinstance(code, str):
            return code
    return ""


def ensure_entity_roster_table(dynamodb_resource: object) -> None:
    """Provision the entity-roster table at boot, or refuse the boot.

    ONE `describe_table` on the happy path — no write-class call at all when
    the table is already there, which is every start after the first.

    Missing table:
      - `DEPLOY_TARGET=dts`: created here (PK `setting_id`, PAY_PER_REQUEST),
        tolerating `ResourceInUseException` from a race with
        `deploy/dts/bootstrap.py`.
      - otherwise (`aws`, the default): raises `SystemExit` naming
        `ENTITY_ROSTER_TABLE`. Same failure mode as `verify_required_env`:
        raised from the ASGI lifespan, uvicorn exits non-zero and the
        orchestrator restarts instead of routing traffic at a container whose
        roster store does not exist.

    Any OTHER DynamoDB error (throttle, transient 5xx, a `DescribeTable`
    permission gap) is logged and swallowed. The roster is a degrade-to-`()`
    path by design — `resolve_entity_roster` never raises — so a blip here
    must not wedge the whole API. Only the one condition this helper exists
    to catch, a table that is genuinely not there, is fatal.
    """
    from src import config, entity_roster  # lazy: keeps this module boto-free

    table_name = entity_roster._entity_roster_table_name()
    client = dynamodb_resource.meta.client  # type: ignore[attr-defined]

    try:
        client.describe_table(TableName=table_name)
        return
    except Exception as exc:  # noqa: BLE001 - re-raised or degraded below
        if _error_code(exc) != "ResourceNotFoundException":
            logger.warning(
                "STARTUP: could not confirm the entity-roster table; the roster "
                "degrades to the playbook's own perspective.party until it can "
                "be read (%s).",
                type(exc).__name__,
            )
            return

    if config.deploy_target() != "dts":
        message = (
            "STARTUP: refusing to start — the entity-roster table named by "
            f"{ENTITY_ROSTER_TABLE_ENV} does not exist. On this deployment "
            "target the table is created by the infrastructure stack "
            "(infra/lib/nested/data-stack.ts), never by the API at runtime: "
            f"deploy the stack, or point {ENTITY_ROSTER_TABLE_ENV} at the "
            "table it created, and restart."
        )
        logger.error(message)
        raise SystemExit(message)

    try:
        dynamodb_resource.create_table(  # type: ignore[attr-defined]
            TableName=table_name,
            KeySchema=[{"AttributeName": "setting_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ResourceInUseException":
            # deploy/dts/bootstrap.py won the race. That is the expected
            # outcome on a normally-bootstrapped compose, not a problem.
            return
        raise
    # CREATING -> ACTIVE before the first request arrives: this runs in the
    # lifespan, so waiting here costs a slower boot and not a 500.
    try:
        client.get_waiter("table_exists").wait(TableName=table_name)
    except Exception:  # noqa: BLE001 - the table is created; the wait is a courtesy
        logger.warning("STARTUP: entity-roster table created but did not confirm ACTIVE.")
    logger.info("STARTUP: created the entity-roster table for the Docker Compose target.")
