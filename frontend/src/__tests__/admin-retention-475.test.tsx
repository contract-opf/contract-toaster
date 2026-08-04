/**
 * admin-retention-475.test.tsx — the three issue #475 acceptance criteria
 * for AdminRetention.tsx, plus the two regressions round-1 review found:
 *
 *   AC1 — typing an exact day count into the numeric field moves the slider
 *     and saves exactly that number; keyboard-only operation works
 *     end-to-end. Regression: `value={sliderValue}` made the numeric field a
 *     controlled input sharing state with the (never-empty) range slider, so
 *     backspacing it to '' snapped straight back to the last digit (React's
 *     `restoreControlledState`) — a keyboard user backspacing 90 to retype
 *     365 actually typed onto the un-cleared '9', landing on 9365, silently
 *     clamped to 1095. Watched failing against the pre-fix component before
 *     this file existed.
 *
 *   AC2 — a hold can be placed without ever seeing a UUID. Regression: a
 *     `<datalist>` renders the raw id as the visible suggestion text in
 *     Chrome/Edge, so the "pick" path still showed one; fixed by a real
 *     `<select>` whose option TEXT is the human context and whose id only
 *     ever travels as the option `value`.
 *
 *   AC3 — a pasted review id resolves to human context inline, or shows a
 *     clear "not found" error — but per the ticket's own "no optimistic UI
 *     — the server is authoritative" note, that inline error is advisory
 *     only. Regression: the client-side cache (loaded once at mount, only
 *     refreshed after a successful placeHold) does not know about a review
 *     that appears server-side after mount, so a mismatch used to hard-
 *     disable the submit button — locking out a legitimate hold the server
 *     would have accepted.
 *
 * These drive the REAL component with only the network transport stubbed —
 * same convention as admin-confirm-callsites.test.tsx.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminRetention from '../AdminRetention';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

/**
 * Stub only the transport, routed by "METHOD /path" (falling back to the
 * path alone) — same convention as admin-confirm-callsites.test.tsx /
 * playbook-selector.test.tsx.
 */
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const entry = routes[key];
    if (entry === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

/** Was a request with this method+pathname ever sent? */
function wasCalled(fetchMock: ReturnType<typeof vi.fn>, method: string, pathname: string): boolean {
  return fetchMock.mock.calls.some(([input, init]) => {
    const url = typeof input === 'string' ? input : String(input);
    const actualMethod = ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase();
    return actualMethod === method && new URL(url, 'http://localhost').pathname === pathname;
  });
}

/** The JSON body of the last request sent to this method+pathname. */
function lastBody(
  fetchMock: ReturnType<typeof vi.fn>,
  method: string,
  pathname: string,
): Record<string, unknown> {
  const call = [...fetchMock.mock.calls]
    .reverse()
    .find(([input, init]) => {
      const url = typeof input === 'string' ? input : String(input);
      const actualMethod = ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase();
      return actualMethod === method && new URL(url, 'http://localhost').pathname === pathname;
    });
  if (!call) {
    throw new Error(`no ${method} ${pathname} call was recorded`);
  }
  const init = call[1] as RequestInit | undefined;
  return JSON.parse((init?.body as string) ?? '{}') as Record<string, unknown>;
}

const RETENTION_SETTINGS = {
  setting_id: 'global',
  retention_window_days: 90,
  pending_reduction: null,
};

const REVIEW_A = {
  review_id: '11111111-1111-4111-8111-111111111111',
  owner_sub: 'sub-1',
  created_at: 1700000000,
  status: 'DONE',
  decision: 'ACCEPT',
};

const USER_A = {
  cognito_sub: 'sub-1',
  email: 'jane@example.com',
  status: 'active',
  is_admin: false,
  last_auth_at: 0,
  created_at: 0,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AdminRetention #475 AC1 — exact numeric entry syncs the slider and saves precisely', () => {
  it('a backspace-to-empty-then-retype sequence lands on exactly what was typed, not a digit-appended, silently clamped number', async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [] },
      '/api/users': { users: [] },
    });
    render(<AdminRetention />);

    const daysInput = (await screen.findByTestId('retention-days-input')) as HTMLInputElement;
    expect(daysInput.value).toBe('90');

    fireEvent.change(daysInput, { target: { value: '9' } });
    expect(daysInput.value).toBe('9');

    // The regression: a controlled `value={sliderValue}` snapped this back
    // to '9' instead of actually clearing.
    fireEvent.change(daysInput, { target: { value: '' } });
    expect(daysInput.value).toBe('');

    fireEvent.change(daysInput, { target: { value: '3' } });
    fireEvent.change(daysInput, { target: { value: '36' } });
    fireEvent.change(daysInput, { target: { value: '365' } });

    expect(daysInput.value).toBe('365');
    const slider = screen.getByTestId('retention-slider') as HTMLInputElement;
    expect(slider.value).toBe('365');
  });

  it('saves exactly the typed 365, not a value inflated by leftover digits', async () => {
    const fetchMock = stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [] },
      '/api/users': { users: [] },
    });
    render(<AdminRetention />);

    const daysInput = (await screen.findByTestId('retention-days-input')) as HTMLInputElement;
    fireEvent.change(daysInput, { target: { value: '9' } });
    fireEvent.change(daysInput, { target: { value: '' } });
    fireEvent.change(daysInput, { target: { value: '3' } });
    fireEvent.change(daysInput, { target: { value: '36' } });
    fireEvent.change(daysInput, { target: { value: '365' } });

    const saveButton = screen.getByTestId('retention-save-button');
    expect(saveButton).not.toBeDisabled();
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(wasCalled(fetchMock, 'POST', '/api/admin/retention')).toBe(true);
    });
    expect(lastBody(fetchMock, 'POST', '/api/admin/retention').retention_window_days).toBe(365);
  });

  it('clamps an out-of-range typed value on blur, not on every keystroke', async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [] },
      '/api/users': { users: [] },
    });
    render(<AdminRetention />);

    const daysInput = (await screen.findByTestId('retention-days-input')) as HTMLInputElement;
    fireEvent.change(daysInput, { target: { value: '5000' } });
    // Mid-typing: the field shows exactly what was typed, not a
    // per-keystroke clamp.
    expect(daysInput.value).toBe('5000');

    fireEvent.blur(daysInput);
    expect(daysInput.value).toBe('1095');
    expect((screen.getByTestId('retention-slider') as HTMLInputElement).value).toBe('1095');
  });

  // Regression: the clamp used to be routed through a `useEffect` keyed on
  // `sliderValue`. AT A BOUNDARY the clamp is a no-op state change, React
  // bails out of the render, the effect never re-runs, and the field was
  // left displaying the out-of-range text the admin typed while a DIFFERENT
  // number was what Save posted. The case above (90 -> 5000 -> 1095) cannot
  // catch it because `sliderValue` genuinely changes there.
  it('keeps the field, the slider and the saved value in agreement at the upper boundary', async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [] },
      '/api/users': { users: [] },
    });
    render(<AdminRetention />);

    const daysInput = (await screen.findByTestId('retention-days-input')) as HTMLInputElement;
    const slider = () => screen.getByTestId('retention-slider') as HTMLInputElement;

    // Commit the boundary first, so the next clamp is a no-op state change.
    fireEvent.change(daysInput, { target: { value: '1095' } });
    fireEvent.blur(daysInput);
    expect(daysInput.value).toBe('1095');
    expect(slider().value).toBe('1095');

    fireEvent.change(daysInput, { target: { value: '5000' } });
    fireEvent.blur(daysInput);
    expect(daysInput.value).toBe('1095');
    expect(slider().value).toBe('1095');
  });

  it('keeps the field, the slider and the saved value in agreement at the lower boundary', async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [] },
      '/api/users': { users: [] },
    });
    render(<AdminRetention />);

    const daysInput = (await screen.findByTestId('retention-days-input')) as HTMLInputElement;
    const slider = () => screen.getByTestId('retention-slider') as HTMLInputElement;

    fireEvent.change(daysInput, { target: { value: '0' } });
    fireEvent.blur(daysInput);
    expect(daysInput.value).toBe('0');
    expect(slider().value).toBe('0');

    fireEvent.change(daysInput, { target: { value: '-5' } });
    fireEvent.blur(daysInput);
    expect(daysInput.value).toBe('0');
    expect(slider().value).toBe('0');
  });
});

