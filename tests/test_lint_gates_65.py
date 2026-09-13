#!/usr/bin/env python3
"""
Lint / type-check gates, and the baselines that may only shrink — issue #65
(diagnostic G4 / action B4).

WHY THIS FILE EXISTS
  Before #65 no linter, formatter or type checker ran anywhere in this
  repository. There was no `ruff`, `mypy`, `eslint` or `prettier`
  configuration; `tsc` ran only inside `npm run build:ci`; every style and
  typing rule was enforced by prose review.

  #65 did NOT fix the tree — a whole-repo reformat or retype in the same series
  as #50-#59 is exactly the change nobody can review. It drew a BASELINE and
  put a ratchet on it:

    ruff    every pre-existing violation carries a `# noqa: <rule>` comment
            written by `ruff check --add-noqa`. Nothing else changed — in
            particular the directives the tree ALREADY carried keep their
            authors' rationales, and none of them names RUF100 (check 8).
    mypy    every module that does not type-check under `strict = true` carries
            a `[[tool.mypy.overrides]] ignore_errors = true` entry in
            pyproject.toml, mirrored line-for-line in `mypy-baseline.txt`.
    eslint  every pre-existing violation carries an `eslint-disable-next-line`
            comment written by `frontend/scripts/eslint-baseline.mjs`
            (asserted from the frontend side, in
            frontend/src/__tests__/lint-baseline-65.test.tsx).

  A baseline with no ratchet is just a switched-off linter with extra steps.
  This file is the ratchet.

WHAT IT CHECKS (exit 0 = all pass, 1 = one or more fail)

  1. `pyproject.toml` declares `[tool.ruff]`, `[tool.ruff.lint]` (the exact
     `select`/`ignore` the issue specifies) and `[tool.mypy]`.
  2. `ruff check backend scripts tests infra/lambda` exits 0 on the committed
     tree. Run for real, as a subprocess, not inferred from config.
  3. THE RATCHET BITES. A temp copy of a real module is linted twice: once
     unmodified (must be green — otherwise check 2 is what is being re-run, not
     the ratchet) and once with an unused local appended (must be red). Both
     runs use the committed `pyproject.toml`, so what is proved is that THIS
     config rejects a new violation.
  4. THE MYPY BASELINE ONLY SHRINKS. Two halves:
       a. every `[[tool.mypy.overrides]] ignore_errors = true` module is one
          `mypy-baseline.txt` lists, so nothing can be silenced without being
          written down where a reviewer sees it;
       b. no path in `mypy-baseline.txt` is one mypy now PASSES — established
          by re-running mypy against a copy of the config with every
          `ignore_errors` override STRIPPED OUT and comparing the files that
          still fail. Fix a module and this goes red until its line and its
          override are deleted, which is the only direction the file may move.
     The path -> module-name derivation mypy uses (walk up while the directory
     holds an `__init__.py`: `backend/src/__init__.py` exists, so
     `backend/src/reviews.py` is `src.reviews`, while `scripts/` has none and
     `scripts/opf_load.py` is `opf_load`) is re-derived here rather than
     restated, and the derived names are required to be unique — two files
     deriving one name would let a single override silence both.
  5. `.github/workflows/lint.yml` triggers on BOTH `push` and `pull_request`,
     and its steps actually run `ruff check`, `mypy`, `npm run lint` and
     `npm run typecheck`.
  6. `frontend/package.json` declares a `lint` script.
  7. `ruff format` is NOT INVOKED by `lint.yml` or `scripts/check.sh`. It is
     deliberately deferred (a whole-tree reformat collides with #50-#59;
     follow-up issue #90 turns it on afterwards), and "deferred" has to be
     checkable or it becomes "forgotten".

     The check reads COMMAND text, not file text: both files discuss
     `ruff format --check` in their comments, on purpose, and a grep over raw
     bytes would fail on the documentation of the decision it is meant to
     police. Check 0 proves the command extractor is not vacuous — it is fed a
     synthetic workflow and a synthetic shell script that really do invoke
     `ruff format --check`, in code and in comments respectively, and must
     report the first and ignore the second.

  8. NO `# noqa` DIRECTIVE OUTLIVES ITS FINDING. RUF100 ("unused `noqa`
     directive") is the half of the ratchet that lets the baseline shrink: fix
     the code and ruff tells you the annotation is now dead, so it can be
     deleted. Two shapes are refused, for two DIFFERENT reasons:

       * a directive that names the rule among its own codes
         (`# noqa: BLE001, RUF100`) genuinely suppresses the report about
         itself, so the annotation is frozen in the tree with no gate able to
         say so. `ruff check --add-noqa` writes this shape by itself, and the
         first #65 baseline pass landed 136 of them, so it is a failure that
         has already happened here, not one that might;
       * a blanket `# noqa` does NOT suppress RUF100 — ruff 0.16.7 reports a
         dead one as "Unused blanket `noqa` directive", which was run through
         the committed config to check rather than reasoned about. It is
         refused because it hides EVERY rule on its line, including rules
         nobody has broken yet: the next edit to that line is unreviewable by
         ruff, and the annotation can never be narrowed to the finding it was
         written for.

     Both halves of the contract are checked, because a scanner that reports
     too much costs a contributor a real annotation: planted directives of
     every refused shape ARE reported, and ordinary annotations, rationales
     and prose are NOT. The shapes were each run through ruff to find out which
     is which — a `RUF` prefix and a lowercase `ruf100` turn out NOT to
     suppress anything, so they belong on the innocent side.

     The scan tokenizes rather than grepping: this file has to discuss
     directives in prose and in string literals without reporting itself, the
     same trap check 7 documents for `ruff format`. Finally, RUF100 is proved
     live against the committed config end to end, rather than inferred from
     the `select`/`ignore` tables check 1 pins.

     Directives naming real ruff rules that `select` deliberately leaves off
     are what `[tool.ruff.lint] external` is for, so RUF100 stays honest about
     the rules we do enable without anyone editing 108 lines or widening
     `select`.

  0. Self-checks for the two pieces of machinery checks 4 and 7 stand on (the
     command extractor and the path -> module-name derivation), so neither can
     go quietly blind and turn the checks above into vacuous passes.

Run with: python3 tests/test_lint_gates_65.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MYPY_BASELINE = REPO_ROOT / "mypy-baseline.txt"
LINT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "lint.yml"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check.sh"
FRONTEND_PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"

# The roots the issue names, and the ones scripts/check.sh and lint.yml lint.
RUFF_ROOTS = ("backend", "scripts", "tests", "infra/lambda")

# The rule selection the issue specifies, verbatim.
EXPECTED_RUFF_SELECT = ["E", "F", "W", "I", "B", "UP", "S", "BLE", "C4", "SIM", "RUF"]
EXPECTED_RUFF_IGNORE = ["E501"]


def _assert(ok: bool, label: str, detail: str = "") -> int:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return 0 if ok else 1


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a tool through THIS interpreter, so the pinned venv/CI copy is used."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    # S603: the argv is built here from constants and this file's own
    # literals — `sys.executable` plus tool names — never from input.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Machinery: extracting the COMMANDS a workflow / shell script actually runs
# ---------------------------------------------------------------------------


def _strip_shell_comment(line: str) -> str:
    """Drop a trailing `#` comment, respecting quotes.

    Naive `line.split("#")[0]` would cut `echo "a#b"` in half and, worse, would
    leave the text of a full-line comment looking like a command if the `#` sat
    inside quotes earlier on the line.
    """
    out: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def workflow_commands(text: str) -> list[str]:
    """Every command line inside a workflow's `run:` steps, comments removed.

    Handles both `run: <one-liner>` and the `run: |` block form. A `run:` value
    is shell, so `#` starts a comment there exactly as it does in a script.
    """
    commands: list[str] = []
    lines = text.splitlines()
    i = 0
    run_re = re.compile(r"^(\s*)-?\s*run:\s*(\|[-+]?|>[-+]?)?\s*(.*?)\s*$")
    while i < len(lines):
        m = run_re.match(lines[i])
        if not m:
            i += 1
            continue
        indent, block, inline = m.group(1), m.group(2), m.group(3)
        if block:
            base = len(indent)
            i += 1
            while i < len(lines):
                line = lines[i]
                if line.strip() and (len(line) - len(line.lstrip())) <= base:
                    break
                stripped = _strip_shell_comment(line).strip()
                if stripped:
                    commands.append(stripped)
                i += 1
            continue
        if inline:
            stripped = _strip_shell_comment(inline).strip()
            if stripped:
                commands.append(stripped)
        i += 1
    return commands


def script_commands(text: str) -> list[str]:
    """Every non-comment, non-blank line of a shell script."""
    out = []
    for line in text.splitlines():
        stripped = _strip_shell_comment(line).strip()
        if stripped:
            out.append(stripped)
    return out


def module_name_for(rel_path: str) -> str:
    """Derive the module name mypy gives `rel_path`.

    mypy walks up from the file for as long as the directory holds an
    `__init__.py`. This has to match, or an override silently matches nothing
    while mypy stays red — which is how the first cut of the baseline was
    written.
    """
    p = Path(rel_path)
    parts = [p.stem]
    d = p.parent
    while (REPO_ROOT / d / "__init__.py").exists():
        parts.insert(0, d.name)
        d = d.parent
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Check 0 — the machinery cannot go quietly blind
# ---------------------------------------------------------------------------

DOCTORED_WORKFLOW = """\
name: doctored
on: [push]
jobs:
  j:
    steps:
      - name: honest step
        run: ruff check backend
      - name: formatter, in code
        run: |
          echo "about to format"
          ruff format --check backend
      - name: formatter, only in a comment
        run: |
          # ruff format --check backend  (deferred — see issue #90)
          ruff check scripts
