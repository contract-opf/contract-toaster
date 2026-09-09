#!/usr/bin/env python3
"""
CI-environment parity gate — issue #639.

WHY THIS FILE EXISTS
  `SKIP_INFRA=1 bash scripts/check.sh` is what everything here treats as the
  pre-push signal, but it is NOT a proxy for CI, and on 2026-08-31 it returned
  `CHECK: ALL GREEN` / exit 0 on the exact tree that then failed two
  independent CI gates on the push to main:

    1. DEPENDENCY ENVIRONMENT. Most CI jobs run `python3 tests/<file>.py`
       directly on an `actions/setup-python` interpreter with NO project
       dependencies installed; every local run uses a venv that has them.
       `1aef16e` added `import primary_review_pass` to
       tests/test_output_schema.py, which pulls `model_client` -> `jsonschema`,
       and `.github/workflows/output-schema.yml` runs that file before the one
       step that installs jsonschema:

           ModuleNotFoundError: No module named 'jsonschema'

       Green locally, red in CI. Fixed in `97b0fa2`; nothing would have caught
       it beforehand. Issue #638 is the same shape found in another job (a gate
       that had been skipping on a missing `boto3` since it was written), and
       the playbook-lint incident is a third.

    2. CLOCK. The runner is UTC; a developer machine here is UTC-4.
       tests/test_audit_query_api_93.py failed in CI at 2026-09-01T01:02:39Z
       while the same commit's local gate passed on a machine still on 08-31.
       Fixed in `bed1fa9`. See Check 4.

  Neither failure was caused by the change under test. This file is the guard
  for that whole category: it reproduces the CI jobs' dependency conditions
  statically, from the workflow files themselves.

WHAT IT CHECKS (exit 0 = all pass, 1 = one or more fail)

  0. Self-checks against synthetic workflows and a synthetic module tree, PLUS
     a coverage floor over the real workflow tree, so this file's own machinery
     cannot go quietly blind. In particular 0a proves the scanner ENUMERATES
     workflow commands rather than consulting a hand-written list: a brand-new
     job invoking a brand-new dependency-needing test is detected with no edit
     to this file. A hand-picked list is the exact failure mode that left main
     red for two days in the playbook-lint incident, so it is the property most
     worth pinning.

     The coverage floor (0h) is the second half of that guarantee. A scanner
     that returns an empty list for input it cannot parse makes every check
     built on top of it pass VACUOUSLY: `jobs:  # a trailing comment` is valid
     YAML, and an exact-match line scanner reading it as "no jobs here" would
     drop the whole file, printing PASS while checking nothing. So the scanner
     tolerates trailing comments and arbitrary indentation on `jobs:`/`steps:`
     and job keys (0e, 0f), and — belt and braces, because the next YAML shape
     nobody anticipated is the one that matters — a file it still fails to read
     is a LOUD failure (0g): every workflow file must yield at least one job,
     and the step bodies parsed out of it must account for every `run:` key a
     raw regex finds in its text.

  1. Every repo `.py` file a workflow invokes actually exists.

  2. BARE-INTERPRETER PARITY. For every workflow step that runs a repo `.py`
     file, the third-party top-level modules that file HARD-imports —
     transitively, through repo-local modules — must all be installed by an
     EARLIER step of the same job. Step order is load-bearing:
     output-schema.yml deliberately installs jsonschema in the middle of its
     job and states the invariant above that step ("Only the v3 step below
     needs it; every other step in this job is stdlib-only"), so a check that
     ignored order would have passed the tree that broke.

  3. DRIFT GUARD. Every `tests/…` file a workflow invokes must also be
     discovered by the local gate — i.e. it must match one of the globs in
     scripts/collect_test_failures.sh, the one loop shared by scripts/check.sh
     and CI GATE A. The glob list is READ from that script, not restated here.
     This is the generalisation of #634 (a suite nothing ran).

  4. CLOCK PARITY. scripts/check.sh pins the gate's timezone to the runner's
     (UTC), so a date-boundary divergence surfaces locally instead of on main.
     The RESOLVED VALUE is what is asserted, not the presence of the token
     "UTC": the export's `${CHECK_TZ:-<default>}` default is parsed, required
     to be UTC, and handed to libc to confirm it really is a zero-offset clock;
     an export that is indented (so conditionally reached) or that sits below
     the line running the suite is rejected as dead code. Grepping the file for
     "UTC" would have been satisfied by this script's own header prose — see
     the doctored-script self-checks 0i-0i''.

WHAT IT DOES NOT CHECK
  Only HARD imports count — a module-level `import x` that is not inside a
  `try:`, a function or a class. An optional import behind try/except is a
  deliberate degrade-to-skip and does not crash the interpreter, so it is not
  a CI-parity failure. That such a skip can be permanently invisible in CI is
  a real problem, but it is issue #638's, not this file's.

Run with: python3 tests/test_ci_env_parity_639.py
"""

from __future__ import annotations

import ast
import importlib.metadata
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
COLLECT_SCRIPT = REPO_ROOT / "scripts" / "collect_test_failures.sh"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check.sh"

STDLIB = set(sys.stdlib_module_names)

# Repo-local module roots. A top-level import that resolves to a file under one
# of these is repo code, not a dependency, whatever sys.path happens to say.
# Files that put an UNUSUAL directory on sys.path (e.g.
# tests/test_pipeline_execution_history_scan.py importing the real
# infra/lambda/mock_review/handler.py) are handled by reading their own
# `sys.path.insert(...)` expressions — see _syspath_dirs().
MODULE_ROOTS = (
    REPO_ROOT,
    REPO_ROOT / "scripts",
    REPO_ROOT / "backend",
    REPO_ROOT / "backend" / "src",
    REPO_ROOT / "tests",
)

