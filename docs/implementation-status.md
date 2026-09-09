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
below; see `tests/test_implementation_status_ledger.py` for the same
assertion running standalone.

## Admin UI and workflow capabilities

| Capability | RUNBOOK reference | Status | Notes |
|---|---|---|---|
| Admin UI (sign-in + panels) | `RUNBOOK.md` (general), `frontend/src/App.tsx:135-147` | **SHIPPED** | Sign-in header (Cognito/Google) plus two real admin panels: `AdminUsers` (issue #92, `frontend/src/AdminUsers.tsx`) and `AdminRetention` (issue #94, `frontend/src/AdminRetention.tsx`), both gated on the caller's `is_admin` flag. Everything below this row that RUNBOOK.md describes as a screen under this UI does not exist yet. |
| Playbook upload / version-history / rollback | `RUNBOOK.md:253-272` | **PLANNED** | No playbook-versioning module exists under `backend/src/` (no `playbook_versions` read/write code, no `/api/playbooks*` endpoint) and no admin UI screen. Today, changing the active playbook is a manual edit + PR + CODEOWNERS review of `playbooks/eiaa-v1.0.0.json` (see README → Contributing), not a self-service admin action. **Demo-critical step "playbook seed" is not backed by a real CLI script; treat RUNBOOK.md:253-272 (and the "Rolling back a bad playbook or prompt" procedure that depends on it) as aspirational until this row moves to STUBBED or SHIPPED.** |
| Release-bundle deactivation / re-activation | `RUNBOOK.md:286-299` | **PLANNED** | No release-bundle activate/deactivate concept exists in `backend/src/main.py` or elsewhere in `backend/src/`. `POST /api/reviews` returning `503 "no active playbook"` on an empty bundle state is not implemented. |
| Admin UI → Corpus → Upload | `RUNBOOK.md:339-346` | **STUBBED** | `backend/src/corpus.py` implements the real ingestion pipeline as importable, tested functions (`run_upload_gauntlet` → `extract_clauses` → `build_clause_record` → `embed_clause_records` → `ingest_to_staging` → `build_manifest`), but nothing wires it to an HTTP endpoint or a CLI entry point, and there is no admin UI screen. **Demo-critical step "corpus upload" is not backed by a real CLI script; treat RUNBOOK.md:339-346 as aspirational until this row moves to SHIPPED.** |
| Audit-log viewer / CSV export | `RUNBOOK.md:349-353` | **PLANNED** | No `/api/audit*` endpoint and no admin UI "Audit" screen exist. Individual modules (`users.py`, `retention.py`) write rows to the `audit` table, and `docs/audit-queries.md` documents the DynamoDB query catalogue for direct table access, but there is no in-app viewer or CSV export. |
| Cost-ledger reconcile action | `RUNBOOK.md:400-411` | **STUBBED** | `reserve_spend` / `settle_spend` in `backend/src/reviews.py` implement the atomic-reservation cost model, and issue #252 adds the read half: `GET /api/admin/spend` (`backend/src/admin_dashboard.py::get_spend_ledger`) serves today's reserved/settled totals against the ceiling plus `outstanding_reservation_usd_cents`, the reserved-but-never-settled remainder this procedure acts on. Issue #653 adds the screen half: the Settings tab (`frontend/src/AdminSettings.tsx`) shows the ceiling actually enforced, what today has committed against it, what is left, and what the next review costs — and `POST /api/admin/spend-cap` makes the ceiling itself an admin-settable, audited stored setting (`backend/src/model_settings.py::set_daily_spend_cap_cents`) instead of a compose-file line. The per-review figures are also served, alone, to any signed-in reviewer by `GET /api/review-cost-estimate` (`backend/src/reviews.py::review_cost_estimate`, an allowlist of two fields) and rendered next to the go button by `frontend/src/ReviewSubmission.tsx`, so #653 Scope item 2's "where a reviewer can see it before submitting" does not depend on being an admin; the cap and the day's spend stay behind the admin gate. Still **STUBBED** overall: there is no full admin "Cost ledger" screen (#91) — the Settings card covers today only, not the trailing window or the per-review outliers — and no reconcile **action** to settle a phantom reservation, which is the procedure this row is about. |
| Manual-review filter / disposition capture | `RUNBOOK.md:615-617` | **SHIPPED** (capture) / **STUBBED** (filter) | Capture is **SHIPPED** (issue #486): `POST /api/reviews/{review_id}/disposition` wires `backend/src/disposition.py`'s `record_disposition` behind a real, owner-scoped, tested route, and both `ReviewSubmission.tsx`'s finished-review panel and `ReviewHistory.tsx`'s Disposition column let a reviewer record and see it end-to-end. The cross-user manual-review-filter half now has its API endpoint (issue #252): `GET /api/admin/manual-review` (`backend/src/admin_dashboard.py::list_manual_review_queue`) serves the admin-only queue of every review in `MANUAL_REVIEW_REQUIRED` / `ERROR_MANUAL_REVIEW_REQUIRED`, filterable by status and by `legal_triage_status`, with the RUNBOOK's 24-hour SLA state per entry. It stays **STUBBED** end-to-end because no admin UI screen binds to it yet (#91), and `mark_triaged` is still unrouted — #252's scope is explicitly read-only. |
| CloudWatch dashboard & alarms | `RUNBOOK.md:601-602` | **STUBBED** | 3 real alarms ship (`contract-toaster-{env}-apprunner-5xx-rate`, `-bedrock-invocation-errors`, `-bedrock-throttles`) plus a dashboard with real App Runner / Bedrock / Step Functions graph widgets — see `infra/lib/nested/observability-stack.ts:362-517`. The remaining ~7 RUNBOOK-listed tiles/alarms (cost-to-date, stale `PENDING`/`RUNNING` reviews, manual-review-state counts, abandoned reservations, purge deleted/skipped counts, audit-archive stream lag, the `contract-toaster-manual-review-stale` alarm) render as placeholder text widgets, per the code comment at `observability-stack.ts:362-517`, pending custom backend metrics (`PutMetricData`) that do not exist yet. |

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