"""

DOCTORED_SCRIPT = """\
#!/usr/bin/env bash
# ruff format --check backend   <- a comment, not a command
echo "hello # not a comment"
ruff check backend
"""


def check_machinery() -> int:
    failures = 0
    cmds = workflow_commands(DOCTORED_WORKFLOW)
    failures += _assert(
        "ruff check backend" in cmds and "ruff check scripts" in cmds,
        "0a. the workflow command extractor finds both one-liner and block `run:` commands",
        f"{cmds}",
    )
    failures += _assert(
        any(c.startswith("ruff format --check") for c in cmds),
        "0b. it REPORTS a `ruff format --check` that is really invoked",
        f"{cmds}",
    )
    failures += _assert(
        sum(1 for c in cmds if "ruff format" in c) == 1,
        "0c. it does NOT report a `ruff format --check` that only appears in a comment",
        f"{cmds}",
    )

    script_cmds = script_commands(DOCTORED_SCRIPT)
    failures += _assert(
        not any("ruff format" in c for c in script_cmds),
        "0d. the script extractor drops full-line comments",
        f"{script_cmds}",
    )
    failures += _assert(
        any("hello # not a comment" in c for c in script_cmds),
        "0e. …without truncating a `#` that sits inside quotes",
        f"{script_cmds}",
    )

    # The path -> module-name derivation, against the two real shapes in this
    # tree: a directory WITH an __init__.py and one without.
    failures += _assert(
        (REPO_ROOT / "backend" / "src" / "__init__.py").exists(),
        "0f. backend/src really is a package (the case the derivation exists for)",
    )
    failures += _assert(
        not (REPO_ROOT / "scripts" / "__init__.py").exists(),
        "0g. scripts/ really is not a package",
    )
    failures += _assert(
        module_name_for("backend/src/reviews.py") == "src.reviews",
        "0h. a file under a package derives a dotted module name",
        module_name_for("backend/src/reviews.py"),
    )
    failures += _assert(
        module_name_for("scripts/opf_load.py") == "opf_load",
        "0i. a file outside one derives a bare module name",
        module_name_for("scripts/opf_load.py"),
    )
    return failures


# ---------------------------------------------------------------------------
# Check 1 — the configuration tables exist and say what the issue specified
# ---------------------------------------------------------------------------


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def check_config_tables() -> int:
    failures = 0
    failures += _assert(PYPROJECT.exists(), "1. pyproject.toml exists")
    if not PYPROJECT.exists():
        return failures + 1
    data = _pyproject()
    tool = data.get("tool", {})
    ruff = tool.get("ruff")
    failures += _assert(isinstance(ruff, dict), "1a. [tool.ruff] is declared")
    if isinstance(ruff, dict):
        failures += _assert(
            ruff.get("line-length") == 100,
            "1b. [tool.ruff] line-length = 100",
            repr(ruff.get("line-length")),
        )
        failures += _assert(
            ruff.get("target-version") == "py313",
            "1c. [tool.ruff] target-version = 'py313' (the repo's one interpreter, #63)",
            repr(ruff.get("target-version")),
        )
        lint = ruff.get("lint", {})
        failures += _assert(isinstance(lint, dict), "1d. [tool.ruff.lint] is declared")
        failures += _assert(
            lint.get("select") == EXPECTED_RUFF_SELECT,
            "1e. [tool.ruff.lint] select is exactly the issue's list",
            repr(lint.get("select")),
        )
        failures += _assert(
            lint.get("ignore") == EXPECTED_RUFF_IGNORE,
            "1f. [tool.ruff.lint] ignore is exactly the issue's list",
            repr(lint.get("ignore")),
        )
        failures += _assert(
            lint.get("per-file-ignores", {}).get("tests/**") == ["S101", "S105", "S106"],
            "1g. [tool.ruff.lint.per-file-ignores] exempts tests/** from S101/S105/S106",
            repr(lint.get("per-file-ignores")),
        )
    mypy_cfg = tool.get("mypy")
    failures += _assert(isinstance(mypy_cfg, dict), "1h. [tool.mypy] is declared")
    if isinstance(mypy_cfg, dict):
        failures += _assert(
            mypy_cfg.get("python_version") == "3.13",
            "1i. [tool.mypy] python_version = '3.13'",
            repr(mypy_cfg.get("python_version")),
        )
        failures += _assert(
            mypy_cfg.get("strict") is True,
            "1j. [tool.mypy] strict = true",
            repr(mypy_cfg.get("strict")),
        )
        failures += _assert(
            mypy_cfg.get("files") == ["backend/src", "scripts"],
            "1k. [tool.mypy] files = ['backend/src', 'scripts']",
            repr(mypy_cfg.get("files")),
        )
    return failures


# ---------------------------------------------------------------------------
# Check 2 — ruff is actually green on the committed tree
# ---------------------------------------------------------------------------


def check_ruff_green() -> int:
    proc = _run(["ruff", "check", *RUFF_ROOTS], cwd=REPO_ROOT)
    return _assert(
        proc.returncode == 0,
        "2. `ruff check " + " ".join(RUFF_ROOTS) + "` exits 0 on the committed tree",
        (proc.stdout + proc.stderr).strip()[-600:],
    )


# ---------------------------------------------------------------------------
# Check 3 — the ratchet bites on a NEW violation
# ---------------------------------------------------------------------------

# F841 (local assigned and never used) is in the `F` selection and is not one
# of the per-file ignores, so it is a rule that must fire anywhere in the tree.
UNUSED_VARIABLE_PROBE = """


