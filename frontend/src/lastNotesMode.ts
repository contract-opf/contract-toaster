/**
 * lastNotesMode.ts — remember the last-selected footnote audience across a reload.
 *
 * Persists a single NotesMode ('none' | 'external' | 'internal' | 'both') behind a
 * namespaced localStorage key, following the exact single preference pattern of
 * lastPlaybook.ts, toaster/notify.ts, and toaster/sounds.ts.
 */
import { DEFAULT_NOTES_MODE, isNotesMode, type NotesMode } from './notesMode';

export const LAST_NOTES_MODE_STORAGE_KEY = 'contract-toaster:last-notes-mode';

export function readLastNotesMode(): NotesMode {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return DEFAULT_NOTES_MODE;
    const stored = window.localStorage.getItem(LAST_NOTES_MODE_STORAGE_KEY);
    if (isNotesMode(stored)) {
      return stored;
    }
  } catch {
    /* fallback */
  }
  return DEFAULT_NOTES_MODE;
}

export function writeLastNotesMode(mode: NotesMode): void {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return;
    window.localStorage.setItem(LAST_NOTES_MODE_STORAGE_KEY, mode);
  } catch {
    /* best-effort persistence only */
  }
}
