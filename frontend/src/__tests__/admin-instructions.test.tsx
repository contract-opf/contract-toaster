/**
 * admin-instructions.test.tsx — the "Playbook instructions" admin tab
 * (AdminInstructions.tsx, issue #484, epic #481 sub-issue C), replacing
 * admin-pen-rules.test.tsx now that AdminPenRules.tsx is deleted.
 *
 * What is worth locking in here, beyond "it renders":
 *
 *   1. **The playbook picker behaves per the issue's spec**: zero installed
 *      shows an empty state and calls no per-playbook route; one installed
 *      is preselected without the admin choosing anything.
 *   2. **The status line is the ONLY state banner** and reads correctly for
 *      all three states this screen can be in: nothing ever saved, a
 *      non-empty version in effect, and an explicitly cleared version.
 *   3. **A save always sends `expected_current_version`**, and a losing
 *      save (409) never silently overwrites — the admin's own unsaved
 *      draft survives, and the version that won is fetched and shown
 *      alongside it.
 *   4. **History is newest-first, expandable, and restorable** — "Restore
 *      as new version" issues a normal save carrying the old text, not a
 *      bespoke revert endpoint (there isn't one; append-only semantics
 *      made visible per the issue's spec).
 *   5. **Error copy leaks nothing** — no `/api/…` path and no `HTTP <n>`
 *      ever reaches rendered text.
 *
 * Fully offline — `fetch` is stubbed, `../auth` is mocked, no network.
 * Per the harness rules (`vitest.config.ts` runs jsdom with `css: false`)
 * every assertion is on structure/text/ARIA/testids, never computed styles.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminInstructions from '../AdminInstructions';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const ONE_PLAYBOOK = {
  playbooks: [{ playbook_id: 'eiaa', display_name: 'EIAA', status: 'active', notes: '' }],
};

const TWO_PLAYBOOKS = {
  playbooks: [
    { playbook_id: 'eiaa', display_name: 'EIAA', status: 'active', notes: '' },
    { playbook_id: 'nda', display_name: 'NDA', status: 'active', notes: '' },
  ],
};

const EMPTY_CATALOG = { playbooks: [] };

const NOTHING_SAVED = { current: null, history: [] };

interface Recorded {
  method: string;
  pathname: string;
  body: unknown;
}

interface Handler {
  method: string;
  /** Matched against the pathname with `endsWith`. */
  suffix: string;
  status: number;
  body: unknown;
}

let requests: Recorded[] = [];

/**
 * Route stub. `overrides` are consulted first, in order (first match
 * wins), then a happy-path default (empty catalog / nothing saved / a
 * generic 200 for anything else). Anything genuinely unmatched 404s.
 */
