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
    // Issue #605: selecting a playbook (the "Version history" click below)
    // now also mounts its standing-instructions pane, which fetches this on
    // its own — every test that selects a playbook needs a safe default for
    // it, not just the ones that exercise the pane directly.
    if (method === 'GET' && pathname.endsWith('/instructions')) {
      return { ok: true, status: 200, json: async () => ({ current: null, history: [] }) } as Response;
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

/**
 * Render, then choose the seeded sample as the playbook whose STANDING
 * INSTRUCTIONS are shown. Since issue #611 that is its own control: opening
 * version history no longer doubles as the instructions selector.
 */
async function renderWithInstructions(): Promise<void> {
  await renderPanel();
  fireEvent.click(screen.getByTestId('playbook-instructions-synthetic-nda-sample'));
  await screen.findByTestId('admin-instructions-panel');
}

function requestsMatching(method: string, suffix: string): Recorded[] {
  return requests.filter((r) => r.method === method && r.pathname.endsWith(suffix));
}

function rendered(): string {
  return document.body.textContent ?? '';
}

/**
 * Issue #597: the version identifier is READ from the artifact, so every test
 * that uploads a new playbook now has to hand over a file that actually
 * carries an `identity` block. A bare `new File(['{}'], …)` no longer means
 * anything to this form.
 *
 * Minimal synthetic OPF shapes only — no corpus text, no real playbook
 * clauses, per the ticket's own instruction.
 */
function opfFile(identity: Record<string, unknown>, name = 'playbook.opf.json'): File {
  return new File([JSON.stringify({ opf_version: '0.3', identity })], name, {
    type: 'application/json',
  });
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
// Approve for activation (issue #485 — Gate 7's missing write path)
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — approve for activation', () => {
  it('sends back exactly the row\'s own content_hash, never operator free-text', async () => {
    stubRoutes();
    await renderWithVersions();

    // v1.0.0 in the shared TRAIL fixture carries a content_hash but no
    // legal_approval_content_hash -- an upload that has never been
    // approved, the normal state for a freshly-uploaded version.
    //
    // Issue #595 moved the control this drives from a standalone "Approve
    // for activation" to the merged "Approve & activate"; the property under
    // test is unchanged -- the approval names the exact bytes on the row.
    const approveButton = screen.getByTestId('playbook-version-approve-activate-v1.0.0');
    fireEvent.click(approveButton);

    await waitFor(() => {
      expect(requestsMatching('POST', '/versions/v1.0.0/legal-approval')).toHaveLength(1);
    });
    const call = requestsMatching('POST', '/versions/v1.0.0/legal-approval')[0]!;
    expect(call.body).toEqual({ content_hash: FULL_HASH });
  });

  it('shows a quiet "Approved" note instead of a button once approved, and re-reads the trail after approving', async () => {
    stubRoutes([
      {
        method: 'GET',
        suffix: '/synthetic-nda-sample/versions',
        status: 200,
        body: {
          versions: [
            {
              ...TRAIL.versions[0],
              legal_approval_content_hash: FULL_HASH,
            },
          ],
        },
      },
    ]);
    await renderWithVersions();

    expect(screen.getByTestId('playbook-version-approved-note-v1.0.0').textContent).toBe(
      'Approved',
    );
    expect(screen.queryByTestId('playbook-version-approve-v1.0.0')).toBeNull();
    expect(screen.queryByTestId('playbook-version-approve-activate-v1.0.0')).toBeNull();
  });

  it('offers neither an Approve button nor an Approved note for a row with no content_hash at all', async () => {
    stubRoutes();
    await renderWithVersions();

    // v3.0.0 in the shared TRAIL fixture has no content_hash (a row
    // written before issue #478) -- nothing to approve.
    expect(screen.queryByTestId('playbook-version-approve-v3.0.0')).toBeNull();
    expect(screen.queryByTestId('playbook-version-approved-note-v3.0.0')).toBeNull();
  });

  /**
   * Issue #594. The shipped playbook is installed by `seed_shipped_playbook`,
   * which deliberately bypasses Gate 7 (see `backend/src/sample_playbooks.py`
   * — fabricating a legal_approval for content nobody reviewed would be worse
   * than not having the ceremony). Nothing in that path calls
   * `record_legal_approval`, so the seeded row is `active` with NO
   * `legal_approval_content_hash` at all.
   *
   * The old cell keyed the Approve button off `isApproved()` alone, so that
   * row offered "Approve for activation" on a version that is already the
   * live one — which reads as either a no-op or a dangerous re-arming, and
   * the owner could not tell which.
   *
   * Option (a) from the ticket: render the control only where it is
   * meaningful, and state the bypass rather than hiding it. Suppressing the
   * button silently (option b) would be worse than the bug — an audit screen
   * that quietly omits "the live playbook was never approved" is actively
   * misleading.
   */
  it('offers no Approve on an already-active, never-approved version, and says why', async () => {
    stubRoutes();
    await renderWithVersions();

    // v2.0.0 in the shared TRAIL fixture is exactly the seeded shape:
    // status 'active', a real content_hash, no legal_approval_content_hash.
    const activeRow = screen.getByTestId('playbook-version-row-v2.0.0');
    expect(within(activeRow).queryByTestId('playbook-version-approve-v2.0.0')).toBeNull();

    const note = within(activeRow).getByTestId(
      'playbook-version-unapproved-active-note-v2.0.0',
    );
    expect(note.textContent).toContain('Never approved');
  });

  it('still offers approval on a version that is neither approved NOR active', async () => {
    stubRoutes();
    await renderWithVersions();

    // The regression guard for the fix above: v1.0.0 is retired and
    // unapproved, so approving it is still a meaningful act. Hiding the
    // approval control everywhere would pass the assertion above and break
    // this one. (Issue #595 moved that control into "Approve & activate";
    // what must not regress is that SOME live approval path exists here.)
    expect(screen.getByTestId('playbook-version-approve-activate-v1.0.0')).toBeEnabled();
    expect(
      screen.queryByTestId('playbook-version-unapproved-active-note-v1.0.0'),
    ).toBeNull();
  });

  it('still shows the plain "Approved" note on a version that IS approved and active', async () => {
    stubRoutes([
      {
        method: 'GET',
        suffix: '/synthetic-nda-sample/versions',
        status: 200,
        body: {
          versions: [{ ...TRAIL.versions[1], legal_approval_content_hash: FULL_HASH }],
        },
      },
    ]);
    await renderPanel();
    fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
    await screen.findByTestId('playbook-version-row-v2.0.0');

    expect(screen.getByTestId('playbook-version-approved-note-v2.0.0').textContent).toBe(
      'Approved',
    );
    expect(screen.queryByTestId('playbook-version-unapproved-active-note-v2.0.0')).toBeNull();
  });

  it("surfaces the backend's mismatch refusal verbatim", async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/versions/v1.0.0/legal-approval',
        status: 409,
        body: { detail: 'content_hash mismatch: approval must name the exact bytes recorded' },
      },
    ]);
    await renderWithVersions();

    fireEvent.click(screen.getByTestId('playbook-version-approve-activate-v1.0.0'));

    const banner = await screen.findByTestId('admin-playbooks-action-error');
    expect(banner.textContent).toContain('content_hash mismatch');
    // A refused approval must not be shown as approved.
    expect(screen.queryByTestId('playbook-version-approved-note-v1.0.0')).toBeNull();
    // ...and a refused APPROVAL must not go on to activate anything: the
    // sequence stops at the first failure (issue #595).
    expect(requestsMatching('POST', '/versions/v1.0.0/activate')).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Version history is an overlay, and row actions are a group (issue #598)
// ---------------------------------------------------------------------------

/**
 * Issue #598, both defects reported by the owner from the live screen.
 *
 * 1. Version history expanded INLINE beneath the table, pushing everything
 *    down and leaving the operator unsure what they were looking at. Owner:
 *    "version history should maybe be in a, like, an overlay window."
 * 2. The Actions cell rendered three full-size buttons stacked vertically, so
 *    a two-row table became a wall of six, with the destructive one carrying
 *    the same visual weight as the routine ones. Owner: "It's like a stack of
 *    jumbley buttons."
 *
 * On the dialog implementation: CTDS has no dialog primitive, and the ticket
 * says not to invent a shared one here — so this is local to the screen. It
 * is a `div[role=dialog][aria-modal]` with hand-written focus management
 * rather than a native `<dialog>` + `showModal()`, because jsdom implements
 * no `showModal` at all (verified: `typeof dialog.showModal` is `undefined`
 * under this harness). A native dialog would have made every assertion below
 * — the role, Escape, the focus return — untestable, which is the opposite of
 * what an a11y-sensitive new overlay surface needs.
 */
describe('AdminPlaybooks — version history overlay and grouped row actions', () => {
  it('opens version history in a dialog whose accessible name says which playbook', async () => {
    stubRoutes();
    await renderPanel();

    // Nothing is showing before the click — this is not a permanently
    // rendered panel that merely toggles a class.
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));

    const dialog = await screen.findByRole('dialog', {
      name: /Version history — Synthetic NDA Sample/,
    });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    // The version table lives INSIDE the overlay, not below the page.
    expect(within(dialog).getByTestId('playbook-versions-table')).toBeInTheDocument();
  });

  it('closes on Escape and returns focus to the control that opened it', async () => {
    stubRoutes();
    await renderPanel();

    const opener = screen.getByTestId('playbook-versions-synthetic-nda-sample');
    fireEvent.click(opener);
    const dialog = await screen.findByRole('dialog');

    // Focus is moved INTO the overlay on open, or the keyboard user is left
    // behind the modal with nothing to Escape from.
    await waitFor(() => {
      expect(dialog.contains(document.activeElement)).toBe(true);
    });

    fireEvent.keyDown(dialog, { key: 'Escape' });

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    // Focus RETURNS. A modal that strands focus on close is worse than the
    // inline panel it replaced.
    expect(document.activeElement).toBe(opener);
  });

  it('closes on its own close control and returns focus there too', async () => {
    stubRoutes();
    await renderPanel();

    const opener = screen.getByTestId('playbook-versions-other-agreement');
    fireEvent.click(opener);
    await screen.findByRole('dialog');

    fireEvent.click(screen.getByTestId('admin-playbooks-versions-close'));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    expect(document.activeElement).toBe(opener);
  });

  it('keeps every version-history capability reachable from inside the overlay', async () => {
    stubRoutes();
    await renderPanel();
    fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByTestId('playbook-version-row-v1.0.0');

    // The full inventory of what the inline panel offered, enumerated so a
    // "move it into a modal" refactor cannot quietly drop one of them.
    const scoped = within(dialog);
    // The permanent Gate-7 explanation.
    expect(scoped.getByTestId('admin-playbooks-activation-note')).toBeInTheDocument();
    // One row per version, with status, hash, uploader and timestamp.
    expect(scoped.getByTestId('playbook-version-row-v1.0.0')).toBeInTheDocument();
    expect(scoped.getByTestId('playbook-version-row-v2.0.0')).toBeInTheDocument();
    expect(scoped.getByTestId('playbook-version-row-v3.0.0')).toBeInTheDocument();
    expect(scoped.getByTestId('playbook-version-status-v2.0.0')).toBeInTheDocument();
    expect(scoped.getByTestId('playbook-version-hash-v1.0.0')).toBeInTheDocument();
    // Per-version notes.
    expect(scoped.getByTestId('playbook-version-notes-edit-v1.0.0')).toBeInTheDocument();
    // Approve & activate (#595), plain Activate, and Roll back.
    expect(scoped.getByTestId('playbook-version-approve-activate-v1.0.0')).toBeInTheDocument();
    expect(scoped.getByTestId('playbook-version-activate-v3.0.0')).toBeInTheDocument();
    expect(scoped.getByTestId('playbook-version-rollback-v1.0.0')).toBeInTheDocument();
    // The already-active row's quiet notes (#476, #594).
    expect(scoped.getByTestId('playbook-version-active-note-v2.0.0')).toBeInTheDocument();
    expect(
      scoped.getByTestId('playbook-version-unapproved-active-note-v2.0.0'),
    ).toBeInTheDocument();
  });

  it('renders the row actions as one group, with Remove separated from the routine actions', async () => {
    stubRoutes();
    await renderPanel();

    const row = screen.getByTestId('playbook-row-synthetic-nda-sample');
    const group = within(row).getByTestId('playbook-row-actions-synthetic-nda-sample');
    expect(group).toHaveAttribute('role', 'group');

    // Routine actions live together...
    const routine = within(row).getByTestId('playbook-row-actions-main-synthetic-nda-sample');
    expect(routine).toContainElement(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
    expect(routine).toContainElement(screen.getByTestId('playbook-rename-synthetic-nda-sample'));

    // ...and the one-way door does NOT. Remove having its own grouping hook
    // is the assertion; "third in a vertical stack" is what it used to be.
    const remove = screen.getByTestId('playbook-remove-synthetic-nda-sample');
    expect(routine).not.toContainElement(remove);
    expect(group).toContainElement(remove);
    expect(
      within(row).getByTestId('playbook-row-actions-danger-synthetic-nda-sample'),
    ).toContainElement(remove);
  });
});

