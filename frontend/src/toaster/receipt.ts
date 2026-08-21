/**
 * The receipt (issue #498) — a thermal-style provenance slip for a finished
 * review, and the one place its content is decided.
 *
 * WHY THIS IS A PURE FUNCTION, NOT A COMPONENT
 *
 * The receipt has three renderings: the spooling slip on the Review tab, the
 * "Copy as text" clipboard payload, and the "Save receipt" image. If each
 * built its own lines, three renderings would drift — and a provenance slip
 * that says something different depending on how you exported it is worse
 * than no slip at all, because someone will paste one into a deal thread.
 * So `receiptLines()` is the single source, and every rendering consumes it.
 *
 * NEVER INVENT PRECISION
 *
 * Every line is sourced from a field the review row actually carries. A line
 * whose source is absent is DROPPED, not filled with a plausible-looking
 * value and not rendered as an empty row. That is why `receiptLines` returns
 * a list rather than a fixed-shape record: "not recorded" is expressed by the
 * line not existing, which is the same convention `get_review_detail` uses on
 * the wire.
 *
 * WHAT IS NOT HERE YET, AND WHY
 *
 * The ticket's mock shows four lines this build cannot honestly produce:
 *
 *   - the toast NUMBER, which needs the lifetime odometer (#501 part 2 —
 *     blocked on a schema decision, see that issue);
 *   - the FILENAME, which needs `original_filename` (#518, unmerged);
 *   - WORDS IN / WORDS CHANGED, which nothing currently computes or records;
 *   - the COST, which needs per-review actuals from the spend ledger
 *     (#414/#415 territory, unwired in production).
 *
 * They are absent rather than estimated. An estimate on a provenance slip is
 * indistinguishable from a fact once it has been pasted somewhere, and the
 * whole point of this artifact is that it can be trusted at a glance.
 */

export interface ReceiptSource {
  review_id?: string | null;
  status?: string | null;
  decision?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  playbook_id?: string | null;
  playbook_version?: string | null;
  instructions_version?: string | number | null;
  primary_model_id?: string | null;
  critic_model_id?: string | null;
  toaster_guidance?: string | null;
  issues?: unknown;
  critic_delta?: { contested_issue_ids?: unknown; added_issues?: unknown } | null;
  // Issue #563: the free-text disclosure that stage 1 accepted one or more
  // of the counterparty's own pending tracked changes into the operative
  // draft before review ever ran. Absent (never null) on a review with
  // nothing to accept -- same convention `get_review_detail` uses for
  // every field on the row.
  normalization_notes?: string | null;
  // Issue #569: present only once the bounded re-quote repair pass has run
  // (the flag can be off, or the ticket unmerged, for a long time yet) --
  // this field is simply absent until then, and the receipt must render
  // correctly either way.
  requote?: { attempted?: number; recovered?: number; still_failed?: number } | null;
}

export interface ReceiptLine {
  /** Stable key for tests and for React. */
  id: string;
  /** Left-hand label. Empty for a rule or a bare value line. */
  label: string;
  /** Right-hand value. Empty for a rule. */
  value: string;
  /** A horizontal rule rather than a label/value pair. */
  rule?: true;
  /** A full-width disclosure SENTENCE rather than a label/value pair —
   *  issue #570 follow-up. The label/value row is a fixed-width column
   *  layout (nowrap on screen, a dot-leader gap in text/PNG) that a long
   *  sentence overflows and gets truncated by rather than fits. A `wrap`
   *  line renders as wrapped prose instead: no label, no dot leader, and
   *  it reflows across as many physical lines as it needs. */
  wrap?: true;
}

const RULE: Omit<ReceiptLine, 'id'> = { label: '', value: '', rule: true };

/** Human duration from two epoch-second strings. Null if either is missing or
 *  unparseable — a duration is a fact about the run, and a wrong one is worse
 *  than a missing one. */
