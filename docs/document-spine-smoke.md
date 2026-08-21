# Document-spine smoke harness

Issue #565. Companion tools: [`tools/document_spine_smoke.py`](../tools/document_spine_smoke.py) (the harness), [`tools/churn_docx.py`](../tools/churn_docx.py) (the synthetic churn generator), [`tests/test_document_shapes.py`](../tests/test_document_shapes.py) (the public regression gate).

## What this is

`tools/document_spine_smoke.py` runs any corpus of `.docx` files through the model-free document spine — `scripts/extraction_normalization_stage.py` → `scripts/quote_locate.py` → `scripts/redline_quote_apply.py` — with **no playbook, no model, no network, no API key**, and prints only per-document and aggregate counts, ratios, and reason codes.

It has two roles:

1. **A discovery instrument for a private corpus.** Point `CORPUS_DIR` at real counterparty paper (yours, never committed, never read by this repo's own tests) and read the printed ratios and reason codes to find structural failure classes the spine does not yet handle. This is exactly how issues #560/#561 (a reserved `ns0` namespace prefix crashing the redline) and #564 (a quote spanning a multi-`<w:p>` paragraph join) were found in the first place — by running the real pipeline against a real, private, 26–31 document corpus and measuring what broke.
2. **The public regression gate for the synthetic corpus this issue adds.** `tools/churn_docx.py` reproduces each known failure class as a named, independently-toggleable transform applied to a generated (never vendored) base contract. `tests/test_document_shapes.py` builds one shape per transform under `tests/fixtures/document-shapes/` and asserts each one still survives the full spine. Running the harness itself against that directory (see Usage below) is a second, informal confirmation of the same property, in the same shape a human operator would use against their own corpus.

**Honest limitation:** the synthetic corpus only regression-tests failure classes someone has already found and turned into a `churn_docx.py` transform. It cannot discover a *new* failure class the private corpus has not yet surfaced — only role 1, run by a human against real (never-committed) documents, can do that. Treat a clean run against the synthetic corpus as "no known regression," never as "the spine handles every real document."

## Usage

```bash
# Point at your own private corpus (any directory of .docx files):
CORPUS_DIR=/path/to/your/corpus .venv/bin/python3.11 tools/document_spine_smoke.py

# Or at the generated public regression corpus (built on first run of the test):
.venv/bin/python3.11 tests/test_document_shapes.py
CORPUS_DIR=tests/fixtures/document-shapes .venv/bin/python3.11 tools/document_spine_smoke.py
```

Nothing is written back to the corpus directory and nothing is uploaded anywhere — every patch the harness "applies" is discarded after being counted; only the printed numbers survive.

## What the numbers mean

For each document, in order:

1. **Extract + normalize** (`extraction_normalization_stage.extract_and_normalize`). A document either `normalized` (every paragraph normalized, per `normalize_input.py`'s documented accept/reject rule) or was `refused` (one or more paragraphs failed closed — see docs/output-contract.md → "Fail-closed internal analysis report").
2. **Derive quotes.** For a normalized document, one candidate quote per normalized paragraph — that paragraph's own full `text`, the same normalized representation a review model would be shown. No model call stands in for what a real model would actually select; this is a structural probe of the locate/apply stages, not a simulation of model behavior.
3. **Locate** (`quote_locate.locate_quote_in_paragraphs`), once per derived quote: `found`, `not_found`, `ambiguous`, or `spans_paragraph_break` (issue #564 — the quote is genuinely present but crosses a physical-paragraph join `docx_editor` cannot write a tracked change across; a real, distinct, non-crashing outcome, never conflated with `not_found`).
4. **Apply** (`redline_quote_apply.apply_quote_patches`), once per document, batching every derived quote as one patch set: `applied` count, plus a `flag_only` reason histogram (`not_found` / `ambiguous` / `spans_paragraph_break` / `round_trip_verification_failed`).

The aggregate section rolls all of the above up across the whole corpus, plus two more buckets that only fire on a genuinely broken document:

- **`refused` reason codes** — `classify_unnormalizable_reason()` maps a normalization failure's notes to one of a small, fixed set of symbolic codes (`accepted_change_missing_resulting_text`, `malformed_pending_revision`, `unknown_tracked_change_status`, `unrecognized_revision_type`, or `unclassified_unnormalizable` for anything that matches none of the above). `pending_change_inside_field_code` was retired by issue #530: a pending change inside a field code no longer fails closed, so that code can no longer occur.
- **`spine_crash` exception classes** — a document that raised anywhere in the four stages above is caught, counted, and reported as `spine_crash:<ExceptionClassName>` (e.g. `spine_crash:ValueError`), and the scan moves on to the next document. This is what "discovery" actually looks like in practice: before issue #561 landed, running this harness (or its equivalent, by hand) against the real corpus would have printed `spine_crash:ValueError` for 17 of 26 documents.

## The privacy invariant

This tool is explicitly designed to be pointed at a real, private, sensitive corpus. Its output — every line it prints, in both the per-document and aggregate sections — **never contains document text, headings, party names, quotes, or a raw exception message.** Only counts, ratios, and the small fixed vocabularies above.

This is enforced in two places, both load-bearing:

- **Refusal reasons** are produced by matching a normalization failure's notes against a fixed set of *known* substrings (`document_spine_smoke._KNOWN_UNNORMALIZABLE_PATTERNS`) and returning only the matched symbolic code. The notes themselves — which embed the paragraph's *heading text*, see `normalize_input._normalize_paragraph` — are never printed, matched or not.
- **Crashes** are reported as `spine_crash:<ExceptionClassName>` — the exception's class name only. `str(exc)` is never read, let alone printed: an exception message can interpolate arbitrary document content (a filename, a party name pulled into a `ValueError`), and the class name alone is exactly as actionable for triage ("something raised a `KeyError` while extracting" is enough to go look at the code; the document's own text is not needed and must not appear in a terminal transcript, a CI log, or a bug report pasted into chat).

`tests/test_document_shapes.py::test_the_smoke_tool_never_echoes_a_sentinel_party_name` is the encoded grep-level check for this invariant: it builds a document carrying a fabricated "sentinel" party name that appears nowhere else in the test file, runs it through `document_spine_smoke.scan_document()`, and asserts the sentinel string does not appear anywhere in the result.

## When a number drops

- **A `spine_crash` count went from 0 to nonzero, or a new exception class appeared.** This is the important one. Something in the spine broke on a document shape it used to handle. Do not add a new fixed reason-code entry to paper over it — find the exception (locally, against the actual document, never in a shared log), fix the underlying crash, and add a `churn_docx.py` transform reproducing the *minimal structural trigger* (never the private document's content — see #560/#561's own fixture for the pattern: one `xmlns:ns0` attribute, nothing else) so the fix has a permanent regression shape under `tests/fixtures/document-shapes/`.
- **`found` ratio dropped / `not_found` or `ambiguous` went up.** Either real documents got structurally weirder (a new revision-history pattern, a new punctuation encoding) or a change to `extraction_normalization_stage.py` / `quote_locate.py` regressed matching. Compare against the previous run's numbers on the *same* corpus before concluding anything — a `not_found` a model would never actually produce in a real `source_quote` is a red herring; a `not_found` on a quote that reads as something a model plausibly would copy is worth chasing.
- **`refused` count went up.** Check the reason-code histogram first. `unclassified_unnormalizable` appearing at all means `normalize_input.py` grew a new failure branch this harness's classifier does not know about yet — add the new branch's stable message substring to `_KNOWN_UNNORMALIZABLE_PATTERNS` in `tools/document_spine_smoke.py` (never widen what gets printed; only widen what gets *classified*).
- **`spans_paragraph_break` is nonzero and expected.** This is not automatically a bug: a logical paragraph made of several physical `<w:p>` siblings (very common — a table's cells routinely get pulled into the preceding heading's body by `clause_boundaries.py`'s fallback detector, and a genuinely multi-sentence clause typed as several short paragraphs does the same) will report `spans_paragraph_break` for any quote spanning the join, by design (issue #564). It only deserves attention if it is happening on a quote a real model plausibly would have produced as one contiguous `source_quote` — the aggregate ratio is a prompt to go look, not a verdict on its own.

## `tools/churn_docx.py`: the six known failure classes

Every transform below is deterministic (same input bytes + same seed → byte-identical output) and independently toggleable. See the module docstring for full detail; summarized here:

| Transform | Reproduces | Guards |
|---|---|---|
| `tracked_changes_multi_author` | Two different authors' `<w:ins>`/`<w:del>` clusters, back-to-back, on one paragraph | Issue #563 — more than one pending cluster/author no longer fails closed |
| `curly_punctuation` | Word's typographic auto-correct (`'`/`"`/`-` → `'`/`"`/`–`) throughout the document | `quote_locate.py`'s `_TYPOGRAPHIC_FOLD` table — 15 of 16 documents in the real EIAA corpus carried curly punctuation |
| `split_paragraphs` | One clause's sentences split across 3+ sibling `<w:p>` elements | Issue #564 — `physical_spans` paragraph-join accounting |
| `strip_heading_styles` | Every `Heading*`-style `<w:pStyle>` removed | `clause_boundaries.py`'s document-signals fallback (numbered/lettered lead-ins, ALL-CAPS, bold) |
| `reserved_ns_prefix` | `xmlns:ns0="..."` declared on the document root | Issues #560/#561 — `ET.register_namespace` refuses any `ns<digits>` prefix; measured at 65% of a real 31-document corpus before the fix |
| `nested_ins_del` | An insertion (`<w:ins>`) later itself deleted (`<w:del>` nested inside it) | A net-zero edit real negotiation history routinely contains; proves nesting depth does not confuse extraction |

`tools/churn_docx.py --list-bases` / `--list-transforms` enumerate what is available; `--base <name> --transform <name> [--transform <name> ...] --seed N --out <path>` writes one churned `.docx`.

## Manual, human-run extra: the LibreOffice round trip

A document that has been opened and re-saved by a *different* OOXML writer (LibreOffice, Google Docs' export, an older Word version) is a real, common source of structural surprises this synthetic corpus does not cover, because `soffice` is not a repo dependency and this harness must stay network-free and dependency-light for CI. If you have LibreOffice installed locally, round-tripping the generated base documents (or your own corpus) through it before running the smoke harness is a useful manual extra:

```bash
soffice --headless --convert-to docx --outdir /tmp/roundtripped /path/to/corpus/*.docx
CORPUS_DIR=/tmp/roundtripped .venv/bin/python3.11 tools/document_spine_smoke.py
```

This is not run in CI and not required for this issue's gate — it is a manual step for a human operator who wants a wider discovery net, per the module docstring's "no LibreOffice dependency" scope boundary.
