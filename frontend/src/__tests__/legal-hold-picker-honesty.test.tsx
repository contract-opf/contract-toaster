/**
 * legal-hold-picker-honesty.test.tsx — issue #525.
 *
 * The picker exists so an admin never has to fish a UUID out of somewhere
 * else (#475 AC2). When `GET /api/reviews` fails it still rendered under
 * "Pick a recent review" with the placeholder as its only option — a dropdown
 * that opens to nothing, with no explanation.
 *
 * Functionally the degradation was already right: pasting a full id still
 * works and is validated server-side. This is purely about telling the truth
 * on screen, and the property under test is that the three states are
 * DISTINGUISHABLE:
 *
 *   the list loaded and has reviews  -> pick one
 *   the list loaded and is empty     -> there is nothing to hold yet
 *   the list failed to load          -> paste an id instead
 *
 * The first two need no action from the admin. The third does. Rendering all
 * three the same way is what made the bug.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import AdminRetention from '../AdminRetention';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const SETTINGS = {
  retention_window_days: 90,
  min_days: 1,
  max_days: 365,
  updated_by: null,
  updated_at: null,
};

const REVIEW = {
  review_id: 'rev-1',
  created_at: 1_700_000_000,
  owner_sub: 'sub-1',
  status: 'DONE',
  decision: 'ACCEPT',
  playbook_id: 'eiaa',
};

function stub({ reviewsOk = true, usersOk = true, reviews = [REVIEW] } = {}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === '/api/reviews') {
      return reviewsOk
        ? ({ ok: true, status: 200, json: async () => ({ reviews }) } as Response)
        : ({ ok: false, status: 404, json: async () => ({}) } as Response);
    }
    if (pathname === '/api/users') {
      return usersOk
        ? ({ ok: true, status: 200, json: async () => ({ users: [] }) } as Response)
        : ({ ok: false, status: 500, json: async () => ({}) } as Response);
    }
    if (pathname === '/api/admin/retention') {
      return { ok: true, status: 200, json: async () => SETTINGS } as Response;
    }
    if (pathname === '/api/admin/retention/holds') {
      return { ok: true, status: 200, json: async () => ({ holds: [] }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  });
}

function hintText(): string {
  // CtField renders its hint into the field's light DOM; read the whole field
  // rather than reaching for an internal structure this test does not own.
  return screen.getByTestId('hold-review-picker-field').textContent ?? '';
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #525 — the picker says which of the three states it is in', () => {
  it('a FAILED review list says so, and points at the paste path', async () => {
    vi.stubGlobal('fetch', stub({ reviewsOk: false }));
    render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    await waitFor(() => expect(hintText()).toMatch(/could not be loaded/i));
    // The whole point of the failure branch: it must not keep promising a
    // picker it cannot provide, and it must name the alternative that works.
    expect(hintText()).toMatch(/paste/i);
    expect(hintText()).not.toMatch(/no need to know its ID/i);
  });

  it('an EMPTY review list reads as "nothing to hold yet", never as a failure', async () => {
    vi.stubGlobal('fetch', stub({ reviews: [] }));
    render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    await waitFor(() => expect(hintText()).toMatch(/no reviews have been submitted/i));
    expect(hintText()).not.toMatch(/could not be loaded/i);
  });

  it('a LOADED list keeps the original hint', async () => {
    vi.stubGlobal('fetch', stub());
    render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    await waitFor(() => expect(hintText()).toMatch(/no need to know its ID/i));
  });

  it('the empty and failed states are not the same message', async () => {
    // Guards the actual defect rather than either branch in isolation: the two
    // states rendered identically, which is why an admin could not tell them
    // apart.
    vi.stubGlobal('fetch', stub({ reviews: [] }));
    const { unmount } = render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    await waitFor(() => expect(hintText()).toMatch(/nothing to pick from/i));
    const empty = hintText();
    unmount();

    vi.stubGlobal('fetch', stub({ reviewsOk: false }));
    render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    await waitFor(() => expect(hintText()).toMatch(/could not be loaded/i));
    expect(hintText()).not.toBe(empty);
  });
});

describe('issue #525 — a failed user directory explains the degraded submitter column', () => {
  it('says the identities are degraded rather than showing bare ids silently', async () => {
    vi.stubGlobal('fetch', stub({ usersOk: false }));
    render(<AdminRetention />);
    await screen.findByTestId('legal-holds-identities-degraded');
  });

  it('says nothing when the directory loaded', async () => {
    vi.stubGlobal('fetch', stub());
    render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    expect(screen.queryByTestId('legal-holds-identities-degraded')).toBeNull();
  });
});
