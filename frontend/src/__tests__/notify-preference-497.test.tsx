/**
 * notify-preference-497.test.tsx — the opt-in "toast's ready" browser
 * Notification (issue #497), `toaster/notify.ts`.
 *
 * jsdom has no `Notification` constructor at all (confirmed against the
 * installed jsdom directly — there is nothing to feature-detect around in
 * this suite otherwise), so every "supported" test here installs a
 * hand-rolled mock via `vi.stubGlobal('Notification', ...)`, mirroring
 * `sounds.test.tsx`'s hand-rolled `AudioContext` for the identical reason.
 * The "unsupported" tests delete it, which is real coverage of the actual
 * production condition on browsers that ship no Notification API at all.
 *
 * The module is reloaded (`vi.resetModules()`) before every test so
 * `readStoredOptIn`'s localStorage read never observes a previous test's
 * write.
 *
 * `localStorage` itself is a hand-rolled in-memory stand-in
 * (`installMockLocalStorage`) rather than jsdom's real one: real jsdom
 * Storage works fine, but only when Node's OWN newer, competing global
 * `localStorage` is disabled (`package.json`'s `test` script passes
 * `NODE_OPTIONS=--no-experimental-webstorage` for exactly this) — a flag
 * this file should not have to depend on to be deterministic under a bare
 * `vitest run`. Same reasoning as `sounds.test.tsx`'s hand-rolled
 * `AudioContext`.
 *
 * Fully offline and deterministic: no real permission dialog, no real
 * network, no timers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

type NotifyModule = typeof import('../toaster/notify');

type Permission = 'default' | 'granted' | 'denied';

/** A minimal in-memory `Storage` — see the module docstring above for why
 *  this sandbox needs one at all. */
