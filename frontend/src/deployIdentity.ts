/**
 * deployIdentity — what is ACTUALLY deployed, and whether the two halves of
 * the deployment agree about it (issue #652, epic #649).
 *
 * ## Why this module exists
 *
 * `GET /version` is ADVISORY ONLY. Issue #613 root-caused, with build logs,
 * a cache-key bug on the build runner: when a commit range touches no backend
 * files, the backend image's `ARG VERSION` / `ENV VERSION` layers ride along
 * as cache hits even though the `--build-arg` value genuinely changed, so the
 * baked stamp stays pinned to an OLDER commit while the frontend bundle is
 * genuinely new. Observed twice on 2026-08-23. That bug is still open; this
 * module does not fix it, it makes it VISIBLE.
 *
 * The check that does work today is manual: snapshot the `index-<hash>.js`
 * asset name before a deploy and diff it after (`index-CzHcT69l.js` →
 * `index-BN3-dr-7.js` verified the 2026-09-01 deploy). It works because the
 * asset name is derived from the bundle's own CONTENT — no build arg, no
 * cache key, nothing a stale layer can fake. This module turns that ritual
 * into something the screen does.
 *
 * ## The two identities, and why only one of them can lie
 *
 *   - BACKEND stamp — `VERSION` / `COMMIT_SHA` / `IMAGE_DIGEST`, baked into
 *     the image as ENV at build time and reported by `GET /version`
 *     (backend/src/main.py). Trustworthy only as far as #613 allows.
 *   - FRONTEND stamp — `VITE_BUILD_VERSION` / `VITE_COMMIT_SHA`, baked into
 *     the bundle by `deploy/dts/frontend.Dockerfile` at build time, plus the
 *     ASSET FINGERPRINT: the running entry chunk's own file name, which is a
 *     content hash and therefore cannot go stale while the content changes.
 *
 * ## What "agree" means here
 *
 * Commit equality, NOT version-string equality. `.github/workflows/dts-image-
 * publish.yml` builds the two images as separate matrix legs, each computing
 * its own `<short-sha>-$(date -u +%Y%m%d%H%M%S)`: two legs of the SAME run
 * legitimately carry timestamps seconds apart, so comparing version strings
 * would report a disagreement on every healthy deploy. The short SHA is the
 * half both legs share.
 *
 * The build timestamps are still parsed — but only to give a DIRECTION once
 * the commits already differ, which is what separates the #613 condition
 * (backend older than the frontend bundle: the deploy shipped, the backend
 * stamp did not move) from its mirror image (a frontend leg that cache-hit
 * while the backend genuinely rebuilt — different, milder, and not #613).
 *
 * Nothing here is derived from a clock, a session, or an uptime: two calls in
 * one deployment return the same answer, and only a genuinely new image
 * changes it. That is what makes the fingerprint survive a plain restart and
 * move across a real deploy.
 */

// ---------------------------------------------------------------------------
// Build timestamp parsing (moved here from App.tsx, issue #603 → #652).
//
// The publish workflow tags each image `<short-sha>-$(date -u +%Y%m%d%H%M%S)`
// and bakes that string in as VERSION, so the build time is ALREADY in the
// string the footer shows — it just isn't legible. Parsing it here rather
// than adding a `built_at` field to /version is deliberate: the backend
// payload would then have to be plumbed through every deployment target's
// compose/env, and issue #469's landmine (restated by #613) is precisely that
// VERSION/COMMIT_SHA/IMAGE_DIGEST must stay EMPTY in
// deploy/dts/docker-compose.coolify.yml so the image's baked-in ENV wins. A
// parser adds no deploy coupling at all. (A real `built_at`, independent of
// the tag, remains a sensible follow-up if one is ever needed.)
//
// The stamp is UTC at the source, and is rendered as UTC — not converted to
// the viewer's local zone. It is a deploy-verification signal read against
// `docker image ls` output and workflow logs, which are all UTC; making the
// one human-readable copy of that instant disagree with them by an offset
// would be worse, not friendlier. It also keeps the rendering deterministic
// rather than dependent on the runner's TZ.
// ---------------------------------------------------------------------------
const BUILD_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * The instant `cb79b4a-20260820191617` encodes, or null when the version
 * string carries no timestamp suffix (VERSION defaults to `dev`) or carries a
 * nonsense one. Null means "we do not know when this was built", never a
 * silently rolled-over date.
 */