function stubRoutes(overrides: Handler[] = []): ReturnType<typeof vi.fn> {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const method = (init?.method ?? 'GET').toUpperCase();
    const rawBody = init?.body;
    const body = typeof rawBody === 'string' ? JSON.parse(rawBody) : undefined;
    requests.push({ method, pathname, body });

    const override = overrides.find((h) => h.method === method && pathname.endsWith(h.suffix));
    if (override) {
      return {
        ok: override.status >= 200 && override.status < 300,
        status: override.status,
        json: async () => override.body,
      } as Response;
    }
    if (method === 'GET' && pathname === '/api/playbooks') {
      return { ok: true, status: 200, json: async () => EMPTY_CATALOG } as Response;
    }
    if (method === 'GET' && pathname.endsWith('/instructions')) {
      return { ok: true, status: 200, json: async () => NOTHING_SAVED } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
  return impl;
}

beforeEach(() => {
  vi.unstubAllGlobals();
  requests = [];
});

describe('AdminInstructions — playbook picker', () => {
  it('shows an empty state and calls no per-playbook route when nothing is installed', async () => {
    stubRoutes([{ method: 'GET', suffix: '/api/playbooks', status: 200, body: EMPTY_CATALOG }]);
    render(<AdminInstructions />);

    expect(await screen.findByTestId('admin-instructions-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-instructions-picker')).toBeNull();
    expect(requests.some((r) => r.pathname.endsWith('/instructions'))).toBe(false);
  });

  it('preselects the single installed playbook without any admin action', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/eiaa/instructions', status: 200, body: NOTHING_SAVED },
    ]);
    render(<AdminInstructions />);

    const picker = (await screen.findByTestId('admin-instructions-picker')) as HTMLSelectElement;
    expect(within(picker).getAllByRole('option')).toHaveLength(1);
    expect(picker.value).toBe('eiaa');
    await waitFor(() => {
      expect(requests.some((r) => r.method === 'GET' && r.pathname.endsWith('/eiaa/instructions'))).toBe(
        true,
      );
    });
  });

  it('defaults to the first playbook and refetches on switching', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: TWO_PLAYBOOKS },
      { method: 'GET', suffix: '/eiaa/instructions', status: 200, body: NOTHING_SAVED },
      { method: 'GET', suffix: '/nda/instructions', status: 200, body: NOTHING_SAVED },
    ]);
    render(<AdminInstructions />);

    const picker = (await screen.findByTestId('admin-instructions-picker')) as HTMLSelectElement;
    await waitFor(() => expect(picker.value).toBe('eiaa'));

    fireEvent.change(picker, { target: { value: 'nda' } });

    await waitFor(() => {
      expect(requests.some((r) => r.method === 'GET' && r.pathname.endsWith('/nda/instructions'))).toBe(
        true,
      );
    });
  });
});

describe('AdminInstructions — stale-response race', () => {
  it('discards an earlier playbook GET that resolves after switching to a later one', async () => {
    let resolveEiaa: ((value: Response) => void) | undefined;
    let resolveNda: ((value: Response) => void) | undefined;

    const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      const method = (init?.method ?? 'GET').toUpperCase();
      requests.push({ method, pathname, body: init?.body ? JSON.parse(init.body as string) : undefined });

      if (method === 'GET' && pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => TWO_PLAYBOOKS } as Response;
      }
      // Both playbooks' GETs are held open deliberately, so the test
      // controls the order in which they resolve.
      if (method === 'GET' && pathname.endsWith('/eiaa/instructions')) {
        return new Promise<Response>((resolve) => {
          resolveEiaa = resolve;
        });
      }
      if (method === 'GET' && pathname.endsWith('/nda/instructions')) {
        return new Promise<Response>((resolve) => {
          resolveNda = resolve;
        });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', impl);

    render(<AdminInstructions />);

    const picker = (await screen.findByTestId('admin-instructions-picker')) as HTMLSelectElement;
    await waitFor(() => expect(picker.value).toBe('eiaa'));
    // eiaa's GET is now in flight (held open by resolveEiaa above).

    fireEvent.change(picker, { target: { value: 'nda' } });
    await waitFor(() => expect(resolveNda).toBeDefined());
    // nda's GET is now ALSO in flight — both requests are outstanding at
    // once, exactly the race the fix guards against.

    // The LATER-selected playbook's response lands FIRST.
    resolveNda!({
      ok: true,
      status: 200,
      json: async () => ({
        current: { version: 5, text: 'NDA text.', saved_by: 'local:nda-admin', saved_at: 1_700_000_500 },
        history: [],
      }),
    } as Response);

    await waitFor(() => {
      expect((screen.getByTestId('admin-instructions-text') as HTMLTextAreaElement).value).toBe(
        'NDA text.',
      );
    });

    // The EARLIER-selected playbook's response — from the playbook the
    // admin already switched away from — lands LAST. It must be discarded,
    // not silently overwrite nda's freshly-loaded state.
    resolveEiaa!({
      ok: true,
      status: 200,
      json: async () => ({
        current: { version: 2, text: 'EIAA text.', saved_by: 'local:eiaa-admin', saved_at: 1_700_000_100 },
        history: [],
      }),
    } as Response);

    // Give the (discarded) eiaa promise's microtasks a turn to run, then
    // assert nothing changed.
    await new Promise((resolve) => setTimeout(resolve, 0));

    const textarea = screen.getByTestId('admin-instructions-text') as HTMLTextAreaElement;
    expect(textarea.value).toBe('NDA text.');
    expect(screen.getByTestId('admin-instructions-status')).toHaveTextContent(/^v5 in effect/);

    // A save now must carry nda's `expected_current_version` (5) against
    // nda's own route — never eiaa's — proving the compare-and-set body
    // follows the selected playbook, not whichever response resolved
    // first.
    fireEvent.click(screen.getByTestId('admin-instructions-save'));
    await waitFor(() => {
      const posted = requests.find((r) => r.method === 'POST');
      expect(posted).toBeDefined();
      expect(posted?.pathname.endsWith('/nda/instructions')).toBe(true);
      expect(posted?.body).toEqual({ text: 'NDA text.', expected_current_version: 5 });
    });
  });
});

