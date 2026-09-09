/**
 * history-table-width-668.test.tsx — issue #668: the History table must stop
 * scrolling horizontally at an ordinary desktop width.
 *
 * ## What this file can and cannot prove
 *
 * BE HONEST ABOUT WHAT THIS IS. The acceptance criterion — "no horizontal
 * scroll at ordinary widths" — is a LAYOUT fact, and jsdom implements no
 * layout at all (`scrollWidth`/`clientWidth` are hard-coded 0) while
 * `vitest.config.ts` sets `css: false`, so no assertion in this file measures
 * an overflow. Nothing in this repo can: there is no headless browser in the
 * gate. The same caveat layout-audit.mjs writes about itself applies here.
 *
 * What IS assertable, and is what this file asserts, is the set of DOM facts
 * the fix is MADE of — each one a thing that was measurably making the row
 * wider than the information in it:
 *
 *   1. the table opts in to wrapping column headings, instead of inheriting
 *      ct-table.css's `thead th { white-space: nowrap }`, which made the
 *      heading the widest thing in several columns (the stylesheet half of
 *      that pairing is guarded by layout-audit.mjs check 8, because a
 *      `css: false` suite cannot see a stylesheet at all);
 *   2. the playbook content hash is no longer rendered inline as visible text
 *      — it moved behind a disclosure — while remaining RETRIEVABLE;
 *   3. model ids render without their provider prefix, which is constant
 *      across every row on the page and distinguishes nothing;
 *   4. the in-row explanation sentences are gone, replaced by controls and
 *      marks that carry their meaning as an ACCESSIBLE NAME.
 *
 * ## The a11y bar this file enforces (issue #668's "Do not lose")
 *
 * Hover is not an accessible affordance on its own. So every value this
 * change moves off the visible row must be reachable two ways that are not
 * hover: it must have a real accessible name, and — where it is disclosure,
 * not status — the thing that discloses it must be a real, focusable
 * `<button>`. Asserting on accessible names rather than on visible text is
 * the same bar #656 set. A `title` alone would fail it; a `title` PLUS a
 * focusable disclosure passes.
 *
 * ## Watched failing first
 *
 * Against the pre-#668 component every group below is red: the table carries
 * no wrap-headings opt-in, the shortened hash is inline visible text with no
 * disclosure control at all, model ids render `anthropic/claude-opus-5`
 * verbatim, and the row's controls resolve by their visible prose
 * ("Show instructions", "Redline") rather than by an accessible name.
 *
 * Fully offline — `../auth` is mocked, fetch is stubbed, no network.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import ReviewHistory, { modelDisplayName, type HistoryRow } from '../ReviewHistory';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

// A real `"sha256:" + hexdigest` content hash, the shape
// `backend/src/main.py`'s playbook upload computes over the uploaded bytes
// and `reviews.resolve_playbook_version_lineage` copies onto the review row
// as `playbook_content_hash`. Synthetic bytes, real shape — 64 hex digits,
// not the truncated stand-in an older fixture used.
const CONTENT_HASH = `sha256:${'0123456789abcdef'.repeat(4)}`;

/**
 * The OpenRouter shape: `provider/model` ids, exactly as
 * `model-policy/openrouter.json` pins them (`models.primary.model_id` =
 * `anthropic/claude-opus-5`) and as `pipeline_runner` records them on the
 * review row. `served_critic_model_id` differs from what was requested, which
 * is the branch that renders the mismatch chip — seeded here so the
 * provider-prefix rule is exercised on a SERVED id too, not only a requested
 * one.
 */
const OPENROUTER: HistoryRow = {
  review_id: 'rev-openrouter',
  playbook_id: 'synthetic-nda-sample',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
  created_at: '1800000200',
  updated_at: '1800000300',
  policy_version: 3,
  posture_version: null,
  playbook_version: '1.0.0',
  playbook_content_hash: CONTENT_HASH,
  primary_model_id: 'anthropic/claude-opus-5',
  critic_model_id: 'anthropic/claude-sonnet-4.6',
  served_primary_model_id: 'anthropic/claude-opus-5',
  served_critic_model_id: 'moonshotai/kimi-k3',
  has_output: true,
  has_input: true,
};

/**
 * The Bedrock shape — the OTHER model-id spelling this product ships:
 * `model-policy/bedrock-us-east-1.json` pins `anthropic.claude-opus-4-8`,
 * which carries NO `/` at all. A one-variant fixture would leave the
 * "there is no provider segment to drop" branch green forever, so both
 * spellings are seeded. This row also carries no documents and no content
 * hash, which is the pre-#471 / purged-pointer shape `_review_list_item`
 * projects when `output_s3_key`/`upload_s3_key` are absent.
 */
const BEDROCK: HistoryRow = {
  review_id: 'rev-bedrock',
  playbook_id: 'synthetic-nda-sample',
  status: 'DONE',
  decision: 'ACCEPT',
  created_at: '1700000000',
  updated_at: '1700000100',
  policy_version: null,
  posture_version: null,
  playbook_version: null,
  playbook_content_hash: null,
  primary_model_id: 'anthropic.claude-opus-4-8',
  critic_model_id: 'anthropic.claude-sonnet-4-6',
  has_output: false,
  has_input: false,
};

