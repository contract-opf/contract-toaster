/**
 * notify.ts — the opt-in "toast's ready" browser Notification (issue #497).
 *
 * The ding (`sounds.ts`'s `playPop`/`playClunk`, already wired into
 * `ReviewSubmission.tsx`'s phase effect) needs no permission and plays
 * unconditionally on every terminal review, tab hidden or not — that part of
 * "tell someone who tabbed away" already worked before this file existed.
 * What did not exist is a browser Notification, which — unlike a sound this
 * app bundles and owns — requires the browser's OWN permission prompt. This
 * module is only that second, optional layer.
 *
 * ## The permission rule this module exists to enforce
 *
 * `Notification.requestPermission()` is called from exactly ONE place below:
 * `useNotifyPreference`'s `toggle`, and only on the branch where the caller
 * is turning the preference ON. Never on mount, never from a poll callback,
 * never speculatively — an unprompted permission dialog is worse than no
 * notification at all, and the acceptance criterion this satisfies is literal
 * ("requesting permission only on that click"). Denial (or the user just
 * dismissing the browser prompt) leaves the stored preference OFF and shows
 * no error: the reviewer already has the ding, so silence is the honest
 * degrade, not a failure state.
 *
 * ## Why `notifyEnabled` re-checks `Notification.permission` every time
 *
 * The stored flag only ever records the user's own opt-in click. It is not
 * re-synchronized if they later revoke the permission from the browser's own
 * site-settings UI — this module has no event for that. So every firing
 * decision re-reads `Notification.permission` live rather than trusting the
 * stored flag alone; a revoked permission silently stops notifications
 * without this module ever "knowing" the user changed their mind, which is
 * the correct outcome either way.
 *
 * ## Storage
 *
 * A namespaced localStorage key, the same shape issue #489 documents for the
 * sound-mute flag ("Nothing sensitive lands in localStorage"): this stores a
 * single boolean preference, never a token, never anything about the review
 * itself. It is deliberately its OWN key, independent of #489's — that issue
 * has not landed yet, so there is no shared "preferences" object to fold
 * this into without reaching outside this ticket's scope.
 *
 * ## The notification still FIRES while muted; it just stays quiet
 *
 * The issue text's acceptance criteria give this module's firing rule
 * explicitly as a closed AND-list: opted in, permission granted, tab hidden,
 * terminal state — full stop; mute is not a fifth gate, so a reviewer who
 * muted the toaster's sound effects for a completely unrelated reason still
 * gets the visual OS notification. But `new Notification(title)` is NOT
 * silent by default — `Notification.prototype.silent` defaults to `false`,
 * so Chrome, Edge, and Firefox hand the notification to the OS, which plays
 * ITS OWN system alert sound. Left alone, that is an audible ding for a
 * reviewer who pressed "Sound off" — exactly the "noise" the other
 * acceptance-criteria bullet's "everything respects mute" is about. So the
 * notification is constructed with `{ silent: isMuted() }` (`./sounds`'s
 * exported mute flag): the visual layer still fires for a muted reviewer
 * with the tab in the background, but the OS-level sound that would
 * otherwise come with it is suppressed.
 */
import { useCallback, useState } from 'react';
import { isMuted } from './sounds';

export const NOTIFY_STORAGE_KEY = 'contract-toaster:notify-on-done';

function hasNotificationApi(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window;
}

function readStoredOptIn(): boolean {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return false;
    return window.localStorage.getItem(NOTIFY_STORAGE_KEY) === '1';
  } catch {
    // A localStorage read that throws (private-mode quirks, a locked-down
    // embed) just means "not opted in this load" — never a reason to break
    // the toggle or the review flow around it.
    return false;
  }
}

function writeStoredOptIn(value: boolean): void {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return;
    if (value) window.localStorage.setItem(NOTIFY_STORAGE_KEY, '1');
    else window.localStorage.removeItem(NOTIFY_STORAGE_KEY);
  } catch {
    /* best-effort persistence; see readStoredOptIn's comment */
  }
}

