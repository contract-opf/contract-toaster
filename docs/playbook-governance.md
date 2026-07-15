# Playbook governance

Architecture lives in [ARCHITECTURE.md](../ARCHITECTURE.md). This document is the authoritative
home for **how a playbook is validated and how a legal-behavior release bundle is activated**.
The playbook JSON Schema is [playbooks/schema.json](../playbooks/schema.json); the seed playbook is
[playbooks/eiaa-v1.0.0.json](../playbooks/eiaa-v1.0.0.json). The evaluation gates that a bundle must
clear are owned by [docs/evaluation.md](evaluation.md); this document owns the **structural** rules
(schema, uniqueness, coverage, detector grammar) and the **lifecycle** (`draft → active → retired`).

A playbook is a production control surface for legal output. A CODEOWNERS rule alone does not make
it safe to activate — the rules below are machine-checked in CI, and a bundle that fails any of them
cannot leave `draft`.

## Structural validation (CI)

Run on every change to `playbooks/`, the prompt, the canonical standard form, the model policy, or a
corpus snapshot:

1. **Schema validity.** The playbook validates against `playbooks/schema.json`. The schema enforces
   `additionalProperties: false` on `hard_rejections.items`, `topics.items`, `topics[].replacement_text`,
   and `protects` — a typo such as `exemptTerms` (instead of `exempt_terms`) is a validation error,
   not a silent no-op. This prevents the C1 bug class where a camelCase typo drops a guard.
2. **Unique keys.** Topic `id` values are unique; topic `section_ref` values are unique **including
   placeholder/absent-section topics** (JSON-Schema `uniqueItems` only de-dupes whole objects, so this
   is a per-key assertion). This is the gate the original seed failed (two topics shared
   `[not in Exos standard]`); absent-section topics now carry distinct refs like `[absent] Insurance`.
3. **Coverage.** Every section **present** in the canonical standard form maps to **exactly one** topic.
   Topics describing a position on a clause **not** in the standard form set `not_in_standard: true`,
   carry `section_anchors: ["sec-_new"]` (the reserved pseudo-anchor — see §4 below), and are exempt
   from this form-coverage check.
