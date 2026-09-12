/**
 * resilience-a11y.test.tsx — polling resilience, friendly errors, aria-live,
 * shared authorizedFetch, and non-navigating download (issue #271).
 *
 * Locks in the five items from #271 against the real components:
 *
 *   1. Polling resilience: one transient (rejected) poll fetch is followed
 *      automatically by a retry — the review flow recovers on its own
 *      without the user re-submitting.
 *   2. Friendly errors: no raw endpoint path or HTTP status code ever
 *      reaches rendered output, and no tenant-brand strings string does either.
 *   3. Accessibility: status and error regions are announced via
 *      role="alert" / aria-live, not visual-only.
 *   4. Shared authorizedFetch: when getToken() resolves to an empty string,
 *      no `Authorization: Bearer ` header is sent at all.
 *   5. Download: the result is handed to the browser via a temporary
 *      anchor (so the SPA never navigates away), not
 *      window.location.assign.
 *
 * Section 6 was added for issue #724 and is a different subject on the same
 * axis: the Orbit Diner console's forced-colours and plain-mode fallbacks —
 * the layout that has to survive when the user's own colours replace ours.
 * Its own header explains what it can and cannot prove in jsdom.
 *
 * Fully offline: aws-amplify/auth and @aws-amplify/ui-react are mocked,
 * fetch is stubbed per test. No live AWS/Cognito/network.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import ReviewSubmission from '../ReviewSubmission';
import {
  DEFAULT_PLAYBOOKS,
  findReviewResult,
  pressSubmit,

} from './support/consoleSurface';
import { resolveStyle } from './support/orbitCss';
import AdminUsers from '../AdminUsers';
import App from '../App';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import { toReviewModel, type ReviewProjectionState } from '../orbit-diner/projection';
import type { Playbook, ReviewModel } from '../orbit-diner/types';
import { allowConsoleErrorsInThisTest } from './support/consoleErrorGuard';

interface FakeAuthSession {
  tokens: {
    idToken?: { toString: () => string };
    accessToken?: { toString: () => string };
  };
}

const fetchAuthSessionMock = vi.fn<() => Promise<FakeAuthSession>>(async () => ({
  tokens: {
    idToken: { toString: () => 'mock-id-token.jwt.value' },
    accessToken: { toString: () => 'mock-access-token.jwt.value' },
  },
}));

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: (...args: unknown[]) =>
    (fetchAuthSessionMock as unknown as (...a: unknown[]) => unknown)(...args),
}));

vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'reviewer-sub', signInDetails: { loginId: 'reviewer@example.com' } },
    signOut: vi.fn(),
  }),
}));

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

beforeEach(() => {
  fetchAuthSessionMock.mockResolvedValue({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// 1. Polling resilience
// ---------------------------------------------------------------------------
describe('polling resilience — ReviewSubmission.tsx', () => {
  it('recovers automatically after one transient poll failure (no user action)', async () => {
    vi.useFakeTimers();

    let getCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      // Issue #733: the console needs an active playbook to submit at all.
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }

      if (method === 'POST' && pathname === '/api/reviews') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ review_id: 'rev-flaky', resumed: false }),
        } as Response;
      }

      if (pathname === '/api/reviews/rev-flaky') {
        getCalls += 1;
        if (getCalls === 1) {
          throw new TypeError('network down');
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: 'rev-flaky',
            status: 'DONE',
            decision: 'ACCEPT',
            message: null,
            has_output: false,
          }),
        } as Response;
      }

      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    // This test owns the clock, so the catalog is landed by driving it rather
    // than by waiting on it (issue #733).
    await vi.runAllTimersAsync();
    fireEvent.click(screen.getByTestId('review-submit-button'));

    // Drive the first (failing) poll and its scheduled retry to completion
    // without any further user interaction.
    await vi.runAllTimersAsync();

    expect(getCalls).toBeGreaterThanOrEqual(2);
    // The retried poll reached the terminal state — asserted via the
    // outcome chip's friendly label (issue #470), not the raw `DONE` enum
    // this screen no longer renders verbatim.
    // Issue #733: the console's lamp reports the machine's state; the OUTCOME
    // is the result panel's headline, which means opening the record this
    // review has no redline to print from.
    fireEvent.click(screen.getByRole('button', { name: /review details/i }));
    expect(screen.getByTestId('review-result').textContent).toContain('Accepted');
  });

  // Issue #470: this screen's status line used to render the raw enum
  // verbatim (`<strong>{detail?.status}</strong>`) — MANUAL_REVIEW_REQUIRED,
  // observed live, is the terminal status that actually leaked. Pinned
  // separately from the DONE/ACCEPT happy path above.
  it('shows a friendly outcome for a MANUAL_REVIEW_REQUIRED review, never the raw token', async () => {
    vi.useFakeTimers();

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      // Issue #733: the console needs an active playbook to submit at all.
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }

      if (method === 'POST' && pathname === '/api/reviews') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ review_id: 'rev-manual', resumed: false }),
        } as Response;
      }

      if (pathname === '/api/reviews/rev-manual') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: 'rev-manual',
            status: 'MANUAL_REVIEW_REQUIRED',
            decision: null,
            message: null,
            has_output: false,
          }),
        } as Response;
      }

      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await vi.runAllTimersAsync();
    fireEvent.click(screen.getByTestId('review-submit-button'));

    await vi.runAllTimersAsync();

    const result = screen.getByTestId('review-result');
    expect(result.textContent).toContain('Needs manual review');
    expect(result.textContent).not.toContain('MANUAL_REVIEW_REQUIRED');
    expect(screen.getByTestId('review-outcome').textContent).toBe('Needs manual review');
    // Whatever the lamp says, it never says the raw enum.
    expect(screen.getByTestId('review-status').textContent).not.toContain(
      'MANUAL_REVIEW_REQUIRED',
    );
  });
});

// ---------------------------------------------------------------------------
// 2. Friendly errors — no raw endpoint/HTTP-code strings, no tenant-brand.
// ---------------------------------------------------------------------------
describe('friendly errors — no raw technical strings', () => {
  function assertFriendly(text: string): void {
    expect(text).not.toMatch(/\/api\//);
    expect(text).not.toMatch(/HTTP\s*\d/i);
    expect(text).not.toMatch(/\bstatus\s*\d{3}\b/i);
    expect(text).not.toMatch(/exos/i);
    expect(text.trim().length).toBeGreaterThan(0);
  }

  it('shows friendly copy for a submit failure with no server detail', async () => {
    vi.stubGlobal(
      'fetch',
      // Issue #733: the catalog succeeds so the console can get as far as the
      // failing submit this test is about; everything else still 500s.
      vi.fn(async (input: RequestInfo | URL) => {
        if (new URL(String(input), 'http://localhost').pathname === '/api/playbooks') {
          return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
        }
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }),
    );

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();

    const errorEl = await screen.findByTestId('review-submit-error');
    assertFriendly(errorEl.textContent ?? '');
  });

  it('shows friendly copy for a download failure with no server detail', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      // Issue #733: the console needs an active playbook to submit at all.
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      if (method === 'POST' && pathname === '/api/reviews') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ review_id: 'rev-dl', resumed: false }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-dl') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: 'rev-dl',
            status: 'DONE',
            decision: 'REQUEST_CHANGE',
            message: null,
            has_output: true,
          }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-dl/output') {
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();
    await findReviewResult();
    fireEvent.click(screen.getByTestId('review-download-button'));

    const errorEl = await screen.findByTestId('review-download-error');
    assertFriendly(errorEl.textContent ?? '');
  });

  // Issue #466: a download 503's `detail` is server configuration (an unset
  // storage env var — #465's own failure mode), not a message meant for a
  // reviewer — the raw-string policy above must hold even when the server
  // DOES send a `detail`, unlike other endpoints where a legitimate
  // server-supplied detail is shown. The env-var-shaped string below stands
  // in for the real retired name without repeating it literally (the repo's
  // own #465 gate greps the tree for that exact string).
  it('never renders the raw server `detail` for a download failure, even when one is sent', async () => {
    // The whole point: the raw detail goes to the console and NOT to the
    // screen, so seeing it logged here is the expected half (issue #68).
    allowConsoleErrorsInThisTest(/EXAMPLE_STORAGE_BUCKET_ENV_VAR not configured/);
    const CONFIG_DETAIL = 'EXAMPLE_STORAGE_BUCKET_ENV_VAR not configured.';
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      // Issue #733: the console needs an active playbook to submit at all.
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      if (method === 'POST' && pathname === '/api/reviews') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ review_id: 'rev-cfg', resumed: false }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-cfg') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: 'rev-cfg',
            status: 'DONE',
            decision: 'REQUEST_CHANGE',
            message: null,
            has_output: true,
          }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-cfg/output') {
        return { ok: false, status: 503, json: async () => ({ detail: CONFIG_DETAIL }) } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();
    await findReviewResult();

    // The automatic save (issue #448) fires on its own here — no click
    // needed — and its failure must be just as honest as the button's.
    const errorEl = await screen.findByTestId('review-download-error');
    assertFriendly(errorEl.textContent ?? '');
    expect(document.body.textContent).not.toContain(CONFIG_DETAIL);
    expect(document.body.textContent).not.toContain('EXAMPLE_STORAGE_BUCKET_ENV_VAR');

    // And the "ready" announcement must never have claimed the save
    // succeeded — the fetch it depended on failed.
    const announcement = screen.getByTestId('review-ready-announcement');
    expect(announcement.textContent).not.toContain('Saving it to your downloads');
    expect(announcement.textContent).not.toContain('saved to your downloads');

    // Issue #492/#466: the visible "Redline saved to your downloads." line
    // must never render when the auto-save actually failed — this is the
    // attempted-and-failed shape (mis-configured outputs bucket), distinct
    // from the never-attempted (critic-gated) path covered elsewhere.
    expect(screen.queryByTestId('review-saved-line')).toBeNull();
  });

  it('shows friendly copy for a version-fetch failure in App.tsx', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/api/me') {
        return { ok: true, status: 200, json: async () => ({ is_admin: false }) } as Response;
      }
      if (pathname === '/version') {
        return { ok: false, status: 503, json: async () => ({}) } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    const errorEl = await screen.findByTestId('version-error');
    assertFriendly(errorEl.textContent ?? '');
  });

  it('shows friendly copy for AdminUsers load failure', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/api/users') {
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }
      if (pathname === '/api/users/sync-status') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            sync_type: 'workspace',
            last_run_at: null,
            last_run_outcome: null,
            users_deprovisioned_count: 0,
            next_run_at: null,
          }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<AdminUsers />);
    const errorEl = await screen.findByTestId('admin-users-error');
    assertFriendly(errorEl.textContent ?? '');
  });
});

// ---------------------------------------------------------------------------
// 3. Accessibility — status/error regions announced via role/aria-live.
// ---------------------------------------------------------------------------
describe('accessibility — status and error regions', () => {
  it('announces the submit error region via role="alert"', async () => {
    vi.stubGlobal(
      'fetch',
      // Issue #733: the catalog succeeds so the console can get as far as the
      // failing submit this test is about; everything else still 500s.
      vi.fn(async (input: RequestInfo | URL) => {
        if (new URL(String(input), 'http://localhost').pathname === '/api/playbooks') {
          return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
        }
        return { ok: false, status: 500, json: async () => ({}) } as Response;
      }),
    );

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();

    const errorEl = await screen.findByTestId('review-submit-error');
    expect(errorEl).toHaveAttribute('role', 'alert');
  });

  // Issue #510 reversed this expectation. `review-status` used to be
  // `aria-live="polite"`, which meant the terminal poll narrated the whole
  // block — id chip, outcome chip, decision copy, critic-delta indicator,
  // download row — back to back with the purpose-written handoff copy in the
  // sibling region beside it. The terminal moment is still announced, by the
  // one region written to announce it; see single-terminal-announcement.test.tsx,
  // which owns that property now.
  it('leaves the review-status region silent, so the terminal moment is announced once', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      // Issue #733: the console needs an active playbook to submit at all.
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      if (method === 'POST' && pathname === '/api/reviews') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ review_id: 'rev-status', resumed: false }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          review_id: 'rev-status',
          status: 'DONE',
          decision: 'ACCEPT',
          message: null,
          has_output: false,
        }),
      } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();

    const statusEl = await screen.findByTestId('review-status');
    expect(statusEl).not.toHaveAttribute('aria-live', 'polite');
    // The announcement did not disappear, it moved: the dedicated handoff
    // region carries it.
    await waitFor(() => {
      expect(screen.getByTestId('review-ready-announcement').textContent).not.toBe('');
    });
  });

  it('announces AdminUsers error region via role="alert"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response),
    );

    render(<AdminUsers />);
    const errorEl = await screen.findByTestId('admin-users-error');
    expect(errorEl).toHaveAttribute('role', 'alert');
  });
});

// ---------------------------------------------------------------------------
// 4. Shared authorizedFetch — no `Authorization: Bearer ` when token empty.
// ---------------------------------------------------------------------------
describe('shared authorizedFetch — empty-token short circuit', () => {
  it('sends no Authorization header when getToken() resolves empty', async () => {
    // This test lets the real fetch run against a relative path, which
    // undici refuses to parse; the component logs the rejection (#68).
    allowConsoleErrorsInThisTest(/Failed to parse URL/);
    // Every call, not only the first: the console reads the catalog before it
    // can submit, so a one-shot empty session would be spent on that and the
    // POST this test inspects would carry a real token (issue #733).
    fetchAuthSessionMock.mockResolvedValue({ tokens: {} });

    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      // Issue #733: the console needs a catalog before it will submit.
      if (new URL(String(input), 'http://localhost').pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ review_id: 'rev-anon', resumed: false }),
      } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls.find(
      ([input]) =>
        new URL(String(input), 'http://localhost').pathname === '/api/reviews',
    ) as [RequestInfo | URL, RequestInit | undefined];
    const headers = (init?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 5. Download — anchor-based, non-navigating (app state survives).
// ---------------------------------------------------------------------------
describe('download — non-navigating', () => {
  it('hands the presigned URL to the browser via a temporary anchor, not window.location.assign', async () => {
    const assignSpy = vi.fn();
    const realLocation = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...realLocation, assign: assignSpy },
    });

    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    const presignedUrl = 'https://s3.example.test/outputs/rev-anchor/out.docx?sig=abc';
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
      // Issue #733: the console needs an active playbook to submit at all.
      if (pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => DEFAULT_PLAYBOOKS } as Response;
      }
      if (method === 'POST' && pathname === '/api/reviews') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ review_id: 'rev-anchor', resumed: false }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-anchor') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            review_id: 'rev-anchor',
            status: 'DONE',
            decision: 'REQUEST_CHANGE',
            message: null,
            has_output: true,
          }),
        } as Response;
      }
      if (pathname === '/api/reviews/rev-anchor/output') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ url: presignedUrl, expires_in: 60 }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    try {
      render(<ReviewSubmission />);
      fireEvent.change(screen.getByTestId('review-file-input'), {
        target: { files: [docxFile()] },
      });
      await pressSubmit();
      await findReviewResult();
      fireEvent.click(screen.getByTestId('review-download-button'));

      await vi.waitFor(() => expect(clickSpy).toHaveBeenCalled());
      expect(assignSpy).not.toHaveBeenCalled();

      // The SPA's own document must never have navigated away — the section
      // that mounted the flow is still present.
      expect(screen.getByTestId('review-submission')).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
    }
  });
});

// ---------------------------------------------------------------------------
// 6. Forced colours and plain mode — the Orbit Diner console (issue #724).
//
// The accepted 04p1 patch claims six things in prose: forced colours
// auto-selects the plain layout, decorative SVG disappears, the select,
// textarea and keys keep a real border, Start and Stop become two separate
// native buttons, the footer toggle is a genuine escape hatch with an
// optional owner, and none of it persists. Each is checked here rather than
// taken on trust.
//
// The console is rendered DIRECTLY, not through ReviewSubmission: the build
// flag stays off for every other test in this file, and `plain` /
// `onPlainChange` are the component's own contract — the app passes neither
// today (owner decision F2: session-only state, no storage key).
//
// BE HONEST ABOUT THE CSS HALF. vitest runs with `css: false`
// (frontend/vitest.config.ts) and jsdom implements no cascade, so
// `getComputedStyle(el).display` reports the jsdom default here and asserting
// it would assert nothing. `resolve()` below defers to `support/orbitCss`,
// which reads the SHIPPED stylesheet off disk and resolves the winning
// declaration for the element the console actually rendered — importance,
// then specificity, then document order — over the rules that apply in the
// emulated mode. It is joined to the live DOM by `Element.matches`, so a
// class the component stops emitting fails here, and every negative is
// paired with an illustrated-view control that fails if the resolver ever
// stops discriminating.
//
// The negatives were mutation-checked as they were written, because a
// stylesheet assertion that resolves nothing passes just as quietly as one
// that resolves the right thing: deleting the shared `.od-decoration` rules,
// zeroing the system-colour borders, and dropping `forcedColours` out of
// `plainView` each turned a different subset of these tests red.
// ---------------------------------------------------------------------------

/** The cascade the console renders under, in this file's two modes. The
 *  resolver itself is shared with the other stylesheet tests — one reader of
 *  `orbit.css`, so a second file cannot grow its own idea of the cascade. */
