/**
 * review-tab-and-notify-497.test.tsx — issue #497 wired into the REAL
 * `ReviewSubmission` panel (unlike `favicon-tab-theater-497.test.tsx` and
 * `notify-preference-497.test.tsx`, which exercise `tabChrome.ts`/`notify.ts`
 * in isolation). This file only needs to prove the WIRING: that the phase
 * effect already driving `playPop`/`playClunk` (issues #448/#501) now also
 * drives `useTabTheater` and `notifyToastDone` with the real `detail` a
 * poll actually returned — not a second, hand-built copy of the state chart.
 *
 * `document.head` is seeded with the same two `<link rel="icon">` elements
 * `index.html` ships (this test file's jsdom document otherwise starts with
 * neither, since RTL's `render` mounts into a bare `document.body`), and a
 * hand-rolled `Notification`/`localStorage` are installed for the same
 * reasons the two unit-test files above document.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { FAVICON_BADGE_DONE, FAVICON_BADGE_FAILED } from '../toaster/faviconFrames';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

type Permission = 'default' | 'granted' | 'denied';

class MockNotification {
  static permission: Permission = 'granted';
  static requestPermission = vi.fn(async () => MockNotification.permission);
  static instances: MockNotification[] = [];
  title: string;
  onclick: (() => void) | null = null;
  closed = false;
  constructor(title: string) {
    this.title = title;
    MockNotification.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
}

function installMockLocalStorage(): void {
  const store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => (store.has(key) ? (store.get(key) as string) : null),
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  } as Storage);
}

/**
 * Resets the two `<link rel="icon">` elements to their default hrefs rather
 * than removing and recreating them. `tabChrome.ts` captures these DOM
 * NODES once, at module scope (exactly as it will in production, where
 * `index.html` ships them once) — this file does not `vi.resetModules()`
 * between tests (it is testing the real, singleton-module wiring, not the
 * module in isolation), so replacing the elements would leave that capture
 * pointing at detached nodes from a previous test while every assertion
 * queries the live (new) ones, and every favicon assertion after the first
 * test would silently observe nothing changing.
 */
function installIconLinks(): void {
  let svg = document.head.querySelector<HTMLLinkElement>('link[rel="icon"][data-kind="svg"]');
  let ico = document.head.querySelector<HTMLLinkElement>('link[rel="icon"][data-kind="ico"]');
  if (!svg) {
    svg = document.createElement('link');
    svg.setAttribute('rel', 'icon');
    svg.setAttribute('data-kind', 'svg');
    document.head.append(svg);
  }
  if (!ico) {
    ico = document.createElement('link');
    ico.setAttribute('rel', 'icon');
    ico.setAttribute('data-kind', 'ico');
    document.head.append(ico);
  }
  svg.setAttribute('href', '/favicon.svg');
  svg.setAttribute('type', 'image/svg+xml');
  ico.setAttribute('href', '/favicon.ico');
  ico.removeAttribute('type');
}

function iconHrefs(): string[] {
  return Array.from(document.head.querySelectorAll('link[rel="icon"]')).map(
    (el) => el.getAttribute('href') ?? '',
  );
}

let visibility: DocumentVisibilityState = 'visible';

function setHidden(hidden: boolean): void {
  visibility = hidden ? 'hidden' : 'visible';
  document.dispatchEvent(new Event('visibilitychange'));
}

function stubPollingFetch(reviewId: string, detail: { current: Record<string, unknown> }): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'POST' && pathname === '/api/reviews') {
        return { ok: true, status: 200, json: async () => ({ review_id: reviewId, resumed: false }) } as Response;
      }
      if (pathname === `/api/reviews/${reviewId}`) {
        return { ok: true, status: 200, json: async () => detail.current } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submit(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [docxFile()] } });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-status');
}

