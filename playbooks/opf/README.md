# Vendored OPF schemas

Pinned, vendored copies of the Open Playbook Format (OPF) JSON Schemas from the
playbook-engine repo:

- `playbook.schema-0.2.json` — `$id`
  `https://contract-opf.github.io/playbook-engine/spec/playbook.schema-0.2.json`
- `playbook.schema-0.3.json` — `$id`
  `https://contract-opf.github.io/playbook-engine/spec/playbook.schema-0.3.json`
  (0.3 = 0.2 + the optional top-level `digest` section; `opf_version` const
  `"0.3"`).

## Provenance — which engine commit each copy came from

| Vendored file | playbook-engine commit | Subject |
| --- | --- | --- |
| `playbook.schema-0.2.json` | `970f907` | fix(#203): purge internal codenames from public surfaces |
| `playbook.schema-0.3.json` | `1cc0237` | fix(digest): cap preferred_variations and enforce the 40K-token budget by construction |

Record the commit here on every re-vendor. The pinned sha256 in
`tests/test_opf_schema_sync.py` tells you *that* a copy changed; this table
tells you *from what*, which is what you need to diagnose a drift.

## Re-vendoring

**Never hand-edit these files.** Re-vendor by re-copying byte-for-byte from
`../playbook-engine/spec/playbook.schema-0.X.json` (or the published copy at
that `$id`), then set `UPSTREAM_FROZEN_SHA256` in
`tests/test_opf_schema_sync.py` **from the engine's published frozen sha**
(its `spec/CHANGELOG.md`) — *not* by running `sha256sum` over the file you just
copied — and update the provenance table above.

That gate alarms in three layers, at deliberately different urgencies:

1. **Shape contract** (high urgency, runs in CI via GATE A) — the vendored
   schema must still declare every field this repo consumes, **at the shape we
   consume it**: exact enum membership, closed item unions, declared scalar
   types, arrays that are still arrays and still uncapped. It is bidirectional
   by design. A widening is a break, not a courtesy: this repo does not merely
   need a field to be *there*, it renders it at an assumed shape, and every
   renderer in `scripts/opf_prompt.py` is fail-soft — so a retype, an extra
   union branch, or a `maxItems` cap produces a *shorter prompt*, never an
   error. An added `band` value is the sharpest case: `DIGEST_INTRO` ships the
   model a hardcoded band legend, so a fourth band does not break the render,
   it makes the legend lie about how to weight precedent. Cosmetic churn (a
   reworded `description`, a field we never read) still stays quiet.
2. **Upstream pin** (medium, runs in CI via GATE A) — the vendored bytes must
   equal the sha the **engine published** as that version's frozen sha. Be
   precise about what this buys:
   - It **detects** a hand-edit of our copy; a re-vendor that forgot the pin; a
     re-vendor from the wrong file, wrong version, or a source that does not
     match what the engine published.
   - It **cannot** detect upstream moving *after* we transcribed the number.
     The pin lives in this repo beside the copy, and one procedure updates
     both; nothing re-fetches upstream. A pin and a copy updated together agree
     with each other whether or not they agree with the world.

   So: a self-pin with an upstream-sourced *value*. That is strictly more than
   a self-pin with a self-sourced value — the number has to come from the
   engine's changelog and match — but it is not an upstream-drift detector, and
   it should not be described as one.
3. **Byte parity vs source** (low, local-only chore signal) — catches "source
   moved, re-vendor when convenient". This is the **only** layer that compares
   our copy to anything outside this repo, and therefore the only true
   upstream-drift detector — and CI cannot run it, because CI has no sibling
   checkout. It skips cleanly when the sibling is absent, and fires only if a
   dev happens to have `../playbook-engine` (or `$PLAYBOOK_ENGINE_REPO`) on
   disk. That asymmetry is exactly why layer 1 carries the weight: in CI, the
   shape contract is the only thing standing between an upstream change and a
   silently degraded prompt.

Both copies have drifted from source (0.2 by a lone `description` string; 0.3
twice — gaining digest `n`/`band` in `3993b4f`, then swapping
`preferred_variations`' item type and adding a 40K-token budget in `1cc0237`). The root cause is
upstream and these layers only *detect* it: the engine mutated a published
version in place rather than issuing a new one. Vendoring is only safe when the
source is versioned-immutable.

Ingest validates an uploaded OPF against the schema named by its `opf_version`
(`scripts/opf_load.py`). Content-hash verification uses
`scripts/opf_canonicalize.py` (the OPF canonical-form definition, vendored from
playbook-engine `playbook_engine/canonicalize.py`).

## Sibling-id uniqueness (spec §3.13) is enforced loader-side, not by the schema

OPF spec §3.13 makes sibling-id uniqueness a blocking normative rule for
`evidence.clauses[].id`, `evidence.clause_library[].concept_id`,
`floor.invariants[].id`, and `corpus.documents[].document_id`. Playbook-engine
shipped it 2026-07-29 (engine commit `390a259`) as a fail-closed validator rule
only — `playbook_engine/validator.py::_check_duplicate_ids` — with **no schema
change and no version bump**, so it does not show up in either vendored schema
file above and JSON Schema validation alone cannot catch a violation.
`scripts/opf_load.py::_check_duplicate_sibling_ids` closes that parity gap
loader-side (issue #480): it runs after schema validation and rejects a
document that repeats an id in any of the four sets, raising
`OpfDuplicateIdError`. Do not try to "fix" this by editing the vendored schema
files — they must stay byte-identical to the engine's pins (see "Re-vendoring"
above); this rule is enforced in code, deliberately outside the schema, the
same way the engine enforces it.