# `python3 tests/foo.py`, `python scripts/bar.py`. Anchored on a token boundary
# so `python3 -m pip install …` and heredoc prose do not match.
INVOCATION_RE = re.compile(
    r"(?:^|[\s;&|(])python3?\s+((?:tests|scripts)/[\w./-]+\.py)"
)
PIP_INSTALL_RE = re.compile(r"(?:^|[\s;&|(])(?:python3?\s+-m\s+)?pip\s+install\s+(.*)$")

# YAML keys the line scanner navigates by. All three tolerate a trailing
# comment, because `jobs:  # the output-contract gates` is perfectly valid YAML
# and an exact-match scanner would read it as "this file has no jobs" — see the
# coverage floor in check_self()'s 0e-0h.
JOBS_KEY_RE = re.compile(r"^jobs:\s*(?:#.*)?$")
JOB_HEADER_RE = re.compile(r"^(\s+)([A-Za-z0-9_-]+):\s*(?:#.*)?$")
STEPS_KEY_RE = re.compile(r"^\s*steps:\s*(?:#.*)?$")

# A `run:` key as it appears in the raw file text, used ONLY to cross-check the
# parser against the bytes on disk (0h). Deliberately independent of the step
# splitter: if the two ever disagree, the parser has gone blind to part of a
# file and the gate must go red rather than scan less and still print PASS.
RAW_RUN_KEY_RE = re.compile(r"^\s*(?:-\s+)?run:", re.MULTILINE)


# ---------------------------------------------------------------------------
# Reporting helpers (same shape as tests/test_frontend_gate_wired_634.py)
# ---------------------------------------------------------------------------

def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


# ---------------------------------------------------------------------------
# Workflow scanning — enumerate, never hand-list
# ---------------------------------------------------------------------------

class Step:
    """One workflow step: which job it belongs to, its name, its shell body."""

    def __init__(self, workflow: str, job: str, index: int, name: str, run: str):
        self.workflow = workflow
        self.job = job
        self.index = index
        self.name = name
        self.run = run

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<Step {self.workflow}:{self.job}[{self.index}] {self.name!r}>"

    @property
    def where(self) -> str:
        return f"{self.workflow} / job `{self.job}` / step {self.index} ({self.name!r})"


