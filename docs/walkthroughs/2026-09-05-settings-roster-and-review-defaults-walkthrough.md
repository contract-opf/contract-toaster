# Walkthrough: Settings Legal Entity Roster & Review Tab Defaults

## Overview
This change accomplishes two key UX and reliability improvements across Contract Toaster:
1. **Legal Entity Roster (Settings Tab)**:
   - Eliminated the roadblock "unavailable / this deployment keeps no entity roster" warning banner.
   - The table, textarea editor, and action buttons (Save, Copy list, Export CSV, Import CSV) are now always established in the Settings tab, even when initially blank, warmly inviting admins to configure subsidiaries, parent companies, and d/b/a entities.
   - On the backend, `_entity_roster_table_name()` defaults to `"contract-toaster-entity-roster-dts"` when `ENTITY_ROSTER_TABLE` is unset in the environment, and automatically creates the DynamoDB table on demand if it does not yet exist.
2. **Review Tab Defaults & Preferences**:
   - Removed the ambiguous "Make this my default" button completely.
   - User choices for **Contract Type** (playbook), **Markup Intensity** (browning level), and **Footnotes** (notes mode) now automatically remember and persist across sessions.
   - Browning level is persisted in client storage via `lastBrowning.ts` and restored on mount.
   - Footnotes mode is persisted in client storage via `lastNotesMode.ts` and asynchronously synced to `/api/me/preferences` in the background on every change.

---

## Changes

### Backend
- [backend/src/entity_roster.py](../../backend/src/entity_roster.py):
  - Defined `DEFAULT_ENTITY_ROSTER_TABLE = "contract-toaster-entity-roster-dts"`.
  - Added `_ensure_table()` helper to auto-create the DynamoDB table with `setting_id` primary key (`PAY_PER_REQUEST` billing mode) if `ResourceNotFoundException` is encountered.
  - Ensured `_stored_row()` and `set_entity_roster()` handle table auto-creation smoothly.
  - `get_entity_roster()` always reports `roster_store_available: True`.
- [tests/test_entity_roster_678.py](../../tests/test_entity_roster_678.py):
  - Updated degradation test to `test_unset_table_env_defaults_to_dts_and_auto_provisions`, verifying that an unset `ENTITY_ROSTER_TABLE` environment variable defaults to the DTS table and auto-provisions table on demand.

### Frontend
- [frontend/src/AdminSettings.tsx](../../frontend/src/AdminSettings.tsx):
  - Removed `<CtBanner data-testid="admin-settings-roster-unavailable">`.
  - When the roster is empty, displays an inviting informational banner (`admin-settings-roster-empty-state`) explaining how recognizing entities works and encouraging additions.
  - The roster table, textarea, and action buttons (Save, Copy, Export, Import) are always rendered.
- [frontend/src/ReviewSubmission.tsx](../../frontend/src/ReviewSubmission.tsx):
  - Removed `<CtButton data-testid="review-notes-mode-remember">Make this my default</CtButton>`.
  - Added `handleBrowningChange` callback that updates state and calls `writeLastBrowning(level)`.
  - Updated `handleNotesModeChange` to save locally (`writeLastNotesMode(mode)`) and trigger `saveNotesModePreference(mode)` in the background.
  - Restores initial state on mount from `readLastBrowning()` and `readLastNotesMode()`.
- [frontend/src/lastBrowning.ts](../../frontend/src/lastBrowning.ts):
  - Created isolated, namespaced storage module for browning (`contract-toaster:last-browning`).
- [frontend/src/lastNotesMode.ts](../../frontend/src/lastNotesMode.ts):
  - Created isolated, namespaced storage module for notes mode (`contract-toaster:last-notes-mode`).

### Test & Posture Alignment
- [frontend/src/__tests__/admin-entity-roster-678.test.tsx](../../frontend/src/__tests__/admin-entity-roster-678.test.tsx):
  - Updated to assert that the roster panel and editor are always established even when empty, and older backend responses are handled gracefully.
- [frontend/src/__tests__/admin-settings-650.test.tsx](../../frontend/src/__tests__/admin-settings-650.test.tsx):
  - Updated capability checks to account for the always-established roster editor on `admin-settings-panel`, while verifying 0 capability controls and buttons on `admin-settings-capabilities-panel`.
- [frontend/src/__tests__/notes-mode-control-523.test.tsx](../../frontend/src/__tests__/notes-mode-control-523.test.tsx):
  - Updated to verify automatic preference persistence to `/api/me/preferences` on mode change without a manual remember button.
- [frontend/src/__tests__/browning-control.test.tsx](../../frontend/src/__tests__/browning-control.test.tsx):
  - Added persistence assertions verifying browning selection is saved and restored on reload.
- [frontend/src/__tests__/security-posture.test.tsx](../../frontend/src/__tests__/security-posture.test.tsx):
  - Registered `lastBrowning.ts` and `lastNotesMode.ts` in `ALLOWED_SETITEM_FILES` and verified dynamic token-safety guarantees.

---

## Verification Results

1. **Backend Tests**:
   - `python -m unittest tests/test_entity_roster_678.py`: 33/33 tests passed in 2.7s.
   - `PYTHON=./.venv/bin/python SKIP_INFRA=1 bash scripts/collect_test_failures.sh .`: `CHECK: ALL GREEN` across all test suites.
2. **Frontend Layout Audit**:
   - `npm run audit:layout`: `LAYOUT AUDIT: ALL GREEN` (enforces >= 14px type scale floor, button alignment, grid tracks).
3. **Frontend Vitest Suite**:
   - `npm test`: 89/89 test files passed (958/958 tests passed in 18.68s).
4. **Production CI Build**:
   - `npm run build:ci`: Clean compile with `tsc && vite build` (1701 modules transformed, 0 errors).
5. **Git Commit & Push**:
   - Commit: `2d32364` (`feat(ui): establish legal entity roster by default and auto-remember review settings`) pushed cleanly to `origin/main`.
6. **GHCR Container Publish**:
   - Workflow: `dts-image-publish.yml` (Run 33967418585).
   - Frontend image: `ghcr.io/${OWNER}/contract-toaster-dts-frontend:latest` / `:2d32364`
     (Digest: `sha256:1946bdee66263ae1661eed02a29f80c4fa85a78053770489db8750055997e57d`)
   - Backend image: `ghcr.io/${OWNER}/contract-toaster-dts-backend:latest` / `:2d32364`
     (Digest: `sha256:fa900348fd679f0c6799f80df61d9b20fc798cc46ff384aadd7698e8d91ca179`)


