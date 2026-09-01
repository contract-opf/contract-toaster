/**
 * admin-instructions.test.tsx — the standing-instructions pane on the
 * merged Playbooks admin screen (AdminInstructions.tsx, issue #605's merge
 * of the old standalone "Playbook instructions" tab into AdminPlaybooks.tsx;
 * issue #484, epic #481 sub-issue C originated the pane itself).
 *
 * Since #605, this component takes its playbook as props (`playbookId`,
 * `playbookDisplayName`) instead of owning its own catalog fetch and
 * `<select>` picker — AdminPlaybooks.tsx's playbook table is the only
 * selector now, and it mounts this pane keyed on the selected id (see the
 * "merged standing instructions (#605)" block in admin-playbooks.test.tsx
 * for that coupling, and this component's own docstring for why keying it
 * removes the old stale-response-race class of bug entirely: a switch is a
 * full remount, not a shared instance racing itself).
 *
 * What is worth locking in here, beyond "it renders":
 *
 *   1. **The status line is the ONLY state banner** and reads correctly for
 *      all three states this pane can be in: nothing ever saved, a
 *      non-empty version in effect, and an explicitly cleared version.
 *   2. **A save always sends `expected_current_version`**, and a losing
 *      save (409) never silently overwrites — the admin's own unsaved
 *      draft survives, and the version that won is fetched and shown
 *      alongside it.
 *   3. **History is newest-first, expandable, and restorable** — "Restore
 *      as new version" issues a normal save carrying the old text, not a
 *      bespoke revert endpoint (there isn't one; append-only semantics
 *      made visible per the issue's spec).
 *   4. **Error copy leaks nothing** — no `/api/…` path and no `HTTP <n>`
 *      ever reaches rendered text.
 *   5. **Privilege**: a 403 from any route this pane calls hides it (and
 *      only it — see the "merged standing instructions (#605)" block in
 *      admin-playbooks.test.tsx for the "doesn't take the rest of the
 *      Playbooks screen down with it" case).
 *
 * Fully offline — `fetch` is stubbed, `../auth` is mocked, no network.
 * Per the harness rules (`vitest.config.ts` runs jsdom with `css: false`)
 * every assertion is on structure/text/ARIA/testids, never computed styles.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminInstructions from '../AdminInstructions';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

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
 * wins), then a happy-path default (nothing saved yet). Anything genuinely
 * unmatched 404s.
 */
