/**
 * receipt-critic-line-96.test.tsx — the receipt's brief, counts-only critic
 * line: issue #96 (Option B, owner decision 2026-09-16), presented as
 * OVERRIDES since issue #140 (ADR 0001 — the critic has the last word, so
 * the slip counts what the critic changed, not what it contested).
 *
 * Locks in, against the real `receiptLines()`:
 *
 *   1. A review whose `critic_delta.overrides` records changes prints one
 *      counts line — "Critic: 3 overrides, 1 issue added, 1 dropped, 1 hard
 *      rejection retained · confidence OK" — counted the way the internal
 *      footnotes explain them, so a change the critic stated AND the
 *      reconciler computed is one override, and a drop is a drop, not also
 *      an override.
 *   2. A delta with nothing changed (KEEP dispositions only — every review
 *      the reviewer raised an issue on carries those) and an OK (null) band
 *      prints NOTHING; a non-OK band alone prints the band.
 *   3. The deprecated contest arrays are not counted: the "contested"
 *      framing is gone.
 *   4. The line contains no reviewer text, critic text or reason — only
 *      counts, fixed words and the `confidence_band` wire token.
 *
 * Fixture fidelity: the main fixture is NOT hand-built. It is the real
 * `scripts/reconciliation.py::reconcile` output for the synthetic review in
 * `tests/test_critic_delta_internal_footnotes_132.py`, written to
 * `tests/fixtures/critic_delta_overrides_140.json` by that test's
 * `--write-receipt-fixture`; its part 11 fails when reconcile's output and
 * this file drift, and pins `expected_note_counts` to the number of
 * internal-footnote notes of each kind the renderer writes for it. The
 * smaller fixtures below are edits of those real records (same keys, same
 * tokens), each reachable from `reconcile`: a computed `added` / `dropped`
 * / `replaced` entry, or one the critic stated itself.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { receiptLines, type ReceiptSource } from '../toaster/receipt';

interface ReconcilerFixture {
  critic_delta: { overrides: Record<string, unknown>[] };
  confidence_band: string | null;
  expected_note_counts: { override: number; added: number; dropped: number; retained: number };
}

const FIXTURE = JSON.parse(
  fs.readFileSync(
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      '../../../tests/fixtures/critic_delta_overrides_140.json',
    ),
    'utf8',
  ),
) as ReconcilerFixture;
const REAL_OVERRIDES = FIXTURE.critic_delta.overrides;

const BASE: ReceiptSource = {
  review_id: 'r-1',
  status: 'DONE',
  decision: 'REQUEST_CHANGE',
};

function criticLineText(review: ReceiptSource): string | undefined {
  return receiptLines(review).find((line) => line.id === 'critic-delta')?.value;
}

/** The real records whose (source, change) match — so each small fixture is
 *  a subset of what reconcile actually emitted, never an invented shape. */
function realRecords(
  predicate: (entry: Record<string, unknown>) => boolean,
): Record<string, unknown>[] {
  const found = REAL_OVERRIDES.filter(predicate);
  expect(found.length).toBeGreaterThan(0);
  return found;
}

const computed = (change: string) => (entry: Record<string, unknown>) =>
  entry.source === 'computed' && entry.change === change;