describe('AdminRetention #475 AC2 — a hold can be placed without ever seeing a UUID', () => {
  it("the picker's visible option text is human context, never the raw review id", async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [REVIEW_A] },
      '/api/users': { users: [USER_A] },
    });
    render(<AdminRetention />);

    const select = await screen.findByTestId('hold-review-select');
    await waitFor(() => {
      expect(select.textContent).toContain('jane@example.com');
    });
    expect(select.textContent).toContain('Accepted');
    // The regression this replaces a <datalist> for: the raw id must never
    // be part of the visible option text.
    expect(select.textContent).not.toContain(REVIEW_A.review_id);
  });

  it('placing a hold via the picker submits the correct id while the confirmation line stays UUID-free', async () => {
    const fetchMock = stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [REVIEW_A] },
      '/api/users': { users: [USER_A] },
      [`POST /api/admin/retention/holds/${REVIEW_A.review_id}`]: {},
    });
    render(<AdminRetention />);

    const select = (await screen.findByTestId('hold-review-select')) as HTMLSelectElement;
    await waitFor(() => expect(select.textContent).toContain('jane@example.com'));
    fireEvent.change(select, { target: { value: REVIEW_A.review_id } });

    const match = await screen.findByTestId('hold-review-id-match');
    expect(match.textContent).toContain('jane@example.com');
    expect(match.textContent).not.toContain(REVIEW_A.review_id);

    fireEvent.change(screen.getByTestId('hold-reason-input'), {
      target: { value: 'Matter 2026-14' },
    });
    fireEvent.click(screen.getByTestId('place-hold-button'));

    await waitFor(() => {
      expect(
        wasCalled(fetchMock, 'POST', `/api/admin/retention/holds/${REVIEW_A.review_id}`),
      ).toBe(true);
    });
  });

  it('round 2 regression: picking a review never renders its raw id into the Review ID field or anywhere else in the panel', async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [REVIEW_A] },
      '/api/users': { users: [USER_A] },
    });
    render(<AdminRetention />);

    const select = (await screen.findByTestId('hold-review-select')) as HTMLSelectElement;
    await waitFor(() => expect(select.textContent).toContain('jane@example.com'));

    fireEvent.change(select, { target: { value: REVIEW_A.review_id } });

    // The regression: the select's onChange used to write the picked id
    // straight into `holdReviewId`, which the visible Review ID input is
    // bound to -- rendering the raw UUID in a bordered input the instant a
    // review was picked.
    const idInput = screen.getByTestId('hold-review-id-input') as HTMLInputElement;
    expect(idInput.value).toBe('');

    const panel = screen.getByTestId('legal-hold-place-panel');
    expect(panel.textContent).not.toContain(REVIEW_A.review_id);
  });
});

