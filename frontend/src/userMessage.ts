/**
 * userMessage.ts — the ONE way a failure becomes text on screen (QA
 * 2026-10-31, W9 item 3).
 *
 * CLAUDE.md: "no raw server exception in the status window". Before this file
 * about forty catch blocks rendered `err instanceof Error ? err.message : …`.
 * That was safe only while every thrown `Error` carried copy someone had
 * written for a reader — and it did not: a network failure rejects `fetch`
 * with the browser's own `TypeError` ("Failed to fetch", "Load failed"), a
 * proxy's HTML error page makes `response.json()` throw a `SyntaxError` that
 * quotes the page, and a non-OK answer's `detail` was re-thrown verbatim
 * whatever its status — a 503's "UPLOADS_BUCKET not configured." included.
 *
 * The rule is now positive rather than a hope:
 *
 *  - `UserFacingError` marks a message somebody WROTE for a reader. Only its
 *    message is ever shown.
 *  - `toUserMessage(err, fallback)` shows a `UserFacingError`'s message and
 *    the caller's fixed `fallback` for anything else — a `TypeError` from
 *    `fetch`, a `SyntaxError` from `json()`, an `AbortError`, a string, a
 *    plain `Error`. The original is logged to the console for an operator.
 *  - `responseError(response, technical, fallback)` turns a non-OK answer
 *    into a `UserFacingError`. The server's `detail` passes through only for
 *    an explicit allowlist of 4xx statuses — the routes' own refusals, which
 *    are written for the person who made the request — and NEVER for a 5xx,
 *    whose detail is server configuration. The same reasoning as
 *    `DOWNLOAD_ERROR_COPY` in api.ts, applied to every other route.
 *
 * `eslint.config.js`'s `ct/no-raw-error-text` rule forbids reading `.message`
 * (or `String(err)`) off a caught value anywhere else in `src/`, so a new
 * catch block cannot quietly reintroduce the old shape.
 */
import { friendlyErrorMessage, readErrorDetail } from './api';

/** A message written for the reader. The only kind `toUserMessage` passes through. */
export class UserFacingError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UserFacingError';
  }
}

/**
 * The 4xx statuses whose string `detail` is the route's own refusal, written
 * for the requester: a validation failure (400), a permission refusal (403 —
 * "Admin privilege required to change the auth-mode setting."), a state
 * conflict (409), a failed precondition (412), a size or type refusal
 * (413/415), and the daily-limit refusal (429). Deliberately absent: 401 (the
 * app shell's session-expired handling owns it), 404 (it can name a route or
 * an id), 410 (callers have their own fixed retention copy), and every 5xx —
 * whose detail is server configuration ("UPLOADS_BUCKET not configured.").
 */
export const DETAIL_ALLOWED_STATUSES: ReadonlySet<number> = new Set([
  400, 403, 409, 412, 413, 415, 422, 429,
]);

/**
 * The sign-in and change-password routes are the one place a 401 is the
 * answer to the question the person asked ("wrong password"), and
 * `demo_auth.py` writes that detail for them.
 */
export const AUTH_DETAIL_STATUSES: ReadonlySet<number> = new Set([...DETAIL_ALLOWED_STATUSES, 401]);

/**
 * Fixed copy for a failure, never the failure's own text. See the module
 * docstring. `fallback` is the caller's context sentence ("We couldn't stop
 * this review. It is still running.").
 */
export function toUserMessage(err: unknown, fallback: string): string {
  if (err instanceof UserFacingError) {
    return err.message;
  }
  return friendlyErrorMessage(err, fallback);
}

/**
 * A non-OK response as a `UserFacingError`: the server's `detail` when the
 * status is in `allowDetailFor` (only 4xx entries count, whatever is passed),
 * otherwise `fallback`. `technical` (method, path, status) goes to the console
 * only.
 *
 * `knownDetails` maps an exact, documented server sentinel to the app's OWN
 * copy for it, at any status — for a 5xx whose cause a reader can act on
 * (`reviews.NO_ACTIVE_PLAYBOOK_DETAIL`). The server's text is matched, never
 * shown: anything that does not match exactly still gets `fallback`.
 */
export async function responseError(
  response: Response,
  technical: string,
  fallback: string,
  allowDetailFor: ReadonlySet<number> | readonly number[] = DETAIL_ALLOWED_STATUSES,
  knownDetails: Readonly<Record<string, string>> = {},
): Promise<UserFacingError> {
  const detail = await readErrorDetail(response);
  const allowed = allowDetailFor instanceof Set ? allowDetailFor : new Set(allowDetailFor);
  const status = response.status;
  if (detail && Object.prototype.hasOwnProperty.call(knownDetails, detail)) {
    // A documented refusal, not a fault: nothing for the console.
    return new UserFacingError(knownDetails[detail]);
  }
  if (detail && status >= 400 && status < 500 && allowed.has(status)) {
    return new UserFacingError(detail);
  }
  return new UserFacingError(
    friendlyErrorMessage(detail ? `${technical}: ${detail}` : technical, fallback),
  );
}

/**
 * Run `run` and re-throw anything it throws as a `UserFacingError`: its own
 * message when it already is one, `fallback` otherwise. For the shared
 * clients (`coverNote.ts`, `disposition.ts`) whose callers render whatever
 * they throw — a network `TypeError` or a `json()` `SyntaxError` escaping one
 * of them is exactly the raw text this module exists to keep off the screen.
 */
export async function withUserFacingErrors<T>(fallback: string, run: () => Promise<T>): Promise<T> {
  try {
    return await run();
  } catch (err) {
    if (err instanceof UserFacingError) {
      throw err;
    }
    throw new UserFacingError(toUserMessage(err, fallback));
  }
}
