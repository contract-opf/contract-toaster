# Walkthrough: Handoff Orientation, Deploy, and Three Tasks

**Date**: 2026-09-04  
**Repo**: `exos-legal/contract-toaster`  
**Commits on `main`**:
- `d511028`: `fix(prompt): treat uploaded draft as counterparty's accepted position so favourable terms are never softened (#687, #684)`
- `70a600e`: `feat(playbooks): add presigned download route and action for playbook versions`
- `d801124`: `docs(handoff): record walkthrough and incident diagnosis for 2026-09-04 handoff`

---

## 1. Task 1 (Issue #687) — Counterparty's Accepted Position

### Objective & Changes
Enforce owner ruling that the uploaded document represents the counterparty's accepted position; the reviewer must never narrow, soften, balance, or mutualize terms that already favour us.

- **[scripts/primary_review_pass.py](../scripts/primary_review_pass.py)**:
  - Updated `render_review_guidance_block(party, counterparty_type)` for both v1 and OPF prompt paths:
    > "The document you are reviewing has already been reviewed by the counterparty and reflects terms they accept. A term that favours us is therefore a term the other side has agreed to: leave it exactly as written. Do not narrow it, balance it, make it mutual, or improve its drafting, whatever the drafting argument, even where you would have drafted it differently."
  - Updated `_CRITIC_TASKING_DUTIES_1_TO_3` Duty 2 (`OVER-FLAGGING AND GIVING TERMS AWAY`):
    Instructed the critic to contest any edit that narrows, softens, balances, or mutualizes a favourable term.
- **[tests/test_review_objective_block_677.py](../tests/test_review_objective_block_677.py)**:
  - Added prompt-pin assertions verifying the new guidance block phrasing and critic duty 2 wording.

### Verification
- `test_review_objective_block_677.py`: All 12 unit tests pass.
- **Live Smoke Evaluation (3 runs against OpenRouter via `scripts/live_smoke_eval.py`)**:
  - Run with `OPENROUTER_API_KEY` (`toaster-key`, limit $50):
    1. `control-flipped-indemnity.docx` (Section 13 edited so Facility indemnifies Institution):
       - Result: `status=OK`, `decision=REQUEST_CHANGE`.
       - Finding 0 correctly flagged `indemnification` as a deviation to be remedied.
    2. `synthetic-input.docx` (Section 13 has Institution indemnifying Facility — runs in our favour):
       - Result: Primary reviewer left Section 13 untouched.
       - Critic pass specifically evaluated and commended leaving it untouched:
         > *"The one-sided terms that favor the Facility — Institution-only indemnity, Institution-only insurance burden, unilateral convenience termination for the Facility, Facility ownership of work product, and Facility audit rights — are all correctly left untouched."*
       - Flawlessly demonstrates the #687 prompt sharpening behavior.
    3. `real-corpus-candidate_7259-affiliation-agreement.docx`:
       - Result: `status=MANUAL_REVIEW_REQUIRED`, `reason=block_transcript_rejected`.
       - Generated 5 review findings, but transcript reconstruction across mid-block revisions in merged blocks `p0014` and `p0010` failed closed as designed, reproducing today's incident live.
  - Total eval cost: $1.76 (mean latency: 202s). Full report and dumps persisted locally to `dump/report.json` and `dump/runs/`.

---

## 2. Task 2 — Playbook Version Download

### Objective & Changes
Added the ability for administrators to download stored playbook version artifacts (`.json`) directly from the Version History table via short-lived presigned S3 URLs.

