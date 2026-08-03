/**
 * admin-users-never-signed-in.test.tsx — a never-signed-in row renders "never"
 * and its type admits the null the backend actually sends (issue #452).
 *
 * `backend/src/demo_auth.py` writes `last_auth_at: None` for every seeded demo
 * row and for every admin-added user, so `GET /api/users` demonstrably returns
 * JSON `null` in that field (proved server-side in
 * tests/test_users_null_last_auth_452.py). `UserRow.last_auth_at` was declared
 * `number`, which is a type LIE about the wire shape — not a runtime bug,
 * because `formatTimestamp` already accepts `number | null` and returns
 * 'never', but a lie that would let a future edit "simplify" that guard away
 * on the grounds that null is impossible.
 *
 * WATCH IT FAIL FIRST — against the pre-fix `last_auth_at: number`, the two
 * `UserRow`-annotated fixtures below are a `tsc` error, so `npm run build:ci`
 * (and therefore scripts/check-frontend.sh) fails:
 *
 *   src/__tests__/admin-users-never-signed-in.test.tsx(NN,3): error TS2322:
 *     Type 'null' is not assignable to type 'number'.
 *
 * The annotation is what makes this a real red: the other Users tests feed
 * untyped object literals through a fetch stub, which `tsc` never checks
 * against `UserRow` at all.
 *
 * Drives the REAL component with only the network transport stubbed. Fully
 * offline — aws-amplify/auth is mocked, fetch is stubbed per test. jsdom runs
 * with `css: false` (vitest.config.ts), so this asserts text content only.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import AdminUsers from '../AdminUsers';
import type { UserRow } from '../AdminUsers';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const SYNC_STATUS_OK = {
  sync_type: 'workspace',
  last_run_at: null,
  last_run_outcome: null,
  users_deprovisioned_count: 0,
  next_run_at: null,
};

/**
 * The two seeded demo rows, exactly as `seed_demo_users` writes them: password
 * rows with `username`, no `email`, and `last_auth_at: null`.
 */
const NEVER_SIGNED_IN: UserRow[] = [
  {
    cognito_sub: 'local:admin',
    username: 'admin',
    status: 'active',
    is_admin: true,
    last_auth_at: null,
    created_at: 1_700_000_000,
    admission: 'seed',
  },
  {
    cognito_sub: 'local:user',
    username: 'user',
    status: 'active',
    is_admin: false,
    last_auth_at: null,
    created_at: 1_700_000_000,
    admission: 'seed',
  },
];

/** A row that HAS signed in, so "never" cannot be the only thing rendered. */
const SIGNED_IN: UserRow = {
  cognito_sub: 'sub-sso',
  email: 'sso.person@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 1_700_000_500,
  created_at: 1_700_000_000,
  admission: 'jit',
};

function stubFetch(users: UserRow[]) {
  const fetchStub = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/users/sync-status')) {
      return new Response(JSON.stringify(SYNC_STATUS_OK), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/api/users')) {
      return new Response(JSON.stringify({ users }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal('fetch', fetchStub);
  return fetchStub;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('AdminUsers — rows that have never signed in (#452)', () => {
  it('renders "never" for every null last_auth_at, not a 1970 date', async () => {
    stubFetch(NEVER_SIGNED_IN);
    render(<AdminUsers />);

    for (const row of NEVER_SIGNED_IN) {
      const tr = await screen.findByTestId(`user-row-${row.cognito_sub}`);
      expect(tr.textContent).toContain('never');
      // The epoch-1970 rendering a sentinel 0 would have produced.
      expect(tr.textContent).not.toContain('1970');
    }
  });

  it('still renders a real timestamp for a row that has signed in', async () => {
    stubFetch([SIGNED_IN, ...NEVER_SIGNED_IN]);
    render(<AdminUsers />);

    const tr = await screen.findByTestId(`user-row-${SIGNED_IN.cognito_sub}`);
    expect(tr.textContent).not.toContain('never');
    expect(tr.textContent).toContain(
      new Date(SIGNED_IN.last_auth_at! * 1000).toLocaleString(),
    );
  });
});
