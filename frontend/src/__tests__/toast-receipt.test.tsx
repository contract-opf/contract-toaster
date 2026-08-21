/**
 * toast-receipt.test.tsx — the provenance slip (issue #498).
 *
 * A receipt is a record. Someone will paste one into a deal thread and it will
 * be read as a statement of what happened, so the two things that matter are:
 *
 *   1. **Every line is sourced.** A line whose field the review row does not
 *      carry is DROPPED, never filled with a plausible-looking value. The test
 *      that matters here is the sparse one: a review missing lineage must
 *      print a SHORTER receipt, not a receipt with invented content.
 *   2. **All three renderings say the same thing.** The slip on screen, "Copy
 *      as text", and the saved image all consume `receiptLines()`. The test
 *      compares the image's drawn strings to the clipboard payload directly —
 *      not both to a fixture — so it bites the moment one rendering grows its
 *      own idea of the content.
 *
 * The image is a real PNG via canvas, which jsdom does not implement. The
 * canvas is stubbed to CAPTURE what was drawn rather than to rasterize it:
 * that proves the content identity, which is the property worth asserting.
 * Pixel fidelity is not asserted and is not claimed.
 *
 * Fully offline: Amplify auth is mocked, fetch is stubbed, canvas is stubbed.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ToastReceipt, drawReceipt } from '../toaster/ToastReceipt';
import {
  acceptedChangesSummary,
  receiptFilename,
  receiptLines,
  receiptText,
  toastedIn,
  type ReceiptLine,
} from '../toaster/receipt';

vi.mock('../auth', () => ({
  getToken: vi.fn(async () => 'mock-token'),
  isPasswordMode: () => true,
  setDemoToken: vi.fn(),
}));

// A finished review carrying everything the row can carry.
const FULL = {
  review_id: 'abcd1234-5678-90ab-cdef-1234567890ab',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
  created_at: '1000000000',
  updated_at: '1000000192', // 3m 12s later
  playbook_id: 'synthetic-nda',
  playbook_version: '1.0.0',
  instructions_version: 3,
  primary_model_id: 'primary/model-a',
  critic_model_id: 'critic/model-b',
  issues: [
    { clause_id: 'c-1', source_quote: 'one' },
    { clause_id: 'c-1', source_quote: 'two' },
    { clause_id: 'c-2', source_quote: 'three' },
  ],
  critic_delta: { contested_issue_ids: ['i-1'], added_issues: [{ id: 'i-9' }] },
};

// The same review as recorded by a deployment that predates the lineage
// fields — the "legacy row" case the ticket calls out.
const SPARSE = {
  review_id: 'abcd1234-5678-90ab-cdef-1234567890ab',
  status: 'DONE',
  decision: 'ACCEPT',
  created_at: '1000000000',
  updated_at: '1000000030',
};

// Issue #570: a review whose stage 1 accepted a counterparty's pending
// edits on two paragraphs — one single-author, one multi-author — before
// review ran, and whose bounded re-quote pass (#569) fully recovered.
const WITH_ASSUMPTIONS = {
  ...SPARSE,
  normalization_notes:
    "Paragraph 'Term': pending tracked change (author: Jane Doe, status: unresolved) " +
    "accepted-all into the operative draft. Paragraph 'Payment': 2 pending tracked " +
    'changes from 2 author(s) accepted-all into the operative draft.',
  requote: { attempted: 2, recovered: 2, still_failed: 0 },
};

/** Replace canvas with a recorder. Returns the strings drawn, in order. */
function stubCanvas(): { drawn: string[]; dataUrls: string[] } {
  const drawn: string[] = [];
  const dataUrls: string[] = [];
  const ctx = {
    fillStyle: '',
    font: '',
    textBaseline: '',
    fillRect: vi.fn(),
    scale: vi.fn(),
    fillText: (text: string) => drawn.push(text),
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    ctx as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(() => {
    const url = 'data:image/png;base64,STUB';
    dataUrls.push(url);
    return url;
  });
  return { drawn, dataUrls };
}

function lastDownload(): HTMLAnchorElement | undefined {
  return clicked[clicked.length - 1];
}
const clicked: HTMLAnchorElement[] = [];

beforeEach(() => {
  vi.restoreAllMocks();
  clicked.length = 0;
  // Capture the download without navigating.
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push(this);
  });
});

