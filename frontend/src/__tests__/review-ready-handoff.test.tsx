/**
 * review-ready-handoff.test.tsx — what happens the instant a review reaches
 * DONE (issue #448): the ding, the announcement, the focus move, and the
 * automatic save that must NOT bypass the download gate.
 *
 * WHAT DRIVES WHAT (so a green run here means something):
 *
 *   - The component is the real ReviewSubmission, driven end to end through a
 *     stubbed `fetch`: upload -> poll -> DONE. Nothing about the completion
 *     path is mocked, so the automatic save is observed the only way a browser
 *     would observe it — a real anchor element being clicked with the
 *     presigned URL on it.
 *   - `src/toaster/sounds` keeps its REAL implementation (mute logic included);
 *     `playPop` is merely wrapped in a spy so this file can assert "the
 *     component asked for the ding on DONE". Whether a muted module actually
 *     stays silent is proven separately, at the Web Audio level, by
 *     sounds.test.tsx ("when muted ... playPop ... sourcesStarted 0"). Splitting
 *     it that way keeps each assertion driven by real code: this file owns the
 *     component's decision, that file owns the module's behaviour.
 *   - jsdom has no AudioContext, so the one test that needs clip loading to
 *     actually happen installs a minimal mock and asserts on the URLs fetched
 *     — never on sound.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import * as sounds from '../toaster/sounds';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// Real module, spied entry point — see the docstring. `importOriginal` keeps
// the module-level mute state and every other export exactly as shipped.
vi.mock('../toaster/sounds', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../toaster/sounds')>();
  return { ...actual, playPop: vi.fn(actual.playPop) };
});

const playPopSpy = sounds.playPop as unknown as ReturnType<typeof vi.fn>;

// --- fetch stub -------------------------------------------------------------
// Routes by "METHOD path" (falls back to path-only for GETs), mirroring
// review-download-gate.test.tsx. Any bundled audio asset (a relative .mp3 URL
// Vite hands the sounds module) is served a tiny ArrayBuffer, so a test that
// installs an AudioContext exercises the real load path without a network.
function stubFetch(routes: Record<string, unknown>): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname.endsWith('.mp3')) {
      return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) } as Response;
    }
    const key = `${method} ${pathname}` in routes ? `${method} ${pathname}` : pathname;
    const body = routes[key];
    if (body === undefined) {
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function fetchedUrls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map(([input]) => String(input));
}

function fetchedPaths(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchedUrls(fetchMock).map((url) => new URL(url, 'http://localhost').pathname);
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

async function submitAndReachResult(): Promise<void> {
  render(<ReviewSubmission />);
  fireEvent.change(screen.getByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await screen.findByTestId('review-result');
}

const PRESIGNED_URL = 'https://s3.example.test/outputs/rev-448/out.docx?sig=abc';

/** A DONE review with output, optionally carrying a critic delta. */
function doneRoutes(criticDelta?: unknown, hasOutput = true): Record<string, unknown> {
  return {
    'POST /api/reviews': { review_id: 'rev-448', resumed: false },
    'GET /api/reviews/rev-448': {
      review_id: 'rev-448',
      status: 'DONE',
      decision: 'REQUEST_CHANGE',
      message: null,
      has_output: hasOutput,
      ...(criticDelta === undefined ? {} : { critic_delta: criticDelta }),
    },
    'GET /api/reviews/rev-448/output': { url: PRESIGNED_URL, expires_in: 60 },
  };
}

const GATING_DELTA = {
  contested_replacements: [
    {
      section: 'sec-8',
      critic_objection: 'Replacement drifts from the playbook position on liability.',
      critic_suggested_replacement: 'Cap liability at fees paid in the prior 12 months.',
    },
  ],
  added_issues: [{ topic: 'indemnity' }],
};

let anchorClickSpy: ReturnType<typeof vi.spyOn>;
let createElementSpy: ReturnType<typeof vi.spyOn>;

/** Every anchor the component created, in creation order. */
function createdAnchors(): HTMLAnchorElement[] {
  const anchors: HTMLAnchorElement[] = [];
  createElementSpy.mock.calls.forEach((call: unknown[], i: number) => {
    if (call[0] === 'a') {
      anchors.push(createElementSpy.mock.results[i]!.value as HTMLAnchorElement);
    }
  });
  return anchors;
}

beforeEach(() => {
  vi.restoreAllMocks();
  playPopSpy.mockClear();
  // The sounds module keeps mute state at module scope; reset it so test order
  // can never decide whether the toggle starts on.
  sounds.setMuted(false);
  anchorClickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  createElementSpy = vi.spyOn(document, 'createElement');
});

afterEach(() => {
  sounds.setMuted(false);
  vi.unstubAllGlobals();
});

describe('completion handoff — automatic save', () => {
  it('saves the redline through a plain <a download> the moment the review lands, with no click', async () => {
    const fetchMock = stubFetch(doneRoutes());

    await submitAndReachResult();

    // Nothing in this test ever clicked the download button.
    await waitFor(() =>
      expect(fetchedPaths(fetchMock)).toContain('/api/reviews/rev-448/output'),
    );
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));

    const anchors = createdAnchors();
    expect(anchors).toHaveLength(1);
    expect(anchors[0]!.href).toBe(PRESIGNED_URL);
    // A plain download anchor — NOT showSaveFilePicker, which would throw
    // SecurityError here for want of transient user activation.
    expect(anchors[0]!.hasAttribute('download')).toBe(true);
  });

  it('does not save automatically when the review produced no output', async () => {
    const fetchMock = stubFetch(doneRoutes(undefined, false));

    await submitAndReachResult();

    expect(screen.queryByTestId('review-download-button')).toBeNull();
    await waitFor(() =>
      expect(screen.getByTestId('review-ready-announcement').textContent).toContain(
        'no marked-up document',
      ),
    );
    expect(fetchedPaths(fetchMock)).not.toContain('/api/reviews/rev-448/output');
    expect(anchorClickSpy).not.toHaveBeenCalled();
  });
});

