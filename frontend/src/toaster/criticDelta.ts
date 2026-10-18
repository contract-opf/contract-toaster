/**
 * The critic delta, counted once (issue #167).
 *
 * Three readers ask "what did the critic change?" of `critic_delta`: the
 * receipt's counts line (`receipt.ts`), the console projection's indicator
 * (`orbit-diner/projection.ts`), and the pre-download gate
 * (`ReviewSubmission.tsx`). Since issue #140 the answer lives in
 * `critic_delta.overrides`, the reconciler's record of every respect in which
 * the critic's final result differs from the reviewer's
 * (`scripts/reconciliation.py::reconcile`, guard 2). The deprecated
 * `contested_replacements` / `added_issues` arrays still ride on the wire for
 * one release, so the gate and the console indicator's COUNTS read them ONLY
 * as a fallback -- when the overrides record no change at all -- and never add
 * the two together (a critic-added issue is in both, and would be counted
 * twice). (The indicator's objection lines are not counts and are not built
 * here: `projection.ts` still reads `contested_replacements` verbatim for
 * them, per the output contract's "Contested-replacement badge" rule.)
 *
 * One module so the gate and the indicator it gates can never disagree about
 * whether there is a delta: both are `criticDeltaCounts` underneath.
 *
 * Counts only. Nothing here reads a `primary_value`, `critic_value`,
 * `reason`, objection or replacement text.
 */

/** The parts of `critic_delta` the counts read. Every field is `unknown`:
 *  the wire is untrusted, and a malformed field counts as nothing. */
export interface CriticDeltaSource {
  overrides?: unknown;
  contested_replacements?: unknown;
  added_issues?: unknown;
}

/** One `critic_delta.overrides[]` record, as far as these counts read it:
 *  four controlled tokens, never a value or a reason. */
interface OverrideRecord {
  source?: unknown;
  change?: unknown;
  retained?: unknown;
  issue_id?: unknown;
  field?: unknown;
}

/** One count per kind of override record. */
export interface OverrideCounts {
  overrides: number;
  added: number;
  dropped: number;
  retained: number;
}

/**
 * Issue #140: `critic_delta.overrides` counted the way the internal footnotes
 * explain it (`scripts/redline_generate.py::_rendered_override_records`), so
 * "N overrides" on the slip is the number of override notes in the
 * document.
 *
 * A COMPUTED record (`source: "computed"`, built by
 * `scripts/reconciliation.py::reconcile`) is `added` (the critic raised an
 * issue the reviewer did not), `dropped` (the reverse -- `retained: true`
 * when it was a hard rejection guard 1 put back), or `replaced` (a kept
 * issue whose edits differ). Every other record is the critic's own
 * statement of a change. The reconciler can record one change twice, so two
 * of the critic's are not counted again: one on an issue the reconciler
 * recorded as dropped (the drop says it), and a `replacement_text` one on an
 * issue it recorded as `replaced` (that entry says it).
 */
export function overrideCounts(overrides: unknown): OverrideCounts {
  const counts: OverrideCounts = { overrides: 0, added: 0, dropped: 0, retained: 0 };
  if (!Array.isArray(overrides)) {
    return counts;
  }
  const records = overrides.filter(
    (entry): entry is OverrideRecord => typeof entry === 'object' && entry !== null,
  );
  const computed = records.filter((entry) => entry.source === 'computed');
  const idsWith = (change: string): Set<string> =>
    new Set(
      computed
        .filter((entry) => entry.change === change && typeof entry.issue_id === 'string')
        .map((entry) => entry.issue_id as string),
    );
  const droppedIds = idsWith('dropped');
  const replacedIds = idsWith('replaced');
  for (const entry of records) {
    if (entry.source === 'computed') {
      if (entry.change === 'added') {
        counts.added += 1;
      } else if (entry.change === 'dropped') {
        if (entry.retained === true) {
          counts.retained += 1;
        } else {
          counts.dropped += 1;
        }
      } else {
        counts.overrides += 1;
      }
      continue;
    }
    const id = typeof entry.issue_id === 'string' ? entry.issue_id : null;
    if (id !== null && droppedIds.has(id)) {
      continue;
    }
    if (id !== null && replacedIds.has(id) && entry.field === 'replacement_text') {
      continue;
    }
    counts.overrides += 1;
  }
  return counts;
}

/**
 * The delta's counts, and which record they came from.
 *
 * `fromOverrides: true` -- the overrides record at least one change; the
 * four override counts are authoritative and `contested` is 0 (the
 * deprecated arrays are not read).
 *
 * `fromOverrides: false` -- the overrides record nothing (absent, empty, or
 * KEEP-only), so the deprecated arrays are read as the fallback while they
 * still exist: `contested` and `added` are their lengths, and the override
 * counts are 0.
 */
export interface CriticDeltaCounts extends OverrideCounts {
  contested: number;
  fromOverrides: boolean;
}

function arrayLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

export function criticDeltaCounts(delta: CriticDeltaSource): CriticDeltaCounts {
  const counts = overrideCounts(delta.overrides);
  if (counts.overrides + counts.added + counts.dropped + counts.retained > 0) {
    return { ...counts, contested: 0, fromOverrides: true };
  }
  return {
    overrides: 0,
    added: arrayLength(delta.added_issues),
    dropped: 0,
    retained: 0,
    contested: arrayLength(delta.contested_replacements),
    fromOverrides: false,
  };
}

/**
 * Whether the delta gates the download (docs/output-contract.md -> "Download
 * gate"): any override of any kind, or -- only when the overrides record
 * nothing -- a deprecated contested replacement or critic-added issue. A
 * null delta, one with empty lists, or one carrying only KEEP dispositions
 * (the critic changed nothing) does not gate.
 */
export function criticDeltaHasContent(delta: CriticDeltaSource | null | undefined): boolean {
  if (!delta) {
    return false;
  }
  const counts = criticDeltaCounts(delta);
  return (
    counts.overrides + counts.added + counts.dropped + counts.retained + counts.contested > 0
  );
}