describe('the receipt prints only what the review actually recorded', () => {
  it('prints every line a fully-recorded review carries', () => {
    render(<ToastReceipt review={FULL} playbookName="Synthetic NDA Sample" />);
    const paper = screen.getByTestId('review-receipt-paper');
    const text = paper.textContent ?? '';

    expect(text).toContain('Synthetic NDA Sample');
    expect(text).toContain('v1.0.0');
    expect(text).toContain('v3');
    expect(text).toContain('CHANGES REQUESTED');
    expect(text).toContain('primary/model-a');
    expect(text).toContain('critic/model-b');
    // Duration from the two timestamps — a fact about the run, computed the
    // one way, never a stopwatch the UI kept.
    expect(text).toContain('3m 12s');
    // Three issues across TWO distinct clauses: the clause count is the
    // distinct-anchor count, which is why it is not simply the issue count.
    expect(paper.querySelector('[data-receipt-line="issues"]')?.textContent).toContain('3');
    expect(paper.querySelector('[data-receipt-line="clauses"]')?.textContent).toContain('2');
  });

  it('DROPS the lines a sparse row cannot source, and invents nothing', () => {
    render(<ToastReceipt review={SPARSE} />);
    const text = screen.getByTestId('review-receipt-paper').textContent ?? '';

    // The receipt still prints — the layout holds.
    expect(text).toContain('CONTRACT TOASTER');
    expect(text).toContain('ACCEPTED AS DRAFTED');
    // ...but nothing claims a version, a model, or a count that was never
    // recorded. This is the assertion a "fill in a sensible default" change
    // would break, and it is the whole reason the receipt is trustworthy.
    expect(text).not.toContain('Playbook version');
    expect(text).not.toContain('Standing instructions');
    expect(text).not.toContain('Primary');
    expect(text).not.toContain('Critic');
    expect(text).not.toContain('Clauses touched');
    // Issue #570: neither assumption line claims anything a sparse row
    // never recorded.
    expect(text).not.toContain('accepted before review');
    expect(text).not.toContain('retried');
    // And no empty rows standing in for the dropped ones.
    expect(text).not.toMatch(/undefined|null|NaN|—\s*$/);
  });

  it('collapses the divider a dropped section would have left behind', () => {
    render(<ToastReceipt review={SPARSE} />);
    const rules = screen
      .getByTestId('review-receipt-paper')
      .querySelectorAll('.toaster-receipt__rule');
    // Every rule has content after it: a sparse receipt reads as short, not
    // as broken.
    rules.forEach((rule) => {
      expect(rule.nextElementSibling).not.toBeNull();
    });
  });

  it('prints the accepted-changes and retry-outcome lines when the review carries them', () => {
    render(<ToastReceipt review={WITH_ASSUMPTIONS} />);
    const paper = screen.getByTestId('review-receipt-paper');

    // One single-author paragraph (1 edit) + one two-author paragraph (2
    // edits) = 3 true edits. Authors is a floor, not a sum: 1 named (Jane
    // Doe) vs. the Payment sentence's own count of 2 -- max(1, 2) = 2,
    // never 3, since the notes cannot prove Jane Doe is NOT one of
    // Payment's two.
    expect(paper.querySelector('[data-receipt-line="accepted-changes"]')?.textContent).toBe(
      '3 pending edits from 2 authors accepted before review (see your original to compare).',
    );
    // requote.still_failed === 0: fully recovered, no blame-laden phrasing.
    expect(paper.querySelector('[data-receipt-line="retry-outcome"]')?.textContent).toBe(
      '2 unresolved quotes retried — all recovered.',
    );
    // Both disclosure sentences use the full-width wrapping line type, not
    // the fixed nowrap label/value row every other line uses -- that row
    // truncates a sentence this long against the paper's capped width.
    expect(paper.querySelector('[data-receipt-line="accepted-changes"]')?.className).toContain(
      'toaster-receipt__line--wrap',
    );
    expect(paper.querySelector('[data-receipt-line="retry-outcome"]')?.className).toContain(
      'toaster-receipt__line--wrap',
    );
  });

  it('states a partial retry outcome plainly', () => {
    const partial = { ...SPARSE, requote: { attempted: 3, recovered: 1, still_failed: 2 } };
    render(<ToastReceipt review={partial} />);
    expect(
      screen
        .getByTestId('review-receipt-paper')
        .querySelector('[data-receipt-line="retry-outcome"]')?.textContent,
    ).toBe('3 unresolved quotes retried — 2 quotes still unapplied.');
  });

  it('drops the retry-outcome line when nothing was attempted', () => {
    const nothingAttempted = { ...SPARSE, requote: { attempted: 0, recovered: 0, still_failed: 0 } };
    render(<ToastReceipt review={nothingAttempted} />);
    expect(
      screen.getByTestId('review-receipt-paper').querySelector('[data-receipt-line="retry-outcome"]'),
    ).toBeNull();
  });

  it('renders the accepted-changes line whether or not #569 has landed', () => {
    // #570's own ticket text: this line must render correctly regardless of
    // whether `requote` is present at all -- it simply is not, pre-#569.
    const preRequote = { ...SPARSE, normalization_notes: WITH_ASSUMPTIONS.normalization_notes };
    render(<ToastReceipt review={preRequote} />);
    const paper = screen.getByTestId('review-receipt-paper');
    expect(paper.querySelector('[data-receipt-line="accepted-changes"]')).not.toBeNull();
    expect(paper.querySelector('[data-receipt-line="retry-outcome"]')).toBeNull();
  });

  it('survives a row with nothing on it at all', () => {
    render(<ToastReceipt review={{}} />);
    const text = screen.getByTestId('review-receipt-paper').textContent ?? '';
    // The masthead is the only unconditional line, so this is the true
    // minimum: a slip that says who printed it and claims nothing else.
    expect(text).toContain('CONTRACT TOASTER');
    expect(text.replace('CONTRACT TOASTER', '').trim()).toBe('');
  });
});

