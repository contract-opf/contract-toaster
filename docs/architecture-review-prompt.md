# Architecture review — initiation prompt

> Hand this prompt to an independent LLM (ideally a strong reasoning model, in a fresh
> session) to get an adversarial read on the `contract-toaster` design. It deliberately does
> **not** contain our own findings — we want an independent assessment, not a confirmation
> of ours.

---

## Prompt

You are a principal-level architecture and security reviewer. Your job is to **red-team**
the design of an internal tool before we build it — find what's wrong, risky, or weak, not
to reassure us. Be specific, be adversarial, and prioritize substance over style.

### What the system is

`contract-toaster` is an internal tool for reviewing **Educational Affiliation Agreements for
Student Internships (EIAAs)** submitted by partner schools, against the company's standard
positions. A reviewer signs in (Google SSO, single hosted domain), uploads a
counterparty-modified `.docx`, and the tool returns either an **ACCEPT** decision or a
**REQUEST_CHANGE** with a redlined `.docx` (tracked changes + footnoted rationales). A human
attorney approves before anything goes back to the school — the tool drafts and analyzes; it
does not give legal advice or constitute approval.

It is built on AWS: a React SPA on Amplify, Cognito federated to Google, a FastAPI API on
App Runner pinned to promoted signed image digests, an async review pipeline where the API
starts Step Functions directly (no SQS entry path), an explicit Bedrock model-policy matrix
with pinned regional primary/critic/fallback IDs, retrieval via a Bedrock Knowledge Base
over a curated and activatable corpus snapshot, S3 + DynamoDB for storage, and AWS CDK for
all infrastructure. Production runs in a separate AWS account from dev; dev uses synthetic
documents/corpus only. It is a low-volume, high-stakes, legal-facing tool.

### The source of truth (read these — do not rely on my summary above)

The authoritative design lives in the GitHub repository **`contract-opf/contract-toaster`** and in
local working artifacts. Please read, by reference:

- `README.md` — overview, architecture-in-brief, repo layout.
- `ARCHITECTURE.md` — the full system design: components, routes, data flow, data model,
  storage, audit posture, security posture, cost shape, environments.
- `docs/design-notes.md` — the *why* behind the contested decisions (RAG vs. fine-tune,
  Bedrock vs. direct API, retrieval-store choice, App Runner, two-agent review,
  prompt-injection model, row-level security, the claude-for-legal fork posture).
- `docs/phase-0-issues.md` — the Phase 0 backlog, sized to PRs, showing what gets built
  first (note that all security-bearing work is intentionally in Phase 0).
- `docs/architecture-issue-spotting-2026-06-01.md` — the current supplemental issue
  register and suggested resolutions from the latest architecture pass.
- `playbooks/schema.json` and `playbooks/eiaa-v1.0.0.json` — the codified review playbook
  the model reasons against.
- `.github/labels.yml` — labels/process, for a sense of how work is organized.
- Local working dir `~/Documents/dev/contract-toaster/`, including `aws-access-request.md` (the
  least-privilege AWS account/access setup) for the operational/IAM context.

### What I want from you

Review the design through three lenses:

1. **Soundness / red-team.** Where does this design break? Failure modes, race conditions,
   correctness gaps, operational traps, scaling or latency cliffs, things that look fine on
   paper and fail in practice. Assume an unfriendly reality.
2. **Security & compliance.** Threat-model it. Auth and authorization, tenant/data
   isolation, prompt injection via the uploaded document, secrets, blast radius, audit
   integrity, data retention and legal hold, least-privilege, supply chain. This is
   legal-facing data — hold it to that bar.
3. **Alternatives.** Where a materially better, simpler, cheaper, or lower-risk approach
   exists, name it and say why it beats what's documented. Call out over-engineering as
   readily as under-engineering.

### How to respond

- A **tight punch list.** Each item: a one-line statement of the issue, a severity
  (**critical / high / medium / low**), the lens it falls under, and a concrete suggested
  fix or the question we need to answer. No filler, no restating the design back to me.
- **Go deep and go novel.** A first pass over the obvious issues has already been worked
  through and is reflected in the documents you're reading — so do not spend your budget
  re-flagging things the docs already resolve. Assume the easy catches are taken; spend
  your effort on the second- and third-order problems we're most likely to have missed.
- If something in the docs is ambiguous or looks internally inconsistent, say so — that
  itself is a finding.
- End with the **top 3 things you would change before writing any code**, in priority
  order.