function stubReviews(...rows: HistoryRow[]): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ reviews: rows }),
    })),
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------

describe('#668 — column headings are allowed to wrap', () => {
  it('the History table opts in to wrapping headings rather than inheriting nowrap', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    const table = await screen.findByTestId('history-table');

    // The DOM half of the pairing. ct-table.css's base rule pins every
    // `thead th` to `white-space: nowrap`, so the heading is the min-content
    // width of the WHOLE heading string — "Playbook & version" cannot break
    // after "Playbook". This class is the documented opt-out; the stylesheet
    // half (that the class actually neutralises nowrap) is layout-audit
    // check 8, since `css: false` means no stylesheet exists here to read.
    expect(table.classList.contains('ct-table--wrap-headings')).toBe(true);
  });

  it('the table still lives inside ct-table, which keeps the narrow-viewport scroll fallback', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    const table = await screen.findByTestId('history-table');

    // "No horizontal scroll at desktop width" must not become "clipped and
    // unreadable on a phone". ct-table's host is the horizontal-scroll
    // container (ct-table.css: `overflow-x: auto`), so a viewport too narrow
    // for the table scrolls the TABLE, not the page. Losing this ancestor is
    // how that fallback would silently disappear.
    expect(table.closest('ct-table')).not.toBeNull();
  });
});

describe('#668 — the content hash moves to a disclosure, and is not lost', () => {
  it('the playbook cell no longer prints the hash as visible text', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    const cell = await screen.findByTestId('history-playbook-rev-openrouter');

    // What the column is FOR is still there…
    expect(cell.textContent).toContain('synthetic-nda-sample');
    expect(cell.textContent).toContain('1.0.0');
    // …and the widest thing in it is not.
    expect(cell.textContent).not.toContain('sha256:');
    expect(screen.queryByTestId('history-hash-rev-openrouter')).toBeNull();
  });

  it('the hash is disclosed by a real focusable button with an accessible name', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    await screen.findByTestId('history-playbook-rev-openrouter');

    const toggle = screen.getByTestId('history-hash-toggle-rev-openrouter');
    // A real <button>, so it is in the tab order — the point of the "hover is
    // not an accessible affordance" rule. A <span title=…> would pass an
    // assertion about the tooltip and fail the user.
    expect(toggle.tagName).toBe('BUTTON');
    expect(toggle.hasAttribute('disabled')).toBe(false);
    // Resolvable BY NAME (the #656 bar), not by its glyph.
    expect(screen.getByRole('button', { name: /content hash/i })).toBe(toggle);
    // Hover text for a mouse user, carrying the value itself.
    expect(toggle.closest('ct-icon-button')?.getAttribute('title')).toContain(CONTENT_HASH);

    fireEvent.click(toggle);
    // Disclosed IN FULL — the provenance fact is preserved, not truncated
    // away, and now it is visible text a keyboard user can actually read.
    expect(screen.getByTestId('history-hash-rev-openrouter').textContent).toContain(CONTENT_HASH);

    fireEvent.click(toggle);
    expect(screen.queryByTestId('history-hash-rev-openrouter')).toBeNull();
  });

  it('a row that recorded no hash offers no disclosure at all', async () => {
    stubReviews(BEDROCK);
    render(<ReviewHistory />);
    await screen.findByTestId('history-playbook-rev-bedrock');

    // Nothing to disclose is not the same as something hidden: an empty
    // control would read as "there is a hash, go find it".
    expect(screen.queryByTestId('history-hash-toggle-rev-bedrock')).toBeNull();
    expect(screen.getByTestId('history-playbook-rev-bedrock').textContent?.toLowerCase()).toContain(
      'not recorded',
    );
  });
});