def _split_jobs(text: str) -> list[tuple[str, list[str]]]:
    """[(job id, the job's own lines)] in file order.

    A small line scanner rather than a YAML parse: PyYAML is not in
    requirements-dev.txt, and every other workflow gate in tests/ reads these
    files as text (tests/test_ci_pipeline.py, tests/test_frontend_gate_wired_634.py).

    It is deliberately loose about whitespace and comments, because a scanner
    that silently yields nothing is worse than no scanner at all: the job-key
    column is READ from the first job header rather than assumed to be 2, and
    both `jobs:` and the job keys may carry a trailing `# comment`. Whatever
    this still fails to read is caught by the coverage floor, not swallowed.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if JOBS_KEY_RE.match(line))
    except StopIteration:
        return []

    jobs: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    job_indent: int | None = None
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            if current is not None:
                current.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            if stripped.startswith("#"):
                continue  # a column-0 comment does not end the jobs: mapping
            break  # a non-indented key ends the jobs: mapping
        header = JOB_HEADER_RE.match(line)
        if header and job_indent is None:
            job_indent = len(header.group(1))
        if header and indent == job_indent:
            current = []
            jobs.append((header.group(2), current))
            continue
        if current is not None:
            current.append(line)
    return jobs


def _split_steps(job_lines: list[str]) -> list[tuple[str, list[str]]]:
    """[(step name, the step's own lines)] in ORDER, for one job's lines.

    Order is the whole point: a `pip install` step only provisions the steps
    that come after it. `steps:` may carry a trailing comment, for the same
    reason `jobs:` may — see _split_jobs.
    """
    try:
        steps_at = next(
            i for i, line in enumerate(job_lines) if STEPS_KEY_RE.match(line)
        )
    except StopIteration:
        return []
    body = job_lines[steps_at + 1:]

    item_re = re.compile(r"^(\s+)-\s")
    item_indent = None
    steps: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in body:
        m = item_re.match(line)
        if m and (item_indent is None or len(m.group(1)) == item_indent):
            if item_indent is None:
                item_indent = len(m.group(1))
            current = [line]
            steps.append(("", current))
            continue
        if current is not None:
            current.append(line)

    named: list[tuple[str, list[str]]] = []
    for _, lines in steps:
        name = ""
        for line in lines:
            m = re.match(r"^\s*(?:-\s+)?name:\s*(.+?)\s*$", line)
            if m:
                name = m.group(1).strip("'\"")
                break
        named.append((name, lines))
    return named


def _step_run(step_lines: list[str]) -> str:
    """The step's `run:` shell body, block scalars included, comments dropped.

    Only lines indented STRICTLY deeper than the `run:` key belong to a block
    scalar. That is what keeps a step's trailing YAML comment block — which in
    this repo sits at the same indent as `run:`, directly below it — out of the
    command text. Without it, ci-pipeline.yml's and playbook-lint.yml's
    explanatory comments (one of which quotes `python3 scripts/canonicalize.py`)
    would be read as invocations.
    """
    run_re = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")
    for i, line in enumerate(step_lines):
        m = run_re.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        # A leading "- " on the same line shifts the key's real column.
        if re.match(r"^\s*-\s+run:", line):
            indent = line.index("run:")
        rest = m.group(2).strip()
        if rest and rest[0] not in "|>":
            return rest
        collected = []
        for follow in step_lines[i + 1:]:
            if not follow.strip():
                collected.append("")
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            collected.append(follow.strip())
        return "\n".join(
            line for line in collected if not line.lstrip().startswith("#")
        )
    return ""


def iter_steps(workflows_dir: Path) -> list[Step]:
    """Every step of every job of every workflow, in file and job order."""
    steps: list[Step] = []
    if not workflows_dir.is_dir():
        return steps
    paths = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for job_id, job_lines in _split_jobs(text):
            for index, (name, step_lines) in enumerate(_split_steps(job_lines)):
                steps.append(
                    Step(path.name, job_id, index, name, _step_run(step_lines))
                )
    return steps


def workflow_scan_coverage(workflows_dir: Path) -> list[str]:
    """Workflow files the scanner read LESS of than the file actually contains.

    The coverage floor for everything above. Checks 1-3 all iterate the steps
    iter_steps() hands them, so a file the splitters cannot navigate does not
    make them fail — it makes them pass, over fewer steps, with no trace in the
    output. That is the #638 shape (a gate that had been skipping on a missing
    dependency since it was written) reproduced inside the guard written to
    catch it, and one valid YAML comment used to be enough to trigger it.

    Two floors, both derived from the tree rather than pinned to a count that
    would need editing every time a workflow is added:

      * every `.github/workflows/*.yml|*.yaml` file yields at least one job;
      * the step bodies parsed out of a file account for every `run:` key a raw
        regex finds in that file's text.

    Returns a list of human-readable problems; empty means full coverage.
    """
    problems: list[str] = []
    paths = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        jobs = _split_jobs(text)
        raw_runs = len(RAW_RUN_KEY_RE.findall(text))
        if not jobs:
            problems.append(
                f"{path.name}: the scanner found 0 jobs in a file holding "
                f"{raw_runs} `run:` key(s). Every check in this gate iterates "
                "the steps the scanner yields, so this file is not being "
                "checked at all — fix the parser, do not accept the silence."
            )
            continue
        parsed_runs = 0
        for _job_id, job_lines in jobs:
            for _name, step_lines in _split_steps(job_lines):
                if _step_run(step_lines).strip():
                    parsed_runs += 1
        if parsed_runs != raw_runs:
            problems.append(
                f"{path.name}: {raw_runs} `run:` key(s) in the file text but "
                f"{parsed_runs} step body/bodies parsed. The scanner is reading "
                "less of this file than it contains, so the checks below run "
                "over a subset and still report PASS."
            )
    return problems


def invoked_files(run: str) -> list[str]:
    """Repo-relative .py paths this shell body runs as `python3 <path>`."""
    return [rel for kind, rel in step_events(run) if kind == "invoke"]


def step_events(run: str) -> list[tuple[str, str]]:
    """This shell body's install/invoke events, IN LINE ORDER.

    Line order matters inside a step as much as step order does inside a job:
    `pip install X && python3 tests/y.py` provisions its own invocation, and a
    checker that folded a step's installs in after its invocations would call
    that a violation.
    """
    events: list[tuple[str, str]] = []
    for line in run.splitlines():
        if line.lstrip().startswith("#"):
            continue
        on_line: list[tuple[int, str, str]] = []
        install = PIP_INSTALL_RE.search(line)
        if install:
            on_line.append((install.start(), "install", line))
        for match in INVOCATION_RE.finditer(line):
            on_line.append((match.start(), "invoke", match.group(1)))
        events.extend((kind, payload) for _, kind, payload in sorted(on_line))
    return events


# ---------------------------------------------------------------------------
# What a job has installed, at each point in the job
# ---------------------------------------------------------------------------

def _normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_to_modules() -> dict[str, set[str]]:
    """distribution -> top-level importable module names, from real metadata.

    Read out of the installed environment (importlib.metadata) rather than
    hand-mapped: `python-docx` imports as `docx`, `docx-editor` as
    `docx_editor`, and a hand list of those is exactly the artifact that goes
    stale. A distribution this machine does not have installed falls back to
    its own normalized name, which is right for the overwhelming majority.

    Both places this gate is authoritative have the pinned deps installed —
    scripts/check.sh auto-activates .venv, and CI GATE A installs
    backend/requirements.txt + requirements-dev.txt before running the suite —
    so the mapping is populated wherever the result counts.
    """
    inverted: dict[str, set[str]] = {}
    for module, dists in importlib.metadata.packages_distributions().items():
        top = module.split("/")[0].split(".")[0]
        for dist in dists:
            inverted.setdefault(_normalize_dist(dist), set()).add(top)
    return inverted


_DIST_MODULES = _dist_to_modules()


def _requirement_name(spec: str) -> str | None:
    """'botocore (<1.44.0,>=1.43.44)' -> 'botocore'; extras-only reqs -> None."""
    spec = spec.strip()
    if not spec:
        return None
    if ";" in spec:
        head, marker = spec.split(";", 1)
        if "extra" in marker:
            return None
        spec = head
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", spec.strip())
    return m.group(1) if m else None


def modules_provided_by(dist: str, _seen: set[str] | None = None) -> set[str]:
    """Top-level modules installing `dist` makes importable, transitively.

    Transitive because `pip install boto3` also lands `botocore`, and a test
    that imports botocore is then legitimately satisfied.
    """
    seen = _seen if _seen is not None else set()
    key = _normalize_dist(dist)
    if key in seen:
        return set()
    seen.add(key)
    modules = set(_DIST_MODULES.get(key, {key.replace("-", "_")}))
    try:
        requires = importlib.metadata.requires(key) or []
    except importlib.metadata.PackageNotFoundError:
        requires = []
    for spec in requires:
        name = _requirement_name(spec)
        if name:
            modules |= modules_provided_by(name, seen)
    return modules


def _parse_requirements_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = _requirement_name(line)
        if name:
            names.append(name)
    return names


def modules_installed_by(run: str, root: Path) -> set[str]:
    """Top-level modules the `pip install` commands in this shell body provide."""
    modules: set[str] = set()
    for line in run.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        m = PIP_INSTALL_RE.search(line)
        if not m:
            continue
        try:
            tokens = shlex.split(m.group(1))
        except ValueError:
            tokens = m.group(1).split()
        expect_req_file = False
        for token in tokens:
            if expect_req_file:
                for name in _parse_requirements_file(root / token):
                    modules |= modules_provided_by(name)
                expect_req_file = False
                continue
            if token in ("-r", "--requirement"):
                expect_req_file = True
                continue
            if token.startswith("-"):
                continue
            name = _requirement_name(token)
            if name:
                modules |= modules_provided_by(name)
    return modules


# ---------------------------------------------------------------------------
# What a file needs: third-party top-level modules, transitively
# ---------------------------------------------------------------------------

def _eval_path(node: ast.AST, env: dict[str, Path], this_file: Path) -> Path | None:
    """Best-effort static evaluation of a pathlib expression.

    Handles the shapes this repo actually writes:
    `Path(__file__).resolve().parents[1]`, `REPO_ROOT / "infra" / "lambda"`,
    `str(SCRIPTS_DIR)`. Anything else evaluates to None and is simply not
    treated as a sys.path entry.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return Path(node.value)
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return this_file
        return env.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _eval_path(node.left, env, this_file)
        if left is None:
            return None
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return left / right.value
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("Path", "str") and node.args:
            return _eval_path(node.args[0], env, this_file)
        if isinstance(func, ast.Attribute) and func.attr == "resolve":
            return _eval_path(func.value, env, this_file)
        return None
    if isinstance(node, ast.Attribute):
        base = _eval_path(node.value, env, this_file)
        if base is None:
            return None
        return base.parent if node.attr == "parent" else None
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "parents":
            base = _eval_path(value.value, env, this_file)
            index = node.slice
            if base is not None and isinstance(index, ast.Constant) and isinstance(index.value, int):
                parents = list(base.parents)
                if index.value < len(parents):
                    return parents[index.value]
        return None
    return None


def _path_env(tree: ast.Module, this_file: Path) -> dict[str, Path]:
    env: dict[str, Path] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                value = _eval_path(node.value, env, this_file)
                if value is not None:
                    env[target.id] = value
    return env


def _syspath_dirs(tree: ast.Module, this_file: Path) -> list[Path]:
    """Directories the module puts on sys.path, read out of its own source.

    Covers both idioms in this tree: a direct
    `sys.path.insert(0, str(SCRIPTS_DIR))`, and the
    `for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR): sys.path.insert(0, str(_dir))`
    loop that scripts/primary_review_pass.py and several tests use.
    """
    env = _path_env(tree, this_file)
    dirs: list[Path] = []

    def is_syspath_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr not in ("insert", "append"):
            return False
        target = node.func.value
        return (
            isinstance(target, ast.Attribute)
            and target.attr == "path"
            and isinstance(target.value, ast.Name)
            and target.value.id == "sys"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if not any(is_syspath_call(inner) for inner in ast.walk(node)):
                continue
            if isinstance(node.iter, (ast.Tuple, ast.List)):
                for element in node.iter.elts:
                    value = _eval_path(element, env, this_file)
                    if value is not None:
                        dirs.append(value)
        if is_syspath_call(node):
            for arg in node.args:  # type: ignore[union-attr]
                value = _eval_path(arg, env, this_file)
                if value is not None:
                    dirs.append(value)
    return dirs


def _hard_imports(tree: ast.Module) -> list[str]:
    """Module names imported at import time, unconditionally.

    Excludes anything inside a `try:`, a function or a class — those are
    optional/deferred imports and do not fail the interpreter at startup, so
    they are not what turns a CI job red on a missing dependency.
    """
    names: list[str] = []

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Try)):
                continue
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if not node.level and node.module:
                    names.append(node.module)
            elif isinstance(node, (ast.If, ast.With)):
                walk(node.body)
                walk(list(getattr(node, "orelse", []) or []))

    walk(tree.body)
    return names