describe('AdminInstructions — status line', () => {
  it('says nothing is saved when current is null', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 200, body: NOTHING_SAVED },
    ]);
    render(<AdminInstructions />);

    expect(await screen.findByTestId('admin-instructions-status')).toHaveTextContent(
      'No standing instructions — the playbook speaks for itself.',
    );
  });

  it('reports the version, author, and date when non-empty text is in effect', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      {
        method: 'GET',
        suffix: '/instructions',
        status: 200,
        body: {
          current: { version: 3, text: 'Flag auto-renewal.', saved_by: 'local:admin', saved_at: 1_700_000_000 },
          history: [],
        },
      },
    ]);
    render(<AdminInstructions />);

    const status = await screen.findByTestId('admin-instructions-status');
    expect(status).toHaveTextContent(/^v3 in effect for every new review/);
    expect(status).toHaveTextContent('local:admin');
  });

  it('reads an explicitly-cleared version as "cleared", not "in effect"', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      {
        method: 'GET',
        suffix: '/instructions',
        status: 200,
        body: {
          current: { version: 5, text: '', saved_by: 'local:admin', saved_at: 1_700_000_000 },
          history: [],
        },
      },
    ]);
    render(<AdminInstructions />);

    const status = await screen.findByTestId('admin-instructions-status');
    expect(status).toHaveTextContent(/^v5 cleared/);
    expect(status.textContent ?? '').not.toMatch(/in effect/);
  });
});

describe('AdminInstructions — precedence copy', () => {
  it('renders the field label and hint verbatim, sharing GUIDANCE_PRECEDENCE_COPY with the Review screen', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 200, body: NOTHING_SAVED },
    ]);
    render(<AdminInstructions />);

    const textarea = await screen.findByTestId('admin-instructions-text');
    expect(
      screen.getByLabelText('Standing instructions for this contract type (optional)'),
    ).toBe(textarea);

    // The hint is wired via aria-describedby, same pattern asserted for the
    // Review screen's own guidance field in review-guidance.test.tsx.
    const describedBy = textarea.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    const hint = document.getElementById(describedBy!.split(' ')[0]!)!.textContent ?? '';

    expect(hint).toContain("govern over the playbook's positions");
    expect(hint).toContain('hard requirements');
    expect(hint).toContain('The instructions box on the Review screen still wins for a single review');
    // Load-bearing wording per guidancePrecedenceCopy.ts: "govern", never a
    // mechanical "will override" of the guidance itself (mirrors
    // review-guidance.test.tsx's own assertion — the copy DOES say hard
    // requirements are things "nothing can override", which is the Floor
    // carve-out, not a claim about the guidance mechanism).
    expect(hint).not.toContain('will override');
  });
});