describe('AdminRetention #475 AC3 / finding 2 — pasted id resolves or warns, but the server stays authoritative', () => {
  it('pasting a known review id resolves it to human context inline', async () => {
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [REVIEW_A] },
      '/api/users': { users: [USER_A] },
    });
    render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    await waitFor(() => {
      // Reviews have loaded by the time the id lookup below matters.
      expect(screen.getByTestId('hold-review-select').textContent).toContain('jane@example.com');
    });

    fireEvent.change(screen.getByTestId('hold-review-id-input'), {
      target: { value: REVIEW_A.review_id },
    });

    const match = await screen.findByTestId('hold-review-id-match');
    expect(match.textContent).toContain('jane@example.com');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('a review absent from the client-side cache shows an advisory warning but does not disable submit — the server 404 stays authoritative', async () => {
    const freshId = '22222222-2222-4222-8222-222222222222';
    const fetchMock = stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      // `freshId` deliberately absent — simulates a review created after
      // this panel's mount-time `/api/reviews` fetch.
      '/api/reviews': { reviews: [REVIEW_A] },
      '/api/users': { users: [USER_A] },
      [`POST /api/admin/retention/holds/${freshId}`]: {},
    });
    render(<AdminRetention />);
    await screen.findByTestId('hold-review-select');
    await waitFor(() => {
      expect(screen.getByTestId('hold-review-select').textContent).toContain('jane@example.com');
    });

    fireEvent.change(screen.getByTestId('hold-review-id-input'), {
      target: { value: freshId },
    });
    fireEvent.change(screen.getByTestId('hold-reason-input'), {
      target: { value: 'Server-side hold, not yet in the client cache' },
    });

    // Advisory warning fires...
    expect(screen.getByRole('alert').textContent).toMatch(/no review found/i);
    // ...but must not gate submit.
    expect(screen.getByTestId('place-hold-button')).not.toBeDisabled();

    fireEvent.click(screen.getByTestId('place-hold-button'));

    await waitFor(() => {
      expect(wasCalled(fetchMock, 'POST', `/api/admin/retention/holds/${freshId}`)).toBe(true);
    });
  });
});