- **[backend/src/download.py](../backend/src/download.py)**:
  - Implemented `generate_presigned_playbook_download_url`:
    - Validates caller is admin (`_is_admin`).
    - Validates storage key is strictly scoped to `playbooks/{playbook_id}/` and prevents path traversal (`..`, `\`).
    - Performs S3 `head_object` check against the uploads bucket; returns HTTP 410 Gone if missing or purged.
    - Generates 60-second presigned URL with `ResponseContentDisposition: attachment; filename="<playbook_id>-v<version>.json"`.
    - Returns response with header `Cache-Control: no-store`.
- **[backend/src/playbook_versions.py](../backend/src/playbook_versions.py)**:
  - Added `get_playbook_version_record(playbook_id, version, dynamodb_resource)`.
  - Added `record_playbook_version_download(...)` which writes an append-only audit entry to `AUDIT_TABLE` (`action="playbook_version_downloaded"`).
- **[backend/src/main.py](../backend/src/main.py)**:
  - Mounted `GET /api/admin/playbooks/{playbook_id}/versions/{version}/download`.
- **[frontend/src/AdminPlaybooks.tsx](../frontend/src/AdminPlaybooks.tsx)**:
  - Added per-row `<CtButton size="sm" variant="secondary">Download</CtButton>` in the Version History table Actions cell.
  - Implemented `downloadVersion(playbookId, version)` handler calling the download route and triggering browser download via `triggerBrowserDownload(url)`.
  - Gracefully handles 403 (forbidden banner) and 410 (friendly error banner indicating the artifact was purged from storage).

### Verification
- **[tests/test_playbook_version_download.py](../tests/test_playbook_version_download.py)**:
  - 6 unit tests covering: non-admin 403, unknown version 404, missing storage_key 404, unscoped key 403, purged S3 object 410, and successful download 200 with presigned URL + audit entry.
- **[frontend/src/__tests__/admin-playbooks.test.tsx](../frontend/src/__tests__/admin-playbooks.test.tsx)**:
  - Unit tests for Download button rendering, download click triggering browser download, and 410 error banner handling.
- **Gate Runs**:
  - `bash scripts/check-frontend.sh`: **`CHECK-FRONTEND: ALL GREEN`** (88 test files, 943 tests, contrast, focus, layout audits).
  - `SKIP_INFRA=1 bash scripts/check.sh`: **`CHECK: ALL GREEN`** (0 errors).
  - `tests/lint-brand-free.py`: **`BRAND-FREE LINT: PASS`**.

---

## 3. Task 3 — Production Incident Diagnosis (`block_transcript_rejected`)

### Document Characterization (Metadata Only)
- **Document**: `scratch/handoff-687-payload/real-corpus-candidate_7259-affiliation-agreement.docx`
- **Result**:
  - Extracted 19 logical blocks.
  - Headings match the production failure report:
    - Block `p0001`: `'Channelside Drive, 12th Floor, Room 1211'`
    - Block `p0004`: `'Overview'`
    - Block `p0007`: `'Joint Responsibilities'`
    - Block `p0010`: `'EEE'` (12 physical paragraphs merged, 1,099 chars)
    - Block `p0012`: `'EE'`
    - Block `p0014`: `'ENSTITUTIONE'` (16 physical paragraphs merged, 8,109 chars)

### Root Cause Analysis
- In `p0014`, 16 physical paragraphs were merged during normalization. The input document contains 7 accepted tracked-change revisions located strictly in the **interior / middle** of the merged block (specifically at physical paragraphs 1, 2, 5, 6, 12, and 14).
- In `p0010`, 12 physical paragraphs were merged, with a mid-block revision at paragraph 9.
- In `scripts/normalize_input.py`, commit `1cd77a4` (#683) resolved trailing-whitespace / trailing-keep reconstruction divergence, but **deliberately left mid-block keep reconstruction failing closed**:
  > Commit `1cd77a4`: *"The trailing-keep divergence is fixed... Mid-block keep reconstruction divergence remains fail-closed until whitespace-claiming rules are codified."*
- When the transcript verification stage attempts to reconcile the reconstructed text with the accepted revisions across internal paragraph boundaries, the mid-block whitespace claim produces an offset mismatch, correctly triggering `block_transcript_rejected` rather than emitting a corrupted contract transcript.

### Recommended Next Steps for Task 3
- Codify the whitespace-claiming rule for intra-block paragraph boundaries in merged blocks (`normalize_input.py`).
- Validate the rule against `p0014` and `p0010` in `real-corpus-candidate_7259-affiliation-agreement.docx` before relaxing the fail-closed assertion.

---

## 4. Deployment Boundary & Status

1. **Git Status**:
   - Commits `d511028` and `70a600e` pushed to `origin/main`.
2. **GHCR Images**:
   - Triggered workflow `dts-image-publish.yml` via GitHub Actions (Run ID `33883928376`).
3. **Coolify Pull Wall**:
   - In accordance with the handoff instructions, pulling the latest images into Coolify behind Cloudflare Access requires Marc's browser session.
   - Once the image build completes, Marc should navigate to Coolify and click **"Restart (pull latest)"** on the **Contract Toaster** service on the Coolify deployment.