describe('AdminInstructions — saving', () => {
  it('sends expected_current_version 0 for a first-ever save', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 200, body: NOTHING_SAVED },
      {
        method: 'POST',
        suffix: '/instructions',
        status: 200,
        body: { playbook_id: 'eiaa', version: 1, saved_by: 'local:admin', saved_at: 1_700_000_100 },
      },
    ]);
    render(<AdminInstructions />);

    const textarea = await screen.findByTestId('admin-instructions-text');
    fireEvent.change(textarea, { target: { value: 'Always flag auto-renewal clauses.' } });
    fireEvent.click(screen.getByTestId('admin-instructions-save'));

    await waitFor(() => {
      const posted = requests.find((r) => r.method === 'POST');
      expect(posted).toBeDefined();
      expect(posted?.body).toEqual({
        text: 'Always flag auto-renewal clauses.',
        expected_current_version: 0,
      });
    });
  });

  it('allows saving empty text, and it reads back as cleared', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      {
        method: 'GET',
        suffix: '/instructions',
        status: 200,
        body: {
          current: { version: 4, text: 'Old text.', saved_by: 'local:admin', saved_at: 1_700_000_000 },
          history: [],
        },
      },
      {
        method: 'POST',
        suffix: '/instructions',
        status: 200,
        body: { playbook_id: 'eiaa', version: 5, saved_by: 'local:admin', saved_at: 1_700_000_200 },
      },
    ]);
    render(<AdminInstructions />);

    const textarea = (await screen.findByTestId('admin-instructions-text')) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe('Old text.'));
    fireEvent.change(textarea, { target: { value: '' } });
    fireEvent.click(screen.getByTestId('admin-instructions-save'));

    await waitFor(() => {
      const posted = requests.find((r) => r.method === 'POST');
      expect(posted?.body).toEqual({ text: '', expected_current_version: 4 });
    });
  });

  it('renders no endpoint path or HTTP status when the save request fails', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 200, body: NOTHING_SAVED },
      { method: 'POST', suffix: '/instructions', status: 500, body: {} },
    ]);
    render(<AdminInstructions />);

    await screen.findByTestId('admin-instructions-text');
    fireEvent.click(screen.getByTestId('admin-instructions-save'));

    const banner = await screen.findByTestId('admin-instructions-save-error');
    expect(banner).toHaveTextContent(/couldn't save/i);
    expect(document.body.textContent ?? '').not.toMatch(/HTTP 500|\/api\/admin\//);
  });
});