export function buildDateFromVersion(version: string | null | undefined): Date | null {
  const match = /-(\d{14})$/.exec(version ?? '');
  if (!match) {
    return null;
  }
  const digits = match[1];
  const [year, month, day, hour, minute, second] = [
    digits.slice(0, 4),
    digits.slice(4, 6),
    digits.slice(6, 8),
    digits.slice(8, 10),
    digits.slice(10, 12),
    digits.slice(12, 14),
  ].map(Number);

  const date = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  // Date.UTC does not reject out-of-range parts, it ROLLS THEM OVER: month 13
  // silently becomes January of the next year, day 32 becomes the 1st. A
  // round-trip comparison is what turns "parsed something" into "parsed this".
  const roundTrips =
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day &&
    date.getUTCHours() === hour &&
    date.getUTCMinutes() === minute &&
    date.getUTCSeconds() === second;
  return roundTrips ? date : null;
}

/**
 * "20 Aug 2026, 19:16 UTC" for `cb79b4a-20260820191617`, or null when the
 * version string carries no usable timestamp suffix. Null means "render no
 * date", never "Invalid Date". Re-exported from App.tsx, where the footer
 * that first needed it lives (issue #603).
 */
export function buildTimestampFromVersion(version: string | null | undefined): string | null {
  const date = buildDateFromVersion(version);
  if (date === null) {
    return null;
  }
  const pad = (n: number): string => String(n).padStart(2, '0');
  return (
    `${date.getUTCDate()} ${BUILD_MONTHS[date.getUTCMonth()]} ${date.getUTCFullYear()}, ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())} UTC`
  );
}

// ---------------------------------------------------------------------------
// Stamps
// ---------------------------------------------------------------------------

/**
 * The placeholders an UNSTAMPED build degrades to. These are the literal
 * defaults in `deploy/dts/backend.Dockerfile` / `frontend.Dockerfile` and in
 * `backend/src/main.py`'s `os.environ.get` fallbacks — a build that carried
 * no `--build-arg` reports one of these, and it must never be mistaken for a
 * real identity that happens to match on the other side.
 */
const UNSTAMPED = new Set(['', 'dev', 'unknown', 'null', 'undefined', 'none']);

/** '' when the value is absent or one of the unstamped placeholders. */
export function stampValue(raw: string | null | undefined): string {
  const trimmed = (raw ?? '').trim();
  return UNSTAMPED.has(trimmed.toLowerCase()) ? '' : trimmed;
}

/** What `GET /version` reports, normalised. '' means "not stamped". */
export interface BackendStamp {
  version: string;
  commit: string;
  imageDigest: string;
}

/** What the running bundle knows about itself. */
export interface FrontendStamp {
  version: string;
  commit: string;
  /**
   * The running entry chunk's own file name, e.g. `index-BN3-dr-7.js` — the
   * content hash the manual pre/post-deploy diff has always used. null on a
   * dev server, where there is no hashed bundle to name.
   */
  assetFingerprint: string | null;
}

export function backendStamp(payload: {
  version?: string | null;
  commit?: string | null;
  image_digest?: string | null;
}): BackendStamp {
  return {
    version: stampValue(payload.version),
    commit: stampValue(payload.commit),
    imageDigest: stampValue(payload.image_digest),
  };
}

