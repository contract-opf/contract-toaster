# CONTEXT — contract-toaster domain vocabulary

A glossary only: the terms and concepts specific to this repo's domain, not
implementation details. Add an entry when a term gets pinned down in
conversation; keep each to a sentence or two.

## The product

**Toaster** — the review tool itself. The kitchen metaphor runs through the UI
deliberately: you load a draft, choose a browning level, and get back a
receipt.

**Browning** — the Light / Medium / Dark control on the Review tab (issue
#495). It sets **markup intensity**: how aggressively the review marks up the
draft. Each level contributes exactly one predefined plain-English sentence to
the review's `toaster_guidance`; Medium contributes nothing, so an untouched
control sends the request the tool would have sent before the control existed.
The sentence shown under the control and the sentence sent to the model are the
same string literal, so a reviewer can never be shown one instruction while the
model receives another.

**Toaster guidance** — the free-text per-review instructions the form sends.
Reaches both model passes and is projected back verbatim in History, so
"Show instructions" is a faithful record of what the model was actually told.

**Receipt / register** — surfaces of the Review console. The receipt is the
outcome summary; the register is the status display. Neither is ever handed
clause text, a raw server exception, or a document excerpt.

**Orbit Diner** — the diner-themed Review-tab console delivered by an outside
designer (epic #729) and now the only Review tab. Its unmodified source of
record is `frontend/vendor/orbit-diner/`, which is never edited.

## Review and its output

**Playbook** — a codified negotiating position for one agreement type:
per topic, the standard-form language, the variations acceptable without a
redline, and the changes that must be pushed back on. Expressed in OPF.

**OPF (Open Playbook Format)** — the machine-readable schema playbooks are
written in, maintained publicly at `contract-opf/opf`.

**Topic** — one negotiable subject in a playbook (`term-length`,
`limitation-of-liability`, `confidentiality`, …), carrying the position and its
acceptable variations.

**Hard rejection** — a change that must always be pushed back on, never
accepted silently. Detected by a rule with its own grammar and CI gate.

**De minimis category** — a class of change small enough to accept without a
redline, so the tool does not manufacture noise.

**Primary pass / critic pass** — the two model passes. The primary produces the
review; the critic adversarially re-reads it. Both receive the same guidance.

**Cover note** — the short covering explanation returned alongside a redline.

**Block transcript** — the active output contract (schema v3). The model
transcribes the document as addressable blocks, and edits are applied against
those blocks rather than by locating quoted text. A transcript that does not
match its source block is rejected (`block_transcript_rejected`) rather than
applied.

**Output contract** — what the tool is allowed to emit and how it is framed:
a binary decision, tool-recommendation framing, citation and footnote rules.
Never a legal verdict.

**MANUAL_REVIEW_REQUIRED** — the terminal status when the system cannot stand
behind an answer. A system status, never presented as a legal outcome.

**Gold set** — the curated known-answer drafts, signed off by Legal, that gate
every model, prompt, and playbook change. The real one is tenant data; the
repo ships a synthetic one.

**Entity roster** — the list of our own legal entities (subsidiaries, parents,
d/b/a names) the tool recognises as *our* contracting party rather than the
counterparty.

**Party recognition** — deciding that a name in a draft is one of our
entities. Two words carry it, both defined by `scripts/entity_normalize.py`:

**Recognition key** — the identity two spellings of one entity are the *same
entity* under: the name with diacritics, punctuation, `&`, a leading "the",
and one trailing legal-form suffix folded away. It is what the roster and the
prompt's recognition set deduplicate on, so `Synthetic Holdings GmbH` and
`SYNTHETIC HOLDINGS G.m.b.H.` are one entity typed twice. Lossy on purpose —
the suffix is gone — so it is never stored, rendered, or shown; the spelling
an admin typed is what gets kept.

**Variant** — a spelling the same entity might plausibly appear under in a
document: each half of a `d/b/a` compound, the suffix-less core, and that core
under every alternative spelling of its suffix. Variants are for *searching* a
document (the preflight card's party advisory), never for rendering into a
prompt — listing a canonical name beside its alternates is exactly the "one
real principal plus also-rans" shape the flat recognition set exists to avoid.

**EIAA** — Educational Affiliation Agreement for student internships, the first
agreement type the tool was built against.

## Repository and deployment

**The engine** — the public, brand-free code at
`contract-opf/contract-toaster`. It knows nothing about any agreement type
until a playbook is activated at runtime, and every deployment-specific value
resolves from context rather than a literal.

**The overlay** — the private Exos-specific layer: real account and domain
values, the activated playbook bundle, the real gold set, and any Exos-only
AWS code. Lives in `exos-legal/contract-toaster`. Adding an Exos-only file is
the overlay's purpose; editing an engine file is a divergence to be fixed
upstream instead.

**The cut** — publishing by building a fresh-history tree from the private repo
and landing it on the public one (`scripts/public-cut.sh`), never by flipping
the private repo's visibility. Its history holds a real client corpus and can
never be published.

**The corpus** — the real signed agreements the playbook was derived from.
Counterparty identities are visible in the folder names alone, so it is
excluded wholesale from anything that ships.

**The two deployment targets** — the AWS path (CDK, App Runner, Step
Functions), and the Docker Compose path that runs under Coolify. The `dts` in
`deploy/dts/` and in the published image names is a legacy container-name
artifact, not a meaningful acronym; read it as "the Docker Compose target".