export function toastedIn(createdAt?: string | null, updatedAt?: string | null): string | null {
  // `Number(null)` is 0, not NaN — so a missing timestamp has to be rejected
  // BEFORE the numeric check, or a review with no `created_at` reports a
  // duration measured from the epoch. Caught by the test, not by reading.
  if (!createdAt || !updatedAt) {
    return null;
  }
  const start = Number(createdAt);
  const end = Number(updatedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return null;
  }
  const seconds = Math.round(end - start);
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

/**
 * The date line, from an epoch-seconds timestamp — despite the name (kept
 * for the receipt's own call site, `toastedOn(created_at)`), this is a
 * generic epoch-seconds formatter: ReviewSubmission.tsx's result-panel meta
 * line (issue #492) reuses it for `updated_at` (the finished-at time)
 * rather than duplicating the same "YYYY-MM-DD  HH:MM UTC" formatting.
 * Exported for that reuse; null-safe on a missing/unparseable/non-positive
 * timestamp the same way every other receipt field is (see this module's
 * docstring — "never invent precision").
 */
export function toastedOn(createdAt?: string | null): string | null {
  const epoch = Number(createdAt);
  if (!Number.isFinite(epoch) || epoch <= 0) {
    return null;
  }
  const when = new Date(epoch * 1000);
  const date = when.toISOString().slice(0, 10);
  const time = when.toISOString().slice(11, 16);
  return `${date}  ${time} UTC`;
}

const DECISION_WORDS: Record<string, string> = {
  ACCEPT: 'ACCEPTED AS DRAFTED',
  REQUEST_CHANGE: 'CHANGES REQUESTED',
};

function count(value: unknown): number | null {
  return Array.isArray(value) ? value.length : null;
}

/** How many distinct clauses the issues touch — an issue carries the anchor it
 *  was raised against, so the clause count is the distinct-anchor count and
 *  never simply the issue count. Null when no issue carries one. */
function clausesTouched(issues: unknown): number | null {
  if (!Array.isArray(issues)) {
    return null;
  }
  const anchors = new Set<string>();
  for (const issue of issues) {
    const anchor =
      (issue as { clause_id?: unknown; anchor_id?: unknown; source_quote?: unknown }) ?? {};
    const key = anchor.clause_id ?? anchor.anchor_id ?? anchor.source_quote;
    if (typeof key === 'string' && key) {
      anchors.add(key);
    }
  }
  return anchors.size > 0 ? anchors.size : null;
}

/** `N thing` / `N things` — never a bare digit next to a noun that might not
 *  agree with it. */
function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`;
}

/**
 * Parsed from the free-text `normalization_notes` disclosure
 * (`scripts/normalize_input.py::_normalize_paragraph`, verbatim, issue
 * #563) into the two counts the receipt needs. Out of scope for this
 * ticket is any change to what #563 persists, so this parses the SAME
 * sentence the row already carries rather than a new structured field.
 *
 * Each accepted paragraph contributes exactly one sentence, in one of two
 * shapes:
 *   "Paragraph 'X': pending tracked change (author: Y, status: Z)
 *    accepted-all into the operative draft."                     (1 edit)
 *   "Paragraph 'X': N pending tracked changes from M author(s)
 *    accepted-all into the operative draft."                     (N edits)
 * — joined with a single space, never anything else.
 *
 * `edits` is the sum of every sentence's own true edit count — N for the
 * multi-change shape, 1 for the single-change shape. Issue #570: the
 * number a "pending edit" line reports must be how many edits were
 * accepted, not how many sentences (paragraphs) happened to carry them —
 * the sentence already states its own N; discarding it in favor of a
 * per-paragraph count of 1 drops the one truthful number the notes give.
 *
 * `authors` is a FLOOR the notes can prove, never a sum across sentences.
 * A multi-author sentence states a COUNT but never names, so a name from
 * one single-author sentence can never be reconciled against an unnamed
 * author counted in another — they may be the same person or different
 * people, and assuming either answer invents an identity match the data
 * does not carry. `max(namedAuthors.size, the largest M any one sentence
 * asserts)` is the largest figure the notes actually support: one
 * sentence's own M is a hard fact about that paragraph, but M values from
 * two DIFFERENT multi-author sentences are never summed (the same
 * unprovable-overlap problem one level up), and a named author already
 * seen is never assumed to add to, or be included in, a multi-author
 * sentence's count. This is deliberately a floor, not the true total —
 * the true total is not knowable from this text, and a receipt must never
 * claim more authors than it can show.
 *
 * Parses defensively, but asymmetrically. A `notes` string with nothing
 * recognizable in it returns null, exactly like an absent field. But once
 * at least one accept-all disposition IS present — detected off its own
 * invariant tail, `accepted-all into the operative draft.`, which every
 * such sentence carries regardless of what heading or author text
 * precedes it — every one of those sentences must parse for the count to
 * be trustworthy. Headings and author names are arbitrary document text
 * (an apostrophe in a heading, a comma in a "Last, First" author name)
 * that can defeat the heading/author-shaped structured parse below while
 * leaving the tail intact; a count that silently excludes the sentence it
 * could not read is WRONG, not merely incomplete, because the paragraph it
 * drops is still part of the set being counted. When the structured parse
 * accounts for fewer accept-all sentences than the tail count finds, the
 * whole summary drops — a missing line beats a wrong one, the receipt's
 * own stated rule (see the file header, "NEVER INVENT PRECISION").
 */
export function acceptedChangesSummary(
  notes?: string | null,
): { edits: number; authors: number } | null {
  if (!notes) {
    return null;
  }
  // The invariant tail every accept-all disposition ends with, independent
  // of the arbitrary heading/author text that precedes it. Used only to
  // COUNT how many such sentences are present, as a check on the
  // structured parse below — never to extract data itself. A fail-closed
  // disposition (`... cannot accept-all.` / `... cannot determine the
  // operative text to accept.`) never matches this tail, so it correctly
  // contributes nothing here.
  const tailCount = (notes.match(/accepted-all into the operative draft\./g) ?? []).length;
  if (tailCount === 0) {
    return null;
  }

  const pattern =
    /Paragraph '[^']*': (?:(\d+) pending tracked changes from (\d+) author\(s\)|pending tracked change \(author: ([^,]+), status: [^)]*\)) accepted-all into the operative draft\./g;
  let edits = 0;
  let parsed = 0;
  const namedAuthors = new Set<string>();
  let maxUnnamedAuthors = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(notes)) !== null) {
    parsed += 1;
    if (match[1] !== undefined && match[2] !== undefined) {
      edits += Number(match[1]);
      maxUnnamedAuthors = Math.max(maxUnnamedAuthors, Number(match[2]));
    } else {
      edits += 1;
      namedAuthors.add(match[3] as string);
    }
  }

  if (parsed !== tailCount) {
    // At least one accept-all sentence used heading or author text this
    // parser could not read. A count that quietly excludes it would
    // understate what was folded in — drop the whole line instead.
    return null;
  }

  return { edits, authors: Math.max(namedAuthors.size, maxUnnamedAuthors) };
}

