/**
 * admin-playbooks.test.tsx — the playbook lifecycle admin tab
 * (AdminPlaybooks.tsx, issue #434).
 *
 * What is worth locking in here, beyond "it renders":
 *
 *   1. **Every mutation is server-confirmed, never optimistic.** Each action
 *      test asserts the REQUEST that went out (method + path + body), not
 *      just that the row changed — a component that re-rendered from local
 *      state without calling the route would pass a text-only assertion.
 *   2. **The two backend refusals this screen depends on are surfaced
 *      verbatim.** Rolling back to a never-active version (409) and a
 *      Gate-7-refused activation both carry a `detail` the client cannot
 *      reconstruct; the tests mock each and assert the server's own sentence
 *      reaches the DOM.
 *   3. **Remove is a two-click confirm (§14).** One click must NOT delete —
 *      the test asserts zero DELETE requests after the first click, which is
 *      the assertion a naive `onClick={remove}` implementation fails.
 *   4. **Error copy leaks nothing.** No `/api/…` path and no `HTTP <n>` ever
 *      reaches rendered text, matching resilience-a11y.test.tsx's rule.
 *
 * Fully offline — `fetch` is stubbed, `../auth` is mocked, no network.
 * Per the harness rules (`vitest.config.ts` runs jsdom with `css: false`)
 * every assertion is on structure/text/ARIA/testids, never computed styles.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AdminPlaybooks, { shortenHash } from '../AdminPlaybooks';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const FULL_HASH = `sha256:${'ab12cd34ef56'.repeat(5)}abcd`;

const CATALOG = {
  playbooks: [
    {
      playbook_id: 'synthetic-nda-sample',
      display_name: 'Synthetic NDA Sample',
      status: 'active',
      notes: 'Shipped sample.',
    },
    {
      playbook_id: 'other-agreement',
      display_name: 'Other Agreement',
      status: 'coming_soon',
      notes: '',
    },
  ],
};

const TRAIL = {
  versions: [
    {
      playbook_id: 'synthetic-nda-sample',
      version: 'v1.0.0',
      uploaded_by: 'admin-sub',
      uploaded_at: 1_700_000_000,
      status: 'retired',
      notes: 'First cut.',
      content_hash: FULL_HASH,
    },
    {
      playbook_id: 'synthetic-nda-sample',
      version: 'v2.0.0',
      uploaded_by: 'admin-sub',
      uploaded_at: 1_700_000_500,
      status: 'active',
      notes: '',
      content_hash: FULL_HASH,
    },
    {
      playbook_id: 'synthetic-nda-sample',
      version: 'v3.0.0',
      uploaded_by: 'admin-sub',
      uploaded_at: 1_700_001_000,
      status: 'draft',
      notes: '',
    },
  ],
};

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
 * Route stub. `overrides` are consulted first (first match wins), then the
 * happy-path catalog/trail reads. Anything unmatched 404s — which surfaces
 * as a visible error rather than silently looking like a pass.
 */
function stubRoutes(overrides: Handler[] = []): void {
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    const method = (init?.method ?? 'GET').toUpperCase();
    const rawBody = init?.body;
    const body =
      typeof rawBody === 'string'
        ? JSON.parse(rawBody)
        : rawBody instanceof FormData
          ? Object.fromEntries(rawBody.entries())
          : undefined;
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
      return { ok: true, status: 200, json: async () => CATALOG } as Response;
    }
    if (method === 'GET' && pathname.endsWith('/versions')) {
      return { ok: true, status: 200, json: async () => TRAIL } as Response;
    }
    if (method !== 'GET') {
      return { ok: true, status: 200, json: async () => ({}) } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', impl);
}

/** Render and wait for the catalog to land. */
async function renderPanel(): Promise<void> {
  render(<AdminPlaybooks />);
  await screen.findByTestId('playbook-row-synthetic-nda-sample');
}

/** Render, then open the version history for the seeded sample. */
async function renderWithVersions(): Promise<void> {
  await renderPanel();
  fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
  await screen.findByTestId('playbook-version-row-v1.0.0');
}

function requestsMatching(method: string, suffix: string): Recorded[] {
  return requests.filter((r) => r.method === method && r.pathname.endsWith(suffix));
}

