/**
 * receipt-critic-line-96.test.tsx — issue #96 (Option B, owner decision
 * 2026-09-16): the receipt's brief, counts-only critic-delta/confidence-band
 * line.
 *
 * Locks in three things against the real `receiptLines()`:
 *
 *   1. A detail with a non-empty `critic_delta` prints the counts line —
 *      for ANY of the three `_CRITIC_DELTA_KEYS`
 *      (`scripts/reconciliation.py`), including a delta whose only content
 *      is `rationale_objections`. That variant is the one the band cannot
 *      cover: `reconciliation.py` sets `has_critic_delta` from all three
 *      but excludes `rationale_objections` from `critic_contests_output`,
 *      so a rationale-only objection off an OK primary yields a non-empty
 *      delta with a NULL band, and a line keyed on the other two counts
 *      would go silent about a critic that did object.
 *   2. An empty delta and an OK (null) confidence band print NOTHING — no
 *      stray "Critic:" line, no empty row.
 *   3. The line contains no objection prose or derived clause text — only
 *      counts, the literal nouns "replacement"/"issue"/"rationale
 *      objection", and the `confidence_band` wire token.
 *      `docs/output-contract.md` -> "Critic-delta presentation" is where
 *      `critic_objection` / `critic_suggested_replacement` /
 *      `rationale_objections[].objection` belong; never on a printed slip.
 *
 * Fixture fidelity: every `critic_delta` below is the merged record shape
 * `scripts/reconciliation.py` actually emits (lists of dicts keyed
 * `section_ref` / `critic_objection` / `objection`, per
 * `playbooks/output-schema-v3.json` -> `definitions.CriticDelta`), carried
 * to the client by `backend/src/reviews.py::get_review_detail` off the
 * analysis artifact. `tests/test_critic_delta_carrythrough_96.py` pins that
 * same shape against a real `review_spine.run_review` run, so these two
 * files cannot drift apart silently.
 */
import { describe, expect, it } from 'vitest';
import { receiptLines, type ReceiptSource } from '../toaster/receipt';

const BASE: ReceiptSource = {
  review_id: 'r-1',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
};

function criticLineText(review: ReceiptSource): string | undefined {
  return receiptLines(review).find((line) => line.id === 'critic-delta')?.value;
}

describe('the receipt critic-delta / confidence-band line (issue #96)', () => {
  it('prints counts for a non-empty critic_delta with no band', () => {
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: {
        contested_replacements: [{ critic_objection: 'ignored' }],
        added_issues: [{ id: 'a' }, { id: 'b' }],
      },
    };
    expect(criticLineText(review)).toBe('Critic: 1 replacement contested, 2 issues added');
  });

  it('prints the band alone when the delta is empty but the band is non-OK', () => {
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: { contested_replacements: [], added_issues: [], rationale_objections: [] },
      confidence_band: 'MANUAL_REVIEW_REQUIRED',
    };
    expect(criticLineText(review)).toBe('Critic: confidence MANUAL_REVIEW_REQUIRED');
  });

  it('prints counts and the trimmed band together', () => {
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: {
        contested_replacements: [{ critic_objection: 'x' }, { critic_objection: 'y' }],
        added_issues: [{ id: 'a' }],
      },
      confidence_band: 'LOW_CONFIDENCE',
    };
    expect(criticLineText(review)).toBe(
      'Critic: 2 replacements contested, 1 issue added · confidence LOW',
    );
  });

  it('prints a rationale-objection-only delta, which carries no band at all', () => {
    // The producible state a counts line keyed on the other two keys would
    // miss entirely: `reconciliation.py` emits this delta (has_critic_delta
    // is true) while `critic_contests_output` is false, so `confidence_band`
    // stays null off an OK primary.
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: {
        contested_replacements: [],
        added_issues: [],
        rationale_objections: [
          { section_ref: '4', objection: 'The external rationale misstates the clause.' },
        ],
      },
      confidence_band: null,
    };
    const text = criticLineText(review) ?? '';
    expect(text).toBe('Critic: 1 rationale objection');
    expect(text).not.toContain('misstates');
  });

  it('counts all three delta keys together, pluralised', () => {
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: {
        contested_replacements: [{ section_ref: '8', critic_objection: 'x' }],
        added_issues: [{ section_ref: '12' }, { section_ref: '13' }],
        rationale_objections: [
          { section_ref: '4', objection: 'a' },
          { section_ref: '5', objection: 'b' },
        ],
      },
      confidence_band: 'LOW_CONFIDENCE',
    };
    expect(criticLineText(review)).toBe(
      'Critic: 1 replacement contested, 2 issues added, 2 rationale objections · confidence LOW',
    );
  });

  it('prints nothing for an empty delta and an OK (null) band', () => {
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: { contested_replacements: [], added_issues: [], rationale_objections: [] },
      confidence_band: null,
    };
    expect(criticLineText(review)).toBeUndefined();
  });

  it('prints nothing when critic_delta and confidence_band are both absent', () => {
    expect(criticLineText(BASE)).toBeUndefined();
  });

  it('never carries objection prose or derived clause text', () => {
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: {
        contested_replacements: [
          {
            critic_objection: 'Drifts from the playbook position on liability caps.',
            critic_suggested_replacement: 'Company shall indemnify Counterparty for...',
          },
        ],
        added_issues: [],
        rationale_objections: [
          { section_ref: '4', objection: 'Over-flags a boilerplate notices clause.' },
        ],
      },
      confidence_band: 'ERROR_MANUAL_REVIEW_REQUIRED',
    };
    const text = criticLineText(review) ?? '';
    expect(text).not.toBe('');
    expect(text).not.toContain('Drifts');
    expect(text).not.toContain('playbook position');
    expect(text).not.toContain('indemnify');
    expect(text).not.toContain('Over-flags');
    expect(text).not.toContain('boilerplate');
    expect(text).toBe(
      'Critic: 1 replacement contested, 1 rationale objection · confidence ERROR_MANUAL_REVIEW_REQUIRED',
    );
  });
});