/**
 * The built asset's own file name from the URL it was served at, or null when
 * the URL is not a built asset.
 *
 * Vite emits every chunk under `build.assetsDir` (default `assets/`) with a
 * content hash in the name, and `deploy/dts/nginx.conf` serves exactly that
 * prefix (`location /assets/`). Anything else — a dev server's
 * `/src/main.tsx`, a bare `/index.html`, a source file under vitest — is not
 * a fingerprint and must report as absent rather than as a wrong answer.
 *
 * The WHOLE file name is the fingerprint, not the hash prised out of it: the
 * hash itself contains `-` (`index-BN3-dr-7.js`), so splitting it back out is
 * a parser waiting to be wrong, and the file name is what an operator already
 * compares against `curl`/devtools output.
 */
export function assetFingerprintFromUrl(url: string | null | undefined): string | null {
  if (!url) {
    return null;
  }
  // Deliberately string surgery, not `new URL()`: the value may be a bare
  // path (`/assets/index-BN3-dr-7.js`) from a `src` attribute, which `new
  // URL()` rejects without a base.
  const withoutQuery = url.split(/[?#]/)[0];
  const lastSlash = withoutQuery.lastIndexOf('/');
  if (lastSlash < 0) {
    return null;
  }
  const dir = withoutQuery.slice(0, lastSlash);
  const file = withoutQuery.slice(lastSlash + 1);
  if (!file || !dir.endsWith('/assets')) {
    return null;
  }
  return file;
}

/**
 * The URL the running bundle was served from.
 *
 * `import.meta.url` is the direct answer in a real build — Vite/Rollup leave
 * it in the ESM output, where it resolves to the chunk's own URL. Under
 * vitest and on the dev server it resolves to a source file instead, which
 * `assetFingerprintFromUrl` correctly rejects; the document's module `<script
 * src>` is then the fallback, because `frontend/index.html` ships exactly one
 * (`<script type="module" ... src="/assets/index-<hash>.js">`, written by
 * `vite build`).
 */
function runningBundleUrl(): string | null {
  const own = typeof import.meta.url === 'string' ? import.meta.url : null;
  if (assetFingerprintFromUrl(own) !== null) {
    return own;
  }
  if (typeof document === 'undefined') {
    return null;
  }
  return document.querySelector('script[type="module"][src]')?.getAttribute('src') ?? null;
}

/**
 * What this bundle knows about itself.
 *
 * `assetUrl` is a parameter only so the derivation can be driven directly in
 * tests with real Vite-shaped URLs; production always takes the default.
 */
export function frontendStamp(assetUrl: string | null = runningBundleUrl()): FrontendStamp {
  const env = import.meta.env as Record<string, string | undefined>;
  return {
    version: stampValue(env.VITE_BUILD_VERSION),
    commit: stampValue(env.VITE_COMMIT_SHA),
    assetFingerprint: assetFingerprintFromUrl(assetUrl),
  };
}

// ---------------------------------------------------------------------------
// Agreement
// ---------------------------------------------------------------------------

export type AgreementStatus = 'agree' | 'disagree' | 'unknown';

export type AgreementKind =
  /** Both stamped with the same commit. */
  | 'same-commit'
  /** THE #613 CONDITION: the backend stamp is older than the frontend bundle. */
  | 'backend-stale'
  /** The mirror case: the frontend leg cache-hit while the backend rebuilt. */
  | 'frontend-stale'
  /** Commits differ and neither carries a usable build time to order them by. */
  | 'unordered'
  /** At least one side carries no stamp at all. */
  | 'unstamped';

export interface DeployAgreement {
  status: AgreementStatus;
  kind: AgreementKind;
  /** One line an operator can act on. */
  headline: string;
  /** Why we say that, and what to do about it. */
  detail: string;
}

/**
 * The shortest short-SHA either side is allowed to be compared on. The
 * publish workflow uses 7 (`${GITHUB_SHA::7}`) and the AWS pipeline uses
 * `git rev-parse --short HEAD`; anything shorter than 7 is not a commit
 * identifier and must not be allowed to produce a match by prefix.
 */
const MIN_SHA_LENGTH = 7;

/** Case-insensitive short-SHA comparison on the shorter of the two. */
export function commitsMatch(a: string, b: string): boolean {
  const shortest = Math.min(a.length, b.length);
  if (shortest < MIN_SHA_LENGTH) {
    return false;
  }
  return a.slice(0, shortest).toLowerCase() === b.slice(0, shortest).toLowerCase();
}

/**
 * Do the backend stamp and the frontend bundle agree about what is deployed?
 *
 * This is the whole point of the screen: an implementation that prints both
 * values side by side looks right and answers nothing, because the #613
 * condition is exactly two plausible-looking values that disagree.
 */
export function deployAgreement(
  frontend: FrontendStamp,
  backend: BackendStamp,
): DeployAgreement {
  // Re-normalised here, not merely trusted. `frontendStamp`/`backendStamp`
  // already strip the unstamped placeholders, but this function is the one
  // that decides whether two builds are the same — and a raw "unknown" on
  // both sides reaching it would be read as a MATCH, turning the one case
  // where nothing is known into the one that looks healthiest.
  const frontendCommit = stampValue(frontend.commit);
  const backendCommit = stampValue(backend.commit);

  if (!frontendCommit || !backendCommit) {
    const missing = !frontendCommit && !backendCommit
      ? 'Neither the app bundle nor the server'
      : !frontendCommit
        ? 'The app bundle'
        : 'The server';
    return {
      status: 'unknown',
      kind: 'unstamped',
      headline: 'Cannot be checked on this build',
      detail:
        `${missing} was built with a commit stamp, so there is nothing to compare and this ` +
        'screen will not guess. That is normal for a local or hand-built image; a published ' +
        'image gets its stamp from the build (VERSION / COMMIT_SHA build arguments). Until ' +
        'both sides are stamped, the bundle fingerprint below is the only reliable evidence ' +
        'of what shipped.',
    };
  }

  if (commitsMatch(frontendCommit, backendCommit)) {
    return {
      status: 'agree',
      kind: 'same-commit',
      headline: 'Server and app bundle are from the same commit',
      detail:
        'Both halves of the deployment report the same commit, so what you are looking at is ' +
        'what shipped. The two build times may differ by a few seconds — the images are built ' +
        'as separate jobs — and that is expected, not drift.',
    };
  }

  const frontendBuiltAt = buildDateFromVersion(stampValue(frontend.version));
  const backendBuiltAt = buildDateFromVersion(stampValue(backend.version));

  if (backendBuiltAt !== null && frontendBuiltAt !== null && backendBuiltAt < frontendBuiltAt) {
    return {
      status: 'disagree',
      kind: 'backend-stale',
      headline: 'The server is reporting an older build than the app bundle',
      detail:
        'The app bundle is newer than the version the server reports, which is the known ' +
        'stale-stamp condition (issue #613): when a change touches no server files, the image ' +
        'build reuses a cached layer and keeps the previous commit baked in, so the server ' +
        'under-reports itself. Trust the bundle fingerprint below over the server version ' +
        'here, and confirm against the publish job for this commit before concluding the ' +
        'server code is actually old.',
    };
  }

  if (backendBuiltAt !== null && frontendBuiltAt !== null && frontendBuiltAt < backendBuiltAt) {
    return {
      status: 'disagree',
      kind: 'frontend-stale',
      headline: 'The app bundle is from an older build than the server',
      detail:
        'The server reports a newer commit than the app bundle carries. This is not the ' +
        'stale-stamp condition on the server side (issue #613) — it points the other way: the ' +
        'frontend image was reused rather than rebuilt, or the browser is holding an older ' +
        'bundle. Reload without cache first; if the fingerprint below does not change, the ' +
        'frontend image is the one that did not move.',
    };
  }

  return {
    status: 'disagree',
    kind: 'unordered',
    headline: 'Server and app bundle report different commits',
    detail:
      'The two halves of the deployment do not name the same commit, and at least one of them ' +
      'carries no build time, so they cannot be put in order here. Treat what is deployed as ' +
      'unconfirmed until the publish job for both images is checked.',
  };
}
