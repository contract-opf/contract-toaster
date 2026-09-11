/**
 * admin-users-columns-608.test.tsx — issue #608 (#602 split): the add-user
 * card's Username and Initial password fields are paired into a single
 * `ct-columns` row (#601) instead of stacking full-width.
 *
 * jsdom implements no layout and `vitest.config.ts` sets `css: false`, so
 * nothing here can observe an actual two-column reflow — the columnar
 * behaviour itself is guarded statically by layout-audit.mjs checks 4/5
 * (#601). What IS a stylesheet-independent DOM fact is WIRING: whether the
 * username input's nearest `ct-columns` ancestor is the same element the
 * password input's is, that Generate rides along in the PASSWORD column
 * rather than wrapping onto its own grid row under the username, and that
 * the source order the keyboard follows is still username → password →
 * Generate. Same convention, and the same reason, as
 * admin-retention-columns-602.test.tsx.
 *
 * The SSO branch is asserted to stay OUT of the primitive: an email field
 * alone is a single logical control with nothing to pair against
 * (docs/frontend-design-system.md §6).
 *
 * Watched failing against the pre-#608 component (both fields plain
 * children of the add form's `ct-stack`, no `ct-columns` ancestor at all).
 *
 * Fully offline — aws-amplify/auth is mocked, fetch is stubbed.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import AdminUsers from '../AdminUsers';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const REVIEWER_ROW = {
  cognito_sub: 'sub-reviewer',
  email: 'reviewer@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
  admission: 'jit',
};

const SYNC_STATUS_OK = {
  sync_type: 'workspace',
  last_run_at: null,
  last_run_outcome: null,
  users_deprovisioned_count: 0,
  next_run_at: null,
};

function stubRoutes(authMode: string): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      if (pathname === '/api/users') {
        return { ok: true, status: 200, json: async () => ({ users: [REVIEWER_ROW] }) } as Response;
      }
      if (pathname === '/api/users/sync-status') {
        return { ok: true, status: 200, json: async () => SYNC_STATUS_OK } as Response;
      }
      if (pathname === '/api/admin/auth-mode') {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            setting_id: 'global',
            auth_mode: authMode,
            default_auth_mode: 'sso',
            auth_mode_options: [],
          }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
}

/** The control that exists ONLY once the `/api/admin/auth-mode` probe has
 *  been applied, per mode. `sso` has no entry on purpose: it is the pre-probe
 *  default, so nothing in the card changes when that probe confirms it. */
const PROBE_APPLIED_TESTID: Record<string, string | undefined> = {
  password: 'admin-users-add-username',
  both: 'admin-users-add-type',
};

/** Renders the screen in `authMode` and opens the (collapsed) add-user card.
 *
 * The card's SHAPE comes from the `/api/admin/auth-mode` probe, which is a
 * second authenticated call behind the roster read — so `await`ing the roster
 * row never proved the card had been re-rendered for `authMode`. It merely
 * happened to win the race. Issue #56 routed `auth.ts::getToken` through a
 * dynamic `import('aws-amplify/auth')`, putting every authenticated call one
 * microtask further out, and the bare `getByTestId` in each case below
 * started losing it under load. Waiting on the probe's own control is what
 * makes the helper mean what it always claimed. */
async function openAddCard(authMode: string): Promise<HTMLElement> {
  stubRoutes(authMode);
  render(<AdminUsers />);
  await screen.findByTestId('user-row-sub-reviewer');
  fireEvent.click(screen.getByTestId('admin-users-add-toggle'));
  const marker = PROBE_APPLIED_TESTID[authMode];
  if (marker) {
    await screen.findByTestId(marker);
  }
  return screen.getByTestId('admin-users-add-panel');
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminUsers / #608 — username and initial password share one ct-columns row', () => {
  it('desktop rendering nests both credential fields inside the SAME ct-columns host', async () => {
    await openAddCard('password');

    const usernameHost = screen.getByTestId('admin-users-add-username').closest('ct-columns');
    const passwordHost = screen.getByTestId('admin-users-add-password').closest('ct-columns');

    expect(usernameHost).not.toBeNull();
    expect(passwordHost).not.toBeNull();
    expect(usernameHost).toBe(passwordHost);
  });

  it('Generate rides in the password column, not on a grid row of its own under the username', async () => {
    await openAddCard('password');

    const columns = screen.getByTestId('admin-users-add-username').closest('ct-columns');
    const generate = screen.getByTestId('admin-users-add-generate');
    const passwordColumn = Array.from(columns?.children ?? []).find((child) =>
      child.contains(screen.getByTestId('admin-users-add-password')),
    );

    // Exactly two grid children — a third would wrap onto a second row,
    // putting Generate under the USERNAME field it has nothing to do with.
    expect(columns?.children.length).toBe(2);
    expect(passwordColumn).toBeDefined();
    expect(passwordColumn?.contains(generate)).toBe(true);
    expect(passwordColumn?.contains(screen.getByTestId('admin-users-add-username'))).toBe(false);
  });

  it('collapsed (source/DOM) reading order is untouched: username, then password, then Generate', async () => {
    const panel = await openAddCard('password');

    // `ct-columns` only repositions children visually (ct-columns.ts), so
    // source order — which is what tab order follows — must be unchanged.
    const order = Array.from(panel.querySelectorAll('[data-testid]')).map((el) =>
      el.getAttribute('data-testid'),
    );
    expect(order.indexOf('admin-users-add-username')).toBeGreaterThanOrEqual(0);
    expect(order.indexOf('admin-users-add-username')).toBeLessThan(
      order.indexOf('admin-users-add-password'),
    );
    expect(order.indexOf('admin-users-add-password')).toBeLessThan(
      order.indexOf('admin-users-add-generate'),
    );
  });

  it('no control lost in the password branch: every pre-existing testid still resolves', async () => {
    await openAddCard('password');
    for (const testid of [
      'admin-users-add-username',
      'admin-users-add-password',
      'admin-users-add-generate',
      'admin-users-add-is-admin',
      'admin-users-add-is-admin-row',
      'admin-users-add-submit',
    ]) {
      expect(screen.getByTestId(testid)).toBeTruthy();
    }
  });
});

describe('AdminUsers / #608 — the SSO branch stays a single column', () => {
  it('the lone Email field is NOT put into a ct-columns row (nothing to pair it against)', async () => {
    await openAddCard('sso');

    const email = screen.getByTestId('admin-users-add-email');
    expect(email.closest('ct-columns')).toBeNull();
    expect(screen.queryByTestId('admin-users-add-username')).toBeNull();
  });

  it('no control lost in the SSO branch: every pre-existing testid still resolves', async () => {
    await openAddCard('sso');
    for (const testid of [
      'admin-users-add-email',
      'admin-users-add-is-admin',
      'admin-users-add-submit',
    ]) {
      expect(screen.getByTestId(testid)).toBeTruthy();
    }
  });
});

describe('AdminUsers / #608 — the user-type select stays a single control', () => {
  it('in "both" mode the type select sits outside the credential columns row', async () => {
    await openAddCard('both');

    const typeSelect = screen.getByTestId('admin-users-add-type');
    expect(typeSelect.closest('ct-columns')).toBeNull();

    // Switching to the password type reveals the pair — and the type
    // select is still not part of it.
    fireEvent.change(typeSelect, { target: { value: 'password' } });
    expect(screen.getByTestId('admin-users-add-username').closest('ct-columns')).not.toBeNull();
    expect(screen.getByTestId('admin-users-add-type').closest('ct-columns')).toBeNull();
  });
});