describe('the receipt critic line counts overrides (issues #96, #140)', () => {
  it("prints the real reconciler record's counts, matching the internal notes", () => {
    const counts = FIXTURE.expected_note_counts;
    // The fixture is the one the footnote test pins; this is what it holds.
    expect(counts).toEqual({ override: 3, added: 1, dropped: 1, retained: 1 });
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: FIXTURE.critic_delta,
      confidence_band: FIXTURE.confidence_band,
    };
    expect(criticLineText(review)).toBe(
      `Critic: ${counts.override} overrides, ${counts.added} issue added, ` +
        `${counts.dropped} dropped, ${counts.retained} hard rejection retained · confidence OK`,
    );
  });

  it('counts a change the critic stated AND the reconciler computed once', () => {
    const replaced = realRecords(computed('replaced'));
    const issueId = replaced[0].issue_id;
    const stated = realRecords(
      (entry) =>
        entry.source === 'critic' &&
        entry.issue_id === issueId &&
        entry.field === 'replacement_text',
    );
    expect(
      criticLineText({ ...BASE, critic_delta: { overrides: [...stated, ...replaced] } }),
    ).toBe('Critic: 1 override · confidence OK');
  });

  it("counts a drop as a drop, not also as the critic's stated decision override", () => {
    const dropped = realRecords((entry) => computed('dropped')(entry) && entry.retained === false);
    const stated = realRecords(
      (entry) => entry.source === 'critic' && entry.issue_id === dropped[0].issue_id,
    );
    expect(
      criticLineText({ ...BASE, critic_delta: { overrides: [...stated, ...dropped] } }),
    ).toBe('Critic: 1 dropped · confidence OK');
  });

  it('names a retained hard rejection, never as dropped', () => {
    const retained = realRecords((entry) => computed('dropped')(entry) && entry.retained === true);
    expect(criticLineText({ ...BASE, critic_delta: { overrides: retained } })).toBe(
      'Critic: 1 hard rejection retained · confidence OK',
    );
  });

  it('pluralises, and trims the band token', () => {
    const added = realRecords(computed('added'));
    const rationale = realRecords(
      (entry) => entry.source === 'critic' && entry.field === 'rationale',
    );
    expect(
      criticLineText({
        ...BASE,
        critic_delta: { overrides: [...added, ...added, rationale[0]] },
        confidence_band: 'LOW_CONFIDENCE',
      }),
    ).toBe('Critic: 1 override, 2 issues added · confidence LOW');
  });

  it('prints the band alone when the critic changed nothing but the band is non-OK', () => {
    expect(
      criticLineText({
        ...BASE,
        critic_delta: {
          dispositions: [{ issue_id: 'I1', disposition: 'KEEP', reason: 'Kept.' }],
          overrides: [],
        },
        confidence_band: 'MANUAL_REVIEW_REQUIRED',
      }),
    ).toBe('Critic: confidence MANUAL_REVIEW_REQUIRED');
  });

  it('prints nothing for a KEEP-only delta and an OK (null) band', () => {
    expect(
      criticLineText({
        ...BASE,
        critic_delta: {
          dispositions: [{ issue_id: 'I1', disposition: 'KEEP', reason: 'Kept.' }],
          overrides: [],
        },
        confidence_band: null,
      }),
    ).toBeUndefined();
  });

  it('prints nothing when critic_delta and confidence_band are both absent', () => {
    expect(criticLineText(BASE)).toBeUndefined();
  });

  it('no longer counts the deprecated contest arrays', () => {
    const text = criticLineText({
      ...BASE,
      critic_delta: {
        overrides: [],
        contested_replacements: [{ section_ref: '8', critic_objection: 'x' }],
        added_issues: [{ section_ref: '12' }],
        rationale_objections: [{ section_ref: '4', objection: 'y' }],
      },
    });
    expect(text).toBeUndefined();
  });

  it('never carries reviewer text, critic text or a reason', () => {
    const review: ReceiptSource = {
      ...BASE,
      critic_delta: FIXTURE.critic_delta,
      confidence_band: 'ERROR_MANUAL_REVIEW_REQUIRED',
    };
    const text = criticLineText(review) ?? '';
    expect(text).not.toBe('');
    const prose = new Set<string>();
    const collect = (value: unknown): void => {
      if (typeof value === 'string' && value.length > 12) {
        prose.add(value);
      } else if (Array.isArray(value)) {
        value.forEach(collect);
      } else if (value && typeof value === 'object') {
        Object.values(value).forEach(collect);
      }
    };
    REAL_OVERRIDES.forEach((entry) =>
      ['primary_value', 'critic_value', 'reason', 'primary_issue', 'critic_issue'].forEach(
        (key) => collect(entry[key]),
      ),
    );
    expect(prose.size).toBeGreaterThan(0);
    for (const value of prose) {
      for (const line of value.split('\n')) {
        const fragment = line.replace(/^[-+] /, '').slice(0, 24);
        if (fragment.length >= 8) {
          expect(text).not.toContain(fragment);
        }
      }
    }
    expect(text).toBe(
      'Critic: 3 overrides, 1 issue added, 1 dropped, 1 hard rejection retained · ' +
        'confidence ERROR_MANUAL_REVIEW_REQUIRED',
    );
  });
});