describe('AdminInstructions — 409 conflict', () => {
  it('never overwrites: keeps the unsaved draft and shows the version that won, side by side', async () => {
    let getCount = 0;
    const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const pathname = new URL(url, 'http://localhost').pathname;
      const method = (init?.method ?? 'GET').toUpperCase();
      requests.push({ method, pathname, body: init?.body ? JSON.parse(init.body as string) : undefined });

      if (method === 'GET' && pathname === '/api/playbooks') {
        return { ok: true, status: 200, json: async () => ONE_PLAYBOOK } as Response;
      }
      if (method === 'GET' && pathname.endsWith('/instructions')) {
        getCount += 1;
        if (getCount === 1) {
          return { ok: true, status: 200, json: async () => NOTHING_SAVED } as Response;
        }
        // The refetch after the 409 sees the version that won the race.
        return {
          ok: true,
          status: 200,
          json: async () => ({
            current: { version: 1, text: "Someone else's edit.", saved_by: 'local:other', saved_at: 1_700_000_050 },
            history: [{ version: 1, text: "Someone else's edit.", saved_by: 'local:other', saved_at: 1_700_000_050 }],
          }),
        } as Response;
      }
      if (method === 'POST' && pathname.endsWith('/instructions')) {
        return {
          ok: false,
          status: 409,
          json: async () => ({ detail: { message: 'conflict', current_version: 1 } }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal('fetch', impl);

    render(<AdminInstructions />);
    const textarea = await screen.findByTestId('admin-instructions-text');
    fireEvent.change(textarea, { target: { value: 'My unsaved edit.' } });
    fireEvent.click(screen.getByTestId('admin-instructions-save'));

    const conflict = await screen.findByTestId('admin-instructions-conflict');
    expect(conflict).toHaveTextContent(
      /Someone saved v1 while you were editing — review their version below, then re-apply your edit\./,
    );
    expect(screen.getByTestId('admin-instructions-conflict-mine')).toHaveTextContent('My unsaved edit.');
    expect(screen.getByTestId('admin-instructions-conflict-theirs')).toHaveTextContent("Someone else's edit.");

    // The admin's draft is untouched, not silently replaced by theirs.
    expect((screen.getByTestId('admin-instructions-text') as HTMLTextAreaElement).value).toBe(
      'My unsaved edit.',
    );
  });
});

describe('AdminInstructions — history', () => {
  const CURRENT = { version: 3, text: 'Latest text.', saved_by: 'local:admin', saved_at: 1_700_000_300 };
  const HISTORY = [
    CURRENT,
    { version: 2, text: 'Middle text.', saved_by: 'local:admin', saved_at: 1_700_000_200 },
    { version: 1, text: 'First text.', saved_by: 'local:admin', saved_at: 1_700_000_100 },
  ];

  it('lists versions newest-first and expands/collapses full text on demand', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 200, body: { current: CURRENT, history: HISTORY } },
    ]);
    render(<AdminInstructions />);

    await screen.findByTestId('admin-instructions-history-card');
    const rows = screen.getAllByTestId(/^admin-instructions-history-row-/);
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'admin-instructions-history-row-3',
      'admin-instructions-history-row-2',
      'admin-instructions-history-row-1',
    ]);

    expect(screen.queryByTestId('admin-instructions-history-text-2')).toBeNull();
    fireEvent.click(screen.getByTestId('admin-instructions-history-toggle-2'));
    expect(await screen.findByTestId('admin-instructions-history-text-2')).toHaveTextContent(
      'Middle text.',
    );
    fireEvent.click(screen.getByTestId('admin-instructions-history-toggle-2'));
    expect(screen.queryByTestId('admin-instructions-history-text-2')).toBeNull();
  });

  it('restoring an old version saves its text as a new version, not an in-place edit', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 200, body: { current: CURRENT, history: HISTORY } },
      {
        method: 'POST',
        suffix: '/instructions',
        status: 200,
        body: { playbook_id: 'eiaa', version: 4, saved_by: 'local:admin', saved_at: 1_700_000_400 },
      },
    ]);
    render(<AdminInstructions />);

    await screen.findByTestId('admin-instructions-history-row-1');
    fireEvent.click(screen.getByTestId('admin-instructions-history-restore-1'));

    await waitFor(() => {
      const posted = requests.find((r) => r.method === 'POST');
      expect(posted).toBeDefined();
      expect(posted?.body).toEqual({ text: 'First text.', expected_current_version: 3 });
    });
  });
});

describe('AdminInstructions — privilege', () => {
  it('hides itself when the catalog read is forbidden', async () => {
    stubRoutes([{ method: 'GET', suffix: '/api/playbooks', status: 403, body: {} }]);
    const { container } = render(<AdminInstructions />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('hides itself when the per-playbook instructions read is forbidden', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 403, body: {} },
    ]);
    const { container } = render(<AdminInstructions />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('hides itself when a save is forbidden', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/api/playbooks', status: 200, body: ONE_PLAYBOOK },
      { method: 'GET', suffix: '/instructions', status: 200, body: NOTHING_SAVED },
      { method: 'POST', suffix: '/instructions', status: 403, body: {} },
    ]);
    const { container } = render(<AdminInstructions />);

    await screen.findByTestId('admin-instructions-text');
    fireEvent.click(screen.getByTestId('admin-instructions-save'));

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });
});
