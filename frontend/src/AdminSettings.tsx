/**
 * AdminSettings — the Settings tab (issue #650, epic #649).
 *
 * ## What this screen is
 *
 * Epic #649's through-line: **the UI should be able to answer "why is the
 * product behaving this way?" without reading source code or opening the
 * deploy host.** This tab is the home for the operator-facing facts that
 * have nowhere else to live — secret rotation without read-back (#651),
 * what is actually deployed and whether the backend stamp and the frontend
 * bundle agree (#652), and the spend cap / spend today / cost of the next
 * review (#653). Those have landed.
 *
 * What it shipped with is the part that cost a full debugging cycle on
 * 2026-09-01: **deployment capabilities** — the read-only explanation of a
 * capability the deployment has gated off, so the Review tab's `Internal
 * (unavailable)` / `Both (unavailable)` stops stop being a three-way guess
 * between "not built", "misconfigured" and "deliberately gated".
 *
 * ## Secrets (#651)
 *
 * The second table is the **secrets inventory**: for each secret this
 * deployment runs on, whether it is set, where it is set, a non-reversible
 * fingerprint, when it was last set and by whom, and how to rotate it.
 *
 * It is a read-only INVENTORY, not a rotation form. The write half of #651
 * already exists on the Models tab (`AdminModel.tsx` → POST
 * /api/admin/model-key), and duplicating a credential input on a second
 * screen would mean two places to keep write-only. So this screen shows the
 * facts and NAMES the screen that rotates — the same "explain, never
 * toggle" line the capability table draws.
 *
 * The load-bearing property, from #651: **nothing here is derived from a
 * secret's characters.** `key_fingerprint` is a salted SHA-256 prefix
 * computed server-side (backend/src/model_settings.py::secret_fingerprint);
 * there is no hint, no mask, no preview and no reveal, and none may be added
 * later. A masked field that still ships the real value to the browser is
 * the exact failure mode the ticket exists to remove.
 *
 * ## What is deployed (#652)
 *
 * The first table answers "did my change actually ship?" — in one glance,
 * instead of by diffing bundle hashes by hand.
 *
 * It does NOT just print two version numbers side by side, because that is
 * precisely the failure it exists to catch. Issue #613 root-caused, from
 * build logs, a builder cache-key bug that leaves the backend image's baked
 * `VERSION`/`COMMIT_SHA` pinned to an OLDER commit whenever a change touches
 * no backend files — so `GET /version` reports a plausible, wrong answer
 * while the frontend bundle is genuinely new. Observed twice on 2026-08-23.
 * Two believable numbers that disagree LOOK fine; the whole value here is
 * saying so out loud.
 *
 * The comparison itself is `deployIdentity.ts::deployAgreement` — commit
 * equality, with the build times used only to say WHICH side is behind once
 * the commits already differ. See that module for why version strings are
 * not compared directly (the two images are built as separate matrix legs
 * and legitimately carry timestamps seconds apart).
 *
 * The bundle fingerprint — the running entry chunk's own hashed file name —
 * is the one value on this screen that no cached build layer can fake, and
 * it is also rendered on the SIGN-IN screen (PasswordLogin.tsx), because a
 * deploy should be checkable before a session is spent proving it landed.
 *
 * The image digest is shown when the deployment reports one and says
 * "unreported" otherwise. It is deliberately never synthesised here: the
 * digest is the digest of the PUSHED manifest, so the build cannot bake it
 * in, and issues #469/#613's landmine is that
 * `deploy/dts/docker-compose.coolify.yml` must keep passing
 * VERSION/COMMIT_SHA/IMAGE_DIGEST through EMPTY so the image's own ENV wins.
 * This screen reads what the container knows about itself; it does not
 * re-arm that.
 *
 * ## Spend (#653)
 *
 * The cap, what today has committed against it, what is left, and what the
 * next review will cost — with the cap itself EDITABLE, so a ceiling can be
 * moved without editing a secret-bearing prod compose file (epic #649's own
 * evidence).
 *
 * Two figures for the next review, not one, because they answer different
 * questions and the gap between them is large: a review RESERVES its worst
 * case (every retry at the full input and output ceilings) and is refused
 * against that, while it is EXPECTED to cost far less — measured at $0.2742
 * actual against a $1.10 worst case on 2026-09-01, the measurement #653 was
 * filed on. Printing only the reservation would tell an operator their cap
 * buys a quarter of the reviews it actually buys; printing only the expected
 * cost would promise reviews the reservation gate then declines. So the
 * "room for N more reviews" count is computed from the WORST case — the
 * number that can actually refuse a submission.
 *
 * `Left today` is the cap less what is COMMITTED (`reserved_usd_cents`),
 * which is exactly what `reviews.reserve_spend`'s condition compares against.
 * `Billed today` is shown beside it rather than folded into it: preflight and
 * cover-note spend settle with no reservation behind them, so the two
 * counters answer different questions and averaging them would produce a
 * number matching neither gate. See `backend/src/admin_dashboard.py::
 * _spend_day_view`.
 *
 * ## Our own legal entities (#678)
 *
 * The second EDITABLE thing on this screen (the spend cap above is the
 * other): the deployment's roster of our own
 * legal entity names. We are ~25 legal entities, any of which can be the
 * contracting party, and on third-party paper the counterparty drafts with
 * whichever name it was given — a subsidiary, a former name, a d/b/a. At
 * review time the roster is unioned with the governing playbook's own
 * `perspective.party` into ONE flat recognition set, so a document naming
 * any of them is a document naming us.
 *
 * It is edited here and not in a playbook because it is organisation-scoped:
 * identical for every playbook we will ever build, and changing over time
 * (acquisitions, dissolutions, renames). Copied into each artifact instead,
 * the affiliation playbook would know 25 entities and an older NDA playbook
 * 22 — and the same counterparty on the same document would be recognised
 * under one agreement type and not another. It is not an environment
 * variable because the first missing entity will be found in production,
 * where a redeploy is the wrong unit of repair.
 *
 * Editable is not a contradiction of the "explains, never toggles" line
 * below: that line is about deployment CAPABILITIES, whose fail-safe
 * direction depends on their being unreachable from inside the running app.
 * This is business data with no fail-safe direction to protect — an empty
 * roster simply narrows recognition back to the playbook's own party.
 *
 * ## What this screen is NOT, deliberately
 *
 * **It is not a feature-switch registry, and no CAPABILITY is toggled here.**
 * The owner decision on #650 (2026-09-01) dropped the switch registry this
 * ticket was originally filed for:
 *
 *   > the notes are optional on the main review screen so we don't need a
 *   > setting for this under Settings pane and we can just leave the feature
 *   > flag in compose file forever.
 *
 * `NOTES_MODE_ENABLED` is a deploy-time capability gate, not an operator
 * control. The *per-review* choice already lives where it belongs — the
 * four-way footnote control on the Review tab — and a second place to
 * express the same intent is exactly the clutter that decision refuses. It
 * also keeps issue #572's fail-safe property for free: a flag that can only
 * be set by the deployment cannot be turned on from inside the running app.
 *
 * So this screen **explains**; it never toggles. There is no control here
 * for notes mode, and no generic environment-variable editor — the latter
 * being a remote-code-execution surface wearing a settings hat (#650's own
 * Notes).
 *
 * ## What it talks to
 *
 *   GET /api/me/preferences     →  { preferences, notes_mode_available }
 *   GET /api/admin/model-key    →  the write-only key's status + fingerprint
 *   GET /version                →  the backend image's baked-in build stamp
 *   GET /api/admin/entity-roster →  our own legal entity names (#678)
 *   PUT /api/admin/entity-roster →  replace them (whole-list save)
 *   GET /api/admin/spend?days=1  →  the ceiling in force, today's totals, what
 *                                  is left, and what the next review costs (#653)
 *   POST /api/admin/spend-cap    →  change the ceiling (null reverts to the
 *                                  deployment's own)
 *
 * `notes_mode_available` is `backend/src/config.py::notes_mode_enabled()`
 * projected to the client, and is ALREADY the one answer the Review tab's
 * control reads (`ReviewSubmission.tsx`). Reusing it — rather than adding a
 * second admin route that re-reads the same config — is deliberate: two
 * projections of one flag would eventually disagree, and this screen exists
 * precisely to be believed.
 *
 * Admin-only in the tab bar (App.tsx's `/api/me` probe, #234/#235). The
 * route it reads is a signed-in user's own preferences route, so it carries
 * no admin-only data; the panel still hides itself on a 403 and re-loads on
 * `credentialsRefreshKey` (#635), because the route is NOT exempt from
 * default-credentials rotation enforcement and would otherwise stay blank
 * for the rest of the session after a rotation.
 *
 * The load is an explicit `LoadState`, so a failed load is TERMINAL and
 * renders an error plus a working retry — never an error and a spinner at
 * once (issue #439).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { type AdminPanelRefreshProps } from './adminRefresh';
import { failedLoad, type LoadState } from './loadState';
import { authorizedFetch, friendlyErrorMessage } from './api';
import { type ModelKeySettings } from './AdminModel';
import {
  backendStamp,
  buildTimestampFromVersion,
  deployAgreement,
  frontendStamp,
  type BackendStamp,
  type DeployAgreement,
  type FrontendStamp,
} from './deployIdentity';
import {
  CtBanner,
  CtButton,
  CtCard,
  CtChip,
  CtField,
  CtProgress,
  CtTable,
  CtToolbar,
} from './ui/react';

/**
 * One row of the "what is deployed" table: a fact, and each half's answer.
 *
 * `server` / `bundle` are '' when that half has nothing to say about the
 * fact (an image digest is a server-only notion; a bundle file name is a
 * frontend-only one), and the renderer shows an em dash. `meaning` is the
 * column that keeps this from being a wall of hex — the same "explain, never
 * just print" line the capability and secrets tables draw.
 */
