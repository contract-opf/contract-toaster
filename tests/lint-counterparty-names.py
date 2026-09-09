#!/usr/bin/env python3
"""CI gate (issue #341): no counterparty identity on the public surface.

## Why this exists

Until 2026-09-08 the only thing standing between a counterparty name and the
public repository was a human reading a diff at cut time. That held while
publication was a deliberate, occasional act. It does not hold once the trunk
is public, because **a push is a publication**.

It had already failed once. On 2026-09-04 two docs landed under `docs/` --
which is not in `public-cut-exclude.txt` -- quoting a real counterparty
agreement filename. They passed `tests/lint-brand-free.py` (which polices the
private ORG, not counterparties), passed `tests/lint-public-cut-exclude.py`
(which checks manifest paths, not content), and passed the cut script's own
scans (keys, `.env` files, unmarked `.docx` -- not prose). The live public repo
escaped only because the previous cut predated them. That cut workflow retired
on 2026-09-09 when development moved here; this gate did not, because a public
trunk needs it more, not less.

## The design constraint: the denylist is itself the secret

A list of counterparty names cannot live in this repository. So the two halves
are split:

  - **This file is public and carries no data.** It takes a path to a token
    list and reports matches.
  - **The token list is private.** `overlay/gen-counterparty-tokens.py`
    derives it from the corpus, and it lives only in the private overlay.

## Fail-open, deliberately

With no token list reachable, this gate SKIPS and exits 0. An outside
contributor with no access to the overlay must still be able to run the checks
and push. The backstop that makes fail-open safe is a scheduled scan run from
the private overlay against the published tree -- somewhere that always has
the list.

## What is scanned

`git ls-files` MINUS every path in `public-cut-exclude.txt` -- the same "public
surface" definition `tests/lint-brand-free.py` uses, so both gates cover
exactly what a public cut would publish.

## Matching

Per line, case-insensitively, over a normalized form: every run of
non-alphanumeric characters collapses to one space. So a token
`Placeholder State University` matches that phrase spaced, hyphenated,
underscored, in any case, and inside the hyphen-joined filename shape the
2026-09-04 leak actually took.

(The example is deliberately fabricated. An earlier draft of this docstring
used a REAL counterparty name to illustrate the same point, and this gate
caught it on the publish path the moment the file was committed -- which is
both the correct behaviour and a reminder that a scanner's own documentation
is on the public surface like everything else.)

## Output discipline

A finding prints the file, the line number, and the token's INDEX in the
private list. It never prints the token or the matching line: a CI log is
public too, and a scanner that echoes what it found would leak exactly what it
exists to protect.

## Self-test

Before trusting the real scan, this plants a known token in a temp file and
asserts the scanner catches it, plants a hyphenated variant and asserts the
same, and plants a clean file and asserts it does not -- so a scanner that
silently matches nothing cannot pass.

Run standalone:
    python3 tests/lint-counterparty-names.py [--tokens PATH] [--tree DIR]

`--tree DIR` scans every text file under DIR instead of this repo's public
surface. That is how the scheduled private scan checks the ALREADY-PUBLISHED
tree: clone the public repo, point `--tree` at it, and supply the private token
list. It is the backstop that makes fail-open safe.

Exit codes: 0 = pass or skipped, 1 = fail.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "public-cut-exclude.txt"

# Where a token list may be found, in order. All are outside the public surface.
TOKEN_ENV = "COUNTERPARTY_TOKENS"
TOKEN_CANDIDATES = (
    REPO_ROOT / "overlay" / "counterparty-tokens.txt",
    REPO_ROOT.parent / "contract-toaster-overlay" / "counterparty-tokens.txt",
)

# A token shorter than this is rejected by the generator and ignored here: short
# strings produce false positives that train people to ignore the gate.
MIN_TOKEN_LEN = 6

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> str:
    """Lowercase, then collapse every non-alphanumeric run to a single space."""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


# ---------------------------------------------------------------------------
# Public surface (same definition as tests/lint-brand-free.py; kept as an
# independent copy so neither gate can break the other by refactoring)
# ---------------------------------------------------------------------------


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _excluded() -> tuple[set[str], set[str]]:
    dirs: set[str] = set()
    exact: set[str] = set()
    if not MANIFEST.exists():
        return dirs, exact
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        p = raw.split("#", 1)[0].strip()
        if not p:
            continue
        (dirs if p.endswith("/") else exact).add(p)
    return dirs, exact


def public_surface() -> list[str]:
    dirs, exact = _excluded()
    return [
        rel
        for rel in _tracked_files()
        if rel not in exact and not any(rel.startswith(d) for d in dirs)
    ]


# ---------------------------------------------------------------------------
# Token list
# ---------------------------------------------------------------------------


def find_token_list(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    env = os.environ.get(TOKEN_ENV)
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    for cand in TOKEN_CANDIDATES:
        if cand.is_file():
            return cand
    return None


def load_tokens(path: Path) -> list[str]:
    """Normalized tokens, in file order. The index printed on a finding is the
    1-based position here, so the operator can look it up in their own copy."""
    tokens = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        tok = raw.split("#", 1)[0].strip()
        if not tok:
            continue
        norm = normalize(tok)
        if len(norm) < MIN_TOKEN_LEN:
            continue
        tokens.append(norm)
    return tokens


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan_text(text: str, tokens: list[str]) -> list[tuple[int, int]]:
    """[(line_number, token_index)] -- never the token, never the line."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        norm = normalize(line)
        if not norm:
            continue
        for idx, tok in enumerate(tokens, 1):
            if tok in norm:
                hits.append((lineno, idx))
    return hits


