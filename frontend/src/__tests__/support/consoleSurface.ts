/**
 * consoleSurface.ts — the one place that knows WHERE the Review tab keeps a
 * control, so a workflow test can assert the behaviour without also hardcoding
 * the markup it happens to sit in today (issue #733, epic #729).
 *
 * ## Why this exists
 *
 * Most of what these tests assert is workflow, not chrome: a half-pull must
 * not POST, a download must go through an anchor, a recorded disposition must
 * survive. What moves is placement — the console keeps a finished review's
 * facts in an overlay rather than on the page — and a test that hardcodes a
 * placement fails for a reason that has nothing to do with what it is testing.
 *
 * So the placement lives here, once, and the tests ask for the control.
 *
 * ## One surface (issues #727, #728)
 *
 * This module used to answer for TWO. A `CONSOLE_ON` constant mirrored the
 * build-time Orbit Diner switch and every resolver below branched on it,
 * because the gate had to prove the suite green with the console off as well
 * as on (#733). #727 deleted the surface the "off" side described —
 * `Toaster.tsx`, its hero, its dial, its controls and its `.ct-review-console`
 * grid — so there is no longer a second placement to resolve, and a branch
 * whose "off" arm queries deleted DOM is a test that can only fail. The
 * branches went with the markup; the resolvers stayed, because WHERE a
 * control lives is still worth saying once.
 *
 * #728 then deleted the switch itself (`orbit-diner/flag.ts`), the env override
 * that forced it on for a run, and the per-file mocks that forced it on for a
 * file. The console is the Review tab unconditionally, so there is one
 * placement and one way to run the suite.
 *
 * ## What does NOT belong here
 *
 * Surface-specific mechanics. This module resolves WHERE something is, never
 * WHAT it does.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { expect } from 'vitest';

/**
 * The catalog a test needs in `GET /api/playbooks` for a submission to be
 * possible at all.
 *
 * The console's `canSubmit` will not arm the lever until an ACTIVE playbook is
 * selected. A fetch stub that answers 404 here is therefore an incomplete
 * fixture rather than a scenario — the app never runs without a catalog — so
 * tests about something else route this and move on.
 */
export const DEFAULT_PLAYBOOKS = {
  playbooks: [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }],
};

/**
 * Press the go control once the surface will actually act on it.
 *
 * The console arms its lever from `canSubmit` — a file AND an active playbook
 * — and the catalog arrives from a fetch, so a press issued in the same tick
 * as the file change lands on a disabled lever and silently does nothing.
 * Waiting for the control to be armed is the honest wait: it is the same thing
 * a person does.
 */
export async function pressSubmit(): Promise<HTMLElement> {
  const button = await screen.findByTestId('review-submit-button');
  await waitFor(() => {
    if (button.getAttribute('aria-disabled') === 'true' || (button as HTMLButtonElement).disabled) {
      throw new Error('the submit control is not armed yet');
    }
  });
  fireEvent.click(button);
  return button;
}

/**
 * Whether the go control will act if pressed.
 *
 * The lever is a real button carrying `aria-disabled`, so it stays focusable
 * and can explain its refusal instead of disappearing from the tab order.
 * `disabled` is checked too, so a control that refuses either way reads as
 * "pressing this does nothing".
 */
export function submitArmed(): boolean {
  const button = screen.getByTestId('review-submit-button');
  return (
    !(button as HTMLButtonElement).disabled &&
    button.getAttribute('aria-disabled') !== 'true'
  );
}

/**
 * The completed review's facts.
 *
 * `review-result` sits behind whichever overlay owns the finished review — the
 * receipt printer once there is a redline to print, the "Review details" key
 * otherwise. A terminal ERROR or a manual-review outcome stays on the page,
 * because that is the one result nobody should have to open something to read;
 * the page yields it while the record or receipt overlay is open, so there is
 * never more than one of it.
 */
export async function findReviewResult(
  options?: { timeout?: number }
): Promise<HTMLElement> {
  return waitFor(() => {
    // Singular, deliberately: the finished result has exactly one render site
    // at a time — on the page for an ERROR or manual-review outcome, and
    // inside whichever overlay owns it otherwise — so finding two of them is a
    // duplicate-testid regression this helper must report, not absorb.
    const onPage = screen.queryByTestId('review-result');
    if (onPage) return onPage;
    // Open nothing until the review has actually ended. "Review details" is
    // offered from the moment a review exists, so clicking it mid-run opens an
    // overlay that then duplicates the result the page is about to render.
    if (document.querySelector('.od-console')?.getAttribute('data-terminal') !== 'true') {
      throw new Error('the review has not finished yet');
    }
    const details = screen.queryByRole('button', { name: /review details/i });
    if (details) fireEvent.click(details);
    else {
      const receipt = screen.queryByRole('button', {
        name: /view review receipt/i,
      }) as HTMLButtonElement | null;
      if (receipt && !receipt.disabled) fireEvent.click(receipt);
    }
    throw new Error('the finished review is not on screen yet');
  }, options);
}

/**
 * The review's own record — the id-copy control and the review's lineage. It
 * lives in the record overlay behind "Review details", or in the receipt once
 * there is a redline to print.
 */
