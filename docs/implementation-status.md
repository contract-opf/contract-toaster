# Implementation status ledger

_Issue #230 (part of the 2026-07 dual-repo audit, #185): RUNBOOK.md and
ARCHITECTURE.md are written "docs as spec" — deliberately describing the
target design, some of it ahead of the code. That is a reasonable style for
a solo team, but it means the docs alone cannot tell an operator (or an
open-source adopter) what actually works today. This table is the
correction: it enumerates every admin-UI capability and observability
surface RUNBOOK.md describes and states, plainly, whether it ships._

**Legend**

| Status | Meaning |
|--------|---------|
| **SHIPPED** | The described UI/API/behavior exists, is reachable end-to-end, and is exercised by tests. |
| **STUBBED** | The underlying logic exists (importable functions, tested) but is not reachable end-to-end — no HTTP endpoint, no admin UI screen, or both. |
| **PLANNED** | Nothing beyond the RUNBOOK prose exists yet. No backend module, no endpoint, no UI, no CLI script. |

Regenerating this table by hand is expected to drift, so `scripts/docs-lint.py`
Check G enforces that it stays present and keeps covering the capabilities
below, each cited by a RUNBOOK heading anchor that must exist (QA 2026-10-31
replaced the old `RUNBOOK.md:NNN` line ranges, which had all gone stale); see
`tests/test_implementation_status_ledger.py` for the same assertion running
standalone. Check H checks that every link here resolves.

## Admin UI and workflow capabilities