function rendered(): string {
  return document.body.textContent ?? '';
}

beforeEach(() => {
  requests = [];
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Load / empty / error / loading states
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — load states', () => {
  it('shows a ct-progress bar while the catalog is in flight, not a bare paragraph', () => {
    stubRoutes();
    render(<AdminPlaybooks />);
    const progress = screen.getByTestId('admin-playbooks-loading');
    expect(progress).toBeInTheDocument();
    expect(screen.queryByTestId('playbooks-table')).toBeNull();
  });

  it('lists every catalog entry with its identifier, status chip and note', async () => {
    stubRoutes();
    await renderPanel();

    const row = screen.getByTestId('playbook-row-synthetic-nda-sample');
    expect(within(row).getByText('Synthetic NDA Sample')).toBeInTheDocument();
    expect(within(row).getByText('synthetic-nda-sample')).toBeInTheDocument();
    expect(within(row).getByText('Shipped sample.')).toBeInTheDocument();
    expect(screen.getByTestId('playbook-status-synthetic-nda-sample').textContent).toContain(
      'active',
    );

    // "coming_soon" is shown honestly as "not active" on the admin surface.
    expect(screen.getByTestId('playbook-status-other-agreement').textContent).toContain(
      'not active',
    );
  });

  it('renders the empty state as an in-table row, not a standalone paragraph', async () => {
    stubRoutes([{ method: 'GET', suffix: '/api/playbooks', status: 200, body: { playbooks: [] } }]);
    render(<AdminPlaybooks />);

    const empty = await screen.findByTestId('admin-playbooks-empty');
    expect(empty.tagName).toBe('TD');
    expect(empty.closest('table')).toBe(screen.getByTestId('playbooks-table'));
  });

  it('shows friendly copy — no endpoint path, no HTTP status — when the catalog read fails', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    stubRoutes([{ method: 'GET', suffix: '/api/playbooks', status: 500, body: {} }]);
    render(<AdminPlaybooks />);

    const banner = await screen.findByTestId('admin-playbooks-error');
    expect(banner.textContent).toContain("We couldn't load your playbooks");
    expect(rendered()).not.toMatch(/HTTP\s*\d/i);
    expect(rendered()).not.toContain('/api/');
  });

  it('hides itself entirely when an admin route answers 403', async () => {
    stubRoutes([{ method: 'GET', suffix: '/api/playbooks', status: 403, body: {} }]);
    const { container } = render(<AdminPlaybooks />);

    await waitFor(() => {
      expect(screen.queryByTestId('admin-playbooks-panel')).toBeNull();
    });
    expect(container.textContent).toBe('');
  });
});

