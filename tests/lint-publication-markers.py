#!/usr/bin/env python3
"""CI gate (QA remediation W2, 2026-10-31): no tracked file may carry an
"internal only" marker or a private local path.

## Why this exists

This repository is public and is the development trunk: a push is a
publication. On 2026-09-08 an internal owner-decision record that described
itself, in its own text, as not for publication was committed under
`docs/plans/` and shipped with the tree. Nothing caught it: the public-cut
exclusion manifest names directories (`docs/planning/`), not individual files
in `docs/plans/`, and no gate read file text for a self-declared marker.
The same sweep found a session scratchpad path under `/private/tmp/` and a
`/Users/<name>/` home path in a handoff note -- harmless on their own, but
they leak a local username and machine layout, and they are dead links to
anyone else.

## What fails

Every tracked text file (`git ls-files`, binary files skipped) is scanned
line by line for:

1. MARKER, case-insensitive: the phrases "not for publication" and
   "never publish". A document that says either about ITSELF does not belong
   here; a document that merely needs to talk about the rule can say it
   another way ("internal-only", "must not ship publicly").
2. PRIVATE PATH, case-sensitive: an absolute path under `/private/tmp/`
   (a macOS per-session temp/scratchpad directory) or under
   `/Users/<name>/` (a macOS home directory). Use `~`, `$HOME`, a
   `<placeholder>`, or a path relative to the repository instead.

Unlike `tests/lint-brand-free.py`, this scans the whole tracked tree, not the
tree minus `public-cut-exclude.txt`: in this repository every tracked file is
published, and a file named in that manifest that reappears is exactly the
case this gate exists to catch.

## Allowlist

`ALLOWLIST` below is deliberately tiny. Each entry says why. Adding one is a
reviewed decision; rewording the offending text is almost always the better
fix.

## Self-test

Before trusting the real scan, it plants each violation class in a temp file
and asserts the scanner flags it, and plants clean look-alikes (a `/users/`
API route, a bare `/private/tmp` with no trailing path, "publication" in an
unrelated sentence) and asserts they are not flagged -- so a scanner that
silently matches nothing cannot pass.

Run standalone: `python3 tests/lint-publication-markers.py`
Exit codes: 0 = pass, 1 = fail.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Phrases a document uses to mark ITSELF as internal. `\s+` also matches
# a newline, and markers are searched across the whole file text, so a phrase
# hard-wrapped onto a second line is still caught (reported at its first line).
MARKER_RE = re.compile(r"not\s+for\s+publication|never\s+publish", re.IGNORECASE)

# Absolute private paths. `/Users/` must be followed by a name segment and a
# slash, so `/users/{id}` routes (lowercase) and a prose mention of the
# directory itself do not trip it. Case-sensitive on purpose: macOS spells
# these two directories exactly this way.
PRIVATE_PATH_RE = re.compile(r"/private/tmp/|/Users/[A-Za-z0-9._-]+/")

# ---------------------------------------------------------------------------
# Allowlist: path -> why. Kept minimal. Every other file is scanned in full.
# ---------------------------------------------------------------------------
ALLOWLIST = {
    "tests/lint-publication-markers.py": "this gate itself -- defines the patterns",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True  # noqa: S607
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable -- nothing textual to leak


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return (line number, kind) for every offending line."""
    hits: list[tuple[int, str]] = []
    for m in MARKER_RE.finditer(text):
        hits.append((text.count("\n", 0, m.start()) + 1, "marker"))
    for i, line in enumerate(text.splitlines(), 1):
        if PRIVATE_PATH_RE.search(line):
            hits.append((i, "private-path"))
    return hits


def scan_tree(root: Path, files: list[str]) -> tuple[list[str], list[str]]:
    """(marker hits, private-path hits) as `path:line` strings."""
    markers: list[str] = []
    paths: list[str] = []
    for rel in files:
        if rel in ALLOWLIST:
            continue
        text = _read(root / rel)
        if text is None:
            continue
        for lineno, kind in scan_text(text):
            (markers if kind == "marker" else paths).append(f"{rel}:{lineno}")
    return markers, paths


# ---------------------------------------------------------------------------
# Self-test: prove the scanner catches each class before trusting a clean run.
# ---------------------------------------------------------------------------


def self_test() -> None:
    planted = {
        "a.md": ("**Internal. Not for " + "publication.**\n", "marker"),
        "b.md": ("This record must NEVER\n" + "publish anywhere.\n", "marker"),
        "c.md": ("bash /private/" + "tmp/claude-1/scratchpad/x.sh\n", "private-path"),
        "d.md": ('scriptPath: "/' + 'Users/someone/.claude/x.js"\n', "private-path"),
    }
    clean = (
        "GET /users/{id}/profile returns the record.\n"
        "Never write plans to `/private/tmp` -- macOS purges it.\n"
        "Publication of the decision is a separate step.\n"
        "Run it from ~/Documents/dev or <home>/.claude/skills.\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for name, (body, kind) in planted.items():
            (tmp / name).write_text(body, encoding="utf-8")
            hits = scan_text((tmp / name).read_text(encoding="utf-8"))
            if (1, kind) not in hits:
                raise AssertionError(f"self-test failed: did not flag {kind} in {body!r}")
        (tmp / "clean.md").write_text(clean, encoding="utf-8")
        if scan_text(clean):
            raise AssertionError(f"self-test failed: flagged clean text: {scan_text(clean)}")
        markers, paths = scan_tree(tmp, [*planted, "clean.md"])
        if len(markers) != 2 or len(paths) != 2:
            raise AssertionError(
                f"self-test failed: scan_tree found {markers} / {paths}, expected 2 + 2"
            )
    print("Self-test OK: scanner catches both marker phrases and both private-path shapes,")
    print("              and does not flag /users/ routes, a bare /private/tmp, or prose.")


def main(argv: list[str]) -> int:
    _ = argv
    self_test()

    files = _tracked_files()
    print(f"\nTracked files: {len(files)} (allowlisted: {len(ALLOWLIST)})")
    markers, paths = scan_tree(REPO_ROOT, files)

    failures = 0

    print('\nCheck 1: no "not for publication" / "never publish" marker in a tracked file …')
    if markers:
        failures += 1
        print(f"  FAIL — {len(markers)} occurrence(s):")
        for h in markers[:40]:
            print(f"    {h}")
        print("  A file that marks itself internal does not belong in this public tree:")
        print("  remove it (and add it to public-cut-exclude.txt), or, if the text only")
        print("  discusses the rule, reword it.")
    else:
        print("  OK — none.")

    print("\nCheck 2: no /private/tmp/ or /Users/<name>/ path in a tracked file …")
    if paths:
        failures += 1
        print(f"  FAIL — {len(paths)} occurrence(s):")
        for h in paths[:40]:
            print(f"    {h}")
        print("  Use ~, $HOME, a <placeholder>, or a repository-relative path instead.")
    else:
        print("  OK — none.")

    if failures:
        print(f"\nPUBLICATION-MARKERS LINT: FAIL ({failures} check(s) failed)")
        return 1
    print("\nPUBLICATION-MARKERS LINT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