export async function openReviewRecord(): Promise<HTMLElement> {
  return waitFor(() => {
    // Singular, for the same reason as `findReviewResult`: the record lives in
    // one overlay at a time, so two of them would be a regression.
    const row = screen.queryByTestId('review-id-row');
    if (row) return row;
    const details = screen.queryByRole('button', { name: /review details/i });
    if (details) fireEvent.click(details);
    else {
      const receipt = screen.queryByRole('button', {
        name: /view review receipt/i,
      }) as HTMLButtonElement | null;
      if (receipt && !receipt.disabled) fireEvent.click(receipt);
    }
    throw new Error('the review record is not open yet');
  });
}

/** The receipt itself (`review-receipt-text`), printed from the register. */
export async function openReceipt(): Promise<HTMLElement> {
  return waitFor(() => {
    const [printed] = screen.queryAllByTestId('review-receipt-text');
    if (printed) return printed;
    const printer = screen.queryByRole('button', {
      name: /view review receipt/i,
    }) as HTMLButtonElement | null;
    if (printer && !printer.disabled) fireEvent.click(printer);
    throw new Error('the receipt is not printed yet');
  });
}

/**
 * The disposition controls, in their own overlay behind the till's "Record
 * outcome" key. Returns the region that holds the three choices.
 */
export async function openDisposition(): Promise<HTMLElement> {
  return waitFor(() => {
    const [inline] = screen.queryAllByTestId('review-disposition');
    if (inline) return inline;
    const key = screen.queryByRole('button', { name: /record outcome/i });
    if (key) fireEvent.click(key);
    throw new Error('the disposition overlay is not open yet');
  });
}

/**
 * The cover-note card. Pressing "Butter it" is what produces it, and that same
 * press opens the overlay it lives in.
 */
export async function butterIt(): Promise<void> {
  fireEvent.click(await screen.findByTestId('review-cover-note-butter'));
}

/**
 * The working progress indicator, as a role rather than as a testid: the
 * console keeps one bar (`toaster-state-progress`) that reports a step once it
 * knows one, and `aria-valuenow` is the fact it expresses, so that is what the
 * tests read (issue #733).
 */
export function progressBar(): HTMLElement | null {
  const bars = screen.queryAllByRole('progressbar');
  return bars[0] ?? null;
}

/** The step the progress indicator claims, or null when it claims none. */
export function progressStep(): number | null {
  const now = progressBar()?.getAttribute('aria-valuenow');
  return now == null || now === '' ? null : Number(now);
}

/**
 * "No review is attached to this panel" (issue #733).
 *
 * The console's status lamp is part of the appliance and is always there, so
 * it says this by reporting a pre-review status rather than by rendering
 * nothing. The claim is that nothing was reattached, not that a particular
 * element is missing.
 */
export function expectNoReviewAttached(): void {
  const status = document.querySelector('.od-console')?.getAttribute('data-status');
  expect(['empty', 'loaded']).toContain(status);
  expect(screen.queryByTestId('review-id-row')).toBeNull();
}

/**
 * The toaster's state badge, by what it MEANS rather than by what it looks
 * like (issue #733).
 *
 * The deleted hero named three appearances — `-progress`, `-done`, `-sober`
 * (burnt). The console names the STATUS instead (`-empty`, `-loaded`,
 * `-running`, `-done`, `-error`, `-cancelled`, `-manual_review_required`),
 * which is why `-sober` needs the status to resolve: one appearance covered
 * every way a review can end badly, and the console distinguishes them.
 * `-progress` and `-done` survive as themselves.
 */
export function stateBadge(
  kind: 'progress' | 'done' | 'sober',
  status = 'ERROR'
): string {
  if (kind !== 'sober') return `toaster-state-${kind}`;
  return `toaster-state-${status.toLowerCase()}`;
}

/** "Nothing is active, and here is who fixes it" (issue #733). */
export const NO_PLAYBOOKS_COPY =
  /no playbook is active\. an admin needs to activate one/i;

/** One entry of the playbook control, however the surface expresses it. */
export interface PlaybookStop {
  id: string;
  label: string;
  selected: boolean;
  selectable: boolean;
}

/**
 * Read the playbook control.
 *
 * It is a native `<select>` whose keyboard contract is the platform's, plus a
 * searchable catalog dialog. What it must say is which playbooks exist, which
 * one is chosen, and which cannot be chosen; this returns that, and the ARIA
 * mechanics stay asserted against the control itself.
 */
export function playbookStops(): PlaybookStop[] {
  const control = screen.queryByTestId('review-playbook-dial');
  if (!control) return [];
  const select = control as HTMLSelectElement;
  return Array.from(select.options)
    .filter((option) => option.value)
    .map((option) => ({
      id: option.value,
      label: option.textContent ?? '',
      selected: option.value === select.value,
      selectable: !option.disabled,
    }));
}

/** One stop by id, or undefined when the control does not offer it. */
export function playbookStop(id: string): PlaybookStop | undefined {
  return playbookStops().find((stop) => stop.id === id);
}

/** The playbook control's current value, however the surface expresses it. */
export function selectedPlaybookId(): string | null {
  return playbookStops().find((stop) => stop.selected)?.id ?? null;
}

/** Choose a playbook by id, through the dial the console offers. */
export async function choosePlaybook(playbookId: string): Promise<void> {
  fireEvent.change(await screen.findByTestId('review-playbook-dial'), {
    target: { value: playbookId },
  });
}

/** The searchable playbook catalog, behind "Browse playbooks". */
export async function openPlaybookCatalog(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole('button', { name: /browse playbooks/i }));
  return screen.findByRole('dialog');
}