// ---------------------------------------------------------------------------
// Version history
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — version history', () => {
  it('loads the trail for the chosen playbook and renders one row per version', async () => {
    stubRoutes();
    await renderWithVersions();

    expect(screen.getByTestId('playbook-versions-table')).toBeInTheDocument();
    expect(screen.getByTestId('playbook-version-row-v1.0.0')).toBeInTheDocument();
    expect(screen.getByTestId('playbook-version-row-v2.0.0')).toBeInTheDocument();
    expect(screen.getByTestId('playbook-version-row-v3.0.0')).toBeInTheDocument();
    expect(requestsMatching('GET', '/synthetic-nda-sample/versions')).toHaveLength(1);
  });

  it('shows the status chip, uploader and truncated hash — with the full hash still reachable', async () => {
    stubRoutes();
    await renderWithVersions();

    expect(screen.getByTestId('playbook-version-status-v2.0.0').textContent).toContain('active');
    expect(screen.getByTestId('playbook-version-status-v3.0.0').textContent).toContain('draft');

    const hashCell = screen.getByTestId('playbook-version-hash-v1.0.0');
    expect(hashCell.textContent).toBe(shortenHash(FULL_HASH));
    expect(hashCell.textContent).not.toBe(FULL_HASH);
    // Truncated in the cell, never lost: the full digest stays on the title.
    expect(hashCell).toHaveAttribute('title', FULL_HASH);
    expect(hashCell.className).toContain('ct-table__mono');

    expect(within(screen.getByTestId('playbook-version-row-v1.0.0')).getByText('admin-sub')).toBeInTheDocument();
  });

  it('shows the in-table empty row for a playbook with no uploads', async () => {
    stubRoutes([
      { method: 'GET', suffix: '/other-agreement/versions', status: 200, body: { versions: [] } },
    ]);
    await renderPanel();
    fireEvent.click(screen.getByTestId('playbook-versions-other-agreement'));

    const empty = await screen.findByTestId('admin-playbooks-versions-empty');
    expect(empty.tagName).toBe('TD');
    expect(empty.closest('table')).toBe(screen.getByTestId('playbook-versions-table'));
  });

  it('states permanently that activation is checked against the approved hash', async () => {
    stubRoutes();
    await renderWithVersions();
    expect(screen.getByTestId('admin-playbooks-activation-note').textContent).toContain(
      'approved hash',
    );
  });
});

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — upload', () => {
  async function openUpload(): Promise<void> {
    fireEvent.click(screen.getByTestId('admin-playbooks-upload-toggle'));
    await screen.findByTestId('admin-playbooks-upload-panel');
  }

  it('posts the file and version as multipart and refreshes the trail', async () => {
    stubRoutes();
    await renderPanel();
    await openUpload();

    fireEvent.change(screen.getByTestId('admin-playbooks-upload-playbook'), {
      target: { value: 'synthetic-nda-sample' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-version'), {
      target: { value: 'v4.0.0' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-file'), {
      target: { files: [new File(['{}'], 'playbook.opf.json', { type: 'application/json' })] },
    });
    fireEvent.click(screen.getByTestId('admin-playbooks-upload-submit'));

    await screen.findByTestId('admin-playbooks-upload-success');

    const uploads = requestsMatching('POST', '/synthetic-nda-sample/versions');
    expect(uploads).toHaveLength(1);
    const form = uploads[0]!.body as Record<string, unknown>;
    expect(form.version).toBe('v4.0.0');
    expect(form.file).toBeInstanceOf(File);
    // The server computes the hash; a client-supplied one is only ever
    // validated against it, so this form never sends one.
    expect(form.content_hash).toBeUndefined();

    // Server-confirmed, then re-read: the trail is fetched again rather than
    // having the new row spliced in locally.
    await waitFor(() => {
      expect(requestsMatching('GET', '/synthetic-nda-sample/versions').length).toBeGreaterThan(0);
    });
  });

  it('sends a typed note as its own notes call, since the upload route records none', async () => {
    stubRoutes();
    await renderPanel();
    await openUpload();

    fireEvent.change(screen.getByTestId('admin-playbooks-upload-playbook'), {
      target: { value: 'synthetic-nda-sample' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-version'), {
      target: { value: 'v4.0.0' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-notes'), {
      target: { value: 'Adds the new indemnity position.' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-file'), {
      target: { files: [new File(['{}'], 'playbook.opf.json')] },
    });
    fireEvent.click(screen.getByTestId('admin-playbooks-upload-submit'));

    await screen.findByTestId('admin-playbooks-upload-success');
    const notesCalls = requestsMatching('PATCH', '/versions/v4.0.0/notes');
    expect(notesCalls).toHaveLength(1);
    expect(notesCalls[0]!.body).toEqual({ notes: 'Adds the new indemnity position.' });
  });

  it('refuses to send an upload with no file chosen', async () => {
    stubRoutes();
    await renderPanel();
    await openUpload();

    fireEvent.change(screen.getByTestId('admin-playbooks-upload-playbook'), {
      target: { value: 'synthetic-nda-sample' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-version'), {
      target: { value: 'v4.0.0' },
    });
    fireEvent.click(screen.getByTestId('admin-playbooks-upload-submit'));

    expect(await screen.findByTestId('admin-playbooks-upload-error')).toBeInTheDocument();
    expect(requestsMatching('POST', '/synthetic-nda-sample/versions')).toHaveLength(0);
  });

  it("surfaces the server's own refusal for a re-used version identifier", async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/synthetic-nda-sample/versions',
        status: 409,
        body: {
          detail:
            'playbook version already recorded (append-only — re-uploads must use a new version identifier)',
        },
      },
    ]);
    await renderPanel();
    await openUpload();

    fireEvent.change(screen.getByTestId('admin-playbooks-upload-playbook'), {
      target: { value: 'synthetic-nda-sample' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-version'), {
      target: { value: 'v1.0.0' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-upload-file'), {
      target: { files: [new File(['{}'], 'playbook.opf.json')] },
    });
    fireEvent.click(screen.getByTestId('admin-playbooks-upload-submit'));

    const banner = await screen.findByTestId('admin-playbooks-upload-error');
    expect(banner.textContent).toContain('append-only');
    expect(screen.queryByTestId('admin-playbooks-upload-success')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Activate / roll back
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — activate and roll back', () => {
  it('activates a draft version through the activate route', async () => {
    stubRoutes();
    await renderWithVersions();

    fireEvent.click(screen.getByTestId('playbook-version-activate-v3.0.0'));

    await waitFor(() => {
      expect(requestsMatching('POST', '/versions/v3.0.0/activate')).toHaveLength(1);
    });
  });

  it("shows the server's Gate-7 refusal instead of a generic failure", async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/versions/v3.0.0/activate',
        status: 409,
        body: {
          detail:
            'Gate 7: the bytes changed after approval (or were never approved) and the bundle cannot be activated',
        },
      },
    ]);
    await renderWithVersions();

    fireEvent.click(screen.getByTestId('playbook-version-activate-v3.0.0'));

    const banner = await screen.findByTestId('admin-playbooks-action-error');
    expect(banner.textContent).toContain('never approved');
    expect(rendered()).not.toMatch(/HTTP\s*\d/i);
  });

  it('rolls back to a retired version', async () => {
    stubRoutes();
    await renderWithVersions();

    fireEvent.click(screen.getByTestId('playbook-version-rollback-v1.0.0'));

    await waitFor(() => {
      expect(requestsMatching('POST', '/versions/v1.0.0/rollback')).toHaveLength(1);
    });
  });

  it('hides roll back entirely for a version that was never active, rather than disabling it', async () => {
    stubRoutes();
    await renderWithVersions();

    // v3.0.0 is a draft — it was never active, so there is nothing to roll
    // back to. The affordance is absent, not a disabled dead end (#476).
    expect(screen.queryByTestId('playbook-version-rollback-v3.0.0')).toBeNull();
  });

  it('hides Activate on the currently-active version and shows a quiet note in its place', async () => {
    stubRoutes();
    await renderWithVersions();

    const activeRow = screen.getByTestId('playbook-version-row-v2.0.0');
    expect(within(activeRow).queryByTestId('playbook-version-activate-v2.0.0')).toBeNull();
    expect(within(activeRow).getByTestId('playbook-version-active-note-v2.0.0').textContent).toContain(
      'active',
    );
    // Re-running activation on the active version is likewise not offered
    // as a hidden rollback target.
    expect(within(activeRow).queryByTestId('playbook-version-rollback-v2.0.0')).toBeNull();
  });

  it('offers Activate on a draft row', async () => {
    stubRoutes();
    await renderWithVersions();

    expect(screen.getByTestId('playbook-version-activate-v3.0.0')).toBeEnabled();
  });

  it('shows neither Activate nor Roll back for a single-version, already-active playbook', async () => {
    stubRoutes([
      {
        method: 'GET',
        suffix: '/other-agreement/versions',
        status: 200,
        body: {
          versions: [
            {
              playbook_id: 'other-agreement',
              version: 'v1.0.0',
              uploaded_by: 'admin-sub',
              uploaded_at: 1_700_000_000,
              status: 'active',
              notes: '',
              content_hash: FULL_HASH,
            },
          ],
        },
      },
    ]);
    await renderPanel();
    fireEvent.click(screen.getByTestId('playbook-versions-other-agreement'));
    await screen.findByTestId('playbook-version-row-v1.0.0');

    expect(screen.queryByTestId('playbook-version-activate-v1.0.0')).toBeNull();
    expect(screen.queryByTestId('playbook-version-rollback-v1.0.0')).toBeNull();
  });

  it("surfaces the backend's never-active refusal when it rejects a rollback", async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/versions/v1.0.0/rollback',
        status: 409,
        body: {
          detail:
            'that version was never active — there is nothing to roll back to; activate it instead',
        },
      },
    ]);
    await renderWithVersions();

    fireEvent.click(screen.getByTestId('playbook-version-rollback-v1.0.0'));

    const banner = await screen.findByTestId('admin-playbooks-action-error');
    expect(banner.textContent).toContain('never active');
  });
});