export interface DeployFactRow {
  id: string;
  label: string;
  server: string;
  bundle: string;
  meaning: string;
  /** Render the two value cells in the mono face (hashes, digests, names). */
  mono: boolean;
}

/**
 * The "what is deployed" table's contents. Exported so the copy and the
 * degradation cases can be asserted directly, without a mounted panel.
 */
export function deployFactRows(frontend: FrontendStamp, backend: BackendStamp): DeployFactRow[] {
  return [
    {
      id: 'commit',
      label: 'Commit',
      server: backend.commit,
      bundle: frontend.commit,
      mono: true,
      meaning:
        'The commit each half was built from. These two matching is what “agree” means above. ' +
        'Either can read as unstamped on a hand-built image, and an unstamped side is never ' +
        'treated as a match.',
    },
    {
      id: 'build-stamp',
      label: 'Build stamp',
      server: backend.version,
      bundle: frontend.version,
      mono: true,
      meaning:
        'The full tag the build baked in, commit plus build time. The two halves are built as ' +
        'separate jobs, so their stamps differ by seconds even on a healthy deploy — that is ' +
        'why the commit, not this string, decides agreement.',
    },
    {
      id: 'built-at',
      label: 'Built',
      server: buildTimestampFromVersion(backend.version) ?? '',
      bundle: buildTimestampFromVersion(frontend.version) ?? '',
      mono: false,
      meaning:
        'When each image was built, read out of its own build stamp and shown in UTC — the ' +
        'same clock as the build logs and “docker image ls”, deliberately not the local zone.',
    },
    {
      id: 'image-digest',
      label: 'Server image digest',
      server: backend.imageDigest,
      bundle: '',
      mono: true,
      meaning:
        'The digest of the image the server is actually running, when the deployment reports ' +
        'one. A digest is only knowable after the image is pushed, so no build can bake it in; ' +
        'it stays unreported unless the deployment supplies IMAGE_DIGEST, and unreported here ' +
        'is expected rather than a fault.',
    },
    {
      id: 'bundle-fingerprint',
      label: 'App bundle file',
      server: '',
      bundle: frontend.assetFingerprint ?? '',
      mono: true,
      meaning:
        'The running bundle’s own file name. It is a hash of the bundle’s CONTENT, so unlike ' +
        'every stamp above it cannot be left behind by a reused build layer — it is the value ' +
        'the manual pre/post-deploy check has always compared. It is also printed on the ' +
        'sign-in screen, so a deploy can be verified without spending a session on it.',
    },
  ];
}

/**
 * One value cell. '' is the table's "this half has nothing to say about this
 * fact" — an em dash, never a blank cell and never a guessed value.
 */
function renderDeployValue(value: string, mono: boolean): React.ReactNode {
  if (!value) {
    return '—';
  }
  return mono ? <code>{value}</code> : value;
}

/** Banner tone for each agreement outcome. Disagreement is a warning, not an
 *  error: the deployment is running, the two halves just do not line up. */
export function agreementVariant(agreement: DeployAgreement): 'ok' | 'warn' | 'muted' {
  if (agreement.status === 'agree') {
    return 'ok';
  }
  return agreement.status === 'disagree' ? 'warn' : 'muted';
}

/** What the deployment has enabled, as this screen understands it. */
export interface DeploymentCapabilities {
  /** `notes_mode_available` from GET /api/me/preferences — the #572
   *  `NOTES_MODE_ENABLED` kill switch, projected to the client. */
  internalNotes: boolean;
}

/**
 * One capability row: its state, and WHY it is in that state.
 *
 * `why` is the whole point of the row — a bare "unavailable" is the string
 * the Review tab already shows, and repeating it here would answer nothing.
 * Both branches are written out, because "available" also needs to say where
 * the per-review choice actually lives so nobody comes looking for a toggle.
 */
export interface CapabilityRow {
  id: string;
  name: string;
  enabled: boolean;
  /** Chip text. Deliberately says who decided, not just yes/no. */
  state: string;
  why: string;
}

/**
 * The capability table's contents, derived from what the backend reported.
 * Exported so the copy can be asserted directly, without a mounted panel.
 *
 * The `NOTES_MODE_ENABLED` prose names the backend service's own
 * `environment:` block rather than the deploy host's environment-variable
 * list: on this deployment the compose declares environment inline per
 * service, and the host's variable list does not inject into it — the exact
 * dead end that cost the #648 attempt (see epic #649's evidence section).
 */
export function capabilityRows(capabilities: DeploymentCapabilities): CapabilityRow[] {
  return [
    {
      id: 'internal-notes',
      name: 'Internal-audience footnotes',
      enabled: capabilities.internalNotes,
      state: capabilities.internalNotes ? 'Enabled by this deployment' : 'Not enabled by this deployment',
      why: capabilities.internalNotes
        ? 'This deployment allows internal notes, so “Internal” and “Both” can be chosen under ' +
          '“Review”. Which footnotes a given review carries is a per-review choice made there — ' +
          'there is deliberately no switch for it on this screen.'
        : 'Internal notes are unavailable because this deployment has not enabled them — not ' +
          'because the feature is missing or misconfigured. That is why “Internal” and “Both” ' +
          'show as unavailable under “Review”, and why the server refuses a review that asks ' +
          'for either (issue #572). It is a deploy-time capability gate, so it is turned on by ' +
          'the deployment and never from inside the app: set NOTES_MODE_ENABLED=1 for the ' +
          'backend and restart it. On the Docker Compose target that means the backend ' +
          'service’s own environment: block in the compose file — the deploy host’s separate ' +
          'environment-variable list does not reach an inline environment: block (issue #649).',
    },
  ];
}

/**
 * One secret this deployment runs on, as the inventory renders it.
 *
 * `fingerprint` is the ONLY thing here derived from a secret at all, and it
 * is derived one-way: `backend/src/model_settings.py::secret_fingerprint`,
 * the first eight hex of a salted SHA-256. It is never a mask — no character
 * of the value survives into it — so it is safe on a screen, in a screenshot
 * and in a support transcript. Anything that would show part of a value
 * belongs nowhere on this screen (issue #651).
 */
export interface SecretRow {
  id: string;
  name: string;
  /** Chip text: WHERE the value is set, not merely yes/no. */
  state: string;
  stateVariant: 'ok' | 'warn' | 'danger' | 'muted';
  /** The one-way fingerprint, or '' when there is nothing to fingerprint. */
  fingerprint: string;
  /** When this app last wrote it, and by whom. '' when this app never did. */
  lastSet: string;
  /** How to rotate it — and, when it cannot be rotated here, why not. */
  rotation: string;
}

/** `updated_at` is unix seconds as a string, the shape the backend stores. */
function formatSetAt(updatedAt: string, updatedBy: string): string {
  const seconds = Number(updatedAt);
  if (!updatedAt || !Number.isFinite(seconds) || seconds <= 0) {
    return '';
  }
  const when = new Date(seconds * 1000).toLocaleString();
  return updatedBy ? `${when} by ${updatedBy}` : when;
}

