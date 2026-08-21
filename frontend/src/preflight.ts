/**
 * preflight.ts — client for `POST /api/reviews/preflight` (issue #491).
 *
 * A cheap, fast, ADVISORY check `ReviewSubmission.tsx` fires the moment a
 * file is chosen — before "Upload for review" — so a reviewer sees word
 * count/page estimate/title and a does-this-match-the-dial signal without
 * waiting for the full two-pass review. Per the issue's own words: "this is
 * advisory only. It never blocks a submission — no enforcement, ever."
 *
 * `runPreflight` therefore NEVER throws and never surfaces a technical
 * error to the caller: a network failure, a non-2xx response, or a
 * malformed body all resolve to `null` (no card renders) rather than an
 * error banner a reviewer would have to dismiss before uploading anyway.
 * This mirrors the backend route's own posture (`backend/src/
 * review_routes.py`'s `post_review_preflight`: every cheap-model failure
 * degrades to `classification: "unavailable"`, never an HTTP error) one
 * layer further out, for the fetch itself.
 *
 * ## Injection-defense rider (2026-08-03 security pass, see #505/#506/#507)
 *
 * `agreement_type_guess` and `paper_side` are UNTRUSTED MODEL OUTPUT,
 * already constrained server-side to a closed enum
 * (`scripts/preflight_pass.py::sanitize_classification`) — but this client
 * still treats them as untrusted rather than assuming the server-side
 * contract holds: `normalizePaperSide` re-validates against the same
 * closed set before this value ever reaches a render decision, so a
 * malformed or unexpected value degrades to `"unclear"` here too rather
 * than propagating whatever string arrived. `one_line_summary` is NOT
 * re-validated here beyond a length clamp (`SUMMARY_MAX_CHARS`, mirroring
 * the backend's own cap) — it is free text by design, and the render-time
 * defense is `ReviewSubmission.tsx` rendering it as a plain text node only,
 * never through React's raw-HTML escape hatch or a link parser. No field
 * from this response is ever used to construct a URL, an href, or markup.
 */
import { authorizedFetch } from './api';

export type PreflightPaperSide = 'ours' | 'counterparty' | 'unclear';
export type PreflightClassification = 'ok' | 'unavailable';
export type PreflightMatch = 'likely' | 'unclear' | 'unlikely';

const PAPER_SIDES: readonly PreflightPaperSide[] = ['ours', 'counterparty', 'unclear'];
const MATCH_VERDICTS: readonly PreflightMatch[] = ['likely', 'unclear', 'unlikely'];

// Mirrors scripts/preflight_pass.py's SUMMARY_MAX_CHARS — defense in depth
// on top of the server's own cap, not a substitute for it.
const SUMMARY_MAX_CHARS = 160;

// Issue #491 rider item 4: "carry its findings into the same card -- one
// flag, not two." `null` (not the scan's own empty-dict shape) means "no
// flag" -- a clean document, or the scan itself degraded -- so a render
// check can be a single truthiness test. Deliberately just the rule ids and
// a count -- the same ids-and-counts-only shape
// `document_injection_scan.summarise` already writes onto the review row --
// never a locator or any document/model text: this field carries no
// payload for a crafted document to ride into the DOM through.
export interface PreflightInjectionScan {
  ruleIds: string[];
  findingCount: number;
}

export interface PreflightResult {
  wordCount: number;
  pageEstimate: number;
  paragraphCount: number;
  title: string | null;
  classification: PreflightClassification;
  agreementTypeGuess: string | null;
  paperSide: PreflightPaperSide;
  confidence: number | null;
  oneLineSummary: string | null;
  match: PreflightMatch | null;
  injectionScan: PreflightInjectionScan | null;
}

function normalizePaperSide(value: unknown): PreflightPaperSide {
  return typeof value === 'string' && (PAPER_SIDES as readonly string[]).includes(value)
    ? (value as PreflightPaperSide)
    : 'unclear';
}

function normalizeMatch(value: unknown): PreflightMatch | null {
  return typeof value === 'string' && (MATCH_VERDICTS as readonly string[]).includes(value)
    ? (value as PreflightMatch)
    : null;
}

