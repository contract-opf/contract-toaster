#!/usr/bin/env python3
"""
The AWS App Runner image (`backend/Dockerfile`) carries exactly the scripts/
modules the backend imports at runtime.

The defect: `backend/src/*.py` put the repo's top-level `scripts/` on
`sys.path` (`Path(__file__).resolve().parents[2] / "scripts"`) and import the
review pipeline from it -- `opf_load`, `playbook_registry`, `cover_note_pass`
and the modules those import in turn, `hard_ledger` and `regex_safety`
among them. `backend/Dockerfile` was built from the `backend/` context and
copied only `src/`, so none of them existed in the image and
`src.main:app` failed at import (`review_routes` imports `cover_note_pass`
at module level). The DTS image (`deploy/dts/backend.Dockerfile`) copied the
whole `scripts/` tree and never had the gap.

The fix builds the AWS image from the repo-root context, mirrors the repo
tree under /app as the DTS image does, and COPYs an explicit list of
scripts/ modules. This file holds that list to the import graph:

  1. The closure is computed from source, not declared: every top-level
     `import X` / `from X import ...` anywhere in `backend/src/**/*.py`
     (function-local imports included -- `main.py` imports `opf_load` and
     `playbook_registry` lazily) that names a `scripts/X.py`, then every
     scripts/ module those import, transitively.
  2. Every module in that closure is COPYed into the image's `scripts/`.
  3. Nothing outside it is: no eval harness, fixture generator or repo
     tooling script rides along into a production image.
  4. The layout keeps the `parents[2]` arithmetic true: `backend/src` lands
     at `backend/src` under the WORKDIR, `scripts/` beside it, and uvicorn
     starts from `/app/backend`.
  5. `playbooks/` is copied too: `playbook_registry` reads
     `playbooks/registry.json` the moment it is imported.
  6. Both producers of the AWS image (`ci-pipeline.yml` STEP E and the CDK
     CodeBuild buildspec) build it from the repo root with
     `-f backend/Dockerfile`; a `backend/` context cannot reach `scripts/`.

Self-checks (0) run the parser against synthetic Dockerfiles so a parsing
regression cannot turn the real checks vacuous.

It is a STRUCTURAL check. That the image really imports `src.main` is shown
by building it (`docker build -f backend/Dockerfile .` then
`docker run --rm --entrypoint python <tag> -c "import src.main"`), which
needs Docker and is not part of the gate.

Run: python3 tests/test_aws_image_runtime_scripts.py
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

AWS_BACKEND = REPO_ROOT / "backend" / "Dockerfile"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
SCRIPTS = REPO_ROOT / "scripts"
CI_PIPELINE = REPO_ROOT / ".github" / "workflows" / "ci-pipeline.yml"
CICD_STACK = REPO_ROOT / "infra" / "lib" / "nested" / "cicd-stack.ts"


def _imported_top_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def runtime_scripts_closure() -> set[str]:
    """scripts/ module names the backend imports, transitively."""
    available = {p.stem for p in SCRIPTS.glob("*.py")}
    pending: set[str] = set()
    for path in BACKEND_SRC.rglob("*.py"):
        pending |= _imported_top_level_names(path) & available
    closure: set[str] = set()
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        pending |= _imported_top_level_names(SCRIPTS / f"{name}.py") & available
    return closure


def _instructions(text: str) -> list[tuple[str, list[str]]]:
    """(KEYWORD, args) per Dockerfile instruction, continuations joined."""
    joined = re.sub(r"\\\n", " ", text)
    out: list[tuple[str, list[str]]] = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        out.append((keyword.upper(), rest.split()))
    return out


def copied_scripts(text: str) -> set[str]:
    """Module names COPYed into scripts/ by individual `scripts/X.py` sources.

    A whole-directory `COPY scripts ...` is reported as "*" so the test can
    reject it: it would carry every tool and eval script."""
    modules: set[str] = set()
    for keyword, args in _instructions(text):
        if keyword != "COPY":
            continue
        sources = [a for a in args[:-1] if not a.startswith("--")]
        for src in sources:
            if src.rstrip("/") == "scripts":
                modules.add("*")
            m = re.fullmatch(r"scripts/([A-Za-z0-9_]+)\.py", src)
            if m:
                if args[-1].rstrip("/") != "scripts":
                    raise AssertionError(f"{src} is copied to {args[-1]}, not scripts/")
                modules.add(m.group(1))
    return modules


class ParserSelfCheck(unittest.TestCase):
    def test_multi_line_copy_is_read(self) -> None:
        text = "FROM x\nCOPY scripts/a.py \\\n     scripts/b_c.py \\\n     scripts/\n"
        self.assertEqual(copied_scripts(text), {"a", "b_c"})

    def test_directory_copy_is_flagged(self) -> None:
        self.assertEqual(copied_scripts("FROM x\nCOPY scripts scripts\n"), {"*"})

    def test_commented_copy_is_ignored(self) -> None:
        self.assertEqual(copied_scripts("FROM x\n# COPY scripts/a.py scripts/\n"), set())

    def test_wrong_destination_is_rejected(self) -> None:
        with self.assertRaises(AssertionError):
            copied_scripts("FROM x\nCOPY scripts/a.py src/\n")

    def test_closure_is_not_vacuous(self) -> None:
        closure = runtime_scripts_closure()
        # The modules this check was written for, directly and transitively
        # imported: if the walker loses them, it has gone blind.
        for name in ("opf_load", "hard_ledger", "regex_safety", "cover_note_pass"):
            self.assertIn(name, closure)


class AwsImageCarriesRuntimeScripts(unittest.TestCase):
    def setUp(self) -> None:
        self.text = AWS_BACKEND.read_text(encoding="utf-8")
        self.copied = copied_scripts(self.text)
        self.closure = runtime_scripts_closure()

    def test_no_whole_directory_copy(self) -> None:
        self.assertNotIn(
            "*", self.copied,
            "backend/Dockerfile copies the whole scripts/ tree; list the "
            "runtime modules instead so no tool or eval script ships",
        )

    def test_every_runtime_import_is_copied(self) -> None:
        missing = sorted(self.closure - self.copied)
        self.assertEqual(
            missing, [],
            "backend/src imports these scripts/ modules (directly or "
            "transitively) but backend/Dockerfile does not COPY them, so the "
            f"App Runner image cannot import them: {missing}",
        )

    def test_nothing_outside_the_closure_is_copied(self) -> None:
        extra = sorted(self.copied - self.closure - {"*"})
        self.assertEqual(
            extra, [],
            "backend/Dockerfile COPYs scripts/ modules the backend never "
            f"imports; drop them from the image: {extra}",
        )

    def test_layout_keeps_parents_2_pointing_at_the_repo_root(self) -> None:
        copies = [args for kw, args in _instructions(self.text) if kw == "COPY"]
        self.assertIn(
            ["backend/src", "backend/src"], copies,
            "backend/src must land at <WORKDIR>/backend/src so parents[2] of "
            "each module is the directory holding scripts/",
        )
        workdirs = [args[0] for kw, args in _instructions(self.text) if kw == "WORKDIR"]
        self.assertEqual(workdirs[0], "/app", "the repo tree is mirrored under /app")
        self.assertEqual(
            workdirs[-1], "/app/backend",
            "uvicorn must start from /app/backend so `src.main:app` resolves",
        )

    def test_import_time_playbook_data_is_copied(self) -> None:
        # playbook_registry reads playbooks/registry.json (and opf_load the
        # output schemas) at import; without them src.main cannot import.
        copies = [args for kw, args in _instructions(self.text) if kw == "COPY"]
        self.assertIn(["playbooks", "playbooks"], copies)


class AwsImageIsBuiltFromTheRepoRoot(unittest.TestCase):
    def test_ci_pipeline_step_e(self) -> None:
        text = CI_PIPELINE.read_text(encoding="utf-8")
        step = re.search(r"docker build \\\n(?:[^\n]*\\\n)*[^\n]*\n", text)
        self.assertIsNotNone(step, "ci-pipeline.yml: STEP E docker build not found")
        assert step is not None
        args = step.group(0).replace("\\\n", " ").split()
        self.assertIn("backend/Dockerfile", args)
        self.assertEqual(args[args.index("backend/Dockerfile") - 1], "-f")
        self.assertEqual(args[-1], ".", "ci-pipeline.yml: the build context must be the repo root")

    def test_codebuild_buildspec(self) -> None:
        text = CICD_STACK.read_text(encoding="utf-8")
        builds = re.findall(r"'(docker build [^'\n]*)'", text)
        self.assertEqual(len(builds), 1, "cicd-stack.ts: expected one docker build command")
        args = builds[0].split()
        self.assertIn("backend/Dockerfile", args)
        self.assertEqual(args[args.index("backend/Dockerfile") - 1], "-f")
        self.assertEqual(args[-1], ".", "cicd-stack.ts: the build context must be the repo root")


if __name__ == "__main__":
    unittest.main(verbosity=2)