describe('completion handoff — the download gate is not bypassed', () => {
  it('suppresses the automatic save while a critic delta is unacknowledged, but still dings and announces', async () => {
    const fetchMock = stubFetch(doneRoutes(GATING_DELTA));

    await submitAndReachResult();

    // The gate itself is in force: the indicator rendered.
    await screen.findByTestId('review-critic-delta');

    // ...and the automatic save did NOT run. A silent auto-save here would
    // defeat docs/output-contract.md's "delta indicator must be visible before
    // download" — the file would already be on disk before anyone read it.
    expect(fetchedPaths(fetchMock)).not.toContain('/api/reviews/rev-448/output');
    expect(anchorClickSpy).not.toHaveBeenCalled();

    // What is NOT suppressed: the ding and the readiness announcement.
    expect(playPopSpy).toHaveBeenCalled();
    expect(screen.getByTestId('review-ready-announcement').textContent).toContain(
      'adversarial critic flagged this review',
    );

    // And the manual path is untouched — the attorney, having passed the
    // indicator, can still save with one click.
    fireEvent.click(screen.getByTestId('review-download-button'));
    await waitFor(() =>
      expect(fetchedPaths(fetchMock)).toContain('/api/reviews/rev-448/output'),
    );
    await waitFor(() => expect(anchorClickSpy).toHaveBeenCalledTimes(1));
  });

  it('an empty critic delta does not gate the automatic save', async () => {
    const fetchMock = stubFetch(doneRoutes({ contested_replacements: [], added_issues: [] }));

    await submitAndReachResult();

    expect(screen.queryByTestId('review-critic-delta')).toBeNull();
    await waitFor(() =>
      expect(fetchedPaths(fetchMock)).toContain('/api/reviews/rev-448/output'),
    );
  });
});

describe('completion handoff — announcement and focus', () => {
  it('announces readiness in a live region and moves keyboard focus to the save control', async () => {
    stubFetch(doneRoutes());

    await submitAndReachResult();

    const announcement = await screen.findByTestId('review-ready-announcement');
    expect(announcement.getAttribute('role')).toBe('status');
    expect(announcement.getAttribute('aria-live')).toBe('polite');
    expect(announcement.textContent).toContain('Your redline is ready');

    // The reliable path: one keystroke away, no dialog required.
    const button = screen.getByTestId('review-download-button');
    expect(document.activeElement).toBe(button);
  });

  it('mounts the live region before the review lands, so the announcement is not inserted with its text', () => {
    stubFetch(doneRoutes());
    render(<ReviewSubmission />);

    const announcement = screen.getByTestId('review-ready-announcement');
    expect(announcement).toBeInTheDocument();
    expect(announcement.textContent).toBe('');
  });
});

describe('completion handoff — the ding', () => {
  it('plays the pop exactly once, on the transition into DONE', async () => {
    stubFetch(doneRoutes());
    render(<ReviewSubmission />);

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    // Uploading is not "done" — nothing has popped yet.
    expect(playPopSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    await waitFor(() => expect(playPopSpy).toHaveBeenCalledTimes(1));
  });

  it('leaves silence to the sound toggle rather than deciding it here', async () => {
    stubFetch(doneRoutes());
    render(<ReviewSubmission />);

    // Turn sound off before submitting.
    fireEvent.click(screen.getByTestId('sound-toggle'));
    expect(sounds.isMuted()).toBe(true);

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    fireEvent.click(screen.getByTestId('review-submit-button'));
    await screen.findByTestId('review-result');

    // The component still routes the ding through the sound manager, which is
    // muted — it never reaches for an audio node itself. That a muted manager
    // starts no buffer source is proven at the Web Audio level in
    // sounds.test.tsx; duplicating it here would assert the double, not the
    // system.
    await waitFor(() => expect(playPopSpy).toHaveBeenCalledTimes(1));
    expect(sounds.isMuted()).toBe(true);
  });
});

describe('completion handoff — audio stays same-origin', () => {
  it('fetches every clip from a relative, same-origin URL (CSP has no media-src)', async () => {
    // jsdom has no AudioContext, so clip loading is normally a no-op. Install a
    // minimal one purely so the real load path RUNS and its URLs are observable.
    class MockAudioContext {
      state = 'running';
      destination = {};
      createGain(): unknown {
        return { gain: { value: 1 }, connect(): void {} };
      }
      createBufferSource(): unknown {
        return { buffer: null, connect(): void {}, start(): void {}, stop(): void {} };
      }
      decodeAudioData(): Promise<unknown> {
        return Promise.resolve({ duration: 0.25 });
      }
      resume(): Promise<void> {
        return Promise.resolve();
      }
    }
    (window as unknown as Record<string, unknown>).AudioContext = MockAudioContext;

    try {
      const fetchMock = stubFetch(doneRoutes());
      await submitAndReachResult();

      const audioUrls = fetchedUrls(fetchMock).filter((url) => url.endsWith('.mp3'));
      expect(audioUrls.length).toBeGreaterThan(0);
      for (const url of audioUrls) {
        // Relative, or at worst this document's own origin — never a CDN. The
        // Amplify CSP declares no media-src, so media falls back to
        // default-src 'self' and a remote clip would simply be blocked.
        expect(new URL(url, window.location.origin).origin).toBe(window.location.origin);
      }
    } finally {
      delete (window as unknown as Record<string, unknown>).AudioContext;
    }
  });
});