function installMockLocalStorage(): Storage {
  const store = new Map<string, string>();
  const storage: Storage = {
    getItem: (key: string) => (store.has(key) ? (store.get(key) as string) : null),
    setItem: (key: string, value: string) => {
      store.set(key, value);
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => {
      store.clear();
    },
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  } as Storage;
  vi.stubGlobal('localStorage', storage);
  return storage;
}

class MockNotification {
  static permission: Permission = 'default';
  static requestPermission = vi.fn(async () => MockNotification.permission);
  static instances: MockNotification[] = [];
  title: string;
  options: NotificationOptions | undefined;
  onclick: (() => void) | null = null;
  closed = false;
  constructor(title: string, options?: NotificationOptions) {
    this.title = title;
    this.options = options;
    MockNotification.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
}

function installNotificationApi(permission: Permission = 'default'): void {
  MockNotification.permission = permission;
  MockNotification.requestPermission = vi.fn(async () => MockNotification.permission);
  MockNotification.instances = [];
  vi.stubGlobal('Notification', MockNotification);
}

function removeNotificationApi(): void {
  vi.stubGlobal('Notification', undefined);
  // vi.stubGlobal(name, undefined) still leaves the KEY present on
  // globalThis (value undefined), but this module's own gate is `'Notification'
  // in window` — delete it outright so that check behaves exactly like the
  // real unsupported browsers it exists for.
  delete (window as unknown as Record<string, unknown>).Notification;
}

async function loadNotify(): Promise<NotifyModule> {
  vi.resetModules();
  return import('../toaster/notify');
}

beforeEach(() => {
  installMockLocalStorage();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #497 — unsupported environments degrade to a safe no-op', () => {
  it('notificationsSupported is false, and nothing throws', async () => {
    removeNotificationApi();
    const notify = await loadNotify();
    expect(notify.notificationsSupported()).toBe(false);

    const { result } = renderHook(() => notify.useNotifyPreference());
    expect(result.current.optedIn).toBe(false);
    expect(() => act(() => result.current.toggle())).not.toThrow();
    expect(() => notify.notifyToastDone({ failed: false, outcomeLabel: 'Accepted' })).not.toThrow();
  });
});

describe('issue #497 — permission is requested ONLY from the opt-in click', () => {
  it('turning the preference ON with permission still "default" calls requestPermission exactly once', async () => {
    installNotificationApi('default');
    const notify = await loadNotify();
    const { result } = renderHook(() => notify.useNotifyPreference());
    expect(result.current.optedIn).toBe(false);
    expect(MockNotification.requestPermission).not.toHaveBeenCalled();

    await act(async () => {
      result.current.toggle();
      await Promise.resolve();
    });

    expect(MockNotification.requestPermission).toHaveBeenCalledTimes(1);
  });

  it('a GRANTED response persists the preference and flips the toggle on', async () => {
    installNotificationApi('default');
    MockNotification.requestPermission = vi.fn(async () => 'granted' as Permission);
    const notify = await loadNotify();
    const { result } = renderHook(() => notify.useNotifyPreference());

    await act(async () => {
      result.current.toggle();
      await Promise.resolve();
    });

    expect(result.current.optedIn).toBe(true);
    expect(localStorage.getItem(notify.NOTIFY_STORAGE_KEY)).toBe('1');
  });

  it('a DENIED response degrades silently: no persisted opt-in, toggle stays off, no throw', async () => {
    installNotificationApi('default');
    MockNotification.requestPermission = vi.fn(async () => 'denied' as Permission);
    const notify = await loadNotify();
    const { result } = renderHook(() => notify.useNotifyPreference());

    await act(async () => {
      result.current.toggle();
      await Promise.resolve();
    });

    expect(result.current.optedIn).toBe(false);
    expect(localStorage.getItem(notify.NOTIFY_STORAGE_KEY)).toBeNull();
  });

  it('permission already denied at the browser level: toggle never re-prompts', async () => {
    installNotificationApi('denied');
    const notify = await loadNotify();
    const { result } = renderHook(() => notify.useNotifyPreference());

    act(() => result.current.toggle());

    expect(MockNotification.requestPermission).not.toHaveBeenCalled();
    expect(result.current.optedIn).toBe(false);
  });

  it('permission already granted: toggle ON persists immediately, no prompt needed', async () => {
    installNotificationApi('granted');
    const notify = await loadNotify();
    const { result } = renderHook(() => notify.useNotifyPreference());

    act(() => result.current.toggle());

    expect(MockNotification.requestPermission).not.toHaveBeenCalled();
    expect(result.current.optedIn).toBe(true);
    expect(localStorage.getItem(notify.NOTIFY_STORAGE_KEY)).toBe('1');
  });

  it('toggling OFF never requests permission and always succeeds', async () => {
    installNotificationApi('granted');
    const notify = await loadNotify();
    // Seed storage AFTER loading the module (so this test can use its real
    // exported key) but BEFORE the hook's lazy initial read.
    localStorage.setItem(notify.NOTIFY_STORAGE_KEY, '1');
    const { result } = renderHook(() => notify.useNotifyPreference());
    expect(result.current.optedIn).toBe(true);

    act(() => result.current.toggle());

    expect(result.current.optedIn).toBe(false);
    expect(MockNotification.requestPermission).not.toHaveBeenCalled();
    expect(localStorage.getItem(notify.NOTIFY_STORAGE_KEY)).toBeNull();
  });
});

describe('issue #497 — notifyToastDone fires only when every gate holds', () => {
  async function primeOptedIn(): Promise<NotifyModule> {
    installNotificationApi('granted');
    const notify = await loadNotify();
    const { result } = renderHook(() => notify.useNotifyPreference());
    act(() => result.current.toggle());
    expect(result.current.optedIn).toBe(true);
    return notify;
  }

  function setHidden(hidden: boolean): void {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => (hidden ? 'hidden' : 'visible'),
    });
  }

  it('not opted in: no Notification is constructed even if permission is granted and the tab is hidden', async () => {
    installNotificationApi('granted');
    const notify = await loadNotify();
    setHidden(true);

    notify.notifyToastDone({ failed: false, outcomeLabel: 'Accepted' });

    expect(MockNotification.instances).toHaveLength(0);
  });

  it('opted in but the tab is VISIBLE: no notification (the reviewer is already looking at the result)', async () => {
    const notify = await primeOptedIn();
    setHidden(false);

    notify.notifyToastDone({ failed: false, outcomeLabel: 'Accepted' });

    expect(MockNotification.instances).toHaveLength(0);
  });

  it('opted in, tab hidden, but permission was revoked since opting in: no notification', async () => {
    const notify = await primeOptedIn();
    setHidden(true);
    MockNotification.permission = 'denied'; // revoked at the browser level

    notify.notifyToastDone({ failed: false, outcomeLabel: 'Accepted' });

    expect(MockNotification.instances).toHaveLength(0);
  });

  it('every gate satisfied for a DONE outcome: fires with the outcome label, never a filename', async () => {
    const notify = await primeOptedIn();
    setHidden(true);

    notify.notifyToastDone({ failed: false, outcomeLabel: 'Changes requested' });

    expect(MockNotification.instances).toHaveLength(1);
    expect(MockNotification.instances[0].title).toBe("Toast's ready — changes requested");
  });

  it('every gate satisfied for an ERROR outcome: the fixed burnt-toast phrase', async () => {
    const notify = await primeOptedIn();
    setHidden(true);

    notify.notifyToastDone({ failed: true, outcomeLabel: null });

    expect(MockNotification.instances).toHaveLength(1);
    expect(MockNotification.instances[0].title).toBe('That one burnt — tap for why');
  });

  it('clicking the notification focuses the window and closes it', async () => {
    const notify = await primeOptedIn();
    setHidden(true);
    const focusSpy = vi.spyOn(window, 'focus').mockImplementation(() => {});

    notify.notifyToastDone({ failed: false, outcomeLabel: 'Accepted' });
    const created = MockNotification.instances[0];
    created.onclick?.();

    expect(focusSpy).toHaveBeenCalledTimes(1);
    expect(created.closed).toBe(true);

    focusSpy.mockRestore();
  });

  // notify.ts imports `./sounds` internally, and `primeOptedIn` -> `loadNotify`
  // calls `vi.resetModules()`, so the ONLY way to control the mute flag the
  // freshly-loaded `notify` module actually reads is to import
  // `../toaster/sounds` again here (no resetModules in between) so it
  // resolves to that same fresh module instance rather than one cached from
  // an earlier test.
  async function loadSoundsForCurrentEpoch(): Promise<typeof import('../toaster/sounds')> {
    return import('../toaster/sounds');
  }

  it('still constructs the Notification while muted (mute is not a fifth gate), with silent: true', async () => {
    const notify = await primeOptedIn();
    setHidden(true);
    const sounds = await loadSoundsForCurrentEpoch();
    sounds.setMuted(true);

    notify.notifyToastDone({ failed: false, outcomeLabel: 'Accepted' });

    expect(MockNotification.instances).toHaveLength(1);
    expect(MockNotification.instances[0].options).toEqual({ silent: true });
  });

  it('constructs the Notification with silent: false when sound is not muted', async () => {
    const notify = await primeOptedIn();
    setHidden(true);
    const sounds = await loadSoundsForCurrentEpoch();
    sounds.setMuted(false);

    notify.notifyToastDone({ failed: false, outcomeLabel: 'Accepted' });

    expect(MockNotification.instances).toHaveLength(1);
    expect(MockNotification.instances[0].options).toEqual({ silent: false });
  });
});