/** The accepted-before-review line's text, or null when there is nothing to
 *  disclose. Plain and factual, no blame — names what was folded in and
 *  points at the one way to check it, exactly like every other receipt
 *  line claims only what was recorded. */
function acceptedChangesLine(notes?: string | null): string | null {
  const summary = acceptedChangesSummary(notes);
  if (!summary) {
    return null;
  }
  return (
    `${plural(summary.edits, 'pending edit')} from ${plural(summary.authors, 'author')} ` +
    'accepted before review (see your original to compare).'
  );
}

/** The retry-outcome line's text, from `requote` (issue #569) when
 *  present. Renders correctly whether or not #569 has landed, since the
 *  field is simply absent until then — same drop-when-absent rule as
 *  every other line. `attempted <= 0` is treated as absent: nothing was
 *  actually retried, so there is nothing to disclose. */
function requoteLine(requote?: ReceiptSource['requote']): string | null {
  if (!requote || typeof requote.attempted !== 'number' || requote.attempted <= 0) {
    return null;
  }
  const attempted = requote.attempted;
  const stillFailed = typeof requote.still_failed === 'number' ? requote.still_failed : 0;
  if (stillFailed <= 0) {
    return `${plural(attempted, 'unresolved quote')} retried — all recovered.`;
  }
  return `${plural(attempted, 'unresolved quote')} retried — ${plural(stillFailed, 'quote')} still unapplied.`;
}

/**
 * The receipt, line by line. Only lines whose source exists are returned.
 *
 * `playbookName` is passed in rather than looked up here: the display name
 * lives in the catalog, and this module must not learn to fetch.
 */