| Capability | RUNBOOK reference | Status | Notes |
|---|---|---|---|
| Admin UI (sign-in + panels) | `RUNBOOK.md` (general), `frontend/src/App.tsx` (`TAB_DEFS`) | **SHIPPED** | Sign-in (Cognito/Google on AWS, username/password on Docker Compose) plus the admin-only tabs Users, Retention, Models, Playbooks, Settings and Diagnostics beside the Review and History tabs every signed-in user gets. Each admin panel hides itself on a `403`; the server is the authority (see the auth column of [ARCHITECTURE.md → Routes](../ARCHITECTURE.md#routes)). There is no Corpus, Audit or Cost-ledger screen (rows below). |
| Playbook upload / version-history / rollback | [`RUNBOOK.md#adding-a-new-playbook-version`](../RUNBOOK.md#adding-a-new-playbook-version), [`RUNBOOK.md#rolling-back-a-bad-playbook-or-prompt`](../RUNBOOK.md#rolling-back-a-bad-playbook-or-prompt) | **SHIPPED** (lifecycle) / **PLANNED** (quarantine on rollback) | The whole lifecycle is reachable from the **Playbooks** tab (`frontend/src/AdminPlaybooks.tsx`) through `/api/admin/playbooks*` (`backend/src/main.py`, logic in `backend/src/playbook_versions.py`): create a playbook, upload a version (parsed, validated, hash computed server-side, stored `draft`), list the version trail, record legal approval, activate (Gate 7: `content_hash == legal_approval.content_hash`), roll back to a `retired` version, download, edit notes, rename and remove. Activate and rollback write `playbooks.active_release_bundle_hash`, which the review resolver serves; each writes an audit row. Tests: `tests/test_playbook_version_routes_430.py`, `tests/test_bundle_activate_rollback_79.py`, `tests/test_playbook_rollback_resolver_462.py`, `tests/test_playbook_version_download.py`. **Demo-critical "playbook seed":** on the Docker Compose target `deploy/dts/bootstrap.py` installs and activates the shipped `synthetic-nda-sample` through the same functions on first boot; on AWS an admin uploads and activates it. **PLANNED:** automatic quarantine of reviews run under a rolled-back bundle, the continuous quarantine sweep and `SUPERSEDED` re-runs (`backend/src/playbook_versions.py`, "Explicitly deferred"). **Known gap:** an uploaded version runs only when it is an OPF document; a v1-format upload is recorded and hashed but the runner loads the on-disk registry file instead (`pipeline_runner._load_playbook_bundle`; QA 2026-10-31 report item 13, open). |
| Release-bundle deactivation / re-activation | [`RUNBOOK.md#suspending-intake-deactivate-without-a-successor`](../RUNBOOK.md#suspending-intake-deactivate-without-a-successor) | **PLANNED** (deactivate) / **SHIPPED** (`503` guard) | No deactivate or re-activate route or button exists (`playbook_versions.py` defers it). The guard it would lead to does ship: `POST /api/reviews` refuses with `503 "no active playbook"` when the requested playbook has no active version (`backend/src/reviews.py::NO_ACTIVE_PLAYBOOK_DETAIL`, `tests/test_no_active_bundle.py`) — before first activation, or after an admin removes the playbook. The RUNBOOK section lists the controls that exist meanwhile (rollback, spend ceiling, suspending users, removal). |
| Admin UI → Corpus → Upload | [`RUNBOOK.md#adding-a-document-to-the-corpus`](../RUNBOOK.md#adding-a-document-to-the-corpus) | **STUBBED** | `POST /api/corpus` (admin, `backend/src/main.py::post_corpus`, `tests/test_corpus_ingestion_endpoint_197.py`) runs the real ingestion pipeline in `backend/src/corpus.py` over caller-supplied paragraphs and returns a draft staging snapshot. There is no admin UI screen, no `.docx` upload path for it, and no snapshot activation; retrieval itself is dormant by decision ([rag-dormant.md](rag-dormant.md)), so no review reads a corpus. **Demo-critical "corpus upload"** stays STUBBED until a screen or CLI drives it and something consumes the result. |
| Audit-log viewer / CSV export | [`RUNBOOK.md#viewing-the-audit-log`](../RUNBOOK.md#viewing-the-audit-log) | **SHIPPED** (API) / **PLANNED** (screen, CSV) | `GET /api/audit` (admin, `backend/src/main.py::get_audit`, `backend/src/audit_queries.py`, `tests/test_audit_query_api_93.py`) serves the [audit-queries.md](audit-queries.md) catalogue as a query API: the index with no `query`, an investigation with one, key/index access only, `limit` clamped. No admin UI screen and no CSV export exist. |
| Cost-ledger reconcile action | [`RUNBOOK.md#adjusting-the-daily-spend-ceiling`](../RUNBOOK.md#adjusting-the-daily-spend-ceiling) | **STUBBED** | `reserve_spend` / `settle_spend` in `backend/src/reviews.py` implement the atomic-reservation cost model. `GET /api/admin/spend` (`backend/src/admin_dashboard.py::get_spend_ledger`) serves today's reserved/settled totals against the ceiling and `outstanding_reservation_usd_cents`; the Settings tab (`frontend/src/AdminSettings.tsx`, `SpendCard.tsx`) shows them and sets the ceiling through the audited `POST /api/admin/spend-cap`. Any signed-in reviewer sees the next review's price from `GET /api/review-cost-estimate`. Still **STUBBED**: there is no Cost-ledger screen (trailing window, outliers) and no reconcile **action** to settle a phantom reservation, which is the procedure this row is about. |
| Manual-review filter / disposition capture | [`RUNBOOK.md#failures-queue-owner-and-sla`](../RUNBOOK.md#failures-queue-owner-and-sla), [`RUNBOOK.md#a-wrong-decision-was-rendered`](../RUNBOOK.md#a-wrong-decision-was-rendered) | **SHIPPED** (capture) / **STUBBED** (filter) | Capture: `POST /api/reviews/{review_id}/disposition` (owner or admin, `backend/src/disposition.py::record_disposition`, `tests/test_disposition_routes_486.py`), recorded from `ReviewSubmission.tsx` and shown in `ReviewHistory.tsx`. Filter: `GET /api/admin/manual-review` (`admin_dashboard.list_manual_review_queue`) serves the admin failures queue (every `ERROR` review, by `reason` and `legal_triage_status`, with SLA state), but no screen binds to it (the Diagnostics tab reads `GET /api/admin/diagnostics/recent-failures` instead) and `disposition.mark_triaged` has no route. |
| CloudWatch dashboard & alarms | [`RUNBOOK.md#observability`](../RUNBOOK.md#observability) | **STUBBED** | Three alarms ship — `contract-toaster-{env}-apprunner-5xx-rate`, `-bedrock-invocation-errors`, `-bedrock-throttles` — publishing to the SNS topic `contract-toaster-alarms-{env}`, plus the dashboard `contract-toaster-{env}` with real App Runner / Bedrock / Step Functions graphs (`infra/lib/nested/observability-stack.ts`). The other RUNBOOK tiles and alarms (cost-to-date, stale `PENDING`/`RUNNING`, failures-queue counts, abandoned reservations, purge counts, audit-archive lag, `contract-toaster-manual-review-stale`) are placeholder text widgets pending custom metrics that do not exist yet. AWS target only; the Docker Compose target's signal is the autopull heartbeat ([`RUNBOOK.md#coolify-deployments-and-autopull-docker-compose-target`](../RUNBOOK.md#coolify-deployments-and-autopull-docker-compose-target)). |

## Reading this table

- A row moves from **PLANNED** to **STUBBED** when the underlying logic lands
  (even without a UI/endpoint) — the corpus-ingestion and disposition-capture
  rows above are the pattern to follow.
- A row moves from **STUBBED** to **SHIPPED** when it is reachable end-to-end
  (HTTP endpoint or CLI script, or an admin UI screen) and covered by a test
  that exercises the reachable path, not just the underlying function.
- Treat every **PLANNED** row's RUNBOOK section as a *design spec*, not an
  operating procedure — do not attempt to follow it literally against a real
  deployment.
