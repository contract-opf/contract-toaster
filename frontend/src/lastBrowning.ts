/**
 * lastBrowning.ts — remember the last-selected markup intensity across a reload.
 *
 * Persists a single BrowningLevel ('light' | 'medium' | 'dark') behind a namespaced
 * localStorage key, following the exact single preference pattern of lastPlaybook.ts,
 * toaster/notify.ts, and toaster/sounds.ts.
 */
import { DEFAULT_BROWNING, type BrowningLevel } from './toaster/browning';

export const LAST_BROWNING_STORAGE_KEY = 'contract-toaster:last-browning';

export function readLastBrowning(): BrowningLevel {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return DEFAULT_BROWNING;
    const stored = window.localStorage.getItem(LAST_BROWNING_STORAGE_KEY);
    if (stored === 'light' || stored === 'medium' || stored === 'dark') {
      return stored;
    }
  } catch {
    /* fallback */
  }
  return DEFAULT_BROWNING;
}

export function writeLastBrowning(level: BrowningLevel): void {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return;
    window.localStorage.setItem(LAST_BROWNING_STORAGE_KEY, level);
  } catch {
    /* best-effort persistence only */
  }
}