export function receiptLines(
  review: ReceiptSource,
  playbookName?: string | null,
): ReceiptLine[] {
  const lines: ReceiptLine[] = [];
  const push = (id: string, label: string, value: string | null | undefined) => {
    if (value !== null && value !== undefined && value !== '') {
      lines.push({ id, label, value });
    }
  };
  // A full-width disclosure sentence rather than a label/value pair — see
  // `ReceiptLine.wrap`. Issue #570 follow-up: the fixed nowrap row every
  // other line uses truncates a sentence this long against the paper's
  // capped width.
  const pushWrap = (id: string, value: string | null | undefined) => {
    if (value !== null && value !== undefined && value !== '') {
      lines.push({ id, label: '', value, wrap: true });
    }
  };

  lines.push({ id: 'title', label: 'CONTRACT TOASTER', value: '' });
  push('date', '', toastedOn(review.created_at));
  lines.push({ id: 'rule-1', ...RULE });

  push('playbook', 'Contract type', playbookName || review.playbook_id);
  push(
    'playbook-version',
    'Playbook version',
    review.playbook_version ? `v${review.playbook_version}` : null,
  );
  push(
    'instructions',
    'Standing instructions',
    review.instructions_version ? `v${review.instructions_version}` : null,
  );

  lines.push({ id: 'rule-2', ...RULE });

  push('outcome', 'Outcome', review.decision ? DECISION_WORDS[review.decision] ?? review.decision : null);
  const issueCount = count(review.issues);
  push('issues', 'Changes requested', issueCount === null ? null : String(issueCount));
  const clauses = clausesTouched(review.issues);
  push('clauses', 'Clauses touched', clauses === null ? null : String(clauses));
  const contested = count(review.critic_delta?.contested_issue_ids);
  push('contested', 'Contested by the critic', contested === null ? null : String(contested));
  const added = count(review.critic_delta?.added_issues);
  push('added', 'Added by the critic', added === null ? null : String(added));

  lines.push({ id: 'rule-3', ...RULE });

  // Issue #570: what was assumed on the reviewer's behalf, before and after
  // the review itself ran. Each line drops independently -- a review with
  // one but not the other still prints, and the rule above collapses away
  // if neither is present.
  pushWrap('accepted-changes', acceptedChangesLine(review.normalization_notes));
  pushWrap('retry-outcome', requoteLine(review.requote));

  lines.push({ id: 'rule-4', ...RULE });

  push('duration', 'Toasted in', toastedIn(review.created_at, review.updated_at));
  push('primary-model', 'Primary', review.primary_model_id);
  push('critic-model', 'Critic', review.critic_model_id);
  // Issue #492: no raw review id anywhere in visible DOM, including this
  // slip -- the id reaches the user only via "Copy review ID"
  // (ReviewSubmission.tsx's own affordance) and `receiptFilename`'s
  // shortened id in the saved PNG's filename, never as a printed line.

  // Two rules in a row means a whole section dropped out. Collapse them, so a
  // sparse receipt reads as short rather than as broken.
  return lines.filter((line, index) => {
    if (!line.rule) {
      return true;
    }
    const next = lines[index + 1];
    return next !== undefined && !next.rule;
  });
}

/** Word-wraps `text` to `width` columns, breaking only on spaces. Used for
 *  the receipt's full-sentence disclosure lines (`ReceiptLine.wrap`), which
 *  are too long for the label/value column and must reflow rather than be
 *  silently cut off. */
function wrapWords(text: string, width: number): string[] {
  const words = text.split(' ');
  const wrapped: string[] = [];
  let current = '';
  for (const word of words) {
    const candidate = current ? `${current} ${word}` : word;
    if (current && candidate.length > width) {
      wrapped.push(current);
      current = word;
    } else {
      current = candidate;
    }
  }
  if (current) {
    wrapped.push(current);
  }
  return wrapped;
}

/** The plain-text rendering — what "Copy as text" puts on the clipboard, and
 *  what the image renderer draws. Both consume `receiptLines`, so all three
 *  renderings say the same thing by construction. */
export function receiptText(lines: ReceiptLine[], width = 44): string {
  return lines
    .flatMap((line) => {
      if (line.rule) {
        return ['-'.repeat(width)];
      }
      if (line.wrap) {
        // No label, no dot leader: the label/value gap formatting below
        // produced a stray leading dot leader for an empty label
        // (`" . <sentence>"`), which is worse than the truncation it stood
        // in for. A wrap line is bare wrapped prose instead.
        return wrapWords(line.value, width);
      }
      if (!line.value) {
        return [line.label];
      }
      const gap = Math.max(1, width - line.label.length - line.value.length - 2);
      return [`${line.label} ${'.'.repeat(gap)} ${line.value}`];
    })
    .join('\n');
}

/** `toast-receipt-<shortid>.<ext>` — the ticket's naming, with the review id
 *  shortened the same way the UI shortens it elsewhere. */
export function receiptFilename(reviewId: string | null | undefined, ext: string): string {
  const short = (reviewId ?? 'unknown').slice(0, 8);
  return `toast-receipt-${short}.${ext}`;
}
