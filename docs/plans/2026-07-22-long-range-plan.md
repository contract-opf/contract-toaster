# Contract-toaster — long-range plan (2026-07-22)

> *Published from the project's planning history on 2026-09-09. Issue numbers
> below predate the tracker migration of 2026-09-08 and refer to the old
> private numbering; read them as history, not as links.*

Canonical roadmap. Supersedes the build order in #101 (kept as the historical index).
Framed by four decisions taken 2026-07-22 (see also memory `llm-native-review-decision`).

## Framing decisions
- **D1 · Deployment: both first-class.** The AWS stack (`infra/`, App Runner + Step Functions, 18 CI gates, CloudWatch) *and* the Docker Compose / Hetzner path (`deploy/dts/`, live at the maintainer's Coolify deployment) are maintained. The AWS-only backlog stays live.
- **D2 · North-star: a working toaster first, then all of it.** Address the OSS launch, demo/pilot, and feature depth over the long range — but **Phase 1 is a working toaster**.
- **D3 · Review is LLM-native — no deterministic contract-review rules.** One intelligent LLM reads the **playbook** (negotiating history / accept-reject positions) + **toaster guidance** (per-review instructions typed into the toaster) + the contract → issues, each with a verbatim `source_quote` → a **critic pass that checks its own work** → decision. **Toaster guidance trumps the playbook** on conflict. The deterministic **detector engine (#76)** and the **standard-form line-diff** are retired from issue-generation. Safety rails (leakage gate, OOXML scan, docx round-trip verify, ReDoS guard, provenance) are **kept** — they aren't "rules" in this sense. Accepted trade: the deterministic hard-stop floor is replaced by the critic self-check + the **judged-NL Floor**.
- **D4 · Playbook governance: build it.** Upload / activate / rollback / quarantine / version history — with an authority model designed first.

**Through-line:** Phase 1 is largely a *deletion + reconnection* job (rip out detectors/diff, unify on model quotes), which is why "working toaster" is achievable first.

⚠️ **Acceptance reality:** the offline gate uses FakeBedrockClient (schema-perfect fixtures) and **cannot prove the toaster works on real docs** — this is the documented "green suite is no evidence" lesson (memory `verification-discipline`, `model-output-contract-drift-2026-07`). Every phase that touches the review brain ends with a **live check on the box against the live model**, not just a green gate.

## Phases & issue disposition (every open item placed)

| Phase | Goal | Issues |
|---|---|---|
| **0 · Hygiene** ✅ done | Honest board | Closed #397 (stale), #195→#244, #123 (skeleton delivered); re-scoped #85, #101 |
| **1 · Working toaster** | LLM-native review + quote redline, live on a reworded doc | overlay (new **#398**), retire detectors #380, wire redline #379, e2e #381, download #162; #76 retired |
| **2 · Governance (D4)** | Self-service playbook lifecycle | #214, #67, #68, #77, #78 (authority model first) |
| **3 · Admin surfaces** | Dashboards, audit, failure UX, ledger STUBBED rows | #252+#91, #253+#93, #244, corpus/cost/disposition endpoints, CloudWatch tiles, #85 remainder |
| **4 · Deployability & demo** | Clone-deployable + demo auth | #225, #243, #245→#246, #58 |
| **5 · Retrieval (RAG)** | Precedent retrieval | #28→#89→#88→#90 |
| **6 · OSS launch** | Apache cut | PR #374, #341, #282/#292, #281 |
| **7 · Prod cutover (AWS)** | Go-live gates (legal) | #96, #97, #98, #99, #100, #86 |

## Phase 1 grind queue (label `afk`, dependency-ordered)
`#398` (LLM overlay: guidance precedence + judged-NL Floor, code-only) → `#380` (retire detectors + diff) → `#379` (wire redline to quotes) → `#381` (offline e2e). `#162` (authenticated download) runs in parallel. The toaster-guidance **UI input** is a fast-follow (`afk-backlog`, separate frontend pass — kept out of this backend grind so the gate stays single-command).