// ---------------------------------------------------------------------------
// Rename / notes / remove
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — rename', () => {
  it('sends the new display name and re-reads the catalog', async () => {
    stubRoutes();
    await renderPanel();

    fireEvent.click(screen.getByTestId('playbook-rename-synthetic-nda-sample'));
    const input = await screen.findByTestId('playbook-rename-input-synthetic-nda-sample');
    expect(input).toHaveValue('Synthetic NDA Sample');

    fireEvent.change(input, { target: { value: 'House NDA' } });
    fireEvent.click(screen.getByTestId('playbook-rename-save-synthetic-nda-sample'));

    await waitFor(() => {
      const calls = requestsMatching('PATCH', '/api/admin/playbooks/synthetic-nda-sample');
      expect(calls).toHaveLength(1);
      expect(calls[0]!.body).toEqual({ display_name: 'House NDA' });
    });
  });

  it('cancels without sending anything', async () => {
    stubRoutes();
    await renderPanel();

    fireEvent.click(screen.getByTestId('playbook-rename-synthetic-nda-sample'));
    await screen.findByTestId('playbook-rename-input-synthetic-nda-sample');
    fireEvent.click(screen.getByTestId('playbook-rename-cancel-synthetic-nda-sample'));

    await waitFor(() => {
      expect(screen.queryByTestId('playbook-rename-input-synthetic-nda-sample')).toBeNull();
    });
    expect(requestsMatching('PATCH', '/api/admin/playbooks/synthetic-nda-sample')).toHaveLength(0);
  });
});

