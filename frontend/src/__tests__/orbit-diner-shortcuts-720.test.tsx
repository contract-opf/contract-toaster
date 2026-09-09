/**
 * orbit-diner-shortcuts-720.test.tsx — the keyboard vocabulary with the Orbit
 * Diner console mounted (issue #720, epic #729).
 *
 * `ReviewSubmission` returns the console at the top of its render (#719), so
 * the whole legacy tree below that early return never mounts — but the
 * `keydown` dispatcher is a hook ABOVE the return, so it stays live and is now
 * the ONLY dispatcher (`keyboardShortcuts={false}`, owner decision H6). Three
 * of its branches still addressed the tree that no longer renders. What this
 * file pins, one claim per way it was broken:
 *
 *   1. `?` and Cmd/Ctrl + / open the CONSOLE's cheat sheet. They used to flip
 *      `showShortcutsModal`, whose only consumer is the unrendered legacy
 *      modal — the state moved, nothing appeared, and the console's own
 *      printed list went on advertising the binding. Exactly one list, and it
 *      is the console's.
 *   2. NO CHORD `preventDefault()`s WITHOUT DOING ITS JOB. Cmd/Ctrl + Shift +
 *      P and + G swallowed the browser's own binding and then focused nothing
 *      when their control was absent. The lookup now runs first.
 *   3. A DIALOG OWNS ITS OWN KEYS. A chord fired inside one of the console's
 *      five dialogs would reach a control on the surface behind the scrim —
 *      focusing a dial the reader cannot see, or ejecting their file out from
 *      under an open receipt.
 *
 * Two of the guard tests build a `<dialog open>` BY HAND, outside the React
 * container. That is deliberate, not a shortcut: inside the console the kit's
 * own handler calls `stopPropagation` while a modal is up, so an event fired
 * there never reaches the window listener at all and would prove nothing about
 * the guard. A hand-built dialog is the one way to put a target inside
 * `dialog[open]` in front of the dispatcher and see what IT does — the layer
 * that has to hold if the kit's listener ever moves.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReviewSubmission from '../ReviewSubmission';
import { OrbitDiner } from '../orbit-diner/OrbitDiner';
import { toReviewModel, type ReviewProjectionState } from '../orbit-diner/projection';

const PLAYBOOKS = [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }];

function docxFile(name = 'contract.docx'): File {
  return new File(['x'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

function mockFetch() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const pathname = new URL(url, 'http://localhost').pathname;
    if (pathname === '/api/playbooks') {
      return {
        ok: true,
        status: 200,
        json: async () => ({ playbooks: PLAYBOOKS }),
      } as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  });
}

/** jsdom ships `<dialog>` without the modal methods; the console's overlay is
 *  a real `dialog` element and `dialog[open]` is the guard's own subject, so
 *  the two methods are filled in with the attribute toggling the spec
 *  defines. */
function stubDialogMethods(): void {
  const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
  if (typeof proto.showModal === 'function') return;
  proto.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  proto.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
}

function console_(): HTMLElement {
  const node = document.querySelector<HTMLElement>('.od-console');
  if (!node) throw new Error('the Orbit Diner console did not render');
  return node;
}

function overlay(): HTMLElement {
  const node = document.querySelector<HTMLElement>('dialog.od-dialog');
  if (!node) throw new Error('the console rendered no dialog');
  return node;
}

/** Render, wait for the catalog, and drop a .docx into the console's intake. */
async function loaded(): Promise<HTMLElement> {
  render(<ReviewSubmission />);
  const input = await screen.findByTestId('review-file-input');
  fireEvent.change(input, { target: { files: [docxFile()] } });
  await waitFor(() => expect(console_()).toHaveAttribute('data-status', 'loaded'));
  return console_();
}

/** A `<dialog open>` outside the React container, with something focusable in
 *  it — the only way to hand the window dispatcher a target inside an open
 *  dialog (see the file header). Removed by the afterEach below. */
function handBuiltDialog(): HTMLButtonElement {
  const dialog = document.createElement('dialog');
  dialog.setAttribute('open', '');
  dialog.setAttribute('data-testid', 'hand-built-dialog');
  const inner = document.createElement('button');
  dialog.appendChild(inner);
  document.body.appendChild(dialog);
  inner.focus();
  return inner;
}

beforeEach(() => {
  stubDialogMethods();
  // jsdom ships no `matchMedia`, and the kit reads it for forced colours.
  // That preference is not what this file is about, so the stub answers "no
  // preference" to every query.
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
  vi.stubGlobal('fetch', mockFetch());
});

afterEach(() => {
  document.querySelector('[data-testid="hand-built-dialog"]')?.remove();
  vi.unstubAllGlobals();
});

describe('issue #720 — the cheat sheet the bindings advertise is the one that opens', () => {
  it('? opens the console’s shortcut list, and only that one', async () => {
    await loaded();
    expect(overlay()).not.toHaveAttribute('data-modal');

    expect(fireEvent.keyDown(window, { key: '?' })).toBe(false);
    await waitFor(() => expect(overlay()).toHaveAttribute('data-modal', 'shortcuts'));

    // Exactly one visible list, and it is the console's: the legacy modal is
    // not merely closed, it is not in the document at all.
    expect(document.querySelectorAll('.od-shortcuts')).toHaveLength(1);
    expect(screen.queryByTestId('review-shortcuts-modal')).toBeNull();
  });

  it('Cmd/Ctrl + / opens the same one list', async () => {
    await loaded();
    expect(fireEvent.keyDown(window, { key: '/', metaKey: true })).toBe(false);
    await waitFor(() => expect(overlay()).toHaveAttribute('data-modal', 'shortcuts'));
    expect(document.querySelectorAll('.od-shortcuts')).toHaveLength(1);

    fireEvent.keyDown(window, { key: '/', ctrlKey: true });
    expect(document.querySelectorAll('.od-shortcuts')).toHaveLength(1);
  });

  it('does not swallow the keystroke when the console offers no cheat sheet', async () => {
    await loaded();
    screen.getByTestId('review-shortcuts-key').remove();
    // Nothing to open, so nothing is claimed: the browser keeps its keystroke.
    expect(fireEvent.keyDown(window, { key: '?' })).toBe(true);
    expect(overlay()).not.toHaveAttribute('data-modal');
  });
});

