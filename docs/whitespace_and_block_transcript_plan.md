# Architecture & Resolution: Block-Map Determinism vs. Whitespace Machinery

## Context & Root Cause Resolution

During live smoke testing on a real counterparty agreement (identity withheld), the review previously failed with:
```
status: MANUAL_REVIEW_REQUIRED
reason: block_transcript_rejected
last_error: [source_mismatch] block p0014: segment 0 diverges from the block's own text at folded offset 0
```

### 1. Root Cause Characterization

Deep offline diagnosis revealed that **the model did not fail on whitespace or transcription fidelity**:
- The model transcribed the paragraph faithfully as presented in the prompt (`"Exos represents and warrants that it is..."`).
- The failure was an internal code desync between Stage 1 and Stage 5:
  - **Stage 1**: `extract_and_normalize` operated on the uploaded raw `.docx` bytes, where `_build_paragraph_record` evaluated `original_text` (pre-tracked-change text) against `is_boundary_paragraph_ooxml`. Three paragraphs carrying unaccepted revisions had short pre-edit text (e.g. `'EEE'`, `'EE'`, `'ENSTITUTIONE'`) that triggered spurious heading detections, splitting the document into **19 blocks** (with `p0014` having 8,109 characters).
  - **Stage 5**: `materialize_accept_all_with_report` physically accepted all revisions into the XML. When `generate_redline_from_blocks` re-extracted blocks from the materialized document, those paragraphs contained full body text and no longer triggered heading boundaries, collapsing the document into **16 blocks**.
  - In Stage 5, `p0014` became an empty block (length 0). The validator attempted to prove the model's valid `p0014` transcript against an empty block, failing immediately at offset 0 (`block_context: ""`).

---

### 2. Architectural Decision: Lean Validation vs. Whitespace Machinery

In response to the hypothesis of adding programmatic whitespace-claiming rules and anchor-based mid-block keep reconstruction:
1. **Model Intelligence Over Heuristic Code**: We rely on the intelligence of the model to reproduce verbatim text for `keep` and `delete` segments, guarded by:
   - Clear prompt instructions (`primary_review_pass.py`).
   - Narrow encoding tolerances (`text_fold.py` for quotes/spacing).
   - The informed retry loop that shows exact offset context upon divergence.
2. **Avoiding Fragile Heuristics**: Programmatic whitespace attribution between keeps and deletes was previously attempted in issue #683 and rightly rejected because it introduces silent text corruption (e.g., turning single spaces into double spaces or deleting multi-space runs). Adding complex whitespace machinery in Python to solve a problem the model didn't cause is anti-pattern.
3. **Strict Fail-Closed Validation**: `block_transcript.py` remains strict, simple, and fail-closed.

---

### 3. Implementation

#### [`scripts/extraction_normalization_stage.py`](../scripts/extraction_normalization_stage.py)
- In `_build_paragraph_record`, expose `resulting_text` (the operative post-acceptance text) alongside `text` (`original_text`) and `revisions`.
- In `extract_document_paragraphs`, evaluate `clause_boundaries.is_boundary_paragraph_ooxml(p_el, operative_text)` where `operative_text = record.get("resulting_text") or record["text"]`.
- `heading_source_text` remains `record["text"]` to preserve heading-text equality guards in downstream checks.
- Result: Both Stage 1 (on uploaded draft) and Stage 5 (on materialized redline bytes) evaluate identical operative text, producing 100% identical block maps.

#### [`tests/test_extraction_normalization_stage_80.py`](../tests/test_extraction_normalization_stage_80.py)
- Added `test_boundary_paragraph_detection_evaluates_operative_text`:
  - Verifies that a paragraph whose pre-edit text is a short all-caps string (`'EEE'`) and whose post-edit text is a long body paragraph is NOT misclassified as a heading boundary.
  - Verifies that `build_block_map` on raw bytes matches `build_block_map` on materialized bytes.

---

### 4. Verification

1. **Unit Test Suite**:
   - `tests/test_extraction_normalization_stage_80.py`: **PASS** (all 18 tests).
   - `tests/test_accept_all_materializer.py`: **PASS** (12/12).
   - `tests/test_formatting_revision_acceptance_685.py`: **PASS** (9/9).
   - `tests/test_tracked_move_extraction_686.py`: **PASS** (7/7).
   - `tests/test_omitted_placeholder_646.py`: **PASS** (11/11).
2. **Real-Corpus Incident Reproduction (identity withheld)**:
   - Stage 1 and Stage 5 block maps both yield 16 blocks with identical keys and character counts.
   - `validate_block_patches` verifies `status: proven` with `failures: []`.
