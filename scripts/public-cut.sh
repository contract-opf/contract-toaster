#!/usr/bin/env bash
# public-cut.sh — verify + dry-run harness for the private -> public cut.
#
# This repo (exos-legal/contract-toaster) stays PRIVATE forever: its history
# holds a real client corpus. The public artifact is a FRESH-HISTORY cut
# published to contract-opf/contract-toaster. This script is the safety
# harness that runs BEFORE any publish: it builds the clean tree, proves the
# corpus/secret exclusions hold, and shows exactly how the cut differs from
# the current public repo.
#
# IT NEVER PUSHES. It produces a verified staging tree and a diff report; a
# human (or a reviewed PR) does the actual publish. Fresh history means a
# leaked file is not recoverable by reverting, so the gate runs first.
#
# What it does:
#   1. git archive <ref> -> a clean tree (no history).
#   2. Scrub every path in public-cut-exclude.txt.
#   3. Run tests/lint-public-cut-exclude.py (the manifest gate).
#   4. Independent scans the lint does NOT do: excluded paths really gone,
#      any non-synthetic .docx, secret patterns, stray .env files.
#   5. Diff the clean tree against the current public repo (added / removed /
#      modified), so a reviewer sees the blast radius before publishing.
#
# Exit non-zero if any HARD safety check fails (an excluded path survived, or
# a real-looking secret is present). Soft findings (non-synthetic docx,
# de-branding drift) are reported, not fatal — they need human judgment.
#
# Usage:
#   scripts/public-cut.sh [<git-ref>] [<public-repo>]
#     <git-ref>      commit/ref to cut from            (default: HEAD)
#     <public-repo>  owner/name of the public repo     (default: contract-opf/contract-toaster)
#
# Output tree is left at $OUTDIR for inspection.

set -euo pipefail

REF="${1:-HEAD}"
PUBLIC_REPO="${2:-contract-opf/contract-toaster}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

OUTDIR="${PUBLIC_CUT_OUTDIR:-/tmp/public-cut}"
STAGE="$OUTDIR/tree"
PUB="$OUTDIR/public"
MANIFEST="public-cut-exclude.txt"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"; command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3

fail=0
note() { printf '  %s\n' "$*"; }
section() { printf '\n=== %s ===\n' "$*"; }

[ -f "$MANIFEST" ] || { echo "FATAL: $MANIFEST not found — the exclusion manifest is the gate"; exit 2; }

rm -rf "$OUTDIR"; mkdir -p "$STAGE"

section "1. archive $REF -> clean tree (no history)"
git archive "$REF" | tar -x -C "$STAGE"
note "$(find "$STAGE" -type f | wc -l | tr -d ' ') files"

section "2. scrub excluded paths (from $MANIFEST)"
while IFS= read -r raw; do
  p="${raw%%#*}"; p="$(printf '%s' "$p" | xargs || true)"; [ -z "$p" ] && continue
  target="$STAGE/${p%/}"
  if [ -e "$target" ]; then rm -rf "$target"; note "removed: $p"; else note "(absent, ok): $p"; fi
done < "$MANIFEST"
note "$(find "$STAGE" -type f | wc -l | tr -d ' ') files after scrub"

section "3. manifest gate (tests/lint-public-cut-exclude.py)"
if "$PYTHON" tests/lint-public-cut-exclude.py; then note "lint PASS"; else note "lint FAIL"; fail=1; fi

section "4. independent safety scans"
# 4a. HARD: excluded paths must be gone from the tree.
while IFS= read -r raw; do
  p="${raw%%#*}"; p="$(printf '%s' "$p" | xargs || true)"; [ -z "$p" ] && continue
  if [ -e "$STAGE/${p%/}" ]; then note "❌ HARD: excluded path survived: $p"; fail=1; fi
done < "$MANIFEST"
# 4b. HARD: no obvious live secrets. (Report counts, not values — a log is public too.)
# NB: grep exits 1 on no-match; under `set -e` that would abort the scan, so
# every grep here is guarded with `|| true` — no-match is the GOOD case.
scan() { { grep -rIlE "$1" "$STAGE" 2>/dev/null || true; } | wc -l | tr -d ' '; }
# HARD patterns — effectively zero false positives; a hit is a real leak.
akia=$(scan 'AKIA[0-9A-Z]{16}'); pk=$(scan 'BEGIN [A-Z ]*PRIVATE KEY')
envs=$(find "$STAGE" -name '.env' -not -name '*.example' 2>/dev/null | wc -l | tr -d ' ')
note "AWS access-key ids: $akia   private-key blocks: $pk   stray .env: $envs"
[ "$akia" != 0 ] && { note "❌ HARD: AWS access key id present"; fail=1; }
[ "$pk"   != 0 ] && { note "❌ HARD: private key block present"; fail=1; }
[ "$envs" != 0 ] && { note "❌ HARD: a non-example .env is tracked"; fail=1; }
# SOFT: OpenRouter-shaped tokens. A regex can't tell a real key from a
# patterned test fixture (sk-or-v1-1111…), so this REPORTS for human review
# rather than failing — list the files (values masked; a CI log is public too).
orfiles=$({ grep -rIlE 'sk-or-v1-[0-9a-f]{20,}' "$STAGE" 2>/dev/null || true; })
if [ -n "$orfiles" ]; then
  note "SOFT: OpenRouter-shaped tokens present — confirm each is a test fixture, not a real key:"
  printf '%s\n' "$orfiles" | sed "s|$STAGE/|       |"
