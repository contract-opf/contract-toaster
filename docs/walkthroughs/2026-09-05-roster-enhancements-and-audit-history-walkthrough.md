# Walkthrough: Legal Entity Roster Usability, Change History, and Footnote Retry

## Overview
This work implements three targeted enhancements across Contract Toaster to elevate administrator usability, audit trail transparency, and preference error recovery:
1. **Entity Roster Client-Side Usability**:
   - Real-time duplicate entity detection in the raw textarea with warning alerts.
   - Dynamic distinct entity count pill (`admin-settings-roster-count`) updating as admins type.
   - Real-time filter search box (`admin-settings-roster-filter`) and clear button above the recognized entities table.
2. **Entity Roster Change History**:
   - Stores chronological change diffs (`added`, `removed`, `actor`, `timestamp`, `count_before`, `count_after`) capped at 25 entries in DynamoDB.
   - Renders an expandable `<details>` section (`admin-settings-roster-history-details`) in the Settings tab with timestamp, actor, and color-coded `+ Added` and `- Removed` badges.
   - Maintains audit isolation: high-frequency deployment audits in `AUDIT_TABLE` record change metadata without entity names, preserving privacy.
3. **Footnote Preference Save Retry**:
   - Adds an inline "Retry" button (`review-notes-mode-retry`) to the footnote preference save error banner in the Review tab.
   - Allows users to retry saving to `/api/me/preferences` without losing their selection or reloading.

---

## Changes

### Backend
- [backend/src/entity_roster.py](../../backend/src/entity_roster.py):
  - Defined `MAX_HISTORY_ENTRIES = 25`.
  - In `set_entity_roster()`, computes `added` and `removed` sets against existing entities, constructs a `history_entry`, prepends it to the historical diff list, and caps the list at 25 entries.
  - In `get_entity_roster()`, projects the `history` array in the response schema.
  - Retains immutable `_write_audit_entry()` to `AUDIT_TABLE` without entity names.
- [tests/test_entity_roster_678.py](../../tests/test_entity_roster_678.py):
  - Added unit test `test_saves_record_chronological_history_with_added_and_removed_diffs` verifying chronological history diffing and cap enforcement.
  - 34/34 tests passed.

### Frontend
- [frontend/src/AdminSettings.tsx](../../frontend/src/AdminSettings.tsx):
  - Exported `EntityRosterHistoryEntry` interface and extended `EntityRosterSettings`.
  - Added memoized entity line parser to detect duplicate lines and distinct count.
  - Rendered `admin-settings-roster-count` badge and `admin-settings-roster-duplicate-notice` alert.
  - Added table search filter `admin-settings-roster-filter` with clear button.
  - Added expandable change history log (`admin-settings-roster-history-details`) with formatted timestamps, actors, and color-coded `+ Added` and `- Removed` chips.
  - Maintained `>= 14px` type scale floor for Issue #600 across all new elements.
- [frontend/src/ReviewSubmission.tsx](../../frontend/src/ReviewSubmission.tsx):
  - Added inline `<CtButton variant="ghost" size="sm" data-testid="review-notes-mode-retry">Retry</CtButton>` inside the `notesModeSaveError` banner.
- [frontend/src/__tests__/admin-entity-roster-678.test.tsx](../../frontend/src/__tests__/admin-entity-roster-678.test.tsx):
  - Added test cases verifying distinct counter badge, duplicate warning notice, search filter matching/clearing, and expandable change history rendering.
  - 15/15 tests passed.
- [frontend/src/__tests__/notes-mode-control-523.test.tsx](../../frontend/src/__tests__/notes-mode-control-523.test.tsx):
  - Added test case verifying the retry button appears on preference save error and retries the PUT request.
  - 19/19 tests passed.

---

## Verification Results

1. **Frontend Production Build**:
   - `npm --prefix frontend run build:ci` passed cleanly (`tsc && vite build`).
2. **Layout & A11y Audits**:
   - `npm --prefix frontend run audit:layout`: `LAYOUT AUDIT: ALL GREEN` (zero violations of type scale floor or CSS layout constraints).
3. **Frontend Test Suite**:
   - `npm --prefix frontend test`: 89/89 test files passed, 962/962 tests passed.
4. **Backend Tests & Linters**:
   - `./.venv/bin/python tests/lint-brand-free.py`: `BRAND-FREE LINT: PASS`.
   - `./.venv/bin/python tests/test_entity_roster_678.py`: 34/34 tests passed.
5. **Git Commit & Push**:
   - Commits `13fe5d1` and `c120ca8` pushed cleanly to `origin/main`.
6. **CI Pipeline Gates**:
   - Workflow run `33978368531`:
     - Frontend suite (typecheck + build + vitest + CTDS audits): `✓` passed in 1m13s.
     - Docs lint: `✓` passed.
     - Detector-correctness gate: `✓` passed.
     - Security and dependency scan: `✓` passed.
7. **GHCR Container Publish**:
   - Workflow: `Publish Docker Compose images` (`dts-image-publish.yml`, Run `33978472665`).
   - Frontend image: `ghcr.io/${OWNER}/contract-toaster-dts-frontend:latest` / `:c120ca8`
     (Digest: `sha256:73617075ee84b4280778d10003a95bbf790d87ec7ef2e5c6910c06feb69449fd`)
   - Backend image: `ghcr.io/${OWNER}/contract-toaster-dts-backend:latest` / `:c120ca8`
     (Digest: `sha256:849f28c9e28aa6355e15f66058a51eca456d99b1bb01ca6b8d0fb705703fcf9a`)

