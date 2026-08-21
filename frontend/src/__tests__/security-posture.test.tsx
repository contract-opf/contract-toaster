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
import { afterEach, describe, expect, it, vi } from 'vitest';
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

  // Recursive walk of the whole `src/` tree (issue #497 fix round 1):
  // the previous version of this guard called `fs.readdirSync(componentsDir)`
  // with no recursion, so it only ever saw files sitting directly in `src/`
  // — anything under `src/toaster/`, `src/ui/`, `src/ui/components/`, etc.
  // was invisible to it. `__tests__` itself is excluded: this guard polices
  // "any top-level component or module", not test files, which legitimately
  // construct their own mock Storage/DOM and call its `setItem` to drive
  // assertions (see e.g. `notify-preference-497.test.tsx`).
  function collectSourceFiles(dir: string): string[] {
    const files: string[] = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === '__tests__') continue;
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        files.push(...collectSourceFiles(fullPath));
      } else if (
        entry.isFile() &&
        (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx')) &&
        !entry.name.endsWith('.d.ts')
      ) {
        files.push(fullPath);
      }
    }
    return files;
  }

  function readComponentSources(): { file: string; content: string }[] {
    return collectSourceFiles(componentsDir).map((fullPath) => ({
      file: path.relative(componentsDir, fullPath),
      content: fs.readFileSync(fullPath, 'utf-8'),
    }));
  }

  it('contains no dangerouslySetInnerHTML in any top-level component or module', () => {
    for (const { file, content } of readComponentSources()) {
      expect(content, `${file} must not use dangerouslySetInnerHTML`).not.toMatch(
        /dangerouslySetInnerHTML/,
      );
    }
  });

  it('contains no localStorage/sessionStorage writes outside the allowed preference keys', () => {
    const SETITEM_RE = /(localStorage|sessionStorage)\.setItem/;
    // The complete, explicitly-allowed call sites, each a single boolean-or-
    // id user preference — never a token, never anything about a review's
    // content (issue #489's own acceptance criterion, "Nothing sensitive
    // lands in localStorage"):
    //   - toaster/notify.ts    — issue #497's opt-in "notify me" flag.
    //   - toaster/sounds.ts    — issue #489 item 3, the sound-mute flag.
    //   - lastPlaybook.ts      — issue #489 item 4, the last-selected
    //                            contract-type id.
    // Recording the allowance HERE — rather than loosening the regex or
    // skipping these files — keeps this guard's teeth: any OTHER file that
    // starts writing to storage, in ANY subdirectory, still fails this test;
    // and if any of the three grows a SECOND call site, the exact-one-match
    // assertion below fails too.
    const ALLOWED_SETITEM_FILES = ['toaster/notify.ts', 'toaster/sounds.ts', 'lastPlaybook.ts'];
    const filesChecked = new Set<string>();

    for (const { file, content } of readComponentSources()) {
      const matchCount = (content.match(new RegExp(SETITEM_RE, 'g')) ?? []).length;
      if (ALLOWED_SETITEM_FILES.includes(file)) {
        filesChecked.add(file);
        expect(matchCount, `${file} must have exactly one setItem call site`).toBe(1);
        continue;
      }
      expect(content, `${file} must not write to localStorage/sessionStorage`).not.toMatch(
        SETITEM_RE,
      );
    }

    for (const allowed of ALLOWED_SETITEM_FILES) {
      expect(filesChecked.has(allowed), `${allowed} was not found by the scan`).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// Issue #489's own acceptance criterion, checked dynamically rather than
// only by the static source scan above: "Nothing sensitive lands in
// localStorage (only the mute flag and a playbook id — assert in a test
// that the auth token never does)." The two allowed writers are exercised
// for real and the resulting storage is inspected — not just "no setItem
// call site exists elsewhere" (the source-scan test above), but "what these
// two call sites actually write is never token-shaped".
// ---------------------------------------------------------------------------
describe('localStorage content posture (issue #489)', () => {
  afterEach(() => {
    window.localStorage.clear();
    vi.resetModules();
  });

  // A real Amplify id/access token is a three-segment JWT: long, and
  // dot-delimited. Neither of the two allowed values below can ever collide
  // with that shape — a mute flag is a single bit and a playbook id is a
  // short slug — but this pins the actual runtime shape rather than trusting
  // that description.
  function looksLikeAToken(value: string): boolean {
    return value.length > 60 || value.includes('.');
  }

  it('the mute flag (toaster/sounds.ts) persists only "0"/"1", never a token', async () => {
    window.localStorage.clear();
    const { MUTE_STORAGE_KEY, setMuted } = await import('../toaster/sounds');

    setMuted(true);
    expect(window.localStorage.getItem(MUTE_STORAGE_KEY)).toBe('1');
    expect(looksLikeAToken(window.localStorage.getItem(MUTE_STORAGE_KEY) as string)).toBe(false);

    setMuted(false);
    expect(window.localStorage.getItem(MUTE_STORAGE_KEY)).toBe('0');
  });

  it('the last-selected playbook id (lastPlaybook.ts) persists only the id, never a token', async () => {
    window.localStorage.clear();
    const { LAST_PLAYBOOK_STORAGE_KEY, writeLastPlaybookId } = await import('../lastPlaybook');

    writeLastPlaybookId('eiaa');
    expect(window.localStorage.getItem(LAST_PLAYBOOK_STORAGE_KEY)).toBe('eiaa');
    expect(
      looksLikeAToken(window.localStorage.getItem(LAST_PLAYBOOK_STORAGE_KEY) as string),
    ).toBe(false);
  });

  it('using both allowed preferences together writes exactly those two keys, nothing else', async () => {
    window.localStorage.clear();
    const { MUTE_STORAGE_KEY, setMuted } = await import('../toaster/sounds');
    const { LAST_PLAYBOOK_STORAGE_KEY, writeLastPlaybookId } = await import('../lastPlaybook');

    setMuted(true);
    writeLastPlaybookId('sample-agreement');

    expect(window.localStorage.length).toBe(2);
    const values = [
      window.localStorage.getItem(MUTE_STORAGE_KEY),
      window.localStorage.getItem(LAST_PLAYBOOK_STORAGE_KEY),
    ];
    expect(values.every((value) => typeof value === 'string' && !looksLikeAToken(value))).toBe(
      true,
    );
  });
});