else
  note "no OpenRouter-shaped tokens found"
fi
# 4c. SOFT: .docx without a SYNTHETIC marker — needs a human to confirm it is not real corpus.
"$PYTHON" - "$STAGE" <<'PY' || true
import sys, zipfile, glob, os
root = sys.argv[1]; flagged = []
for p in glob.glob(os.path.join(root, "**", "*.docx"), recursive=True):
    rel = os.path.relpath(p, root)
    # A fixture may declare itself synthetic by FILENAME (*.SYNTHETIC.docx --
    # this repo's convention, since the generators name the output rather than
    # embedding a marker paragraph that would perturb the paragraph/anchor
    # offsets several fixtures exist to pin) or in its body text. Same rule as
    # tests/lint-brand-free.py's check 3 (issue #404) -- the two must agree,
    # or one tool's "clean" contradicts the other's.
    if ".SYNTHETIC." in rel.upper():
        continue
    try:
        with zipfile.ZipFile(p) as z:
            body = z.read("word/document.xml").decode("utf8", "ignore")
        if "SYNTHETIC" not in body.upper():
            flagged.append(rel)
    except Exception:
        flagged.append(rel + " (unreadable)")
if flagged:
    print("  SOFT: .docx without a SYNTHETIC marker (confirm each is not real corpus):")
    for f in flagged: print("       ", f)
else:
    print("  all .docx carry a SYNTHETIC marker")
PY

section "5. diff vs current public repo ($PUBLIC_REPO)"
if git clone -q --depth 1 "https://github.com/$PUBLIC_REPO" "$PUB" 2>/dev/null; then
  ( cd "$STAGE" && find . -type f | sed 's|^\./||' | sort ) > "$OUTDIR/cut.list"
  ( cd "$PUB" && find . -type f -not -path './.git/*' | sed 's|^\./||' | sort ) > "$OUTDIR/pub.list"
  added=$(comm -23 "$OUTDIR/cut.list" "$OUTDIR/pub.list" | wc -l | tr -d ' ')
  removed=$(comm -13 "$OUTDIR/cut.list" "$OUTDIR/pub.list" | wc -l | tr -d ' ')
  modified=0
  while IFS= read -r f; do cmp -s "$STAGE/$f" "$PUB/$f" || modified=$((modified+1)); done < <(comm -12 "$OUTDIR/cut.list" "$OUTDIR/pub.list")
  note "added (in cut, not public): $added   removed (in public, not cut): $removed   modified: $modified"
  note "files that exist ONLY in public (a fresh-history overwrite would DELETE these):"
  comm -13 "$OUTDIR/cut.list" "$OUTDIR/pub.list" | sed 's/^/       /' || true
  cutx=$({ grep -rIoh 'exos-legal' "$STAGE" 2>/dev/null || true; } | wc -l | tr -d ' ')
  pubx=$({ grep -rIoh 'exos-legal' "$PUB" --exclude-dir=.git 2>/dev/null || true; } | wc -l | tr -d ' ')
  note "de-branding check — 'exos-legal' occurrences  cut=$cutx  public=$pubx"
else
  note "(could not clone $PUBLIC_REPO — skipping diff; run with a reachable public repo)"
fi

section "verdict"
if [ "$fail" -ne 0 ]; then
  echo "  ❌ HARD SAFETY CHECK FAILED — do NOT publish. Fix $MANIFEST / the tree and re-run."
  echo "  staging tree: $STAGE"
  exit 1
fi
cat <<EOF
  ✅ hard safety checks passed. This is a DRY RUN — nothing was pushed.
  Review the soft findings and the diff above, then publish deliberately
  (a reviewed PR to $PUBLIC_REPO is the recommended path; the public repo has
  its own de-branding + history, so a blind force-push can regress it).
  Clean tree for inspection: $STAGE
EOF
