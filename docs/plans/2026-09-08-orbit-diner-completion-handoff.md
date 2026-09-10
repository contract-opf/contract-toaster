# Orbit Diner — completion handoff

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

8 September 2026. Written for a fresh session on another machine and another
Claude account. It needs no other context. Supersedes
`2026-09-07-orbit-diner-loop-handoff.md`, which described the run that has now
finished.

## State in one line

The Orbit Diner console **is** the Review tab. All 21 loop tickets under epic
#729 are closed, the build-time flag is gone, and `main` is at `7118e1c` with
every gate green.

## What is actually done

Twenty-two commits, `eb115da` through `e015c0f`, replaced the Review tab; a
twenty-third (`7118e1c`) changed the DTS publication rules. Verified on
`7118e1c`, by hand, not by trusting the loop:

```
bash scripts/check-frontend.sh          CHECK-FRONTEND: ALL GREEN
bash scripts/check.sh                   CHECK: ALL GREEN
python3 tests/lint-brand-free.py        BRAND-FREE LINT: PASS  (exit 0)
python3 tests/lint-issue-349-debrand.py PASS
shasum -a 256 -c frontend/vendor/orbit-diner/MANIFEST.sha256   71/71 OK
```

`frontend/src/orbit-diner/flag.ts` no longer exists and `ORBIT_DINER_ENABLED`
has no references anywhere.

## The one thing that matters most

`frontend/vendor/orbit-diner/` is the designer's source of record and must never
be edited — it is the baseline the next supplier patch verifies its
before-hashes against. All 71 hashes still match. Our copy, the one that is
compiled, tested and audited, is `frontend/src/orbit-diner/`. The procedure for
applying a future designer patch is in that vendor directory's `README.md`.

## What is NOT done

**#736 — attended acceptance. This is the only open work, and it needs a
human.** A GPU frame trace, a real Windows screen reader, and a real `.docx`
through a live deployment. There is no CLI, API or browser-automation path;
that is why it was never in the loop. Everything around the human's part is
prepared — work the enumerated list in the issue and paste the numbers into one
comment.

**Nobody has looked at this UI.** Twenty-two commits replaced the entire Review
tab and every one of them was verified only by automated gates. That matters
more than it sounds, because of the next section.

## The gap in the gate, and why it bit twice

`frontend/vitest.config.ts` runs jsdom with `css: false`. **No gate in this repo
can see a stylesheet.** Two real defects rode straight through green suites:

- **#727** deleted `Toaster.tsx`, which held the only declarations of the
  `.toaster-receipt*` rules — while `ToastReceipt.tsx` was deliberately kept and
  is still rendered from `ReviewHistory.tsx`. The History tab's expanded row
  would have lost the flex rule that separates each label from its value
  ("Playbook type" and its value painting concatenated). Every gate stayed
  green. A reviewer reading the diff caught it. The rules now live in
  `frontend/src/styles/app.css`.
- **#739** shortened filenames by measurement but let the extension be orphaned
  onto its own line, so the last painted line carried no `.docx`. Nine
  `endsWith('.docx')` assertions passed over the hole because the fixtures never
  landed in the band where it happens.

When judging Orbit Diner work, a green gate is necessary and not sufficient.
Read the diff for anything touching CSS.

## Publication rules changed today

`7118e1c`. `.github/workflows/dts-image-publish.yml` no longer waits for a human
click: it fires on the **`CI pipeline`** workflow succeeding on `main`, and a
`deploy` job triggers the Coolify pull itself.

**This governs the DTS/Coolify path only.** `ci-pipeline.yml`'s security
invariant 1 — no auto-deploy from main, promotion to App Runner is a deliberate,
cosign-verified, audited step, `autoDeploymentsEnabled=false` pinned in AppStack
(#55) — is untouched and governs the AWS path. `ARCHITECTURE.md` says which
invariant covers which target. Do not loosen the AWS one without a decision.

### Two things must be finished by hand before the automation works

1. **GitHub Actions secrets** at `Settings -> Secrets and variables -> Actions`
   in the GitHub repo (not Coolify):
   - `COOLIFY_DEPLOY_WEBHOOK` =
     `https://<your-coolify-host>/api/v1/services/<service-uuid>/restart?latest=true`
   - `COOLIFY_API_TOKEN` = a Coolify API token with deploy permission
     (Coolify -> Keys & Tokens -> API tokens)
   - `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` = a Cloudflare Access
     service token scoped to the Coolify application, only if the host sits
     behind Access (both halves optional otherwise)

   Until the webhook exists the deploy job warns and exits 0 — images still
   publish, the deployment simply does not move.

2. **The deploy leg first ran on 2026-09-10** and taught two things, both now
   fixed in the workflow. The generic `/api/v1/deploy` endpoint ignores `force`
   for a service and recreates containers from the `:latest` already on the
   box, so it deploys nothing new — the services `restart?latest=true`
   endpoint is the API twin of the UI's "Restart (pull latest)" and is what the
   webhook must point at. And a Coolify host behind Cloudflare Access answers
   the runner with a challenge page before the bearer token is ever read; the
   step now recognises that and names the service-token fix instead of
   printing the HTML. The endpoint is a GET; a `COOLIFY_DEPLOY_METHOD` repo
   variable overrides it.

### Deploy state right now

Images for `e015c0f` are published to the container registry
(`contract-toaster-dts-{backend,frontend}`, `:latest` and `:e015c0f`) — under
the private origin's namespace at the time; publishing now happens from this
repository, so the path is `ghcr.io/contract-opf/...` — but **the DTS host was never rolled forward** — that click never
happened. `7118e1c` should trigger a fresh publish on its own now; the roll
forward still needs the secrets above or a manual Restart.

**Do not verify a deploy with `GET /version`.** Issue #613 is open: the backend
build cache can ignore a changed `--build-arg COMMIT_SHA`, so it serves a stale
stamp after a good pull. The SPA bakes its own `VITE_COMMIT_SHA` at build time,
so the Settings tab showing the two halves agree is the honest check.

## Open follow-ups, both filed on epic #729

- Separator-heavy filenames flush line 1 short of the box, so the preserved head
  is shorter than it needs to be. Correctness is fine; head length is not.
- The console is cramped just above its own breakpoint: the phone step takes
  over at 560px but the desktop grid keeps splitting the scene into columns
  above it, so a console at 561–660px gets a 58–69px inscription box at 20px
  type — too narrow for `….docx` on a line of its own. #739's width sweep skips
  that band and documents why. Decide whether the phone step should extend
  higher or the desktop grid collapse sooner.

Also open and unrelated to this epic: **#613** (stale `/version` stamp), which
is `afk-backlog` rather than loop-servable — its acceptance needs two real CI
runs diffed against each other and a look at the self-hosted runner.

## Notes for the next session

- The autonomous loop is `~/Dev/Skillz/workflow-loop/assets/workflow-loop.js`,
  driven entirely through `args`. It halted twice on this queue and both halts
  were worth inspecting rather than retrying: once on a jsdom worker that failed
  to initialise under load (`window.localStorage is undefined`, did not
  reproduce), once on a landing failure. Re-running a single test file before
  concluding the work is broken is worth the ten seconds.
- Two tickets parked on legitimate review findings (#727, #739) and were
  finished by hand. Parked work is stashed; the park comment names the stash.
- `.claude/settings.local.json` is gitignored and will NOT travel to the new
  machine. It carried `Bash(git commit:*)`, which this session needed before it
  could commit changes touching `.github/workflows/`. Recreate it if the same
  block appears.
- Commit attribution: no co-author trailers in this repo.