describe('issue #720 — no chord preventDefaults without doing its job', () => {
  it('Cmd/Ctrl + Shift + P focuses the console’s own dial', async () => {
    await loaded();
    expect(fireEvent.keyDown(window, { key: 'p', metaKey: true, shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(screen.getByTestId('review-playbook-dial'));
  });

  it('Cmd/Ctrl + Shift + G focuses the console’s own instructions pad', async () => {
    await loaded();
    expect(fireEvent.keyDown(window, { key: 'g', ctrlKey: true, shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(screen.getByTestId('review-guidance-input'));
  });

  it('releases the keystroke when those controls are absent', async () => {
    await loaded();
    screen.getByTestId('review-playbook-dial').remove();
    screen.getByTestId('review-guidance-field').remove();

    // Cmd/Ctrl + Shift + P is the command palette in more than one shell.
    // Swallowing it to focus nothing is the regression.
    expect(fireEvent.keyDown(window, { key: 'p', metaKey: true, shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(window, { key: 'g', metaKey: true, shiftKey: true })).toBe(true);
    // The single-key pair reaches the same two controls and answers the same
    // way.
    expect(fireEvent.keyDown(window, { key: 'p' })).toBe(true);
    expect(fireEvent.keyDown(window, { key: 'g' })).toBe(true);
  });
});

describe('issue #720 — a dialog owns its own keys', () => {
  it('a chord fired inside an open dialog reaches nothing behind it', async () => {
    await loaded();
    const inner = handBuiltDialog();

    // Cmd/Ctrl + Shift + P would focus the dial behind the scrim.
    expect(fireEvent.keyDown(inner, { key: 'p', metaKey: true, shiftKey: true })).toBe(true);
    expect(document.activeElement).toBe(inner);

    // Cmd/Ctrl + Shift + M would toggle appliance sound from inside a dialog
    // that says nothing about sound.
    const soundKey = document.querySelector<HTMLElement>('[aria-label^="App sounds"]');
    const before = soundKey?.getAttribute('aria-label');
    expect(before).toContain('App sounds on');
    expect(fireEvent.keyDown(inner, { key: 'm', metaKey: true, shiftKey: true })).toBe(true);
    expect(
      document.querySelector('[aria-label^="App sounds"]')?.getAttribute('aria-label'),
    ).toBe(before);
  });

  it('Escape inside a dialog does not eject the file', async () => {
    await loaded();
    const inner = handBuiltDialog();

    expect(fireEvent.keyDown(inner, { key: 'Escape' })).toBe(true);
    // The document is still loaded: Escape belonged to the dialog.
    expect(console_()).toHaveAttribute('data-status', 'loaded');
  });

  it('Escape inside the console’s own dialog closes it and keeps the file', async () => {
    await loaded();
    fireEvent.keyDown(window, { key: '?' });
    await waitFor(() => expect(overlay()).toHaveAttribute('data-modal', 'shortcuts'));

    fireEvent.keyDown(overlay(), { key: 'Escape' });
    await waitFor(() => expect(overlay()).not.toHaveAttribute('data-modal'));
    expect(console_()).toHaveAttribute('data-status', 'loaded');
  });

  it('typing into the instructions pad is typing, not a shortcut', async () => {
    await loaded();
    const pad = screen.getByTestId('review-guidance-input');
    pad.focus();
    const soundKey = document.querySelector<HTMLElement>('[aria-label^="App sounds"]');
    const before = soundKey?.getAttribute('aria-label');

    expect(fireEvent.keyDown(pad, { key: 'm' })).toBe(true);
    expect(
      document.querySelector('[aria-label^="App sounds"]')?.getAttribute('aria-label'),
    ).toBe(before);
  });
});

const baseState = (over: Partial<ReviewProjectionState> = {}): ReviewProjectionState => ({
  file: null,
  submitting: false,
  reviewId: null,
  detail: null,
  playbookId: 'nda',
  browning: 'medium',
  notesMode: 'external',
  toasterGuidance: '',
  appliedGuidance: null,
  dispositionNote: '',
  playbooks: PLAYBOOKS,
  notesModeInternalAvailable: false,
  muted: false,
  notificationsSupported: true,
  ...over,
});

describe('issue #720 — H6 defence in depth: the kit’s default is OFF', () => {
  it('a mount that forgets the prop dispatches nothing of its own', () => {
    render(
      <OrbitDiner
        model={toReviewModel(baseState())}
        onFile={() => {}}
        onPreferences={() => {}}
        onAction={() => {}}
      />,
    );
    const node = console_();
    expect(overlay()).not.toHaveAttribute('data-modal');

    // The kit's own Cmd/Ctrl + Shift + P opens ITS playbook dialog. With the
    // default off it must do nothing: in this app the host panel owns the
    // vocabulary, and a forgotten prop must be inert rather than a second
    // dispatcher firing every chord twice.
    fireEvent.keyDown(node, { key: 'p', ctrlKey: true, shiftKey: true });
    fireEvent.keyDown(node, { key: 'P', metaKey: true, shiftKey: true });
    expect(overlay()).not.toHaveAttribute('data-modal');
  });
});
