/**
 * error-boundary-and-session.test.tsx — issue #487.
 *
 * Two resilience gaps that would embarrass a prominent beta in its first week:
 *
 *   1. No React error boundary anywhere. ANY render-time exception in ANY
 *      panel white-screened all eight tabs at once, including a review in
 *      flight. For a legal tool the failure mode has to be "this screen hit a
 *      problem", never a dead page.
 *   2. No 401 interception. When the session expired mid-use, every panel
 *      independently started failing with its own "We couldn't load…" copy,
 *      and none of them said the one true, actionable thing: you are signed
 *      out.
 *
 * The boundary is tested against a component that genuinely throws during
 * render, not a mock of one — a boundary that catches a stubbed error but not
 * a real one is exactly the thing that would fail in production.
 */
import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ErrorBoundary, PANEL_ERROR_COPY } from '../ErrorBoundary';
import { authorizedFetch, onSessionExpired, __resetSessionExpiredListeners } from '../api';

const SECRET = 'the counterparty shall indemnify nobody in particular';

function Boom(): React.ReactElement {
  // A real throw during render, and its message deliberately contains
  // document-shaped text — the boundary must not paint it.
  throw new Error(`render exploded on ${SECRET}`);
}

function Fine(): React.ReactElement {
  return <p data-testid="fine">still here</p>;
}

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React logs caught boundary errors itself; silence it so a passing run is
  // readable, and so the assertion below is about OUR console call.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  __resetSessionExpiredListeners();
});

afterEach(() => {
  consoleError.mockRestore();
  vi.unstubAllGlobals();
  __resetSessionExpiredListeners();
});

describe('issue #487 — a panel crash does not take the app with it', () => {
  it('a throwing child renders the fallback instead of nothing', () => {
    render(
      <ErrorBoundary name="review">
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('panel-error-review').textContent).toContain(PANEL_ERROR_COPY);
  });

  it('a sibling boundary is unaffected — no white screen', () => {
    // The property that matters. One boundary per panel is what keeps the
    // other seven tabs alive; a single top-level boundary would catch the
    // same throw and still blank everything.
    render(
      <>
        <ErrorBoundary name="review">
          <Boom />
        </ErrorBoundary>
        <ErrorBoundary name="history">
          <Fine />
        </ErrorBoundary>
      </>,
    );
    expect(screen.getByTestId('panel-error-review')).toBeTruthy();
    expect(screen.getByTestId('fine')).toBeTruthy();
    expect(screen.queryByTestId('panel-error-history')).toBeNull();
  });

  it('no error message or stack reaches the DOM', () => {
    render(
      <ErrorBoundary name="review">
        <Boom />
      </ErrorBoundary>,
    );
    expect(document.body.textContent).not.toContain(SECRET);
    expect(document.body.textContent).not.toContain('render exploded');
  });

  it('the technical detail goes to the console instead', () => {
    render(
      <ErrorBoundary name="review">
        <Boom />
      </ErrorBoundary>,
    );
    expect(
      consoleError.mock.calls.some((call: unknown[]) => String(call[0]).includes('[review] panel error')),
    ).toBe(true);
  });

  it('"Reload this screen" brings the panel back when the cause has cleared', () => {
    // React never resets a boundary on its own, so without this button the
    // fallback is permanent until a full page reload — and on a password-mode
    // deployment a reload used to sign the user out (#468). A component that
    // throws once and then works is how a transient bad payload behaves.
    // The flag is flipped by the TEST, not by the render. React may invoke a
    // render function more than once, so a component that disarms itself
    // mid-render recovers before the boundary ever shows — which made the
    // first version of this test pass for the wrong reason and then fail.
    let failing = true;
    function Flaky(): React.ReactElement {
      if (failing) throw new Error('transient');
      return <p data-testid="recovered">recovered</p>;
    }
    render(
      <ErrorBoundary name="review">
        <Flaky />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('panel-error-review')).toBeTruthy();
    failing = false;
    fireEvent.click(screen.getByTestId('panel-error-retry-review'));
    expect(screen.getByTestId('recovered')).toBeTruthy();
    expect(screen.queryByTestId('panel-error-review')).toBeNull();
  });
});

describe('issue #487 — one central place notices an expired session', () => {
  function stubStatus(status: number) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: status < 400, status, json: async () => ({}) }) as Response),
    );
  }

  it('a 401 on an authenticated route notifies once', async () => {
    const seen = vi.fn();
    onSessionExpired(seen);
    stubStatus(401);
    await authorizedFetch('/api/reviews');
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it('a 401 from the LOGIN route does NOT — that is a wrong password', async () => {
    // The distinction is the route, not the status. Bouncing someone to the
    // sign-in screen they are already looking at, and telling them their
    // session expired, would be actively misleading.
    const seen = vi.fn();
    onSessionExpired(seen);
    stubStatus(401);
    await authorizedFetch('/api/auth/login', { method: 'POST' });
    expect(seen).not.toHaveBeenCalled();
  });

  it('an ordinary failure is not an expired session', async () => {
    const seen = vi.fn();
    onSessionExpired(seen);
    stubStatus(500);
    await authorizedFetch('/api/reviews');
    expect(seen).not.toHaveBeenCalled();
  });

  it('the response is still returned to the caller unchanged', async () => {
    // This is an observer, not an interceptor. Every existing caller's own
    // error handling has to keep working exactly as it did.
    onSessionExpired(() => {});
    stubStatus(401);
    const response = await authorizedFetch('/api/reviews');
    expect(response.status).toBe(401);
  });

  it('a listener that throws does not break the fetch', async () => {
    onSessionExpired(() => {
      throw new Error('listener blew up');
    });
    stubStatus(401);
    await expect(authorizedFetch('/api/reviews')).resolves.toBeTruthy();
  });

  it('unsubscribing stops the notifications', async () => {
    const seen = vi.fn();
    const off = onSessionExpired(seen);
    off();
    stubStatus(401);
    await authorizedFetch('/api/reviews');
    expect(seen).not.toHaveBeenCalled();
  });
});