/**
 * The live, re-checked-every-time answer to "may a notification fire right
 * now" — everything EXCEPT the tab-hidden and terminal-state gates, which
 * `notifyToastDone` below checks itself since only its caller knows whether
 * the review is actually terminal.
 */
function notifyEnabled(): boolean {
  return hasNotificationApi() && Notification.permission === 'granted' && readStoredOptIn();
}

/** Whether this environment could ever grant this preference — the caller
 *  uses this to decide whether to render the opt-in control at all, rather
 *  than showing an affordance that can never do anything (many mobile
 *  browsers, and any embed with the API stripped, have no `Notification`). */
export function notificationsSupported(): boolean {
  return hasNotificationApi();
}

/**
 * Hook for the opt-in control beside the sound toggle (same `{ x, toggle }`
 * shape as `sounds.ts`'s `useSoundMuted`, so the two controls read as a
 * matched pair in `ReviewSubmission.tsx`).
 */
export function useNotifyPreference(): { optedIn: boolean; toggle: () => void } {
  const [optedIn, setOptedIn] = useState<boolean>(() => notifyEnabled());

  const toggle = useCallback(() => {
    if (optedIn) {
      // Turning off never needs permission and never fails.
      writeStoredOptIn(false);
      setOptedIn(false);
      return;
    }
    if (!hasNotificationApi()) {
      return; // Nothing this control can do in an environment with no API.
    }
    if (Notification.permission === 'granted') {
      writeStoredOptIn(true);
      setOptedIn(true);
      return;
    }
    if (Notification.permission === 'denied') {
      // Already refused at the browser level — requesting again would not
      // even show a prompt in any real browser. Stay off, silently.
      return;
    }
    // The only branch that may show the browser's permission dialog, and it
    // is reached only from this click.
    void Notification.requestPermission().then((permission) => {
      if (permission === 'granted') {
        writeStoredOptIn(true);
        setOptedIn(true);
      }
      // 'denied' or the prompt was dismissed ('default'): degrade silently,
      // per this module's docstring — no banner, no retry nag.
    });
  }, [optedIn]);

  return { optedIn, toggle };
}

/**
 * Fire the "toast's ready" Notification for a review that just went
 * terminal — called from `ReviewSubmission.tsx`'s existing phase effect
 * (the same place `playPop`/`playClunk` already live), which is the only
 * caller that knows both the real terminal outcome and can pass it along.
 *
 * All four gates the acceptance criteria name are checked HERE, in one
 * place, so no call site can accidentally satisfy only three of them:
 * opted in, permission granted (`notifyEnabled`), the tab actually hidden,
 * and — by construction, since the caller only reaches this from the done/
 * error branch of its own terminal check — a genuinely terminal state.
 *
 * `outcomeLabel` is the shared outcome map's label (issue #470) lowercased,
 * never a filename — the body this constructs can name what happened to the
 * review, never what the document was called (screen-lock privacy, per the
 * issue).
 */
export function notifyToastDone(outcome: { failed: boolean; outcomeLabel: string | null }): void {
  if (!notifyEnabled()) return;
  if (typeof document === 'undefined' || document.visibilityState !== 'hidden') return;

  const title = outcome.failed
    ? "That one burnt — tap for why"
    : `Toast's ready — ${(outcome.outcomeLabel ?? 'ready').toLowerCase()}`;

  try {
    // silent: isMuted() — see the module docstring's "still FIRES while
    // muted" section: the visual notification always fires once the four
    // gates above hold, but a muted reviewer must not get an audible OS
    // alert alongside it (Notification's `silent` defaults to false).
    const notification = new Notification(title, { silent: isMuted() });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch {
    // A Notification constructor can throw in some locked-down embeds or
    // mid-permission-change races; a notification is decoration, and must
    // never take the review flow down with it.
  }
}
