# Implementation Plan: Handoff Orientation, Deploy, and Three Tasks

**Date**: 2026-09-04  
**Workspace**: `exos-legal/contract-toaster`  
**Base Commit**: `1301e09` (cleanly fast-forwarded from `origin/main`)  
**Gate Baseline**: `SKIP_INFRA=1 bash scripts/check.sh` -> **CHECK: ALL GREEN**  

---

## Overview

This plan covers the three tasks handed off on 2026-09-04, incorporating the verified local environment, baseline test status, offline docx characterization, and deployment constraints:

1. **Task 1 — Issue #687**: Sharpen the primary objective block and critic duty to enforce the rule that the counterparty's uploaded document is their accepted position; never narrow, soften, balance, or mutualize any term running in our favour.
2. **Task 2 — Playbook Version Download**: Add backend presigned download endpoint and frontend "Download" action under Version history.
3. **Task 3 — Production Incident Diagnosis (`block_transcript_rejected`)**: Offline characterization of `real-corpus-candidate_7259-affiliation-agreement.docx`, confirming mid-block tracked-change acceptance across large merged blocks (`p0014` and `p0010`), reproducing the exact predicted trigger left by commit `1cd77a4` (#683).

---

## User Review Required

> [!IMPORTANT]
> **Task 1 Live Verification**: Live evaluation requires `OPENROUTER_API_KEY`. As observed, `OPENROUTER_API_KEY` is not currently set in the environment. Once Marc provides the key, we will run the 3-run evaluation (1 synthetic + 1 real corpus candidate + 1 flipped-indemnity control). Prompt-pin tests will be landed and verified immediately offline.

> [!IMPORTANT]
> **Task 3 Incident Decision**: Offline characterization has confirmed that `real-corpus-candidate_7259-affiliation-agreement.docx` matches today's failed upload identically. It contains 19 blocks, including `p0014` (16 physical paragraphs merged, 8,109 characters) with 7 mid-block tracked changes, and `p0010` (12 physical paragraphs merged, 1,099 characters) with 1 mid-block tracked change. Commit `1cd77a4` (#683) explicitly left mid-block keep reconstruction unfixed because a general anchor-based design failed on whitespace claiming (double spaces and deleted space runs). We report this diagnosis and seek Marc's alignment on the whitespace rule before modifying mid-block segment placement.

---

## Decisions & Resolved Questions

1. **Task 2 Audit Trail**: Confirmed — standard `playbook_version_downloaded` append-only audit entry in `AUDIT_TABLE` is sufficient.
2. **Task 2 Download Placement**: Confirmed — placed per-row in the Version History table's Actions column.

---

## Proposed Changes

### Task 1: Issue #687 — Counterparty's Accepted Position

#### [MODIFY] [scripts/primary_review_pass.py](../../scripts/primary_review_pass.py)
- In `render_review_guidance_block(party, counterparty_type)`:
  - Sharpen the guidance block so that both v1 and OPF prompt paths receive the explicit rule:
    ```python
    "The document you are reviewing has already been reviewed by the "
    "counterparty and reflects terms they accept. A term that favours us "
    "is therefore a term the other side has agreed to: leave it exactly "
    "as written. Do not narrow it, balance it, make it mutual, or improve "
    "its drafting, whatever the drafting argument, even where you would "
    "have drafted it differently.\n"
    ```
- In `_CRITIC_TASKING_DUTIES_1_TO_3`:
  - Sharpen Duty 2 (`2. OVER-FLAGGING AND GIVING TERMS AWAY`):
    - Explicitly state that a term running in our favour reflects terms the counterparty has agreed to; any edit that narrows, balances, or mutualizes it concedes an accepted position and must be contested in `critic_delta.rationale_objections`.

#### [MODIFY] [tests/test_review_objective_block_677.py](../../tests/test_review_objective_block_677.py)
- Add prompt-pin assertions for the new sentences:
  - Assert presence of `"reflects terms they accept"`
  - Assert presence of `"whatever the drafting argument"`
  - Assert presence of `"make it mutual"`
  - Assert presence of the sharpened Duty 2 wording in `CRITIC_TASKING_BLOCK`.

---

### Task 2: Playbook Version Download

#### [MODIFY] [backend/src/main.py](../../backend/src/main.py)
- Mount `GET /api/admin/playbooks/{playbook_id}/versions/{version}/download`:
  - Require admin (`_is_admin(caller_row)`).
  - Retrieve version record using `_get_version_item(playbook_id, version, dynamodb_resource)`. If not found, return 404.
  - Verify `storage_key` is present on the record.
  - Probe object existence in `config.uploads_bucket()` via `s3_client.head_object`. If missing / 404, return HTTP 410 Gone ("This playbook version is no longer available in storage.").
  - Generate presigned GET URL with TTL 60s using `config.presigning_s3_client_kwargs()` (supporting MinIO/S3 public endpoints).
  - Include `ResponseContentDisposition` with filename e.g. `{playbook_id}-v{version}.json`.
  - Return `{"url": presigned_url, "expires_in": 60, "playbook_id": playbook_id, "version": version}` with `Cache-Control: no-store`.

#### [NEW] [tests/test_playbook_version_download.py](../../tests/test_playbook_version_download.py)
- Comprehensive unit tests:
  - Admin gate: non-admin gets 403.
  - Unknown version: gets 404.
  - Missing storage object: gets 410 Gone.
  - Success: returns 200 with presigned URL and `Cache-Control: no-store`.

#### [MODIFY] [frontend/src/AdminPlaybooks.tsx](../../frontend/src/AdminPlaybooks.tsx)
- Add "Download" button to the Version History overlay table actions:
  - Add download action alongside existing version actions.
  - On click, call `/api/admin/playbooks/${playbookId}/versions/${version}/download`.
  - On 200, trigger browser download.
  - On 410, display inline error indicator that the version file has been purged / is unavailable.

---

### Task 3: Production Incident Diagnosis (`block_transcript_rejected`)

#### Offline Characterization Summary
- **Target document**: `scratch/handoff-687-payload/real-corpus-candidate_7259-affiliation-agreement.docx`
- **Result**:
  - Successfully extracted and normalized: 19 logical blocks, 3,212 normalization note items across multiple physical paragraphs.
  - Heading match: Exact matches for `'Channelside Drive, 12th Floor, Room 1211'`, `'Overview'`, `'Joint Responsibilities'`, `'EEE'`, `'EE'`, and `'ENSTITUTIONE'`.
  - Block `p0014` (`'ENSTITUTIONE'`): 16 physical paragraphs merged into a single block of 8,109 characters. Revisions accepted in physical paragraphs 0 through 15, with multiple revisions occurring strictly in the **middle** of the block (paragraphs 1, 2, 5, 6, 12, 14).
  - Block `p0010` (`'Joint Responsibilities'`): 12 physical paragraphs merged into 1,099 characters, with a tracked change at paragraph 9 (mid-block).
  - **Root Cause Confirmation**: Confirmed the exact predicted scenario from issue #683: commit `1cd77a4` specifically fixed only trailing keep reconstruction (`placements[-1][1] = nb`), leaving mid-block keep divergences to fail closed with `block_transcript_rejected`.

---

## Verification Plan

### Automated Tests
1. Unit tests for Task 1:
   ```bash
   .venv/bin/python tests/test_review_objective_block_677.py
   ```
2. Unit tests for Task 2:
   ```bash
   .venv/bin/python tests/test_playbook_version_download.py
   ```
3. Full gate verification:
   ```bash
   SKIP_INFRA=1 bash scripts/check.sh
   ```

### Live Verification (Task 1)
Upon provision of `OPENROUTER_API_KEY`:
```bash
mkdir -p verify_687/synthetic verify_687/real verify_687/control
cp scratch/handoff-687-payload/synthetic-input.docx verify_687/synthetic/
cp scratch/handoff-687-payload/real-corpus-candidate_7259-affiliation-agreement.docx verify_687/real/
# Control: synthetic input with Section 13 reversed
python3 scripts/live_smoke_eval.py verify_687/synthetic --playbook-id synthetic-generic --out synthetic.json --dump-dir dump/synthetic --yes
python3 scripts/live_smoke_eval.py verify_687/real --playbook-id synthetic-generic --out real.json --dump-dir dump/real --yes
python3 scripts/live_smoke_eval.py verify_687/control --playbook-id synthetic-generic --out control.json --dump-dir dump/control --yes
```
Persist dump artifacts to `~/Documents/dev/contract-toaster-handoff/redlines/real/` (if accessible) or `scratch/dump/real/`.
