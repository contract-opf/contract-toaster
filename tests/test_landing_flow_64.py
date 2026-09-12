#!/usr/bin/env python3
"""
Landing flow: branch-per-change, a pre-push guard, PR-triggered CI, and a
self-closing main-is-red issue (issue #64; diagnostic G8 / action B8).

PROBLEM THIS LOCKS DOWN
-----------------------
Work landed straight on `main` — zero pull-request merges in the 40 commits
before 2026-09-05, and three red-`main` runs on that day alone, each fixed by
a follow-up commit. Branch protection and rulesets are unavailable on this
repository's GitHub plan (the API answers 403), so the server-side control
cannot be turned on. Owner decision Q6 chose a local interim control:
`scripts/land.sh` runs the gates and refuses to push red, `.githooks/pre-push`
refuses a direct push to `main`, CI runs on pull requests, and the
`ci-main-red` issue closes itself once `main` is green again (repository issue
#1 stayed open from 2026-07-28 while `main` had been green since 4bbfea6).

WHAT IS REAL HERE AND WHAT IS FAKED
-----------------------------------
Checks 1-3 run the REAL `scripts/land.sh` as a subprocess, inside a throwaway
`git init` repository in a temporary directory. Nothing about that repository
is test-only shaped: it is an ordinary git repository with a `main` branch and
a feature branch, which is exactly what a developer runs land.sh in.

Two seams are faked, both of them seams that exist in the non-test code:

  * The three gate commands. `scripts/land.sh` reads them from
    LAND_GATE_FRONTEND_CMD / LAND_GATE_CHECK_CMD / LAND_GATE_BRAND_CMD and
    falls back to the real gates. The stubs here stand in for `npm test`,
    `scripts/check.sh` and `tests/lint-brand-free.py`, whose real exit codes
    are what land.sh consumes — including check.sh's documented 2
    (FLAKY-UNRESOLVED) and 3 (lock busy), both seeded below so neither branch
    can rot green.
  * `git push` and `gh`, through a PATH shim that appends every invocation to
    a log. The `git` shim delegates every OTHER subcommand to the real git, so
    land.sh's branch detection and `git diff` run for real; only the network
    calls are intercepted. Assertions about "did not push" read that log.
    Each shim's exit code is seeded (SHIM_PUSH_RC / SHIM_GH_RC), so the
    refused-push and failed-`gh` states land.sh handles after the gates are
    exercised and not merely written down.

Check 4 feeds `.githooks/pre-push` the exact stdin line git writes
(`<local ref> <local sha> <remote ref> <remote sha>`). Checks 5-8 are text
assertions over the workflow, the PR template and the repository, in the style
of tests/test_ci_pipeline.py.

Checks:
  1. `land.sh` on `main` exits 1 and pushes nothing.
  2. `land.sh` on a feature branch with a red gate exits 1, names the failing
     gate, and pushes nothing — seeded for EACH of the three gates, and for
     check.sh's exit 2 and exit 3.
  3. `land.sh` with every gate green pushes the branch and then runs
     `gh pr create --fill`; SKIP_INFRA=1 for a branch that leaves `infra/`
     alone and is unset for one that touches it (both seeded, the second of
     them also on a branch whose `git diff --name-only` output is past 64 KB,
     which is where a pipefail'd `| grep -q` reader would silently invert the
     answer and skip the slow infra tests that shell out to the CDK). A
     refused `git push` and a failing `gh pr create` are seeded too: each
     exits 1, and the `gh` one says the branch is already pushed.
  4. `.githooks/pre-push` refuses `refs/heads/main`, allows it under
     LAND_TO_MAIN=1 / CI=1 / CONTRACT_TOASTER_LOOP=1, allows a non-main ref,
     and runs the counterparty-name lint on the paths it does not skip.
  5. `ci-pipeline.yml` closes open `ci-main-red` issues on a green push to
     `main`, with a comment naming GITHUB_SHA, and cannot run on a red one.
  6. Every gate workflow runs on `pull_request`.
  7. `.github/PULL_REQUEST_TEMPLATE.md` names the three gates, "docs updated
     in the same PR", and "plan/diagnostic row marked landed".
  8. `scripts/setup-hooks.sh` stays opt-in: no gate, no npm lifecycle script
     and no workflow runs it.

Run with: python3 tests/test_landing_flow_64.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAND_SH = REPO_ROOT / "scripts" / "land.sh"
PRE_PUSH = REPO_ROOT / ".githooks" / "pre-push"
SETUP_HOOKS = REPO_ROOT / "scripts" / "setup-hooks.sh"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CI_PIPELINE = WORKFLOWS_DIR / "ci-pipeline.yml"
PR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"

# Workflows that are NOT gates and therefore correctly do not run on a pull
# request: the image publisher is chained off a completed CI run, and the
# dependency audit is a weekly cron. Check 6 also asserts this set is exactly
# the set of workflows with no `push:` trigger, so a new gate workflow cannot
# quietly join it.
NON_GATE_WORKFLOWS = frozenset({"dts-image-publish.yml", "dependency-audit.yml"})

# Environment variables that would make .githooks/pre-push skip its guard.
# The gate itself runs in CI, where CI=true is set, so every subprocess here
# starts from an environment with these REMOVED.
HOOK_ESCAPE_VARS = ("LAND_TO_MAIN", "CI", "CONTRACT_TOASTER_LOOP", "SKIP_INFRA")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert(ok: bool, label: str, detail: str = "") -> int:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return 0 if ok else 1


def _clean_env(**overrides) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in HOOK_ESCAPE_VARS}
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Throwaway git repository + PATH shims
# ---------------------------------------------------------------------------

GIT_SHIM = """#!/usr/bin/env bash
# Intercepts `git push` (logs it, never touches a network) and delegates every
# other subcommand to the real git, so land.sh's branch detection is real.
if [ "${1:-}" = "push" ]; then
  printf 'git %s\\n' "$*" >> "$SHIM_LOG"
  exit "${SHIM_PUSH_RC:-0}"