function resolve(el: Element, properties: string[], forcedColours: boolean): string | undefined {
  return resolveStyle(el, properties, forcedColours ? 'forced-colors' : 'screen');
}

/** The border width the stylesheet gives this element, in px. No applicable
 *  declaration means no border at all — the defect #724 is about. */
function borderWidthPx(el: Element, forcedColours: boolean): number {
  const value = resolve(el, ['border', 'border-width'], forcedColours);
  if (value === undefined) return 0;
  const px = /(^|\s)(\d*\.?\d+)px(\s|$)/.exec(value);
  if (px) return Number(px[2]);
  return /(^|\s)0(\s|$)/.test(value) ? 0 : Number.NaN;
}

const ORBIT_PLAYBOOKS: Playbook[] = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' },
];

function orbitState(over: Partial<ReviewProjectionState> = {}): ReviewProjectionState {
  return {
    file: null,
    submitting: false,
    reviewId: null,
    detail: null,
    playbookId: 'nda',
    browning: 'medium',
    notesMode: 'external',
    toasterGuidance: '',
    appliedGuidance: null,
    dispositionNote: '',
    playbooks: ORBIT_PLAYBOOKS,
    notesModeInternalAvailable: false,
    muted: false,
    notificationsSupported: true,
    ...over,
  };
}