def _ratchet_probe_65() -> None:
    unused_probe_variable = "this local is never read"
"""


def check_ratchet_bites() -> int:
    failures = 0
    donor = REPO_ROOT / "scripts" / "opf_load.py"
    failures += _assert(donor.exists(), "3. the probe's donor module exists", str(donor))
    if not donor.exists():
        return failures

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # The config has to be found by the same name ruff resolves it under.
        shutil.copy2(PYPROJECT, work / "pyproject.toml")
        target = work / "probe_module.py"
        original = donor.read_text(encoding="utf-8")
        target.write_text(original, encoding="utf-8")

        clean = _run(["ruff", "check", "probe_module.py"], cwd=work)
        failures += _assert(
            clean.returncode == 0,
            "3a. the UNMODIFIED copy is green (so 3b's red comes from the change, not the copy)",
            (clean.stdout + clean.stderr).strip()[-400:],
        )

        target.write_text(original + UNUSED_VARIABLE_PROBE, encoding="utf-8")
        dirty = _run(["ruff", "check", "probe_module.py"], cwd=work)
        failures += _assert(
            dirty.returncode != 0,
            "3b. adding one unused local makes `ruff check` exit non-zero",
            f"exit {dirty.returncode}",
        )
        failures += _assert(
            "F841" in (dirty.stdout + dirty.stderr),
            "3c. …and names F841, so it is the unused local that was caught",
            (dirty.stdout + dirty.stderr).strip()[-400:],
        )
    return failures


# ---------------------------------------------------------------------------
# Check 4 — the mypy baseline only shrinks
# ---------------------------------------------------------------------------


def _baseline_paths() -> list[str]:
    out = []
    for line in MYPY_BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _override_modules() -> list[str]:
    data = _pyproject()
    out = []
    for entry in data.get("tool", {}).get("mypy", {}).get("overrides", []):
        if entry.get("ignore_errors") is True:
            mod = entry.get("module")
            if isinstance(mod, str):
                out.append(mod)
            elif isinstance(mod, list):
                out.extend(mod)
    return out


_MYPY_FILE_RE = re.compile(r"^(\S+\.py):\d+:", re.MULTILINE)


def _mypy_failing_files(config_text: str, cache_dir: Path) -> tuple[set[str], str]:
    """Run mypy from the repo root with `config_text` as its configuration."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(config_text)
        cfg = fh.name
    try:
        proc = _run(
            [
                "mypy",
                "--config-file",
                cfg,
                "--cache-dir",
                str(cache_dir),
                "--no-error-summary",
                "--no-pretty",
            ],
            cwd=REPO_ROOT,
        )
    finally:
        os.unlink(cfg)
    files = {m.group(1) for m in _MYPY_FILE_RE.finditer(proc.stdout)}
    return files, (proc.stdout + proc.stderr)