// ---------------------------------------------------------------------------
// The version identifier is DERIVED, never typed (issue #597)
// ---------------------------------------------------------------------------

/**
 * Issue #597, owner decision 2026-08-22 — option (a), derive only.
 *
 * The form used to ask the operator to type a version identifier. It is now
 * read out of the artifact: `identity.version` when the document carries one,
 * otherwise `<upload date>-<content_hash prefix>`. The field is never
 * editable — option (b), prefill-but-editable, was explicitly rejected.
 *
 * The derivation RULE itself is pinned as a pure function in
 * opf-identity-597.test.ts (including its UTC and collision behaviour). What
 * is asserted here is the part only the component can be wrong about: that
 * the derived value is what actually reaches the wire, and that no typeable
 * input is offered.
 */
describe('AdminPlaybooks — derived version identifier', () => {
  async function openCreate(): Promise<void> {
    fireEvent.click(screen.getByTestId('admin-playbooks-create-toggle'));
    await screen.findByTestId('admin-playbooks-create-panel');
  }

  it('offers no typeable version field at all', async () => {
    stubRoutes();
    await renderPanel();
    await openCreate();

    // The strongest form of "not operator-typed": there is no text input on
    // this panel that could hold a version. A readOnly input would still be
    // an input, and option (b) was rejected.
    const panel = screen.getByTestId('admin-playbooks-create-panel');
    expect(panel.querySelector('input[type="text"]')).toBeNull();
  });

  it("shows the artifact's own identity.version, read-only, before submit", async () => {
    stubRoutes();
    await renderPanel();
    await openCreate();

    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [opfFile({ id: 'synthetic-sample', version: '4.2.0', content_hash: FULL_HASH })] },
    });

    // Shown BEFORE submit, so the operator sees what they are about to create.
    const shown = await screen.findByTestId('admin-playbooks-create-version');
    expect(shown.textContent).toContain('4.2.0');
    expect(shown.tagName).not.toBe('INPUT');
  });

  it('derives an identifier for an artifact with NO identity.version, and sends that exact value', async () => {
    // The real production playbook (educational-affiliation) is this shape:
    // opf_version 0.3, content_hash present, identity.version absent. "Just
    // read it from the file" cannot be the whole rule, so the derived value
    // has to be what goes to the wire.
    //
    // The DATE half of the rule (UTC, format) is pinned in
    // opf-identity-597.test.ts, where the clock is an argument and the test
    // is not wall-clock-stamped. What this test owns is the half only the
    // component can get wrong: that the value reaching the wire is derived
    // FROM THE FILE the operator chose -- hence the assertion on the hash
    // prefix, which is the part carrying the file's identity.
    const derivedShape = /^\d{4}-\d{2}-\d{2}-ab12cd34$/;

    stubRoutes([
      {
        method: 'POST',
        suffix: '/api/admin/playbooks',
        status: 200,
        body: { playbook_id: 'educational-affiliation', version: 'whatever-the-server-says' },
      },
    ]);
    await renderPanel();
    await openCreate();

    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [opfFile({ id: 'educational-affiliation', content_hash: FULL_HASH })] },
    });

    // Shown before submit, so the operator sees what they are about to make.
    await waitFor(() => {
      expect(screen.getByTestId('admin-playbooks-create-version').textContent).toMatch(
        derivedShape,
      );
    });

    const shownBeforeSubmit = screen
      .getByTestId('admin-playbooks-create-version')
      .textContent?.trim();

    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));
    await waitFor(() => {
      expect(requestsMatching('POST', '/api/admin/playbooks')).toHaveLength(1);
    });

    const form = requestsMatching('POST', '/api/admin/playbooks')[0]!.body as Record<
      string,
      unknown
    >;
    expect(form.version).toMatch(derivedShape);
    // ...and it is exactly the value the operator was shown, not a second
    // derivation done at submit time that could differ from it.
    expect(form.version).toBe(shownBeforeSubmit);
  });

  it('refuses an artifact with no identity to derive from, and does not post', async () => {
    stubRoutes();
    await renderPanel();
    await openCreate();

    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [new File(['{"opf_version":"0.3"}'], 'no-identity.opf.json')] },
    });

    const error = await screen.findByTestId('admin-playbooks-create-error');
    expect(error.textContent).toContain('identity');
    // Never a silent fallback to an operator-typed or empty value.
    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));
    expect(requestsMatching('POST', '/api/admin/playbooks')).toHaveLength(0);
  });

  it('reads identity out of the .opf.html single-file bundle too', async () => {
    stubRoutes();
    await renderPanel();
    await openCreate();

    const json = JSON.stringify({
      opf_version: '0.3',
      identity: { id: 'synthetic-sample', version: '7.7.7', content_hash: FULL_HASH },
    });
    const html = `<!doctype html><html><body><script id="opf-canonical" type="application/json">${json.replace(
      /<\//g,
      '<\\/',
    )}</script></body></html>`;

    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [new File([html], 'playbook.opf.html', { type: 'text/html' })] },
    });

    const shown = await screen.findByTestId('admin-playbooks-create-version');
    expect(shown.textContent).toContain('7.7.7');
  });

  it('still surfaces the append-only refusal, naming the colliding identifier', async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/api/admin/playbooks',
        status: 409,
        body: { detail: "version '4.2.0' was already uploaded for this playbook" },
      },
    ]);
    await renderPanel();
    await openCreate();

    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [opfFile({ version: '4.2.0', content_hash: FULL_HASH })] },
    });
    await screen.findByTestId('admin-playbooks-create-version');
    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));

    const banner = await screen.findByTestId('admin-playbooks-create-error');
    expect(banner.textContent).toContain('4.2.0');
  });

  it('leaves the Note field exactly as it was — free text, optional', async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/api/admin/playbooks',
        status: 200,
        body: { playbook_id: 'educational-affiliation', version: '4.2.0' },
      },
    ]);
    await renderPanel();
    await openCreate();

    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [opfFile({ version: '4.2.0', content_hash: FULL_HASH })] },
    });
    await screen.findByTestId('admin-playbooks-create-version');

    // Optional: submitting with no note at all still works.
    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));
    await screen.findByTestId('admin-playbooks-create-success');
    expect(requestsMatching('PATCH', '/versions/4.2.0/notes')).toHaveLength(0);

    const notes = screen.getByTestId('admin-playbooks-create-notes');
    expect(notes.tagName).toBe('TEXTAREA');
    expect(notes).toBeEnabled();
  });
});

