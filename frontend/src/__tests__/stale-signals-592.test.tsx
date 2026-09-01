/**
 * stale-signals-592.test.tsx — issue #592: two small user-visible defects
 * on the Review tab, both observed live.
 *
 *   1. Stale version footer (App.tsx) — after a backend redeploy the
 *      footer kept rendering the version fetched once at first load. The
 *      fix polls `/version` on an interval for as long as the app stays
 *      mounted, so the footer picks up a version change without a reload.
 *   2. Grammar in the form-match warning (ReviewSubmission.tsx) — the
 *      classifier's "Other" fallback (and any other vowel-leading type in
 *      the closed vocabulary, e.g. "Employment Agreement") rendered "This
 *      reads like a Other" instead of "an Other".
 *
 * fix-round-1 additions (review findings on the above):
 *   1a. A successful poll must clear an error set by an earlier failed
 *       fetch — otherwise the footer stays pinned to "unavailable" forever
 *       even once the backend is reachable again.
 *   2a. The version poll is an idle background authenticated request; a
 *       401 from it must NOT fire the app's global session-expiry
 *       notifier (that would force-sign-out an unattended tab ~60s after
 *       the session TTL lapses — exactly the scenario polling, instead of
 *       a focus/visibility refetch, was chosen to handle).
 *   3a. "an Other" is not idiomatic English either — the fallback label
 *       is special-cased to read naturally, and the same fix must reach
 *       the neutral (`match === 'unclear'`) type/side line, not just the
 *       mismatch banner.
 *
 * Fully offline: aws-amplify/auth and @aws-amplify/ui-react are mocked;
 * fetch is stubbed per test; no live network.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../App';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

vi.mock('@aws-amplify/ui-react', () => ({
  Authenticator: ({ children }: { children: () => React.ReactElement }) => children(),
  useAuthenticator: () => ({
    user: { username: 'user-sub', signInDetails: { loginId: 'user@example.com' } },
    signOut: vi.fn(),
  }),
}));

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('version footer re-fetches after a backend redeploy (#592)', () => {
  it('updates the rendered footer text once the backend reports a new version', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    let currentVersion = { version: '0', commit: 'aaaaaaaa11111111', image_digest: '', uptime_seconds: 0 };
    const impl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/version') {
        return { ok: true, status: 200, json: async () => currentVersion } as Response;
      }
      if (pathname === '/api/me') {
        return { ok: true, status: 200, json: async () => ({ is_admin: false }) } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', impl);

    render(<App />);

    const footer = await screen.findByTestId('version-display');
    expect(footer).toHaveTextContent('Version 0 (aaaaaaaa)');

    // Simulate a backend redeploy: the endpoint now reports a new build.
    currentVersion = { version: '1', commit: 'bbbbbbbb22222222', image_digest: '', uptime_seconds: 0 };

    // Advance past the poll interval so the app re-fetches /version on its
    // own, with no reload and no user interaction.
    await vi.advanceTimersByTimeAsync(60_000);

    await waitFor(() => {
      expect(screen.getByTestId('version-display')).toHaveTextContent('Version 1 (bbbbbbbb)');
    });
  });

  it('shows the version once a later poll succeeds, even when the first fetch failed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    let versionShouldFail = true;
    const goodVersion = { version: '2', commit: 'cccccccc33333333', image_digest: '', uptime_seconds: 0 };
    const impl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/version') {
        if (versionShouldFail) {
          return { ok: false, status: 503, json: async () => ({}) } as Response;
        }
        return { ok: true, status: 200, json: async () => goodVersion } as Response;
      }
      if (pathname === '/api/me') {
        return { ok: true, status: 200, json: async () => ({ is_admin: false }) } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', impl);

    render(<App />);

    // The mount-time fetch fails: the footer pins to the error copy.
    await screen.findByTestId('version-error');
    expect(screen.queryByTestId('version-display')).toBeNull();

    // The backend recovers before the next scheduled poll.
    versionShouldFail = false;
    await vi.advanceTimersByTimeAsync(60_000);

    // The footer must show the newly-reported version, not stay pinned to
    // "unavailable" forever (issue #592 fix-round-1, finding 1).
    await waitFor(() => {
      expect(screen.getByTestId('version-display')).toHaveTextContent('Version 2 (cccccccc)');
    });
    expect(screen.queryByTestId('version-error')).toBeNull();
  });

  it('does not force a sign-out when the version poll (not a user-triggered request) gets a 401', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });

    let versionCallCount = 0;
    const impl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/version') {
        versionCallCount += 1;
        if (versionCallCount === 1) {
          return {
            ok: true,
            status: 200,
            json: async () => ({ version: '0', commit: 'aaaaaaaa11111111', image_digest: '', uptime_seconds: 0 }),
          } as Response;
        }
        // The session lapsed while the tab sat idle: the poll now gets a 401.
        return { ok: false, status: 401, json: async () => ({}) } as Response;
      }
      if (pathname === '/api/me') {
        return { ok: true, status: 200, json: async () => ({ is_admin: false }) } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', impl);

    render(<App />);
    await screen.findByTestId('version-display');

    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => {
      expect(versionCallCount).toBeGreaterThanOrEqual(2);
    });

    // A 401 from any other authenticated route forces the "session expired"
    // banner via the global notifier (api.ts). The version poll must be
    // exempt from that (issue #592 fix-round-1, finding 2) — an unattended
    // tab must not get force-signed-out ~60s after its session TTL lapses.
    expect(screen.queryByTestId('session-expired')).toBeNull();
  });
});

// --- Grammar fix -------------------------------------------------------

function stubReviewFetch(routes: Record<string, unknown>): void {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

function docxFile(name = 'contract.docx'): File {
  return new File(['contents'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

const CATALOG = [
  { playbook_id: 'synthetic-nda-sample', display_name: 'Synthetic NDA Sample', status: 'active' },
];

const BASE_STATS = {
  word_count: 1200,
  page_estimate: 4,
  paragraph_count: 9,
  title: null,
};

function selectFile(file: File): void {
  fireEvent.change(screen.getByTestId('review-file-input'), { target: { files: [file] } });
}

describe('form-match warning grammar (#592)', () => {
  it('never renders "a Other" (or the equally ungrammatical "an Other") for the unclassified fallback type', async () => {
    stubReviewFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Other',
        paper_side: 'unclear',
        confidence: 0.3,
        one_line_summary: null,
        match: 'unlikely',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const banner = await screen.findByTestId('review-preflight-match-unlikely');
    expect(banner).not.toHaveTextContent('a Other');
    expect(banner).not.toHaveTextContent('an Other');
    expect(banner).toHaveTextContent(/This reads like an unrecognized type, not Synthetic NDA Sample/);
  });

  it('renders the same natural fallback phrasing on the neutral type/side line (match: unclear)', async () => {
    stubReviewFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Other',
        paper_side: 'unclear',
        confidence: 0.3,
        one_line_summary: null,
        match: 'unclear',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const line = await screen.findByTestId('review-preflight-type-side');
    expect(line).not.toHaveTextContent('a Other');
    expect(line).not.toHaveTextContent('an Other');
    expect(line).toHaveTextContent(/This reads like an unrecognized type/);
  });

  it('uses "an" for another vowel-leading type in the closed vocabulary (Employment Agreement)', async () => {
    stubReviewFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Employment Agreement',
        paper_side: 'unclear',
        confidence: 0.6,
        one_line_summary: null,
        match: 'unlikely',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const banner = await screen.findByTestId('review-preflight-match-unlikely');
    expect(banner).not.toHaveTextContent('a Employment Agreement');
    expect(banner).toHaveTextContent(/This reads like an Employment Agreement/);
  });

  it('still uses "a" for a consonant-leading type (unaffected by the fix)', async () => {
    stubReviewFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews/preflight': {
        ...BASE_STATS,
        classification: 'ok',
        agreement_type_guess: 'Master Services Agreement',
        paper_side: 'unclear',
        confidence: 0.6,
        one_line_summary: null,
        match: 'unlikely',
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-file-input');
    selectFile(docxFile());

    const banner = await screen.findByTestId('review-preflight-match-unlikely');
    expect(banner).toHaveTextContent(/This reads like a Master Services Agreement/);
  });
});