/** A console holding a chosen .docx with nothing submitted: Start is
 *  available and Stop is not. */
const loadedModel = (): ReviewModel => toReviewModel(orbitState({ file: docxFile() }));

/** The same console mid-review: Stop is available and Start is not. */
const runningModel = (): ReviewModel =>
  toReviewModel(
    orbitState({
      file: docxFile(),
      reviewId: 'rev-fc',
      detail: { review_id: 'rev-fc', status: 'RUNNING', progress_stage: 'primary_pass' },
    }),
  );

/** A console carrying decorative `SmallArt` in BOTH layouts, so "the
 *  decoration is hidden" is a claim about nodes really in the tree rather
 *  than about a branch that never emitted them.
 *
 *  Two of them, deliberately. The steam an ERROR draws has a rule of its own
 *  (`.od-plain .od-steam`) that would hide it whatever happened to the shared
 *  `.od-decoration` rules, so a butter pat — drawn once a cover note is ready,
 *  and hidden ONLY by those shared rules — rides along as the element that
 *  actually tests the class this ticket is about. */
const decoratedModel = (): ReviewModel =>
  toReviewModel(
    orbitState({
      file: docxFile(),
      reviewId: 'rev-fc',
      detail: { review_id: 'rev-fc', status: 'ERROR' },
      coverNoteDraft: 'Dear counterparty,',
    }),
  );

