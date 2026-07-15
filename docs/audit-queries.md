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
- *Which textual changes were accepted, and why* — `verdict_summary`, `issue_rationale_text` on
  the `reviews` row are cleared by the purge worker (purge invariant 4). For an `ACCEPT`, this is
  the **only** record of the reasoning; there is no separate output document to fall back on.
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

## Notes

- These queries return **non-substantive** facts only — actor, action, target, decision, hashes, model
  IDs/region, token counts, cost, scanner rule IDs, authorization result. Audit rows never contain raw
  clause text, model rationales, or substantive deltas (see [docs/data-handling.md](data-handling.md)).
- The admin UI → Audit exposes the common filters (user, action, playbook version, date range, CSV
  export). Deeper/ad-hoc queries run directly against DynamoDB or via Athena over the `audit-archive`
  trail; per-object S3 data events are off by default and enabled temporarily only for an investigation
  (see [RUNBOOK.md](../RUNBOOK.md) → Observability).