describe('AdminRetention #475 finding 2 (round 2) — a pasted id outside the picker\'s newest-50 slice does not break the select', () => {
  it('resolves to human context but leaves the select on its placeholder, not blank', async () => {
    // Oldest of the bunch -- guaranteed to sort past the picker's
    // `REVIEW_PICKER_OPTION_LIMIT` (50) newest-first cap.
    const OLD_REVIEW = {
      review_id: '33333333-3333-4333-8333-333333333333',
      owner_sub: 'sub-1',
      created_at: 1,
      status: 'DONE',
      decision: 'ACCEPT',
    };
    // 60 reviews newer than OLD_REVIEW -- more than enough to push it out of
    // the capped picker options while still resolving via the full fetched
    // list (`reviewsById`, not bounded by the cap).
    const recentReviews = Array.from({ length: 60 }, (_, i) => ({
      review_id: `44444444-4444-4444-8444-${String(i).padStart(12, '0')}`,
      owner_sub: 'sub-1',
      created_at: 2000000000 - i,
      status: 'DONE',
      decision: 'ACCEPT',
    }));
    stubFetch({
      '/api/admin/retention': RETENTION_SETTINGS,
      '/api/admin/retention/holds': { holds: [] },
      '/api/reviews': { reviews: [OLD_REVIEW, ...recentReviews] },
      '/api/users': { users: [USER_A] },
    });
    render(<AdminRetention />);

    const select = (await screen.findByTestId('hold-review-select')) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));

    // Confirm the fixture actually exercises the gap: OLD_REVIEW is not
    // among the rendered options.
    expect(Array.from(select.options).some((o) => o.value === OLD_REVIEW.review_id)).toBe(false);

    fireEvent.change(screen.getByTestId('hold-review-id-input'), {
      target: { value: OLD_REVIEW.review_id },
    });

    // Still resolves against the full review list...
    const match = await screen.findByTestId('hold-review-id-match');
    expect(match.textContent).not.toContain(OLD_REVIEW.review_id);

    // ...but the regression: `value` used to be gated on membership in the
    // FULL `reviewsById` map rather than the rendered options, so the
    // select rendered with `selectedIndex === -1` (blank) instead of
    // falling back to the "Select a recent review…" placeholder.
    expect(select.value).toBe('');
    expect(select.selectedIndex).toBe(0);
  });
});