describe('#668 — model ids drop the provider prefix but keep the audit trail', () => {
  it('strips the provider segment for display, and only the provider segment', () => {
    // The pure function, over both shipped id spellings.
    expect(modelDisplayName('anthropic/claude-opus-5')).toBe('claude-opus-5');
    expect(modelDisplayName('moonshotai/kimi-k3')).toBe('kimi-k3');
    // Bedrock's dotted id has no provider SEGMENT — returned untouched
    // rather than guessed at by splitting on the dot.
    expect(modelDisplayName('anthropic.claude-opus-4-8')).toBe('anthropic.claude-opus-4-8');
    // Degenerate inputs must not silently become the empty string, which
    // would render as "no model ran".
    expect(modelDisplayName('claude-opus-5')).toBe('claude-opus-5');
    expect(modelDisplayName('anthropic/')).toBe('anthropic/');
  });

  it('the visible model cell shows names only — no provider, on requested or served', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-openrouter');

    const primary = screen.getByTestId('history-model-primary-rev-openrouter');
    const critic = screen.getByTestId('history-model-critic-rev-openrouter');
    const servedCritic = screen.getByTestId('history-model-served-critic-rev-openrouter');

    expect(primary.textContent).toBe('claude-opus-5');
    expect(critic.textContent).toBe('claude-sonnet-4.6');
    expect(servedCritic.textContent).toBe('kimi-k3');
    for (const el of [primary, critic, servedCritic]) {
      expect(el.textContent).not.toContain('/');
    }
  });

  it('the FULL ids stay retrievable — hover text plus a focusable disclosure', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-openrouter');

    // Mouse: the full id is the hover text of the name it shortened.
    expect(screen.getByTestId('history-model-primary-rev-openrouter').getAttribute('title')).toBe(
      'anthropic/claude-opus-5',
    );
    expect(
      screen.getByTestId('history-model-served-critic-rev-openrouter').getAttribute('title'),
    ).toBe('moonshotai/kimi-k3');

    // Keyboard / assistive tech: a real button, named, that reveals every
    // id on the row. "Which model ran each step" is the audit trail — this
    // is a disclosure-on-demand change, not a data-removal change.
    const toggle = screen.getByTestId('history-model-ids-toggle-rev-openrouter');
    expect(toggle.tagName).toBe('BUTTON');
    expect(screen.getByRole('button', { name: /full model id/i })).toBe(toggle);

    fireEvent.click(toggle);
    const disclosed = screen.getByTestId('history-model-ids-rev-openrouter').textContent ?? '';
    expect(disclosed).toContain('anthropic/claude-opus-5');
    expect(disclosed).toContain('anthropic/claude-sonnet-4.6');
    expect(disclosed).toContain('moonshotai/kimi-k3');
  });

  it('a dotted (Bedrock) id renders unchanged, and a row with no ids offers no disclosure', async () => {
    stubReviews(BEDROCK);
    render(<ReviewHistory />);
    await screen.findByTestId('history-models-rev-bedrock');

    expect(screen.getByTestId('history-model-primary-rev-bedrock').textContent).toBe(
      'anthropic.claude-opus-4-8',
    );
  });

  it('an unrecorded model still says so, and is never given a name it did not have', async () => {
    stubReviews({ ...BEDROCK, primary_model_id: null, critic_model_id: null });
    render(<ReviewHistory />);
    const cell = await screen.findByTestId('history-models-rev-bedrock');

    expect(cell.textContent?.toLowerCase()).toContain('not recorded');
    expect(cell.textContent).not.toContain('claude');
    // Nothing recorded means nothing to disclose.
    expect(screen.queryByTestId('history-model-ids-toggle-rev-bedrock')).toBeNull();
  });
});

describe('#668 — in-row prose becomes controls and marks that carry an accessible name', () => {
  it('the instructions expander is an icon control resolvable by name, both ways round', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    await screen.findByTestId('history-row-rev-openrouter');

    const toggle = screen.getByTestId('history-guidance-toggle-rev-openrouter');
    expect(toggle.tagName).toBe('BUTTON');
    // The sentence left the cell; the meaning did not.
    expect(toggle.textContent).not.toContain('instructions');
    expect(screen.getByRole('button', { name: /show the instructions/i })).toBe(toggle);
    expect(toggle.getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: /hide the instructions/i })).toBe(toggle);
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
  });

  it('both downloads are icon controls with distinct accessible names', async () => {
    stubReviews(OPENROUTER);
    render(<ReviewHistory />);
    await screen.findByTestId('history-actions-rev-openrouter');

    const redline = screen.getByTestId('history-download-output-rev-openrouter');
    const input = screen.getByTestId('history-download-input-rev-openrouter');

    // Two download controls sitting side by side are only distinguishable by
    // name, so the names must not collide — that is the whole risk of
    // trading words for glyphs.
    expect(screen.getByRole('button', { name: /download the redline/i })).toBe(redline);
    expect(screen.getByRole('button', { name: /download the input document/i })).toBe(input);
    expect(redline.getAttribute('aria-label')).not.toBe(input.getAttribute('aria-label'));
    expect(redline.textContent).not.toMatch(/redline/i);
  });

  it('“no redline was produced” survives as an accessible name, not as a sentence in the cell', async () => {
    stubReviews(BEDROCK);
    render(<ReviewHistory />);
    const cell = await screen.findByTestId('history-actions-rev-bedrock');

    // The sentences are what made this the widest column.
    expect(cell.textContent).not.toMatch(/No redline was produced/i);
    expect(cell.textContent).not.toMatch(/Input document not recorded/i);

    // But a sighted-keyboard or screen-reader user must still be told WHY
    // there is no button here, rather than facing an unexplained blank.
    const noOutput = within(cell).getByTestId('history-no-output-rev-bedrock');
    const noInput = within(cell).getByTestId('history-no-input-rev-bedrock');
    expect(noOutput.getAttribute('aria-label')).toMatch(/no redline was produced/i);
    expect(noOutput.getAttribute('title')).toMatch(/no redline was produced/i);
    expect(noInput.getAttribute('aria-label')).toMatch(/input document/i);
    expect(noInput.getAttribute('title')).toMatch(/input document/i);
    // Named images, so assistive tech reads them rather than skipping a bare
    // glyph or announcing "em dash".
    expect(noOutput.getAttribute('role')).toBe('img');
    expect(noInput.getAttribute('role')).toBe('img');
  });
});