describe('AdminPlaybooks — per-version notes', () => {
  it('edits an existing note through the notes route', async () => {
    stubRoutes();
    await renderWithVersions();

    fireEvent.click(screen.getByTestId('playbook-version-notes-edit-v1.0.0'));
    const input = await screen.findByTestId('playbook-version-notes-input-v1.0.0');
    expect(input).toHaveValue('First cut.');

    fireEvent.change(input, { target: { value: 'Superseded by v2.' } });
    fireEvent.click(screen.getByTestId('playbook-version-notes-save-v1.0.0'));

    await waitFor(() => {
      const calls = requestsMatching('PATCH', '/versions/v1.0.0/notes');
      expect(calls).toHaveLength(1);
      expect(calls[0]!.body).toEqual({ notes: 'Superseded by v2.' });
    });
  });

  it('offers to add a note on a version that has none', async () => {
    stubRoutes();
    await renderWithVersions();

    const addButton = screen.getByTestId('playbook-version-notes-edit-v2.0.0');
    expect(addButton.textContent).toContain('Add a note');
    fireEvent.click(addButton);
    expect(await screen.findByTestId('playbook-version-notes-input-v2.0.0')).toHaveValue('');
  });
});

// ---------------------------------------------------------------------------
// Note links (issue #476)
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — linkified notes', () => {
  const SAMPLE_NOTE_URL = 'https://github.com/contract-opf/playbooks';

  it("linkifies an http(s) URL in a version's note, opening it in a new tab", async () => {
    stubRoutes([
      {
        method: 'GET',
        suffix: '/synthetic-nda-sample/versions',
        status: 200,
        body: {
          versions: [
            {
              playbook_id: 'synthetic-nda-sample',
              version: 'v1.0.0',
              uploaded_by: 'admin-sub',
              uploaded_at: 1_700_000_000,
              status: 'active',
              notes: `See ${SAMPLE_NOTE_URL} for the source.`,
              content_hash: FULL_HASH,
            },
          ],
        },
      },
    ]);
    await renderPanel();
    fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
    const row = await screen.findByTestId('playbook-version-row-v1.0.0');

    const link = within(row).getByRole('link', { name: SAMPLE_NOTE_URL });
    expect(link).toHaveAttribute('href', SAMPLE_NOTE_URL);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
    expect(link.getAttribute('rel')).toContain('noreferrer');
    // The trailing period is punctuation, not part of the link.
    expect(row.textContent).toContain(`${SAMPLE_NOTE_URL} for the source.`);
  });

  it('linkifies the active version note on the top-level playbook table too', async () => {
    stubRoutes([
      {
        method: 'GET',
        suffix: '/api/playbooks',
        status: 200,
        body: {
          playbooks: [
            {
              playbook_id: 'synthetic-nda-sample',
              display_name: 'Synthetic NDA Sample',
              status: 'active',
              notes: `Shipped sample — see ${SAMPLE_NOTE_URL}`,
            },
          ],
        },
      },
    ]);
    await renderPanel();

    const row = screen.getByTestId('playbook-row-synthetic-nda-sample');
    expect(within(row).getByRole('link', { name: SAMPLE_NOTE_URL })).toHaveAttribute(
      'href',
      SAMPLE_NOTE_URL,
    );
  });

  it('leaves a non-http(s) scheme as plain text, never a clickable link', async () => {
    stubRoutes([
      {
        method: 'GET',
        suffix: '/synthetic-nda-sample/versions',
        status: 200,
        body: {
          versions: [
            {
              playbook_id: 'synthetic-nda-sample',
              version: 'v1.0.0',
              uploaded_by: 'admin-sub',
              uploaded_at: 1_700_000_000,
              status: 'draft',
              notes: 'Do not click javascript:alert(1) — testing only.',
              content_hash: FULL_HASH,
            },
          ],
        },
      },
    ]);
    await renderPanel();
    fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
    const row = await screen.findByTestId('playbook-version-row-v1.0.0');

    expect(within(row).queryByRole('link')).toBeNull();
    expect(row.textContent).toContain('javascript:alert(1)');
  });
});

