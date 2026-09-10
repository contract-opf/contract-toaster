# Audit & hardening diagnostic — 2026-09-05

> *Published from the project's planning history on 2026-09-09. This is a
> record of what was decided at the time, not current instructions. Issue
> numbers predate the tracker migration of 2026-09-08 and refer to the old
> private numbering, and any repository path or command shown may name the
> private origin this work was done in. Read both as history, not as links
> to follow.*

> **Status note, 2026-09-10.** F1 / A1 landed as public issue
> `contract-opf/contract-toaster#50` (private #690 in the table below):
> polling limit 300 / 5 min, adaptive 3 s → 10 s poll anchored on the
> review's `created_at`, pinned by `frontend/src/__tests__/poll-budget-waf.test.tsx`.
> The finding text below is left as written; it describes the state before
> that change. Later steps will add their own lines here.
>
> F3 / A3 landed as public issue `contract-opf/contract-toaster#51` (private
> #691): `backend/src/config.py::botocore_config` pins the Bedrock client to
> `read_timeout=300`, `connect_timeout=10`, `retries={"mode": "standard",
> "max_attempts": 2}` and the DynamoDB client to `adaptive` mode with 15 s /
> 5 s timeouts, routed through `boto3_client_kwargs` and
> `LiveBedrockModelClient._get_client`; a `ReadTimeoutError` now surfaces as
> `model_timeout`. Pinned by `tests/test_config_botocore_51.py` and
> `tests/test_bedrock_client_transport_51.py`; rationale in
> `ARCHITECTURE.md` → Cost shape → "Transport retry budget (Bedrock)".

Principal-engineer sweep of Contract Toaster across four pillars (stability,
accuracy, security, performance). This document is the FINDINGS record and the
prioritised plan. Nothing here had been changed in code when it was written
(the status note above tracks what has landed since); each finding names
the file and the intended remediation so a follow-up session can execute it
without re-deriving the diagnosis. The walkthrough for executed work lands in
`docs/walkthroughs/2026-09-05-audit-hardening-walkthrough.md`.

Baseline at `260db27` (main, clean):

| Gate | Result |
|---|---|
| `frontend: npm test` | 89 files, 962 tests passed |
| `tests/lint-brand-free.py` | PASS |
| `vite build --mode production` | one JS chunk, 953 kB min / 270 kB gzip; one CSS chunk 340 kB |
| `scripts/check.sh` (SKIP_INFRA) | ALL GREEN, exit 0 (24 cdk-synth infra tests skipped) |

Reference codes are stable for the rest of this work: `F` findings, `A` planned
actions, `R` risks, `Q` questions parked for the owner.

## Priority summary

| Code | Pillar | Severity | One line |
|---|---|---|---|
| F1 | Stability | **P1** | *Landed — see the status note above.* WAF polling rule (60 req / 5 min per IP) is below the UI's own poll rate (3 s = 100 req / 5 min); any review longer than ~3 min gets the reviewer's IP blocked mid-review. |
| F2 | Performance | **P1** | The `reviews` table is `.scan()`ed on six live request paths (admin list, admin health, retention preview/sweep/holds, legal triage) despite the documented "never scan `reviews`/`audit`" invariant. |
| F3 | Stability | P2 | *Landed — see the status note above.* Bedrock and DynamoDB boto clients are built with no `botocore.Config`: default 60 s read timeout and legacy retry mode. A long primary-pass generation trips the socket timeout and is silently retried by botocore, paying up to 4x. |
| F4 | Stability | P2 | The upload `put_object` sends no integrity checksum; S3 accepts a truncated/corrupted body and the review runs on it. The submit fetch has no abort/timeout, so a stalled upload spins forever in "Submitting". |
| F5 | Accuracy | P2 | Markup intensity ("browning") is not a wire field. The frontend composes a prose sentence into `toaster_guidance`; the backend has no `browning` concept, no enum, no schema slot, no audit field. Output contract is non-deterministic for the dial. |
| F6 | Accuracy | P2 | Party recognition is exact-string, case/whitespace-insensitive only. No suffix folding (GmbH/SAS/B.V./S.r.l./Ltd/LLC), no punctuation folding ("Acme, Inc." vs "Acme Inc"), no d/b/a splitting. A roster entry "Acme GmbH" will not match "ACME G.m.b.H." in the document. |
| F7 | Performance | P2 | Single 953 kB JS chunk. Six admin panels (~8.3 k lines) and `@aws-amplify/ui-react` ship to every non-admin and to every password-mode deployment where Amplify is never configured. |
| F8 | Stability | P3 | In-flight review is not recoverable across a page reload: `reviewId` is in-memory only. A reload during a 10-minute review loses the panel; the reviewer must find it in History. |
| F9 | Stability | P3 | `entity_roster._ensure_table` issues `create_table` on the request path of every roster read, relying on `ResourceInUseException` to no-op. Under throttling this becomes a 400/ProvisionedThroughput noise source and a needless IAM `dynamodb:CreateTable` grant on the API role. |
| F10 | Security | P3 | CSP carries `style-src 'unsafe-inline'` for Amplify UI. No `Strict-Transport-Security`, `Permissions-Policy`, or `Cross-Origin-Opener-Policy` in the Amplify custom-header block. |
| F11 | Security | Info | Admin boundary is sound: every `/api/admin/*`, `/api/users*`, `/api/audit`, `/api/corpus` route resolves `caller_row` from the DynamoDB users table via `get_active_user_row` and gates on `_is_admin(row)` or a module `_require_admin(row)`. No route trusts a JWT claim. `security-posture.test.tsx` allowlist has exactly four `setItem` sites, all preference keys. |
| F12 | Security | Info | Buckets: BLOCK_ALL public access, `enforceSSL`, per-data-class CMKs, KMS encryption-context conditions on the API role. State-machine CMK exists (`alias/…-state-machine`); pointer-only payload rule is enforced by convention plus `pipeline-execution-history-gate.yml`. WAF: managed Common + KnownBadInputs, 50 MiB body cap, upload 10/5 min. |
| F13 | Accuracy | Info | Redline path is span-level, not word-level: the block transcript resolves model spans onto document characters and `docx-editor` writes `<w:del>`/`<w:ins>` per span. Commentary is delivered as tracked footnotes, never `<w:comment>` parts. The e2e tests cover round-trip and accept-all. This is a deliberate design (issue #621/#626), not a defect. |
| F14 | Accuracy | Info | Model output is validated twice: provider-native `response_format.json_schema` where the model policy declares `structured_outputs`, then `jsonschema.validate` locally in `model_client.py`. Deterministic by construction. |

## Pillar 1 — Stability & resilience

### F1 — WAF polling rule blocks long reviews  (P1)

`infra/lib/nested/waf-stack.ts` rule `RateLimitPollingEndpoint`: `limit: 60`,
`aggregateKeyType: 'IP'`, scope `STARTS_WITH /api/reviews/`. AWS WAF evaluates
rate rules over a trailing 5-minute window.

`frontend/src/ReviewSubmission.tsx`: `POLL_INTERVAL_MS = 3000`. One reviewer
polling one review issues 100 GETs per 5 minutes. `EXECUTION_TIMEOUT` in
`pipeline-stack.ts` is 15 minutes, so a review that is merely slow, not stuck,
runs past the point the IP is blocked. Every subsequent poll 403s, the UI shows
"still checking" with backoff to 30 s, and the download/cancel routes
(`/api/reviews/{id}/output`, `/cancel`) share the same prefix so they are
blocked too. An office behind one NAT reaches the limit with a single user.

`tests/test_infra_waf_rate_227.py` asserts the upload rule is POST-scoped but
does not check the polling limit against the UI interval.

**A1.** Raise the polling limit and make the poll interval adaptive.
Limit to 300/5 min (still 5x headroom against a scripted client: the
application-layer per-user concurrency cap is the real abuse control), and in
`ReviewSubmission.tsx` back the interval off from 3 s to 10 s after the first
two minutes. Add a test that derives the request budget from
`POLL_INTERVAL_MS` and the timeout and fails if it exceeds the WAF limit.

### F3 — Boto clients without explicit timeouts/retry mode  (P2)

`backend/src/model_client.py::BedrockModelClient._get_client` builds
`boto3.client("bedrock-runtime")` with region only. `backend/src/main.py:474`
builds the DynamoDB resource with `config.boto3_client_kwargs` (endpoint and
region only). Defaults: `read_timeout=60`, `connect_timeout=60`,
`retries={"mode": "legacy", "max_attempts": 5}`.

Consequences: a primary pass on `claude-opus` producing a large redline
transcript can exceed 60 s wall-clock on a single response; botocore raises
`ReadTimeoutError` and, in legacy mode, retries the whole `InvokeModel` up to
four more times. Each retry is a billed invocation the ledger never sees as a
retry. For DynamoDB, legacy mode does not use adaptive backoff during
`ProvisionedThroughputExceededException` bursts at cold start.

**A3.** Add a `botocore.config.Config` factory in `config.py`:
Bedrock `read_timeout=300, connect_timeout=10, retries={"mode": "standard",
"max_attempts": 2}`; DynamoDB `retries={"mode": "adaptive", "max_attempts": 5},
connect_timeout=5, read_timeout=15`. Route both call sites through it.

### F4 — Upload integrity and stalled-submit handling  (P2)

`backend/src/review_routes.py::_put_upload_object` calls
`put_object(Bucket, Key, Body=contents)` with no `ChecksumSHA256`. The row is
written after the object, so a failed `put_object` correctly fails closed with
no review row. A corrupted body that S3 accepts is not detected; the pipeline
later reads a different sha256 from the one recorded on the row.

`frontend/src/ReviewSubmission.tsx` submit uses `authorizedFetch` with no
`AbortController`; a stalled upload (captive portal, mid-upload disconnect)
leaves `submitting=true` indefinitely with no way out except reload.

**A4.** Pass `ChecksumSHA256` (base64 of the already-computed `file_sha256`)
on `put_object`; S3 rejects a mismatched body with `BadDigest`, which the
route maps to a 502 with an actionable `detail`. Frontend: wrap the submit in
an `AbortController` with a size-scaled timeout (60 s + 1 s/MB) and surface
"the upload stalled, try again" as a retryable state.

### F8 — In-flight review lost on reload  (P3)

`reviewId` lives only in React state ("never carried across a page load", per
the comment). Recovery today is the History tab. Storing the in-flight
`review_id` (an opaque UUID, not a credential) under a namespaced key would
let the panel resume polling after a reload. This requires adding one
allowlisted `setItem` site to `security-posture.test.tsx`.

**A8.** Persist `ct:inflight-review-id` in `sessionStorage` (tab-scoped, cleared
on close), resume polling on mount if the stored review is still non-terminal,
extend the allowlist with a value-shape test.

### F9 — `create_table` on the request path  (P3)

`backend/src/entity_roster.py::_ensure_table` issues `create_table` on every
table handle. Move creation to a startup check (`startup_checks.py`) for the
Docker target and use `describe_table` once, cached, thereafter.

### Verified sound

- Step Functions: per-stage `timeout` + `addRetry` + shared `addCatch`, 15-minute
  execution timeout, orphan reconciler for hard-killed executions, `StopExecution`
  cancel path that owns the terminal write and settlement and refuses to answer
  202 when the stop fails.
- Poll loop: capped exponential backoff on transient failure; `ErrorBoundary`
  per panel plus a top-level one; 401 handled once via `onSessionExpired`.

## Pillar 2 — Accuracy & fidelity

### F5 — Markup intensity is prose, not contract  (P2)

`ReviewSubmission.tsx::composeGuidance(browning, text)` prepends a sentence to
`toaster_guidance`. `grep browning backend/src scripts` returns nothing: the
backend, the audit row, the ledger, and the output schema have no notion of the
dial. Two reviews at different intensities are indistinguishable in audit, and
the model reads the setting as free text it may weigh differently per run.

**A5.** Add `markup_intensity` as an explicit form field with a closed enum
(`light | medium | heavy`) validated in `review_routes.py`, recorded on the
review row and audit entry, and rendered by `opf_prompt.py` as its own
system block with fixed wording per level. Keep `composeGuidance` sending the
sentence for one release so the DTS image and the API can deploy independently.

### F6 — Party recognition has no legal-suffix normalisation  (P2)

`entity_roster.normalize_entities` and `opf_prompt._recognition_key` both fold
only case and whitespace. The prompt hands the model a flat list of verbatim
names and relies on it to match variants. Observed gaps:

- Suffix variants: `GmbH` / `G.m.b.H.`, `B.V.` / `BV`, `S.r.l.` / `SRL`, `SAS`,
  `S.A.`, `Ltd.` / `Limited`, `Inc.` / `Incorporated`, `LLC` / `L.L.C.`.
- Punctuation: `Acme, Inc.` vs `Acme Inc`.
- `d/b/a` / `dba` / `trading as`: one roster line "Acme Holdings LLC d/b/a Acme
  Legal" is one entry, but the document may use either half alone.
- Diacritics: `Société` vs `Societe`.

**A6.** Add `scripts/entity_normalize.py` with a deterministic
`recognition_variants(name) -> set[str]` (suffix table, punctuation fold,
NFKD-strip, d/b/a split) used in three places: roster dedup, prompt
`our_entities` rendering (emit the canonical name plus its variants so the
model does not have to guess), and a new preflight signal "party not found in
document" when none of the variants appear in the extracted text. Unit tests
on the variant table; no model call involved.

### Verified sound (F13, F14)

Redline: span-level tracked changes via `docx-editor` with owned-XML pure
insertions, footnote bodies tracked, accept-all safety tested, no field codes
allowed in output. Output schema: provider-native plus local `jsonschema`
validation, `structured_output_retry_exhausted` reason token on failure.

## Pillar 3 — Security posture

### F11, F12 — verified

- Authorization: `get_active_user_row` re-reads the users row per request;
  every admin route gates on that row. `/api/me` reports `is_admin` from the
  row. The frontend's `useAdminCapability` is display-only.
- Frontend storage: four `setItem` sites (mute, last playbook, browning, notes
  mode) all allowlisted with value-shape tests; SSO token never persisted;
  password mode uses an httpOnly cookie.
- Infra: BLOCK_ALL, enforceSSL, per-class CMKs with encryption-context
  conditions, state-machine CMK, WAF managed rules and body cap.

### F10 — CSP and hardening headers  (P3)

`frontend-stack.ts` custom headers: CSP with `style-src 'self' 'unsafe-inline'`
(required by Amplify UI's runtime style injection). Missing:
`Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`,
`Permissions-Policy: camera=(), microphone=(), geolocation=()`,
`Cross-Origin-Opener-Policy: same-origin`. Present already: `X-Content-Type-Options`,
`X-Frame-Options`, `Referrer-Policy`, `Cache-Control`.

**A10.** Add the missing headers; keep `unsafe-inline` for styles until
Amplify UI is lazy-loaded (A7), then re-evaluate a nonce.

## Pillar 4 — Performance & efficiency

### F2 — `reviews` table scans on live paths  (P1)

`docs/audit-queries.md` §"No full-table scans, by construction" names `reviews`
and `audit` as unbounded and never to be scanned. Live call sites that scan
`reviews` today:

| Site | Caller | Bound |
|---|---|---|
| `reviews._page_all` | `GET /api/reviews` (admin, `scope=all`) | `Limit` per page, but sorted within page only |
| `reviews._scan_all_reviews` | `GET /api/admin/health` | none |
| `retention.preview_purge` | `POST /api/admin/retention/preview` | none, single page (no `LastEvaluatedKey` loop — silently under-counts past 1 MB) |
| `retention.run_purge_sweep` | scheduler + admin | none, single page (same under-count) |
| `retention.list_legal_holds` | `GET /api/admin/retention/holds` | none, single page |
| `disposition.list_legal_triage_queue` / `_scan_by_owner` | triage queue | none |
| `infra/lambda/orphan_reconciler` | scheduled | filter-scan, acceptable but single page |
| `infra/lambda/purge_worker` | scheduled | paginated |

The `audit` table is clean (audit_queries has `assert_no_scans` tests).
`daily_spend`, `cost_ledger` (row-capped), `playbook_versions` (projected),
`users`, and `review_submissions` fallback scans are on bounded tables and are
acceptable.

**A2.** Add one GSI `status-index` (`status` PK, `created_at` SK) to the
reviews table in `data-stack.ts`. Rewrite: admin listing as a merge of
per-status queries newest-first (or accept `owner_sub-index` over a synthetic
`ALL` partition — rejected: hot partition); health counts and stale sample via
`status-index` queries on PENDING/RUNNING plus `Select=COUNT` per status;
retention preview/sweep/holds via `status-index` on terminal statuses with a
`legal_hold` filter and a full `LastEvaluatedKey` loop; triage via a
`legal_triage_status-index` or a filter on the terminal query. Add a
`tests/test_reviews_no_scan.py` spy in the style of `test_audit_query_api_93.py`.
Fix the missing pagination loops in `retention.py` regardless of the index.

### F7 — Bundle and code splitting  (P2)

Single chunk 953 kB. Contributors: `@aws-amplify/ui-react` + `aws-amplify`
(imported unconditionally in `App.tsx`/`main.tsx`), six admin panels, the
Lit web-component library. Fonts are already latin-only, sounds are 20 kB
and already excluded from inlining.

**A7.** `React.lazy` the six admin panels behind the `isAdmin` gate and
`ReviewHistory` behind its tab; lazy-import the Amplify `Authenticator`
only when `!isPasswordMode()`; add `manualChunks` for `lit`, `react`,
`aws-amplify`. Target: initial chunk under 300 kB min. Keep every panel
mounted-once semantics (App.tsx's "every panel stays MOUNTED" invariant) by
lazy-loading the module, not unmounting the panel.

### Preflight and latency budget

`review_routes.py` preflight: 8 s timeout, no retries, cached per active
playbook version. Model client: 120 s default timeout, bounded jittered
retries, cancel checkpoints. No change proposed beyond A3.

## Owner decisions (2026-09-05)

- Q1: status GSI on the reviews table approved.
- Q2: WAF polling limit 300 / 5 min with adaptive poll interval approved.
- Q3: `markup_intensity` lands as a hard cutover, no dual-send.

## GitHub issues (execution order)

| Step | Issue | Action |
|---|---|---|
| 1 | #690 | A1 WAF polling limit + adaptive poll |
| 2 | #691 | A3 botocore timeouts and retry modes |
| 3 | #692 | A2 status-index GSI and de-scan |
| 4 | #693 | A4 upload checksum + stalled-submit abort |
| 5 | #694 | A5 markup_intensity wire field |
| 6 | #695 | A6 entity normalisation + preflight party signal |
| 7 | #696 | A7 code splitting + bundle budget |
| 8 | #697 | A10 HSTS / Permissions-Policy / COOP |
| 9 | #698 | A8 resume in-flight review after reload |
| 10 | #699 | A9 entity-roster table provisioning |

Tracking epic: see the `audit-2026-09` label.

## Execution order

1. A1 (WAF + adaptive poll) and A3 (boto configs) — small, high impact, infra + backend.
2. A2 (status GSI + de-scan + pagination fixes + no-scan test).
3. A4 (upload checksum + submit abort).
4. A5 (markup intensity as a contract field).
5. A6 (entity variant normalisation + preflight party signal).
6. A7 (code splitting), then A10 (headers).
7. A8, A9.

Each step: fast-tier implementation, deep-tier review, `npm test`,
`scripts/check.sh`, `tests/lint-brand-free.py`, and the 14 px floor untouched.

## Parked questions

- Q1. A2 adds a GSI to the reviews table in both deploy targets (CDK and the
  Docker Compose local DynamoDB bootstrap). Confirm a schema change is
  acceptable now; the alternative keeps scans but bounds them with `Limit`
  and honest pagination, which does not fix the growth problem.
- Q2. A1 raises a WAF limit. Confirm 300/5 min is acceptable, or prefer
  keeping 60 and lengthening the poll interval to 6 s (50 req/5 min, zero
  headroom for History or cancel).
- Q3. A5 changes the wire contract between the SPA and the API. Confirm the
  one-release dual-send is acceptable versus a hard cutover.
