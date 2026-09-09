/**
 * orbit-diner-review-details-734.test.tsx — the review's own record must stay
 * reachable on a review that FAILED (issue #734, owner decision H1, epic #729).
 *
 * ## The hole this closes
 *
 * The toaster base rail has one free key slot after "Browse playbooks", and on
 * ERROR the retry — "Toast another slice" — takes it. So `open("record")` had
 * no call site on the one status where the record matters most: the review id
 * a reviewer quotes to support, the Copy ID control that puts it on the
 * clipboard, and Save original for the document the pipeline still holds. All
 * three were unreachable, and the key that WAS offered is the one that clears
 * the failed review out from under them.
 *
 * Owner decision H1 keeps the retry where the designer put it and gives the
 * record its own call site beside the failure's cause and fix — the card that
 * already renders for ERROR and for both manual handoffs.
 *
 * ## What is pinned here
 *
 *   1. On ERROR the control exists, sits with the cause/fix rather than in the
 *      base rail, and opens the record overlay carrying the id row and Copy ID.
 *   2. Save original follows `has_input` and nothing else — a review whose
 *      input retention has lapsed offers no key that could only 404.
 *   3. Both manual statuses take the same path, and offer exactly ONE "Review
 *      details" control: the result card owns it there, so the rail must yield
 *      rather than render a second one (the duplicate-control regression).
 *   4. Opening the record does not disarm the retry: closing the overlay and
 *      pressing "Toast another slice" still clears the review, and — the
 *      ordering the whole ticket is about — the record was reachable BEFORE
 *      that happened.
 *   5. DONE is untouched: the base key stays "Save redline", the result card
 *      renders nothing on the page, and the id still lives in the receipt.
 *   6. Keyboard focus comes back. This control is the one overlay opener that
 *      unmounts itself by opening its overlay — the page yields the result
 *      card to the record — so the console's usual "focus what opened me"
 *      restore has a detached node to aim at and would drop focus to <body>.
 *
 * The flag is forced on for this file: the control only exists on the console,
 * and the required verification runs with the flag at its shipped default.
 *
 * Fully offline — fetch stubbed per test, Amplify auth mocked.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

const REVIEW_ID = 'rev-734';
const PLAYBOOKS = [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }];

interface Scenario {
  status: string;
  has_input: boolean;
  has_output?: boolean;
  reason?: string | null;
  failing_stage?: string | null;
}

let scenario: Scenario;
let calls: string[] = [];

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function stubFetch(): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const pathname = new URL(String(input), 'http://localhost').pathname;
    calls.push(`${method} ${pathname}`);
    if (pathname === '/api/playbooks') return ok({ playbooks: PLAYBOOKS });
    if (method === 'POST' && pathname === '/api/reviews') {
      return ok({ review_id: REVIEW_ID, resumed: false });
    }
    if (pathname === `/api/reviews/${REVIEW_ID}/input`) {
      return ok({ url: 'https://example.invalid/original.docx' });
    }
    if (pathname === `/api/reviews/${REVIEW_ID}`) {
      return ok({
        review_id: REVIEW_ID,
        status: scenario.status,
        decision: null,
        message: null,
        has_output: scenario.has_output ?? false,
        has_input: scenario.has_input,
        reason: scenario.reason ?? 'model_context_length_exceeded',
        failing_stage: scenario.failing_stage ?? 'run_review',
      });
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function docxFile(): File {
  return new File(['x'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function consoleNode(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

/** Render, submit one document, and wait for the scenario's terminal status. */
async function submitAndSettle(next: Scenario): Promise<ReturnType<typeof vi.fn>> {
  scenario = next;
  const fetchMock = stubFetch();
  render(<ReviewSubmission />);
  fireEvent.change(await screen.findByTestId('review-file-input'), {
    target: { files: [docxFile()] },
  });
  await waitFor(() => expect(consoleNode()).toHaveAttribute('data-status', 'loaded'));
  fireEvent.click(screen.getByTestId('review-submit-button'));
  await waitFor(
    () => expect(consoleNode()).toHaveAttribute('data-status', next.status.toLowerCase()),
    { timeout: 8000 },
  );
  return fetchMock;
}

/** The one open overlay. */
function overlay(): HTMLElement {
  const node = document.querySelector<HTMLElement>('dialog.od-dialog[open]');
  if (!node) throw new Error('no overlay is open');
  return node;
}

