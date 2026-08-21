/**
 * coverNote.ts — "Butter it" (issue #499): the shared client for
 * `POST /api/reviews/{id}/cover-note`, shared by `ReviewSubmission.tsx`
 * (the just-finished panel) and `ReviewHistory.tsx` (a past row's expanded
 * detail) so the two surfaces cannot drift on wording or on how a failure
 * is turned into copy — same convention `disposition.ts`'s module docstring
 * establishes for the same reason.
 *
 * ## Copy-only, never a send
 *
 * The response is plain body text a reviewer pastes into their own email
 * client. This module never constructs a `mailto:` link, never calls a
 * send API, and never renders the draft through anything but a plain text
 * node — see the render sites in `ReviewSubmission.tsx` / `ReviewHistory.tsx`.
 *
 * ## Failure is quiet, not a banner
 *
 * `backend/src/review_routes.py`'s route returns HTTP 502 for "the model
 * call itself failed or was unavailable" — a routine, retryable condition,
 * not a bug — and this client turns THAT status specifically into
 * `{ ok: false }` rather than throwing, so the caller can render the
 * issue's own quiet copy ("Couldn't butter this one — the redline is
 * unaffected") plus a retry, never a scary red banner. Every OTHER
 * non-2xx (404/403/409, or a network failure) still throws, exactly like
 * `disposition.ts`'s `recordDisposition` — those are real, surfaced
 * problems (an already-cancelled review, a caller who was never the
 * owner), not "the cheap-model call had a bad day".
 */
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';

export interface CoverNoteResult {
  reviewId: string;
  draft: string;
  costUsdCents: number;
  cached: boolean;
  generatedAt: string | null;
  servedModelId: string | null;
  /** The stored cost of the generation that produced a CACHED draft (null
   * on a fresh, non-cached generation — `costUsdCents` above already
   * carries the real cost in that case). Backend field
   * `last_generation_cost_usd_cents`, issue #499 fix round 1: without
   * this, the cached path (reload / History revisit) has no real cost to
   * seed the Regenerate button's cost hint from. */
  lastGenerationCostUsdCents: number | null;
}

export type CoverNoteOutcome =
  | ({ ok: true } & CoverNoteResult)
  | { ok: false };

/** The quiet, non-alarming copy for a degraded generation (issue #499 AC:
 * "failure degrades to a quiet ... with retry"). Shared so neither surface
 * invents its own wording. */
export const COVER_NOTE_FAILURE_COPY =
  "Couldn't butter this one — the redline is unaffected.";

function parseCoverNoteResponse(body: Record<string, unknown>): CoverNoteResult {
  return {
    reviewId: typeof body.review_id === 'string' ? body.review_id : '',
    draft: typeof body.draft === 'string' ? body.draft : '',
    costUsdCents: typeof body.cost_usd_cents === 'number' ? body.cost_usd_cents : 0,
    cached: body.cached === true,
    generatedAt: typeof body.generated_at === 'string' ? body.generated_at : null,
    servedModelId: typeof body.served_model_id === 'string' ? body.served_model_id : null,
    lastGenerationCostUsdCents:
      typeof body.last_generation_cost_usd_cents === 'number'
        ? body.last_generation_cost_usd_cents
        : null,
  };
}

/**
 * POST /api/reviews/{id}/cover-note. `regenerate: false` (the default)
 * returns a previously cached draft for free when one exists; `true`
 * always pays for a fresh draft (the issue's own "Regenerate (with cost
 * hint)" control).
 *
 * Returns `{ ok: false }` for the backend's own "couldn't generate this
 * one" signal (HTTP 502) — never throws for that case. Throws a
 * `friendlyErrorMessage`-wrapped `Error` for every other failure (404/403/
 * 409, or the fetch itself failing), same as `disposition.ts
 * ::recordDisposition` — those are real problems the caller must surface,
 * not the routine "the model had a bad day" case 502 represents.
 */
export async function butterIt(
  reviewId: string,
  { regenerate = false }: { regenerate?: boolean } = {},
): Promise<CoverNoteOutcome> {
  const response = await authorizedFetch(`/api/reviews/${reviewId}/cover-note`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ regenerate }),
  });
  if (response.status === 502) {
    // Logged for support/diagnostics correlation, never rendered raw.
    void readErrorDetail(response).then((detail) => {
      if (detail) {
        // eslint-disable-next-line no-console
        console.error(detail);
      }
    });
    return { ok: false };
  }
  if (!response.ok) {
    const errorDetail = await readErrorDetail(response);
    throw new Error(
      friendlyErrorMessage(
        errorDetail ??
          `POST /api/reviews/${reviewId}/cover-note returned HTTP ${response.status}`,
        "We couldn't load that. Please try again.",
      ),
    );
  }
  const body = (await response.json()) as Record<string, unknown>;
  return { ok: true, ...parseCoverNoteResponse(body) };
}

/**
 * `$0.00` / `<$0.01` / `$0.03` — the same cents-as-money convention the
 * backend's own log lines use ("cost logged... show the cents like the
 * receipt does", issue #499's Design section), rendered here since no
 * shared frontend formatter exists yet for a USD-cents integer.
 */
export function formatCostUsdCents(cents: number): string {
  if (cents <= 0) {
    return '$0.00';
  }
  const dollars = cents / 100;
  return dollars < 0.01 ? '<$0.01' : `$${dollars.toFixed(2)}`;
}
