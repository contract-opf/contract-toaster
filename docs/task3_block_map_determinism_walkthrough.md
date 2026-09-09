# Walkthrough: Resolving Block-Map Desync & Production Transcript Rejection

## Summary

We investigated the production incident on a real counterparty agreement (identity withheld; a 19-block affiliation agreement), where reviews failed with `block_transcript_rejected: [source_mismatch] block p0014: segment 0 diverges at folded offset 0`.

Instead of adding complex, fragile programmatic whitespace-claiming rules and anchor solvers to second-guess the model, we identified and eliminated the exact root cause: **a desync between the Stage 1 extraction block map and the Stage 5 materialized block map**.

---

## 1. Root Cause

1. In Stage 1, `extract_and_normalize` operated on the uploaded raw `.docx` bytes. In `_build_paragraph_record`, pre-tracked-change text (`original_text`) was evaluated for clause boundaries. Paragraphs 39, 49, and 61 carried revisions where the pre-edit text was short all-caps markers (`'EEE'`, `'EE'`, `'ENSTITUTIONE'`), spuriously triggering heading detections that split the document into **19 blocks** (with `p0014` at 8,109 chars).
2. The model was prompted with these 19 blocks and transcribed `p0014` faithfully starting with `"Exos represents and warrants that it is..."`.
3. In Stage 5, `materialize_accept_all_with_report` physically accepted the tracked changes into the XML. When `generate_redline_from_blocks` re-extracted blocks from the materialized document, those paragraphs contained full body text and no longer triggered heading boundaries, collapsing the document into **16 blocks**.
4. In Stage 5, `p0014` became an empty block of length 0. The validator attempted to prove the model's valid `p0014` transcript against an empty block, failing immediately at offset 0 (`block_context: ""`).

---

## 2. Changes Implemented

### [`scripts/extraction_normalization_stage.py`](../scripts/extraction_normalization_stage.py)
- In `_build_paragraph_record`, returned `resulting_text` (the operative post-acceptance text) alongside `text` (`original_text`) and `revisions`.
- In `extract_document_paragraphs`, evaluated `clause_boundaries.is_boundary_paragraph_ooxml` on `operative_text = record.get("resulting_text") or record["text"]`.
- `heading_source_text` remains `record["text"]` to preserve heading-text equality guards in downstream checks.

### [`tests/test_extraction_normalization_stage_80.py`](../tests/test_extraction_normalization_stage_80.py)
- Added `test_boundary_paragraph_detection_evaluates_operative_text` asserting that a paragraph whose pre-edit text is a short all-caps string (`'EEE'`) and whose post-edit text is a long body paragraph is NOT misclassified as a heading boundary, and asserting that `build_block_map` on raw bytes matches `build_block_map` on materialized bytes.

### [`docs/whitespace_and_block_transcript_plan.md`](whitespace_and_block_transcript_plan.md)
- Recorded the architectural decision to avoid heuristic whitespace machinery in code in favor of model intelligence + strict fail-closed validation.

---

## 3. Verification Results

### Automated Gates
- `SKIP_INFRA=1 bash scripts/check.sh` -> **CHECK: ALL GREEN** (all ~140 test files passed).
- `bash scripts/check-frontend.sh` -> **CHECK-FRONTEND: ALL GREEN** (all 88 vitest files / 943 tests passed, contrast/focus/layout audits green).
- `python3 tests/lint-brand-free.py` -> **BRAND-FREE LINT: PASS**.

### Real Document Offline Validation
- Ran block map comparison on `scratch/handoff-687-payload/real-corpus-candidate_7259-affiliation-agreement.docx`:
  - Raw and materialized block maps match **100% across all 16 blocks** (identical IDs, texts, and lengths).
  - Validated sample block patches on the document with `block_transcript.validate_block_patches`: **`status: proven`** with zero failures.

---

## 4. Container Deployment & Image Publishing

- **CI Workflow**: Run `33890533924` on `main` commit `2a866e3` passed all gates.
- **Container Build & Publish Workflow**: Run `33910233752` (`dts-image-publish.yml`) succeeded:
  - `ghcr.io/<org>/contract-toaster-dts-backend:latest` and `:<short-sha>`
  - `ghcr.io/<org>/contract-toaster-dts-frontend:latest` and `:<short-sha>`
- **Coolify Next Step**: Trigger "Restart (pull latest)" in Coolify on the live deployment to pull the latest images into production.

