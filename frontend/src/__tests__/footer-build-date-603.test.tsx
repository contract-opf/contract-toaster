/**
 * footer-build-date-603.test.tsx — the footer shows WHEN this build was
 * published, not just which build it is (issue #603).
 *
 * The publish workflow tags every image `<short-sha>-$(date -u
 * +%Y%m%d%H%M%S)` and bakes it in as VERSION, so the instant is already in
 * the footer string — as fourteen undelimited digits nobody reads. This
 * parses it out.
 *
 * TIMEZONE: rendered as UTC, because the stamp IS UTC (`date -u` in
 * .github/workflows/dts-image-publish.yml) and the footer is read against
 * workflow logs and `docker image ls`, which are UTC too. That also makes
 * these assertions independent of the runner's TZ, so a literal expected
 * string here is a real assertion rather than a re-derivation of the code
 * under test.
 *
 * The degradation case matters as much as the happy one: VERSION defaults to
 * `dev`, with no suffix at all, and the footer must then show no date —
 * never the string "Invalid Date", which is what any unguarded
 * `new Date(...)` render produces.
 *
 * Fully offline — aws-amplify/auth and @aws-amplify/ui-react are mocked,
 * fetch is stubbed. No live AWS/Cognito/network.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import App, { buildTimestampFromVersion } from '../App';

const APPEAR_TIMEOUT = { timeout: 5000 };

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
    user: { username: 'admin', signInDetails: { loginId: 'admin' } },
    signOut: vi.fn(),
  }),
}));

function stubFetch(routes: Record<string, unknown>): void {
  const impl = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const body = routes[pathname];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

const ME = { username: 'admin', is_admin: true, default_credentials_warning: false };

describe('buildTimestampFromVersion (issue #603)', () => {
  it('reads the timestamp suffix the publish workflow stamps', () => {
    // The live example recorded in the ticket.
    expect(buildTimestampFromVersion('cb79b4a-20260820191617')).toBe('20 Aug 2026, 19:16 UTC');
  });

  it('returns null for VERSION=dev, the shipped default', () => {
    expect(buildTimestampFromVersion('dev')).toBeNull();
  });

  it('returns null for a bare sha with no timestamp suffix', () => {
    expect(buildTimestampFromVersion('cb79b4a')).toBeNull();
    expect(buildTimestampFromVersion('')).toBeNull();
    expect(buildTimestampFromVersion(undefined)).toBeNull();
  });

  it('returns null rather than a rolled-over date for impossible parts', () => {
    // Date.UTC does not reject these, it rolls them over: month 13 becomes
    // January of the following year, day 32 becomes the 1st of the next.
    // Reporting a confidently wrong build date is worse than reporting none.
    expect(buildTimestampFromVersion('abc-20261320191617')).toBeNull();
    expect(buildTimestampFromVersion('abc-20260832191617')).toBeNull();
    expect(buildTimestampFromVersion('abc-20260820991617')).toBeNull();
  });

  it('ignores a suffix that is not exactly fourteen digits', () => {
    expect(buildTimestampFromVersion('abc-2026082019161')).toBeNull();
    expect(buildTimestampFromVersion('abc-202608201916177')).toBeNull();
  });
});

describe('footer build date (issue #603)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('renders a readable build date beside the version', async () => {
    stubFetch({ '/api/me': ME, '/version': { version: 'cb79b4a-20260820191617', commit: 'cb79b4a1' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    const builtAt = await screen.findByTestId('version-built-at', {}, APPEAR_TIMEOUT);
    expect(builtAt.textContent).toContain('20 Aug 2026, 19:16 UTC');

    // The existing deploy-verification hook is untouched — other tests match
    // its text exactly (stale-signals-592.test.tsx).
    expect(screen.getByTestId('version-display').textContent).toContain('Version cb79b4a-20260820191617');
  });

  it('renders no date at all — and no "Invalid Date" — for VERSION=dev', async () => {
    stubFetch({ '/api/me': ME, '/version': { version: 'dev', commit: 'abcdef12' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    const versionDisplay = await screen.findByTestId('version-display', {}, APPEAR_TIMEOUT);
    expect(versionDisplay.textContent).toContain('Version dev');

    expect(screen.queryByTestId('version-built-at')).toBeNull();
    const footer = document.querySelector('[slot="footer"]');
    expect(footer?.textContent).not.toContain('Invalid Date');
    expect(footer?.textContent).not.toContain('NaN');
  });
});