function normalizeSummary(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, SUMMARY_MAX_CHARS) : null;
}

// `body.injection_scan` is `{}` (clean, or the scan degraded) or
// `{injection_scan_rule_ids: string[], injection_scan_finding_count: number}`
// (backend/src/review_routes.py, via document_injection_scan.summarise) --
// never anything else. Anything that doesn't match that exact shape
// normalizes to `null` (no flag) rather than propagating an unexpected
// value into render.
function normalizeInjectionScan(value: unknown): PreflightInjectionScan | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const raw = value as Record<string, unknown>;
  const ruleIdsRaw = raw.injection_scan_rule_ids;
  const findingCount = raw.injection_scan_finding_count;
  if (!Array.isArray(ruleIdsRaw) || typeof findingCount !== 'number' || findingCount <= 0) {
    return null;
  }
  const ruleIds = ruleIdsRaw.filter((id): id is string => typeof id === 'string');
  if (ruleIds.length === 0) {
    return null;
  }
  return { ruleIds, findingCount };
}

function parsePreflightResponse(body: Record<string, unknown>): PreflightResult {
  const classification = body.classification === 'ok' ? 'ok' : 'unavailable';
  return {
    wordCount: typeof body.word_count === 'number' ? body.word_count : 0,
    pageEstimate: typeof body.page_estimate === 'number' ? body.page_estimate : 0,
    paragraphCount: typeof body.paragraph_count === 'number' ? body.paragraph_count : 0,
    title: typeof body.title === 'string' ? body.title : null,
    classification,
    agreementTypeGuess:
      classification === 'ok' && typeof body.agreement_type_guess === 'string'
        ? body.agreement_type_guess
        : null,
    paperSide: normalizePaperSide(body.paper_side),
    confidence: typeof body.confidence === 'number' ? body.confidence : null,
    oneLineSummary: classification === 'ok' ? normalizeSummary(body.one_line_summary) : null,
    match: classification === 'ok' ? normalizeMatch(body.match) : null,
    injectionScan: normalizeInjectionScan(body.injection_scan),
  };
}

/**
 * POST the chosen file (+ selected playbook) to `/api/reviews/preflight`
 * and return a normalized result, or `null` on ANY failure (network error,
 * non-2xx status, unparseable body) — see module docstring for why this
 * never throws and never surfaces a technical error to the caller.
 */
export async function runPreflight(
  file: File,
  playbookId: string,
): Promise<PreflightResult | null> {
  try {
    const formData = new FormData();
    formData.append('file', file);
    if (playbookId) {
      formData.append('playbook_id', playbookId);
    }
    const response = await authorizedFetch('/api/reviews/preflight', {
      method: 'POST',
      body: formData,
    });
    if (!response.ok) {
      return null;
    }
    const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body) {
      return null;
    }
    return parsePreflightResponse(body);
  } catch {
    return null;
  }
}

/**
 * POST just the already-classified `agreementTypeGuess` and a (possibly
 * NEW) `playbookId` to `/api/reviews/preflight/match`, and return the
 * recomputed match verdict alone, or `null` on ANY failure — same
 * never-throws, never-surfaces-an-error posture as `runPreflight` (see
 * module docstring).
 *
 * Issue #491 fix round 1: `render_preflight_user_prompt` never reads
 * `playbookId` — only the match verdict does — so a dial change only needs
 * THIS cheap, file-free call, not a full re-upload + re-classification via
 * `runPreflight`. `ReviewSubmission.tsx` fires this instead of `runPreflight`
 * when the file has not changed but the selected playbook has.
 */
export async function refreshMatchVerdict(
  agreementTypeGuess: string | null,
  playbookId: string,
): Promise<PreflightMatch | null> {
  try {
    const formData = new FormData();
    if (agreementTypeGuess) {
      formData.append('agreement_type_guess', agreementTypeGuess);
    }
    if (playbookId) {
      formData.append('playbook_id', playbookId);
    }
    const response = await authorizedFetch('/api/reviews/preflight/match', {
      method: 'POST',
      body: formData,
    });
    if (!response.ok) {
      return null;
    }
    const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body) {
      return null;
    }
    return normalizeMatch(body.match);
  } catch {
    return null;
  }
}