describe('all three renderings say the same thing', () => {
  it('draws exactly the clipboard text into the image', () => {
    const { drawn } = stubCanvas();
    render(<ToastReceipt review={FULL} playbookName="Synthetic NDA Sample" />);
    fireEvent.click(screen.getByTestId('review-receipt-save'));

    // Compare the DRAWN strings to the clipboard payload's lines — not both
    // to a fixture. This bites the moment one rendering grows its own idea of
    // the content, which is the failure a provenance slip must not have.
    const clipboard = receiptText(receiptLines(FULL, 'Synthetic NDA Sample'), 40);
    expect(drawn.join('\n')).toBe(clipboard);
  });

  it('saves under the ticket-specified name', () => {
    stubCanvas();
    render(<ToastReceipt review={FULL} playbookName="Synthetic NDA Sample" />);
    fireEvent.click(screen.getByTestId('review-receipt-save'));
    expect(lastDownload()?.download).toBe('toast-receipt-abcd1234.png');
    expect(receiptFilename(FULL.review_id, 'png')).toBe('toast-receipt-abcd1234.png');
  });

  it('copies the text rendering to the clipboard', async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    render(<ToastReceipt review={FULL} playbookName="Synthetic NDA Sample" />);
    fireEvent.click(screen.getByTestId('review-receipt-copy'));
    expect(writeText).toHaveBeenCalledWith(receiptText(receiptLines(FULL, 'Synthetic NDA Sample')));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  it('says so when the image cannot be produced, instead of doing nothing', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    render(<ToastReceipt review={FULL} />);
    fireEvent.click(screen.getByTestId('review-receipt-save'));
    expect(screen.getByTestId('review-receipt-error').textContent).toContain('Copy as text');
    expect(lastDownload()).toBeUndefined();
  });

  // Issue #570 follow-up: the accepted-changes/retry-outcome sentences are
  // long enough to overflow the fixed label/value column that every other
  // line uses, in all three renderings at once (nowrap + clip-path on
  // screen, a 40-column image, a dot-leader clipboard line). These cases
  // exercise the wrapping fix the same way as the block above -- by
  // construction, not by a fixture of expected pixels.
  it('draws the same wrapped disclosure lines into the image as the clipboard holds', () => {
    const { drawn } = stubCanvas();
    render(<ToastReceipt review={WITH_ASSUMPTIONS} />);
    fireEvent.click(screen.getByTestId('review-receipt-save'));

    const lines = receiptLines(WITH_ASSUMPTIONS);
    const clipboard = receiptText(lines, 40);
    expect(drawn.join('\n')).toBe(clipboard);
    // The image genuinely reflowed the long sentences across more than one
    // drawn row rather than drawing (and clipping) one long one per line —
    // this is what catches a fix that wraps `receiptText` but forgets to
    // size the canvas off the wrapped row count instead of `lines.length`.
    expect(drawn.length).toBeGreaterThan(lines.length);
  });

  it('word-wraps a long disclosure line instead of truncating it or leaving a stray dot leader', () => {
    // Constructed directly against `receiptText`/`ReceiptLine.wrap` rather
    // than through a fixture, so the assertion is exact regardless of where
    // the word-wrap happens to break.
    const longSentence =
      '3 pending edits from 2 authors accepted before review (see your original to compare).';
    const lines: ReceiptLine[] = [{ id: 'accepted-changes', label: '', value: longSentence, wrap: true }];
    const rows = receiptText(lines, 40).split('\n');

    // Wrapped onto more than one physical row, not silently cut off at 40
    // columns the way the old label/value formatting would have.
    expect(rows.length).toBeGreaterThan(1);
    rows.forEach((row) => expect(row.length).toBeLessThanOrEqual(40));
    // Word-wrap only ever breaks on a space already between two words, so
    // rejoining the rows with a single space reconstructs the sentence
    // exactly -- nothing is lost or reordered.
    expect(rows.join(' ')).toBe(longSentence);
    // Never falls back to the label/value dot-leader format an empty label
    // used to produce (a stray leading ".").
    rows.forEach((row) => expect(row.trimStart().startsWith('.')).toBe(false));
  });
});

