#!/usr/bin/env python3
"""
Python-interpreter parity gate — issue #63.

WHY THIS FILE EXISTS
  Before #63 this repo ran three different Pythons and declared none of them:
  16 workflows pinned `python-version: '3.11'`, `deploy/dts/backend.Dockerfile`
  built `FROM python:3.12-slim`, and local development was whatever `python3`
  happened to be (3.14 on the machine this was written on, with a 3.11 `.venv`).
  Issue #654 was the same failure on the Node side.

  Owner decision Q5 standardised on **3.13**. Four places now DECLARE that, and
  nothing but this file stops them drifting apart again:

    1. `.python-version`                    — what a version manager reads
    2. `pyproject.toml` `requires-python`   — what pip refuses to install under
    3. `deploy/dts/backend.Dockerfile`      — the interpreter the DTS image ships
    4. every `python-version:` in a workflow — the interpreter CI actually runs

  `.python-version` is the anchor: checks 2-4 are compared against whatever it
  says, so a future bump is a one-line change plus the guard in check 1.

WHAT THIS FILE CANNOT DO
  It compares DECLARATIONS, not behaviour. That every pinned wheel in
  `backend/requirements.txt` actually installs on 3.13 is proved by BUILDING
  `deploy/dts/backend.Dockerfile` (the issue's own verification block requires
  it), not here — the local gate runs on a 3.11 venv and could never tell.
  `scripts/check.sh` prints the interpreter it is really using and warns on a
  mismatch with `.python-version` for exactly this reason.

SCOPE
  `backend/Dockerfile` (the AWS App Runner image, built from the `backend/`
  context) is deliberately NOT covered: #63's change list names only the DTS
  image, and that path promotes signed image digests on its own schedule. It
  still says `FROM python:3.12-slim`. Do not add it here without also proving
  that image builds.

WHAT IT CHECKS (exit 0 = all pass, 1 = one or more fail)

  0. Self-checks against synthetic workflow files, so the scanner cannot go
     quietly blind: a literal pin that disagrees, an `${{ env.X }}` pin that
     disagrees, and an `${{ env.X }}` with no definition must ALL be reported,
     and agreeing pins in both forms must pass.
  1. `.python-version` exists and reads `3.13` (the owner decision).
  2. `pyproject.toml` declares `requires-python = ">=3.13,<3.14"`.
  3. `deploy/dts/backend.Dockerfile` is `FROM python:3.13-slim`.
  4. Every `python-version:` in `.github/workflows/*.yml` resolves to 3.13,
     `${{ env.PYTHON_VERSION }}` included (resolved from that workflow's own
     `env:` block, which is where GitHub resolves it from).
  5. The set of workflows carrying a pin equals the frozen list below, so a new
     workflow with a stray pin is a red gate rather than a silent third Python.
  6. Coverage floor: the real tree still exercises BOTH pin forms. If a refactor
     left no `${{ env.… }}` pin behind, the resolver in check 4 would be dead
     code passing vacuously, and this goes red so someone looks.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# WORKFLOWS_DIR/REPO_ROOT/_assert are the ones the sibling CI-parity gate
# already uses, so both files enumerate the same tree by the same rule.
from test_ci_env_parity_639 import (  # noqa: E402
    REPO_ROOT,
    WORKFLOWS_DIR,
    _assert,
)

PYTHON_VERSION_FILE = REPO_ROOT / ".python-version"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DTS_DOCKERFILE = REPO_ROOT / "deploy" / "dts" / "backend.Dockerfile"

# The owner decision (Q5). Bumping the toolchain means changing this line, the
# four declarations it guards, and the docs that quote them.
DECIDED_VERSION = "3.13"

# Every workflow that pins a Python today, frozen. Check 5 compares the SCANNED
# set against this, in both directions: a new workflow with a pin fails until
# someone adds it here (and therefore looks at what it pinned), and deleting a
# workflow without updating this list fails too.
EXPECTED_PINNED_WORKFLOWS = frozenset(
    {
        "auth-stack-gate.yml",
        "bedrock-access-gate.yml",
        "bedrock-kb-gate.yml",
        "brand-free-gate.yml",
        "ci-pipeline-gate.yml",
        "ci-pipeline.yml",
        "codeowners-coverage.yml",
        "cost-model-lint.yml",
        "counterparty-name-gate.yml",
        "dependency-audit.yml",
        "detector-correctness.yml",
        "docs-lint.yml",
        "fixture-deident-gate.yml",
        "frontend-stack-gate.yml",
        "identity-auth-threat-model.yml",
        "input-normalization-gate.yml",
        "output-schema.yml",
        "pipeline-execution-history-gate.yml",
        "playbook-lint.yml",
        "prompt-manifest-lint.yml",
        "split-api-role-waf-gate.yml",
    }
)

# `python-version: '3.13'` / `"3.13"` / `${{ env.PYTHON_VERSION }}`, trailing
# comment tolerated. Matched on raw lines rather than parsed YAML for the same
# reason as the sibling gate: PyYAML is not in requirements-dev.txt and every
# other workflow gate in tests/ reads these files as text.
PIN_RE = re.compile(r"^\s*python-version:\s*(\S.*?)\s*$")
ENV_KEY_RE = re.compile(r"^(\s*)env:\s*(?:#.*)?$")
ENV_ENTRY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*):\s*(\S.*?)\s*$")
ENV_EXPR_RE = re.compile(r"^\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}$")
FROM_RE = re.compile(r"^FROM\s+python:(\S+)\s*$", re.MULTILINE)
REQUIRES_PYTHON_RE = re.compile(
    r"^\s*requires-python\s*=\s*[\"'](.+?)[\"']\s*$", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _strip_comment_and_quotes(value: str) -> str:
    """`'3.13'  # why` -> `3.13`. Quoted values keep their content verbatim."""
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]
        return value[1:]
    return value.split("#", 1)[0].strip()


def workflow_env(text: str) -> dict[str, str]:
    """Every `KEY: value` under any `env:` mapping in one workflow file.

    Workflow-level and job-level `env:` are merged into one namespace, which is
    coarser than GitHub's scoping but strictly SAFER for this gate: a name
    defined twice with different values is reported rather than resolved, so a
    job-scoped override can never let a stray pin through unnoticed.
    """
    env: dict[str, str] = {}
    conflicts: set[str] = set()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        header = ENV_KEY_RE.match(lines[i])
        if not header:
            i += 1
            continue
        base_indent = len(header.group(1))
        i += 1
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                i += 1
                continue
            entry = ENV_ENTRY_RE.match(line)
            if entry is None or len(entry.group(1)) <= base_indent:
                break
            name = entry.group(2)
            value = _strip_comment_and_quotes(entry.group(3))
            if name in env and env[name] != value:
                conflicts.add(name)
            env[name] = value
            i += 1
    for name in conflicts:
        env[name] = f"<conflicting definitions of {name}>"
    return env


def python_pins(path: Path) -> list[tuple[int, str, str | None]]:
    """[(line number, raw value, resolved value or None)] for one workflow."""
    text = path.read_text(encoding="utf-8")
    env = workflow_env(text)
    found: list[tuple[int, str, str | None]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = PIN_RE.match(line)
        if not match:
            continue
        raw = _strip_comment_and_quotes(match.group(1))
        expr = ENV_EXPR_RE.match(raw)
        if expr:
            found.append((lineno, raw, env.get(expr.group(1))))
        else:
            found.append((lineno, raw, raw))
    return found


def workflow_files(workflows_dir: Path) -> list[Path]:
    return sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))


def scan(workflows_dir: Path) -> dict[str, list[tuple[int, str, str | None]]]:
    """{workflow filename: its pins}, only for files that carry at least one."""
    out: dict[str, list[tuple[int, str, str | None]]] = {}
    for path in workflow_files(workflows_dir):
        pins = python_pins(path)
        if pins:
            out[path.name] = pins
    return out


# ---------------------------------------------------------------------------
# Check 0 — self-checks on synthetic workflows
# ---------------------------------------------------------------------------

_GOOD_LITERAL = """\
name: literal
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
"""

_GOOD_ENV = """\
name: env-indirect
on: [push]
env:
  PYTHON_VERSION: '3.13'  # pinned by issue #63
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
"""

_STRAY_LITERAL = _GOOD_LITERAL.replace("'3.13'", '"3.11"')
_STRAY_ENV = _GOOD_ENV.replace("PYTHON_VERSION: '3.13'", "PYTHON_VERSION: '3.11'")
_UNRESOLVABLE_ENV = _GOOD_ENV.replace("env:\n  PYTHON_VERSION: '3.13'  # pinned by issue #63\n", "")


def check_self() -> list[str]:
    print("\nCheck 0 — the scanner catches what it is supposed to catch")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, body in (
            ("good-literal.yml", _GOOD_LITERAL),
            ("good-env.yml", _GOOD_ENV),
            ("stray-literal.yml", _STRAY_LITERAL),
            ("stray-env.yml", _STRAY_ENV),
            ("unresolvable-env.yml", _UNRESOLVABLE_ENV),
        ):
            (root / name).write_text(body, encoding="utf-8")
        scanned = scan(root)

        failures += _assert(
            set(scanned) == {
                "good-literal.yml",
                "good-env.yml",
                "stray-literal.yml",
                "stray-env.yml",
                "unresolvable-env.yml",
            },
            "0a every synthetic workflow with a pin is enumerated",
            f"found {sorted(scanned)}",
        )
        failures += _assert(
            scanned.get("good-literal.yml", [(0, "", None)])[0][2] == "3.13",
            "0b a quoted literal pin resolves to itself",
            f"got {scanned.get('good-literal.yml')}",
        )
        failures += _assert(
            scanned.get("good-env.yml", [(0, "", None)])[0][2] == "3.13",
            "0c an ${{ env.X }} pin resolves through the workflow's env block",
            f"got {scanned.get('good-env.yml')}",
        )
        failures += _assert(
            scanned.get("stray-literal.yml", [(0, "", None)])[0][2] == "3.11",
            "0d a disagreeing literal pin is reported with its real value",
            f"got {scanned.get('stray-literal.yml')}",
        )
        failures += _assert(
            scanned.get("stray-env.yml", [(0, "", None)])[0][2] == "3.11",
            "0e a disagreeing ${{ env.X }} pin is reported with its real value",
            f"got {scanned.get('stray-env.yml')}",
        )
        failures += _assert(
            scanned.get("unresolvable-env.yml", [(0, "", "x")])[0][2] is None,
            "0f an ${{ env.X }} with no definition resolves to None, not silence",
            f"got {scanned.get('unresolvable-env.yml')}",
        )
        conflicting = workflow_env(
            "env:\n  PYTHON_VERSION: '3.13'\njobs:\n  a:\n    env:\n"
            "      PYTHON_VERSION: '3.11'\n"
        )["PYTHON_VERSION"]
        failures += _assert(
            conflicting.startswith("<conflicting"),
            "0g two disagreeing definitions of one env name resolve to neither",
            f"got {conflicting!r}",
        )
    return failures


# ---------------------------------------------------------------------------
# Checks 1-6 — the real tree
# ---------------------------------------------------------------------------

def check_declarations() -> tuple[list[str], str | None]:
    print("\nChecks 1-3 — .python-version, pyproject.toml, the DTS image")
    failures: list[str] = []

    declared: str | None = None
    if not PYTHON_VERSION_FILE.is_file():
        failures += _assert(False, "1 .python-version exists", str(PYTHON_VERSION_FILE))
    else:
        declared = PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
        failures += _assert(
            declared == DECIDED_VERSION,
            f"1 .python-version is {DECIDED_VERSION} (owner decision Q5)",
            f"reads {declared!r}",
        )

    major_minor = declared or DECIDED_VERSION
    major, _, minor = major_minor.partition(".")
    try:
        upper = f"{major}.{int(minor) + 1}"
    except ValueError:
        upper = ""
    expected_requires = f">={major_minor},<{upper}"

    if not PYPROJECT.is_file():
        failures += _assert(False, "2 pyproject.toml exists", str(PYPROJECT))
    else:
        found = REQUIRES_PYTHON_RE.search(PYPROJECT.read_text(encoding="utf-8"))
        failures += _assert(
            found is not None and found.group(1) == expected_requires,
            f"2 pyproject.toml requires-python is {expected_requires!r}",
            f"reads {found.group(1)!r}" if found else "no requires-python declared",
        )

    if not DTS_DOCKERFILE.is_file():
        failures += _assert(False, "3 the DTS Dockerfile exists", str(DTS_DOCKERFILE))
    else:
        tags = FROM_RE.findall(DTS_DOCKERFILE.read_text(encoding="utf-8"))
        failures += _assert(
            tags == [f"{major_minor}-slim"],
            f"3 deploy/dts/backend.Dockerfile is FROM python:{major_minor}-slim",
            f"FROM python: tags are {tags}",
        )

    return failures, declared


def check_workflow_pins(declared: str | None) -> list[str]:
    print("\nChecks 4-6 — every workflow pin")
    failures: list[str] = []
    expected = declared or DECIDED_VERSION
    scanned = scan(WORKFLOWS_DIR)
    print(
        f"  (scanned {len(workflow_files(WORKFLOWS_DIR))} workflow files, "
        f"{sum(len(v) for v in scanned.values())} python-version pins "
        f"across {len(scanned)} of them)"
    )

    wrong: list[str] = []
    literal_forms = 0
    env_forms = 0
    for name, pins in sorted(scanned.items()):
        for lineno, raw, resolved in pins:
            if ENV_EXPR_RE.match(raw):
                env_forms += 1
            else:
                literal_forms += 1
            if resolved != expected:
                shown = "unresolvable" if resolved is None else repr(resolved)
                wrong.append(f"{name}:{lineno} {raw!r} -> {shown}")
    failures += _assert(
        not wrong,
        f"4 every workflow python-version resolves to {expected}",
        "\n         ".join(wrong),
    )

    missing = EXPECTED_PINNED_WORKFLOWS - set(scanned)
    extra = set(scanned) - EXPECTED_PINNED_WORKFLOWS
    detail = []
    if extra:
        detail.append(f"not in the frozen list: {sorted(extra)}")
    if missing:
        detail.append(f"no longer pins a Python: {sorted(missing)}")
    failures += _assert(
        not missing and not extra,
        "5 the set of workflows pinning a Python is the frozen list",
        "; ".join(detail),
    )

    failures += _assert(
        literal_forms > 0 and env_forms > 0,
        "6 the real tree still exercises both pin forms (literal and env)",
        f"{literal_forms} literal, {env_forms} via ${{{{ env.… }}}}",
    )
    return failures


def main() -> int:
    print("Python-interpreter parity gate (issue #63)")
    print("=" * 72)
    failures: list[str] = []
    failures += check_self()
    declaration_failures, declared = check_declarations()
    failures += declaration_failures
    failures += check_workflow_pins(declared)
    print("=" * 72)
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print(f"PASS: CI, the DTS image and local dev all declare Python {DECIDED_VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