def _resolve_local(name: str, search_dirs: list[Path]) -> Path | None:
    parts = name.split(".")
    top = parts[0]
    for directory in search_dirs:
        candidates = (
            directory.joinpath(*parts).with_suffix(".py"),
            directory.joinpath(*parts) / "__init__.py",
            directory.joinpath(top).with_suffix(".py"),
            directory.joinpath(top) / "__init__.py",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def third_party_requirements(
    entry: Path, module_roots: tuple[Path, ...] = MODULE_ROOTS
) -> dict[str, str]:
    """{third-party top-level module: the repo file that hard-imports it}.

    Transitive through repo-local modules — the `1aef16e` break was
    tests/test_output_schema.py -> scripts/primary_review_pass.py ->
    backend/src/model_client.py -> jsonschema, three hops from the edited file.
    """
    needed: dict[str, str] = {}
    seen: set[Path] = set()
    stack = [entry]
    while stack:
        current = stack.pop()
        current = current.resolve()
        if current in seen:
            continue
        seen.add(current)
        try:
            tree = ast.parse(current.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        search_dirs = [current.parent, *_syspath_dirs(tree, current), *module_roots]
        for name in _hard_imports(tree):
            local = _resolve_local(name, search_dirs)
            if local is not None:
                stack.append(local)
                continue
            top = name.split(".")[0]
            if top in STDLIB:
                continue
            needed.setdefault(top, str(current))
    return needed


# ---------------------------------------------------------------------------
# The local gate's own discovery globs
# ---------------------------------------------------------------------------

def local_gate_globs(script: Path = COLLECT_SCRIPT) -> list[str]:
    """The `for t in …` glob list from scripts/collect_test_failures.sh.

    Read, not restated. That loop is the single implementation shared by
    scripts/check.sh and CI GATE A (issue #276); if it ever stops globbing a
    directory, Check 3 must notice on the next run rather than keep comparing
    against a copy of the old patterns.
    """
    text = script.read_text(encoding="utf-8")
    match = re.search(r"^\s*for\s+t\s+in\s+(.+?);\s*do\s*$", text, re.MULTILINE)
    if not match:
        raise RuntimeError(
            f"could not find the discovery loop's glob list in {script}. "
            "Check 3 cannot be evaluated against a guess — fix this parser."
        )
    return shlex.split(match.group(1))


def _covered_by_local_gate(rel_path: str, globs: list[str]) -> bool:
    """Would `for t in <globs>` in the repo root expand to this path?

    The component-count guard makes the match ANCHORED at the repo root.
    Path.match is right-anchored, so without it `other/tests/test_x.py` would
    count as covered by `tests/test_*.py` — and a shell glob would never have
    expanded to it.
    """
    candidate = Path(rel_path)
    return any(
        len(Path(pattern).parts) == len(candidate.parts) and candidate.match(pattern)
        for pattern in globs
    )


# ---------------------------------------------------------------------------
# Clock pin — read the VALUE, not the presence of a token
# ---------------------------------------------------------------------------
#
# `"UTC" in text` would be satisfied by the prose in scripts/check.sh's own
# header, so it proves nothing about what the gate's clock is actually set to:
# `export TZ="${CHECK_TZ:-America/New_York}"` — the very override that header
# advertises — would sail past it. So would an export appended below the line
# that runs the suite, where it can never take effect. What follows resolves
# the export's default and pins where it sits, so Check 4 fails on a CHANGED
# value, not only on a deleted line.

# `export TZ=<rhs>`. The RHS is a single shell word (no spaces), so \S* stops
# at a trailing comment. Leading whitespace is captured deliberately: an
# INDENTED export is inside a function or a conditional, i.e. not certain to be
# reached, and is rejected below.
TZ_EXPORT_RE = re.compile(r"^(\s*)export\s+TZ=(\S*)")

# The RHS shapes that pin a clock readably: `"${CHECK_TZ:-UTC}"` (an
# overridable default) or a bare `UTC`. Anything else is unreadable, and an
# unreadable pin is reported rather than assumed good.
TZ_DEFAULT_RE = re.compile(r"\$\{CHECK_TZ:-([^}]*)\}")
TZ_LITERAL_RE = re.compile(r"[A-Za-z0-9_+/-]+")

COLLECT_SCRIPT_NAME = "collect_test_failures.sh"


def tz_export_default(rhs: str) -> str | None:
    """What TZ resolves to with CHECK_TZ unset, or None if the RHS is unreadable."""
    value = rhs.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1]
    parameterised = TZ_DEFAULT_RE.fullmatch(value)
    if parameterised:
        return parameterised.group(1)
    if TZ_LITERAL_RE.fullmatch(value):
        return value
    return None


def tz_exports(
    text: str,
) -> tuple[list[tuple[int, str, str, str | None]], int | None]:
    """Every non-comment `export TZ=` line, plus the line running the suite.

    Each export is (line number, leading indent, RHS, resolved default). The
    second element is the line that invokes scripts/collect_test_failures.sh —
    the point past which an export is dead code.
    """
    exports: list[tuple[int, str, str, str | None]] = []
    collect_line: int | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        match = TZ_EXPORT_RE.match(line)
        if match:
            indent, rhs = match.group(1), match.group(2)
            exports.append((lineno, indent, rhs, tz_export_default(rhs)))
        if collect_line is None and COLLECT_SCRIPT_NAME in line:
            collect_line = lineno
    return exports, collect_line


def tz_pin_problems(text: str) -> list[str]:
    """Everything wrong with the clock pin in this scripts/check.sh text.

    Pure text-in, problems-out, so check_self() can run it over doctored
    copies without touching the real script.
    """
    exports, collect_line = tz_exports(text)

    problems: list[str] = []
    if collect_line is None:
        problems.append(
            f"no line invokes scripts/{COLLECT_SCRIPT_NAME}, so there is nothing to "
            "order the TZ export against — this parser is reading the wrong script"
        )
    if not exports:
        problems.append(
            "no `export TZ=` line: the gate runs on whatever timezone the developer's "
            "machine is in, which is the divergence this is here to remove"
        )

    for lineno, _indent, rhs, default in exports:
        if default is None:
            problems.append(
                f"line {lineno}: `export TZ={rhs}` — cannot read what this resolves "
                "to with CHECK_TZ unset; write `\"${CHECK_TZ:-UTC}\"` or a bare zone"
            )
        elif default != "UTC":
            problems.append(
                f"line {lineno}: `export TZ={rhs}` defaults to {default!r}, not 'UTC'. "
                "The runner is UTC; a gate pinned to anything else reintroduces the "
                "date-boundary divergence rather than removing it"
            )

    reaching = [
        lineno
        for lineno, indent, _rhs, default in exports
        if default == "UTC"
        and indent == ""
        and (collect_line is None or lineno < collect_line)
    ]
    if exports and not reaching:
        problems.append(
            "no unconditional top-level `export TZ=\"${CHECK_TZ:-UTC}\"` runs before "
            f"line {collect_line} (`scripts/{COLLECT_SCRIPT_NAME}`): an export that is "
            "indented (inside a function or conditional) or that sits below the line "
            "running the suite cannot pin the clock the suite runs under"
        )
    return problems


def effective_utc_offsets(zone: str) -> set[int] | None:
    """The process's UTC offsets under TZ=<zone>, or None where tzset is absent.

    The parsed default is a string until something proves it names a real
    zero-offset zone; this asks libc. Both standard and DST offsets must be 0,
    so a zone that is merely UTC in winter (Europe/London) does not qualify.
    """
    if not hasattr(time, "tzset"):
        return None
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        return {time.timezone, time.altzone}
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


# ---------------------------------------------------------------------------
# Check 0 — self-checks
# ---------------------------------------------------------------------------

_SELF_WORKFLOW = """\
name: Throwaway gate

on:
  push:

jobs:
  needy-gate:
    name: Throwaway
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

{install_step}\
      - name: Run the needy gate
        # A comment mentioning `python3 tests/not_invoked.py` must NOT count.
        run: python3 tests/needy/test_needy.py
"""

_INSTALL_STEP = """\
      - name: Install the gate's dependency
        run: python3 -m pip install --quiet jsonschema==4.26.0

"""


def _mangle_workflow(text: str, mangle: str | None) -> str:
    """Rewrite the synthetic workflow into a shape the scanner must survive.

    Every variant except `unreadable` and `typo-steps` is VALID YAML that
    GitHub Actions runs identically — which is the point: the scanner must not
    treat a cosmetic edit as "this file has nothing in it".
    """
    if mangle is None:
        return text
    if mangle == "comment":
        # Valid YAML. Before #639's fix round this alone dropped the file.
        return text.replace("\njobs:\n", "\njobs:  # the throwaway gates\n", 1)
    if mangle == "reindent":
        # Valid YAML: the jobs: body two columns deeper than this repo writes it.
        lines = text.splitlines()
        at = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
        body = [("  " + line if line.strip() else line) for line in lines[at + 1:]]
        return "\n".join(lines[: at + 1] + body) + "\n"
    if mangle == "unreadable":
        # NOT valid YAML — stands in for the next shape nobody anticipated.
        return text.replace("\njobs:\n", "\n", 1)
    if mangle == "steps-comment":
        # Valid YAML. Same silent-zero as `comment`, one level down.
        return text.replace("    steps:\n", "    steps:  # the throwaway steps\n", 1)
    if mangle == "typo-steps":
        # A `steps:` typo: the job parses, its run: bodies vanish.
        return text.replace("    steps:\n", "    stops:\n", 1)
    raise ValueError(f"unknown mangle: {mangle!r}")


def _write_self_fixture(
    root: Path, install_before: bool | None, mangle: str | None = None
) -> Path:
    """A synthetic repo: one workflow, one test, one local module, one dep.

    `install_before` is True (install step first), False (install step after
    the test step) or None (no install step at all). `mangle` reshapes the
    workflow's YAML — see _mangle_workflow.
    """
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    install_step = _INSTALL_STEP if install_before is True else ""
    text = _SELF_WORKFLOW.format(install_step=install_step)
    if install_before is False:
        text += "\n" + _INSTALL_STEP.rstrip("\n") + "\n"
    (workflows / "throwaway.yml").write_text(
        _mangle_workflow(text, mangle), encoding="utf-8"
    )

    (root / "tests" / "needy").mkdir(parents=True)
    # The test imports a REPO-LOCAL module, which is what imports the
    # third-party one: the transitive chain that broke `1aef16e`.
    (root / "tests" / "needy" / "test_needy.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "SCRIPTS_DIR = Path(__file__).resolve().parents[2] / 'scripts'\n"
        "sys.path.insert(0, str(SCRIPTS_DIR))\n"
        "import needy_helper\n"
        "def go():\n"
        "    import moto\n"  # inside a function: not a hard import
        "    return moto\n"
        "try:\n"
        "    import boto3\n"  # optional: not a hard import
        "except ImportError:\n"
        "    boto3 = None\n",
        encoding="utf-8",
    )
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "needy_helper.py").write_text(
        "import json\nimport jsonschema\n", encoding="utf-8"
    )
    return root