/** jsdom ships no `matchMedia`, and the console reads it for forced colours
 *  (and the motion controller for reduced motion). Answers `matches` only for
 *  the features named here. */
function stubMatchMedia(matching: string[]): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: matching.some((feature) => query.includes(feature)),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

type OrbitProps = ComponentProps<typeof OrbitDiner>;

function renderConsole(model: ReviewModel, props: Partial<OrbitProps> = {}): void {
  render(
    <OrbitDiner
      model={model}
      keyboardShortcuts={false}
      onFile={vi.fn()}
      onPreferences={vi.fn()}
      onAction={vi.fn()}
      onSound={vi.fn()}
      {...props}
    />,
  );
}

/** Every key either store holds, minus the ones the PANEL is allowed to write
 *  and that say nothing about the console's layout. */
function storedKeys(): string[] {
  const allowed = [
    'contract-toaster:last-playbook',
    'contract-toaster:last-browning',
    'contract-toaster:last-notes-mode',
  ];
  return [window.localStorage, window.sessionStorage]
    .flatMap((store) => Object.keys(store))
    .filter((key) => !allowed.includes(key));
}

function consoleRoot(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

describe('forced colours — the console falls back to the plain layout (issue #724)', () => {
  it('auto-selects the plain layout, asking for no preference and storing none', () => {
    stubMatchMedia(['forced-colors']);
    renderConsole(loadedModel());

    expect(consoleRoot()).toHaveClass('od-plain');
    // The console writes NOTHING for this: no plain-mode key, no
    // forced-colours key. Asserted against the allowlist rather than against
    // an empty store, because tests earlier in this file legitimately leave
    // the panel's own `last-playbook` key behind (issue #733).
    expect(storedKeys()).toEqual([]);
  });

  it('leaves the illustrated layout alone when forced colours are off (the control)', () => {
    stubMatchMedia([]);
    renderConsole(loadedModel());

    expect(consoleRoot()).not.toHaveClass('od-plain');
  });

  it('follows a forced-colours mode switched on after mount', () => {
    const listeners: (() => void)[] = [];
    let active = false;
    vi.stubGlobal('matchMedia', (query: string) => ({
      get matches() {
        return active && query.includes('forced-colors');
      },
      media: query,
      onchange: null,
      addEventListener: (_type: string, listener: () => void) => {
        if (query.includes('forced-colors')) listeners.push(listener);
      },
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }));

    renderConsole(loadedModel());
    expect(consoleRoot()).not.toHaveClass('od-plain');
    expect(listeners.length).toBeGreaterThan(0);

    active = true;
    act(() => {
      for (const listener of listeners) listener();
    });
    expect(consoleRoot()).toHaveClass('od-plain');
  });

  it('hides every decoration it rendered, and keeps them in the illustrated view', () => {
    stubMatchMedia(['forced-colors']);
    renderConsole(decoratedModel());

    const decorations = Array.from(document.querySelectorAll('.od-decoration'));
    // Named, so this cannot pass by rendering no decorations at all — and so
    // the pat whose only hiding rule IS `.od-decoration` is provably present.
    expect(document.querySelectorAll('.od-decoration.od-butter-art')).toHaveLength(1);
    expect(decorations.length).toBeGreaterThan(1);
    for (const node of decorations) {
      expect(resolve(node, ['display'], true), node.getAttribute('class') ?? '').toBe('none');
    }
    // The photographed plates go the same way: no decorative artwork survives
    // this mode, only the native controls under it.
    const plates = Array.from(document.querySelectorAll('.od-material'));
    expect(plates.length).toBeGreaterThan(0);
    for (const node of plates) {
      expect(resolve(node, ['display'], true), node.getAttribute('class') ?? '').toBe('none');
    }

    cleanup();
    stubMatchMedia([]);
    renderConsole(decoratedModel());

    // The control: the resolver discriminates, rather than answering "none"
    // to everything it is handed.
    const illustrated = Array.from(document.querySelectorAll('.od-decoration'));
    expect(illustrated.length).toBeGreaterThan(0);
    expect(illustrated.some((node) => resolve(node, ['display'], false) !== 'none')).toBe(true);
    expect(
      Array.from(document.querySelectorAll('.od-material')).some(
        (node) => resolve(node, ['display'], false) !== 'none',
      ),
    ).toBe(true);
  });

  it('gives the select, the textarea and every key a non-zero border', () => {
    stubMatchMedia(['forced-colors']);
    renderConsole(loadedModel());

    const keys = Array.from(document.querySelectorAll('.od-key'));
    const radioFaces = Array.from(document.querySelectorAll('.od-radio-key > span'));
    // A console that rendered no keys would satisfy a per-element loop
    // vacuously.
    expect(keys.length).toBeGreaterThan(0);
    expect(radioFaces.length).toBeGreaterThan(0);

    for (const node of [
      screen.getByTestId('review-playbook-dial'),
      screen.getByTestId('review-guidance-input'),
      ...keys,
      ...radioFaces,
    ]) {
      const label = `${node.tagName.toLowerCase()}.${node.getAttribute('class') ?? ''}`;
      expect(borderWidthPx(node, true), label).toBeGreaterThan(0);
    }
  });

  it('marks the checked radio with a border of its own, never colour alone', () => {
    stubMatchMedia(['forced-colors']);
    renderConsole(loadedModel());

    const checked = document.querySelector('.od-radio-key input:checked + span');
    expect(checked).not.toBeNull();
    expect(resolve(checked as Element, ['border', 'border-width'], true)).toMatch(/Highlight/);
  });
});

describe('forced colours — Start and Stop are two separate controls (issue #724)', () => {
  it('offers both buttons, with only Start live on a loaded console', () => {
    stubMatchMedia(['forced-colors']);
    renderConsole(loadedModel());

    // The illustrated lever is ONE element that renames itself; assistive
    // technology in this mode must find two.
    expect(document.querySelector('.od-lever')).toBeNull();
    expect(screen.getByTestId('review-submit-button')).toBeEnabled();
    expect(screen.getByTestId('review-cancel-button')).toBeDisabled();
  });

  it('swaps which of the two is live once the review is running', () => {
    stubMatchMedia(['forced-colors']);
    renderConsole(runningModel());

    expect(screen.getByTestId('review-submit-button')).toBeDisabled();
    expect(screen.getByTestId('review-cancel-button')).toBeEnabled();
  });

  it('reaches the app action handler separately from each button', () => {
    stubMatchMedia(['forced-colors']);
    const onAction = vi.fn();
    renderConsole(loadedModel(), { onAction });

    fireEvent.click(screen.getByTestId('review-submit-button'));
    expect(onAction).toHaveBeenCalledWith({ type: 'submit' });

    cleanup();
    onAction.mockClear();
    renderConsole(runningModel(), { onAction });
    fireEvent.click(screen.getByTestId('review-cancel-button'));
    expect(onAction).toHaveBeenCalledWith({ type: 'cancel' });
  });
});

describe('plain mode is a real escape hatch (issue #724)', () => {
  function toggle(): HTMLElement {
    return screen.getByRole('button', { name: /^(Plain|Illustrated) controls$/ });
  }

  it('switches the view from the footer and switches it back, session-only', () => {
    stubMatchMedia([]);
    renderConsole(loadedModel());

    expect(toggle()).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(toggle());
    expect(consoleRoot()).toHaveClass('od-plain');
    expect(toggle()).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(toggle());
    expect(consoleRoot()).not.toHaveClass('od-plain');

    // Owner decision F2: session-only React state. Nothing is persisted, so
    // security-posture.test.tsx's storage allowlist stays as it is.
    // The console writes NOTHING for this: no plain-mode key, no
    // forced-colours key. Asserted against the allowlist rather than against
    // an empty store, because tests earlier in this file legitimately leave
    // the panel's own `last-playbook` key behind (issue #733).
    expect(storedKeys()).toEqual([]);
  });

  it('hands the choice to the app when onPlainChange is supplied, and owns nothing itself', () => {
    stubMatchMedia([]);
    const onPlainChange = vi.fn();
    renderConsole(loadedModel(), { onPlainChange });

    fireEvent.click(toggle());
    expect(onPlainChange).toHaveBeenCalledWith(true);
    // The app owns the value now: a console that ALSO flipped itself would
    // drift from its owner the moment the app declined.
    expect(consoleRoot()).not.toHaveClass('od-plain');
  });

  it('renders the plain layout, with both operations, when the app passes plain', () => {
    stubMatchMedia([]);
    renderConsole(loadedModel(), { plain: true });

    expect(consoleRoot()).toHaveClass('od-plain');
    expect(screen.getByTestId('review-submit-button')).toBeInTheDocument();
    expect(screen.getByTestId('review-cancel-button')).toBeInTheDocument();
  });

  it('lets forced colours win: the way back to the illustration is closed', () => {
    stubMatchMedia(['forced-colors']);
    const onPlainChange = vi.fn();
    renderConsole(loadedModel(), { onPlainChange });

    expect(consoleRoot()).toHaveClass('od-plain');
    expect(toggle()).toBeDisabled();
    fireEvent.click(toggle());
    expect(onPlainChange).not.toHaveBeenCalled();
    expect(consoleRoot()).toHaveClass('od-plain');
  });
});