// ---------------------------------------------------------------------------
// Approve & activate as ONE audited act (issue #595)
// ---------------------------------------------------------------------------

/**
 * Issue #595. Approval is the precheck activation requires, so approving and
 * then activating were two clicks for one decision. They are now one.
 *
 * The governance line this must not cross: Gate 7 exists so that UPLOADING is
 * not the same as putting a version in front of a counterparty. Merging
 * approve → activate keeps that — it is still one explicit, audited operator
 * act naming an exact content hash, still strictly after upload. Merging
 * upload → approve would destroy it (`record_legal_approval`'s docstring:
 * an upload would then be self-approving), and nothing here does that. The
 * server-side gate in `activate_release_bundle` is untouched and remains the
 * authority; this is two existing client calls sequenced, not a new endpoint
 * that would give the gate a second place to drift.
 */
describe('AdminPlaybooks — approve & activate as one action', () => {
  it('drives BOTH endpoints, in order, with the row\'s own content_hash', async () => {
    stubRoutes();
    await renderWithVersions();

    // v1.0.0 is retired and unapproved: the pair collapses into one control,
    // so neither half is offered on its own any more.
    expect(screen.queryByTestId('playbook-version-approve-v1.0.0')).toBeNull();
    expect(screen.queryByTestId('playbook-version-activate-v1.0.0')).toBeNull();

    fireEvent.click(screen.getByTestId('playbook-version-approve-activate-v1.0.0'));

    await waitFor(() => {
      expect(requestsMatching('POST', '/versions/v1.0.0/activate')).toHaveLength(1);
    });

    // ORDER matters, not just occurrence: activating before the approval
    // lands is exactly the Gate-7 refusal this sequence exists to avoid.
    const approvalIndex = requests.findIndex(
      (r) => r.method === 'POST' && r.pathname.endsWith('/versions/v1.0.0/legal-approval'),
    );
    const activateIndex = requests.findIndex(
      (r) => r.method === 'POST' && r.pathname.endsWith('/versions/v1.0.0/activate'),
    );
    expect(approvalIndex).toBeGreaterThanOrEqual(0);
    expect(activateIndex).toBeGreaterThan(approvalIndex);

    // The approval names the EXACT bytes on this row — never free-text.
    expect(requests[approvalIndex]!.body).toEqual({ content_hash: FULL_HASH });
  });

  it('offers a plain Activate — and calls ONLY activate — for a version already approved but not active', async () => {
    // The rollback-to-a-previously-approved-version path: re-approving
    // bytes that already carry an approval is ceremony with no content.
    stubRoutes([
      {
        method: 'GET',
        suffix: '/synthetic-nda-sample/versions',
        status: 200,
        body: {
          versions: [{ ...TRAIL.versions[0], legal_approval_content_hash: FULL_HASH }],
        },
      },
    ]);
    await renderWithVersions();

    expect(screen.queryByTestId('playbook-version-approve-activate-v1.0.0')).toBeNull();
    fireEvent.click(screen.getByTestId('playbook-version-activate-v1.0.0'));

    await waitFor(() => {
      expect(requestsMatching('POST', '/versions/v1.0.0/activate')).toHaveLength(1);
    });
    expect(requestsMatching('POST', '/versions/v1.0.0/legal-approval')).toHaveLength(0);
  });

  it('surfaces a half-completed sequence plainly and never claims the version is live', async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/versions/v1.0.0/activate',
        status: 409,
        body: { detail: 'another activation is already in flight for this playbook' },
      },
    ]);
    await renderWithVersions();

    fireEvent.click(screen.getByTestId('playbook-version-approve-activate-v1.0.0'));

    const banner = await screen.findByTestId('admin-playbooks-action-error');
    // The server's own refusal, verbatim.
    expect(banner.textContent).toContain('another activation is already in flight');
    // ...and the truth about what DID land. The approval record stands —
    // that is correct and auditable — but the version is not live, and
    // swallowing either half would leave the operator guessing.
    expect(banner.textContent).toContain('approval was recorded');
    expect(banner.textContent).toContain('not live');
    expect(rendered()).not.toMatch(/HTTP\s*\d/i);

    // The approval really did go out; it is not hidden or pretended away.
    expect(requestsMatching('POST', '/versions/v1.0.0/legal-approval')).toHaveLength(1);
    // And the row still reads as what it is.
    expect(screen.getByTestId('playbook-version-status-v1.0.0').textContent).toContain('retired');
  });

  it('leaves a hashless row on the plain Activate path, so the server states the Gate-7 refusal', async () => {
    stubRoutes();
    await renderWithVersions();

    // v3.0.0 has no content_hash at all (a row written before issue #478):
    // there is nothing to name in an approval, so there is no merged action
    // to offer. Activate stays, and the backend's refusal is the answer.
    expect(screen.queryByTestId('playbook-version-approve-activate-v3.0.0')).toBeNull();
    expect(screen.getByTestId('playbook-version-activate-v3.0.0')).toBeEnabled();
  });
});

