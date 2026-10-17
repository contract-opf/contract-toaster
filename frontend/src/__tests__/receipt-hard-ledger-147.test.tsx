/**
 * receipt-hard-ledger-147.test.tsx — issue #147: the receipt's counts-only
 * hard-rejection ledger line.
 *
 * Fixture fidelity: `hard_ledger` below is the record shape
 * `scripts/hard_ledger.py::enforce` returns (`entries`, `cleared`,
 * `asserted`, `inserted`) as `hard_ledger.settle` leaves it once the redline
 * stage has delivered, carried by `review_spine.run_review` into the
 * analysis artifact (`pipeline_runner._ANALYSIS_FIELDS`) and projected by
 * `backend/src/reviews.py::get_review_detail`. `inserted` falls short of
 * `asserted` when a rule has no standard language to insert, when a second
 * rule on the same clause shares the first one's insertion, or when the
 * redline stage refused the insertion (the OPF pen-rule defaults, say) —
 * `tests/test_hard_ledger.py` drives all three through the real `run_review`;
 * a refusal beside another delivered edit is the `{asserted: 1, inserted: 0}`
 * record of a DONE review below.
 */
import { describe, expect, it } from 'vitest';
import { receiptLines, type ReceiptSource } from '../toaster/receipt';

const BASE: ReceiptSource = {
  review_id: 'r-147',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
};

function ledgerLineText(review: ReceiptSource): string | undefined {
  return receiptLines(review).find((line) => line.id === 'hard-ledger')?.value;
}

describe('the receipt hard-rejection ledger line (issue #147)', () => {
  it('prints one asserted, inserted protection', () => {
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 1, inserted: 1, cleared: 0 } })).toBe(
      '1 required protection not found — standard language inserted.',
    );
  });

  it('prints two asserted, inserted protections', () => {
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 2, inserted: 2, cleared: 3 } })).toBe(
      '2 required protections not found — standard language inserted.',
    );
  });

  it('names the flagged-only remainder, singular and plural', () => {
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 2, inserted: 1, cleared: 0 } })).toBe(
      '2 required protections not found — standard language inserted for 1, 1 protection flagged only.',
    );
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 3, inserted: 1, cleared: 0 } })).toBe(
      '3 required protections not found — standard language inserted for 1, 2 protections flagged only.',
    );
  });

  it('never mentions an insertion when none was delivered, singular and plural', () => {
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 1, inserted: 0, cleared: 0 } })).toBe(
      '1 required protection not found — flagged only, no standard language inserted.',
    );
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 2, inserted: 0, cleared: 1 } })).toBe(
      '2 required protections not found — flagged only, no standard language inserted.',
    );
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 2 } })).toBe(
      '2 required protections not found — flagged only, no standard language inserted.',
    );
  });

  it('prints nothing for an all-cleared ledger or no ledger at all', () => {
    expect(ledgerLineText({ ...BASE, hard_ledger: { asserted: 0, inserted: 0, cleared: 2 } })).toBeUndefined();
    expect(ledgerLineText({ ...BASE, hard_ledger: null })).toBeUndefined();
    expect(ledgerLineText(BASE)).toBeUndefined();
  });

  it('is a wrapped disclosure sentence that carries no rule id or clause text', () => {
    const line = receiptLines({
      ...BASE,
      hard_ledger: { asserted: 1, inserted: 1, cleared: 0 },
    }).find((entry) => entry.id === 'hard-ledger');
    expect(line?.wrap).toBe(true);
    expect(line?.label).toBe('');
    expect(line?.value).not.toMatch(/IND-|ledger:|indemn/i);
  });
});