def _jobs_of(steps: list[Step]) -> list[list[Step]]:
    """Group a flat step list back into per-job lists, preserving order."""
    grouped: dict[tuple[str, str], list[Step]] = {}
    for step in steps:
        grouped.setdefault((step.workflow, step.job), []).append(step)
    return list(grouped.values())


def parity_violations(
    root: Path, module_roots: tuple[Path, ...] = MODULE_ROOTS
) -> tuple[list[str], int]:
    """Check 2's rule, over any repo root. Returns (violations, files checked).

    ONE implementation, used by both the real repo and Check 0's synthetic
    fixtures — so the self-checks exercise the code that actually gates, not a
    parallel copy of it that could pass while the real one is broken.
    """
    problems: list[str] = []
    checked = 0
    for job_steps in _jobs_of(iter_steps(root / ".github" / "workflows")):
        provided: set[str] = set()
        for step in job_steps:
            for kind, payload in step_events(step.run):
                if kind == "install":
                    provided |= modules_installed_by(payload, root)
                    continue
                target = root / payload
                if not target.is_file():
                    continue  # Check 1 reports a missing invocation target
                checked += 1
                for module, importer in sorted(
                    third_party_requirements(target, module_roots).items()
                ):
                    if module in provided:
                        continue
                    problems.append(
                        f"{step.where}\n           runs `{payload}`, which hard-imports "
                        f"`{module}` (via {importer}),\n           but nothing earlier "
                        f"in that job installs it. On the runner this is "
                        f"ModuleNotFoundError: No module named '{module}'."
                    )
    return problems, checked


