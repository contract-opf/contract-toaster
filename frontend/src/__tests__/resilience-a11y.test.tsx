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
 *      reaches rendered output, and no 'Exos'/'EXOS' string does either.
 *   3. Accessibility: status and error regions are announced via
 *      role="alert" / aria-live, not visual-only.
 *   4. Shared authorizedFetch: when getToken() resolves to an empty string,
 *      no `Authorization: Bearer ` header is sent at all.
 *   5. Download: the result is handed to the browser via a temporary
 *      anchor (so the SPA never navigates away), not
 *      window.location.assign.
 *
 * Fully offline: aws-amplify/auth and @aws-amplify/ui-react are mocked,
 * fetch is stubbed per test. No live AWS/Cognito/network.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import AdminUsers from '../AdminUsers';
import App from '../App';

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
    fireEvent.click(screen.getByTestId('review-submit-button'));

    // Drive the first (failing) poll and its scheduled retry to completion
    // without any further user interaction.
    await vi.runAllTimersAsync();

    expect(getCalls).toBeGreaterThanOrEqual(2);
    expect(screen.getByTestId('review-status').textContent).toContain('DONE');
  });
});

// ---------------------------------------------------------------------------
// 2. Friendly errors — no raw endpoint/HTTP-code strings, no Exos/EXOS.
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
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response),
    );

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));

    const errorEl = await screen.findByTestId('review-submit-error');
    assertFriendly(errorEl.textContent ?? '');
  });

  it('shows friendly copy for a download failure with no server detail', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
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
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');
    fireEvent.click(screen.getByTestId('review-download-button'));

    const errorEl = await screen.findByTestId('review-download-error');
    assertFriendly(errorEl.textContent ?? '');
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
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response),
    );

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));

    const errorEl = await screen.findByTestId('review-submit-error');
    expect(errorEl).toHaveAttribute('role', 'alert');
  });

  it('announces the review-status region via aria-live', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method ?? 'GET').toUpperCase();
      const pathname = new URL(url, 'http://localhost').pathname;
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
    fireEvent.click(screen.getByTestId('review-submit-button'));

    const statusEl = await screen.findByTestId('review-status');
    expect(statusEl).toHaveAttribute('aria-live', 'polite');
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
    fetchAuthSessionMock.mockResolvedValueOnce({ tokens: {} });

    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ review_id: 'rev-anon', resumed: false }),
    }) as Response);
    vi.stubGlobal('fetch', fetchMock);

    render(<ReviewSubmission />);
    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit | undefined];
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
      fireEvent.click(screen.getByTestId('review-submit-button'));
      await screen.findByTestId('review-result');
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
