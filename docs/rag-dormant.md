# Retrieval (RAG): dormant by decision — status, rationale, and the revival playbook

**Status: OFF. Owner decision, 2026-08-11.** The review runs on the LLM's own judgment over
a projected playbook digest, with no retrieval stage. This document records what exists,
what does not, why, and exactly what would have to happen to turn it on — so the decision
can be revisited deliberately rather than by attrition.

Nothing in this document is a plan of record. It is the map you would need if you decided
to go.

---

## 1. What actually exists today

Retrieval was **never turned on**. It was partly built, then its premise was retired.

| Piece | State | Where |
|---|---|---|
| Vector store + Knowledge Base | **built, empty** (#60, closed) | `infra/lib/nested/data-stack.ts` — `CfnKnowledgeBase`, S3 Vectors bucket + index, KB service role, Bedrock DataSource over the corpus bucket |
| Corpus ingestion | **never run** | DataSource exists; nothing ingested |
| Retrieval stage | **never written** | #89 open (`phase:3-rag`, p1) |
| K / chunking / token budget | **never pinned** | #28 open (spike) |
| Snapshot promotion | **never written** | #88 open |
| Corpus curation UI | **never written** | #90 open |
| Prompt plumbing | **present, fed empty** | `scripts/review_spine.py:577` passes `retrieved_precedent=[]` |

#60's own test docstring is explicit that it was the *empty-corpus phase*: "this issue
proves the store exists, is private, and is queryable; real ingestion/extraction is out of
scope."

**Idle cost is near-zero by design.** #60 deliberately chose S3 Vectors over OpenSearch
Serverless for exactly this reason ("no OpenSearch Serverless collection"). What remains
live is IAM surface — the KB service role and a `corpus/*` read grant — for a subsystem
nobody uses. That is the main argument for eventually tearing it down.

## 2. Why it is off

The 2026-07-22 architecture decision made review **LLM-native**: the model leads with its
own judgment, the playbook is *negotiating history* (evidence), and toaster guidance
outranks it. Retrieved precedent was a feature of the older design in which the pipeline
assembled evidence for a comparatively passive model.

The owner reaffirmed this on 2026-08-11: *"we want LLM to lead with its intelligence…
Primary focus at the moment is a fully working system without RAG."*

## 3. What replaces it: projection + index

This is the part worth understanding before reconsidering retrieval, because **the
non-RAG depth path is already designed** and is not the same as "no depth."

- **The projection.** `scripts/opf_prompt.py::_digest_block` renders the OPF `digest`
  (`digest_version` 4, OPF 0.5 — the only format this repo reads since issue #128) as
  precedent: per clause our standard, how many deals opened with it and kept it, the
  positions of the signed texts, the signed variants with how many were reached from our
  standard or signed as proposed, the refused asks and the openings not signed as proposed,
  each with distinct-deal counts and a citation. It carries no stance, band or risk
  verdict, and no per-deal precedent record: variant and ask texts are ≤ 300-character
  summaries. The wholesale alternative "measured ~1M tokens on a real corpus and cannot
  reach a model at all."
- **The index.** Depth comes from exact keys, not similarity: per-clause critic dossiers
  selected deterministically from the rule ids the reviewer and the open hard-rejection
  ledger name and the manifest predicates a pre-critic code check finds broken in the
  document (#170, `hard_ledger.anomaly_rule_ids`), and appended by orchestration to the
  critic's role suffix (#40, `scripts/critic_dossiers.py`). OPF 0.5 ships those dossiers precomputed
  (`dossiers`, with the hard-rule `manifest`), so #40 selects rather than builds them, and
  sends each whole. An earlier design served
  the same keys through a model-callable `lookup_clause_evidence` tool; it was never wired
  into a request and was retired in #161.

Why this is preferable to retrieval for this problem: the clause identity is already known
from the diff, so the question is *"what did we do on this clause?"*, not *"what is
similar?"* An exact-key lookup answers the first question deterministically and auditably,
with no embeddings, no ranking, and no re-embedding governance.

## 4. The honest argument *for* retrieval

Recorded so the case is not strawmanned:

**Id-lookup can only go deeper on clauses the playbook already knows.** Counterparty
language with no matching `clause_id` has no depth path at all — the model is on its own.
Retrieval could surface precedent for genuinely novel terms.

If it turns out that much of the real review value sits in clauses the playbook does not
cover, that is a substantive reason to revisit this, and no amount of index-polishing
addresses it. **Measure that before reviving anything:** across real reviews, what fraction
of flagged issues concern clauses absent from the playbook digest? That number is the
decision input, and nobody has it.

## 5. Revival playbook — in order

Do not start at #89. The order below exists because several steps are prerequisites that
are easy to miss.

### 5.1 Decide the question first
Measure the novel-clause fraction (§4). If it is small, revival buys little and the
remaining steps are cost without benefit.

### 5.2 Re-arm the leakage controls — **blocking, security-critical**

This is the step most likely to be skipped, and it is the one that matters.

When this section was written, **four of six `ConfidentialCorpus` lists were empty in
production**; since #40 `precedent_verbatim_spans` and `internal_precedent_ids` no longer
are:

| List | Production | Why |
|---|---|---|
| `system_prompt_ngrams` | empty (#521 populates it) | not passed by `review_spine.py:482`/`:486` |
| `playbook_ngrams` | 4 entries | derived from the playbook |
| `standard_clause_ngrams` | 3 entries | derived from the playbook |
| `internal_precedent_ids` | the dossier precedent ids (#40) and every deal `document_id` (#172) | `from_opf_document` reads every dossier excerpt's `precedent_id` and every `omitted_precedent_ids` entry (`_opf_dossier_precedent_ids`), then every deal `document_id` the digest citations and `evidence.precedent[]` carry (`_opf_deal_document_ids`) |
| `counterparty_names` | empty | not passed |
| `precedent_verbatim_spans` | the dossier excerpts (#40) | `from_opf_document` reads every dossier excerpt's whole `opening` / `signed` text that is not our standard |

Leakage check **3** (`citation_leakage`) is **live, for the dossiers' precedent ids and
the deals' `document_id`s**: the critic receives each selected dossier whole, ids
included, and the prompt names each deal by its `document_id` on two paths, the
digest's citations in both passes' prompts and engine-written Floor rationale prose
(printed verbatim in both passes' shared prefix and carried again in each dossier's
`floor_rules`); since #172 `internal_precedent_ids` holds both, so check 3 blocks an
external field that echoes one. The deal ids come from the digest citations and the
`evidence.precedent[]` walk; nothing reads the Floor rationale, so a deal only that
prose names is held because it has a precedent record. It is a token match and blocks
the slug as rendered, and only that: `counterparty_names` is still empty, so the same
counterparty name de-slugged into plain words (`Theta Logistics` for `theta-logistics`)
passes (#173).
Check **4** (`precedent_verbatim_spans`)
is **live, for the critic dossiers only**: retrieval is off, but since #40 the critic
receives whole precedent excerpts (texts a counterparty wrote or signed), and check 4 is
the only check that guards them on the internal channel. It blocks an internal note that
copies a whole excerpt; it does not block a part-quotation or a paraphrase, nor a digest
precedent text that is in no dossier (#171). Both checks are pure list iteration with no heuristic fallback.

Reviving retrieval makes both **load-bearing for retrieved text, immediately**, and
interacts with the notes-mode epic:

- The 2026-08-09 owner decision permits `citation_leakage` **internally** — internal notes
  may name past counterparties. Its stated safety argument is that check 4 prevents
  "permit citation" from becoming "dump the corpus." **Check 4 does that job only for the
  texts it holds, and only against whole-text copying.** Today it holds the dossier
  excerpts; retrieved text would be a new, unguarded corpus.
- Therefore: **`counterparty_names`, `internal_precedent_ids` and `precedent_verbatim_spans`
  must be populated from the retrieved text before internal notes ship with retrieval on.**
  #521 records this coupling as a blocking prerequisite. See also #574.

**Do not enable retrieval and internal notes in the same change.**

### 5.3 Related correctness change that becomes obsolete
#582 makes the prompt omit `RETRIEVED_PRECEDENT` when the list is empty. Its acceptance
criteria deliberately assert the **non-empty** path composes exactly as before, so revival
needs no prompt change — just a non-empty list.

### 5.4 Re-activated governance obligations
`ARCHITECTURE.md:240` documents `embedding_model_id` as pinned, **recertified quarterly**,
with **admin (GC) approval** required to change it or to re-embed, producing a new
`corpus_snapshot_version`. #582 removes that obligation while nothing is embedded.
**Reviving retrieval re-activates it**, and it is a recurring human commitment, not a
code setting. Budget for it.

### 5.5 Then the build
#28 (pin K / chunking / token budget) → #89 (retrieval stage integration) → #88 (snapshot
promotion) → #90 (curation). Note #89's own title carries the requirements that matter:
*diff-scoped, budget-capped, channel-separated*.

### 5.6 Things that must remain true
- Retrieved precedent is **untrusted input** and must stay inside a delimited block with the
  adjacent warning (`_delimited_block`, `UNTRUSTED_BEARING_TAGS`). It is our corpus but
  third-party in origin.
- Positive and negative precedent must not commingle in one top-K context — #60 built
  `document_type` and `corpus_polarity` for this; honour them.
- Retrieval must never become policy. The digest (digest_version 4) renders precedent as
  facts only (counts, the signed or proposed wording, a citation), and its one judged value,
  the `vs_standard` label, is an index to check, never an instruction, precisely so the
  playbook stays evidence; retrieved precedent is subject to the same rule.

## 6. Alternative: commit to no-RAG

The other end of the decision, recorded for symmetry. Delete the prompt plumbing, close
#28/#88/#89/#90 as won't-do, tear down the KB / vector bucket / KB role, and delete this
document's §5. Gains: no idle IAM surface, no dormant-subsystem confusion, and the leakage
coupling in §5.2 becomes permanent by design rather than by accident. Cost: rebuilding #60
from scratch if the §4 measurement later says retrieval is needed.

**Not chosen.** The park was chosen instead, so the option stays open.

## 7. Decision record

| Date | Decision |
|---|---|
| 2026-07-22 | Review is LLM-native; retrieval's premise retired |
| 2026-08-11 | RAG stays **off**; focus is a fully working system without it; revival path documented rather than deleted |

**Related:** #28, #88, #89, #90 (retrieval build, open, `afk-backlog`) · #60 (store, closed)
· #521 (leakage channel + the coupling in §5.2) · #574 (check-4 population) · #579/#580
(the non-RAG drill-down) · #581 (tool-calling measurement) · #582 (doc corrections).