def _self_violations(root: Path) -> list[str]:
    """parity_violations() over a synthetic repo, with ITS module roots."""
    roots = (root, root / "scripts", root / "tests")
    return parity_violations(root, roots)[0]


def check_self() -> list[str]:
    print(
        "Check 0: scanner self-checks (synthetic workflow + synthetic module "
        "tree) and the real tree's coverage floor …"
    )
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = _write_self_fixture(Path(tmp) / "noinstall", install_before=None)
        problems = _self_violations(root)
        failures += _assert(
            any("jsonschema" in p for p in problems),
            "0a a brand-new workflow job invoking a dependency-needing test is DETECTED "
            "(the scanner enumerates workflow commands; it consults no hand-written list)",
            f"Expected a jsonschema violation, got: {problems}",
        )
        failures += _assert(
            not any("moto" in p or "boto3" in p for p in problems),
            "0c function-local and try/except imports are NOT counted as hard requirements",
            f"Unexpected soft-import violations in: {problems}",
        )
        failures += _assert(
            not any("not_invoked" in p for p in problems),
            "0a' a `python3 …` mention inside a YAML/shell comment is not read as an invocation",
            f"Comment text was scanned as a command: {problems}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = _write_self_fixture(Path(tmp) / "before", install_before=True)
        problems = _self_violations(root)
        failures += _assert(
            problems == [],
            "0b installing the dependency BEFORE the step that needs it passes",
            f"Unexpected violations: {problems}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = _write_self_fixture(Path(tmp) / "after", install_before=False)
        problems = _self_violations(root)
        failures += _assert(
            any("jsonschema" in p for p in problems),
            "0b' installing it AFTER that step still FAILS (step order is honoured)",
            "output-schema.yml installs jsonschema mid-job on purpose; a check that "
            "ignored order would have passed the tree that broke CI. "
            f"Got: {problems}",
        )

    # 0e-0g: the scanner must not go blind on a cosmetic YAML edit, and must
    # SAY SO when it does go blind. Each case is proved end to end — the
    # jsonschema violation of 0a has to survive the reshaping, because "the
    # file was still scanned" is only credible if the finding still lands.
    for mangle, label in (
        (
            "comment",
            "0e a `jobs:` key carrying a trailing YAML comment is still scanned "
            "(valid YAML; an exact-match scanner drops the whole file)",
        ),
        (
            "steps-comment",
            "0e' a `steps:` key carrying a trailing YAML comment is still scanned",
        ),
        (
            "reindent",
            "0f a `jobs:` body at a deeper indentation is still scanned "
            "(the job-key column is read, not assumed)",
        ),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_self_fixture(
                Path(tmp) / mangle, install_before=None, mangle=mangle
            )
            problems = _self_violations(root)
            coverage = workflow_scan_coverage(root / ".github" / "workflows")
            failures += _assert(
                any("jsonschema" in p for p in problems) and coverage == [],
                label,
                f"Violations: {problems}\n         Coverage gaps: {coverage}",
            )

    for mangle, label in (
        (
            "unreadable",
            "0g' a workflow whose jobs: mapping cannot be parsed is REPORTED, "
            "not silently scanned as zero steps",
        ),
        (
            "typo-steps",
            "0g'' a job whose `run:` keys the step splitter cannot reach is "
            "REPORTED (raw `run:` count vs parsed step bodies)",
        ),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_self_fixture(
                Path(tmp) / mangle, install_before=None, mangle=mangle
            )
            failures += _assert(
                _self_violations(root) == []
                and workflow_scan_coverage(root / ".github" / "workflows") != [],
                label,
                "This is the negative branch that lets 0h fail at all: a file "
                "the scanner reads nothing out of must produce a coverage "
                "problem. It produced none, so the floor cannot go red and "
                "every check above can pass vacuously.",
            )

    # 0h: the same floor, over the REAL tree. Derived from the tree — every
    # workflow file yields >= 1 job and every `run:` key in its text is
    # accounted for — so adding a workflow needs no edit here, while a parser
    # regression turns this red instead of quietly shrinking the scan.
    real_workflows = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(
        WORKFLOWS_DIR.glob("*.yaml")
    )
    coverage = workflow_scan_coverage(WORKFLOWS_DIR)
    failures += _assert(
        bool(real_workflows) and not coverage,
        f"0h all {len(real_workflows)} real workflow files yield >= 1 job and every "
        "`run:` key in them is parsed into a step body",
        "\n         ".join(coverage) or f"No workflow files found in {WORKFLOWS_DIR}",
    )

    globs = local_gate_globs()
    failures += _assert(
        bool(globs) and _covered_by_local_gate("tests/test_x.py", globs),
        "0d the local gate's globs are parsed from scripts/collect_test_failures.sh",
        f"Parsed globs: {globs}",
    )
    failures += _assert(
        not _covered_by_local_gate("tests/helpers/helper_thing.py", globs),
        "0d' the drift guard's negative branch really rejects an undiscovered path",
        "A path the local gate does not glob must not be reported as covered, or "
        "Check 3 can never fail.",
    )

    # 0i-0i'': Check 4's negative branches, run over DOCTORED copies of the real
    # scripts/check.sh. Each doctored text keeps the header prose — which says
    # "UTC" several times — so a check that greps for the token passes all three
    # while the gate's clock is wrong. Note the mangles are substitutions on the
    # real text: if the export line is ever reshaped past the parser, they stop
    # changing anything, the doctored text passes, and these go red.
    check_text = CHECK_SCRIPT.read_text(encoding="utf-8")
    deleted = re.sub(r"(?m)^export TZ=.*\n", "", check_text)
    for doctored, label in (
        (
            re.sub(
                r"(?m)^export TZ=.*$",
                'export TZ="${CHECK_TZ:-America/New_York}"',
                check_text,
            ),
            "0i a non-UTC default is REJECTED (the resolved value is read, not the "
            "presence of the token `UTC` — which the header's own prose supplies)",
        ),
        (
            deleted + '\nexport TZ="${CHECK_TZ:-UTC}"\n',
            "0i' an `export TZ` sitting BELOW the line that runs the suite is REJECTED "
            "(dead code cannot pin the clock the tests run under)",
        ),
        (
            deleted,
            "0i'' a deleted `export TZ` line is REJECTED",
        ),
    ):
        failures += _assert(
            tz_pin_problems(doctored) != [],
            label,
            "This doctored scripts/check.sh was accepted. Check 4 then passes on "
            "exactly the state it exists to reject, and prints a PASS line that is "
            "false.",
        )

    utc_offsets = effective_utc_offsets("UTC")
    eastern_offsets = effective_utc_offsets("America/New_York")
    failures += _assert(
        utc_offsets == {0} and eastern_offsets not in ({0}, None),
        "0j the runtime timezone probe distinguishes UTC from a non-UTC zone",
        "effective_utc_offsets() is what turns Check 4's parsed default from a "
        f"string into a clock. UTC -> {utc_offsets}, "
        f"America/New_York -> {eastern_offsets} (None = no time.tzset() here, so "
        "the probe cannot run and must not read as a pass).",
    )
    return failures


# ---------------------------------------------------------------------------
# Checks 1-4 — the real repo
# ---------------------------------------------------------------------------

def check_invocations_exist(steps: list[Step]) -> list[str]:
    print("Check 1: every repo .py file a workflow invokes exists …")
    missing = []
    total = 0
    for step in steps:
        for rel in invoked_files(step.run):
            total += 1
            if not (REPO_ROOT / rel).is_file():
                missing.append(f"{step.where} runs `{rel}`, which does not exist")
    return _assert(
        not missing,
        f"all {total} workflow-invoked repo .py paths exist",
        "\n         ".join(missing),
    )


def check_bare_interpreter_parity() -> list[str]:
    print(
        "Check 2: every workflow-invoked file's hard third-party imports are "
        "installed by an EARLIER step of its job …"
    )
    problems, checked = parity_violations(REPO_ROOT)
    return _assert(
        not problems,
        f"all {checked} workflow test invocations run on an interpreter that has what they import",
        "\n         ".join(problems),
    )


def check_local_gate_drift(steps: list[Step]) -> list[str]:
    print(
        "Check 3: every tests/ file CI invokes is also discovered by the local gate …"
    )
    globs = local_gate_globs()
    problems = []
    seen = set()
    for step in steps:
        for rel in invoked_files(step.run):
            if not rel.startswith("tests/") or rel in seen:
                continue
            seen.add(rel)
            if not _covered_by_local_gate(rel, globs):
                problems.append(
                    f"{step.where} runs `{rel}`, which none of the local gate's "
                    f"globs ({' '.join(globs)}) discovers — so scripts/check.sh has "
                    "never run it and a red CI is the first anyone hears of it"
                )
    return _assert(
        not problems,
        f"all {len(seen)} CI-invoked tests/ files are inside the local gate's globs",
        "\n         ".join(problems),
    )


def check_clock_parity() -> list[str]:
    print("Check 4: scripts/check.sh pins the gate clock to the runner's (UTC) …")
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    problems = tz_pin_problems(text)
    failures = _assert(
        not problems,
        "scripts/check.sh exports TZ with a resolved default of UTC, unconditionally, "
        f"before it runs scripts/{COLLECT_SCRIPT_NAME}",
        "GitHub runners are UTC and a machine here is UTC-4; without this, a "
        "date-boundary test can pass locally on 08-31 and fail in CI at "
        "01:02Z on 09-01 (issue #639, fixed once in bed1fa9).\n         "
        + "\n         ".join(problems),
    )

    # The default parsed above is still only a string. Ask libc what clock it
    # actually produces, so a zone that merely LOOKS like UTC cannot pass.
    defaults = sorted(
        {
            default
            for _lineno, _indent, _rhs, default in tz_exports(text)[0]
            if default is not None
        }
    )
    offsets = {zone: effective_utc_offsets(zone) for zone in defaults}
    failures += _assert(
        bool(offsets) and all(value == {0} for value in offsets.values()),
        f"the zone it defaults to ({', '.join(defaults) or 'none parsed'}) really "
        "gives this interpreter a zero-offset clock, standard time and DST alike",
        f"time.timezone/altzone per parsed default: {offsets} "
        "(None = no time.tzset() here, so the pin cannot be verified at runtime).",
    )
    return failures


def main() -> int:
    print("CI-environment parity gate (issue #639)")
    print("=" * 72)
    failures: list[str] = []
    failures += check_self()
    steps = iter_steps(WORKFLOWS_DIR)
    print(f"  (scanned {len(steps)} steps across {len(_jobs_of(steps))} workflow jobs)")
    failures += check_invocations_exist(steps)
    failures += check_bare_interpreter_parity()
    failures += check_local_gate_drift(steps)
    failures += check_clock_parity()
    print("=" * 72)
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("PASS: local gate and CI job environments agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
