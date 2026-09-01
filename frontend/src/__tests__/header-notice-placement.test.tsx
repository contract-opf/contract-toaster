/**
 * header-notice-placement.test.tsx — the default-password warning belongs in
 * the shell's full-width notice row, never inside the identity cluster, and
 * (issue #603) that row now sits at the FOOT of the page.
 *
 * Reported 2026-08-04 from a screenshot of the live deployment: the header
 * read as a broken layout, with "Contract Toaster Review Tool" stacked one
 * word per line beside a wide amber alert box sitting inline between "Sign
 * out" and the page edge.
 *
 * The cause was structural, not cosmetic. ct-app-shell is a CSS grid whose
 * identity track is sized to its content, and App.tsx rendered <CtBanner>
 * (a block-level alert) as a child of `slot="identity"` — so the alert's
 * width set the identity column's width and squeezed the brand column down
 * to its min-content. Restyling the banner would not have fixed it; the
 * banner had to leave that cluster.
 *
 * This test asserts the DOM relationship rather than any visual property,
 * because the relationship is what the grid reacts to: a jsdom test cannot
 * see the stacking, but it can see the containment that causes it.
 *
 * Issue #603 moved the row to the bottom of the page. The assertions added
 * for it are deliberately about DOCUMENT ORDER, not CSS position: jsdom
 * computes no layout, so an assertion about where the banner is PAINTED
 * would be worthless here — and document order is the stronger property
 * anyway, since it is also what a screen reader and the tab ring follow. A
 * CSS-only reorder (a `grid-area` move with the element left in the header)
 * would pass a paint-position check and fail these.
 *
 * Fully offline — aws-amplify/auth and @aws-amplify/ui-react are mocked,
 * fetch is stubbed. No live AWS/Cognito/network.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import App from '../App';

// The banner appears only after TWO sequential GET /api/me probes resolve
// (the admin gate and the credentials warning are deliberately independent —
// see App.tsx's useDefaultCredentialsWarning). Under a full parallel suite
// run that reliably exceeds testing-library's 1s default and fails the file
// while it passes in isolation. These assertions are about DOM structure,
// not latency, so they get room rather than a race.
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

// The exact /api/me shape that produces the reported header: a signed-in
// admin still on the shipped default password.
const ME_WITH_WARNING = {
  username: 'admin',
  is_admin: true,
  default_credentials_warning: true,
};

describe('default-password warning placement', () => {
  // The warning is password-mode only (an SSO row has no shipped password to
  // warn about), so every case here runs the Docker Compose auth path.
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('renders the warning outside the identity cluster', async () => {
    stubFetch({ '/api/me': ME_WITH_WARNING, '/version': { version: '0', commit: 'abcdef12' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    const banner = await screen.findByTestId('default-credentials-warning', {}, APPEAR_TIMEOUT);
    const identity = document.querySelector('[slot="identity"]');

    expect(identity).not.toBeNull();
    expect(identity?.contains(banner)).toBe(false);
  });

  it('places the warning in the shell notice row', async () => {
    stubFetch({ '/api/me': ME_WITH_WARNING, '/version': { version: '0', commit: 'abcdef12' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    const banner = await screen.findByTestId('default-credentials-warning', {}, APPEAR_TIMEOUT);
    expect(banner.closest('[slot="notice"]')).not.toBeNull();
  });

  it('renders no notice row at all when the password has been changed', async () => {
    stubFetch({
      '/api/me': { username: 'admin', is_admin: true, default_credentials_warning: false },
      '/version': { version: '0', commit: 'abcdef12' },
    });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    // Wait for the identity cluster so the probes have resolved before
    // asserting on an absence.
    await waitFor(() => expect(screen.getByTestId('user-email')).toBeTruthy(), APPEAR_TIMEOUT);
    expect(screen.queryByTestId('default-credentials-warning')).toBeNull();
    expect(document.querySelector('[slot="notice"]')).toBeNull();
  });

  // ---- issue #603: the row moved to the foot of the page ----------------

  it('renders the warning AFTER every tabpanel in document order', async () => {
    stubFetch({ '/api/me': ME_WITH_WARNING, '/version': { version: '0', commit: 'abcdef12' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    const banner = await screen.findByTestId('default-credentials-warning', {}, APPEAR_TIMEOUT);
    const panels = Array.from(document.querySelectorAll('[role="tabpanel"]'));
    expect(panels.length).toBeGreaterThan(0);

    for (const panel of panels) {
      // DOCUMENT_POSITION_FOLLOWING: the banner comes after this panel in
      // the tree. Not "is painted below" — see this file's docstring.
      const relation = panel.compareDocumentPosition(banner);
      expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  it('renders the warning BEFORE the footer, not adrift past it', async () => {
    stubFetch({ '/api/me': ME_WITH_WARNING, '/version': { version: '0', commit: 'abcdef12' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    const banner = await screen.findByTestId('default-credentials-warning', {}, APPEAR_TIMEOUT);
    const footer = document.querySelector('[slot="footer"]');
    expect(footer).not.toBeNull();
    expect(footer!.compareDocumentPosition(banner) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });

  it('keeps a working change-password affordance in the moved warning', async () => {
    stubFetch({ '/api/me': ME_WITH_WARNING, '/version': { version: '0', commit: 'abcdef12' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    const banner = await screen.findByTestId('default-credentials-warning', {}, APPEAR_TIMEOUT);
    // "Move it" must not silently become "drop it": the warning still says
    // what is wrong AND still offers the remedy, now that it no longer sits
    // beside the identity cluster's own button.
    expect(banner.textContent).toContain('shipped default password');

    const link = screen.getByTestId('default-credentials-change-link') as HTMLAnchorElement;
    expect(banner.contains(link)).toBe(true);
    const fragment = link.getAttribute('href') ?? '';
    expect(fragment.startsWith('#')).toBe(true);

    // The link must point at something that EXISTS — a fragment with no
    // target is a dead affordance, which is the failure this guards.
    // ct-button forwards data-testid to the inner <button> it builds, so the
    // id sits on the ct-button HOST — the anchor target is that host, and the
    // control it opens must be inside it.
    const target = document.getElementById(fragment.slice(1));
    expect(target).not.toBeNull();
    expect(target!.contains(screen.getByTestId('change-password-open'))).toBe(true);
  });

  it('keeps the identity cluster to identity controls only', async () => {
    stubFetch({ '/api/me': ME_WITH_WARNING, '/version': { version: '0', commit: 'abcdef12' } });
    vi.stubEnv('VITE_AUTH_MODE', 'password');
    render(<App />);

    await screen.findByTestId('default-credentials-warning', {}, APPEAR_TIMEOUT);
    const identity = document.querySelector('[slot="identity"]');
    // A block-level alert anywhere in this cluster is the bug, whatever its
    // testid: the cluster is a wrap-friendly row of small inline controls.
    expect(identity?.querySelector('ct-banner')).toBeNull();
  });
});
