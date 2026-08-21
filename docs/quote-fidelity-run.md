# Model quote-fidelity measurement runner

Issue #566 (second half of #507). Companion tools: [`tools/quote_fidelity_run.py`](../tools/quote_fidelity_run.py) (the runner), [`tests/test_quote_fidelity_runner.py`](../tests/test_quote_fidelity_runner.py) (the offline gate). Modeled on [`scripts/live_smoke_eval.py`](../scripts/live_smoke_eval.py) / [`docs/document-spine-smoke.md`](document-spine-smoke.md)'s "AFK build, human execute" split.

## What this is

Every redline-mechanics number measured so far (`tools/document_spine_smoke.py`, issue #565) derives its candidate quotes FROM the normalized text itself, so every quote it feeds `scripts/quote_locate.py` is by construction character-perfect. That proves the *locate* side of the spine survives real document shapes; it proves nothing about whether a REAL model, asked to copy a verbatim `source_quote` out of the document it is shown, actually does so faithfully.

`tools/quote_fidelity_run.py` closes that gap. For each `.docx` under `CORPUS_DIR`, it runs the SAME stage-1 extraction/normalization a real review runs, then the REAL primary review pass (`scripts/primary_review_pass.run_primary_pass`, composed exactly the way `scripts/review_spine.py::run_review` composes it — no diff hunks, no anchored clauses, an empty playbook), through whichever model client the deployment actually selects (Bedrock or OpenRouter — never a hardcoded adapter). Every `source_quote` the model's response carries is then located against the same normalized paragraphs the model was shown, and only counts — never quote or document text — reach stdout.

**Building and offline-testing this script is AFK work.** Running it against a live model and a real, private corpus is a human step: it costs real money and reads real counterparty paper.

## Usage

```bash
# Point at your private corpus, review the printed call-count estimate, then opt in:
CORPUS_DIR=/path/to/your/corpus .venv/bin/python3.11 tools/quote_fidelity_run.py
# -> prints "found N document(s) -> up to N primary-pass model call(s)" and refuses.

CORPUS_DIR=/path/to/your/corpus FIDELITY_RUN_ACK=1 \
    .venv/bin/python3.11 tools/quote_fidelity_run.py

# Optional: also write full per-document JSON (CONFIDENTIAL -- includes the
# quotes themselves and the normalized paragraph text) for local inspection
# of a specific not_found/ambiguous case. This directory MUST be outside
# this repository, or gitignored if it is under the repo root -- it holds
# real counterparty text and must never be committed:
CORPUS_DIR=/path/to/your/corpus FIDELITY_RUN_ACK=1 \
    .venv/bin/python3.11 tools/quote_fidelity_run.py --artifacts /tmp/quote-fidelity-artifacts
```

### Which deployment / model this actually calls

This runner never hardcodes a provider. It reads `DEPLOY_TARGET` (the same env var that selects every other adapter for this codebase's two deployment targets — `backend/src/config.py`):

- `aws` (the default): a real `LiveBedrockModelClient` against the pinned model in `model-policy/bedrock-us-east-1.json`. Needs working AWS credentials with `bedrock:InvokeModel` on that model.
- `dts`: a real `OpenRouterModelClient`, keyed by `OPENROUTER_API_KEY` (or an admin-set key, if this process also has a reachable settings store) and `OPENROUTER_PRIMARY_MODEL_ID` (or the policy pin in `model-policy/openrouter.json`) — the exact same resolution the Docker Compose deployment's own real pipeline uses.

Set `DEPLOY_TARGET`/the relevant credentials before running, exactly as you would to run a real review against that deployment.

### Expected cost shape

One model call per document (the primary pass only — no critic pass, no reconciliation, no redline). Each call may retry once on a schema-validation failure (`scripts/primary_review_pass.py`'s `MAX_RETRIES_PER_PASS`), so the true worst case is 2 calls/document. There is no dollar-figure estimate printed (unlike `scripts/live_smoke_eval.py`'s budget preview) — only the call count — because per-call cost depends entirely on which deployment/model you pointed this at; price it against that model's own published per-token rate before running a large corpus.

## Spend safety

- **`FIDELITY_RUN_ACK=1` is required.** The runner always prints the call-count estimate first, then refuses (non-zero exit, no client ever built) unless this is set to exactly `"1"`.
- **The production daily-spend ledger is honored when reachable.** If `DAILY_SPEND_TABLE` is configured AND a DynamoDB resource can actually be reached from wherever you're running this, each document's call reserves/settles against that same cap (`backend/src/reviews.py::reserve_spend` / `settle_spend`) — the run stops early if the day's cap is genuinely exhausted, exactly like a real review would be refused.
- **Most of the time, that machinery is NOT reachable** — an operator running this ad hoc, from a laptop, with no deployment env vars set at all, will see `daily-spend machinery NOT reachable from this script context` printed at the top of the run. When that is so, **this run proceeds OUTSIDE the daily cap and the human operator running it owns the spend.** Estimate your own exposure from the call count and the target model's rate before setting `FIDELITY_RUN_ACK=1`.

## What the numbers mean

For each document, in order: extract + normalize (materialized — issue #563's accept-all disposition is applied to both the text the model reads and, when at least one pending tracked change was accepted, the docx bytes too, exercising the exact stage-1 code path a real review runs) → the real primary review pass → locate every `source_quote` the model emitted.

Per-document outcomes (opaque `[NNNN]` index only — never the source filename):

- **`reviewed`** — the primary pass returned a schema-valid response. Reports `decision`, `quotes` (how many issues carried a non-empty `source_quote`), and the four `quote_locate.locate_quote_in_paragraphs` outcomes for those quotes: `located` (found), `not_found`, `ambiguous`, `spans_paragraph_break` (issue #564 — genuinely present, but crossing a physical-paragraph join; a real, distinct, non-crashing outcome, never conflated with `not_found`).
- **`unnormalizable`** — stage 1 refused the document (see `docs/document-spine-smoke.md` for what that means).
- **`primary_pass_failed`** — the primary pass exhausted its bounded retry still schema-invalid, or failed closed for another documented reason (`document_too_large`, etc.). Reports the status/reason tokens only.
- **`crash`** — an exception anywhere in the chain (extraction, materialization, the model call itself). Reported as `crash:<ExceptionClassName>` only — never the exception's own message, which can echo document content.

The aggregate section rolls the above up across the whole corpus, plus the number this tool exists to produce: **the source_quote locate rate** — `located / emitted` across every reviewed document. That is the decision-rule input.

## The decision rule

- **Locate rate ≥ ~97%** → the model quotes faithfully enough that the re-quote retry pass stays flagged off; the quote-based addressing architecture (`scripts/quote_locate.py`) is doing its job.
- **Materially below that** → the model's own copying is the bottleneck, not the locator. Enable (or design) the re-quote retry pass (ask the model to re-emit a `source_quote` that failed to locate, before falling back to a flag-only issue), and revisit whether quote-based addressing is the right primary mechanism at all before investing further in it.

Read the `not_found` / `ambiguous` breakdown before concluding anything: a `not_found` on a quote that reads as something the model plausibly tried to copy verbatim is the real signal; a handful of `ambiguous` results on short, repeated boilerplate phrases is expected and not itself evidence of a fidelity problem. Use `--artifacts` to inspect specific cases.

## The privacy invariant

This tool is designed to be pointed at a real, private, sensitive corpus AND a real model. Its default (stdout) output — every per-document line, every aggregate line — **never contains document text, quote text, party names, or a raw exception message.** Only counts, ratios, and an opaque per-document index.

`--artifacts DIR` is the one deliberate exception: it writes the quotes and normalized paragraph text to local JSON files, for an operator who wants to inspect a specific case. That directory is CONFIDENTIAL-corpus material — put it outside this repository, or make sure it is gitignored if it must live under the repo root. Never commit it, never paste its contents into a shared channel or bug report.

`tests/test_quote_fidelity_runner.py` encodes the grep-level check for the default-report half of this invariant: it runs a scripted corpus carrying sentinel quote/rationale strings and asserts none of them appear anywhere in the captured stdout report, while confirming they DO appear in the `--artifacts` output (the positive control that substance is going somewhere, not silently dropped).
