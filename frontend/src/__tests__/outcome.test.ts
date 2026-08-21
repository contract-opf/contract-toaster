/**
 * outcome.test.ts — the shared outcome→(label, variant) map (issue #470).
 *
 * Two properties this file exists to pin:
 *
 *   1. **Totality.** Every member of `ReviewOutcome` has a map entry with a
 *      human label (no underscore) and a real chip variant. A `Record`
 *      already makes an omission a compile error; this test is the runtime
 *      check the ticket asks for, and it walks the union directly rather
 *      than trusting `Object.keys(OUTCOME_CHIPS)` — which would trivially
 *      "cover" whatever the map itself contains even if a union member were
 *      dropped.
 *   2. **The label and the variant can never disagree**, because they come
 *      from the SAME resolved token — the defect this ticket exists to fix
 *      (`ReviewHistory.tsx` used to read the label off `decision` and the
 *      variant off `status`, two different fields).
 */
import { describe, expect, it } from 'vitest';
import { describeOutcome, OUTCOME_CHIPS, resolveOutcome, type ReviewOutcome } from '../outcome';

// Mirrors the union in outcome.ts exactly. Listed by hand, not derived from
// `Object.keys(OUTCOME_CHIPS)`, so a union member the map forgot would fail
// this test rather than passing vacuously.
const ALL_OUTCOMES: ReviewOutcome[] = [
  'PENDING',
  'RUNNING',
  'DONE',
  'ACCEPT',
  'REQUEST_CHANGE',
  'MANUAL_REVIEW_REQUIRED',
  'ERROR_MANUAL_REVIEW_REQUIRED',
  'ERROR',
  'QUARANTINED',
  'SUPERSEDED',
  'CANCELLED',
];

describe('OUTCOME_CHIPS — total over the outcome union', () => {
  it.each(ALL_OUTCOMES)('has a human label and a real chip variant for %s', (outcome) => {
    const chip = OUTCOME_CHIPS[outcome];
    expect(chip).toBeDefined();
    expect(chip.label.length).toBeGreaterThan(0);
    // The defect this ticket fixes: a raw, underscored wire identifier
    // reaching a reviewer's screen as the chip's own text.
    expect(chip.label).not.toContain('_');
    expect(chip.label).not.toEqual(chip.label.toUpperCase());
    expect(['ok', 'warn', 'danger', 'info', 'muted']).toContain(chip.variant);
  });

  it('covers every member of the union — no outcome silently falls through', () => {
    expect(Object.keys(OUTCOME_CHIPS).sort()).toEqual([...ALL_OUTCOMES].sort());
  });
});

describe('describeOutcome — label and variant never disagree', () => {
  it('resolves REQUEST_CHANGE to the same label and variant regardless of the row it came from', () => {
    // The two rows from the live bug report: same decision, different
    // status/provenance shape. Both must render identically.
    const a = describeOutcome('DONE', 'REQUEST_CHANGE');
    const b = describeOutcome('ERROR_MANUAL_REVIEW_REQUIRED', 'REQUEST_CHANGE');
    expect(a).toEqual(b);
    expect(a.label).toBe('Changes requested');
    expect(a.label).not.toContain('_');
  });

  it('prefers the decision over the status when both are present and known', () => {
    expect(describeOutcome('DONE', 'ACCEPT').label).toBe('Accepted');
    expect(describeOutcome('DONE', 'REQUEST_CHANGE').label).toBe('Changes requested');
  });

  it('falls back to the status when there is no decision', () => {
    expect(describeOutcome('PENDING', null).label).toBe('In progress');
    expect(describeOutcome('ERROR', undefined).label).toBe('Failed');
    expect(describeOutcome('QUARANTINED').label).toBe('Quarantined');
  });

  it('handles the mock pipeline’s MANUAL_REVIEW_REQUIRED decision the same as the status-only case', () => {
    // backend/src/pipeline_runner.py::_mock_decision can write
    // decision="MANUAL_REVIEW_REQUIRED" alongside status="MANUAL_REVIEW_REQUIRED".
    expect(describeOutcome('MANUAL_REVIEW_REQUIRED', 'MANUAL_REVIEW_REQUIRED')).toEqual(
      describeOutcome('MANUAL_REVIEW_REQUIRED', null),
    );
  });

  it('never renders a bare underscored token, even for an outcome it does not recognise', () => {
    const chip = describeOutcome('SOME_FUTURE_STATUS', null);
    expect(chip.label).not.toContain('_');
    expect(chip.variant).toBe('danger');
  });

  it('resolveOutcome returns null only when neither field is known', () => {
    expect(resolveOutcome('DONE', 'REQUEST_CHANGE')).toBe('REQUEST_CHANGE');
    expect(resolveOutcome('QUARANTINED', null)).toBe('QUARANTINED');
    expect(resolveOutcome('SOME_FUTURE_STATUS', null)).toBeNull();
  });

  it('prefers the QUARANTINED/SUPERSEDED overlay over a stale decision the pipeline already wrote', () => {
    // RUNBOOK.md: an operator quarantines (or supersedes) a review that has
    // ALREADY gone terminal with a decision — the quarantine writer
    // (backend/src/reviews.py) only SETs status/quarantine_reason/
    // quarantine_bundle_hash, it never clears `decision`. So this row shape
    // — status QUARANTINED, decision ACCEPT — is exactly what a quarantined,
    // previously-accepted review looks like on the wire, and it must not
    // read as "Accepted".
    expect(describeOutcome('QUARANTINED', 'ACCEPT')).toEqual(OUTCOME_CHIPS.QUARANTINED);
    expect(describeOutcome('QUARANTINED', 'ACCEPT').label).toBe('Quarantined');
    expect(describeOutcome('QUARANTINED', 'ACCEPT').variant).toBe('danger');

    expect(describeOutcome('SUPERSEDED', 'ACCEPT')).toEqual(OUTCOME_CHIPS.SUPERSEDED);
    expect(describeOutcome('SUPERSEDED', 'ACCEPT').label).toBe('Superseded');
    expect(describeOutcome('SUPERSEDED', 'ACCEPT').variant).toBe('muted');

    expect(describeOutcome('SUPERSEDED', 'REQUEST_CHANGE').label).toBe('Superseded');
    expect(resolveOutcome('QUARANTINED', 'ACCEPT')).toBe('QUARANTINED');
    expect(resolveOutcome('SUPERSEDED', 'ACCEPT')).toBe('SUPERSEDED');
  });
});