// ---------------------------------------------------------------------------
// Upload a new playbook (issue #485 — no operator-typed playbook_id; #596 — the
// operator-facing name of this action is "Upload new playbook", never "Create")
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — create playbook', () => {
  async function openCreate(): Promise<void> {
    fireEvent.click(screen.getByTestId('admin-playbooks-create-toggle'));
    await screen.findByTestId('admin-playbooks-create-panel');
  }

  it('has no playbook_id field, and posts the file and version to POST /api/admin/playbooks', async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/api/admin/playbooks',
        status: 200,
        body: { playbook_id: 'educational-affiliation', version: '1.0.0' },
      },
    ]);
    await renderPanel();
    await openCreate();

    // No playbook select/input anywhere in this panel.
    expect(screen.queryByTestId('admin-playbooks-create-playbook')).toBeNull();

    // Issue #597: the version is read out of the artifact, so the file has to
    // carry an identity block -- there is no field to type one into.
    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [opfFile({ version: '1.0.0', content_hash: FULL_HASH })] },
    });
    await screen.findByText('1.0.0');
    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));

    const success = await screen.findByTestId('admin-playbooks-create-success');
    // The derived identity, read back from the server's own response, is
    // what reaches the admin -- never a client-side guess.
    expect(success.textContent).toContain('educational-affiliation');

    const creates = requestsMatching('POST', '/api/admin/playbooks');
    expect(creates).toHaveLength(1);
    const form = creates[0]!.body as Record<string, unknown>;
    expect(form.version).toBe('1.0.0');
    expect(form.file).toBeInstanceOf(File);
    expect(form.playbook_id).toBeUndefined();

    // The newly-created (server-derived) playbook_id's trail is fetched,
    // not something already on the page.
    await waitFor(() => {
      expect(
        requestsMatching('GET', '/educational-affiliation/versions').length,
      ).toBeGreaterThan(0);
    });
  });

  it('sends a typed note as its own notes call against the derived playbook_id', async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/api/admin/playbooks',
        status: 200,
        body: { playbook_id: 'educational-affiliation', version: '1.0.0' },
      },
    ]);
    await renderPanel();
    await openCreate();

    fireEvent.change(screen.getByTestId('admin-playbooks-create-notes'), {
      target: { value: 'A brand-new agreement type.' },
    });
    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [opfFile({ version: '1.0.0', content_hash: FULL_HASH })] },
    });
    await screen.findByText('1.0.0');
    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));

    await screen.findByTestId('admin-playbooks-create-success');
    const notesCalls = requestsMatching('PATCH', '/educational-affiliation/versions/1.0.0/notes');
    expect(notesCalls).toHaveLength(1);
    expect(notesCalls[0]!.body).toEqual({ notes: 'A brand-new agreement type.' });
  });

  it('refuses to submit with no file chosen', async () => {
    stubRoutes();
    await renderPanel();
    await openCreate();

    // No file at all -- and since issue #597 that also means no version, so
    // the form has nothing to send either way.
    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));

    expect(await screen.findByTestId('admin-playbooks-create-error')).toBeInTheDocument();
    expect(requestsMatching('POST', '/api/admin/playbooks')).toHaveLength(0);
  });

  it("surfaces the server's own identity-conflict refusal verbatim", async () => {
    stubRoutes([
      {
        method: 'POST',
        suffix: '/api/admin/playbooks',
        status: 409,
        body: {
          detail:
            "This document's agreement_type already matches the registered playbook_id " +
            "'synthetic-generic'. Upload a new version onto that playbook_id instead.",
        },
      },
    ]);
    await renderPanel();
    await openCreate();

    fireEvent.change(screen.getByTestId('admin-playbooks-create-file'), {
      target: { files: [opfFile({ version: '1.0.0', content_hash: FULL_HASH })] },
    });
    await screen.findByText('1.0.0');
    fireEvent.click(screen.getByTestId('admin-playbooks-create-submit'));

    const banner = await screen.findByTestId('admin-playbooks-create-error');
    expect(banner.textContent).toContain('synthetic-generic');
    expect(screen.queryByTestId('admin-playbooks-create-success')).toBeNull();
  });

  /**
   * Issue #596. A playbook is authored in playbook-engine and UPLOADED here;
   * nothing about it is created in the toaster. The screen used to say
   * "Create playbook" on the toolbar action, the card heading and the submit,
   * which teaches an operator the wrong model of the product on the one
   * screen where they form it.
   *
   * Asserted on the ACCESSIBLE NAME, not a testid, because the testid is
   * exactly the thing that did not need to change — the operator-facing
   * string is.
   */
  it('names the new-playbook action "Upload new playbook" everywhere an operator reads it', async () => {
    stubRoutes();
    await renderPanel();

    // The panel is closed, so the toolbar action is the only control that can
    // carry this name yet.
    expect(screen.getByTestId('admin-playbooks-create-toggle')).toContainElement(
      screen.getByRole('button', { name: 'Upload new playbook' }),
    );

    await openCreate();

    // The card heading matches the action that opened it.
    expect(screen.getByTestId('admin-playbooks-create-panel').textContent).toContain(
      'Upload a new playbook',
    );
    // ...and so does the submit, so the operator is never told mid-form that
    // they are about to "create" something.
    expect(screen.getByTestId('admin-playbooks-create-submit').textContent).toContain(
      'Upload new playbook',
    );

    // No operator-visible string on this screen still calls the action
    // "Create playbook" / "Create a playbook".
    expect(rendered()).not.toContain('Create playbook');
    expect(rendered()).not.toContain('Create a playbook');
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
// Standing-instructions discoverability (issue #611)
// ---------------------------------------------------------------------------

/**
 * Issue #611, two regressions #605's merge introduced, found in its own
 * independent review.
 *
 * 1. Standing instructions became reachable ONLY through a button labelled
 *    "Version history" — and after #598 that button opens a modal, so the
 *    label was not merely imprecise, it pointed somewhere else entirely.
 * 2. #484's "one playbook installed: preselected and quiet" was lost;
 *    `selectedPlaybookId` started `null` unconditionally, so a
 *    single-playbook deployment had to click a mislabelled button to find
 *    its own standing instructions.
 *
 * The fix splits the two jobs that button was doing. Version history opens
 * the overlay and nothing else; a separate control chooses the playbook whose
 * instructions are shown. Note the auto-select is NARROWER than #484's
 * original, which defaulted to the FIRST playbook however many were
 * installed: with two or more, guessing would render one playbook's standing
 * guidance under a heading naming another, and standing instructions steer
 * every review run against that playbook.
 */
describe('AdminPlaybooks — standing instructions are discoverable (#611)', () => {
  it('names the control that opens the instructions pane after what it does', async () => {
    stubRoutes();
    await renderPanel();

    const control = screen.getByTestId('playbook-instructions-synthetic-nda-sample');
    expect(control.textContent).toContain('Standing instructions');
    // ...and it is NOT the version-history control wearing a second hat.
    expect(control).not.toBe(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
  });

  it('opens the instructions pane without opening the version-history overlay', async () => {
    stubRoutes();
    await renderPanel();

    fireEvent.click(screen.getByTestId('playbook-instructions-synthetic-nda-sample'));

    await screen.findByTestId('admin-instructions-panel');
    // Choosing whose guidance to edit must not throw a modal over the screen.
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('preselects the single installed playbook, so no click is needed to find its instructions', async () => {
    stubRoutes([
      {
        method: 'GET',
        suffix: '/api/playbooks',
        status: 200,
        body: { playbooks: [CATALOG.playbooks[0]] },
      },
    ]);
    render(<AdminPlaybooks />);

    // No interaction at all between render and this assertion.
    const panel = await screen.findByTestId('admin-instructions-panel');
    expect(panel).toHaveTextContent('Standing instructions — Synthetic NDA Sample');
  });

  it('does NOT auto-select when more than one playbook is installed', async () => {
    stubRoutes();
    await renderPanel();

    // Guessing here would put one playbook's standing guidance under a
    // heading naming another — and a Save from that state would write it
    // there. An explicit choice is the only safe default with two.
    expect(screen.queryByTestId('admin-instructions-panel')).toBeNull();
  });

  it('leaves the instructions selection alone when version history is opened for a different playbook', async () => {
    stubRoutes();
    await renderWithInstructions();

    fireEvent.click(screen.getByTestId('playbook-versions-other-agreement'));
    await screen.findByRole('dialog', { name: /Other Agreement/ });

    // The overlay shows one playbook's trail; the pane underneath still
    // belongs to the playbook that was explicitly chosen for it.
    expect(screen.getByTestId('admin-instructions-panel')).toHaveTextContent(
      'Standing instructions — Synthetic NDA Sample',
    );
  });
});

// ---------------------------------------------------------------------------
// Merged standing-instructions pane (issue #605)
// ---------------------------------------------------------------------------

describe('AdminPlaybooks — merged standing instructions (#605)', () => {
  it('renders no instructions pane until a playbook is selected', async () => {
    stubRoutes();
    await renderPanel();

    expect(screen.queryByTestId('admin-instructions-panel')).toBeNull();
  });

  it('choosing a playbook for its standing instructions drives the pane', async () => {
    stubRoutes();
    await renderWithInstructions();

    const panel = screen.getByTestId('admin-instructions-panel');
    expect(panel).toHaveTextContent('Standing instructions — Synthetic NDA Sample');
    expect(
      requests.some((r) => r.method === 'GET' && r.pathname.endsWith('/synthetic-nda-sample/instructions')),
    ).toBe(true);
  });

  it('switching the selected playbook swaps the instructions pane onto the new one', async () => {
    stubRoutes();
    await renderWithInstructions();

    fireEvent.click(screen.getByTestId('playbook-instructions-other-agreement'));

    await waitFor(() => {
      expect(screen.getByTestId('admin-instructions-panel')).toHaveTextContent(
        'Standing instructions — Other Agreement',
      );
    });
    expect(
      requests.some((r) => r.method === 'GET' && r.pathname.endsWith('/other-agreement/instructions')),
    ).toBe(true);
  });

  it('removing the selected playbook drops both the version-history and instructions panes', async () => {
    stubRoutes();
    await renderWithInstructions();
    fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
    await screen.findByTestId('playbook-version-row-v1.0.0');

    // Issue #598 put version history behind a modal, so close it before
    // touching the row underneath — clicking a control the overlay covers is
    // not something an operator can actually do, and a test that does it is
    // asserting about a state the product cannot reach.
    fireEvent.click(screen.getByTestId('admin-playbooks-versions-close'));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // Closing the overlay does NOT clear the instructions selection.
    expect(screen.getByTestId('admin-instructions-panel')).toBeInTheDocument();

    const removeButton = screen.getByTestId('playbook-remove-synthetic-nda-sample');
    fireEvent.click(removeButton);
    fireEvent.click(removeButton);

    await waitFor(() => {
      expect(screen.queryByTestId('admin-instructions-panel')).toBeNull();
    });
    expect(screen.queryByTestId('admin-playbooks-versions-panel')).toBeNull();
  });

  it('places the instructions pane after the version-history table, and the upload/create forms last', async () => {
    stubRoutes();
    await renderWithInstructions();
    fireEvent.click(screen.getByTestId('playbook-versions-synthetic-nda-sample'));
    await screen.findByTestId('playbook-version-row-v1.0.0');

    fireEvent.click(screen.getByTestId('admin-playbooks-create-toggle'));
    fireEvent.click(screen.getByTestId('admin-playbooks-upload-toggle'));
    await screen.findByTestId('admin-playbooks-create-panel');
    await screen.findByTestId('admin-playbooks-upload-panel');

    const order = [
      'admin-playbooks-table-panel',
      'admin-playbooks-versions-panel',
      'admin-instructions-panel',
      'admin-playbooks-create-panel',
      'admin-playbooks-upload-panel',
    ].map((testId) => screen.getByTestId(testId));

    for (let i = 1; i < order.length; i += 1) {
      // Each element's DOM position is after the previous one's.
      // eslint-disable-next-line no-bitwise
      expect(order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  /**
   * Carries over the coverage of the `AdminInstructions — stale-response
   * race` test that #605 deleted along with `selectedPlaybookIdRef`.
   *
   * That ref was the pane's own "am I still the selected playbook?" guard.
   * #605 removed it and relies instead on `AdminPlaybooks` mounting the
   * pane as `key={selectedPlaybookId}`, so a switch is a full remount and a
   * dead instance's late response is dropped by React rather than painted.
   * That reasoning is correct — but it rests entirely on one `key` prop
   * that no assertion covered, so dropping the `key` (an easy thing to do
   * in a later refactor) silently reintroduced the bug with the suite still
   * green. Verified fail-first: with `key` removed this test fails with
   * `expected 'A TEXT' to be 'B TEXT'`.
   *
   * The failure is not cosmetic. After the stale paint the heading still
   * reads playbook B while the textarea holds A's text and `current.version`
   * is A's — so the next Save POSTs A's standing instructions to B's route
   * under A's `expected_current_version`, writing one playbook's standing
   * guidance onto another. Standing instructions steer every review against
   * that playbook, which is the governance blast radius #605's own "case
   * against" section warned about.
   */
  it('drops a late instructions response from the playbook the admin switched away from', async () => {
    let resolveSample: ((value: Response) => void) | undefined;
    let resolveOther: ((value: Response) => void) | undefined;

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const pathname = new URL(
          typeof input === 'string' ? input : input.toString(),
          'http://localhost',
        ).pathname;
        const method = (init?.method ?? 'GET').toUpperCase();

        if (method === 'GET' && pathname === '/api/playbooks') {
          return { ok: true, status: 200, json: async () => CATALOG } as Response;
        }
        if (method === 'GET' && pathname.endsWith('/versions')) {
          return { ok: true, status: 200, json: async () => TRAIL } as Response;
        }
        // Both playbooks' instruction GETs are held open deliberately, so
        // this test controls the order in which they resolve.
        if (method === 'GET' && pathname.endsWith('/synthetic-nda-sample/instructions')) {
          return new Promise<Response>((resolve) => {
            resolveSample = resolve;
          });
        }
        if (method === 'GET' && pathname.endsWith('/other-agreement/instructions')) {
          return new Promise<Response>((resolve) => {
            resolveOther = resolve;
          });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    render(<AdminPlaybooks />);
    await screen.findByTestId('playbook-row-synthetic-nda-sample');

    // Select the sample — its instructions GET is in flight, held open.
    fireEvent.click(screen.getByTestId('playbook-instructions-synthetic-nda-sample'));
    await waitFor(() => expect(resolveSample).toBeDefined());

    // Switch to the other playbook — its GET is ALSO in flight now, so both
    // are outstanding at once: exactly the race the `key` remount guards.
    fireEvent.click(screen.getByTestId('playbook-instructions-other-agreement'));
    await waitFor(() => expect(resolveOther).toBeDefined());

    // The playbook actually on screen resolves FIRST.
    resolveOther!({
      ok: true,
      status: 200,
      json: async () => ({
        current: { version: 9, text: 'B TEXT', saved_by: 'other-admin', saved_at: 1_700_000_500 },
        history: [],
      }),
    } as Response);

    await waitFor(() => {
      expect((screen.getByTestId('admin-instructions-text') as HTMLTextAreaElement).value).toBe(
        'B TEXT',
      );
    });

    // The playbook the admin already switched AWAY from resolves LAST. It
    // must be discarded, not painted over the pane now showing the other.
    resolveSample!({
      ok: true,
      status: 200,
      json: async () => ({
        current: { version: 2, text: 'A TEXT', saved_by: 'sample-admin', saved_at: 1_700_000_100 },
        history: [],
      }),
    } as Response);
    // Give the discarded promise's microtasks a turn, then assert nothing moved.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect((screen.getByTestId('admin-instructions-text') as HTMLTextAreaElement).value).toBe(
      'B TEXT',
    );
    expect(screen.getByTestId('admin-instructions-status')).toHaveTextContent(/^v9 in effect/);
    expect(screen.getByTestId('admin-instructions-panel')).toHaveTextContent(
      'Standing instructions — Other Agreement',
    );
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