/**
 * The secrets table's contents. Exported so the copy can be asserted
 * directly, without a mounted panel — the same shape as `capabilityRows`.
 *
 * `key` is null while the key status has not loaded; the row then says so
 * rather than guessing, because "not set" and "we could not ask" are
 * different answers and only one of them is an emergency.
 */
export function secretRows(key: ModelKeySettings | null): SecretRow[] {
  return [modelKeyRow(key), sessionSecretRow()];
}

function modelKeyRow(key: ModelKeySettings | null): SecretRow {
  const base = {
    id: 'model-api-key',
    name: 'Model provider API key',
  };

  if (key === null) {
    return {
      ...base,
      state: 'Unknown',
      stateVariant: 'muted' as const,
      fingerprint: '',
      lastSet: '',
      rotation: 'We could not read this key’s status just now, so nothing below is a claim about it.',
    };
  }

  const lastSet = formatSetAt(key.updated_at, key.updated_by);

  if (!key.key_store_available) {
    // No app-side store (the AWS/Bedrock target). The key may still be set in
    // the environment or not set at all, and those are different answers —
    // saying "Set by the deployment" for an absent key would be a plain lie,
    // and this screen exists to be believed.
    return {
      ...base,
      state: key.key_source === null ? 'Not set' : 'Set by the deployment',
      stateVariant: 'muted' as const,
      fingerprint: key.key_fingerprint,
      lastSet: '',
      rotation:
        'This deployment keeps no secret store inside the app, so this key is set where the ' +
        'deployment is configured and rotated by changing it there and restarting. There is ' +
        'nothing to rotate on the “Models” screen here.',
    };
  }

  if (key.key_source === 'admin') {
    return {
      ...base,
      state: 'Set in this app',
      stateVariant: 'ok' as const,
      fingerprint: key.key_fingerprint,
      lastSet,
      rotation:
        'Rotate it under “Models”: paste the new key and save, and the next review uses it — ' +
        'no redeploy. Rotation is set-new, never edit-existing: no screen and no endpoint can ' +
        'read the saved key back, so if you have lost it, generate a fresh one at ' +
        'openrouter.ai/keys rather than looking for it here. The fingerprint changing is how ' +
        'you confirm the rotation landed.',
    };
  }

  if (key.key_source === 'env') {
    return {
      ...base,
      state: 'Set by the deployment',
      stateVariant: 'warn' as const,
      fingerprint: key.key_fingerprint,
      lastSet: '',
      rotation:
        'This key comes from the deployment’s OPENROUTER_API_KEY, so rotating it there means ' +
        'editing the deployment and restarting. Saving a key under “Models” instead overrides ' +
        'it from the next review on, and can be rotated without a restart from then on.',
    };
  }

  return {
    ...base,
    state: 'Not set',
    stateVariant: 'danger' as const,
    fingerprint: '',
    lastSet: '',
    rotation:
      'No key is configured anywhere, so every review will fail until one is saved under ' +
      '“Models”.',
  };
}

/**
 * The sign-in session-signing secret.
 *
 * DELIBERATELY NOT ROTATABLE HERE (issue #651's own carve-out). The ticket
 * put it in scope only if the UI could state plainly that rotating it signs
 * everyone out; the sentence is easy, but the mechanism is not. Every
 * request verifies its session token against
 * `os.environ["DEMO_TOKEN_SECRET"]` read at verification time
 * (backend/src/demo_auth.py::_demo_token_secret), so an app-managed
 * override would put a DynamoDB read on the authentication hot path — where
 * a transient blip reading the stored value falls back to the environment
 * one and signs every user out at once, for no reason anybody could see.
 * Trading a rare planned rotation for an unplanned mass logout is a bad
 * trade, so it stays in the deployment and this row says where and what it
 * costs.
 *
 * Nothing here is read from the value: there is no fingerprint for this
 * secret because no endpoint reports one, and inventing a client-side one
 * would need the value in the browser.
 */
function sessionSecretRow(): SecretRow {
  return {
    id: 'session-signing-secret',
    name: 'Sign-in session signing secret',
    state: 'Managed by the deployment',
    stateVariant: 'muted',
    fingerprint: '',
    lastSet: '',
    rotation:
      'DEMO_TOKEN_SECRET signs the session issued at password sign-in, and the server checks ' +
      'every request against whatever the environment holds at that moment. Rotating it ' +
      'invalidates every session at once: everyone signed in — including whoever rotates it — ' +
      'is signed out immediately and has to sign in again. That is why it is rotated where ' +
      'the deployment is configured (the backend service’s own environment: block, then ' +
      'restart the stack) and never from inside the running app, where a momentary failure to ' +
      'read it would sign everybody out for no reason.',
  };
}

/**
 * GET/PUT /api/admin/entity-roster's shape (backend/src/entity_roster.py).
 *
 * `roster_store_available` false means this deployment provisioned no roster
 * store (`ENTITY_ROSTER_TABLE` unset): the panel explains that instead of
 * offering a form the server would refuse, and reviews recognise the
 * playbook's own party alone.
 */
export interface EntityRosterHistoryEntry {
  timestamp: string;
  actor: string;
  count: number;
  added: string[];
  removed: string[];
}

export interface EntityRosterSettings {
  setting_id: string;
  roster_store_available: boolean;
  entities: string[];
  max_entities: number;
  max_name_length: number;
  updated_at: string;
  updated_by: string;
  history?: EntityRosterHistoryEntry[];
}

/**
 * The textarea's text, as a list of entity names: one per line, blanks
 * dropped, duplicates removed case- and whitespace-insensitively.
 *
 * One-per-line rather than comma-separated because entity names CONTAIN
 * commas — "Acme Holdings, LLC" is one entity, and a comma split would file
 * it as two. Exported so the parse can be asserted without a mounted panel.
 *
 * The server normalises again on save (and the prompt composer deduplicates
 * once more over the union with the playbook's own party): this is the
 * courtesy pass that makes what the admin sees match what is stored, never
 * the enforcement.
 */
export function parseRosterDraft(text: string): string[] {
  const seen = new Set<string>();
  const entities: string[] = [];
  for (const line of text.split('\n')) {
    const name = line.trim();
    if (!name) {
      continue;
    }
    const identity = name.replace(/\s+/g, ' ').toLocaleLowerCase();
    if (seen.has(identity)) {
      continue;
    }
    seen.add(identity);
    entities.push(name);
  }
  return entities;
}

/** The stored list as textarea text — the inverse of `parseRosterDraft`. */
export function rosterDraftText(entities: string[]): string {
  return entities.join('\n');
}

/**
 * The spend surface (#653) — one read of GET /api/admin/spend.
 *
 * The cap arrives on the LEDGER route rather than one of its own, so the
 * ceiling and the spend it is compared against are always one answer taken at
 * one moment; only the write (POST /api/admin/spend-cap) is separate.
 */
export interface SpendCapSetting {
  cap_store_available: boolean;
  /** null when no admin value is stored — a different fact from "the cap is
   * the deployment's value", because a cleared setting keeps TRACKING the
   * deployment if that changes underneath it. */
  stored_daily_cap_usd_cents: number | null;
  env_daily_cap_usd_cents: number | null;
  default_daily_cap_usd_cents: number;
  daily_cap_usd_cents: number;
  daily_cap_source: 'admin' | 'env' | 'default';
  min_daily_cap_usd_cents: number;
  max_daily_cap_usd_cents: number;
  updated_at: string;
  updated_by: string;
}

export interface SpendLedger {
  /** The ceiling the reservation path enforces right now. */
  daily_cap_usd_cents: number;
  /** `today.reserved_usd_cents` — the one counter the cap is checked against. */
  spent_today_usd_cents: number;
  settled_today_usd_cents: number;
  remaining_usd_cents: number;
  worst_case_reservation_usd_cents: number;
  estimated_review_usd_cents: number | null;
  next_review_admissible: boolean;
  cap_setting: SpendCapSetting;
}

/**
 * A whole number of US cents rendered as dollars.
 *
 * Two decimals, exactly: every number on this panel is a ledger amount or a
 * ceiling — already integral in cents — so unlike the model picker's
 * `formatUsd` (which prices a hypothetical review from per-million rates and
 * has to widen until a cheap model shows a digit) there is no sub-cent
 * precision to preserve here, and printing some would suggest an accuracy the
 * counter does not have.
 */