beforeEach(() => {
  calls = [];
  vi.clearAllMocks();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('issue #734 — a failed review still reaches its own record', () => {
  it('offers Review details beside the cause and fix, not in the base rail', async () => {
    await submitAndSettle({ status: 'ERROR', has_input: true });

    // The card the reader is already looking at is the one that carries it.
    const card = await screen.findByTestId('review-result');
    const key = screen.getByTestId('review-details-button');
    expect(card).toContainElement(key);
    expect(key).toHaveAccessibleName('Review details');

    // ...and it is NOT inside the assertive region: the alert speaks the
    // diagnosis, and a control is not part of what interrupts a reader.
    expect(screen.getByTestId('review-failure')).not.toContainElement(key);

    // The rail is untouched: the retry key still holds the one free slot.
    expect(screen.getByTestId('review-retry-button')).toHaveTextContent(
      'Toast another slice',
    );
  });

  it('opens the record overlay with the review id and Copy ID', async () => {
    await submitAndSettle({ status: 'ERROR', has_input: true });

    // Nothing is on screen until it is asked for.
    expect(screen.queryByTestId('review-id-row')).toBeNull();
    fireEvent.click(await screen.findByTestId('review-details-button'));

    const record = overlay();
    expect(within(record).getByRole('heading', { name: 'Review record' })).toBeInTheDocument();
    expect(within(record).getByTestId('review-id-row')).toBeInTheDocument();
    expect(
      within(record).getByTestId('review-copy-id-button'),
    ).toHaveTextContent('Copy review ID');

    // Issue #733's rule survives: the result fragment has ONE render site, so
    // the page yields it to the overlay rather than duplicating eight testids.
    expect(screen.getAllByTestId('review-result')).toHaveLength(1);
    expect(record).toContainElement(screen.getByTestId('review-result'));
    // And the overlay's own copy offers no key that opens the overlay again.
    expect(screen.queryByTestId('review-details-button')).toBeNull();
  });

  it('offers Save original only while the input is still retained', async () => {
    await submitAndSettle({ status: 'ERROR', has_input: true });
    fireEvent.click(await screen.findByTestId('review-details-button'));
    fireEvent.click(within(overlay()).getByRole('button', { name: 'Save original' }));
    await waitFor(() => expect(calls).toContain(`GET /api/reviews/${REVIEW_ID}/input`));
  });

  it('offers no Save original when retention has already purged the input', async () => {
    await submitAndSettle({ status: 'ERROR', has_input: false });
    fireEvent.click(await screen.findByTestId('review-details-button'));

    const record = overlay();
    // The id and its copy control are still there — only the dead key is gone.
    expect(within(record).getByTestId('review-copy-id-button')).toBeInTheDocument();
    expect(within(record).queryByRole('button', { name: 'Save original' })).toBeNull();
  });

  it.each(['MANUAL_REVIEW_REQUIRED', 'ERROR_MANUAL_REVIEW_REQUIRED'])(
    'takes the same path on %s, with exactly one Review details control',
    async (status) => {
      await submitAndSettle({ status, has_input: true });

      // The duplicate-control regression: the result card owns the key on a
      // manual handoff, so the base rail must yield rather than render a
      // second control with the same name.
      const keys = await screen.findAllByRole('button', { name: 'Review details' });
      expect(keys).toHaveLength(1);
      expect(screen.getByTestId('review-result')).toContainElement(keys[0]);

      fireEvent.click(keys[0]);
      expect(within(overlay()).getByTestId('review-copy-id-button')).toBeInTheDocument();
    },
    14_000,
  );

  it.each([
    'ERROR',
    'MANUAL_REVIEW_REQUIRED',
    'ERROR_MANUAL_REVIEW_REQUIRED',
  ])(
    'returns keyboard focus to Review details when the record closes on %s',
    async (status) => {
      await submitAndSettle({ status, has_input: true });

      const key = await screen.findByTestId('review-details-button');
      // A real click focuses the button it lands on; fireEvent does not, and
      // the console captures `document.activeElement` as the opener.
      key.focus();
      fireEvent.click(key);

      // The opener is detached the moment it works: the page hands its copy of
      // the result card to the overlay, taking this key with it.
      expect(key.isConnected).toBe(false);

      fireEvent.click(within(overlay()).getByRole('button', { name: 'Close' }));
      await waitFor(() =>
        expect(document.activeElement).toBe(
          screen.getByTestId('review-details-button'),
        ),
      );
    },
    20_000,
  );

  it('leaves the retry able to clear the review it was read from', async () => {
    const fetchMock = await submitAndSettle({ status: 'ERROR', has_input: true });
    const posts = () =>
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          new URL(String(input), 'http://localhost').pathname === '/api/reviews' &&
          (init as RequestInit | undefined)?.method === 'POST',
      ).length;
    expect(posts()).toBe(1);

    // The ordering the ticket is about: the record is read FIRST, while the
    // review still exists...
    fireEvent.click(await screen.findByTestId('review-details-button'));
    expect(within(overlay()).getByTestId('review-id-row')).toBeInTheDocument();
    fireEvent.click(within(overlay()).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByTestId('review-id-row')).toBeNull());

    // ...and only then is it thrown away, exactly as it was before.
    fireEvent.click(screen.getByTestId('review-retry-button'));
    await waitFor(() => expect(screen.queryByTestId('review-failure')).toBeNull());
    expect(screen.queryByTestId('review-details-button')).toBeNull();
    expect(posts()).toBe(1);
  });

  it('changes nothing on DONE — the id stays in the receipt, the key stays Save redline', async () => {
    await submitAndSettle({ status: 'DONE', has_input: true, has_output: true });

    expect(await screen.findByTestId('review-download-button')).toHaveTextContent(
      'Save redline',
    );
    // The finished result is not on the page at all, so neither is the key.
    expect(screen.queryByTestId('review-result')).toBeNull();
    expect(screen.queryByTestId('review-details-button')).toBeNull();
    expect(screen.queryByTestId('review-id-row')).toBeNull();
  });
});
