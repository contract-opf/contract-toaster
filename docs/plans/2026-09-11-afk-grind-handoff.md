# AFK grind handoff — 2026-09-11

Written for a fresh session picking up the autonomous backlog grind on
`contract-opf/contract-toaster`. Needs no other context. Supersedes the
Orbit Diner completion handoff of 2026-09-08 for "what to do next".

## State in one line

`main` is at `54597b6`, green, deployed. Epic #60 steps 1–4 (#50–#53) have
landed; #54 is mid-flight with a stashed, unreviewed fix attempt; 22 tickets
remain on the `afk` label. The loop is stopped, cleanly, waiting to be
relaunched.

## What landed today (2026-09-10 → 11)

| Ticket | Commit | What |
|---|---|---|
| #50 | `10283c1` (PR #87) | WAF polling limit 60→300, adaptive 3 s→10 s poll anchored on the review's `created_at` |
| #51 | `d16ca80` | explicit botocore timeouts and retry modes; Bedrock read timeout → `ModelTimeoutError` |
| #52 | `e1aa37d` | `status-index` GSI; every live-path scan of the reviews table replaced |
| #53 | `54597b6` | `ChecksumSHA256` on every S3 put; refused put → fixed 502; stalled submits aborted in the SPA |

Also today, outside the epics: PR #85/#86 fixed the DTS deploy leg, PR #88
filed the two remaining per-IP WAF exposures, and the Coolify compose was
repointed from the private repo's images to `ghcr.io/contract-opf/...`.

## Where the loop stopped

Three separate usage-limit resets killed the run mid-ticket (≈16:20, ≈21:20,
≈02:20 ET). Each time the same recovery was applied and it is the recovery
to apply again:

1. **Read the tree before anything else** (`git status`, `git stash list`).
   The kill table in the workflow-loop skill decides what a staged diff
   means. Today it was always "staged, complete, unreviewed" → stash it under
   the ticket's name, never land it.
2. **Post a `⏹️ Halted #N` marker on the run-log issue #89** so the
   head-blocker guard does not park the ticket on relaunch.
3. **Relaunch with the same args plus `autoRecover: true`.** The next coder
   finds the stash by grepping for `#N` (step 2d) and re-verifies it.

Current stash (only one; the #51 and #53 stashes were dropped after landing):

```
stash@{0}: wl #54 fix-round-2 attempt, staged but UNREVIEWED
```

#54's round-one review findings were recovered from the workflow journal and
posted as a comment on #54; the next coder must address them before
re-staging. `#89` carries the halted marker.

## How to relaunch

From `~/Documents/dev/contract-toaster-public` on a clean `main` that matches
`origin/main`:

```
Workflow({
  scriptPath: "/Users/marcmandel/.claude/skills/workflow-loop/assets/workflow-loop.js",
  args: {
    repo: "contract-opf/contract-toaster", label: "afk", branch: "main",
    checkCommand: "bash scripts/check-frontend.sh && bash scripts/check.sh",
    setupCommand: "source .venv/bin/activate",
    coderModel: "", coderEffort: "high",
    reviewerAgentType: "general-purpose", reviewerModel: "", reviewerEffort: "xhigh",
    onBlocked: "skip", blockedLabel: "afk-blocked", reportIssue: "auto",
    autoRecover: true, maxTickets: 0, maxReviewIterations: 3,
    coderNote: "<the CLAUDE.md hard-rules paragraph; copy from the previous run's args in the session log or from CLAUDE.md>",
    priority: [54,55,56,57,58,59,61,62,63,64,65,66,67,68,69,70,71,72,73,74,75,78,77],
    workers: 1
  }
})
```

Run `dryRun: true` first if anything about the queue looks off; it is
read-only. The journal issue is #89; the loop reuses it.

**Order** is epic #60 (#54–#59) then epic #76 (#61–#75) then #78 then #77.
#65, #73 and #75 are dependency-held and open up as their blockers land.
#79 is attended (rung 3: real hardware and a screen reader) and is not
labelled.

**Pace observed:** about one ticket per hour of loop time including review;
#54 needed a second review round. Expect ~22 hours of loop time, stretched
by every usage reset.

## Prod roll-forward (standing rule, owner decision 2026-09-10)

Bot Fight Mode stays ON for the zone, so the GitHub deploy leg of
`dts-image-publish.yml` always fails at the edge (expected, not a
regression). Images still publish on every green `main`. After each landing:

```bash
bash /private/tmp/claude-502/-Users-marcmandel-Documents-dev-contract-toaster-public/910c593d-c65e-416a-92ec-05c953bec404/scratchpad/roll-forward.sh
```

That scratchpad path is session-specific and may be gone. The script is
small; its logic: newest completed `dts-image-publish.yml` run with both
build jobs green → if its SHA differs from the last rolled one, POST

```
https://coolify.marcmandel.com/api/v1/services/zb33rd4l8nnf68h202bfjj25/restart?latest=true
```

with headers `CF-Access-Client-Id` / `CF-Access-Client-Secret` /
`Authorization: Bearer …` read from the login keychain items
`cf-access-coolify-client-id`, `cf-access-coolify-client-secret`,
`coolify-marcmandel-api`. GET returns 405; Python's default User-Agent gets
Cloudflare 1010 (use curl). Verify by fetching `/` with a cachebust in a
browser session and diffing the `index-*.js` hash; `/version` is advisory
(#613). Last rolled: `54597b6`, hash `index-BuakuPjj.js`.

The Chrome session to `toaster.marcmandel.com` expired overnight; the hash
check still works unauthenticated, `/version` does not.

## Gotchas hit today

- `frontend/.env.local` must not exist when running the gate.
- The public checkout's `.venv` must be Python 3.11
  (`/usr/local/opt/python@3.11/bin/python3.11 -m venv .venv`), then
  `pip install -r backend/requirements.txt -r requirements-dev.txt`.
- Two `scripts/check.sh` runs writing the same log file corrupt it; the exit
  code is still authoritative.
- `npm test -- src/__tests__/<file>` is the single-file shape; the wrapper
  inserts `run`.
- Never write `new URL('...', import.meta.url)` in a test — Vite rewrites it;
  use `fileURLToPath(import.meta.url)` like the sibling tests.
- The workflow journal is `~/.claude/projects/<project>/<session>/subagents/workflows/<runId>/journal.jsonl`;
  records are keyed by `key` (e.g. `review-54-i1`), and `result` holds the
  full agent return. Mine it before paying for a re-review.

## Open beyond the queue

- #88 — per-IP WAF exposures (rule 4's 10 POSTs/5 min covers cancel and
  preflight; rule 5 is ~5 concurrent reviews per NAT).
- #79 — attended device/accessibility matrix.
- #613 — stale `/version` stamp.
- The Cloudflare Access service token's two halves were displayed in the
  2026-09-10 session transcript by a browser tool; rotate if that transcript
  is ever a concern (Zero Trust → Access controls → Service credentials).