export function formatUsdCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

/** Where the cap in force came from, said in words rather than as a code. */
export function capSourceNote(cap: SpendCapSetting): string {
  if (cap.daily_cap_source === 'admin') {
    return (
      'Set here. It overrides the deployment’s own cap of ' +
      `${formatUsdCents(cap.env_daily_cap_usd_cents ?? cap.default_daily_cap_usd_cents)}.`
    );
  }
  if (cap.daily_cap_source === 'env') {
    return 'Set by the deployment environment. Setting a cap here overrides it.';
  }
  return 'Nobody has set one, so this is the built-in default. Setting a cap here overrides it.';
}

/**
 * How many more reviews today's remaining budget admits.
 *
 * Measured against the WORST CASE, not the expected cost, because that is the
 * figure a submission is actually refused against — a count computed from the
 * expected cost would promise reviews the reservation gate then declines.
 * Returns null when the worst case is not a positive number, rather than
 * dividing by zero and rendering "Infinity reviews".
 */
export function reviewsRemaining(ledger: SpendLedger): number | null {
  if (!(ledger.worst_case_reservation_usd_cents > 0)) {
    return null;
  }
  return Math.floor(ledger.remaining_usd_cents / ledger.worst_case_reservation_usd_cents);
}

/**
 * A dollars-typed cap turned into whole cents, or null when the text is not a
 * cap at all.
 *
 * The field takes dollars because that is how an operator holds the number;
 * the API takes cents because that is what the ledger counts in. Rejecting
 * here rather than posting a NaN is what keeps a typo from CLEARING the cap:
 * `null` on the wire MEANS "revert to the deployment's", so an accidental
 * empty POST would be a silent cap change rather than a validation error.
 */
export function parseCapDollars(text: string): number | null {
  const trimmed = text.trim().replace(/^\$/, '');
  if (!trimmed || !/^\d+(\.\d{1,2})?$/.test(trimmed)) {
    return null;
  }
  return Math.round(Number(trimmed) * 100);
}

/** The editable field's text for a cap already in force. */
export function capDraftText(cents: number): string {
  return (cents / 100).toFixed(2);
}

