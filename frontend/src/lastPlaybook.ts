/**
 * lastPlaybook.ts — remember the last-selected contract type across a reload
 * (issue #489, item 4).
 *
 * With several playbooks installed (#485), `ReviewSubmission.tsx`'s dial used
 * to reset to the catalog default on every load — a reviewer whose work is
 * mostly one contract type had to re-select it every session. This module is
 * the one, narrow persistence seam for that: a single playbook id string
 * behind a namespaced localStorage key, the same shape `toaster/notify.ts`
 * documents for its own opt-in flag and `toaster/sounds.ts` documents for the
 * mute flag — one boolean-or-id preference per key, never a token, never
 * anything about a review's content.
 *
 * `ReviewSubmission.tsx` is the only caller: it seeds `playbookId`'s initial
 * state from `readLastPlaybookId()` and persists every change with
 * `writeLastPlaybookId()`. The stored id is never trusted blindly — the
 * existing catalog-sync logic (issue #464) already falls back to the first
 * active playbook when the current selection (stored or not) is no longer a
 * loaded, active entry, so a playbook removed by an admin since the last
 * visit degrades silently to the default rather than erroring (issue #489
 * acceptance criteria).
 */

export const LAST_PLAYBOOK_STORAGE_KEY = 'contract-toaster:last-playbook';

/** Best-effort read: any Storage failure (private-mode quirks, a
 *  locked-down embed with no `window.localStorage`) just means "nothing
 *  remembered" — never a reason to break the dial. */
export function readLastPlaybookId(): string | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return null;
    return window.localStorage.getItem(LAST_PLAYBOOK_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Best-effort write; same failure posture as the read above. */
export function writeLastPlaybookId(playbookId: string): void {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return;
    window.localStorage.setItem(LAST_PLAYBOOK_STORAGE_KEY, playbookId);
  } catch {
    /* best-effort persistence only */
  }
}
