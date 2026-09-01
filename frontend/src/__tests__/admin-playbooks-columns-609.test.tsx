/**
 * admin-playbooks-columns-609.test.tsx — issue #609 (the AdminPlaybooks
 * split of #602): apply the `ct-columns` primitive (#601) to this panel now
 * that the concurrent screen rework it was blocked on has landed.
 *
 * ## What this file can and cannot assert
 *
 * jsdom implements no layout and `vitest.config.ts` sets `css: false`, so —
 * exactly as layout-audit.mjs's docstring and admin-retention-columns-602's
 * both say — nothing here observes a real reflow. What IS a DOM fact
 * independent of any stylesheet is WIRING: which elements share a
 * `ct-columns` ancestor, which fields carry `narrow`, and what order the
 * controls sit in when the grid collapses to one column. That is what is
 * asserted, and the same convention #602 established.
 *
 * ## Why only two pairs
 *
 * `ct-columns.ts`'s own docstring: do not use it for "a single logical
 * control (nothing to pair it with — `ct-stack` alone is correct)". After
 * #596/#594/#595/#597/#598 this screen's control inventory is:
 *
 *   toolbar                  2 buttons                       — not fields
 *   playbooks table row      1 rename field + 5 buttons       — one field
 *   version-history overlay  1 note field per version + buttons — one field
 *   upload a NEW playbook    file drop, derived identifier, note
 *   upload a VERSION         playbook select, identifier, file drop, note
 *
 * Only the last two panels hold more than one field at a time, so only they
 * have anything to pair. The rename field and the per-version note field are
 * each alone in a table cell; forcing a grid around a lone control would be
 * exactly the misuse the primitive's docstring names.
 *
 * The standing-instructions pane is NOT in scope here — it is panel 5 of
 * #602 (`AdminInstructions.tsx`), whose own ticket notes its textarea is
 * legitimately wide.
 *
 * The derived version identifier on the "new playbook" form is deliberately
 * NOT marked `narrow`: it renders as an `<output>`, which is inline and
 * therefore never stretched by `ct-field`'s `align-items: stretch` in the
 * first place. `ct-field[narrow]`'s rule only targets
 * `:is(input, select, textarea)` (ct-field.css, guarded by layout-audit
 * check 5), so `narrow` there would be inert decoration asserting nothing.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import AdminPlaybooks from '../AdminPlaybooks';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

const CATALOG = {
  playbooks: [
    {
      playbook_id: 'synthetic-nda-sample',
      display_name: 'Synthetic NDA Sample',
      status: 'active',
      notes: '',
    },
  ],
};

function stubRoutes(): void {
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
        return { ok: true, status: 200, json: async () => ({ versions: [] }) } as Response;
      }
      if (method === 'GET' && pathname.endsWith('/instructions')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ current: null, history: [] }),
        } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
}

async function openUploadVersion(): Promise<HTMLElement> {
  render(<AdminPlaybooks />);
  await screen.findByTestId('playbook-row-synthetic-nda-sample');
  fireEvent.click(screen.getByTestId('admin-playbooks-upload-toggle'));
  return screen.findByTestId('admin-playbooks-upload-panel');
}

async function openNewPlaybook(): Promise<HTMLElement> {
  render(<AdminPlaybooks />);
  await screen.findByTestId('playbook-row-synthetic-nda-sample');
  fireEvent.click(screen.getByTestId('admin-playbooks-create-toggle'));
  return screen.findByTestId('admin-playbooks-create-panel');
}

/** Control ids in DOM order — which IS what reading and tab order follow,
 * and what `ct-columns` leaves untouched by construction (it repositions
 * children visually only; see ct-columns.ts's docstring). */
function controlOrder(panel: HTMLElement): string[] {
  return Array.from(panel.querySelectorAll('input,select,textarea')).map((el) => el.id);
}

beforeEach(() => {
  vi.unstubAllGlobals();
  stubRoutes();
});

