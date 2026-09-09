/**
 * Issue #656: a dial stop's accessible name must carry its GROUP's meaning.
 *
 * The groups were already labelled correctly (`aria-labelledby` on each
 * `radiogroup`, resolving to a real element) and each stop was already a real
 * `role="radio"` with `aria-checked`. What was missing was on the stop itself:
 * its accessible name came from its visible text ALONE, so `Light`, `None` and
 * `Both` were unambiguous inside the group and ambiguous the moment they were
 * announced on their own -- which is how a screen reader reads a control the
 * user tabs onto after the group label has scrolled past.
 *
 * It also cost a false regression report: driving the UI, a coordinate-derived
 * click landed on nothing and a capture-phase listener proved ZERO events
 * reached the button, which is indistinguishable from "the control is broken"
 * until you instrument it. Names a driver can resolve unambiguously remove that
 * whole failure mode.
 *
 * These assertions fail on the pre-#656 tree: `getByRole('radio', { name: ... })`
 * matches on the ACCESSIBLE NAME, so every query below misses when the name is
 * the bare visible text.
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import type React from 'react';

import App from '../App';

// Same offline convention as the sibling dial/tab tests: amplify is mocked so
// the authenticator renders its children directly instead of a sign-in form.
// Without these the app renders the Amplify sign-in screen and none of the
// review controls exist to assert on.
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

const ROUTES: Record<string, unknown> = {
  '/version': { version: '0.0.1', commit: 'abcdef1234567890', image_digest: 'sha256:x', uptime_seconds: 1 },
  '/api/me': { is_admin: true },
  '/api/users': { users: [] },
  '/api/users/sync-status': { last_sync_at: null, status: 'idle' },
  '/api/admin/retention': { retention_days: 30 },
  '/api/admin/retention/holds': { holds: [] },
  '/api/admin/diagnostics/recent-failures': { failures: [] },
  '/api/admin/model-key': { configured: true },
  '/api/admin/model-selection': { default_primary: 'x', default_critic: 'y' },
  '/api/admin/auth-mode': { mode: 'password' },
  '/api/me/preferences': { preferences: {}, notes_mode_available: true },
  '/api/playbooks': {
    playbooks: [
      { playbook_id: 'synthetic-nda-sample', display_name: 'Synthetic NDA Sample', status: 'active', version: '1.0.0' },
    ],
  },
  '/api/reviews': { reviews: [] },
};

function stubFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      const body = ROUTES[pathname];
      if (body === undefined) return { ok: false, status: 404, json: async () => ({}) } as Response;
      return { ok: true, status: 200, json: async () => body } as Response;
    }),
  );
}

describe('the console names its groups natively (#733)', () => {
  // The catalog has to be answered for the playbook dial to have anything to
  // offer; the second test re-stubs `fetch` for its own scenario, so the
  // unstub is what keeps the two independent.
  beforeEach(() => stubFetch());
  afterEach(() => vi.unstubAllGlobals());

  it('groups intensity and footnotes in labelled fieldsets, and names the playbook control', async () => {
    render(<App />);

    // A native fieldset/legend: the legend IS the group name, and each radio
    // keeps its own visible text as its name — the pair a screen reader reads
    // together without either being restated in the other.
    const markup = await screen.findByTestId('review-browning-control');
    expect(markup.tagName).toBe('FIELDSET');
    expect(markup).toHaveAccessibleName(/markup intensity/i);
    for (const stop of ['Light', 'Medium', 'Dark']) {
      expect(within(markup).getByRole('radio', { name: stop })).toBeInTheDocument();
    }

    const footnotes = screen.getByTestId('review-notes-mode-control');
    expect(footnotes.tagName).toBe('FIELDSET');
    expect(footnotes).toHaveAccessibleName(/footnotes/i);
    for (const stop of ['None', 'External', 'Internal', 'Both']) {
      expect(within(footnotes).getByRole('radio', { name: stop })).toBeInTheDocument();
    }

    expect(screen.getByTestId('review-playbook-dial')).toHaveAccessibleName('Playbook type');
  });

  it('carries the unavailable state as a real disabled control, not only as styling', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        const pathname = new URL(url, 'http://localhost').pathname;
        if (pathname === '/api/me/preferences') {
          return {
            ok: true,
            status: 200,
            json: async () => ({ preferences: {}, notes_mode_available: false }),
          } as Response;
        }
        const body = ROUTES[pathname];
        if (body === undefined) return { ok: false, status: 404, json: async () => ({}) } as Response;
        return { ok: true, status: 200, json: async () => body } as Response;
      }),
    );
    render(<App />);

    const footnotes = await screen.findByTestId('review-notes-mode-control');
    // `disabled` rather than `aria-disabled`: a native control that refuses is
    // announced as unavailable AND cannot be reached by keyboard, which is the
    // state #656's name suffix was standing in for.
    expect(within(footnotes).getByRole('radio', { name: 'Internal' })).toBeDisabled();
    expect(within(footnotes).getByRole('radio', { name: 'Both' })).toBeDisabled();
    expect(within(footnotes).getByRole('radio', { name: 'External' })).toBeEnabled();
  });
});
