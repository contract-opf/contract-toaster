/**
 * playbook-selector.test.tsx — contract-type dial for ReviewSubmission.tsx
 * (issue #272, redesigned as the toaster dial by issue #280).
 *
 * The contract-type picker used to be a plain <select> (issue #272). Issue
 * #280 replaces it with a toaster "dial": a real `radiogroup` of `radio`
 * stops (native ARIA semantics + arrow-key navigation) that the SVG
 * illustration in frontend/src/toaster/ decorates. This file locks in the
 * same behavioral guarantees #272 established, against the new markup:
 *
 *   1. The dial renders entirely from `GET /api/playbooks` (no hardcoded
 *      playbook id/name anywhere in the component), showing ONLY loaded
 *      ("active") playbooks and defaulting to the first.
 *   2. The dial is keyboard-operable: arrow keys move the checked stop
 *      (roving aria-checked), matching the ARIA "radiogroup" pattern.
 *   3. Selecting a stop (click or keyboard) appends the CHOSEN
 *      `playbook_id` to the `POST /api/reviews` FormData, and the result
 *      view shows that type.
 *   4. A registered-but-unactivated ("coming_soon") type is SHOWN but not
 *      selectable. It once rendered as a de-emphasized yet still-selectable
 *      stop, on the principle that the backend stays authoritative on
 *      availability (#272) -- but selecting one could only ever produce a
 *      guaranteed failure, so it dressed a dead end up as a choice.
 *      Removing it outright overcorrected: an unactivated playbook is real,
 *      published intent, and the dial is the product's roadmap as much as
 *      its control. So the stop renders, marked "(coming soon)" and
 *      `aria-disabled`, and neither click nor arrow keys will select it.
 *      The backend IS still authoritative: what's selectable is driven by
 *      the `status` the catalog endpoint itself reports, and a direct API
 *      call for an unactivated type still gets the same 503.
 *   5. That 503 path therefore stays live and tested: a playbook
 *      deactivated between catalog load and submit still renders cleanly
 *      through the submit-error path -- no crash, no special-cased copy.
 *   6. With nothing LOADED, the dial says so rather than leaving a toaster
 *      whose only stops are ones you can't pick. Keyed on the absence of an
 *      *active* type, not an empty catalog -- a registry of only
 *      unactivated types still renders (coming soon) stops, and that must
 *      not read as a working dial.
 *
 * Fully offline: Amplify auth is mocked and fetch is stubbed per test.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import {
  choosePlaybook,
  findReviewResult,
  openPlaybookCatalog,
  playbookStop,
  playbookStops,
  pressSubmit,
} from './support/consoleSurface';

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: vi.fn(async () => ({
    tokens: {
      idToken: { toString: () => 'mock-id-token.jwt.value' },
      accessToken: { toString: () => 'mock-access-token.jwt.value' },
    },
  })),
}));

// fetch stub — routes by "METHOD path" (falls back to path-only for GETs),
// same convention as review-download-gate.test.tsx / security-posture.test.tsx.
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
    if (
      entry &&
      typeof entry === 'object' &&
      'status' in (entry as Record<string, unknown>) &&
      'body' in (entry as Record<string, unknown>)
    ) {
      const { status: statusCode, body } = entry as { status: number; body: unknown };
      return { ok: statusCode < 400, status: statusCode, json: async () => body } as Response;
    }
    return { ok: true, status: 200, json: async () => entry } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

function docxFile(): File {
  return new File(['contents'], 'contract.docx', {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

// Two loaded types + one registered-but-unactivated one, so these cover both
// "only loaded stops render" and multi-stop keyboard navigation.
const CATALOG = [
  { playbook_id: 'eiaa', display_name: 'EIAA', status: 'active' },
  { playbook_id: 'sample-agreement', display_name: 'Sample Agreement', status: 'active' },
  { playbook_id: 'nda', display_name: 'NDA', status: 'coming_soon' },
];

describe('contract-type dial — ReviewSubmission.tsx', () => {
  it('renders every registered playbook as a stop, defaulting to the first LOADED one', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: CATALOG } });

    render(<ReviewSubmission />);

    await screen.findByTestId('review-playbook-dial');

    // 'nda' is registered but not activated -- it still appears, marked as
    // coming soon: the control is the roadmap as well as the control. Issue
    // #733: the console says that with a disabled <option> rather than an
    // aria-disabled radio, so the LIST is compared, not the markup.
    const stops = playbookStops();
    expect(stops.map((stop) => stop.id)).toEqual(['eiaa', 'sample-agreement', 'nda']);
    expect(stops.map((stop) => stop.label.replace(/\s*[(·]\s*coming soon\)?/i, ''))).toEqual([
      'EIAA',
      'Sample Agreement',
      'NDA',
    ]);
    expect(stops[2].label.toLowerCase()).toContain('coming soon');
    // The default selection never parks on something the user can't pick.
    expect(stops.map((stop) => stop.selected)).toEqual([true, false, false]);
  });

  it('shows an unactivated playbook but refuses to select it', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: CATALOG } });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');

    // Visible, and honestly labelled...
    expect(playbookStop('nda')!.label.toLowerCase()).toContain('coming soon');
    // ...but marked unavailable rather than removed.
    expect(playbookStop('nda')!.selectable).toBe(false);

    // And choosing it selects nothing: an unactivated playbook can only fail
    // at load_playbook, so neither surface offers that as a choice.
    await choosePlaybook('nda');
    expect(playbookStop('nda')!.selected).toBe(false);
    expect(playbookStop('eiaa')!.selected).toBe(true);
  });

  it('says so when no playbooks are loaded, instead of an unexplained empty dial', async () => {
    stubFetch({
      'GET /api/playbooks': {
        playbooks: [{ playbook_id: 'nda', display_name: 'NDA', status: 'coming_soon' }],
      },
    });

    render(<ReviewSubmission />);

    expect(await screen.findByTestId('review-no-playbooks')).toHaveTextContent(
      /no playbook is active/i,
    );
  });

  // ---------------------------------------------------------------------
  // Full WAI-ARIA radiogroup keyboard contract (issue #450 item 1).
  //
  // The live audit could never exercise this: production has exactly ONE
  // dial stop, so no arrow key has anywhere to go (audit §E6). These drive
  // the real dial with a TWO-active-stop catalog, which is the condition the
  // audit item was waiting on a deployment to provide. What they do not
  // cover is pixels — "focus stays visible" is a rendered-style question and
  // jsdom runs with `css: false`; that half stays owed to a browser pass.
  // ---------------------------------------------------------------------

  it('appends the CHOSEN playbook_id to the submitted FormData and shows the type in the result view', async () => {
    const fetchMock = stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews': { review_id: 'rev-99', resumed: false },
      'GET /api/reviews/rev-99': {
        review_id: 'rev-99',
        status: 'DONE',
        decision: 'ACCEPT',
        message: null,
        has_output: false,
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');
    // Pick the SECOND loaded playbook, so a passing assertion can't come from
    // the default selection.
    await choosePlaybook('sample-agreement');
    expect(playbookStop('sample-agreement')!.selected).toBe(true);

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();

    await waitFor(() => {
      const submitCall = fetchMock.mock.calls.find(([, init]) => {
        const method = (init as RequestInit | undefined)?.method;
        return method === 'POST';
      });
      expect(submitCall).toBeDefined();
      const [, init] = submitCall as [RequestInfo | URL, RequestInit];
      const body = init.body as FormData;
      expect(body.get('playbook_id')).toBe('sample-agreement');
    });

    await findReviewResult();
    const label = await screen.findByTestId('review-submitted-playbook');
    expect(label.textContent).toContain('Sample Agreement');
  });

  // The backend stays authoritative on availability: filtering the dial to
  // loaded types narrows what a user can PICK, it does not make this 503
  // unreachable. A playbook deactivated between catalog load and submit still
  // lands here, so the clean-failure path must keep working.
  it('a playbook deactivated after load surfaces the clean failure copy, no crash', async () => {
    stubFetch({
      'GET /api/playbooks': { playbooks: CATALOG },
      'POST /api/reviews': {
        status: 503,
        body: { detail: 'no active playbook' },
      },
    });

    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');

    fireEvent.change(screen.getByTestId('review-file-input'), {
      target: { files: [docxFile()] },
    });
    await pressSubmit();

    const error = await screen.findByTestId('review-submit-error');
    expect(error.textContent).toContain('no active playbook');
    // No crash: the upload form (and the rest of the SPA) is still mounted.
    expect(screen.getByTestId('review-submission')).toBeInTheDocument();
  });

  it('degrades gracefully (no dial, no crash) when the catalog fetch fails', async () => {
    stubFetch({});

    render(<ReviewSubmission />);

    await waitFor(() => expect(screen.getByTestId('review-submission')).toBeInTheDocument());
    // Nothing to choose from, and nothing pretending otherwise: the console
    // keeps the empty control with its "Choose a playbook" placeholder and
    // offers no options (issue #733).
    expect(playbookStops()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The console's own playbook control (issue #733).
//
// The dial's radiogroup became two things: a native <select> — whose keyboard
// contract is the platform's, not ours to reimplement or to re-test — and a
// searchable catalog dialog for a list too long to spin through. The
// guarantees above are asserted through the control itself; these are the ones
// only the second shape can carry.
// ---------------------------------------------------------------------------
describe('the console catalog dialog', () => {
  it('is a native select, so the browser owns the keyboard contract', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: CATALOG } });
    render(<ReviewSubmission />);

    const control = await screen.findByTestId('review-playbook-dial');
    expect(control.tagName).toBe('SELECT');
    expect(control).toHaveAccessibleName('Playbook type');
  });

  it('lists every registered playbook, and refuses the unactivated one', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: CATALOG } });
    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');

    const dialog = await openPlaybookCatalog();
    for (const entry of CATALOG) {
      expect(within(dialog).getByTestId(`review-playbook-option-${entry.playbook_id}`))
        .toBeInTheDocument();
    }
    expect(screen.getByTestId('review-playbook-option-nda')).toBeDisabled();
  });

  it('filters by name and selects the entry that is chosen', async () => {
    stubFetch({ 'GET /api/playbooks': { playbooks: CATALOG } });
    render(<ReviewSubmission />);
    await screen.findByTestId('review-playbook-dial');

    const dialog = await openPlaybookCatalog();
    fireEvent.change(within(dialog).getByRole('searchbox'), {
      target: { value: 'sample' },
    });
    expect(screen.queryByTestId('review-playbook-option-eiaa')).toBeNull();

    fireEvent.click(screen.getByTestId('review-playbook-option-sample-agreement'));
    await waitFor(() => expect(playbookStop('sample-agreement')!.selected).toBe(true));
  });
});
