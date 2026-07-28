/**
 * security-posture.test.tsx — frontend token storage + XSS posture (issue #72).
 *
 * Two invariants are locked in here, against the *real* components:
 *
 *   1. Token posture: the ID/access token from the Amplify session is never
 *      persisted to localStorage or sessionStorage. Only Amplify's own
 *      in-memory session handling (mocked here) should ever see it.
 *   2. No unsafe HTML rendering: untrusted, model-/document-derived text
 *      (user email, legal-hold reason, model-output message) is rendered as
 *      escaped text — never parsed as HTML — and no component uses
 *      `dangerouslySetInnerHTML`.
 *
 * Fully offline: `aws-amplify/auth` and `@aws-amplify/ui-react` are mocked
 * (vi.mock below) and `fetch` is stubbed per test — no live AWS/Cognito/
 * network is touched.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import App from '../App';
import AdminUsers from '../AdminUsers';
import AdminRetention from '../AdminRetention';
import ReviewSubmission from '../ReviewSubmission';

// A hostile string that would execute if it were ever parsed as HTML
// (e.g. via dangerouslySetInnerHTML or an unescaped template). If any of
// the assertions below find a real <img> element, or fail to find the
// literal markup as text, escaping has broken.
const HOSTILE = '<img src=x onerror="window.__xss_fired = true">';

// ---------------------------------------------------------------------------
// Mocks — Amplify auth/session layer. No live Cognito/AWS anywhere in this
// file; every session lookup below is served from these mocks.
// ---------------------------------------------------------------------------

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
    user: { username: 'reviewer-sub', signInDetails: { loginId: 'reviewer@example.com' } },
    signOut: vi.fn(),
  }),
}));

// ---------------------------------------------------------------------------
// fetch stub — routes by "METHOD path" (falls back to path-only for GETs).
// apiBase is unset in tests, so authorizedFetch() calls fetch with a
// relative path; resolve against a dummy origin to read the pathname.
// ---------------------------------------------------------------------------

function stubFetch(routes: Record<string, unknown>): void {
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

const SYNC_STATUS_OK = {
  sync_type: 'workspace',
  last_run_at: null,
  last_run_outcome: null,
  users_deprovisioned_count: 0,
  next_run_at: null,
};

const RETENTION_SETTINGS_OK = {
  setting_id: 'default',
  retention_window_days: 90,
  pending_reduction: null,
};

describe('token posture — App.tsx', () => {
  it('never persists the Amplify session token to localStorage or sessionStorage', async () => {
    stubFetch({
      '/version': { version: '0.0.1', commit: 'abcdef1234567890', image_digest: 'sha256:x', uptime_seconds: 1 },
      // Admin panels only mount once GET /api/me resolves is_admin: true
      // (#234/#235) — this test renders the whole App, admin panels
      // included, so it needs an admin identity to reach them.
      '/api/me': { is_admin: true },
      '/api/users': { users: [] },
      '/api/users/sync-status': SYNC_STATUS_OK,
      '/api/admin/retention': RETENTION_SETTINGS_OK,
      '/api/admin/retention/holds': { holds: [] },
    });
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem');

    render(<App />);

    // Wait for every effect that fetches an authenticated session to settle.
    await screen.findByTestId('version-display');
    await screen.findByTestId('users-table');
    await screen.findByTestId('retention-slider-panel');

    expect(setItemSpy).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});

describe('XSS posture — AdminUsers.tsx', () => {
  it('renders an untrusted user email as escaped text, never as HTML', async () => {
    stubFetch({
      '/api/users': {
        users: [
          {
            cognito_sub: 'sub-1',
            email: HOSTILE,
            status: 'active',
            is_admin: false,
            last_auth_at: 0,
            created_at: 0,
            admission: 'jit',
          },
        ],
      },
      '/api/users/sync-status': SYNC_STATUS_OK,
    });

    render(<AdminUsers />);

    const row = await screen.findByTestId('user-row-sub-1');
    expect(row.textContent).toContain(HOSTILE);
    // If escaping ever broke, the string would be parsed into a real <img>.
    expect(row.querySelector('img')).toBeNull();
  });
});

describe('XSS posture — AdminRetention.tsx', () => {
  it('renders an untrusted legal-hold reason as escaped text, never as HTML', async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS_OK,
      '/api/admin/retention/holds': {
        holds: [
          {
            review_id: 'rev-1',
            legal_hold: true,
            legal_hold_reason: HOSTILE,
            legal_hold_set_by: 'admin@example.com',
          },
        ],
      },
    });

    render(<AdminRetention />);

    const row = await screen.findByTestId('hold-row-rev-1');
    expect(row.textContent).toContain(HOSTILE);
    expect(row.querySelector('img')).toBeNull();
  });
});

describe('XSS posture — ReviewSubmission.tsx (model output)', () => {
  it('renders an untrusted model-output message as escaped text, never as HTML', async () => {
    stubFetch({
      'POST /api/reviews': { review_id: 'rev-42', resumed: false },
      'GET /api/reviews/rev-42': {
        review_id: 'rev-42',
        status: 'DONE',
        decision: 'REQUEST_CHANGE',
        message: HOSTILE,
        has_output: false,
      },
    });

    render(<ReviewSubmission />);

    const file = new File(['contents'], 'contract.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
    const input = screen.getByTestId('review-file-input');
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(screen.getByTestId('review-submit-button'));

    const result = await screen.findByTestId('review-result');
    expect(result.textContent).toContain(HOSTILE);
    expect(result.querySelector('img')).toBeNull();
  });
});

describe('source posture (regression guard)', () => {
  const srcDir = path.dirname(fileURLToPath(import.meta.url)); // .../src/__tests__
  const componentsDir = path.resolve(srcDir, '..');

  function readComponentSources(): { file: string; content: string }[] {
    return fs
      .readdirSync(componentsDir)
      .filter((name) => (name.endsWith('.ts') || name.endsWith('.tsx')) && !name.endsWith('.d.ts'))
      .map((name) => ({
        file: name,
        content: fs.readFileSync(path.join(componentsDir, name), 'utf-8'),
      }));
  }

  it('contains no dangerouslySetInnerHTML in any top-level component or module', () => {
    for (const { file, content } of readComponentSources()) {
      expect(content, `${file} must not use dangerouslySetInnerHTML`).not.toMatch(
        /dangerouslySetInnerHTML/,
      );
    }
  });

  it('contains no localStorage/sessionStorage writes in any top-level component or module', () => {
    for (const { file, content } of readComponentSources()) {
      expect(content, `${file} must not write to localStorage/sessionStorage`).not.toMatch(
        /(localStorage|sessionStorage)\.setItem/,
      );
    }
  });
});
