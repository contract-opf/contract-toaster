# Vendored OPF schemas

Pinned, vendored copies of the Open Playbook Format (OPF) JSON Schema and its
conformance vectors from the playbook-engine repo. This repo reads exactly ONE
format: `opf_version` "0.6" with `digest_version` "5" (issue #177, which
replaced 0.5 / digest 4 in place; the owner's 2026-10-06 direction on issue
#128 stands: no backward compatibility, no dispatch by version).

- `playbook.schema-0.6.json` — `$id`
  `https://contract-opf.github.io/playbook-engine/spec/playbook.schema-0.6.json`.
  Everything 0.5 carried (the verdict-free per-deal precedent record with
  `opened_with` and an `opening_text` for any distinct opening, the
  `vs_standard` label judged part by part against our standard, the hard-rule
  `manifest`, critic `dossiers` and `provenance_index`), plus 0.6's signing
  dates (`signed_on`), our former forms (`baseline.prior_templates`,
  `prior_standards`, `why_changed`, `opened_with: prior_standard`), verified
  absence (`evidence.absence`) and digest 5 (`first_signed` / `last_signed`,
  `settled`, `standard_history`, `n_signed_without_verified` /
  `n_signed_without_possible`, `verified_absent_deals`).
- `conformance-0.6/` — the engine's OPF 0.6 conformance set (`README.md`,
  `manifest.json` + the 14 `vectors/*.json`, byte-for-byte from
  `spec/conformance/0.6/`). `scripts/opf_load.py` names the directory once
  (`OPF_CONFORMANCE_DIR`, `OPF_CONFORMANCE_VECTORS_DIR`) and every test reads
  it from there. `tests/test_opf_canonicalize.py` replays every vector's
  `canonical` / `content_hash` / `section_digests`, and
  `tests/test_opf_load.py` loads every vector as the engine emits it.

OPF 0.2 and 0.3 (with digest 2) were deleted by issue #128, and OPF 0.5 (with
digest 4) by issue #177, not kept beside 0.6. They stay in this repo's git
history and in contract-opf/opf. A document declaring any other
`opf_version` or `digest_version` is refused at ingest with a version error
that names 0.6 (`scripts/opf_load.py`).

## Provenance — which engine commit each copy came from

| Vendored artifact | playbook-engine commit | Engine pin (`spec/CHANGELOG.md`) |
| --- | --- | --- |
| `playbook.schema-0.6.json` | `fe89bc6` | `b2a925811fd661c5d7c11eaa768444fa63764b479c34fff9a50187c580668bf2` |
| `conformance-0.6/` (manifest + vectors, concatenated) | `fe89bc6` | `c5e3828e7d59c961aca2d633b390b3ea536902e7818efe0dd778a0916cf8be96` |

The engine announced 0.6 on its epic contract-opf/playbook-engine#245. Until
this repo vendored it, 0.6 had no consumer and the engine changed it in place;
vendoring is what freezes it. A later format is a new `opf_version` and a new
ticket here, never a re-vendor over 0.6.

The conformance set's `README.md` is vendored beside it for the reader but is
not part of the engine's concatenated pin (manifest + vectors only), so the
pin and `tests/test_opf_schema_sync.py`'s parity check cover the manifest and
the vectors.

Record the commit here on every re-vendor. The pinned sha256 values in
`tests/test_opf_schema_sync.py` tell you *that* a copy changed; this table
tells you *from what*, which is what you need to diagnose a drift.

## Re-vendoring

**Never hand-edit these files.** Re-vendor by re-copying byte-for-byte from
`../playbook-engine/spec/playbook.schema-0.6.json` and
`../playbook-engine/spec/conformance/0.6/{README.md,manifest.json,vectors/*.json}`, then
set `UPSTREAM_FROZEN_SHA256` and `UPSTREAM_CONFORMANCE_SHA256` in
`tests/test_opf_schema_sync.py` **from the engine's published pins** (its
`spec/CHANGELOG.md`) — *not* by running `sha256sum` over the files you just
copied — and update the provenance table above.

The gold fixtures in `tests/gold-fixtures-opf/` are generated against the same
engine commit (`tests/gold-fixtures-opf/generate_opf_fixture.py` stamps every
derived value with the engine's reference functions), so a re-vendor
regenerates them too.

That gate alarms in three layers, at deliberately different urgencies:

1. **Shape contract** (high urgency, runs in CI via GATE A) — the vendored
   schema must still declare every field this repo consumes, **at the shape we
   consume it**: exact enum membership, exact key sets, closed item unions,
   declared scalar types, arrays that are still arrays and still uncapped. It
   is bidirectional by design. A widening is a break, not a courtesy: this repo
   does not merely need a field to be *there*, it renders it at an assumed
   shape, and every renderer in `scripts/opf_prompt.py` is fail-soft — so a
   retype, an extra union branch, or a `maxItems` cap produces a *shorter
   prompt*, never an error. An added `vs_standard` label or part-map token is
   the sharpest case: `DIGEST_INTRO` ships the model a hardcoded legend for
   them, so a new value does not break the render, it reaches the model
   unexplained. Cosmetic churn (a reworded `description`, a field we never
   read) still stays quiet.
2. **Upstream pin** (medium, runs in CI via GATE A) — the vendored bytes must
   equal the shas the **engine published**. Be precise about what this buys:
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
   disk. That asymmetry is exactly why layer 1 carries the weight, in CI the
   shape contract is the only thing standing between an upstream change and a
   silently degraded prompt.

Ingest validates an uploaded OPF against `playbook.schema-0.6.json`
(`scripts/opf_load.py`). Content-hash verification uses
`scripts/opf_canonicalize.py` (the OPF canonical-form definition, vendored from
playbook-engine `playbook_engine/canonicalize.py`; since 0.5 only `identity`
and `compiler.generated_at` / `run_id` are excluded from `content_hash`).

## Sibling-id uniqueness is enforced loader-side, not by the schema

The engine's validator makes ids unique among siblings in
`evidence.clauses[].id`, `evidence.precedent[].id`, `floor.invariants[].id` and
`corpus.documents[].document_id` (OPF spec §3.13 named the clause, invariant
and document sets in 0.2/0.3; 0.4 added precedent ids and retired 0.3's
`evidence.clause_library`). It does so as fail-closed validator rules only
(`playbook_engine/validator.py::_check_duplicate_ids` and `_check_precedent`),
with no schema construct — JSON Schema cannot say "unique across siblings" —
so schema validation alone cannot catch a violation.
`scripts/opf_load.py::_check_duplicate_sibling_ids` closes that parity gap
loader-side (issue #480, extended to precedent ids by issue #128): it runs
after schema validation and rejects a document that repeats an id in any of
the four sets, raising `OpfDuplicateIdError`. Do not try to "fix" this by
editing the vendored schema — it must stay byte-identical to the engine's pin
(see "Re-vendoring" above); this rule is enforced in code, deliberately outside
the schema, the same way the engine enforces it.
