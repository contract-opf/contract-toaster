# Vendored OPF schemas

Pinned, vendored copies of the Open Playbook Format (OPF) JSON Schema and its
conformance vectors from the playbook-engine repo. This repo reads exactly ONE
format: `opf_version` "0.5" with `digest_version` "4" (issue #128; the owner's
2026-10-06 direction: no backward compatibility, no dispatch by version).

- `playbook.schema-0.5.json` — `$id`
  `https://contract-opf.github.io/playbook-engine/spec/playbook.schema-0.5.json`.
  The verdict-free per-deal precedent record (`evidence.precedent[]`, with
  `opened_with` and an `opening_text` for any distinct opening), digest 4,
  the `vs_standard` label judged part by part against our standard, and the
  hard-rule `manifest`, critic `dossiers` and `provenance_index` sections.
- `conformance-0.5/` — the engine's OPF 0.5 conformance set
  (`manifest.json` + `vectors/*.json`, byte-for-byte from
  `spec/conformance/0.5/`). `tests/test_opf_canonicalize.py` replays every
  vector's `canonical` / `content_hash` / `section_digests`, and
  `tests/test_opf_load.py` loads every vector as the engine emits it.

OPF 0.2 and 0.3 (with digest 2) were deleted by issue #128, not kept beside
0.5. They stay in this repo's git history and in contract-opf/opf. A document
declaring any other `opf_version` or `digest_version` is refused at ingest
with a version error (`scripts/opf_load.py`).

## Provenance — which engine commit each copy came from

| Vendored artifact | playbook-engine commit | Engine pin (`spec/CHANGELOG.md`) |
| --- | --- | --- |
| `playbook.schema-0.5.json` | `b90c647` | `f09c1b7abc08c14b80f7f2b8d572fd5cf26d9491a0566e27311387af960e6657` |
| `conformance-0.5/` (manifest + vectors, concatenated) | `b90c647` | `79cb9f0b4b01b4dc914258ff530a5a5aa22d1b9bf17dd54e4663a16a796fb308` |

The engine announced these pins on contract-opf/playbook-engine#236. Until
this repo vendored it, 0.5 had no consumer and the engine changed it in place;
vendoring is what freezes it. A later format is a new `opf_version` and a new
ticket here, never a re-vendor over 0.5.

Record the commit here on every re-vendor. The pinned sha256 values in
`tests/test_opf_schema_sync.py` tell you *that* a copy changed; this table
tells you *from what*, which is what you need to diagnose a drift.

## Re-vendoring

**Never hand-edit these files.** Re-vendor by re-copying byte-for-byte from
`../playbook-engine/spec/playbook.schema-0.5.json` and
`../playbook-engine/spec/conformance/0.5/{manifest.json,vectors/*.json}`, then
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

Ingest validates an uploaded OPF against `playbook.schema-0.5.json`
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