4. **Anchor resolution.** Every `section_anchors[]` entry on a topic (other than the reserved
   pseudo-anchor `sec-_new`) resolves to a real section in the bundle's standard-form section-anchor
   map (derived from the standard-form `.docx` at bundle-build time). The §10 Miscellaneous heading
   resolves to sub-clause anchors (`sec-10-notices`, `sec-10-non-exclusive`, `sec-10-merger`,
   `sec-10-precedence`) so the four §10 topics don't collide. The pseudo-anchor `sec-_new` is exempt
   from this resolution check because it has no corresponding standard-form section.
   **`not_in_standard` / `section_anchors` implication (schema-encoded):**
   - A present-section topic (`not_in_standard` absent or `false`) must have `section_anchors` with
     `minItems: 1` — an empty anchor list makes any detector rule scoped to that topic dead config.
   - A `not_in_standard: true` topic must have `section_anchors` equal to exactly `["sec-_new"]`
     (`maxItems: 1`, `items const: "sec-_new"`) — carrying a real section anchor on a not-in-standard
     topic violates the contract. Both implications are encoded as `if/then` in `topics.items.allOf`
     and enforced by `tests/test_schema_hardening.py`.

   **`sec-_new` pseudo-anchor.** `sec-_new` is a reserved pseudo-anchor whose semantics are defined
   in [playbooks/schema.json](../playbooks/schema.json) (`sec-_new_anchor`) and
   [ARCHITECTURE.md → Section-anchor map](../ARCHITECTURE.md#section-anchor-map-deterministic-detector-scoping).
   In brief: the diff tagger assigns `sec-_new` to any inserted hunk that does not fall inside any
   existing standard-form section. Every `not_in_standard: true` topic MUST carry
   `section_anchors: ["sec-_new"]` — CI fails any `not_in_standard` topic with empty `section_anchors`
   (that would make any `on_insert` rule referencing that topic dead config, unable to fire on any hunk).
   `on_remove_or_alter` rules must not reference `sec-_new`.

## Coverage gate

The **form-coverage gate** runs in CI (`tests/anchor/test_form_coverage.py`) on every change to
`standard-forms/` or `playbooks/`:

- Reads the active anchor map from `standard-forms/eiaa-v<version>.anchor-map.json`.
- For every anchor in the map (excluding pseudo-anchors like `sec-_new`), verifies that exactly
  one playbook topic lists that anchor in its `section_anchors[]`.
- Topics with `not_in_standard: true` are **exempt** from this check (they carry `sec-_new`).
- Any form anchor not covered by a topic and not listed in `coverage_exempt_anchors` is a **CI
  failure**.  Exemptions must be explicit, reviewed decisions recorded in the **anchor map**
  (`standard-forms/eiaa-v<version>.anchor-map.json`), as siblings of `anchors`, each with a
  rationale in `coverage_exempt_rationales`.  `coverage_exempt_anchors` is **canonical in the
  anchor map, not the playbook** — an exemption is a reviewed property of the standard-form section
  it describes, so it is governed alongside the form.  Common exempt anchors: preamble and
  signature-block sections that carry no reviewable legal clause.

The gate prevents silent coverage regressions: if a standard-form revision adds a new section and
no one adds a topic (or exemption), CI fails before merge.

## Anchor resolution gate

The **anchor resolution gate** verifies that every `section_anchors[]` entry in every non-`sec-_new`
topic resolves to a real section in the bundled `standard-forms/*.anchor-map.json`.  A topic that
references `sec-8` but no such anchor exists in the current form is dead config that **fails CI**.

## Heading-hash drift gate

The **heading-hash drift gate** runs in CI (`tests/anchor/test_heading_hash_drift.py`) on every
change to `standard-forms/` or `playbooks/`:

- Compares the heading hashes in the current anchor map against those in a renumbered-form
  regression fixture (which simulates a standard-form revision that changes §8/§9 heading text).
- If any anchor's `heading_hash` has changed and no `anchor_migrations` record in the playbook
  covers it, the gate **fails with DRIFT WITHOUT MIGRATION**.
- If every drifted anchor has a covering migration record, the gate passes.

**Normative rule**: An anchor whose heading hash changes without a covering migration record **fails
the drift gate**.  Authors must add an `anchor_migrations` entry in the playbook for every drifted
anchor before CI can pass.  See RUNBOOK.md "Revising the standard form" for the full procedure.

The gate ensures that a standard-form revision that silently changes a heading (e.g., renumbering
§8 to §9) cannot reach production without an explicit, GC-approved migration record.

## Acceptable-variations lint (CI)

Every `acceptable_variations[].if/to` text in every topic is rendered through the full `on_insert`
detector pass as a simulated inserted hunk in the topic's anchored section. **Required result: zero
hard-rejection fires.** Implemented in `tests/lint-acceptable-variations.py`, run on every change to
`playbooks/` by `.github/workflows/playbook-lint.yml`.

This gate encodes the invariant that the playbook cannot contradict itself: a documented acceptable
variation must never be blocked by a monotonic hard-rejection rule. The original bug (issue #2) was
that `indemnification.acceptable_variations` accepted "narrow mutual IP indemnification, capped by
Section 8" while `no-exos-indemnity` had `"indemnification"` as a bare trigger term — and similarly
`insurance.acceptable_variations` accepted "additional insured…limited to vicarious liability" while
`no-excess-insurance-levels` listed bare `"additional insured"` as a trigger.

**Authoring rule:** when adding or editing an `acceptable_variations` entry, run
`python3 tests/lint-acceptable-variations.py` locally before committing. If a new acceptable
variation fires an existing `on_insert` rule, choose one lever (GC judgment required):
1. **Narrow the trigger** — remove the broad term and replace with more specific patterns or use
   `exempt_terms` to carve out the acceptable phrasing.
2. **Add `exempt_terms`** — the exempt phrase must contain at least one of the rule's `trigger_terms`;
   a dead exemption warns/fails CI (see `§ Hard-rejection detector grammar` below).
3. **Demote to `reject_if_proposed`** — if the rule is better served by LLM judgment than a lexical
   detector, remove the hard-rejection rule and move the prohibition to the topic's `reject_if_proposed`
   list. Example: one-way confidentiality is handled as the entry "One-way confidentiality favoring
   the institution." in `topics[id=confidentiality].reject_if_proposed[]` rather than as a named
   `hard_rejections` rule, because lexical matching cannot reliably distinguish one-way from mutual
   confidentiality scope.

**Issue #213 extends this gate to `on_remove_or_alter` rules.** The `on_insert` pass above only ever
covered additive prohibitions; a `protects.required_tokens` Floor rule (e.g. `preserve-liability-cap`)
could contradict a documented acceptable variation with zero CI visibility — the exact bug class of
issue #2, recurring one layer down. Concretely: `limitation-of-liability.acceptable_variations`
documents a mutual cap raise to "up to $500,000", but `preserve-liability-cap` fires whenever the
required_token `'$150,000'` is deleted or altered — which raising the cap necessarily does. The lint
now also renders every `acceptable_variations[].to` text (only `to` — `if` describes the counterparty's
proposal, never text we'd put in the contract, so it is not a meaningful stand-in for the accepted/
remaining clause an `on_remove_or_alter` rule reads) through
`scripts/detector_common.check_on_remove_or_alter_rule_fires` and asserts zero fires.

For `on_remove_or_alter` fires, levers 1–2 above do not apply (`on_remove_or_alter` rules carry no
`trigger_terms`/`exempt_terms` — schema.json forbids them for this `kind`). Two levers apply instead
(GC judgment required):
1. **`requires_attorney_override: true`** (new optional field on `acceptable_variations[]` items,
   `additionalProperties: false` still enforced) — use when implementing the variation is a genuine,
   unavoidable consequence of the Floor's protected token (e.g. a cap raise necessarily changes the
   dollar figure `preserve-liability-cap` protects). `schema.json`'s `hard_rejections[].protects`
   description is explicit that numeric-threshold and similar judgment calls are deliberately **not**
   decided by this detector — that stays with attorney review. Marking a variation this way does
   **not** weaken the Floor: the deterministic layer still fires and still forces `REQUEST_CHANGE`
   exactly as it would for any other protected-token change; the acceptance path is the attorney's
   disposition of that `REQUEST_CHANGE` (`backend/src/disposition.py` — `attorney_disposition` is
   always recorded outside the tool), never a silent model auto-accept. The lint fails if a marked
   variation does *not* actually fire anything (stale-marker check), so this field cannot silently
   rot into blanket cover for an unrelated future contradiction.
2. **Demote to `reject_if_proposed`** — same lever as #3 above, with the same caveat: only remove an
   `on_remove_or_alter` rule if a *different* deterministic rule (or the LLM layer) still covers the
   risk it protected against. Do not choose this lever if it would leave a Floor-level protection with
   no deterministic backstop at all.

## Hard-rejection detector grammar (CI)

Detectors run over the **standard-form diff**, never raw full-document text (see
[ARCHITECTURE.md → Retrieval](../ARCHITECTURE.md#semantic-retrieval-plus-a-deterministic-lexical-layer)).
Every `hard_rejections[]` rule declares a `kind`, and CI enforces the per-kind contract:

| `kind` | Fires when | Required fields | Forbidden fields |
|--------|-----------|-----------------|------------------|
| `on_insert` | a `trigger_terms` phrase appears in an **inserted / modified-new** diff span, in scope of `applies_to_topics` | `trigger_terms` | `protects` |
| `on_remove_or_alter` | a `protects.required_token` present in the **standard** side of the anchored section is **deleted or altered** on the counterparty side | `protects` (`section_anchor`, `required_tokens`) | `trigger_terms` |

Additional CI rules:

- **`not_in_standard` topics may be referenced only by `on_insert` rules** — you cannot remove a clause
  the standard form never contained.
- **`not_in_standard` topics must carry `sec-_new`.** Every `not_in_standard: true` topic must have
  `section_anchors: ["sec-_new"]`. A `not_in_standard` topic with empty `section_anchors` makes any
  `on_insert` rule referencing it dead config (it would have an empty effective hunk-scope and could
  never fire). **CI fails the build** if any `not_in_standard` topic has empty `section_anchors`.
- **No rule has empty effective scope.** For every `hard_rejections[]` entry that has
  `applies_to_topics` defined, the union of `section_anchors` over all referenced topics must be
  non-empty (where `sec-_new` counts as non-empty for `on_insert` rules referencing `not_in_standard`
  topics). A rule with an empty effective scope is dead config that can never fire, and **CI fails the
  build**. This gate is implemented in `tests/detector/test_empty_scope_gate.py`.
- **Protective rules must guard tokens that exist.** Every `on_remove_or_alter.required_tokens` entry
  must be **present in its anchored section of the canonical standard form**. A protective rule guarding
  an absent token is dead config and **fails the build** — this is the structural check that prevents the
  inverted-semantics bug (where a `preserve-*` rule fired on the *presence* of a protection).
- **`exempt_terms` must be live.** Each `exempt_terms` phrase (e.g. `non-exclusive`) must contain at
  least one of the rule's `trigger_terms`; a dead exemption warns/fails.
- **`hard_rejection_refs` resolve.** Every topic `hard_rejection_refs` id exists in `hard_rejections[]`.
  Configured high-risk topics (indemnity, liability) must reference at least one rule.
- **`applies_to_topics` resolves.** Every id in `hard_rejections[].applies_to_topics` must exist in
  `topics[].id` (referential integrity). The reverse direction is also enforced: every id in
  `topics[].hard_rejection_refs` must exist in `hard_rejections[].id`. CI fails on either direction
  of a dangling cross-reference. Implemented in `tests/test_schema_hardening.py`.

The deterministic **detector-correctness gate** (zero fires on clean inputs; right rule fires on planted
violations; injection probes don't fire) is specified in
[docs/evaluation.md → Detector-correctness gate](evaluation.md#detector-correctness-gate). Rules that
lexical matching fits poorly (e.g. one-way confidentiality, bare payment terms) are **not** encoded as
detectors — they live in `topics[].reject_if_proposed` and are judged by the LLM review.

## Regex-dialect and ReDoS constraint (CI)

**Decision (issue #7, 2026-06-12): keep `match:'regex'`, constrained.**

The `match` field on `hard_rejections[]` rules accepts three values: `substring`,
`word_boundary` (default), and `regex`. The `regex` mode is kept because one production
rule (`no-exos-indemnity`) legitimately requires it to distinguish one-way from mutual
indemnification across modal phrasings — a distinction that substring and word-boundary
matching cannot make reliably. Dropping `regex` entirely would require demoting that rule
to `reject_if_proposed` (LLM-judged), which lowers the determinism guarantee for the
highest-risk clause type.

**Update (issue #220, 2026-07):** `match:'regex'` applies to **every** entry in
`trigger_terms` rule-wide — a rule that mixes a genuine regex pattern with plain
phrases (e.g. `no-exos-indemnity`'s `'hold harmless'` / `'duty to defend'` alongside its
one-way-indemnity regex) used to be forced to set `match:'regex'` for the whole rule,
silently compiling the plain phrases as regex too. Harmless while those phrases contain
no metacharacters, but a future plain phrase with metacharacters would silently change
what it matches instead of erroring. `regex_trigger_terms` is the fix: a per-rule,
ALWAYS-regex trigger list, independent of `match`, so `trigger_terms`/`match` stays on
`word_boundary` or `substring` for plain phrases and `regex_trigger_terms` carries only
the entries that are genuinely regex. `no-exos-indemnity` now uses this split. Every
constraint below (forbidden constructs, timeout, static check, GC review) applies
identically to `regex_trigger_terms` entries, regardless of the rule's `match` value —
see Check C's handling of `regex_trigger_terms` in `tests/detector/test_regex_redos_guard.py`.
Separately, a `regex` (or `regex_trigger_terms`) entry that fails to compile is now a
loud build failure (`scripts/detector_common.DetectorConfigError`), not a silent
substring-match fallback.

**Risk.** A catastrophic-backtracking regex in an activated playbook, fed a crafted
counterparty diff, stalls the deterministic detector stage — a pipeline DoS introduced
via the legal-content path, where reviewers are lawyers, not regex auditors.

**Constraints that must be met before any `match:'regex'` rule may be promoted to
`active` status:**

1. **Forbidden constructs.** The following regex constructs are banned from
   `trigger_terms[]` when `match:'regex'`, and from `regex_trigger_terms[]`
   (issue #220) unconditionally. They are the primary sources of
   catastrophic backtracking in backtracking NFA engines (Python `re`):
   - **Nested unbounded quantifiers**: `(X+)+`, `(X*)+`, `(X+)*`, `(X{n,})+` where
     `X` is any sub-expression that can match the same character as the outer group.
   - **Ambiguous alternation inside unbounded repetition**: `(a|ab)+`, `(ab|a)*`, or
     any pattern where alternatives overlap and the group has an outer `+`, `*`, or
     `{n,}` quantifier.
   - **Unrestricted `.{n,}` or `[^X]{n,}` inside alternation**: the form
     `alt1|[^X]{n,}alt2` where the character class overlaps with `alt2` triggers
     exponential backtracking on adversarial input. Bounded forms (`{0,40}` anchored
     to a non-overlapping character class) are permitted with GC review.

2. **Per-rule execution-time bound (timeout).** Every `match:'regex'` trigger_term
   must complete in under **0.5 seconds** on the adversarial corpus defined in
   `tests/detector/test_regex_redos_guard.py`. This is a hard CI gate; a timeout is a
   build failure.

3. **Static structural check.** CI (`tests/detector/test_regex_redos_guard.py`
   Check C) applies a static heuristic to detect nested-quantifier and
   ambiguous-alternation constructs. A trigger_term flagged by the heuristic fails
   the build regardless of measured runtime.

4. **GC review required.** Any addition or modification of a `match:'regex'` rule
   requires General Counsel (or designated legal tech owner) sign-off in the PR
   description, citing the specific prohibition the rule must detect and confirming
   that a lexical (`word_boundary` or `substring`) rule cannot serve the same purpose.

**CI gate:** `tests/detector/test_regex_redos_guard.py` enforces checks A (governance
doc), B (schema description), and C (adversarial timing + static heuristic). It runs in
the `detector-correctness` workflow on every change to `playbooks/` or
`docs/playbook-governance.md`.

## Canonicalization and content_hash (issue #5)

`release.content_hash` is "SHA-256 over the **canonical** playbook content". The canonical form
is **not** the whole playbook JSON. Hashing the whole document is self-referential: the `release`
block contains `content_hash` itself, so you cannot compute the hash before writing it, but writing
it changes the bytes. Likewise, flipping `playbook.status` from `draft` to `active` at promotion
time would change the hash, violating the "content-addressed immutable snapshot" guarantee.

### Canonical form (normative)

The canonical form is the playbook JSON with these two keys **removed** from the `playbook` object
before serialization:

| Excluded key | Why excluded |
|---|---|
| `playbook.status` | Lifecycle state; `playbook_versions.status` is the sole authority. Status flips must not change the hash. |
| `playbook.release` | The release block contains `content_hash` itself — including it would be circular. |

Serialization is deterministic: **keys sorted recursively, no extra whitespace, UTF-8**
(`json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).

Hash = `"sha256:" + sha256(canonical_bytes).hexdigest()`.

Implementation: **`scripts/canonicalize.py`** — functions `canonicalize()` and `content_hash()`.
Golden-hash CI fixture: `tests/gold-fixtures/canonicalize-golden-hash.json`.

Run locally:
```
python3 scripts/canonicalize.py                   # print hash of eiaa-v1.0.0.json
python3 scripts/canonicalize.py path/to/play.json # hash any playbook
python3 scripts/canonicalize.py --record          # update golden-hash fixture
```

### Status authority (normative)

**`playbook_versions.status`** (the DynamoDB row) is the **sole lifecycle authority** for
draft/active/retired state. `playbook.status` in the JSON document is a **snapshot label**
written at upload time for human readability — a projection, never the runtime gate.

This means:
- Promoting a bundle from draft to active does **not** require re-hashing or re-signing the
  playbook JSON. The hash was computed at upload time and is stable through the lifecycle.
- Code that gates production reviews must read `playbook_versions.status`, never `playbook.status`.

### Gate 7 — "approved hashes match the artifacts being promoted"

Gate 7 is now implementable step-by-step:

1. **Upload time.** `content_hash(playbook_doc)` is computed (via `scripts/canonicalize.py`)
   and stored in `playbook_versions.content_hash`.
2. **Legal-approval time.** The approver reviews the playbook at the hash recorded in step 1 and
   records that exact hash in `playbook_versions.legal_approval.content_hash`.
3. **Activation time.** The CI / admin activation endpoint asserts:
   ```
   playbook_versions.content_hash == playbook_versions.legal_approval.content_hash
   ```
   If they differ, the bytes changed after approval — the bundle **cannot** be activated.
4. The `release.content_hash` field in the JSON document is written at upload time (for audit
   trail purposes) — but the activation gate reads the **DB row**, not the document field.

**CI golden-hash gate.** `tests/gold-fixtures/canonicalize-golden-hash.json` records the
expected hash of `playbooks/eiaa-v1.0.0.json`. If the canonical form or playbook content drifts,
`tests/test_canonicalize.py::test_golden_hash_fixture` fails. After an intentional playbook-content
change, update the fixture with `python3 scripts/canonicalize.py --record` and commit it.

## Schema versioning

Every playbook instance declares `$schema: "https://teamexos.com/playbooks/schema/v1.json"`.
The engine that validates and executes playbooks maintains an explicit **supported schema
versions** list. Today that list contains exactly one entry:

- `https://teamexos.com/playbooks/schema/v1.json`

The four rules that govern schema evolution:

1. **Supported-versions declaration.** The engine declares its supported schema versions
   list in code (not inferred from file-system presence). On every upload and on every
   rollback target re-validation, the server reads `$schema` from the playbook JSON and
   rejects the bundle if the declared version is not in the supported list. **Fail-closed:**
   an unsupported or missing `$schema` value causes the upload to be refused with a clear
   error; there is no fallback to an assumed version.

2. **Rollback target re-validation.** A one-click rollback re-validates the old bundle
   against its declared `$schema` version before the rollback completes. If the old bundle
   declares a schema version that is no longer supported (i.e., it has been retired from
   the supported list), the rollback is refused and the operator must use a bundle that
   declares a supported version. This rule preserves the invariant that every active bundle
   can be re-played through the engine's validators without silent schema drift. Because
   retiring a schema version from the supported list blocks rollback to bundles that use it,
   schema versions are retired only after all non-retired stored bundles have been migrated
   (see rule 3).

3. **Schema major bump requires documented migration.** When a schema major version
   increment is needed (e.g., v2), the following steps are mandatory before the old version
   can be removed from the supported list:
   - Author a migration guide describing every breaking change and the required edits to
     each affected playbook field.
   - Run the migration against every non-retired `playbook_versions` row and verify each
     migrated bundle validates against the new schema.
   - Re-validate all candidate rollback targets against the new schema (or confirm they
     are pinned to a supported version before the old one is retired).
   - The old schema version remains in the supported list until all non-retired bundles
     have been migrated; only then may it be removed.
   Non-breaking additions (new optional fields) are a minor bump and do not require a
   migration guide; all existing playbooks remain valid.

4. **Schema changes go through the GC-gated path.** Any change to `playbooks/schema.json`
   — whether a minor addition or a major breaking change — is treated as a legal-behavior
   change and must clear the same GC-gated deliberate-activation path as a playbook change:
   PR review, CI gates, Legal approval, and a new release bundle. Schema changes are never
   hot-applied in the console; they ship through normal CI and promotion (build → sign →
   ECR → promote a digest).

**CI guard.** `.github/workflows/playbook-lint.yml` runs
`tests/test_schema_versioning_policy.py` on every change to `playbooks/`. The check
verifies that every non-retired playbook in `playbooks/` declares a schema version in the
engine's supported list and that the fail-closed rejection logic is exercisable.

## Release-bundle lifecycle

A legal-behavior release is a **bundle**, not just the playbook JSON: playbook hash, prompt hash,
canonical standard-form hash, model-policy hash (which pins primary/critic/**embedding** model IDs,
single-region native inference, and the request contract), active corpus snapshot version, evaluation
run ID, and signed Legal approval. The bundle moves through three statuses:

- **`draft`** — admin upload lands here. Not query-eligible. May be edited/re-uploaded.
- **`active`** — exactly one bundle per playbook id. Reached only by a **deliberate activation** after
  every structural rule above and every evaluation gate in [docs/evaluation.md](evaluation.md) passes,
  and the signed `legal_approval` block matches the exact content hashes being promoted. Every
  production review records the active bundle's component hashes at execution start.
- **`retired`** — a superseded bundle; its content-addressed snapshot is preserved, never deleted, for
  rollback/quarantine lineage.

**No-active-bundle system state.** There is no requirement that an active bundle always exists for a given playbook. The system explicitly supports a **no-active-bundle** state: when no bundle is currently `active`, `POST /api/reviews` is refused with HTTP `503` and the user-visible message "no active playbook". This state is entered by the **deactivate action** (see below) or when the very first bundle is found bad before any successor exists. Intake resumes as soon as an admin activates a bundle.

**Deactivate action.** An admin (GC-gated — the same approval level required to activate) may **deactivate** the currently active bundle without promoting a successor. This is distinct from **rollback** (which requires a prior bundle as the revert target) and is the correct action when: (a) the first-ever bundle is bad and no prior bundle exists, or (b) intake must be suspended during a quarantine investigation. Deactivate transitions the bundle to `retired` (preserving its snapshot) and clears `active_release_bundle_hash`. The action is audited. In-flight reviews continue to completion. The operating procedure is in [RUNBOOK.md → Suspending intake](../RUNBOOK.md#suspending-intake-deactivate-without-a-successor).

Activation, rollback, and deactivate (one-click revert to a prior bundle, auto-`QUARANTINED` of reviews run under the bad
bundle, `SUPERSEDED` on re-run, and no-active-bundle suspend-intake), and the audit entries for each are owned by
[ARCHITECTURE.md → Audit posture](../ARCHITECTURE.md#audit-posture) and the operating procedures in
[RUNBOOK.md](../RUNBOOK.md). The model-policy and embedding-model change rules (admin/GC approval,
quarterly recertification) are in [ARCHITECTURE.md → Model-selection policy](../ARCHITECTURE.md#model-selection-policy).

## Single-item corrections (issue #294)

For an OPF-bound playbook (`playbooks/bundle.schema-v2.json`, issue #286), your GC does not author
per-clause content — your GC provides high-level guidance plus, occasionally, a single-item
correction to something the playbook engine produced. **A single-item correction must NEVER require
re-running the playbook engine.** Every correction is instead expressed as an edit to the bundle's
`overrides` block, re-bound locally (seconds, no engine involvement), and activated through the same
release-bundle governance as any other bundle above — new bundle hash, the same deliberate-activation
gate, the same audit lineage.

There are exactly three correction levers. Each maps to a different part of the bundle, and none of
them ever re-runs the engine:

| Your GC says | Lever | Engine re-run? |
|---|---|---|
| "Tone it down — prioritize closing at lower risk over maximizing position" | `overrides.posture` (a new, governed Posture-version edit) | **Never** |
| "Never allow unlimited liability — make that absolute" | `overrides.floor_additions` (a new judged Floor invariant) | **Never** |
| "Stop proposing replacements that mention consequential damages" | pen-rules `per_topic` overrides (issue #293's toaster-owned `must_not_introduce` resolution) | **Never** |

**`overrides.posture`.** A governed edit to the Posture block's `system_prompt`. Carries a monotonic
integer `version` (absent overrides implies genesis version 0 — the very first correction is version
1), the edited prose, `edited_by`/`approved_at`, and a `parent_section_digest` that MUST equal this
OPF document's own `opf.identity.section_digests.posture` (OPF spec §8). `scripts/bind_bundle.py`
enforces both as fail-closed, exit-1 checks at bind time:
  - **Stale-edit guard.** If `parent_section_digest` does not match the OPF's current
    `section_digests.posture`, the OPF moved under your GC since the edit was authored — bind refuses
    and names both digests so the edit can be re-reviewed against the new genesis posture.
  - **Monotonic versioning.** With `--previous-bundle <path>` given, the new `version` must be
    strictly greater than the previous bundle's `overrides.posture.version` (0 if it had none).

`compose_opf_system_blocks` (issue #284) uses `overrides.posture.system_prompt` for the Posture block
when present, genesis `posture.system_prompt` otherwise — the embedded OPF itself is never edited; it
stays byte-identical through every override.

**`overrides.floor_additions`.** New, stricter-only Floor invariants (same `{id, statement,
rationale}` shape as `opf.floor.invariants`). Every id must be new: `bind_bundle.py` rejects any
`floor_additions` id colliding with a genesis `opf.floor.invariants` id. At composition and judging
time (`scripts/floor_judge.py`'s `judge_floor_invariants`, driven by `opf_prompt
.resolve_floor_invariants`), a `floor_additions` entry is unioned with the genesis invariants
(genesis first, stable order) and judged exactly the same way — a violated addition fires and forces
`REQUEST_CHANGE` exactly like a genesis Floor violation.

**Stricter-wins by construction.** There is deliberately NO key anywhere in `overrides` for removing
or weakening a genesis invariant or the genesis posture — additions and governed edits only.
`playbooks/bundle.schema-v2.json`'s `overrides` object sets `additionalProperties: false`, so an
invented key such as `overrides.floor_removals` fails schema validation outright, not a silent no-op.

**Re-bind + activate flow.** (1) Edit the override input — a `--posture-override <json>` file and/or
a `--floor-additions <json>` file. (2) Re-bind locally: `python3 scripts/bind_bundle.py --opf ...
--posture-override ... --floor-additions ... --previous-bundle <the currently active bundle> --out
<new bundle path>` — this is a local, offline operation; it never touches the playbook engine. (3)
The new bundle gets a new content hash (Gate-7-style activation, see above) and goes through the
existing deliberate-activation path — draft → structural validation → Legal approval → activate —
exactly like any other bundle. The audit lineage is unchanged: review rows additionally record
`posture_version` (int; absent ⇒ genesis) alongside the existing OPF §8 lineage fields (issue #287).

**Evidence corrections are out of scope here.** A correction to Evidence (the knowledge/precedent
layer) is engine-side, via embedded curation pins that survive recompile — it is never expressed as a
bundle override, and it is not covered by this section.
