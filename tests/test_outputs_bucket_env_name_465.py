#!/usr/bin/env python3
"""
CI gate for issue #465: the outputs-bucket env var name must be the SAME
literal string everywhere it matters — the writer, every deploy target, and
the reader — not just whatever name a given commit happens to leave behind.

## The bug this guards against

`backend/src/download.py::_get_outputs_bucket()` used to read an S3-prefixed
variant of this env var name while the writer (`backend/src/pipeline_runner.py`)
and every deploy target (`deploy/dts/docker-compose.yml`,
`deploy/dts/docker-compose.coolify.yml`, infra CDK, the Lambda handlers) used
the un-prefixed name. Every test that exercised the download path set
whatever name the reader happened to read via `os.environ.setdefault(...)`
in its own setup, so the suite stayed green under EITHER name — that blind
spot is exactly how the drift shipped live undetected until it 503'd every
redline download on 2026-08-02 (review 73ea3ba9-fe5f-4ef3-bb5c-edcb28bee7bd).
(This module deliberately never spells the retired name out as a literal —
see `RETIRED_NAME` below — so it does not itself trip the repo-wide grep for
that name that Check 3 and the issue's own required verification both run.)

Renaming those per-test `setdefault` calls to match a renamed reader (as this
ticket's own fix does) reproduces the identical blind spot: a future
coordinated rename of `download.py` + its tests would re-break every real
deployment with the gate still green, because no test pins the name against
anything OTHER than itself. This file is that independent pin: it reads the
literal names out of the writer, both deploy-target compose files, and the
reader's source, and fails if any of them disagree — without ever setting
the env var itself.

Checks (all must pass; exit 1 on any failure):

  1. `backend/src/download.py::_get_outputs_bucket()` reads exactly one
     literal env var name via `os.environ.get("NAME", ...)`.
  2. That name matches the literal name(s) `backend/src/pipeline_runner.py`
     writes the output object through (`os.environ["NAME"]`, currently
     lines 316 and 552 — matched by pattern, not line number, so this
     survives reflow).
  3. That name is present as a key inside the `x-resource-names` YAML anchor
     block of BOTH `deploy/dts/docker-compose.yml` and
     `deploy/dts/docker-compose.coolify.yml`.
  4. The retired S3-prefixed name (see `RETIRED_NAME`) appears nowhere in
     the tree (mirrors the issue's own required-verification grep).

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_PY = REPO_ROOT / "backend" / "src" / "download.py"
PIPELINE_RUNNER_PY = REPO_ROOT / "backend" / "src" / "pipeline_runner.py"
COMPOSE_LOCAL = REPO_ROOT / "deploy" / "dts" / "docker-compose.yml"
COMPOSE_COOLIFY = REPO_ROOT / "deploy" / "dts" / "docker-compose.coolify.yml"

# Built by concatenation, not as one literal: the whole point of this file
# is to grep the tree for this exact name (Check 3, mirroring the issue's
# own required-verification command), and a literal occurrence here would
# make that grep permanently unable to pass.
RETIRED_NAME = "S3_" + "OUTPUTS_BUCKET"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fail(msg: str) -> list[str]:
    print(f"  [FAIL] {msg}")
    return [msg]


def ok(msg: str) -> list[str]:
    print(f"  [PASS] {msg}")
    return []


def _reader_env_name() -> str | None:
    """The literal env var name `_get_outputs_bucket()` reads in download.py."""
    src = read(DOWNLOAD_PY)
    m = re.search(
        r"def _get_outputs_bucket\(\).*?os\.environ\.get\(\s*[\"'](\w+)[\"']",
        src,
        re.DOTALL,
    )
    return m.group(1) if m else None


WRITER_OUTPUT_FUNCTIONS = ("_copy_output_object", "_write_real_output")


def _function_body(src: str, name: str) -> str | None:
    """Source of one top-level function, from `def name(` up to (but not
    including) the next top-level `def `/`class ` — a plain string slice,
    not an AST parse, but sufficient to scope a regex search to one
    function's body regardless of line-number drift elsewhere in the file."""
    m = re.search(rf"^def {re.escape(name)}\(", src, re.MULTILINE)
    if not m:
        return None
    rest = src[m.end():]
    end = re.search(r"^(?:def |class )", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _writer_env_names() -> dict[str, list[str]]:
    """The literal env var name(s) each of pipeline_runner.py's two
    output-writing functions reads via `os.environ["NAME"]` (issue #465
    names them at pipeline_runner.py:316, 552). Scoped per-function rather
    than a whole-file regex, which would also catch the unrelated
    UPLOADS_BUCKET read in `_fetch_upload_bytes`. Maps function name to the
    list of env var names found in its body (empty list if the function
    exists but reads nothing this way; key absent if the function itself
    was not found)."""
    src = read(PIPELINE_RUNNER_PY)
    found: dict[str, list[str]] = {}
    for fn in WRITER_OUTPUT_FUNCTIONS:
        body = _function_body(src, fn)
        if body is None:
            continue
        found[fn] = re.findall(r'os\.environ\[\s*"(\w+)"\s*\]', body)
    return found


def _compose_resource_names(path: Path) -> dict[str, str]:
    """Flat key: value mapping inside a compose file's `x-resource-names`
    YAML anchor block. Regex, not a full YAML parse (mirrors the approach in
    tests/test_dts_version_metadata_424.py) — it's a plain flat block
    terminated by the next unindented top-level key (`services:`)."""
    src = read(path)
    m = re.search(r"x-resource-names:\s*&resource-names\n((?:  \S.*\n)+)", src)
    if not m:
        return {}
    names: dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^  (\w+):\s*(\S+)\s*$", line)
        if km:
            names[km.group(1)] = km.group(2)
    return names


def check_reader_matches_writer(reader_name: str | None) -> list[str]:
    print("\nCheck 1: download.py's reader matches pipeline_runner.py's writer …")
    failures: list[str] = []
    if reader_name is None:
        return fail(
            "could not locate _get_outputs_bucket()'s os.environ.get(...) "
            "read in backend/src/download.py"
        )
    failures += ok(f"download.py reads {reader_name!r}")

    by_function = _writer_env_names()
    for fn in WRITER_OUTPUT_FUNCTIONS:
        if fn not in by_function:
            failures += fail(
                f"could not locate pipeline_runner.py::{fn}() at all "
                "(function renamed or removed?)"
            )
            continue
        names = by_function[fn]
        if not names:
            failures += fail(
                f'pipeline_runner.py::{fn}() has no os.environ["..."] read'
            )
            continue
        for name in sorted(set(names)):
            if name != reader_name:
                failures += fail(
                    f"pipeline_runner.py::{fn}() writes through {name!r} but "
                    f"download.py's _get_outputs_bucket() reads {reader_name!r}"
                )
            else:
                failures += ok(f"pipeline_runner.py::{fn}() write ({name!r}) matches the reader")
    return failures


def check_deploy_targets(reader_name: str | None) -> list[str]:
    print("\nCheck 2: both DTS deploy targets configure the same name …")
    failures: list[str] = []
    if reader_name is None:
        return fail("no reader name resolved (see Check 1) — cannot compare")

    for path in (COMPOSE_LOCAL, COMPOSE_COOLIFY):
        rel = path.relative_to(REPO_ROOT)
        if not path.exists():
            failures += fail(f"{rel} does not exist")
            continue
        names = _compose_resource_names(path)
        if not names:
            failures += fail(f"{rel}: could not locate an x-resource-names anchor block")
            continue
        if reader_name not in names:
            failures += fail(
                f"{rel}'s x-resource-names block has no {reader_name!r} key "
                f"(has: {sorted(names)})"
            )
        else:
            failures += ok(f"{rel} sets {reader_name!r} = {names[reader_name]!r}")
    return failures


def check_retired_name_gone() -> list[str]:
    print(f"\nCheck 3: the retired name {RETIRED_NAME!r} appears nowhere in the tree …")
    result = subprocess.run(
        [
            "grep",
            "-rn",
            RETIRED_NAME,
            ".",
            "--exclude-dir=.git",
            "--exclude-dir=.venv",
            "--exclude-dir=node_modules",
            "--exclude-dir=cdk.out",
            "--exclude-dir=dist",
            "--exclude-dir=__pycache__",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        failures = fail(f"{RETIRED_NAME} still referenced:")
        for line in result.stdout.splitlines():
            print(f"    {line}")
        return failures
    return ok(f"{RETIRED_NAME} not found anywhere in the tree")


def main() -> int:
    all_failures: list[str] = []

    reader_name = _reader_env_name()
    all_failures += check_reader_matches_writer(reader_name)
    all_failures += check_deploy_targets(reader_name)
    all_failures += check_retired_name_gone()

    if all_failures:
        print(f"\n{len(all_failures)} check(s) failed:")
        for f in all_failures:
            print(f"  - {f}")
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