beforeEach(() => {
  visibility = 'visible';
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
  document.title = 'Contract Toaster';
  installIconLinks();
  installMockLocalStorage();
  MockNotification.permission = 'granted';
  MockNotification.instances = [];
  vi.stubGlobal('Notification', MockNotification);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #497 — wired into the real ReviewSubmission panel', () => {
  it('the tab title mirrors the polled progress_stage while working, and restores on DONE', async () => {
    const detail: { current: Record<string, unknown> } = {
      current: {
        review_id: 'rev-497',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
        progress_stage: 'primary_pass',
      },
    };
    stubPollingFetch('rev-497', detail);
    await submit();

    // stageTheater.ts's `caption` field is doc-commented "shown under the
    // glass and in the tab title" — the short `label` is for a different
    // surface (the tight "Step 2 of 4 · …" line).
    await waitFor(() =>
      expect(document.title).toBe('Reading your contract against the playbook… — Contract Toaster'),
    );

    detail.current = { ...detail.current, status: 'DONE', decision: 'ACCEPT', progress_stage: 'redline' };
    await waitFor(() => expect(document.title).toBe('Contract Toaster'), { timeout: 6000 });
  });

  it('the favicon badge appears only if the tab was hidden when the review went terminal, and clears on focus', async () => {
    const detail: { current: Record<string, unknown> } = {
      current: {
        review_id: 'rev-497',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
        progress_stage: 'redline',
      },
    };
    stubPollingFetch('rev-497', detail);
    await submit();
    await waitFor(() => expect(iconHrefs()[0]).not.toBe('/favicon.svg'));

    setHidden(true);
    detail.current = { ...detail.current, status: 'DONE', decision: 'ACCEPT', has_output: false };
    await waitFor(() => expect(iconHrefs()).toEqual([FAVICON_BADGE_DONE, FAVICON_BADGE_DONE]), {
      timeout: 6000,
    });

    visibility = 'visible';
    window.dispatchEvent(new Event('focus'));
    expect(iconHrefs()).toEqual(['/favicon.svg', '/favicon.ico']);
  });

  it('the notify-toggle control renders (Notification API present) and its label reflects opt-in', async () => {
    stubPollingFetch('rev-497', {
      current: {
        review_id: 'rev-497',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
        progress_stage: null,
      },
    });
    await submit();

    const toggle = screen.getByTestId('notify-toggle');
    expect(toggle.textContent).toContain('Notify me when toasts finish');
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle.textContent).toContain('Notifications on'));
  });

  it('a terminal DONE while hidden and opted-in fires a Notification carrying the real outcome label, never a filename', async () => {
    const detail: { current: Record<string, unknown> } = {
      current: {
        review_id: 'rev-497',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
        progress_stage: null,
      },
    };
    stubPollingFetch('rev-497', detail);
    await submit();

    fireEvent.click(screen.getByTestId('notify-toggle'));
    await waitFor(() => expect(screen.getByTestId('notify-toggle').textContent).toContain('on'));

    setHidden(true);
    detail.current = {
      ...detail.current,
      status: 'DONE',
      decision: 'REQUEST_CHANGE',
      has_output: false,
    };

    await waitFor(() => expect(MockNotification.instances).toHaveLength(1), { timeout: 6000 });
    const notification = MockNotification.instances[0];
    expect(notification.title).toBe("Toast's ready — changes requested");
    expect(notification.title).not.toContain('contract.docx');

    const focusSpy = vi.spyOn(window, 'focus').mockImplementation(() => {});
    notification.onclick?.();
    expect(focusSpy).toHaveBeenCalledTimes(1);
    expect(notification.closed).toBe(true);
    focusSpy.mockRestore();
  });

  it('a terminal ERROR while hidden and opted-in fires the fixed burnt-toast phrase, and the favicon shows the FAILED badge', async () => {
    const detail: { current: Record<string, unknown> } = {
      current: {
        review_id: 'rev-497',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
        progress_stage: 'critic_pass',
      },
    };
    stubPollingFetch('rev-497', detail);
    await submit();

    fireEvent.click(screen.getByTestId('notify-toggle'));
    await waitFor(() => expect(screen.getByTestId('notify-toggle').textContent).toContain('on'));

    setHidden(true);
    detail.current = { ...detail.current, status: 'ERROR', failing_stage: 'critic_pass', reason: 'llm_timeout' };

    await waitFor(() => expect(MockNotification.instances).toHaveLength(1), { timeout: 6000 });
    expect(MockNotification.instances[0].title).toBe('That one burnt — tap for why');
    await waitFor(() => expect(iconHrefs()).toEqual([FAVICON_BADGE_FAILED, FAVICON_BADGE_FAILED]), {
      timeout: 6000,
    });
  });

  it('no notification fires when the reviewer never opted in, even hidden and terminal', async () => {
    const detail: { current: Record<string, unknown> } = {
      current: {
        review_id: 'rev-497',
        status: 'RUNNING',
        decision: null,
        message: null,
        has_output: false,
        progress_stage: null,
      },
    };
    stubPollingFetch('rev-497', detail);
    await submit();

    setHidden(true);
    detail.current = { ...detail.current, status: 'DONE', decision: 'ACCEPT', has_output: false };
    await screen.findByTestId('review-result', {}, { timeout: 6000 });

    expect(MockNotification.instances).toHaveLength(0);
  });
});