export default function AdminSettings({
  credentialsRefreshKey = 0,
}: AdminPanelRefreshProps = {}): React.ReactElement | null {
  const [load, setLoad] = useState<LoadState<DeploymentCapabilities>>({ status: 'loading' });
  // The secrets inventory loads SEPARATELY (issue #651): it reads an
  // admin-only route, and a failure there must not blank the capability table
  // that answers a different question.
  const [secretsLoad, setSecretsLoad] = useState<LoadState<ModelKeySettings>>({
    status: 'loading',
  });
  // A 403 from a route is the sole signal to hide this panel — no
  // client-side "am I an admin" claim to keep in sync or spoof. Cleared on a
  // 2xx so a post-rotation reload un-hides it (#635).
  //
  // ONE LATCH PER ROUTE, deliberately. A single shared flag would be decided
  // by whichever of the two loads resolved last: the capability route is a
  // signed-in user's own preferences (it answers 200 to anybody), so it would
  // routinely clear a 403 the admin-only secrets route had just set, and the
  // panel would render for a non-admin. Hiding when EITHER route refuses
  // keeps #635's "track the server's current answer" property per route.
  const [isCapabilitiesForbidden, setIsCapabilitiesForbidden] = useState(false);
  const [isSecretsForbidden, setIsSecretsForbidden] = useState(false);
  const [isDeployForbidden, setIsDeployForbidden] = useState(false);
  const [isRosterForbidden, setIsRosterForbidden] = useState(false);
  const [isSpendForbidden, setIsSpendForbidden] = useState(false);
  const isForbidden =
    isCapabilitiesForbidden ||
    isSecretsForbidden ||
    isDeployForbidden ||
    isRosterForbidden ||
    isSpendForbidden;

  // Our own legal entities (#678) — the one editable thing here, and so the
  // only load on this screen with a draft, a save and a save error of its
  // own. Its own LoadState and its own 403 latch, for the same reason every
  // other panel has them: a failure to read one table must not blank another
  // that answers a different question.
  const [rosterLoad, setRosterLoad] = useState<LoadState<EntityRosterSettings>>({
    status: 'loading',
  });
  const [rosterDraft, setRosterDraft] = useState('');
  const [rosterSaving, setRosterSaving] = useState(false);
  const [rosterSaveError, setRosterSaveError] = useState('');
  const [rosterSaved, setRosterSaved] = useState(false);
  const [rosterCopied, setRosterCopied] = useState(false);
  const [rosterFilterQuery, setRosterFilterQuery] = useState('');
  const rosterFileInputRef = useRef<HTMLInputElement | null>(null);

  const copyRoster = useCallback(() => {
    if (!rosterDraft) return;
    void navigator.clipboard.writeText(rosterDraft).then(() => {
      setRosterCopied(true);
      setTimeout(() => setRosterCopied(false), 2000);
    });
  }, [rosterDraft]);

  const exportRosterCsv = useCallback(() => {
    const blob = new Blob([rosterDraft], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'legal-entities.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [rosterDraft]);

  const importRosterCsv = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (event) => {
      const text = event.target?.result;
      if (typeof text === 'string') {
        setRosterDraft(text);
        setRosterSaved(false);
      }
    };
    reader.readAsText(file);
    e.target.value = '';
  }, []);

  const draftLines = useMemo(() => {
    return rosterDraft
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
  }, [rosterDraft]);

  const distinctDraftEntities = useMemo(() => {
    return parseRosterDraft(rosterDraft);
  }, [rosterDraft]);

  const duplicateLineCount = draftLines.length - distinctDraftEntities.length;

  // Spend (#653) — the second editable thing here, and the only one whose
  // value can refuse a submission. Its own LoadState, its own 403 latch and
  // its own draft, for the same reason the roster has them.
  const [spendLoad, setSpendLoad] = useState<LoadState<SpendLedger>>({ status: 'loading' });
  const [capDraft, setCapDraft] = useState('');
  const [capSaving, setCapSaving] = useState(false);
  const [capSaveError, setCapSaveError] = useState('');
  const [capSaved, setCapSaved] = useState(false);

  // The deploy stamp loads SEPARATELY too (#652), for the same reason the
  // secrets inventory does: it answers a different question off a different
  // route, and a failure to read one must not blank the other two.
  //
  // It re-reads GET /version rather than taking App.tsx's polled copy as a
  // prop: this panel's contract is that every table on it has its own
  // explicit LoadState and its own place in the one "Try again", and a
  // prop-fed value would have neither. Same route, same shape — so unlike
  // two DIFFERENT routes projecting one flag, the two readers here cannot
  // drift apart.
  const [deployLoad, setDeployLoad] = useState<LoadState<BackendStamp>>({ status: 'loading' });

  // What the running bundle knows about ITSELF. Not fetched and not state:
  // it is baked in at build time and read off the loaded chunk's own URL, so
  // it is the same value on every render of a given deployment.
  const frontend = frontendStamp();

  const loadCapabilities = useCallback(async () => {
    try {
      const response = await authorizedFetch('/api/me/preferences');
      if (response.status === 403) {
        setIsCapabilitiesForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/me/preferences returned HTTP ${response.status}`,
            "We couldn't load this deployment's settings. Please try again.",
          ),
        );
      }
      const body = (await response.json()) as { notes_mode_available?: unknown };
      setIsCapabilitiesForbidden(false);
      // Strict `=== true`: anything else — absent, null, the string "false"
      // from a proxy that stringified the body — means "not enabled", which
      // is the fail-safe direction for a #572-gated capability.
      setLoad({ status: 'ready', data: { internalNotes: body.notes_mode_available === true } });
    } catch (err) {
      setLoad(failedLoad(err, "We couldn't load this deployment's settings. Please try again."));
    }
  }, []);

  const loadSecrets = useCallback(async () => {
    try {
      const response = await authorizedFetch('/api/admin/model-key');
      if (response.status === 403) {
        setIsSecretsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/model-key returned HTTP ${response.status}`,
            "We couldn't load this deployment's secrets. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as ModelKeySettings;
      setIsSecretsForbidden(false);
      setSecretsLoad({ status: 'ready', data });
    } catch (err) {
      setSecretsLoad(
        failedLoad(err, "We couldn't load this deployment's secrets. Please try again."),
      );
    }
  }, []);

  const loadRoster = useCallback(async () => {
    try {
      const response = await authorizedFetch('/api/admin/entity-roster');
      if (response.status === 403) {
        setIsRosterForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/entity-roster returned HTTP ${response.status}`,
            "We couldn't load your legal entity names. Please try again.",
          ),
        );
      }
      const body = (await response.json()) as Partial<EntityRosterSettings>;
      // Strict `=== true` and a defensive array check: a backend older than
      // this bundle (#613's twice-observed commit skew) answers this route
      // with something else entirely, and offering a form the server will
      // refuse is worse than saying there is no store.
      const data: EntityRosterSettings = {
        setting_id: String(body.setting_id ?? 'global'),
        roster_store_available: body.roster_store_available !== false,
        entities: Array.isArray(body.entities)
          ? body.entities.filter((name): name is string => typeof name === 'string')
          : [],
        max_entities: Number(body.max_entities ?? 0),
        max_name_length: Number(body.max_name_length ?? 0),
        updated_at: String(body.updated_at ?? ''),
        updated_by: String(body.updated_by ?? ''),
        history: Array.isArray(body.history) ? body.history : [],
      };
      setIsRosterForbidden(false);
      setRosterLoad({ status: 'ready', data });
      setRosterDraft(rosterDraftText(data.entities));
      setRosterSaveError('');
      setRosterSaved(false);
    } catch (err) {
      setRosterLoad(
        failedLoad(err, "We couldn't load your legal entity names. Please try again."),
      );
    }
  }, []);

  const saveRoster = useCallback(async () => {
    setRosterSaving(true);
    setRosterSaveError('');
    setRosterSaved(false);
    try {
      const response = await authorizedFetch('/api/admin/entity-roster', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entities: parseRosterDraft(rosterDraft) }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
        // The server's own 400 detail is the useful half here (which name is
        // too long, which character is not allowed), so it is shown rather
        // than replaced — it names no endpoint and no status code (#425).
        throw new Error(
          typeof body.detail === 'string' && body.detail
            ? body.detail
            : friendlyErrorMessage(
                `PUT /api/admin/entity-roster returned HTTP ${response.status}`,
                "We couldn't save your legal entity names. Please try again.",
              ),
        );
      }
      const data = (await response.json()) as EntityRosterSettings;
      setRosterLoad({ status: 'ready', data });
      setRosterDraft(rosterDraftText(data.entities ?? []));
      setRosterSaved(true);
    } catch (err) {
      setRosterSaveError(
        err instanceof Error
          ? err.message
          : "We couldn't save your legal entity names. Please try again.",
      );
    } finally {
      setRosterSaving(false);
    }
  }, [rosterDraft]);

  const loadSpend = useCallback(async () => {
    try {
      // ONE route. The cap rides on the ledger (backend/src/admin_dashboard.py
      // ::get_spend_ledger) precisely so the ceiling and the spend it is
      // compared against can never be two answers taken at different moments.
      // `days=1` because this card is about today; the trailing window belongs
      // to the cost-ledger screen (#91) that does not exist yet.
      const response = await authorizedFetch('/api/admin/spend?days=1');
      if (response.status === 403) {
        setIsSpendForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/admin/spend returned HTTP ${response.status}`,
            "We couldn't load this deployment's spend. Please try again.",
          ),
        );
      }
      const body = (await response.json()) as {
        today?: { settled_usd_cents?: unknown };
        cap_setting?: Partial<SpendCapSetting>;
        daily_cap_usd_cents?: unknown;
        spent_today_usd_cents?: unknown;
        remaining_usd_cents?: unknown;
        worst_case_reservation_usd_cents?: unknown;
        estimated_review_usd_cents?: unknown;
        next_review_admissible?: unknown;
      };
      // Coerced field by field, the same defensiveness the roster load
      // applies: a backend older than this bundle (#613's twice-observed
      // commit skew) answers this route without the #653 fields, and a spend
      // screen rendering `undefined` as a ceiling is worse than one rendering
      // a zero it can be seen to have.
      const capBody = body.cap_setting ?? {};
      const cap: SpendCapSetting = {
        cap_store_available: capBody.cap_store_available === true,
        stored_daily_cap_usd_cents:
          typeof capBody.stored_daily_cap_usd_cents === 'number'
            ? capBody.stored_daily_cap_usd_cents
            : null,
        env_daily_cap_usd_cents:
          typeof capBody.env_daily_cap_usd_cents === 'number'
            ? capBody.env_daily_cap_usd_cents
            : null,
        default_daily_cap_usd_cents: Number(capBody.default_daily_cap_usd_cents ?? 0),
        daily_cap_usd_cents: Number(capBody.daily_cap_usd_cents ?? body.daily_cap_usd_cents ?? 0),
        daily_cap_source:
          capBody.daily_cap_source === 'admin' || capBody.daily_cap_source === 'env'
            ? capBody.daily_cap_source
            : 'default',
        min_daily_cap_usd_cents: Number(capBody.min_daily_cap_usd_cents ?? 0),
        max_daily_cap_usd_cents: Number(capBody.max_daily_cap_usd_cents ?? 0),
        updated_at: String(capBody.updated_at ?? ''),
        updated_by: String(capBody.updated_by ?? ''),
      };
      const ledger: SpendLedger = {
        daily_cap_usd_cents: Number(body.daily_cap_usd_cents ?? 0),
        spent_today_usd_cents: Number(body.spent_today_usd_cents ?? 0),
        settled_today_usd_cents: Number(body.today?.settled_usd_cents ?? 0),
        remaining_usd_cents: Number(body.remaining_usd_cents ?? 0),
        worst_case_reservation_usd_cents: Number(body.worst_case_reservation_usd_cents ?? 0),
        estimated_review_usd_cents:
          typeof body.estimated_review_usd_cents === 'number'
            ? body.estimated_review_usd_cents
            : null,
        next_review_admissible: body.next_review_admissible === true,
        cap_setting: cap,
      };
      setIsSpendForbidden(false);
      setSpendLoad({ status: 'ready', data: ledger });
      setCapDraft(capDraftText(ledger.daily_cap_usd_cents));
      setCapSaveError('');
      setCapSaved(false);
    } catch (err) {
      setSpendLoad(
        failedLoad(err, "We couldn't load this deployment's spend. Please try again."),
      );
    }
  }, []);

  const saveCap = useCallback(
    async (nextCapUsdCents: number | null) => {
      setCapSaving(true);
      setCapSaveError('');
      setCapSaved(false);
      try {
        const response = await authorizedFetch('/api/admin/spend-cap', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ daily_cap_usd_cents: nextCapUsdCents }),
        });
        if (!response.ok) {
          const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
          // The server's own 400 detail is the useful half here (which bound
          // was missed), so it is shown rather than replaced — it names no
          // endpoint and no status code (#425).
          throw new Error(
            typeof body.detail === 'string' && body.detail
              ? body.detail
              : friendlyErrorMessage(
                  `POST /api/admin/spend-cap returned HTTP ${response.status}`,
                  "We couldn't save the daily spend cap. Please try again.",
                ),
          );
        }
        // Re-read the LEDGER, not just the saved cap: "what is left today" and
        // "would the next review be admitted" are both functions of the cap, so
        // a save that did not refresh them would leave figures computed against
        // the old ceiling sitting beside the new one.
        await loadSpend();
        setCapSaved(true);
      } catch (err) {
        setCapSaveError(
          err instanceof Error
            ? err.message
            : "We couldn't save the daily spend cap. Please try again.",
        );
      } finally {
        setCapSaving(false);
      }
    },
    [loadSpend],
  );

  const loadDeploy = useCallback(async () => {
    try {
      const response = await authorizedFetch('/version');
      if (response.status === 403) {
        setIsDeployForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /version returned HTTP ${response.status}`,
            "We couldn't read what this deployment is running. Please try again.",
          ),
        );
      }
      const body = (await response.json()) as {
        version?: string | null;
        commit?: string | null;
        image_digest?: string | null;
      };
      setIsDeployForbidden(false);
      setDeployLoad({ status: 'ready', data: backendStamp(body) });
    } catch (err) {
      setDeployLoad(
        failedLoad(err, "We couldn't read what this deployment is running. Please try again."),
      );
    }
  }, []);

  useEffect(() => {
    void loadCapabilities();
    void loadSecrets();
    void loadDeploy();
    void loadRoster();
    void loadSpend();
  }, [loadCapabilities, loadSecrets, loadDeploy, loadRoster, loadSpend, credentialsRefreshKey]);

  // ONE retry for the whole screen: two independent loads, but an operator
  // pressing "Try again" means "ask the server again", not "ask it about the
  // half that failed".
  const retry = useCallback(() => {
    setLoad({ status: 'loading' });
    setSecretsLoad({ status: 'loading' });
    setDeployLoad({ status: 'loading' });
    setRosterLoad({ status: 'loading' });
    setSpendLoad({ status: 'loading' });
    void loadCapabilities();
    void loadSecrets();
    void loadDeploy();
    void loadRoster();
    void loadSpend();
  }, [loadCapabilities, loadSecrets, loadDeploy, loadRoster, loadSpend]);

  if (isForbidden) {
    return null;
  }

  return (
    <section data-testid="admin-settings-panel" className="ct-section ct-stack">
      <CtToolbar />

      {/* Permanent scope note. Says what this surface is and — just as
          importantly — what it is not, so nobody reads a read-only row as a
          control that failed to render. */}
      <CtBanner variant="muted" data-testid="admin-settings-scope-note">
        What is actually deployed, what this deployment has turned on, which secrets it runs on,
        and why the product behaves the way it does. Apart from your own legal entity names
        below, everything here is read-only: a capability below is decided by the deployment,
        not on this screen, and the copy says where it is actually set. Secrets are never
        displayed — not even partly — so this screen shows a one-way fingerprint and names the
        screen that rotates. Per-review choices — including which footnotes a redline carries —
        stay under “Review”.
      </CtBanner>

      {load.status === 'failed' && (
        <CtBanner variant="danger" data-testid="admin-settings-error">
          {load.message}
        </CtBanner>
      )}

      {secretsLoad.status === 'failed' && (
        <CtBanner variant="danger" data-testid="admin-settings-secrets-error">
          {secretsLoad.message}
        </CtBanner>
      )}

      {deployLoad.status === 'failed' && (
        <CtBanner variant="danger" data-testid="admin-settings-deploy-error">
          {deployLoad.message}
        </CtBanner>
      )}

      {rosterLoad.status === 'failed' && (
        <CtBanner variant="danger" data-testid="admin-settings-roster-error">
          {rosterLoad.message}
        </CtBanner>
      )}

      {spendLoad.status === 'failed' && (
        <CtBanner variant="danger" data-testid="admin-settings-spend-error">
          {spendLoad.message}
        </CtBanner>
      )}

      {(load.status === 'failed' ||
        secretsLoad.status === 'failed' ||
        deployLoad.status === 'failed' ||
        rosterLoad.status === 'failed' ||
        spendLoad.status === 'failed') && (
        <div className="ct-stack">
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="admin-settings-retry"
              onClick={retry}
            >
              Try again
            </CtButton>
          </div>
        </div>
      )}

      {rosterLoad.status === 'failed' ? null : rosterLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-settings-roster-loading" label="Loading legal entities…" />
      ) : (
        <CtCard data-testid="admin-settings-roster-panel">
          <CtToolbar title="Our legal entities" />
          <div className="ct-stack">
            <p data-testid="admin-settings-roster-explainer">
              Every legal entity of ours that can be the contracting party. A review recognises
              any of these names as us — together with the name the governing playbook itself
              carries, which is treated as one of them and not as the main one. Add the
              subsidiaries, the former names and the trading names a counterparty might have
              drafted against, because the one that is missing is the one that will turn up.
              This list is the same for every playbook, and a change takes effect on the next
              review.
            </p>

            {rosterSaveError && (
              <CtBanner variant="danger" data-testid="admin-settings-roster-save-error">
                {rosterSaveError}
              </CtBanner>
            )}

            {rosterSaved && (
              <CtBanner variant="ok" data-testid="admin-settings-roster-saved">
                Saved. The next review recognises {rosterLoad.data.entities.length}{' '}
                {rosterLoad.data.entities.length === 1 ? 'name' : 'names'}.
              </CtBanner>
            )}

            {(() => {
              const currentEntities = distinctDraftEntities;
              const filteredEntities = rosterFilterQuery.trim()
                ? currentEntities.filter((name) =>
                    name.toLowerCase().includes(rosterFilterQuery.trim().toLowerCase()),
                  )
                : currentEntities;

              return currentEntities.length > 0 ? (
                <div className="ct-stack" style={{ gap: '0.75rem' }}>
                  <div
                    className="ct-row"
                    style={{
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      flexWrap: 'wrap',
                      gap: '0.5rem',
                    }}
                  >
                    <div style={{ maxWidth: '320px', width: '100%' }}>
                      <input
                        type="search"
                        className="ct-input"
                        placeholder="Filter recognised entities…"
                        aria-label="Filter recognised entities"
                        data-testid="admin-settings-roster-filter"
                        value={rosterFilterQuery}
                        onChange={(e) => setRosterFilterQuery(e.target.value)}
                        style={{ width: '100%', fontSize: '14px' }}
                      />
                    </div>
                    {rosterFilterQuery.trim() && (
                      <CtButton
                        type="button"
                        variant="ghost"
                        size="sm"
                        data-testid="admin-settings-roster-filter-clear"
                        onClick={() => setRosterFilterQuery('')}
                      >
                        Clear filter
                      </CtButton>
                    )}
                  </div>

                  {filteredEntities.length > 0 ? (
                    <CtTable>
                      <table data-testid="admin-settings-roster-table">
                        <thead>
                          <tr>
                            <th>Recognised entity name</th>
                            <th style={{ width: '90px', textAlign: 'right' }}>Action</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filteredEntities.map((name) => {
                            const originalIndex = currentEntities.indexOf(name);
                            return (
                              <tr key={name}>
                                <td><strong>{name}</strong></td>
                                <td style={{ textAlign: 'right' }}>
                                  <CtButton
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => {
                                      const next = currentEntities.filter((_, i) => i !== originalIndex);
                                      setRosterDraft(next.join('\n'));
                                      setRosterSaved(false);
                                    }}
                                  >
                                    Remove
                                  </CtButton>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </CtTable>
                  ) : (
                    <p className="ct-muted" data-testid="admin-settings-roster-filter-empty" style={{ margin: '0.5rem 0', fontSize: '14px' }}>
                      No recognised entities match &ldquo;{rosterFilterQuery}&rdquo;.
                    </p>
                  )}
                </div>
              ) : (
                <CtBanner variant="info" data-testid="admin-settings-roster-empty-state">
                  No additional legal entities are configured yet. Reviews recognise the primary
                  party defined in each playbook. Add subsidiaries, parent entities, or d/b/a names
                  below or import a CSV so contract reviews always recognize our parties.
                </CtBanner>
              );
            })()}

            <form
              className="ct-stack"
              noValidate
              onSubmit={(event) => {
                event.preventDefault();
                void saveRoster();
              }}
            >
              <CtField
                label="One legal entity name per line"
                hint={
                  'Names contain commas, so each one gets its own line. Blank lines and ' +
                  'repeats are dropped when you save. Saving an empty box clears the list, ' +
                  'and reviews fall back to the playbook’s own party name alone.'
                }
              >
                <div className="ct-stack" style={{ gap: '0.35rem' }}>
                  <div
                    className="ct-row"
                    style={{
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      flexWrap: 'wrap',
                      gap: '0.5rem',
                      fontSize: '14px',
                    }}
                  >
                    <span
                      className="ct-muted"
                      data-testid="admin-settings-roster-count"
                      style={{ fontWeight: 600, fontSize: '14px' }}
                    >
                      {distinctDraftEntities.length}{' '}
                      {distinctDraftEntities.length === 1 ? 'distinct entity' : 'distinct entities'}
                    </span>
                    {duplicateLineCount > 0 && (
                      <span
                        data-testid="admin-settings-roster-duplicate-notice"
                        style={{ fontSize: '14px', color: 'var(--ct-color-warn, #b45309)', fontWeight: 500 }}
                      >
                        {duplicateLineCount}{' '}
                        {duplicateLineCount === 1 ? 'duplicate entry' : 'duplicate entries'} will be merged on save
                      </span>
                    )}
                  </div>
                  <textarea
                    data-testid="admin-settings-roster-text"
                    rows={6}
                    value={rosterDraft}
                    onChange={(event) => {
                      setRosterDraft(event.target.value);
                      setRosterSaved(false);
                    }}
                  />
                </div>
              </CtField>

              <div className="ct-row" style={{ flexWrap: 'wrap', gap: '0.5rem' }}>
                <CtButton
                  type="submit"
                  variant="primary"
                  data-testid="admin-settings-roster-save"
                  disabled={rosterSaving}
                  loading={rosterSaving}
                >
                  {rosterSaving ? 'Saving…' : 'Save — takes effect for the next review'}
                </CtButton>
                <CtButton
                  type="button"
                  variant="secondary"
                  data-testid="admin-settings-roster-copy"
                  onClick={copyRoster}
                  disabled={!rosterDraft.trim()}
                >
                  {rosterCopied ? 'Copied!' : 'Copy list'}
                </CtButton>
                <CtButton
                  type="button"
                  variant="secondary"
                  data-testid="admin-settings-roster-export"
                  onClick={exportRosterCsv}
                  disabled={!rosterDraft.trim()}
                >
                  Export CSV
                </CtButton>
                <input
                  ref={rosterFileInputRef}
                  type="file"
                  accept=".csv,.txt"
                  data-testid="admin-settings-roster-import-input"
                  style={{ display: 'none' }}
                  onChange={importRosterCsv}
                />
                <CtButton
                  type="button"
                  variant="secondary"
                  data-testid="admin-settings-roster-import-btn"
                  onClick={() => rosterFileInputRef.current?.click()}
                >
                  Import CSV
                </CtButton>
              </div>
            </form>

            <p className="ct-muted" data-testid="admin-settings-roster-last-saved">
              {formatSetAt(rosterLoad.data.updated_at, rosterLoad.data.updated_by)
                ? `Last saved ${formatSetAt(rosterLoad.data.updated_at, rosterLoad.data.updated_by)}.`
                : 'Nothing has been saved here yet.'}
            </p>

            {Array.isArray(rosterLoad.data.history) && rosterLoad.data.history.length > 0 && (
              <details
                data-testid="admin-settings-roster-history-details"
                style={{
                  marginTop: '0.5rem',
                  padding: '0.75rem',
                  borderRadius: 'var(--ct-radius, 6px)',
                  background: 'var(--ct-color-surface-subtle, rgba(0,0,0,0.02))',
                  border: '1px solid var(--ct-color-border-subtle, rgba(0,0,0,0.08))',
                  fontSize: '14px',
                }}
              >
                <summary
                  style={{
                    cursor: 'pointer',
                    fontWeight: 600,
                    fontSize: '14px',
                    userSelect: 'none',
                  }}
                  data-testid="admin-settings-roster-history-summary"
                >
                  Change history ({rosterLoad.data.history.length})
                </summary>
                <div
                  className="ct-stack"
                  data-testid="admin-settings-roster-history-list"
                  style={{ marginTop: '0.75rem', gap: '0.75rem' }}
                >
                  {rosterLoad.data.history.map((entry, idx) => (
                    <div
                      key={`${entry.timestamp}-${idx}`}
                      style={{
                        paddingBottom: '0.5rem',
                        borderBottom:
                          idx === rosterLoad.data.history!.length - 1
                            ? 'none'
                            : '1px solid var(--ct-color-border-subtle, rgba(0,0,0,0.06))',
                        fontSize: '14px',
                      }}
                      data-testid={`admin-settings-roster-history-entry-${idx}`}
                    >
                      <div
                        className="ct-row"
                        style={{
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          marginBottom: '0.25rem',
                          flexWrap: 'wrap',
                          gap: '0.5rem',
                          fontSize: '14px',
                        }}
                      >
                        <span style={{ fontWeight: 600, fontSize: '14px' }}>
                          {formatSetAt(entry.timestamp, entry.actor) || 'Unknown save'}
                        </span>
                        <span className="ct-muted" style={{ fontSize: '14px' }}>
                          {entry.count} {entry.count === 1 ? 'entity' : 'entities'} total
                        </span>
                      </div>
                      <div className="ct-row" style={{ flexWrap: 'wrap', gap: '0.35rem' }}>
                        {entry.added.map((name) => (
                          <span
                            key={`added-${name}`}
                            style={{
                              fontSize: '14px',
                              color: 'var(--ct-color-success-text, #15803d)',
                              background: 'var(--ct-color-success-bg, #f0fdf4)',
                              border: '1px solid var(--ct-color-success-border, #bbf7d0)',
                              padding: '2px 8px',
                              borderRadius: '4px',
                              display: 'inline-block',
                            }}
                          >
                            + {name}
                          </span>
                        ))}
                        {entry.removed.map((name) => (
                          <span
                            key={`removed-${name}`}
                            style={{
                              fontSize: '14px',
                              color: 'var(--ct-color-danger-text, #b91c1c)',
                              background: 'var(--ct-color-danger-bg, #fef2f2)',
                              border: '1px solid var(--ct-color-danger-border, #fecaca)',
                              padding: '2px 8px',
                              borderRadius: '4px',
                              display: 'inline-block',
                            }}
                          >
                            - {name}
                          </span>
                        ))}
                        {entry.added.length === 0 && entry.removed.length === 0 && (
                          <span className="ct-muted" style={{ fontSize: '14px' }}>
                            Updated roster list
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        </CtCard>
      )}

      {spendLoad.status === 'failed' ? null : spendLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-settings-spend-loading" label="Loading spend…" />
      ) : (
        <CtCard data-testid="admin-settings-spend-panel">
          <CtToolbar title="Spend" />
          <div className="ct-stack">
            <p data-testid="admin-settings-spend-explainer">
              Every review reserves its worst-case cost before it starts, and a submission is
              refused once the day’s reservations reach the cap. The cap resets at midnight UTC.
              Lowering it never claws back spend already made — it stops the next review.
            </p>

            {!spendLoad.data.next_review_admissible && (
              <CtBanner variant="warn" data-testid="admin-settings-spend-blocked">
                The next review would be refused: what today has committed leaves less than one
                review’s reservation under the cap. Raise the cap, choose cheaper models, or wait
                for midnight UTC.
              </CtBanner>
            )}

            <CtTable>
              <table data-testid="spend-today-table">
                <thead>
                  <tr>
                    <th>Today</th>
                    <th>Amount</th>
                    <th>What it means</th>
                  </tr>
                </thead>
                <tbody>
                  <tr data-testid="spend-row-cap">
                    <td>Daily cap</td>
                    <td className="ct-table__mono" data-testid="spend-value-cap">
                      {formatUsdCents(spendLoad.data.daily_cap_usd_cents)}
                    </td>
                    <td data-testid="spend-meaning-cap">
                      {capSourceNote(spendLoad.data.cap_setting)}
                    </td>
                  </tr>
                  <tr data-testid="spend-row-spent">
                    <td>Committed today</td>
                    <td className="ct-table__mono" data-testid="spend-value-spent">
                      {formatUsdCents(spendLoad.data.spent_today_usd_cents)}
                    </td>
                    <td data-testid="spend-meaning-spent">
                      What today’s reviews hold against the cap. A review in flight holds its
                      worst case; once it finishes, this falls to what it actually cost.
                    </td>
                  </tr>
                  <tr data-testid="spend-row-settled">
                    <td>Billed today</td>
                    <td className="ct-table__mono" data-testid="spend-value-settled">
                      {formatUsdCents(spendLoad.data.settled_today_usd_cents)}
                    </td>
                    <td data-testid="spend-meaning-settled">
                      What finished work actually cost, including the cheap check that runs when
                      a file is chosen and any cover notes drafted. Those two do not reserve, so
                      they are billed but sit outside the ceiling above.
                    </td>
                  </tr>
                  <tr data-testid="spend-row-remaining">
                    <td>Left today</td>
                    <td className="ct-table__mono" data-testid="spend-value-remaining">
                      {formatUsdCents(spendLoad.data.remaining_usd_cents)}
                    </td>
                    <td data-testid="spend-meaning-remaining">
                      {(() => {
                        const count = reviewsRemaining(spendLoad.data);
                        if (count === null) {
                          return 'The cap less what is committed today.';
                        }
                        return count === 1
                          ? 'Room for one more review today.'
                          : `Room for ${count} more reviews today.`;
                      })()}
                    </td>
                  </tr>
                  <tr data-testid="spend-row-next">
                    <td>The next review</td>
                    <td className="ct-table__mono" data-testid="spend-value-next">
                      {spendLoad.data.estimated_review_usd_cents === null
                        ? '—'
                        : formatUsdCents(spendLoad.data.estimated_review_usd_cents)}
                    </td>
                    <td data-testid="spend-meaning-next">
                      What a document of ordinary size is expected to cost on the models
                      currently selected. It reserves{' '}
                      {formatUsdCents(spendLoad.data.worst_case_reservation_usd_cents)} while it
                      runs — the worst case is what the cap is measured against, and the unused
                      part is released when the review finishes.
                    </td>
                  </tr>
                </tbody>
              </table>
            </CtTable>

            {!spendLoad.data.cap_setting.cap_store_available ? (
              <CtBanner variant="warn" data-testid="admin-settings-spend-unavailable">
                This deployment keeps no settings store, so the cap can only be changed where the
                deployment is configured (DAILY_SPEND_CAP_USD_CENTS), not here.
              </CtBanner>
            ) : (
              <>
                {capSaveError && (
                  <CtBanner variant="danger" data-testid="admin-settings-cap-save-error">
                    {capSaveError}
                  </CtBanner>
                )}

                {capSaved && (
                  <CtBanner variant="ok" data-testid="admin-settings-cap-saved">
                    Saved. The next review is checked against{' '}
                    {formatUsdCents(spendLoad.data.daily_cap_usd_cents)}.
                  </CtBanner>
                )}

                <form
                  className="ct-stack"
                  noValidate
                  onSubmit={(event) => {
                    event.preventDefault();
                    const cents = parseCapDollars(capDraft);
                    if (cents === null) {
                      // Refused here rather than posted: `null` on the wire
                      // MEANS "use the deployment's cap", so sending a mistyped
                      // field would silently change the ceiling instead of
                      // reporting a mistake.
                      setCapSaveError(
                        'Enter the cap in dollars, like 20 or 12.50. To go back to the ' +
                          'deployment’s own cap, use the other button.',
                      );
                      return;
                    }
                    void saveCap(cents);
                  }}
                >
                  <CtField
                    label="Daily cap (US dollars)"
                    hint={
                      `Between ${formatUsdCents(
                        spendLoad.data.cap_setting.min_daily_cap_usd_cents,
                      )} and ${formatUsdCents(
                        spendLoad.data.cap_setting.max_daily_cap_usd_cents,
                      )}. Takes effect on the next review, with no redeploy.`
                    }
                  >
                    <input
                      type="text"
                      inputMode="decimal"
                      data-testid="admin-settings-cap-input"
                      value={capDraft}
                      onChange={(event) => {
                        setCapDraft(event.target.value);
                        setCapSaved(false);
                      }}
                    />
                  </CtField>

                  <div className="ct-row">
                    <CtButton
                      type="submit"
                      variant="primary"
                      data-testid="admin-settings-cap-save"
                      disabled={capSaving}
                      loading={capSaving}
                    >
                      {capSaving ? 'Saving…' : 'Save cap'}
                    </CtButton>
                    {spendLoad.data.cap_setting.stored_daily_cap_usd_cents !== null && (
                      <CtButton
                        type="button"
                        data-testid="admin-settings-cap-clear"
                        disabled={capSaving}
                        onClick={() => {
                          void saveCap(null);
                        }}
                      >
                        {`Use the deployment’s cap (${formatUsdCents(
                          spendLoad.data.cap_setting.env_daily_cap_usd_cents ??
                            spendLoad.data.cap_setting.default_daily_cap_usd_cents,
                        )})`}
                      </CtButton>
                    )}
                  </div>
                </form>

                <p className="ct-muted" data-testid="admin-settings-cap-last-saved">
                  {formatSetAt(
                    spendLoad.data.cap_setting.updated_at,
                    spendLoad.data.cap_setting.updated_by,
                  )
                    ? `Last changed ${formatSetAt(
                        spendLoad.data.cap_setting.updated_at,
                        spendLoad.data.cap_setting.updated_by,
                      )}.`
                    : 'The cap has not been changed here.'}
                </p>
              </>
            )}
          </div>
        </CtCard>
      )}

      {load.status === 'failed' ? null : load.status === 'loading' ? (
        <CtProgress data-testid="admin-settings-loading" label="Loading deployment settings…" />
      ) : (
        <CtCard data-testid="admin-settings-capabilities-panel">
          <CtTable>
            <table data-testid="deployment-capabilities-table">
              <thead>
                <tr>
                  <th>Capability</th>
                  <th>State</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {capabilityRows(load.data).map((row) => (
                  <tr key={row.id} data-testid={`capability-row-${row.id}`}>
                    <td>{row.name}</td>
                    <td>
                      <CtChip
                        variant={row.enabled ? 'ok' : 'muted'}
                        data-testid={`capability-state-${row.id}`}
                      >
                        {row.state}
                      </CtChip>
                    </td>
                    <td data-testid={`capability-why-${row.id}`}>{row.why}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CtTable>
        </CtCard>
      )}

      {secretsLoad.status === 'failed' ? null : secretsLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-settings-secrets-loading" label="Loading secrets…" />
      ) : (
        <CtCard data-testid="admin-settings-secrets-panel">
          <CtTable>
            <table data-testid="deployment-secrets-table">
              <thead>
                <tr>
                  <th>Secret</th>
                  <th>State</th>
                  <th>Fingerprint</th>
                  <th>Last set here</th>
                  <th>Rotating it</th>
                </tr>
              </thead>
              <tbody>
                {secretRows(secretsLoad.data).map((row) => (
                  <tr key={row.id} data-testid={`secret-row-${row.id}`}>
                    <td>{row.name}</td>
                    <td>
                      <CtChip variant={row.stateVariant} data-testid={`secret-state-${row.id}`}>
                        {row.state}
                      </CtChip>
                    </td>
                    <td className="ct-table__mono" data-testid={`secret-fingerprint-${row.id}`}>
                      {row.fingerprint ? <code>{row.fingerprint}</code> : '—'}
                    </td>
                    <td data-testid={`secret-last-set-${row.id}`}>{row.lastSet || '—'}</td>
                    <td data-testid={`secret-rotation-${row.id}`}>{row.rotation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CtTable>
        </CtCard>
      )}

      {deployLoad.status === 'failed' ? null : deployLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-settings-deploy-loading" label="Loading deployed version…" />
      ) : (
        <CtCard data-testid="admin-settings-deploy-panel">
          {(() => {
            // Computed here, from the two stamps, so the verdict and the
            // table below it can never be read from different data.
            const agreement = deployAgreement(frontend, deployLoad.data);
            return (
              <>
                <CtBanner
                  variant={agreementVariant(agreement)}
                  data-testid="deploy-agreement"
                  data-agreement-status={agreement.status}
                  data-agreement-kind={agreement.kind}
                >
                  <strong data-testid="deploy-agreement-headline">{agreement.headline}</strong>{' '}
                  <span data-testid="deploy-agreement-detail">{agreement.detail}</span>
                </CtBanner>
                <details className="ct-stack" open>
                  <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
                    Deployed version details
                  </summary>
                  <CtTable>
                    <table data-testid="deployed-version-table">
                      <thead>
                        <tr>
                          <th>What</th>
                          <th>Server</th>
                          <th>App bundle</th>
                          <th>What it tells you</th>
                        </tr>
                      </thead>
                      <tbody>
                        {deployFactRows(frontend, deployLoad.data).map((row) => (
                          <tr key={row.id} data-testid={`deploy-row-${row.id}`}>
                            <td>{row.label}</td>
                            <td
                              className={row.mono ? 'ct-table__mono' : undefined}
                              data-testid={`deploy-server-${row.id}`}
                            >
                              {renderDeployValue(row.server, row.mono)}
                            </td>
                            <td
                              className={row.mono ? 'ct-table__mono' : undefined}
                              data-testid={`deploy-bundle-${row.id}`}
                            >
                              {renderDeployValue(row.bundle, row.mono)}
                            </td>
                            <td data-testid={`deploy-meaning-${row.id}`}>{row.meaning}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </CtTable>
                </details>
              </>
            );
          })()}
        </CtCard>
      )}
    </section>
  );
}
