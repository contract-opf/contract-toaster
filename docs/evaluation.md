# Evaluation & quality

Architecture lives in [ARCHITECTURE.md](../ARCHITECTURE.md).

This document describes how `contract-toaster` is held to a quality bar: the gold test set that gates every model integration, the regression gates that gate every playbook and prompt change, and the human-review feedback loop that grows the gold set over time. It is the authoritative home for the evaluation harness. For the LLM design and the playbook governance it gates, see [ARCHITECTURE.md → LLM](../ARCHITECTURE.md#llm--bedrock--claude-opus-48-primary-sonnet-46-critic) and [ARCHITECTURE.md → Redlining](../ARCHITECTURE.md#redlining--owned-docx-library). For the threats the harness is part of defending against, see [docs/threat-model.md](threat-model.md). For the foundation backlog, see [docs/phase-0-issues.md](phase-0-issues.md).

The premise of this document is blunt: **a legal-review tool that has not been measured against known-answer drafts is not a tool, it is a liability.** Everything below exists so that no model, prompt, or playbook change reaches production without first proving — deterministically, in CI — that it still gets the known cases right.

## The gold test set

The gold test set is a curated collection of known counterparty drafts with **known-correct answers**, built and signed off by Legal. It is the single source of truth for "is the tool behaving correctly?", and it is the acceptance gate for any model integration.

**It MUST exist before the LLM path is coded.** This is a hard ordering constraint, not a recommendation. We do not write the extraction → retrieval → primary → adversarial → redline pipeline and then go looking for a way to test it; we build the gold set first, then build the pipeline against it. A model integration with no gold-set coverage does not ship — there is no "we'll add tests later" path, because the whole point of the tool is correctness on the edge cases, and edge cases that aren't enumerated up front are exactly the ones a single-pass LLM silently gets wrong.

### What a gold case contains

Each gold case is an immutable, content-addressed fixture committed to the repo (synthetic or fully de-identified — see [Provenance and confidentiality](#provenance-and-confidentiality)). A case carries:

| Field | Purpose |
|-------|---------|
| `case_id` | Stable, unique identifier; referenced by metrics and regressions. |
| `input_docx` | The counterparty draft (content-hashed; the hash is part of the fixture). |
| `playbook_version` | The playbook version the expected answers were authored against. |
| `expected_decision` | `ACCEPT` or `REQUEST_CHANGE` — the externally-visible verdict. |
| `expected_issues[]` | The expected per-issue list: `playbook_topic_id`, `section_ref`, and whether the issue is a **hard rejection** (must always be caught) or a softer flag. |
| `must_not_flag[]` | Clauses that are acceptable as written — flagging any of these is a false positive. |
| `fp_tolerance` | The allowed false-positive budget for this case (often `0` for clean drafts). |
| `redline_checks[]` | For each expected `REQUEST_CHANGE` issue: the paragraph/table-cell anchor and source-text hash of the clause that **must** be the one patched. |

Cases are tagged by category — clean drafts that must `ACCEPT`, drafts with a single planted hard rejection, drafts with multiple interacting issues, near-miss drafts that probe over-flagging, and adversarial drafts that probe prompt injection from the document body. Coverage is tracked per playbook topic ID: **every hard-rejection topic in the active playbook must have at least one gold case that exercises it**, and the coverage check fails the build if a topic has none (see [Regression gates](#regression-gates)).

### What the harness verifies per case

For each case the harness runs the full pipeline (extraction, retrieval, both model passes, reconciliation, and redline generation) and checks four things:

1. **Decision accuracy.** Did the tool return the expected `ACCEPT` / `REQUEST_CHANGE`? A wrong decision is the most serious failure; a draft that should have triggered `REQUEST_CHANGE` but returned `ACCEPT` is a missed issue with a face-valid summary attached, which is precisely the failure mode the tool must not have.
2. **Missed-issue check.** Every expected hard rejection in `expected_issues[]` must appear in the output. The reconciliation rules guarantee that a hard rejection found by *either* the primary or the adversarial pass forces `REQUEST_CHANGE`; the harness asserts that guarantee holds end-to-end, not just in principle.
3. **False-positive check.** Nothing in `must_not_flag[]` may be flagged, and the total count of unexpected issues must stay within `fp_tolerance`. A tool that flags everything is as useless as one that flags nothing — it trains attorneys to ignore it.
4. **Redline-patch correctness.** For each issue, the harness confirms the patch landed on the **right clause**: the patched paragraph/table-cell anchor and source-text hash must match the `redline_checks[]` entry exactly. This is the same exact-match anchoring the pipeline uses to fail closed (see [docs/threat-model.md](threat-model.md)); the gold set asserts that a correct patch is verifiably correct, not merely plausible-looking. A patch applied to the wrong paragraph fails the case even if the replacement prose is otherwise reasonable.

A case passes only if all four checks pass. The harness emits a per-case report and the aggregate metrics below.

## Metrics

The harness reports a fixed set of metrics over the gold set on every run. These are the numbers that gate promotion:

- **Decision accuracy** — fraction of cases with the correct `ACCEPT` / `REQUEST_CHANGE` verdict.
- **Missed-issue rate** — fraction of expected hard rejections not surfaced. The target for hard rejections is **zero**; any regression here is a hard gate failure, not a percentage to negotiate.
- **False-positive rate** — unexpected flags per case, against each case's `fp_tolerance` and an aggregate budget.
- **Redline-patch correctness** — fraction of patches that landed on the anchored, hash-matched target clause.

Metrics are recorded per run against the **model ID, playbook hash, and prompt hash** in effect, so a regression can always be attributed to a specific change. The same identifiers are recorded on every production review (see [ARCHITECTURE.md → Audit posture](../ARCHITECTURE.md#audit-posture)), which lets us correlate a gold-set regression with the population of production reviews run under the same versions.

### Playbook-id namespacing

Every evaluation artifact — gold cases, detector gate fixtures, stochastic-stability runs, retrieval regression tests, and spend-ledger rows generated by the harness — is namespaced by **`playbook_id`**. The v1 namespace is `eiaa`. When a second agreement type is introduced its harness fixtures live under a distinct `playbook_id` and its CI gate runs independently. Namespacing is required because:

- Gold cases from one playbook are not valid test inputs for another (different standard form, different playbook topics, different hard-rejection rules).
- CI eval spend must be attributable per playbook so the documented per-playbook budget cap is enforceable.
- Detector-correctness gates (D1/D2/D3) are scoped per playbook — a D1 zero-fire assertion proved on the EIAA standard form is not portable to a different standard form.

The harness records `playbook_id` on every metrics row and spend-ledger entry. Aggregate dashboards may sum across playbooks, but the per-playbook breakdown is always available.

## Regression gates

Every change that can alter legal output — a model version or request-contract change, a prompt change, a playbook change, a canonical standard-form change, or a corpus-snapshot activation — runs the full gold set in CI and must clear the gates below before it can become active. The gates run in the same signed CI pipeline that builds and pushes the container image; nothing is promoted by a merge to `main` (see [ARCHITECTURE.md → Backend](../ARCHITECTURE.md#backend--app-runner--fastapi) for the build-and-deploy-by-digest model).

A legal-behavior release is a **bundle**: playbook hash, prompt hash, canonical standard-form hash, **anchor-map hash**, model-policy hash, active corpus snapshot version, evaluation run ID, and Legal approval. A bundle moves through three statuses — **`draft` → `active` → `retired`** — and the gates are the boundary between `draft` and `active`. A version stays `draft` until every gate passes; only then may it be promoted to `active`, and promotion is deliberate, never automatic.

### The gates

1. **Schema and release-bundle validation.** The playbook validates against `playbooks/schema.json`: unique topic IDs, **unique section refs** (placeholder/absent-section topics included), structured hard rejections with a valid **`kind`** and the per-kind field requirements, **`section_anchors` that resolve** to the bundled standard form, `not_in_standard` topics referenced only by `on_insert` rules, every `on_remove_or_alter.required_tokens` actually present in its anchored standard section, required hard-rejection mappings present, allowed citation behaviour declared, and replacement-text constraints satisfied. The release bundle must also bind the prompt hash, canonical standard-form hash, anchor-map hash, model-policy hash (incl. embedding model), corpus snapshot version, and evaluation run ID. A bundle that does not validate cannot leave `draft`. (Full rules in [docs/playbook-governance.md](playbook-governance.md).)
2. **Topic-coverage tests.** Every hard-rejection topic in the candidate playbook has at least one gold case exercising it. A new hard-rejection topic with no gold case fails the build — you cannot add a rule the harness can't check.
3. **Prompt/model/playbook regression tests.** The full gold set runs against the candidate release bundle. The metrics above must meet their gates: **zero new missed hard rejections**, false-positive rate within budget, and decision accuracy and redline-patch correctness at or above the current `active` baseline. A failing gate **blocks promotion** — the candidate stays `draft` and cannot be made `active`.
4. **Redline fixture tests.** A dedicated set of fixtures exercises the owned docx library's tracked-change semantics directly: `<w:ins>`/`<w:del>` correctness, anchor/hash exact-match patching, fail-closed behaviour when the target text no longer matches, and footnote insertion. These run against the vendored library and gate any change to it (see [ARCHITECTURE.md → Redlining](../ARCHITECTURE.md#redlining--owned-docx-library)).
5. **Stochastic stability.** Because the pinned request contract omits unsupported sampling parameters, deterministic model output is not assumed. The candidate bundle runs the gold set multiple times. Promotion requires zero hard-rejection misses across runs, stable redline anchors/source hashes, and false-positive variance within the approved budget. (The deterministic detector layer is exempt — it is fully reproducible, which is why its zero-false-positive gate in §"Detector-correctness gate" below is a hard gate, not a budget.)
6. **Corpus and retrieval regression.** A corpus snapshot cannot become active merely because ingestion succeeded. Candidate snapshots must pass retrieval tests that prove known relevant positive precedents are retrievable, rejected/negative examples stay in the hard-labeled channel, leakage checks pass, and a corpus change does not regress the gold set.
7. **Legal-approval metadata.** A release bundle cannot become `active` without signed release metadata recording the approving Legal reviewer, the approval timestamp, and the content hashes of the exact artifacts approved. CODEOWNERS review is necessary but **not sufficient** — the approval metadata is a machine-checked release gate, and the gate verifies the approved hashes match the artifacts being promoted.
8. **Detector-correctness gate (deterministic; runs without the model).** The hard-rejection detectors are run in isolation over the diff and must pass three checks — see [§Detector-correctness gate](#detector-correctness-gate) below. This is the deterministic backstop that makes the C1 false-positive class a permanent regression rather than a tuning problem.
9. **CI evaluation spend budget.** The harness invokes Bedrock (both passes, multiple stochastic runs), which would otherwise spend *outside* the per-review daily ceiling. CI Bedrock spend is therefore routed through the same reservation/ledger (or a separate, explicitly-budgeted CI ceiling), surfaced on the cost dashboard, and the gold-set size × stochastic-run count is **capped against a documented budget**; the harness `log`s and fails loudly when it would exceed it rather than silently truncating coverage. (Closes the cost-control hole where CI eval bypassed the `$20/day` ceiling.)

### Eval harness rate-limiting and quota-aware scheduling

The eval harness invokes Bedrock at a volume — 39 cases × 2 passes × multiple stochastic runs at ~60K tokens/call — that can exhaust the on-demand TPM quota for Opus 4.8 if CI runners are not coordinated. Exhausting quota causes `ThrottlingException` retries, extends the multi-hour CI run, and (without a proper alarm split) fires the "Bedrock errors > 0" alarm with noise. To prevent this:

- **Serialize primary-pass (Opus 4.8) calls.** The harness issues **one primary-pass Bedrock call at a time** per CI runner. The granted quota for Opus 4.8 in `us-east-1` is `40,000 TPM` (see `model-policy/bedrock-us-east-1.json`). Each primary call consumes ~68,000 tokens (input + output, uncached) — more than a single minute's quota — so even one concurrent runner can throttle if it issues calls back-to-back without a delay. The harness uses a **token-bucket rate limiter** seeded from `model-policy/bedrock-us-east-1.json → max_eval_parallelism.primary_rate_limit_tpm`.

- **Critic-pass (Sonnet 4.6) calls.** The critic quota is higher (`100,000 TPM`). Critic calls are also token-bucket limited but may overlap with the *next* primary call setup window. The harness uses `max_eval_parallelism.critic_rate_limit_tpm` from the policy artifact.

- **Max CI parallelism (`max_eval_parallelism`).** The `model-policy/bedrock-us-east-1.json` artifact records `max_eval_parallelism.max_runners = 1` under the current `40,000 TPM` Opus quota. If the account quota is increased (via an AWS Service Quotas request), update `max_runners` and `primary_rate_limit_tpm` accordingly. CI should not launch more parallel eval runners than `max_runners`.

- **Prompt-cache hit rate optimization.** Serializing calls (one in-flight primary call at a time) also **maximizes prompt-cache hit rate** on back-to-back stochastic runs: the playbook block (~30,000 tokens) cached on the first run is still warm for the second and third runs — calls are sequenced within the cache TTL window per model. A cached run consumes ~38,000 input tokens instead of ~60,000 — roughly a 37% reduction — compressing the eval wall-clock time from ~199 min to ~111 min at the same quota level (derivation in `model-policy/bedrock-us-east-1.json → review_throughput_ceiling`). Note: caches are per-model; the Opus primary cache cannot serve Sonnet critic calls and vice versa — serialization is applied independently per model.

- **Per-run cache-hit metrics recorded.** The harness records a **per-run cache-hit rate** (and per-model cache-hit count) in the eval run summary, derived from the `usage.cache_read_input_tokens` field returned by Bedrock on each call. This lets actual savings be measured against the estimated 37% token reduction and provides evidence for quarterly recertification that the caching structure is functioning as expected. A run where the hit rate is unexpectedly zero (outside the first cold run) should be investigated as a possible cache-structure regression.

- **Quota recertification.** The `granted_tpm` and `granted_rpm` figures in `model-policy/bedrock-us-east-1.json` must be re-verified at each quarterly model recertification (see [RUNBOOK.md → Model recertification](../RUNBOOK.md)). If quota has changed, re-derive `max_eval_parallelism` before running the next CI eval. An outdated quota figure in the policy artifact causes the harness to either under-utilize quota (too conservative) or throttle (too aggressive).

Every production review records the release-bundle hash and its component hashes at execution start, so a review is always attributable to a bundle that cleared these gates. When a bad bundle is rolled back, the reviews run under it are marked `QUARANTINED`; re-runs create replacement records and the originals become `SUPERSEDED`. The rollback mechanism and its audit entry are described in [ARCHITECTURE.md → Audit posture](../ARCHITECTURE.md#audit-posture) and the operating procedure in [RUNBOOK.md](../RUNBOOK.md).

### CI eval budget, gate tiers, and gold-set growth policy

#### Separate CI eval budget

CI eval spend is **not** routed through the production `$20/day` spend ledger. Sharing the production ceiling means one full stochastic gate run ($70–$150 per run — see cost derivation below) would consume 3–8 days of production budget and starve reviewers for the rest of the day. The CI eval budget is therefore a **separate, dedicated CI eval ceiling** with its own reservation/ledger entry, surfaced on the cost dashboard and attributable per `playbook_id`.

**Explicit CI eval budget caps:**

| Scope | Default cap | Notes |
|-------|------------|-------|
| Per-run (full stochastic gate) | **$200/run** | Upper bound for a 39-case × 3–5 stochastic-run gate at uncached pricing; a cached run at ~37% savings lands around $70–$130 |
| Monthly aggregate | **$1,000/month** | Bounds how many full gates can fire in a month without a manual increase; typically well under this at the expected release cadence |

**Per-run cost derivation.** A full stochastic gate (39 cases × 5 stochastic runs, serialized, uncached worst-case): each case requires one primary (Opus 4.8, ~60K tokens input + ~8K output) and one critic (Sonnet 4.6, ~60K + ~8K). At the Bedrock rates from ARCHITECTURE.md (Opus ~$5.50/M input, ~$27.50/M output; Sonnet ~$3.30/M input, ~$16.50/M output) and 5 stochastic runs:

```
Opus:    39 × 5 × (60K × $5.50/M + 8K × $27.50/M)  ≈  $128
Sonnet:  39 × 5 × (60K × $3.30/M + 8K × $16.50/M)  ≈  $ 77
Total uncached worst-case                             ≈  $205
```

With prompt-cache hits (37% token reduction on back-to-back stochastic runs — see [Eval harness rate-limiting](#eval-harness-rate-limiting-and-quota-aware-scheduling)):

```
Cached estimate:  $205 × ~0.65                       ≈  $133
```

A single-run gate (no stochastic repeats): ~$41 uncached, ~$27 with cache. The **$200/run cap** clears the 5-run uncached worst-case and any stochastic rounding; the **$1,000/month cap** allows ~5 full stochastic gates per month before requiring a manual cap increase. The harness **fails loudly** (exits non-zero, emits a clear budget-exceeded error) if a planned run would exceed the CI eval ceiling — it never silently truncates coverage by skipping cases to stay under budget, because truncated coverage defeats the gate.

**Wall-clock.** A full 5-run stochastic gate is **~2–4 hours wall-clock** (39 cases × 5 runs × ~2 min/case serialized at `max_eval_parallelism = 1`, with prompt-cache warming reducing later runs). A smoke-subset every-change run (≤ 10 cases, no stochastic repeats) is **~15–25 min**. CI pipelines should allocate at least 4.5 hours for a full gate and 30 minutes for an every-change smoke run.

#### Gate tiers

Running the full 39-case × 5-stochastic-run gate on every PR would cost ~$133–$200 per change and take 2–4 hours — impractical at normal development velocity. The gate is therefore **tiered**:

| Tier | Trigger | Case set | Stochastic runs | Approx. cost | Approx. wall-clock |
|------|---------|----------|-----------------|-------------|-------------------|
| **Detector gate** (deterministic) | Every change | All 39 cases, detectors only (no Bedrock) | n/a | ~$0 | < 1 min |
| **Smoke subset** (every-change LLM) | Every change | ≤ 10 cases (one per category, highest signal) | 1 | ~$5–$10 | ~15–25 min |
| **Full stochastic gate** | Release candidates and quarterly recertification | All 39 cases | 3–5 | ~$70–$200 | ~2–4 hours |

**Detector gate — every change.** The deterministic hard-rejection detectors run over all 39 fixtures on every PR (no Bedrock calls, near-$0 cost). This catches the most likely regressions — a broken detector is worse than a slow LLM — without spending budget. Every PR must pass the detector gate.

**Smoke subset — every change.** Up to **10 cases** (≤ 10 is the hard cap; Legal selects which cases are in the smoke set at each gold-set review) run with a single LLM pass to catch prompt and schema regressions early. Cases are selected to maximize coverage per dollar: one clean-ACCEPT baseline, one multi-issue planted rejection, one near-miss probe, and one injection probe are the minimum four. The smoke set must always include at least one `on_insert` and one `on_remove_or_alter` case. The **10-case cap is fixed** regardless of how many cases are in the full gold set; if the smoke set would need to exceed 10 to maintain minimum coverage, the minimum-coverage requirement wins and the cap is raised only by explicit joint decision of Legal and Engineering.

**Full stochastic gate — release candidates and quarterly recertification.** The full 39-case × 3–5-run gate fires only for:

- **Release candidates:** a bundle moving from `draft` toward `active` (any playbook, prompt, standard-form, model-policy, or corpus-snapshot change).
- **Quarterly recertification:** the model recertification cycle (see [ARCHITECTURE.md → Model-selection policy](../ARCHITECTURE.md#model-selection-policy)) requires a fresh full gate run even if no release bundle is pending, to confirm the pinned model still meets quality thresholds.

No other trigger fires the full stochastic gate. A merge to a feature branch that does not produce a release candidate does not run the full gate.

#### Gold-set growth policy

The gold set grows through the human-review feedback loop (see [Human-review feedback loop](#human-review-feedback-loop)). Without a tiering and pruning policy the every-change smoke subset would inflate linearly with every new case, making the every-change tier progressively more expensive. The policy:

**New cases enter as candidates first.** A case proposed from the feedback loop or from a new playbook rule enters a **candidate tier** — run only in full stochastic gates (release candidates and quarterly recerts), not in the every-change smoke subset. A candidate case proves its value over at least two consecutive full gates before being considered for promotion to the smoke subset.

**Promotion to the smoke subset is selective.** A candidate case is promoted to the every-change smoke subset only if Legal and Engineering jointly determine it catches a class of regression that the current smoke set does not cover. The smoke set **cap of 10 cases** is fixed; promoting a new case requires retiring an existing smoke case unless the cap is explicitly raised. Retiring a smoke case does not delete it from the gold set — it reverts to the full-gate-only set.

**The full gold set is bounded and periodically pruned by Legal.** Legal reviews the full gold set at each quarterly recertification and may **retire** (mark inactive) gold cases that are:

- Fully redundant with another case (same rule, same outcome, no incremental signal).
- Superseded by a more precise case added later.
- No longer exercising a live playbook rule (the rule was removed or substantially changed).

Retired cases are archived (not deleted) in case a playbook reversion would need them restored. The full active gold set is **bounded at ≤ 60 cases** (the current v1 target of 39 cases, with room to grow through feedback loop; beyond 60 active cases the quarterly pruning pass is required before adding more).

**Coverage invariant.** After any promotion, retirement, or growth event, the topic-coverage gate must still pass: every hard-rejection topic in the active playbook must have at least one active gold case exercising it (the smoke subset may rely on the full set for coverage — the invariant applies to the full active set, not just the smoke subset).

### Detector-correctness gate

The deterministic hard-rejection detectors (see [ARCHITECTURE.md → Retrieval](../ARCHITECTURE.md#semantic-retrieval-plus-a-deterministic-lexical-layer) and [docs/playbook-governance.md](playbook-governance.md)) run over the standard-form **diff**, so they can be checked in isolation, deterministically, without invoking the model. Three checks gate `draft → active`:

- **D1 — zero-fire floor.** Run every detector against (i) the clean canonical standard form diffed against itself (the empty diff) and (ii) every clean-`ACCEPT` gold case, plus the near-miss probes (a draft that *keeps* the `$150,000` cap and the `consequential damages` waiver, that says `non-exclusive`, that restates "students are not employees", that uses "neither party is a Business Associate", that uses "including without limitation"). **Required result: zero hard-rejection fires.** This is impossible to satisfy with a raw-text matcher and trivially satisfied by the diff-driven design — it encodes the C1 false-positive bugs as permanent regressions. Metric: **detector false-positive count on clean inputs (target 0, hard gate)**.
- **D2 — right-rejection fires.** For each `hard_rejections[].id`, at least one gold case plants exactly that violation in the diff (an inserted indemnity clause for an `on_insert` rule; a *deleted* `$150,000` cap for an `on_remove_or_alter` rule). Required: the expected rejection fires, and no other (within `fp_tolerance`). Metric: **planted-violation hit rate (target 100%, hard gate)**. This extends the topic-coverage gate from "a case exists" to "the right rule fires, and only it".
- **D3 — injection probes.** Run against adversarial cases whose document *body* contains trigger phrases inside instruction-like or quoted text not actually inserted in scope (e.g. "do not add any indemnify clause"). Required: no fire unless the phrase is a genuine in-scope counterparty injection.

### Critic-input manifest gate (issue #29)

The per-pass prompt manifest (see [ARCHITECTURE.md → Per-pass prompt manifest](../ARCHITECTURE.md#per-pass-prompt-manifest)) made a deliberate change to the critic's input: the raw counterparty document is **omitted** from the critic pass in favour of diff + anchored clauses + primary output. The decision is stated in [docs/design-notes.md → Why the critic prompt omits the raw document](design-notes.md#why-the-critic-prompt-omits-the-raw-counterparty-document), but a design assertion alone is not evidence. This section defines the eval-harness gate that makes the critic-input decision machine-checkable and reverts it if evidence contradicts it.

**What the gate measures.** On every full stochastic gate run (release candidates and quarterly recertification), the harness computes for the critic pass:

- **Missed-issue rate on single-planted-rejection cases** — the fraction of expected hard rejections that the critic *alone* would have surfaced (i.e., that the primary missed and the critic caught). Target: matches or exceeds the baseline recorded below.
- **Assembled critic-pass token count (P95)** — the 95th-percentile assembled token count for the critic pass across all gold cases. This is the assembled-size evidence for the manifest decision: the diff + anchored clauses + primary output representation must be materially smaller than diff + anchored clauses + raw document would be. Target: **assembled critic-pass size ≤ 65,000 tokens P95** (vs. an estimated 73,000–78,000 tokens P95 if the raw document were included at the 15K-token threshold).
- **Critic-pass missed-issue detection parity baseline** — the critic with diff + anchored clauses + primary output must achieve **≥ 100% of the missed-issue catch rate** of the equivalent critic prompt that includes the raw document, measured over the full gold set on the stochastic gate run that established this baseline. The baseline is: **diff+anchored+primary-output critic: 0 missed hard rejections across 15 single-planted cases, 3–5 stochastic runs** (the denominator is the set of cases where the primary missed the issue and the critic was expected to catch it).

**Projected baseline (v1, drafted 2026-06-22 — not yet a measured result).** The v1 manifest decision is based on the following PROJECTION, not a recorded/measured comparison: at v1 the gold-set harness had not yet run a live stochastic gate (issue #204 — no model call, no documents, existed at the time this table was drafted), so every number below is an estimate derived from documented manifest blocks and typical EIAA size ranges, not an observed run. Do not cite this table as "recorded" evidence. It must be replaced by the harness's actual measured output the first time a full stochastic gate executes (scripts/eval_harness.py's smoke-tier model pass, issue #204) — until then, treat every cell as a projection pending verification:

| Metric | PROJECTED: critic with diff+anchored+primary output | PROJECTED: critic with raw doc added |
|--------|------------------------------------------|--------------------------------------|
| Missed hard rejections (15 cases × 3 runs) | **0** (target: 0) | 0 (expected parity; raw doc provides no additional diff signal) |
| Assembled token count P95 | **≤ 65,000 tokens** | ~73,000–78,000 tokens (+12–20%) |
| False-positive rate on clean cases | Within budget (same as primary gate) | Same (raw doc does not reduce FPs) |

The baseline is recorded here because at v1 the gold-set harness is not yet live (the eval harness is built before the LLM path per the ordering constraint in [The gold test set](#the-gold-test-set)); the token-count estimates are derived from the documented manifest blocks and the full-doc size range for typical EIAAs. When the live harness runs its first full stochastic gate, it must record the actual observed values against these targets; if observed missed hard rejections > 0 and attributable to the omission of the raw document from the critic pass, the manifest must be revised under the release-bundle gate (not by softening this test).

**Gate enforcement.** A manifest change that alters what the critic receives (adding the raw document, removing primary output, etc.) requires:

1. A new full stochastic gate run that records the above metrics.
2. The measured missed-issue rate must be ≤ the baseline above (zero new missed hard rejections).
3. The assembled-size evidence must be recorded in this section before the bundle can be activated.

A manifest change with no updated comparison evidence in this section **blocks bundle activation** — the same as a prompt change with no eval run ID. This gate is the machine-checkable form of the design decision recorded in [docs/design-notes.md](design-notes.md#why-the-critic-prompt-omits-the-raw-counterparty-document).

### The synthetic gold set (v1)

The v1 gold set is **synthetic**, seeded from the existing playbook (its topics, `hard_rejections`, and `de_minimis_categories`) and the referenced canonical standard form — no production legal documents (see [Provenance and confidentiality](#provenance-and-confidentiality) and [ARCHITECTURE.md → Environments](../ARCHITECTURE.md#environments)). Target ≈33–39 cases, each carrying the fixture fields above:

| Category | ~Count | Purpose |
|----------|-------:|---------|
| Clean-`ACCEPT`, verbatim standard form | 1 | Empty-diff zero-fire baseline (D1) |
| Clean-`ACCEPT`, acceptable variations | 4–6 | Drafts using `acceptable_variations` (higher cap, longer cure, deemed-receipt email) still `ACCEPT` |
| Clean-`ACCEPT`, de-minimis edits | 2–3 | Name/typo/format changes; reaffirming language — explicit FP-bug repros |
| Single planted hard rejection | 15 (one per rule) | `on_insert` cases insert prohibited language; `on_remove_or_alter` cases delete/alter the protected token (D2) |
| Near-miss / over-flag probes | 6–8 | `non-exclusive`, "neither party is a Business Associate", "no benefits", cap kept, waiver kept (D1) |
| Injection probes | 2–3 | Trigger phrases as fake instructions / quoted text (D3) |
| Multi-issue interaction | 2 | Several planted rejections at once; exercises monotonic reconciliation |

The 15 single-planted cases satisfy the per-rule topic-coverage gate; the clean + near-miss + injection cases satisfy D1/D3; the multi-issue cases exercise reconciliation. Planting recipe: start from the verbatim standard form and either INSERT a clause containing a trigger term in the in-scope section (`on_insert`) or DELETE/alter a `required_token` in the protected section (`on_remove_or_alter`); `redline_checks[]` pins the anchor + source-text hash of the clause that must be patched.

#### Issue #2 additions (reconcile-hard-rejections)

Four fixtures added by issue #2 are now part of the regression set:

| Fixture | Category | Rule exercised | Purpose |
|---------|----------|---------------|---------|
| `accept-narrow-mutual-ip-indemnification` | Clean-`ACCEPT`, acceptable variation | `no-exos-indemnity` (must not fire) | Narrow mutual IP indemnification capped by Section 8 is an acceptable variation; verifies the narrowed rule does not produce a false positive on this accepted phrasing. D1 guard. |
| `accept-vicarious-liability-additional-insured` | Clean-`ACCEPT`, acceptable variation | `no-excess-insurance-levels` (must not fire) | Additional-insured status mutually granted and limited to vicarious liability is an acceptable variation; verifies the narrowed rule does not false-positive on this accepted phrasing. D1 guard. |
| `reject-one-way-hold-harmless` | Single planted hard rejection | `no-exos-indemnity` (must fire) | One-way Exos hold-harmless obligation ("Exos shall defend, indemnify, and hold harmless Institution"). D2: verifies `hold harmless` trigger fires and is not over-exempted. |
| `reject-one-way-exos-indemnify` | Single planted hard rejection | `no-exos-indemnity` (must fire) | One-way uncapped Exos indemnification with no `hold harmless`/`duty to defend` language ("Exos shall indemnify Institution … with no cap on liability"). D2: verifies the `exos shall indemnif` regex trigger catches the bare-indemnify violation class that escaped detection before the fix. |
| `reject-one-way-exos-will-indemnify` | Single planted hard rejection | `no-exos-indemnity` (must fire) | One-way Exos indemnification using "will indemnify" phrasing — a variant that escaped the prior `exos shall indemnif` trigger. D2: verifies the broadened `exos\s+(shall\|will\|must\|agrees?\s+to)\b[^.]{0,40}indemnif` regex catches "will/must/agrees to indemnify" phrasings. |

These fixtures enforce the detector-correctness gate (D1/D2) for the two rules modified by issue #2 and must not be deleted or weakened without a corresponding playbook change reviewed under the `legal-review-required` process.

## Human-review feedback loop

Approval happens **outside** this tool — the tool issues recommendations, attorneys make decisions, and nothing here changes that (see [ARCHITECTURE.md → What we are explicitly not building](../ARCHITECTURE.md#what-we-are-explicitly-not-building)). But the moment an attorney acts on a review is the single richest quality signal we have, and we capture it without turning the tool into an approval system.

For each completed review, the tool records the **attorney disposition** — whether the attorney **accepted** the tool output as-is, **edited** it before use, or **rejected** it — as a lightweight capture in the reviewer UI. This is metadata about the tool's quality, not a legal approval state: the disposition has no effect on the review's verdict, gates nothing, and is never surfaced as "approved". The capture includes structured reason codes and topic IDs when an attorney edits or rejects output, so later triage can identify whether the miss was playbook, retrieval, model, redline, or usability.

The disposition feeds quality in two ways:

- **Quality metrics over time.** Edit and reject rates, sliced by playbook topic and playbook version, are the leading indicator that a topic is over- or under-flagging in the field. A topic whose recommendations are routinely edited is a candidate for playbook refinement; a spike in rejections after a version goes `active` is an early rollback signal.
- **Gold-set growth.** Reviews where the attorney's edit reveals a clear miss or a clear false positive are triaged by Legal as candidate **new gold cases**. Promising candidates are de-identified or reconstructed as synthetic fixtures, given expected answers, and added to the gold set — so the harness gets stronger exactly where the tool was weak in production. This is the loop that turns field experience into permanent regression coverage rather than tribal knowledge.

Edited or rejected dispositions enter a Legal triage queue with a target review cadence. Triage outcomes are audited as quality metadata and can produce playbook changes, corpus curation changes, prompt/model-policy changes, or new gold cases. Raw attorney edits are Confidential document substance and follow the document retention policy.

Crucially, raw dispositions and any attorney edits are treated as confidential document substance and are subject to the same logging and retention controls as the documents themselves (see [docs/threat-model.md](threat-model.md) and [ARCHITECTURE.md → Storage](../ARCHITECTURE.md#storage)); they are not written to application logs, and a candidate gold case is built from a de-identified or synthetic reconstruction, never by pinning a real counterparty document into the repo.

## De-identification standard

When a production review reveals a quality signal strong enough to become a gold fixture (an attorney edit or rejection that uncovers a clear miss or false positive), the fixture is **not promoted by copying the real document**. Instead, it follows this standard before it can enter the repo. The standard is enforced by CI: any fixture whose `"provenance"` field is `"production"` must carry `deidentification_approved_by` (the GC or their designee who verified the de-identification) and `deidentification_approved_at` (the ISO 8601 date of sign-off). A fixture without those fields fails the build.

### What de-identification requires

All four steps are mandatory. Skipping any step leaves a quasi-identifier that can re-identify a party in a small corpus of schools.

1. **Strip names and party identifiers.** Remove or replace all explicit counterparty names, institution names, city/state references, signatory names, and contact information. Use generic placeholders: "Institution", "University", "Party B". Verify that no form of the real name remains — including in section headings, recitals, and signature blocks.

2. **Strip dates and deal-specific metadata.** Remove or genericize all deal dates, effective dates, renewal dates, amendment numbers, and any date that could be cross-referenced against a known signing event. Replace with a synthetic date (e.g. "January 1, 20XX") or omit where the field is not legally material to the test case.

3. **Change dollar values.** Replace all specific dollar figures (caps, fees, deposit amounts, hourly rates) with values that do not match any real executed agreement in the corpus. The replacement value must still satisfy the playbook rule being tested — if the test asserts the $150K cap is required, a synthetic cap of $150,000 is fine; use a clearly fictional value for amounts that are not the tested figure (e.g. "$99,999" for a de-minimis fee).

4. **Structural rewording of distinctive clauses.** If the original clause phrasing is distinctive enough to identify the counterparty (unusual structure, bespoke fallback language, non-standard defined terms), reword the clause to preserve the legally-tested feature while eliminating the identifying phrasing. Distinctive constructions are quasi-identifiers in a small corpus of schools even without an explicit party name.

### GC sign-off

Before a de-identified fixture is committed, the GC or their designee reviews the de-identification and records sign-off in the fixture JSON:

```json
"provenance": "production",
"deidentification_approved_by": "gc@example.com",
"deidentification_approved_at": "2026-06-01"
```

The sign-off is a legal certification that the fixture no longer contains identifiable counterparty information. Fixture additions that claim `"provenance": "production"` route through the CODEOWNERS legal path, so a code reviewer alone cannot merge a production-derived fixture; it requires a CODEOWNERS review from Legal.

Fixtures generated from scratch (synthetic, seeded from the playbook with no real counterparty document) set `"provenance": "synthetic"` and are exempt from the sign-off requirement — they have no real counterparty data to de-identify.

### Fixture promotion procedure

When Legal triages an attorney edit and decides to promote it to a gold fixture:

1. **Reconstruct, do not copy.** Build the fixture from the playbook rule and the issue pattern — do not start from the raw production document. If a synthetic reconstruction adequately captures the quality signal, prefer it.
2. **Apply the de-identification steps** (above) if starting from a real clause.
3. **Obtain GC sign-off** and populate `deidentification_approved_by` + `deidentification_approved_at` in the fixture JSON.
4. **Open a PR through the legal CODEOWNERS path.** CI will enforce the sign-off fields. CODEOWNERS will require a Legal review.

## Provenance and confidentiality

Gold-set and fixture inputs are **synthetic or fully de-identified** — see [De-identification standard](#de-identification-standard) above for the written standard and sign-off requirement. Production legal documents are not committed to the repo and are not reachable from developer laptops (see [ARCHITECTURE.md → Environments](../ARCHITECTURE.md#environments)). Each fixture is content-addressed and immutable; changing a fixture's expected answers is itself a reviewed change, so the meaning of "passing the gold set" cannot drift silently. The gold set lives alongside the code it gates and is versioned with it, so any commit can be checked out and its exact quality bar reproduced.