def _read(rel: str) -> str | None:
    try:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable


def scan_surface(tokens: list[str], token_list_path: Path) -> list[tuple[str, int, int]]:
    findings = []
    try:
        token_rel = str(token_list_path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        token_rel = None
    for rel in public_surface():
        if rel == token_rel:
            continue  # never scan the list against itself
        text = _read(rel)
        if text is None:
            continue
        for lineno, idx in scan_text(text, tokens):
            findings.append((rel, lineno, idx))
    return findings


# Directories with nothing a human wrote; skipped when walking an arbitrary tree.
_SKIP_DIRS = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}


def scan_tree(root: Path, tokens: list[str]) -> list[tuple[str, int, int]]:
    """Scan every readable text file under `root` -- used for the scheduled
    scan of the already-published tree, where there is no manifest to subtract
    and everything present is by definition public."""
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(path.relative_to(root))
        for lineno, idx in scan_text(text, tokens):
            findings.append((rel, lineno, idx))
    return findings


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def self_test() -> None:
    """A scanner that matches nothing must not be able to report a clean run."""
    tokens = [normalize("Placeholder University"), normalize("Example Health")]

    planted_plain = "We reviewed the Placeholder University agreement.\n"
    planted_hyphen = "file: 7259-Example Health - Affiliation Agreement.docx\n"
    planted_under = "path/to/placeholder_university_2026.docx\n"
    clean = "We reviewed a real counterparty agreement (identity withheld).\n"

    for label, sample in (
        ("plain", planted_plain),
        ("hyphenated", planted_hyphen),
        ("underscored", planted_under),
    ):
        if not scan_text(sample, tokens):
            raise SystemExit(f"SELF-TEST FAILED: scanner missed a {label} planted token")
    if scan_text(clean, tokens):
        raise SystemExit("SELF-TEST FAILED: scanner fired on clean text")

    # A short token must be dropped by the loader, not silently matched.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("# comment\nUSF\nPlaceholder University\n\n")
        tmp = Path(fh.name)
    try:
        loaded = load_tokens(tmp)
        if normalize("USF") in loaded:
            raise SystemExit("SELF-TEST FAILED: a token under the length floor was loaded")
        if normalize("Placeholder University") not in loaded:
            raise SystemExit("SELF-TEST FAILED: a valid token was dropped")
    finally:
        tmp.unlink(missing_ok=True)

    print("  self-test OK — planted plain/hyphenated/underscored caught, clean text not flagged.")


# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    explicit = None
    if "--tokens" in argv:
        explicit = argv[argv.index("--tokens") + 1]
    tree = None
    if "--tree" in argv:
        tree = Path(argv[argv.index("--tree") + 1]).expanduser()
        if not tree.is_dir():
            print(f"--tree {tree} is not a directory")
            return 1

    print("COUNTERPARTY-NAME LINT (issue #341)\n")
    self_test()

    path = find_token_list(explicit)
    if path is None:
        print(
            f"\n  SKIPPED — no token list found.\n"
            f"  Looked at: ${TOKEN_ENV}, "
            + ", ".join(str(c) for c in TOKEN_CANDIDATES)
            + "\n"
            "  This is the intended behaviour outside the private overlay: an\n"
            "  outside contributor must be able to run the checks. The private\n"
            "  scheduled scan of the published tree is the backstop.\n"
        )
        print("COUNTERPARTY-NAME LINT: SKIPPED (no token list)")
        return 0

    tokens = load_tokens(path)
    if not tokens:
        print(f"\n  Token list {path} contains no usable tokens (min length {MIN_TOKEN_LEN}).")
        print("COUNTERPARTY-NAME LINT: FAIL")
        return 1

    print(f"\n  Token list: {path} ({len(tokens)} tokens)")
    if tree is not None:
        print(f"  Scanning published tree: {tree}\n")
        findings = scan_tree(tree, tokens)
    else:
        surface = public_surface()
        print(
            f"  Public surface: {len(surface)} tracked files "
            "(excl. public-cut-exclude.txt paths)\n"
        )
        findings = scan_surface(tokens, path)
    if findings:
        where = "in the published tree" if tree is not None else "on the public surface"
        print(f"  ❌ counterparty identity found {where}:\n")
        for rel, lineno, idx in findings:
            # File and line only. Never the token, never the line content.
            print(f"     {rel}:{lineno} — matches private token #{idx}")
        print(
            f"\n  {len(findings)} finding(s). Look up the token index in your own copy of\n"
            f"  {path}. Remove or redact the reference; do not add it to an allowlist.\n"
        )
        print("COUNTERPARTY-NAME LINT: FAIL")
        return 1

    where = "in the published tree" if tree is not None else "on the public surface"
    print(f"  OK — no counterparty identity {where}.\n")
    print("COUNTERPARTY-NAME LINT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