def _config_without_ignore_errors() -> str:
    """The committed pyproject with every `ignore_errors` override removed."""
    text = PYPROJECT.read_text(encoding="utf-8")
    out: list[str] = []
    block: list[str] = []
    in_override = False
    for line in text.splitlines():
        if line.strip() == "[[tool.mypy.overrides]]":
            if in_override and "ignore_errors = true" not in "\n".join(block):
                out.extend(block)
            in_override, block = True, [line]
            continue
        if in_override:
            if line.startswith("[") and line.strip() != "[[tool.mypy.overrides]]":
                if "ignore_errors = true" not in "\n".join(block):
                    out.extend(block)
                in_override, block = False, []
                out.append(line)
                continue
            block.append(line)
            continue
        out.append(line)
    if in_override and "ignore_errors = true" not in "\n".join(block):
        out.extend(block)
    return "\n".join(out) + "\n"


def check_mypy_baseline_only_shrinks() -> int:
    failures = 0
    failures += _assert(MYPY_BASELINE.exists(), "4. mypy-baseline.txt exists")
    if not MYPY_BASELINE.exists():
        return failures

    baseline = _baseline_paths()
    failures += _assert(len(baseline) > 0, "4a. the baseline is non-empty", f"{len(baseline)} paths")
    failures += _assert(
        baseline == sorted(baseline),
        "4b. the baseline is sorted (so a diff to it is readable)",
    )
    missing_files = [p for p in baseline if not (REPO_ROOT / p).exists()]
    failures += _assert(
        not missing_files,
        "4c. every baselined path still exists",
        f"{missing_files[:5]}",
    )

    derived = [module_name_for(p) for p in baseline]
    failures += _assert(
        len(set(derived)) == len(derived),
        "4d. the derived module names are unique (one override cannot silence two files)",
    )

    overrides = _override_modules()
    failures += _assert(
        len(overrides) > 0,
        "4e. pyproject.toml carries the ignore_errors overrides",
        f"{len(overrides)}",
    )
    extra = sorted(set(overrides) - set(derived))
    failures += _assert(
        not extra,
        "4f. every ignore_errors override is a module mypy-baseline.txt lists",
        f"un-baselined: {extra[:5]}",
    )
    unsilenced = sorted(set(derived) - set(overrides))
    failures += _assert(
        not unsilenced,
        "4g. …and every baselined path has an override (the two lists are one list)",
        f"un-overridden: {unsilenced[:5]}",
    )

    # The shrink half: with the overrides stripped, every baselined path must
    # STILL fail. One that passes is debt that has been paid and not written
    # off.
    with tempfile.TemporaryDirectory() as tmp:
        failing, output = _mypy_failing_files(
            _config_without_ignore_errors(), Path(tmp) / "mypy-cache"
        )
    failures += _assert(
        len(failing) > 0,
        "4h. the stripped-config mypy run really does report errors (not a vacuous pass)",
        output.strip()[-400:],
    )
    now_passing = sorted(set(baseline) - failing)
    failures += _assert(
        not now_passing,
        "4i. mypy-baseline.txt lists no module mypy now PASSES — delete its line and "
        "its override",
        f"now clean: {now_passing[:8]}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check 5 — lint.yml triggers and steps
# ---------------------------------------------------------------------------

REQUIRED_LINT_COMMANDS = (
    "ruff check",
    "mypy",
    "npm run lint",
    "npm run typecheck",
)


def check_lint_workflow() -> int:
    failures = 0
    failures += _assert(LINT_WORKFLOW.exists(), "5. .github/workflows/lint.yml exists")
    if not LINT_WORKFLOW.exists():
        return failures
    text = LINT_WORKFLOW.read_text(encoding="utf-8")

    # The `on:` block: everything between `on:` and the next top-level key.
    on_block: list[str] = []
    collecting = False
    for line in text.splitlines():
        if re.match(r"^on:\s*$", line) or re.match(r"^on:\s*\[", line):
            collecting = True
            on_block.append(line)
            continue
        if collecting:
            if line and not line[0].isspace():
                break
            on_block.append(line)
    on_text = "\n".join(on_block)
    failures += _assert(bool(on_block), "5a. lint.yml has an `on:` block")
    for trigger in ("push", "pull_request"):
        failures += _assert(
            re.search(rf"^\s+{trigger}:|\b{trigger}\b", on_text) is not None,
            f"5b. lint.yml triggers on `{trigger}`",
            on_text.strip()[:200],
        )

    commands = workflow_commands(text)
    for required in REQUIRED_LINT_COMMANDS:
        failures += _assert(
            any(required in c for c in commands),
            f"5c. lint.yml runs `{required}`",
            f"{commands}",
        )
    return failures


# ---------------------------------------------------------------------------
# Check 6 — `npm run lint` exists to be run
# ---------------------------------------------------------------------------


def check_frontend_lint_script() -> int:
    import json

    failures = 0
    failures += _assert(FRONTEND_PACKAGE_JSON.exists(), "6. frontend/package.json exists")
    if not FRONTEND_PACKAGE_JSON.exists():
        return failures
    pkg = json.loads(FRONTEND_PACKAGE_JSON.read_text(encoding="utf-8"))
    scripts = pkg.get("scripts", {})
    failures += _assert(
        "lint" in scripts, "6a. frontend/package.json declares a `lint` script", repr(scripts)
    )
    failures += _assert(
        "eslint" in scripts.get("lint", ""),
        "6b. …and it runs eslint",
        repr(scripts.get("lint")),
    )
    failures += _assert(
        "typecheck" in scripts,
        "6c. frontend/package.json still declares `typecheck` (lint.yml runs it)",
    )
    for dep in ("eslint", "typescript-eslint", "eslint-plugin-react-hooks", "eslint-plugin-jsx-a11y", "prettier"):
        failures += _assert(
            dep in pkg.get("devDependencies", {}),
            f"6d. {dep} is a pinned devDependency",
        )
    return failures


# ---------------------------------------------------------------------------
# Check 7 — `ruff format` stays deferred, visibly
# ---------------------------------------------------------------------------


def check_formatter_deferred() -> int:
    failures = 0
    wf_cmds = workflow_commands(LINT_WORKFLOW.read_text(encoding="utf-8"))
    offenders = [c for c in wf_cmds if "ruff format" in c]
    failures += _assert(
        not offenders,
        "7a. lint.yml does not INVOKE `ruff format` (deferred — follow-up issue #90)",
        f"{offenders}",
    )
    sh_cmds = script_commands(CHECK_SCRIPT.read_text(encoding="utf-8"))
    sh_offenders = [c for c in sh_cmds if "ruff format" in c]
    failures += _assert(
        not sh_offenders,
        "7b. scripts/check.sh does not INVOKE `ruff format`",
        f"{sh_offenders}",
    )
    # …and the decision is written down where the next person will read it.
    failures += _assert(
        "ruff format" in LINT_WORKFLOW.read_text(encoding="utf-8"),
        "7c. lint.yml still DOCUMENTS the deferral (so it is a decision, not an omission)",
    )
    # scripts/check.sh must genuinely run the pair, or 7b passes vacuously.
    failures += _assert(
        any("ruff check" in c for c in sh_cmds),
        "7d. scripts/check.sh really does invoke `ruff check` (7b is not vacuous)",
        f"{[c for c in sh_cmds if 'ruff' in c]}",
    )
    failures += _assert(
        any(re.match(r"^(if\s+!\s+)?mypy\b", c) for c in sh_cmds),
        "7e. scripts/check.sh really does invoke `mypy`",
        f"{[c for c in sh_cmds if 'mypy' in c]}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check 8 — no noqa directive outlives its finding
# ---------------------------------------------------------------------------
#
# RUF100 ("unused `noqa` directive") is the half of the ratchet that makes the
# baseline SHRINK: fix the code under an annotation and ruff reports the
# annotation as dead, so it can be deleted. A directive that lists RUF100 among
# its OWN codes suppresses exactly that report, and the annotation is then
# frozen in the tree forever with no gate able to say so — the inverse of this
# file's whole premise.
#
# A blanket directive is refused alongside it, for a different reason that was
# checked against ruff rather than assumed: ruff DOES still report a dead
# blanket one ("Unused blanket `noqa` directive"), so it is not self-hiding.
# What it hides is every OTHER rule on its line, forever, including the ones
# the next edit to that line will break — so it can never be narrowed to the
# finding it was written for, and no gate can tell you it has grown too broad.
#
# This is not hypothetical. `ruff check --add-noqa` appends RUF100 to the
# directives it writes whenever the tree already carries directives ruff would
# call stale, and the first #65 baseline pass landed 136 of them. Re-running
# `--add-noqa` will do it again; this check is what catches that.
#
# The "non-enabled" half of the same problem — hand-written directives naming
# real ruff rules that `select` deliberately leaves off — is handled once, in
# `[tool.ruff.lint] external`, not by masking RUF100 line by line.

# A noqa is only a directive when it is a real COMMENT token. This file
# necessarily DISCUSSES directives in prose and in string literals, and a raw
# regex over file bytes would report its own documentation — the same trap
# check 7 documents for `ruff format`. So the scan tokenizes.
#
# Ruff itself honours the marker ANYWHERE inside a comment, not only at its
# start, so this regex searches rather than anchors — which is also why no
# comment in this file may spell out an example directive. The examples live
# in the module docstring and in the probe strings below, where ruff does not
# look. (Spell one out here and ruff reads it as a real directive: it did.)
NOQA_COMMENT_RE = re.compile(r"#\s*noqa\b\s*(?::\s*(?P<codes>.*))?$", re.I)
# Ruff reads the code list left to right and stops at the first thing that is
# not a code, which is what lets a directive carry a trailing rationale (see
# INNOCENT_PROBES). A parser that split on commas and demanded every piece be a
# bare code would stop at the FIRST code on such a line and never see the ones
# after it. Codes must be UPPERCASE to count — the shapes below were each run
# through ruff to find out which is which.
NEXT_CODE_RE = re.compile(r"[,\s]*([A-Z]+[0-9]+)")

# The rule whose report is the only thing that can ever retire an annotation.
UNUSED_DIRECTIVE_RULE = "RUF100"

# Shapes this check refuses, planted to prove the scanner is not vacuous. The
# first four silence the rule named above; the blanket one does not (ruff still
# calls a dead blanket directive unused) and is refused for hiding every rule
# on its line instead. The code is spliced in at runtime: written out
# literally, the probe on the RIGHT of these assignments is a string token
# rather than a comment, but the one thing this check must never do is depend
# on that distinction holding.
_R = "RUF" + "100"
SELF_SUPPRESSION_PROBES = (
    f"# noqa: F401, {_R}",  # names the rule outright — what `--add-noqa` writes
    f"# noqa: BLE001, {_R} - degrade, never wedge a review",  # …behind a rationale
    f"# noqa:{_R}",  # no space after the colon
    f"# NOQA: {_R}",  # the keyword is case-insensitive to ruff; the code is not
    "# noqa",  # blanket: hides every rule on its line, present and future
)
# The other half of the contract. Each of these was run through ruff and does
# NOT suppress the report, so reporting one would be a false alarm that costs a
# contributor a real annotation.
INNOCENT_PROBES = (
    "# noqa: F401",  # a normal baseline annotation
    "# noqa: BLE001 - degrade, never wedge a review",  # …with a rationale
    "# noqa: ARG001, B008 -- auth gate only",  # …with two codes and a rationale
    "# a comment that merely mentions noqa: RUF100 in prose",
    "# noqa: RUF012",  # a sibling RUF rule, not this one
    "# noqa: F401, RUF",  # a prefix: ruff wants the whole code, so this is inert
    "# noqa: F401, ruf100",  # lowercase: ruff does not honour it
)


def _noqa_comments() -> list[tuple[str, int, str]]:
    """Every `# noqa…` COMMENT token under the linted roots, as (path, line, text)."""
    found: list[tuple[str, int, str]] = []
    for root in RUFF_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            try:
                with path.open("rb") as fh:
                    tokens = list(tokenize.tokenize(fh.readline))
            except (OSError, SyntaxError, tokenize.TokenError, UnicodeDecodeError):
                continue
            rel = str(path.relative_to(REPO_ROOT))
            for tok in tokens:
                if tok.type == tokenize.COMMENT and NOQA_COMMENT_RE.search(tok.string):
                    found.append((rel, tok.start[0], tok.string))
    return found


def _silences_unused_directive_rule(comment: str) -> bool:
    """True when this directive must not stand: it hides its own retirement.

    Either literally — it names the rule that reports dead directives among its
    own codes — or in effect: a blanket directive hides every rule on its line,
    so nothing can ever tell you it covers more than the finding it was written
    for. (Ruff does still report a dead BLANKET directive as unused; that shape
    is refused for its breadth, not for self-suppression.)
    """
    m = NOQA_COMMENT_RE.search(comment)
    if not m:
        return False
    codes = m.group("codes")
    if codes is None:
        return True  # blanket noqa — hides every rule on its line, not just this one
    pos = 0
    while (hit := NEXT_CODE_RE.match(codes, pos)) is not None:
        pos = hit.end()
        if hit.group(1) == UNUSED_DIRECTIVE_RULE:
            return True
    return False


def check_no_ruf100_self_suppression() -> int:
    failures = 0
    comments = _noqa_comments()

    # Not vacuous: there IS a baseline to police, and the scanner finds it.
    failures += _assert(
        len(comments) > 100,
        "8. the tree carries a `# noqa` baseline for this check to police",
        f"{len(comments)} directives across {len(RUFF_ROOTS)} roots",
    )
    # Not vacuous, and not indiscriminate: every silencing shape is caught and
    # every ordinary annotation is left alone.
    caught = [p for p in SELF_SUPPRESSION_PROBES if _silences_unused_directive_rule(p)]
    failures += _assert(
        len(caught) == len(SELF_SUPPRESSION_PROBES),
        "8a. planted self-suppressing directives are all reported (8c is not vacuous)",
        f"caught {caught} of {list(SELF_SUPPRESSION_PROBES)}",
    )
    false_alarms = [p for p in INNOCENT_PROBES if _silences_unused_directive_rule(p)]
    failures += _assert(
        not false_alarms,
        "8b. ordinary annotations and prose are NOT reported (8c is not indiscriminate)",
        f"{false_alarms}",
    )

    offenders = [
        f"{rel}:{n}: {text[:100]}"
        for rel, n, text in comments
        if _silences_unused_directive_rule(text)
    ]
    failures += _assert(
        not offenders,
        f"8c. no `# noqa` in the tree hides {UNUSED_DIRECTIVE_RULE} or the rest of its line",
        (f"{len(offenders)} offender(s): " + "; ".join(offenders[:5])) if offenders else "",
    )

    # And the rule really is live under the WHOLE committed config — `select`
    # and `ignore` are pinned by check 1, but `external` is not, so prove the
    # end state rather than reasoning about the tables. Without this, 8c
    # polices a report that was never going to be made.
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copy2(PYPROJECT, work / "pyproject.toml")
        # `x = 1` violates nothing, so this directive is dead by construction.
        (work / "probe_module.py").write_text("x = 1  # noqa: F821\n", encoding="utf-8")
        proc = _run(["ruff", "check", "probe_module.py"], cwd=work)
        failures += _assert(
            proc.returncode != 0 and UNUSED_DIRECTIVE_RULE in (proc.stdout + proc.stderr),
            f"8d. the committed config still REPORTS a dead `# noqa` ({UNUSED_DIRECTIVE_RULE} is live)",
            (proc.stdout + proc.stderr).strip()[-400:],
        )
    return failures


def main() -> int:
    total = 0
    for label, fn in (
        ("Check 0 — the machinery cannot go quietly blind", check_machinery),
        ("Check 1 — ruff/mypy configuration tables", check_config_tables),
        ("Check 2 — ruff is green on the committed tree", check_ruff_green),
        ("Check 3 — the ratchet bites on a new violation", check_ratchet_bites),
        ("Check 4 — the mypy baseline only shrinks", check_mypy_baseline_only_shrinks),
        ("Check 5 — lint.yml triggers and steps", check_lint_workflow),
        ("Check 6 — frontend lint script and dependencies", check_frontend_lint_script),
        ("Check 7 — `ruff format` stays deferred, visibly", check_formatter_deferred),
        ("Check 8 — no `# noqa` outlives its finding", check_no_ruf100_self_suppression),
    ):
        print(label)
        total += fn()
    print("")
    if total:
        print(f"LINT-GATES: {total} check(s) FAILED")
        return 1
    print("LINT-GATES: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