function stubRoutes(overrides: Handler[] = []): void {
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
    if (method === 'GET' && pathname.endsWith('/instructions')) {
      return { ok: true, status: 200, json: async () => NOTHING_SAVED } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

beforeEach(() => {
  vi.unstubAllGlobals();
  requests = [];
});

describe('AdminInstructions — selection is prop-driven', () => {
  it('loads exactly the playbook named by props, and titles the pane with its display name', async () => {
    stubRoutes([{ method: 'GET', suffix: '/eiaa/instructions', status: 200, body: NOTHING_SAVED }]);
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    await screen.findByTestId('admin-instructions-text');
    expect(screen.getByText('Standing instructions — EIAA')).toBeInTheDocument();
    expect(requests.some((r) => r.method === 'GET' && r.pathname.endsWith('/eiaa/instructions'))).toBe(
      true,
    );
  });
});

describe('AdminInstructions — status line', () => {
  it('says nothing is saved when current is null', async () => {
    stubRoutes();
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    expect(await screen.findByTestId('admin-instructions-status')).toHaveTextContent(
      'No standing instructions — the playbook speaks for itself.',
    );
  });

  it('reports the version, author, and date when non-empty text is in effect', async () => {
    stubRoutes([
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
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    const status = await screen.findByTestId('admin-instructions-status');
    expect(status).toHaveTextContent(/^v3 in effect for every new review/);
    expect(status).toHaveTextContent('local:admin');
  });

  it('reads an explicitly-cleared version as "cleared", not "in effect"', async () => {
    stubRoutes([
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
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    const status = await screen.findByTestId('admin-instructions-status');
    expect(status).toHaveTextContent(/^v5 cleared/);
    expect(status.textContent ?? '').not.toMatch(/in effect/);
  });
});

describe('AdminInstructions — precedence copy', () => {
  it('renders the field label and hint verbatim, sharing GUIDANCE_PRECEDENCE_COPY with the Review screen', async () => {
    stubRoutes();
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

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
    expect(hint).not.toContain('will override');
  });
});

describe('AdminInstructions — saving', () => {
  it('sends expected_current_version 0 for a first-ever save', async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/instructions',
        status: 200,
        body: { playbook_id: 'eiaa', version: 1, saved_by: 'local:admin', saved_at: 1_700_000_100 },
      },
    ]);
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    const textarea = await screen.findByTestId('admin-instructions-text');
    fireEvent.change(textarea, { target: { value: 'Always flag auto-renewal clauses.' } });
    fireEvent.click(screen.getByTestId('admin-instructions-save'));

    await waitFor(() => {
      const posted = requests.find((r) => r.method === 'POST');
      expect(posted).toBeDefined();
      expect(posted?.pathname.endsWith('/eiaa/instructions')).toBe(true);
      expect(posted?.body).toEqual({
        text: 'Always flag auto-renewal clauses.',
        expected_current_version: 0,
      });
    });
  });

  it('allows saving empty text, and it reads back as cleared', async () => {
    stubRoutes([
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
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

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
    stubRoutes([{ method: 'POST', suffix: '/instructions', status: 500, body: {} }]);
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

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

    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);
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
      { method: 'GET', suffix: '/instructions', status: 200, body: { current: CURRENT, history: HISTORY } },
    ]);
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

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
      { method: 'GET', suffix: '/instructions', status: 200, body: { current: CURRENT, history: HISTORY } },
      {
        method: 'POST',
        suffix: '/instructions',
        status: 200,
        body: { playbook_id: 'eiaa', version: 4, saved_by: 'local:admin', saved_at: 1_700_000_400 },
      },
    ]);
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    await screen.findByTestId('admin-instructions-history-row-1');
    fireEvent.click(screen.getByTestId('admin-instructions-history-restore-1'));

    await waitFor(() => {
      const posted = requests.find((r) => r.method === 'POST');
      expect(posted).toBeDefined();
      expect(posted?.body).toEqual({ text: 'First text.', expected_current_version: 3 });
    });
  });
});

describe('AdminInstructions — layout stays one column (#610)', () => {
  /**
   * #610 (#602 split) re-checked this pane during the `ct-columns` wave
   * and concluded no two-column change applies: since #605 moved the
   * playbook picker to AdminPlaybooks.tsx there is exactly ONE form
   * control left here, and #602's own Notes single it out — "the
   * standing-instructions textarea is legitimately wide; do not force it
   * into a narrow column". These two assertions are what makes that
   * conclusion enforceable rather than a comment: an eager pass at
   * "finishing" #602 across the admin screens now trips a test.
   */
  it('the standing-instructions textarea is NOT put into a ct-columns row', async () => {
    stubRoutes();
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    const textarea = await screen.findByTestId('admin-instructions-text');
    expect(textarea.closest('ct-columns')).toBeNull();
    expect(textarea.closest('ct-field')?.hasAttribute('narrow')).toBe(false);
  });

  it('the pane has exactly one form control, which is why there is nothing to pair', async () => {
    stubRoutes();
    render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    const panel = await screen.findByTestId('admin-instructions-panel');
    // History rows render <ct-button>s, not form controls; the picker that
    // used to live here is AdminPlaybooks' now (#605).
    const controls = panel.querySelectorAll('input,select,textarea');
    expect(controls.length).toBe(1);
    expect(controls[0].tagName.toLowerCase()).toBe('textarea');
  });
});

describe('AdminInstructions — privilege', () => {
  it('hides itself when the per-playbook instructions read is forbidden', async () => {
    stubRoutes([{ method: 'GET', suffix: '/instructions', status: 403, body: {} }]);
    const { container } = render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('hides itself when a save is forbidden', async () => {
    stubRoutes([{ method: 'POST', suffix: '/instructions', status: 403, body: {} }]);
    const { container } = render(<AdminInstructions playbookId="eiaa" playbookDisplayName="EIAA" />);

    await screen.findByTestId('admin-instructions-text');
    fireEvent.click(screen.getByTestId('admin-instructions-save'));

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });
});
