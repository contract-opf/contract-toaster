/**
 * api.ts — shared `authorizedFetch` + friendly-error helpers (issue #271).
 *
 * Before this file, `authorizedFetch` was copy-pasted three times
 * (ReviewSubmission.tsx, AdminUsers.tsx, AdminRetention.tsx) and each copy
 * always sent `Authorization: Bearer ${token}` even when `getToken()`
 * (auth.ts:42-47) resolved to an empty string (the Docker Compose password-mode
 * pre-login state) — sending a literal `Authorization: Bearer `. This is
 * the single implementation all three (and any future caller — see #272 /
 * #274 / #280) import instead.
 *
 * `friendlyErrorMessage` centralizes the "no raw endpoint/HTTP-code
 * strings, no tenant-brand strings in rendered output" requirement: callers pass
 * the full technical detail (which is logged to the console for
 * debugging) and a user-safe fallback string; only the fallback — or a
 * legitimate server-supplied `detail` message read via `readErrorDetail`
 * — ever reaches the DOM.
 */
import { getToken } from './auth';

function resolveApiBase(): string {
  return (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';
}

/**
 * Shared fetch wrapper for authenticated API calls. Adds `Authorization:
 * Bearer <token>` when `getToken()` resolves a non-empty token; when it
 * resolves an empty string (no session yet, or password mode — issue #468
 * — where the session is a cookie, not a token this module ever holds),
 * the header is omitted entirely rather than sending `Authorization:
 * Bearer `. `credentials: 'same-origin'` is always set so the browser
 * attaches the password-mode session cookie on same-origin requests
 * (nginx reverse-proxies /api/ same-origin — see deploy/dts/nginx.conf);
 * it is a no-op for the Cognito/Amplify (sso) path, which authenticates via
 * the Authorization header instead and sets no cookie of its own.
 */
/**
 * Session expiry, handled once (issue #487).
 *
 * The session has a TTL, and nothing intercepted a 401. When it expired
 * mid-session every panel independently started failing with whatever copy
 * its own error path produced -- "We couldn't load the users list", "We
 * couldn't load your playbooks" -- and none of them said the one thing that
 * was actually true and actionable: you are signed out.
 *
 * This is a module-level notifier rather than a React context because
 * `authorizedFetch` is called from modules that are not components (and from
 * inside callbacks that have no hook access). One subscriber -- the app shell
 * -- listens; everything else keeps calling `authorizedFetch` exactly as
 * before and needs no change.
 */
type SessionExpiredListener = () => void;

const sessionExpiredListeners = new Set<SessionExpiredListener>();

/** Subscribe to "the server rejected our credentials". Returns an unsubscribe. */
export function onSessionExpired(listener: SessionExpiredListener): () => void {
  sessionExpiredListeners.add(listener);
  return () => {
    sessionExpiredListeners.delete(listener);
  };
}

/** Test seam. Production code never calls this. */
export function __resetSessionExpiredListeners(): void {
  sessionExpiredListeners.clear();
}

export async function authorizedFetch(path: string, init?: RequestInit): Promise<Response> {
  const token = await getToken();
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string> | undefined) };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const response = await fetch(`${resolveApiBase()}${path}`, {
    ...init,
    headers,
    credentials: 'same-origin',
  });
  // A 401 from the LOGIN route is "wrong password", not "session expired", and
  // it must not bounce the user to a screen they are already on. The
  // distinction is the route, not the status: everything else behind
  // `authorizedFetch` is a route that only an authenticated caller reaches.
  if (response.status === 401 && !isUnauthenticatedRoute(path)) {
    for (const listener of sessionExpiredListeners) {
      try {
        listener();
      } catch {
        // A listener that throws must not turn an expired session into an
        // unhandled rejection inside every caller's fetch.
      }
    }
  }
  return response;
}

/** Routes a signed-OUT caller legitimately hits, where 401 means "no". */
function isUnauthenticatedRoute(path: string): boolean {
  return path.startsWith('/api/auth/login') || path.startsWith('/api/auth/password');
}

/**
 * Log the full technical detail (endpoint, HTTP status, stack, whatever
 * the caller has) to the console for debugging, and return a user-safe
 * fallback string for rendering. Rendered output must never carry raw
 * endpoint paths or HTTP status codes.
 */
export function friendlyErrorMessage(technicalDetail: unknown, fallback: string): string {
  // eslint-disable-next-line no-console
  console.error(technicalDetail);
  return fallback;
}

/** Read a JSON error body's string `detail` field, if present. */
export async function readErrorDetail(response: Response): Promise<string | undefined> {
  const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
  return typeof body.detail === 'string' ? body.detail : undefined;
}

/**
 * The one friendly message for every "prepare a download" failure — the
 * Review panel's manual click, its automatic save on completion, and
 * History's per-row redline/input buttons (issue #466). Unlike most routes,
 * an output/input-download 503's `detail` is server *configuration* — an
 * unset storage env var, the very shape of bug #465 fixed — not something a
 * reviewer should ever read, so `readErrorDetail`'s result must never reach
 * this family of banners the way it legitimately does for other endpoints.
 * Nor is it copy that promises a retry will help: a mis-configured bucket
 * does not fix itself on a second click, and this deliberately says so
 * without either "Please try again" or the raw technical string.
 */
export const DOWNLOAD_ERROR_COPY =
  "We couldn't prepare the download. The review itself finished and is safe — " +
  "an admin needs to check the deployment's storage settings.";

/**
 * Log `technicalDetail` (the server's raw `detail`, or a status-line
 * fallback) to the console for an operator, and return the fixed
 * `DOWNLOAD_ERROR_COPY` for rendering — never the detail itself. Every
 * download-failure call site should route through this instead of composing
 * its own message, so the three surfaces above can never drift on wording.
 */
export function friendlyDownloadError(technicalDetail: unknown): string {
  return friendlyErrorMessage(technicalDetail, DOWNLOAD_ERROR_COPY);
}

/**
 * Hand a same-tab-safe URL to the browser for download via a temporary
 * anchor, instead of `window.location.assign` (which navigates the SPA
 * away and loses in-memory app state — issue #271 item 5). The anchor is
 * appended, clicked, and removed synchronously; it is never visible.
 */
export function triggerBrowserDownload(url: string): void {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = '';
  anchor.rel = 'noopener';
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
}
