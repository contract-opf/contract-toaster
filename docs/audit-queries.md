# Audit query catalogue

Architecture lives in [ARCHITECTURE.md](../ARCHITECTURE.md) (Audit posture, Storage). This is the
standard catalogue of audit/review queries operators run for investigations, rollback/quarantine, and
compliance. The `audit` table is **append-only** (deny `UpdateItem`/`DeleteItem` to all app roles) and
mirrored to the object-locked `audit-archive` S3 bucket; these queries are read-only.

## Key shapes (recap)

- **`audit`** — PK `partition` (`YYYY-MM`, or `target_type#target_id` for entity-scoped history), SK
  `timestamp#event_id`. GSIs on **`actor`** and **`review_id`**.
- **`reviews`** — PK `review_id`; GSI on `owner_sub` ("my reviews"); component-hash fields
  (`playbook_hash`, `prompt_hash`, `standard_form_hash`, `model_policy_hash`, `corpus_snapshot_version`)
  for rollback/quarantine population queries.

## Standard queries

| Need | Query |
|------|-------|
| What happened this month, in order | `audit` by PK `YYYY-MM`, SK range on `timestamp` |
| Full history of one review/document | `audit` by PK `review#<review_id>` (or via the `review_id` GSI) |
| Everything a given user did | `audit` `actor` GSI, optionally with a `timestamp` range |
| Who viewed/downloaded a document (and who was denied) | `audit` by `review_id`, filter `action in {view, download, presign, access_denied}` |
| **Which clauses informed review X** — exact retrieved set (for corpus-poisoning investigation) | `audit` by `review_id` GSI, filter `action = review_complete`, project `retrieved_clause_ids` (each entry carries `clause_id`, `polarity`, `channel`); snapshot manifest gives the candidate pool, this field gives the retrieved set |
| **Rollback/quarantine population** — every review run under a bad bundle | `reviews` by the relevant component hash (`playbook_hash` / `prompt_hash` / `standard_form_hash` / `model_policy_hash` / `corpus_snapshot_version`); these become `QUARANTINED`, re-runs `SUPERSEDED` |
| Every `REQUEST_CHANGE` under a given playbook version | `reviews` filtered on `playbook_version` + `decision = REQUEST_CHANGE` |
| Release-bundle activations / rollbacks | `audit` filter `action in {bundle_activate, bundle_rollback}` |
| Break-glass / governance-bypass uses | `audit` filter `reason = emergency-override` or `action = governance_bypass` (also alarmed) |
| Denied audit-table mutation attempts | CloudWatch alarm + CloudTrail (management events); cross-check against the `audit-archive` copy |
| Model recertification record | `audit` filter `action = model_recertification` (recorded even when "no change; pin reaffirmed") |

## Substance retention boundary

Every query above is either **always** answerable (it reads only the `audit` table's
non-substantive facts, which are never purged) or **only inside a review's retention window**
(it reads document substance, which is purged on a schedule). Know which is which before an
investigation runs into a dead end:

**Answerable only inside the window** — gone once the review's own `retention_window_at_creation`
elapses and the purge worker sweeps it (see [docs/data-handling.md](data-handling.md) → "Document
retention and purge safety"):
- *Which textual changes were accepted, and why* — `summary` (the model's own output key for it is
  `verdict_summary`) and `toaster_guidance` on the `reviews` row are cleared by the purge worker
  (purge invariant 4). For an `ACCEPT`, this is the **only** record of the reasoning; there is no
  separate output document to fall back on.
- The uploaded document, the generated redline, and the standalone `analysis_report` — the
  underlying `uploads`/`outputs`/`analysis_report` S3 objects are deleted outright (unversioned,
  so the delete is immediate and real).
- The exact retrieved-clause **text** — the `retrieved_clause_ids` audit field (see "Which clauses
  informed review X" above) survives purge, but it is opaque identifiers only; resolving an ID back
  to clause text requires the corpus snapshot still holding that clause, which is a separate
  retention concern from the review's own window.

**Answerable forever** (the `audit` table and the `reviews` row's non-substantive fields are never
purged): that a review ran, under which release-bundle/playbook/prompt/standard-form/model-policy
hash and corpus snapshot version, its `decision` and `attorney_disposition`, actor, timestamps,
cost, token counts, scanner rule IDs, and the retrieved-clause-**ID** set (not the text).

**What this means for an investigation.** Past the window, re-running any of the rollback/quarantine
or "every REQUEST_CHANGE under a playbook version" queries above still works — they only ever
needed the non-substantive fields. But "why did we accept this contract in March" is answerable
past the window only as "the same playbook/prompt/corpus hashes that were active in March would
recur if re-run today" (a hash-comparison proof), never as a recovery of the attorney's actual
reasoning or the accepted text. If a class of review needs the reasoning itself preserved
indefinitely — e.g. executed agreements a GC wants answerable years later — the retention window for
those reviews can be set to **forever / indefinite preservation** rather than relying on the
90-day default (see [docs/data-handling.md](data-handling.md) → "Document retention and purge
safety"); that keeps the reasoning inside the "answerable" boundary permanently instead of trying
to reconstruct it after the fact.

## The query API — `GET /api/audit`

Every catalogue row above is served by the admin-only `GET /api/audit` endpoint
(`backend/src/audit_queries.py`, issue #253). `GET /api/audit` with no `query` returns the
catalogue index — the table below, machine-readable — so the audit explorer UI builds its
picker from the backend rather than from a second copy of this file.

| Query name | Catalogue row | Parameters | How it reads |
|---|---|---|---|
| `month_activity` | What happened this month | `month`, `since`, `until`, `limit` | `audit` Query on PK `YYYY-MM`, SK range |
| `review_history` | Full history of one review | `review_id`, `limit` | `audit` Query on `review_id-index` |
| `actor_activity` | Everything a given user did | `actor`, `since`, `until`, `limit` | `audit` Query on `actor-index`, SK range |
| `document_access` | Who viewed/downloaded (and who was denied) | `review_id`, `limit` | `audit` Query on `review_id-index`, action filter |
| `review_clause_ids` | Which clauses informed review X | `review_id`, `limit` | `audit` Query on `review_id-index`, `action = review_complete` |
| `rollback_population` | Rollback/quarantine population | `component`, `value`, `limit` | `reviews` Query on `playbook_hash-index` + BatchGetItem |
| `playbook_request_changes` | Every `REQUEST_CHANGE` under a playbook version | `playbook_id`, `playbook_version`, `limit` | `playbook_versions` GetItem → `reviews` Query on `playbook_hash-index`, capped at the newest `AUDIT_QUERY_MAX_LIMIT` reviews, then filtered on `decision`; reports `reviews_examined` / `population_truncated` |
| `release_activity` | Release-bundle activations / rollbacks | `limit` | `audit` Query per month partition, action filter |
| `break_glass` | Break-glass / governance-bypass uses | `limit` | `audit` Query per month partition, action/reason filter |
| `denied_audit_mutations` | Denied audit-table mutation attempts | — | **Not answerable via the API** — returns the CloudWatch/CloudTrail pointer |
| `model_recertification` | Model recertification record | `limit` | `audit` Query per month partition, action filter |

**No full-table scans, by construction.** The endpoint never calls `Scan` on `audit` or
`reviews` — both grow without bound, so a scan-backed explorer works in a demo and times
out in the investigation it was built for. The action-filter entries walk the trailing 12
month partitions (a Query each) and report `months_searched`, so an empty result reads as
"nothing in the last year", never as "nothing, ever".

`playbook_request_changes` filters on `decision`, which no index covers, so it reads the
newest `AUDIT_QUERY_MAX_LIMIT` reviews under the version — the same cap the catalogue index
reports as `max_limit` — and filters those. Because the filter shrinks the answer, a small
`count` there is not evidence the version is clean, so the payload carries
`reviews_examined` (the pre-filter population) and `population_truncated`, set when the cap
was reached and older `REQUEST_CHANGE`s therefore lie outside the window.

**Where the catalogue currently outruns the pipeline.** These are reported in the response
payload rather than served as a bare empty list, because an empty list reads as an
all-clear:

- `review_clause_ids` returns `dormant: true` and `producers_wired: false`. Retrieval was
  retired by owner decision 2026-08-11 ([rag-dormant.md](rag-dormant.md)); no review run
  under the current pipeline records clause IDs, and no writer in this repo's history ever
  recorded them (#27 landed as a docs-only CI gate), so there is no pre-retirement
  population either. The entry stands as the standing filter should retrieval be revived.
- `document_access` returns `denials_recorded: false`. The download routes audit only a
  *successful* presigned-URL issuance, so no `access_denied` row exists yet.
- `break_glass` and `model_recertification` return `producers_wired: false`. No
  application writer emits those actions today; both are standing filters.
- `rollback_population` accepts `component=playbook_hash` (equivalently
  `playbook_content_hash`) only. `prompt_hash`, `standard_form_hash`, `model_policy_hash`
  and `corpus_snapshot_version` are **release-bundle** fields (`scripts/bind_bundle.py`),
  never written onto a `reviews` row and carrying no GSI, so the endpoint refuses them with
  400 naming the missing index rather than scanning. `standard_form_hash` is historical-only
  regardless: #631 removes the standard-form-diff subsystem while preserving the bundle
  schema fields, so no new record will carry a meaningful value. Run those ad hoc against
  the `audit-archive` trail (see [RUNBOOK.md](../RUNBOOK.md) → Observability).

## Notes

- These queries return **non-substantive** facts only — actor, action, target, decision, hashes, model
  IDs/region, token counts, cost, scanner rule IDs, authorization result. Audit rows never contain raw
  clause text, model rationales, or substantive deltas (see [docs/data-handling.md](data-handling.md)).
- The admin UI → Audit exposes the common filters (user, action, playbook version, date range, CSV
  export). Deeper/ad-hoc queries run directly against DynamoDB or via Athena over the `audit-archive`
  trail; per-object S3 data events are off by default and enabled temporarily only for an investigation
  (see [RUNBOOK.md](../RUNBOOK.md) → Observability).