describe('AdminPlaybooks / #609 — "upload a version" pairs its two short fields', () => {
  it('the playbook select and the version identifier share the SAME ct-columns host', async () => {
    await openUploadVersion();

    const selectField = screen.getByTestId('admin-playbooks-upload-playbook').closest('ct-field');
    const versionField = screen.getByTestId('admin-playbooks-upload-version').closest('ct-field');

    expect(selectField).not.toBeNull();
    expect(versionField).not.toBeNull();
    expect(selectField?.parentElement?.tagName.toLowerCase()).toBe('ct-columns');
    expect(selectField?.parentElement).toBe(versionField?.parentElement);
  });

  it('the version identifier opts out of the full-bleed stretch via `narrow`', async () => {
    await openUploadVersion();

    const versionField = screen.getByTestId('admin-playbooks-upload-version').closest('ct-field');
    expect(versionField?.hasAttribute('narrow')).toBe(true);

    // The playbook select is the half of the pair that SHOULD keep filling
    // its column: a display name is arbitrarily long, and clipping it to its
    // intrinsic width would be a new defect, not a fix.
    const selectField = screen.getByTestId('admin-playbooks-upload-playbook').closest('ct-field');
    expect(selectField?.hasAttribute('narrow')).toBe(false);
  });

  it('collapsed reading order is unchanged: playbook, then version, then the note', async () => {
    const panel = await openUploadVersion();
    const order = controlOrder(panel);

    const playbook = order.indexOf(
      screen.getByTestId('admin-playbooks-upload-playbook').id,
    );
    const version = order.indexOf(screen.getByTestId('admin-playbooks-upload-version').id);
    const note = order.indexOf(screen.getByTestId('admin-playbooks-upload-notes').id);

    expect(playbook).toBeGreaterThanOrEqual(0);
    expect(playbook).toBeLessThan(version);
    expect(version).toBeLessThan(note);
  });

  it('the wide controls stay out of the grid — the file drop and the note are not paired', async () => {
    await openUploadVersion();

    // A drop well and a free-text note are both legitimately full width.
    // Pairing them would halve two controls that need the room, which is the
    // opposite of what #602 asked for.
    const fileHost = screen.getByTestId('admin-playbooks-upload-file').closest('ct-file-drop');
    const noteField = screen.getByTestId('admin-playbooks-upload-notes').closest('ct-field');
    expect(fileHost?.parentElement?.tagName.toLowerCase()).not.toBe('ct-columns');
    expect(noteField?.parentElement?.tagName.toLowerCase()).not.toBe('ct-columns');
  });
});

describe('AdminPlaybooks / #609 — "upload a new playbook" pairs the artifact with what it derives to', () => {
  it('the file drop and the derived identifier share the SAME ct-columns host', async () => {
    await openNewPlaybook();

    // These two ARE one thought since #597: choose the artifact, and read
    // back the version identifier that was extracted from it. Side by side
    // they say that; stacked full-width they read as two unrelated steps.
    const fileHost = screen.getByTestId('admin-playbooks-create-file').closest('ct-file-drop');
    const versionField = screen.getByTestId('admin-playbooks-create-version').closest('ct-field');

    expect(fileHost).not.toBeNull();
    expect(versionField).not.toBeNull();
    expect(fileHost?.parentElement?.tagName.toLowerCase()).toBe('ct-columns');
    expect(fileHost?.parentElement).toBe(versionField?.parentElement);
  });

  it('collapsed reading order puts the file before the identifier it produces, and the note last', async () => {
    const panel = await openNewPlaybook();

    // The identifier is an <output>, not a form control, so this one is
    // asserted on document position rather than control order.
    const file = screen.getByTestId('admin-playbooks-create-file');
    const version = screen.getByTestId('admin-playbooks-create-version');
    const note = screen.getByTestId('admin-playbooks-create-notes');

    // eslint-disable-next-line no-bitwise
    expect(file.compareDocumentPosition(version) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // eslint-disable-next-line no-bitwise
    expect(version.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(panel).toContainElement(note);
  });

  it('the note stays full width, out of the grid', async () => {
    await openNewPlaybook();
    const noteField = screen.getByTestId('admin-playbooks-create-notes').closest('ct-field');
    expect(noteField?.parentElement?.tagName.toLowerCase()).not.toBe('ct-columns');
  });
});

describe('AdminPlaybooks / #609 — no control lost in the regrouping', () => {
  it('every control on the "upload a version" form is still present', async () => {
    await openUploadVersion();
    for (const testid of [
      'admin-playbooks-upload-playbook',
      'admin-playbooks-upload-version',
      'admin-playbooks-upload-file',
      'admin-playbooks-upload-notes',
      'admin-playbooks-upload-submit',
    ]) {
      expect(screen.getByTestId(testid)).toBeInTheDocument();
    }
  });

  it('every control on the "upload a new playbook" form is still present', async () => {
    await openNewPlaybook();
    for (const testid of [
      'admin-playbooks-create-identity-note',
      'admin-playbooks-create-file',
      'admin-playbooks-create-version',
      'admin-playbooks-create-notes',
      'admin-playbooks-create-submit',
    ]) {
      expect(screen.getByTestId(testid)).toBeInTheDocument();
    }
  });
});