fi
exec "$REAL_GIT" "$@"
"""

GH_SHIM = """#!/usr/bin/env bash
printf 'gh %s\\n' "$*" >> "$SHIM_LOG"
exit "${SHIM_GH_RC:-0}"
"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        env=_clean_env(
            GIT_AUTHOR_NAME="gate",
            GIT_AUTHOR_EMAIL="gate@example.invalid",
            GIT_COMMITTER_NAME="gate",
            GIT_COMMITTER_EMAIL="gate@example.invalid",
        ),
    )


def _make_repo(
    root: Path,
    *,
    branch: str,
    touches_infra: bool,
    base_branch: str = "main",
    bulk_files: int = 0,
) -> Path:
    """An ordinary git repository: a base commit, then a feature branch.

    `bulk_files` adds that many files under a path that sorts AFTER `infra/`,
    so `git diff --name-only` keeps producing output long past the `infra/`
    line. That is what a tree-wide branch looks like, and what a reader of the
    file list has to survive — see the large-diff case in check 3.
    """
    repo = root / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    (repo / "scripts" / "land.sh").write_text(_read(LAND_SH), encoding="utf-8")
    (repo / "scripts" / "land.sh").chmod(0o755)

    _git(repo, "init", "-b", base_branch)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")

    if branch != base_branch:
        _git(repo, "checkout", "-b", branch)
        if touches_infra:
            (repo / "infra").mkdir()
            (repo / "infra" / "stack.ts").write_text("// change\n", encoding="utf-8")
        else:
            (repo / "app.txt").write_text("change\n", encoding="utf-8")
        if bulk_files:
            bulk = repo / "zz-bulk" / ("p" * 200)
            bulk.mkdir(parents=True)
            for i in range(bulk_files):
                (bulk / f"f{i:04d}-{'q' * 200}.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "change")

    return repo


def _make_shims(root: Path) -> tuple[Path, Path]:
    shims = root / "shims"
    shims.mkdir()
    (shims / "git").write_text(GIT_SHIM, encoding="utf-8")
    (shims / "gh").write_text(GH_SHIM, encoding="utf-8")
    (shims / "git").chmod(0o755)
    (shims / "gh").chmod(0o755)
    return shims, root / "shim.log"


def _run_land(
    root: Path,
    repo: Path,
    *,
    frontend_rc: int = 0,
    check_rc: int = 0,
    brand_rc: int = 0,
    push_rc: int = 0,
    gh_rc: int = 0,
) -> tuple[int, str, list[str]]:
    shims, log = _make_shims(root)
    real_git = subprocess.run(
        ["/usr/bin/env", "which", "git"], capture_output=True, text=True
    ).stdout.strip()

    env = _clean_env(
        PATH=f"{shims}{os.pathsep}{os.environ.get('PATH', '')}",
        REAL_GIT=real_git,
        SHIM_LOG=str(log),
        SHIM_PUSH_RC=str(push_rc),
        SHIM_GH_RC=str(gh_rc),
        LAND_GATE_FRONTEND_CMD=(
            f'printf "gate frontend\\n" >> "$SHIM_LOG"; exit {frontend_rc}'
        ),
        LAND_GATE_CHECK_CMD=(
            'printf "gate check SKIP_INFRA=[${SKIP_INFRA:-unset}]\\n" >> "$SHIM_LOG"; '
            f"exit {check_rc}"
        ),
        LAND_GATE_BRAND_CMD=f'printf "gate brand-free\\n" >> "$SHIM_LOG"; exit {brand_rc}',
        GIT_AUTHOR_NAME="gate",
        GIT_AUTHOR_EMAIL="gate@example.invalid",
        GIT_COMMITTER_NAME="gate",
        GIT_COMMITTER_EMAIL="gate@example.invalid",
    )

    proc = subprocess.run(
        ["bash", "scripts/land.sh"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, lines


# ---------------------------------------------------------------------------
# Check 1 — land.sh refuses to run on main
# ---------------------------------------------------------------------------


def check_refuses_on_main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _make_repo(root, branch="main", touches_infra=False)
        rc, output, log = _run_land(root, repo)

    failures += _assert(rc == 1, "land.sh on main exits 1", f"exit {rc}")
    failures += _assert(
        "main" in output and "refus" in output.lower(),
        "land.sh on main says why",
        output.strip().splitlines()[0] if output.strip() else "(no output)",
    )
    failures += _assert(
        not any(line.startswith("git push") for line in log),
        "land.sh on main pushed nothing",
        f"shim log: {log}",
    )
    failures += _assert(
        not any(line.startswith("gate ") for line in log),
        "land.sh on main ran no gate (it bails first)",
        f"shim log: {log}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check 2 — a red gate stops the landing, by name, before any push
# ---------------------------------------------------------------------------


def check_red_gate_blocks_push() -> int:
    failures = 0
    # Every gate in land.sh's vocabulary, plus check.sh's two non-1 red codes.
    scenarios = [
        ("frontend", {"frontend_rc": 1}, None),
        ("check", {"check_rc": 1}, None),
        ("brand-free", {"brand_rc": 1}, None),
        ("check", {"check_rc": 2}, "FLAKY"),
        ("check", {"check_rc": 3}, "lock"),
    ]
    for gate, kwargs, extra in scenarios:
        rc_label = next(iter(kwargs.values()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root, branch="phase-9/red", touches_infra=False)
            rc, output, log = _run_land(root, repo, **kwargs)

        failures += _assert(
            rc == 1, f"red gate '{gate}' (exit {rc_label}) makes land.sh exit 1", f"exit {rc}"
        )
        failures += _assert(
            f"FAILING GATE: {gate}" in output,
            f"red gate '{gate}' (exit {rc_label}) is named in the output",
        )
        failures += _assert(
            not any(line.startswith("git push") for line in log),
            f"red gate '{gate}' (exit {rc_label}) pushed nothing",
            f"shim log: {log}",
        )
        failures += _assert(
            not any(line.startswith("gh ") for line in log),
            f"red gate '{gate}' (exit {rc_label}) opened no pull request",
        )
        if extra:
            failures += _assert(
                extra.lower() in output.lower(),
                f"check.sh exit {rc_label} is explained as '{extra}'",
            )
    return failures


# ---------------------------------------------------------------------------
# Check 3 — green gates push, then open the pull request
# ---------------------------------------------------------------------------


def check_green_pushes_and_opens_pr() -> int:
    failures = 0
    for touches_infra, expected_skip in ((False, "1"), (True, "unset")):
        label = "infra/ touched" if touches_infra else "infra/ untouched"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root, branch="phase-9/green", touches_infra=touches_infra)
            rc, output, log = _run_land(root, repo)

        failures += _assert(rc == 0, f"green run exits 0 ({label})", f"exit {rc}; {output[-400:]}")
        push_idx = next(
            (i for i, line in enumerate(log) if line.startswith("git push")), None
        )
        pr_idx = next(
            (i for i, line in enumerate(log) if line.startswith("gh pr create")), None
        )
        failures += _assert(
            push_idx is not None, f"green run pushed the branch ({label})", f"shim log: {log}"
        )
        failures += _assert(
            pr_idx is not None and "--fill" in log[pr_idx],
            f"green run ran `gh pr create --fill` ({label})",
            f"shim log: {log}",
        )
        failures += _assert(
            push_idx is not None and pr_idx is not None and push_idx < pr_idx,
            f"push happens BEFORE the pull request is opened ({label})",
        )
        failures += _assert(
            push_idx is not None and "phase-9/green" in log[push_idx],
            f"the branch, not main, is what gets pushed ({label})",
            f"shim log: {log}",
        )
        failures += _assert(
            f"gate check SKIP_INFRA=[{expected_skip}]" in log,
            f"check.sh gate ran with SKIP_INFRA=[{expected_skip}] ({label})",
            f"shim log: {log}",
        )
        failures += _assert(
            ["gate frontend", f"gate check SKIP_INFRA=[{expected_skip}]", "gate brand-free"]
            == [line for line in log if line.startswith("gate ")],
            f"all three gates ran, in order ({label})",
            f"shim log: {log}",
        )

    # Third branch of the same decision: no `main` and no `origin/main` to diff
    # against. Unknown must resolve to the FULL gate, never to a quietly fast one.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _make_repo(
            root, branch="phase-9/green", touches_infra=False, base_branch="trunk"
        )
        rc, output, log = _run_land(root, repo)
    failures += _assert(rc == 0, "green run exits 0 (no main to diff against)", f"exit {rc}")
    failures += _assert(
        "gate check SKIP_INFRA=[unset]" in log,
        "an unresolvable diff base falls back to the FULL gate",
        f"shim log: {log}",
    )
    failures += _assert(
        "no 'main' or 'origin/main'" in output,
        "land.sh says why it chose the full gate",
    )

    # Fourth branch: infra/ IS touched, on a branch whose file list is far too
    # long to fit a pipe buffer. Reading the list through a
    # `git diff --name-only ... | grep -q '^infra/'` pipeline under `pipefail`
    # inverts the answer here — grep -q exits at the match, git dies of SIGPIPE
    # (141), pipefail reports 141, and land.sh quietly picks the FAST gate for a
    # branch that changes infra/, skipping the 24 slow infra tests that shell
    # out to the CDK. The one-file infra fixture above can never catch that;
    # this one can.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _make_repo(
            root, branch="phase-9/big", touches_infra=True, bulk_files=200
        )
        name_only = _git(repo, "diff", "--name-only", "main...").stdout
        rc, output, log = _run_land(root, repo)
    failures += _assert(
        len(name_only.encode("utf-8")) > 64 * 1024,
        "the large-diff fixture really does exceed 64 KB of file names",
        f"{len(name_only.encode('utf-8'))} bytes",
    )
    failures += _assert(
        rc == 0, "green run exits 0 (large infra diff)", f"exit {rc}; {output[-400:]}"
    )
    failures += _assert(
        "gate check SKIP_INFRA=[unset]" in log,
        "a LARGE infra diff still runs the FULL gate",
        f"shim log: {log}",
    )
    failures += _assert(
        "branch touches infra/" in output,
        "land.sh names infra/ as the reason on a large diff too",
    )

    # The two post-gate failure branches. Both are ordinary production states —
    # no network, an unauthenticated `gh`, a remote that rejects the push — and
    # neither is reachable through the happy path above.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _make_repo(root, branch="phase-9/green", touches_infra=False)
        rc, output, log = _run_land(root, repo, push_rc=1)
    failures += _assert(rc == 1, "a failed `git push` makes land.sh exit 1", f"exit {rc}")
    failures += _assert(
        not any(line.startswith("gh pr create") for line in log),
        "a failed push opens NO pull request",
        f"shim log: {log}",
    )
    failures += _assert(
        "no pull request was opened" in output,
        "land.sh says no pull request was opened",
        output.strip()[-200:],
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _make_repo(root, branch="phase-9/green", touches_infra=False)
        rc, output, log = _run_land(root, repo, gh_rc=1)
    failures += _assert(rc == 1, "a failed `gh pr create` makes land.sh exit 1", f"exit {rc}")
    failures += _assert(
        any(line.startswith("git push") for line in log),
        "the branch is pushed before `gh pr create` is tried",
        f"shim log: {log}",
    )
    failures += _assert(
        "branch IS pushed" in output,
        "a failed `gh` says the branch is already pushed — open the PR by hand",
        output.strip()[-200:],
    )
    return failures


# ---------------------------------------------------------------------------
# Check 4 — the pre-push hook
# ---------------------------------------------------------------------------

MAIN_REF_LINE = (
    "refs/heads/phase-9/x 1111111111111111111111111111111111111111 "
    "refs/heads/main 2222222222222222222222222222222222222222\n"
)
FEATURE_REF_LINE = (
    "refs/heads/phase-9/x 1111111111111111111111111111111111111111 "
    "refs/heads/phase-9/x 2222222222222222222222222222222222222222\n"
)

COUNTERPARTY_STUB = """#!/usr/bin/env python3
import os, sys
with open(os.environ["SHIM_LOG"], "a", encoding="utf-8") as fh:
    fh.write("counterparty-lint\\n")
sys.exit(int(os.environ.get("COUNTERPARTY_RC", "0")))
"""


def _hook_repo(root: Path, *, with_counterparty_lint: bool) -> Path:
    repo = root / "hookrepo"
    (repo / ".githooks").mkdir(parents=True)
    (repo / ".githooks" / "pre-push").write_text(_read(PRE_PUSH), encoding="utf-8")
    (repo / ".githooks" / "pre-push").chmod(0o755)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    if with_counterparty_lint:
        (repo / "tests").mkdir()
        lint = repo / "tests" / "lint-counterparty-names.py"
        lint.write_text(COUNTERPARTY_STUB, encoding="utf-8")
        lint.chmod(0o755)
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _run_hook(repo: Path, stdin: str, log: Path, **env_extra) -> tuple[int, str, list[str]]:
    env = _clean_env(SHIM_LOG=str(log), **env_extra)
    proc = subprocess.run(
        ["bash", ".githooks/pre-push", "origin", "git@example.invalid:o/r.git"],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, lines


def check_pre_push_guard() -> int:
    failures = 0

    cases = [
        ("main ref, no override", MAIN_REF_LINE, {}, False),
        ("main ref, LAND_TO_MAIN=1", MAIN_REF_LINE, {"LAND_TO_MAIN": "1"}, True),
        ("main ref, CI=1", MAIN_REF_LINE, {"CI": "1"}, True),
        (
            "main ref, CONTRACT_TOASTER_LOOP=1",
            MAIN_REF_LINE,
            {"CONTRACT_TOASTER_LOOP": "1"},
            True,
        ),
        ("feature ref, no override", FEATURE_REF_LINE, {}, True),
    ]
    for label, stdin, env_extra, should_allow in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _hook_repo(root, with_counterparty_lint=False)
            rc, output, _log = _run_hook(repo, stdin, root / "hook.log", **env_extra)
        if should_allow:
            failures += _assert(rc == 0, f"pre-push ALLOWS: {label}", f"exit {rc}; {output.strip()}")
        else:
            failures += _assert(
                rc != 0, f"pre-push REFUSES: {label}", f"exit {rc}; {output.strip()}"
            )
            failures += _assert(
                "LAND_TO_MAIN=1" in output,
                "the refusal names the deliberate override",
                output.strip(),
            )

    # The counterparty-name lint runs on an allowed push, and its red fails it.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _hook_repo(root, with_counterparty_lint=True)
        rc, _out, log = _run_hook(repo, FEATURE_REF_LINE, root / "hook.log")
    failures += _assert(rc == 0, "pre-push allows a green feature push", f"exit {rc}")
    failures += _assert(
        "counterparty-lint" in log, "pre-push runs the counterparty-name lint", f"log: {log}"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _hook_repo(root, with_counterparty_lint=True)
        rc, out, log = _run_hook(
            repo, FEATURE_REF_LINE, root / "hook.log", COUNTERPARTY_RC="1"
        )
    failures += _assert(
        rc != 0, "a red counterparty-name lint refuses the push", f"exit {rc}; {out.strip()}"
    )

    # The escape hatches skip the lint too — that is what keeps the hook from
    # ever blocking the autonomous loop's or CI's own pushes.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _hook_repo(root, with_counterparty_lint=True)
        rc, _out, log = _run_hook(
            repo, FEATURE_REF_LINE, root / "hook.log", CONTRACT_TOASTER_LOOP="1"
        )
    failures += _assert(
        rc == 0 and "counterparty-lint" not in log,
        "an escape-hatch push exits before doing any work",
        f"exit {rc}; log: {log}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check 5 — the workflow closes ci-main-red issues on a green push to main
# ---------------------------------------------------------------------------


def _job_block(text: str, job: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if re.match(rf"^  {re.escape(job)}:\s*$", line):
            inside = True
            continue
        if inside:
            if re.match(r"^  \S", line):  # next job at the same indent
                break
            out.append(line)
    return "\n".join(out)


def check_workflow_closes_main_red() -> int:
    failures = 0
    text = _read(CI_PIPELINE)
    block = _job_block(text, "close-main-red")

    failures += _assert(bool(block.strip()), "ci-pipeline.yml defines a close-main-red job")
    if not block.strip():
        return failures

    for needle, label in (
        ("gh issue list", "lists issues"),
        ("--label ci-main-red", "scoped to the ci-main-red label"),
        ("--state open", "only open ones"),
        ("gh issue close", "closes them"),
        ("${GITHUB_SHA}", "the comment names the green commit (GITHUB_SHA)"),
        ("--body-file", "comments before closing"),
        ("issues: write", "declares the issues:write permission"),
    ):
        failures += _assert(needle in block, f"close-main-red {label}", needle)

    failures += _assert(
        "github.event_name == 'push'" in block and "github.ref == 'refs/heads/main'" in block,
        "close-main-red runs only on a push to main",
    )
    failures += _assert(
        "always()" not in block,
        "close-main-red has no always() — a red gate skips it",
    )
    for needed in ("test-suite", "docs-lint", "detector-correctness", "security-scan", "frontend-gate"):
        failures += _assert(
            re.search(rf"needs:.*\b{re.escape(needed)}\b", block) is not None,
            f"close-main-red waits on the {needed} gate",
        )

    # The filer it mirrors must still be there.
    filer = _job_block(text, "report-main-failure")
    failures += _assert(
        "gh issue create" in filer and "--label ci-main-red" in filer,
        "report-main-failure still files the issue this job closes",
    )
    return failures


# ---------------------------------------------------------------------------
# Check 6 — every gate workflow runs on pull_request
# ---------------------------------------------------------------------------


def _on_block(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            if re.match(r"^on:", line):
                inside = True
                out.append(line)
            continue
        if line.strip() and re.match(r"^\S", line):
            break
        out.append(line)
    return "\n".join(out)


def check_every_gate_runs_on_pull_request() -> int:
    failures = 0
    workflows = sorted(WORKFLOWS_DIR.glob("*.yml"))
    failures += _assert(len(workflows) >= 20, "workflows discovered", f"{len(workflows)} files")

    no_push = set()
    for wf in workflows:
        block = _on_block(_read(wf))
        has_pr = re.search(r"^\s+pull_request:", block, re.M) is not None
        has_push = re.search(r"^\s+push:", block, re.M) is not None
        if not has_push:
            no_push.add(wf.name)
        if wf.name in NON_GATE_WORKFLOWS:
            continue
        failures += _assert(has_pr, f"{wf.name} runs on pull_request")

    failures += _assert(
        no_push == set(NON_GATE_WORKFLOWS),
        "the pull_request exemption is exactly the two non-gate workflows",
        f"workflows with no push trigger: {sorted(no_push)}",
    )
    return failures


# ---------------------------------------------------------------------------
# Check 7 — the pull-request template
# ---------------------------------------------------------------------------


def check_pr_template() -> int:
    failures = 0
    text = _read(PR_TEMPLATE)
    lowered = text.lower()
    for needle, label in (
        ("scripts/check-frontend.sh", "the frontend gate"),
        ("scripts/check.sh", "the Python gate"),
        ("tests/lint-brand-free.py", "the brand-free gate"),
    ):
        failures += _assert(needle in text, f"PR template names {label}", needle)
    failures += _assert(
        "docs updated in the same pr" in lowered, "PR template requires docs in the same PR"
    )
    failures += _assert(
        "plan/diagnostic row marked landed" in lowered,
        "PR template requires the plan/diagnostic row marked landed",
    )
    return failures


# ---------------------------------------------------------------------------
# Check 8 — setup-hooks.sh stays opt-in
# ---------------------------------------------------------------------------


def check_hooks_install_is_opt_in() -> int:
    failures = 0
    failures += _assert(SETUP_HOOKS.exists(), "scripts/setup-hooks.sh exists")
    failures += _assert(
        "core.hooksPath .githooks" in _read(SETUP_HOOKS),
        "setup-hooks.sh points core.hooksPath at .githooks",
    )

    watched: list[Path] = [
        REPO_ROOT / "scripts" / "check.sh",
        REPO_ROOT / "scripts" / "check-frontend.sh",
        REPO_ROOT / "scripts" / "collect_test_failures.sh",
        REPO_ROOT / ".pre-commit-config.yaml",
    ]
    watched += sorted(WORKFLOWS_DIR.glob("*.yml"))
    for path in watched:
        if not path.exists():
            continue
        failures += _assert(
            "setup-hooks" not in _read(path),
            f"{path.relative_to(REPO_ROOT)} does not install the hooks",
        )

    for pkg in (REPO_ROOT / "frontend" / "package.json", REPO_ROOT / "infra" / "package.json"):
        if not pkg.exists():
            continue
        scripts = json.loads(_read(pkg)).get("scripts", {})
        offenders = {k: v for k, v in scripts.items() if "setup-hooks" in v or "hooksPath" in v}
        failures += _assert(
            not offenders,
            f"{pkg.relative_to(REPO_ROOT)} has no lifecycle script installing hooks",
            str(offenders),
        )
    return failures


# ---------------------------------------------------------------------------


def main() -> int:
    for required in (LAND_SH, PRE_PUSH, SETUP_HOOKS):
        if not required.exists():
            print(f"MISSING: {required.relative_to(REPO_ROOT)}")
            return 1

    checks = [
        ("1", "land.sh refuses to run on main", check_refuses_on_main),
        ("2", "a red gate blocks the push, by name", check_red_gate_blocks_push),
        ("3", "green gates push, then open the PR", check_green_pushes_and_opens_pr),
        ("4", "pre-push guards main", check_pre_push_guard),
        ("5", "CI closes ci-main-red when main is green", check_workflow_closes_main_red),
        ("6", "every gate workflow runs on pull_request", check_every_gate_runs_on_pull_request),
        ("7", "the PR template names the gates", check_pr_template),
        ("8", "the hooks install stays opt-in", check_hooks_install_is_opt_in),
    ]

    total = 0
    for code, name, fn in checks:
        print(f"\n--- Check {code}: {name} ---")
        failed = fn()
        total += failed
        print(f"Check {code}: {name} … {'PASS' if failed == 0 else 'FAIL'}")

    print("\n" + "=" * 60)
    print("ALL GREEN" if total == 0 else f"FAILURES: {total} assertion(s) above")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