describe('the pieces', () => {
  it('formats durations without lying about them', () => {
    expect(toastedIn('100', '292')).toBe('3m 12s');
    expect(toastedIn('100', '130')).toBe('30s');
    // Missing, unparseable, or backwards: no duration rather than a wrong one.
    expect(toastedIn(null, '130')).toBeNull();
    expect(toastedIn('not-a-number', '130')).toBeNull();
    expect(toastedIn('300', '100')).toBeNull();
  });

  it('parses the accepted-edit and author counts from normalization_notes', () => {
    // Absent, or nothing recognizable in it: null, never zero (zero would
    // claim "we checked and there were none", which is not what an empty or
    // garbled string means).
    expect(acceptedChangesSummary(null)).toBeNull();
    expect(acceptedChangesSummary(undefined)).toBeNull();
    expect(acceptedChangesSummary('')).toBeNull();
    expect(acceptedChangesSummary('a future wording this parser has never seen')).toBeNull();

    // One single-author paragraph: 1 edit, 1 author.
    expect(
      acceptedChangesSummary(
        "Paragraph 'Term': pending tracked change (author: Jane Doe, status: unresolved) " +
          'accepted-all into the operative draft.',
      ),
    ).toEqual({ edits: 1, authors: 1 });

    // Two single-author paragraphs, SAME author: named authors dedupe by
    // exact string match across paragraphs. 2 edits (one each), 1 author.
    expect(
      acceptedChangesSummary(
        "Paragraph 'Term': pending tracked change (author: Jane Doe, status: unresolved) " +
          "accepted-all into the operative draft. Paragraph 'Notice': pending tracked change " +
          '(author: Jane Doe, status: rejected) accepted-all into the operative draft.',
      ),
    ).toEqual({ edits: 2, authors: 1 });

    // One multi-author paragraph: the note names a count, never names. The
    // sentence's own N (2) is the edit count -- not "1 paragraph" -- and its
    // own M (2) is the author count.
    expect(
      acceptedChangesSummary(
        "Paragraph 'Payment': 2 pending tracked changes from 2 author(s) accepted-all " +
          'into the operative draft.',
      ),
    ).toEqual({ edits: 2, authors: 2 });

    // Issue #570: one lawyer edits "Term" once (a single-author sentence,
    // which never states an author COUNT) and "Payment" twice (a
    // multi-author sentence that itself asserts exactly one author) -- the
    // SAME person both times. True edit count is 1 + 2 = 3. The notes give
    // no way to confirm the named author ("Jane Doe") is (or is not) the
    // Payment sentence's one unnamed author, so the receipt must not SUM
    // them into 2 -- the floor the data actually supports is
    // max(1 named, 1 asserted) = 1, exactly what really happened here.
    expect(
      acceptedChangesSummary(
        "Paragraph 'Term': pending tracked change (author: Jane Doe, status: unresolved) " +
          "accepted-all into the operative draft. Paragraph 'Payment': 2 pending tracked " +
          'changes from 1 author(s) accepted-all into the operative draft.',
      ),
    ).toEqual({ edits: 3, authors: 1 });

    // A named author plus a genuinely-separate multi-author paragraph: the
    // multi-author sentence's own M (2) is still never summed with the
    // named author -- max(1, 2) = 2, the largest figure the notes actually
    // support, never 3 (which would assert the two are provably distinct
    // people, which the text does not say either).
    expect(
      acceptedChangesSummary(
        "Paragraph 'Term': pending tracked change (author: Jane Doe, status: unresolved) " +
          "accepted-all into the operative draft. Paragraph 'Payment': 2 pending tracked " +
          'changes from 2 author(s) accepted-all into the operative draft.',
      ),
    ).toEqual({ edits: 3, authors: 2 });
  });

  it('fails closed on a sentence its own structured parse cannot read, rather than counting around it', () => {
    // An apostrophe in the heading -- arbitrary document text taken
    // straight from the .docx (scripts/extraction_normalization_stage.py)
    // -- defeats `Paragraph '[^']*':` for that sentence, but the sentence's
    // invariant tail ("accepted-all into the operative draft.") is still
    // there, so the mismatch is detectable: two accept-all sentences are
    // present, only one parses, so the whole summary drops rather than
    // silently reporting "1 pending edit" for a note that folded in two.
    expect(
      acceptedChangesSummary(
        "Paragraph 'Company's Obligations': pending tracked change (author: Jane Doe, " +
          "status: unresolved) accepted-all into the operative draft. Paragraph 'Notice': " +
          'pending tracked change (author: Jane Doe, status: unresolved) accepted-all into ' +
          'the operative draft.',
      ),
    ).toBeNull();

    // A "Last, First" author name (the standard AD/Office display form) --
    // the comma defeats `\(author: ([^,]+), status:`. Same fail-closed
    // outcome: one sentence parses, one doesn't, so nothing is reported.
    expect(
      acceptedChangesSummary(
        "Paragraph 'Term': pending tracked change (author: Doe, Jane, status: unresolved) " +
          "accepted-all into the operative draft. Paragraph 'Notice': pending tracked change " +
          '(author: Jane Smith, status: unresolved) accepted-all into the operative draft.',
      ),
    ).toBeNull();

    // Every author name in the note has a comma: no sentence parses at
    // all, which is also fail-closed (not a false "nothing was accepted").
    expect(
      acceptedChangesSummary(
        "Paragraph 'Term': pending tracked change (author: Doe, Jane, status: unresolved) " +
          'accepted-all into the operative draft.',
      ),
    ).toBeNull();

    // A genuinely fail-closed normalization note (scripts/normalize_input.py's
    // "cannot determine the operative text to accept" wording -- issue #530
    // narrowed this to the one remaining fail-closed condition, a malformed
    // record with no resulting_text) never carries the accept-all tail at
    // all -- correctly null, not a false report of edits accepted.
    expect(
      acceptedChangesSummary(
        "Paragraph 'Indemnification': pending tracked change has no resulting_text " +
          '-- malformed revision record; cannot determine the operative text to accept.',
      ),
    ).toBeNull();
  });

  it('drawReceipt writes the same lines it is given', () => {
    const drawn: string[] = [];
    const ctx = {
      fillStyle: '',
      font: '',
      textBaseline: '',
      fillRect: () => {},
      scale: () => {},
      fillText: (text: string) => drawn.push(text),
    } as unknown as CanvasRenderingContext2D;
    const lines = receiptLines(FULL, 'Synthetic NDA Sample');
    drawReceipt(ctx, lines);
    expect(drawn.join('\n')).toBe(receiptText(lines, 40));
  });
});