describe('AdminPlaybooks — remove (confirm-step)', () => {
  it('does NOT remove on the first click — it only arms', async () => {
    stubRoutes();
    await renderPanel();

    fireEvent.click(screen.getByTestId('playbook-remove-synthetic-nda-sample'));

    // The load-bearing assertion: a naive onClick={remove} sends here.
    await waitFor(() => {
      expect(requestsMatching('DELETE', '/api/admin/playbooks/synthetic-nda-sample')).toHaveLength(
        0,
      );
    });
    expect(screen.getByTestId('playbook-remove-synthetic-nda-sample').textContent).toContain(
      'Click again to remove',
    );
  });

  it('removes on the second click and re-reads the catalog', async () => {
    stubRoutes();
    await renderPanel();

    const button = screen.getByTestId('playbook-remove-synthetic-nda-sample');
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => {
      expect(requestsMatching('DELETE', '/api/admin/playbooks/synthetic-nda-sample')).toHaveLength(
        1,
      );
    });
    await waitFor(() => {
      expect(requestsMatching('GET', '/api/playbooks').length).toBeGreaterThan(1);
    });
  });

  it('disarms on blur, so a stray click elsewhere cancels the removal', async () => {
    stubRoutes();
    await renderPanel();

    const button = screen.getByTestId('playbook-remove-synthetic-nda-sample');
    fireEvent.click(button);
    expect(button.textContent).toContain('Click again to remove');

    fireEvent.blur(button);
    expect(button.textContent).toContain('Remove');
    expect(button.textContent).not.toContain('Click again');

    fireEvent.click(button);
    await waitFor(() => {
      expect(requestsMatching('DELETE', '/api/admin/playbooks/synthetic-nda-sample')).toHaveLength(
        0,
      );
    });
  });
});

// ---------------------------------------------------------------------------
// Pure helper
// ---------------------------------------------------------------------------

describe('shortenHash', () => {
  it('keeps the algorithm prefix and the leading digest, and marks the truncation', () => {
    expect(shortenHash(`sha256:${'a'.repeat(64)}`)).toBe(`sha256:${'a'.repeat(12)}…`);
  });

  it('leaves a value that is already short alone', () => {
    expect(shortenHash('sha256:abc')).toBe('sha256:abc');
    expect(shortenHash('abc')).toBe('abc');
  });
});