// ---------------------------------------------------------------------------
// The receipt's second home (issue #498): History's expanded row. One artifact,
// two homes -- and NO second request: the detail fetch that already loaded the
// instructions feeds the receipt too.
// ---------------------------------------------------------------------------
describe('History prints the same receipt for a past review', () => {
  it('renders it from the detail fetch that was already happening', async () => {
    const detailCalls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const pathname = new URL(String(input), 'http://localhost').pathname;
        if (pathname === '/api/reviews') {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              reviews: [
                {
                  review_id: FULL.review_id,
                  status: 'DONE',
                  decision: 'REQUEST_CHANGE',
                  playbook_id: 'synthetic-nda',
                  created_at: FULL.created_at,
                  primary_model_id: FULL.primary_model_id,
                  critic_model_id: FULL.critic_model_id,
                  has_output: true,
                },
              ],
            }),
          } as Response;
        }
        detailCalls.push(pathname);
        return { ok: true, status: 200, json: async () => FULL } as Response;
      }),
    );

    const { default: ReviewHistory } = await import('../ReviewHistory');
    render(<ReviewHistory />);
    fireEvent.click(await screen.findByTestId(`history-guidance-toggle-${FULL.review_id}`));

    const paper = await screen.findByTestId('review-receipt-paper');
    expect(paper.textContent).toContain('v1.0.0');
    expect(paper.textContent).toContain('3m 12s');
    // One detail request, not two: the receipt rides the fetch the
    // instructions expander was already making.
    expect(detailCalls).toHaveLength(1);
  });
});
