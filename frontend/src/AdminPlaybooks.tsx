/**
 * AdminPlaybooks — the merged Playbooks admin screen (issue #605), covering
 * both playbook LIFECYCLE (issue #434, docs/frontend-design-system.md
 * §15.1: upload, activate, roll back, rename, remove, per-version notes)
 * and, since #605, each playbook's standing INSTRUCTIONS (issue #484,
 * `AdminInstructions.tsx` — now rendered here instead of its own tab).
 *
 * ## The merge (issue #605)
 *
 * Owner-confirmed 2026-08-22: the two screens are about one object, a
 * playbook, and installing one then writing its standing instructions used
 * to mean crossing tabs, with the instructions tab's own selector
 * duplicating this table. Per the confirmed scope, this screen now reads
 * top to bottom as: the playbook list (below) → the version history →
 * the selected playbook's standing instructions (`<AdminInstructions>`,
 * new) → the upload forms, LAST.
 *
 * SUPERSEDED IN PART: #605 made a row's "Version history" action the single
 * selection driving both the version-history table and the instructions pane.
 * #598 then moved version history into an overlay and #611 gave standing
 * instructions their own row action, so those are now two selections with two
 * controls — see the sections on both below, and `selectPlaybook` /
 * `showHistoryFor`.
 *
 * Destructive lifecycle actions (Remove, Activate, Roll back) all live
 * above the instructions pane, in their own cards, never sharing one with
 * the text box an admin types standing guidance into — the spatial
 * separation the confirmation comment called for.
 *
 * The catalog had no administration UI at all: the backend routed upload,
 * activate, roll back, rename, remove, and per-version notes (issues #242 /
 * #411 / #412 / #430), and nothing in the SPA called any of them. This is
 * that surface, and — since issue #433 retired the bespoke
 * "activate the bundled sample" button — it is the ONLY playbook-lifecycle
 * UI in the app.
 *
 * ## What it talks to
 *
 *   GET    /api/playbooks                                        catalog (id, display name, active/not, active version's notes) — issue #485/#490: a UNION of registry entries and DB-created playbooks
 *   GET    /api/admin/playbooks/{id}/versions                    the append-only upload trail
 *   POST   /api/admin/playbooks                                  create a brand-new playbook_id + its first version, atomically (issue #485) — identity comes from the uploaded OPF document, never a typed id
 *   POST   /api/admin/playbooks/{id}/versions                    multipart upload of a new version onto an EXISTING playbook_id
 *   POST   /api/admin/playbooks/{id}/versions/{v}/legal-approval  record legal approval of a version's exact content_hash (issue #485) — the precondition Activate checks
 *   POST   /api/admin/playbooks/{id}/versions/{v}/activate        Gate-7-checked activation
 *   POST   /api/admin/playbooks/{id}/versions/{v}/rollback        restore a previously-active version
 *   PATCH  /api/admin/playbooks/{id}/versions/{v}/notes           the one mutable field on a version row
 *   PATCH  /api/admin/playbooks/{id}                              rename (catalog display name only)
 *   DELETE /api/admin/playbooks/{id}                              remove (tombstone; one-way door)
 *
 * The catalog read is the only call here any authenticated user may make;
 * every other route 403s a non-admin caller server-side. A 403 from any of
 * them is the sole signal to hide this panel — the same defense-in-depth
 * posture as AdminUsers/AdminRetention/AdminModel/AdminInstructions, with no
 * client-side "am I an admin" claim to keep in sync (App.tsx's /api/me probe
 * decides whether this component mounts at all; the server stays
 * authoritative for every action it offers).
 *
 * ## Backend constraints this screen must not paper over
 *
 *   1. **Activation is Gate 7'd, and approval is now a real button, not a
 *      dead end.** `activate_release_bundle` refuses a version whose
 *      `content_hash` does not equal its recorded `legal_approval.
 *      content_hash` — including the (normal, for a freshly-uploaded
 *      version) case where no approval was ever recorded. Issue #485 added
 *      the missing product path for recording one
 *      (`POST .../legal-approval`): the version-history table offers an
 *      "Approve for activation" action on any version that has a
 *      `content_hash` and has not yet been approved for it, sending back
 *      the EXACT hash already shown on that row (never operator free-text —
 *      the server independently re-checks it matches anyway). Once
 *      approved, the button is replaced with a quiet "Approved" note (issue
 *      #476's hide-don't-disable convention), and the permanent banner
 *      below still states the rule for the version that hasn't been
 *      approved yet — an upload is not self-activating, and this screen
 *      never pretends otherwise. The server's own refusal message is
 *      surfaced verbatim rather than replaced with a generic failure
 *      string.
 *
 *      Issue #594: the one row that must NOT offer approval is one that is
 *      already `active` and was never approved. Only the deploy seed can
 *      produce that — `seed_shipped_playbook` bypasses Gate 7 deliberately
 *      (see sample_playbooks.py) — and on such a row approval is not a
 *      precondition for anything, because the version is already live.
 *      The button is withdrawn there, and replaced by a note that SAYS the
 *      version went live unapproved. Hiding the button without saying so
 *      would leave this audit surface implying the live playbook had been
 *      approved. Backfilling an approval at seed time was rejected outright:
 *      `record_legal_approval`'s own docstring refuses it, and fabricating a
 *      record for bytes nobody with legal authority reviewed widens the
 *      bypass rather than honoring it.
 *
 *      Issue #595: approval and activation are now ONE control,
 *      "Approve & activate". Approval was always the precheck activation
 *      requires, so two clicks for one decision bought nothing. What Gate 7
 *      protects survives untouched: an explicit human act, naming the exact
 *      bytes, recorded in the audit trail, DISTINCT FROM UPLOAD. Merging
 *      upload → approve would have destroyed it (an upload would then be
 *      self-approving — `record_legal_approval`'s docstring says so
 *      outright) and is not done. A version that is already approved but
 *      not active keeps a plain Activate that calls only the activate
 *      route, so rolling forward to a previously-approved version never
 *      demands re-approval.
 *   2. **Rollback only accepts a `retired` target.** `rollback_playbook_version`
 *      rejects a version that was never active ("rolling back to a version
 *      that was never active is just a (second) activation"). Since only
 *      activate/rollback ever write `retired`, that status IS the "has
 *      something to roll back to" signal — so a draft or the currently-
 *      active row offers no Roll back button at all (hidden, not disabled;
 *      issue #476) rather than a dead one with nowhere to go. The backend's
 *      409 is still the authority and is rendered verbatim if it disagrees.
 *   3. **Installing a playbook never takes an operator-typed identifier —
 *      not the playbook_id, and since issue #597 not the version either.**
 *      `POST /api/admin/playbooks` (issue #485) derives the new
 *      playbook_id from the uploaded OPF document's own `agreement_type.id`
 *      — the "Upload a new playbook" form has no playbook_id field at all,
 *      and the derived id is read back from the response and shown to the
 *      admin (and used to select that playbook's version history) rather
 *      than guessed client-side.
 *
 *      Issue #597 finished the job: the VERSION identifier is now read out
 *      of the artifact too (`opfIdentity.ts`, owner decision 2026-08-22 —
 *      `identity.version` when the document declares one, otherwise
 *      upload-date + `content_hash` prefix), displayed read-only BEFORE
 *      submit, and never editable. So this form now has exactly two operator
 *      inputs: the file, and a free-text Note. Everything identifying is
 *      read from the bytes. A file carrying no derivable identity is refused
 *      here with an explanation rather than falling back to a typed value —
 *      prefill-but-editable (option b) was considered and rejected by the
 *      owner. The "Upload version" form for an EXISTING playbook still asks
 *      for a typed identifier; #597 put that in a follow-up on purpose, so
 *      both screens derive identically once the rule settled here.
 *
 * ## Version history is an overlay, and row actions are a group (issue #598)
 *
 * Two presentation defects the owner hit on the live screen. Version history
 * used to expand INLINE beneath the playbook list — a whole second table that
 * pushed the page down and left them unsure what they were looking at
 * ("version history should maybe be in a, like, an overlay window"). And the
 * Actions cell rendered three full-size buttons which, in a narrow table
 * column, wrapped one per line, so a two-row table read as a wall of six with
 * the one-way door carrying the same weight as Rename ("it's like a stack of
 * jumbley buttons").
 *
 * The overlay is LOCAL to this screen: CTDS has no dialog primitive and #598
 * says not to invent a shared one here. If a second consumer ever appears,
 * that is the moment to promote `.ct-overlay` to a `ct-dialog` component.
 *
 * It is a `div[role=dialog][aria-modal]` with hand-written focus management
 * rather than a native `<dialog>` + `showModal()`, because jsdom implements
 * no `showModal` at all — a native dialog would have made the role, the
 * Escape handling and the focus return untestable, which is precisely the
 * wrong trade for a brand-new overlay surface. It renders in the same DOM
 * position the inline panel occupied, so the top-to-bottom reading order
 * #605 established survives for anything walking the document.
 *
 * `historyPlaybookId` is deliberately its OWN state rather than a reuse of
 * `selectedPlaybookId`: selection also drives the standing-instructions pane
 * (#605), and those stopped being the same question the moment history became
 * dismissible. Closing the overlay leaves the selection — and the instructions
 * pane — exactly where they were.
 *
 * ## Standing instructions are discoverable (issue #611)
 *
 * Two regressions #605's merge introduced, caught in its own independent
 * review. Standing instructions became reachable ONLY through a button
 * labelled "Version history" — and once #598 made that button open a modal,
 * the label was not merely vague, it pointed somewhere else. And #484's "one
 * playbook installed: preselected and quiet" was lost, so a single-playbook
 * deployment had to hunt behind that mislabelled button for its own guidance.
 *
 * The two jobs that button was doing are now split. "Version history" opens
 * the overlay and nothing else; a sibling "Standing instructions" action
 * chooses whose guidance the pane below shows, and opens nothing. Opening one
 * playbook's trail while another's guidance is on screen is an ordinary thing
 * to do and no longer switches both.
 *
 * The restored auto-select is NARROWER than #484's original, which defaulted
 * to the FIRST playbook however many were installed. With two or more, a
 * guess renders one playbook's standing guidance under a heading naming
 * another — and a Save from that state writes it there. Standing instructions
 * steer every review run against a playbook, which is exactly the blast
 * radius #605's own "case against" section warned about. So: exactly one
 * installed, preselect it; more than one, ask.
 *
 * ## Two-column layout (issue #609, the AdminPlaybooks split of #602)
 *
 * Two pairs, and only two, use `ct-columns` (#601): the "upload a version"
 * form's two short fields (which playbook / which version), and the "upload a
 * new playbook" form's file drop beside the version identifier derived from
 * it — since #597 those two are one thought, and side by side they say so.
 *
 * Everything else on this screen is deliberately left alone. `ct-columns.ts`
 * says not to use it for "a single logical control (nothing to pair it
 * with)", and the rename field and the per-version note field are each alone
 * in a table cell. The file drops and the free-text notes are legitimately
 * full width; halving them would be a new defect, not #602's fix. The
 * standing-instructions pane is `AdminInstructions.tsx` — panel 5 of #602,
 * its own ticket.
 *
 * ## Operator language: uploaded, never created (issue #596)
 *
 * A playbook is authored elsewhere (playbook-engine) and UPLOADED here.
 * Nothing on this screen creates one, so no operator-facing string calls the
 * action "Create playbook": the toolbar action, the card heading and the
 * submit all say "Upload new playbook" / "Upload a new playbook". The REST
 * spelling stays `POST /api/admin/playbooks` — that route really does create
 * the resource, and renaming it would be churn with no reader. The internal
 * `create*` state and testids are likewise left alone deliberately; they name
 * the request, not the operator's model of it.
 *
 * Activate is likewise hidden — not merely disabled — on the row that is
 * already `active` (issue #476): re-running activation on the active
 * version is a no-op an admin can't distinguish from "something happened".
 * A quiet "Currently active" note takes its place.
 *
 * Removal is a ONE-WAY DOOR (`remove_playbook` writes a tombstone nothing in
 * this codebase clears — see that function's docstring), which is why it is
 * the first consumer of the §14 confirm-step (`confirm` on ct-button): one
 * click arms, a second within the window removes, blur/Escape/timeout cancels.
 *
 * No optimistic UI anywhere: every table only reflects a change after the
 * server confirms it, same rule as AdminUsers (these mutations decide which
 * legal positions a review is run against).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { failedLoad, type LoadState } from './loadState';
import {
  invalidateCatalog,
  usePlaybookCatalog,
  type PlaybookCatalogEntry,
  type PlaybookCatalogStatus,
} from './playbooksStore';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail, triggerBrowserDownload } from './api';
import { linkifyText } from './linkify';
import { deriveVersionIdentifier, readOpfIdentity } from './opfIdentity';
import AdminInstructions from './AdminInstructions';
import {
  CtBanner,
  CtButton,
  CtCard,
  CtChip,
  CtColumns,
  CtField,
  CtFileDrop,
  CtProgress,
  CtTable,
  CtToolbar,
} from './ui/react';
import type { CtChipVariant } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/review_routes.py::_load_playbook_catalog and
// backend/src/playbook_versions.py::list_playbook_version_trail.
// ---------------------------------------------------------------------------

// The catalog entry and its status moved to `playbooksStore.ts` when the fetch
// became shared (issue #72), and that is now the only import site for them —
// this module deliberately does NOT re-export them, because nothing imports
// them from here and a second name for one type is how the two drift apart.

/**
 * This panel's own copy for a catalog read that failed (issue #72). The
 * shared store carries the HTTP status and the raw failure and renders
 * nothing; the sentence a person sees is still decided here, and is the same
 * one this screen showed before the fetch moved.
 */
const CATALOG_ERROR_COPY = "We couldn't load your playbooks. Please try again.";

/** `playbook_versions.status` — the sole lifecycle authority (#79). */
export type PlaybookVersionStatus = 'draft' | 'active' | 'retired';

export interface PlaybookVersionRow {
  playbook_id: string;
  version: string;
  uploaded_by: string;
  uploaded_at: number;
  status: PlaybookVersionStatus;
  notes: string;
  /** Absent on rows written before content hashes were recorded. */
  content_hash?: string;
  /**
   * The hash `record_legal_approval` most recently approved for this row
   * (issue #485) — absent when no approval has ever been recorded. A row is
   * Gate-7-ready for activation iff this equals `content_hash` above; that
   * comparison is what `isApproved` below computes.
   */
  legal_approval_content_hash?: string;
}

/** Gate 7, mirrored client-side for display only — the server re-checks for
 * real on every activate/approve call; this never gates anything itself. */
export function isApproved(row: PlaybookVersionRow): boolean {
  return row.content_hash !== undefined && row.legal_approval_content_hash === row.content_hash;
}

// Each admin screen keeps its own `jsonFetch` wrapper rather than sharing one
// — the established convention (see api.ts's docstring for why only
// `authorizedFetch` itself is shared).
function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

function formatTimestamp(epochSeconds: number | null): string {
  if (epochSeconds === null || epochSeconds === undefined) {
    return 'never';
  }
  return new Date(epochSeconds * 1000).toLocaleString();
}

/**
 * `sha256:<64 hex>` → `sha256:0123456789ab…`. The full value is never
 * dropped: it is carried on the cell's `title` so it stays readable (and
 * copyable from the tooltip) rather than silently truncated.
 */
export function shortenHash(hash: string): string {
  const separator = hash.indexOf(':');
  const prefix = separator === -1 ? '' : hash.slice(0, separator + 1);
  const digest = separator === -1 ? hash : hash.slice(separator + 1);
  if (digest.length <= 12) {
    return hash;
  }
  return `${prefix}${digest.slice(0, 12)}…`;
}

// Catalog status → chip variant. Exhaustive over PlaybookCatalogStatus, same
// shape as AdminUsers' `statusChipVariant`.
function catalogChipVariant(status: PlaybookCatalogStatus): CtChipVariant {
  switch (status) {
    case 'active':
      return 'ok';
    case 'coming_soon':
      return 'muted';
  }
}

// "coming_soon" is the catalog's wire value for "registered but nothing
// active". On an admin lifecycle screen that reads as a launch date rather
// than a state, so it is shown honestly as "not active" (§15.2).
function catalogStatusLabel(status: PlaybookCatalogStatus): string {
  return status === 'active' ? 'active' : 'not active';
}

// Version status → chip variant. Exhaustive over PlaybookVersionStatus.
function versionChipVariant(status: PlaybookVersionStatus): CtChipVariant {
  switch (status) {
    case 'active':
      return 'ok';
    case 'retired':
      return 'muted';
    case 'draft':
      return 'info';
  }
}

/**
 * This panel takes NO props (issue #72).
 *
 * Issue #464 gave it an `onCatalogChange` callback so App.tsx could bump a
 * counter ReviewSubmission's dial listened on; issue #635 gave it a
 * `credentialsRefreshKey` so a password rotation would make its catalog read
 * run again. Both are gone. The mutation handlers below call
 * `invalidateCatalog()` on the shared store, which every consumer already
 * subscribes to, so there is no signal left to thread through a parent; and
 * the rotation seam moved to App.tsx's `handleCredentialsRotated`, because
 * this panel renders only for an admin while the catalog it reads is refused
 * for every unrotated caller — hosting the invalidation here left a non-admin
 * with no way back (see adminRefresh.ts).
 */
export default function AdminPlaybooks(): React.ReactElement | null {
  // Issue #511: two explicit three-state loads. Both previously shared ONE
  // `error` string alongside a `T | null` sentinel, so a failed catalog fetch
  // left a permanent "Loading playbooks…" under a danger banner, and a failed
  // version fetch was indistinguishable from one still in flight.
  //
  // Issue #72: the catalog itself is no longer this panel's to own — one
  // shared, memoised read serves the Review tab's dial and this table alike
  // (playbooksStore.ts), so an activation here is visible there without a
  // reload and neither screen pays for the other's fetch. What stays local is
  // the PRESENTATION of that one load: `playbooksLoad` is the same
  // three-state shape the render below was written against, and the message
  // is still this panel's own copy — the store deliberately carries the HTTP
  // status and the raw failure, never a sentence.
  const catalog = usePlaybookCatalog();
  const playbooks = catalog.status === 'ready' ? catalog.data : null;
  const playbooksLoad: LoadState<PlaybookCatalogEntry[]> =
    catalog.status === 'ready'
      ? { status: 'ready', data: catalog.data }
      : catalog.status === 'failed'
        ? { status: 'failed', message: CATALOG_ERROR_COPY }
        : { status: 'loading' };
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  // Any admin route answering 403 hides the panel outright — no client-side
  // admin claim is trusted here (see this module's docstring). The catalog's
  // own 403 is DERIVED rather than latched into this state (issue #72): the
  // shared store reports the status, and reading it straight means there is
  // never a render in which the panel is forbidden but has already painted an
  // error banner on its way to finding out.
  const [isForbidden, setIsForbidden] = useState(false);
  const catalogForbidden = catalog.status === 'failed' && catalog.error.httpStatus === 403;

  // Version history is loaded for one playbook at a time (the trail route is
  // per-playbook), so the table below is scoped to this selection.
  const [selectedPlaybookId, setSelectedPlaybookId] = useState<string | null>(null);
  const [versionsLoad, setVersionsLoad] = useState<LoadState<PlaybookVersionRow[]>>({
    status: 'loading',
  });
  // Derived views, so the render below reads exactly as it did.
  const versions = versionsLoad.status === 'ready' ? versionsLoad.data : null;

  // Issue #598: version history is an OVERLAY, not an inline expansion. It
  // used to render as a second table below the playbook list, pushing the
  // rest of the page down — the owner could not tell what they were looking
  // at.
  //
  // Issue #611 made this hold the playbook id rather than a bare open/closed
  // flag, because the overlay's subject and the instructions pane's subject
  // are two different questions. `selectedPlaybookId` above answers "whose
  // standing instructions am I editing?" and persists; this answers "whose
  // trail is the overlay showing?" and is null whenever it is closed. Opening
  // one playbook's history while another's guidance is on screen below is a
  // perfectly reasonable thing to do, and used to silently switch both.
  const [historyPlaybookId, setHistoryPlaybookId] = useState<string | null>(null);
  const historyDialogRef = useRef<HTMLDivElement | null>(null);
  // The control that opened the overlay, so focus can go back to it.
  const historyOpenerRef = useRef<HTMLElement | null>(null);

  // Rename — inline on the row being renamed, never a second screen.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState('');

  // Per-version notes — inline in the notes cell.
  const [notesVersion, setNotesVersion] = useState<string | null>(null);
  const [notesDraft, setNotesDraft] = useState('');

  // Upload form (collapsed until the toolbar action opens it).
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadPlaybookId, setUploadPlaybookId] = useState('');
  const [uploadVersion, setUploadVersion] = useState('');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadNotes, setUploadNotes] = useState('');
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  // Bumped after a successful upload to remount ct-file-drop, which owns its
  // own selected-file pill — clearing our state alone would leave the last
  // filename showing under an empty form.
  const [fileDropNonce, setFileDropNonce] = useState(0);

  const [playbookSearchQuery, setPlaybookSearchQuery] = useState('');
  const [diffOpen, setDiffOpen] = useState(false);
  const [diffVersionA, setDiffVersionA] = useState('');
  const [diffVersionB, setDiffVersionB] = useState('');

  const filteredPlaybooks = (playbooks ?? []).filter((entry) => {
    if (!playbookSearchQuery.trim()) return true;
    const q = playbookSearchQuery.toLowerCase().trim();
    return (
      entry.display_name.toLowerCase().includes(q) ||
      entry.playbook_id.toLowerCase().includes(q) ||
      Boolean(entry.notes?.toLowerCase().includes(q))
    );
  });

  // Create-playbook form (issue #485) — deliberately separate from the
  // upload form above, and with NO playbook_id field: identity comes from
  // the uploaded OPF document itself (POST /api/admin/playbooks derives it
  // server-side), never operator free-text.
  const [createOpen, setCreateOpen] = useState(false);
  // Issue #597: the version identifier is DERIVED from the chosen artifact,
  // not typed. `null` means "no file yet, or a file we could not derive one
  // from" — either way there is nothing to submit. There is deliberately no
  // editable counterpart to fall back to.
  const [createVersion, setCreateVersion] = useState<string | null>(null);
  const [createFile, setCreateFile] = useState<File | null>(null);
  const [createNotes, setCreateNotes] = useState('');
  const [createError, setCreateError] = useState<string | null>(null);
  const [createResult, setCreateResult] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createFileDropNonce, setCreateFileDropNonce] = useState(0);

  // Issue #635: the latch tracks the server's CURRENT answer, not its first
  // one — see adminRefresh.ts. A mutation's 403 sets it; the next catalog read
  // that actually succeeds clears it. `catalog` is a new snapshot object on
  // every publish (including a ready → ready refetch), which is what makes a
  // post-rotation refresh observable here at all. The other half of #635 —
  // something making that read run AGAIN, since this panel mounts once and
  // only `hidden` toggles — is App.tsx's `handleCredentialsRotated` calling
  // `invalidateCatalog()` on the shared store (issue #72); this panel no
  // longer takes the refresh key, because the catalog is no longer its own.
  useEffect(() => {
    if (catalog.status === 'ready') {
      setIsForbidden(false);
    }
  }, [catalog]);

  const loadVersions = useCallback(async (playbookId: string) => {
    setVersionsLoad({ status: 'loading' });
    try {
      const response = await jsonFetch(
        `/api/admin/playbooks/${encodeURIComponent(playbookId)}/versions`,
      );
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET playbook versions returned HTTP ${response.status}`,
            "We couldn't load that playbook's version history. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as { versions: PlaybookVersionRow[] };
      setVersionsLoad({ status: 'ready', data: data.versions });
    } catch (err) {
      setVersionsLoad(
        failedLoad(err, "We couldn't load that playbook's version history. Please try again."),
      );
    }
  }, []);

  /**
   * Issue #611, restoring #484's "one playbook installed: preselected and
   * quiet". #605's merge dropped it — `selectedPlaybookId` started `null`
   * unconditionally — so a single-playbook deployment had to hunt for its own
   * standing instructions behind a button labelled "Version history".
   *
   * NARROWER than #484's original, deliberately. That version defaulted to
   * the FIRST playbook however many were installed. With two or more, a guess
   * renders one playbook's standing guidance under a heading naming another,
   * and a Save from that state writes it there — standing instructions steer
   * every review run against a playbook, which is exactly the blast radius
   * #605's own "case against" section warned about. With one installed there
   * is no ambiguity to resolve, so there is nothing to ask.
   *
   * `current ?? …` never overrides a choice the operator has already made.
   */
  useEffect(() => {
    if (playbooks === null || playbooks.length !== 1) {
      return;
    }
    const only = playbooks[0]!.playbook_id;
    setSelectedPlaybookId((current) => current ?? only);
  }, [playbooks]);

  /**
   * Issue #611: choose the playbook whose STANDING INSTRUCTIONS are shown —
   * and nothing else.
   *
   * #605 gave this screen one selection driving both the version-history
   * table and the instructions pane, with a row's "Version history" button as
   * its only trigger. #598 then made version history a modal, at which point
   * that button was not merely a vague name for the instructions selector: it
   * pointed somewhere else entirely. So the two jobs are split, and this one
   * gets its own control (see the row's "Standing instructions" action).
   *
   * Setting `selectedPlaybookId` is all the pane needs: it is mounted keyed
   * on this same id, so a switch here fully remounts it onto the newly-chosen
   * playbook (and a dead instance's late response is dropped by React rather
   * than painted over the live one).
   */
  const selectPlaybook = useCallback((playbookId: string) => {
    setActionError(null);
    setSelectedPlaybookId(playbookId);
  }, []);

  /**
   * Issue #598/#611: show one playbook's version trail in the overlay. Does
   * NOT touch `selectedPlaybookId` — opening a playbook's history while
   * another's standing guidance is on screen below is an ordinary thing to
   * do, and it used to silently switch both.
   */
  const showHistoryFor = useCallback(
    (playbookId: string) => {
      setActionError(null);
      setNotesVersion(null);
      setHistoryPlaybookId(playbookId);
      void loadVersions(playbookId);
    },
    [loadVersions],
  );

  /**
   * Issue #598: open the overlay from a table row, remembering the control
   * that opened it so focus can be handed back on close.
   *
   * The opener is looked up by its own testid rather than taken from the
   * click event: `ct-button` MOVES `data-testid` onto the real inner
   * `<button>` it builds (see ct-button.ts), so the event's `currentTarget`
   * is the custom-element host — which is not focusable, and focusing it
   * would silently do nothing.
   */
  const openHistory = useCallback(
    (playbookId: string) => {
      // Quoted attribute selector with `"` and `\` escaped by hand — not
      // CSS.escape, which is for IDENT contexts and is not guaranteed present
      // in every runtime this suite runs under.
      const quoted = playbookId.replace(/["\\]/g, '\\$&');
      historyOpenerRef.current = document.querySelector<HTMLElement>(
        `[data-testid="playbook-versions-${quoted}"]`,
      );
      showHistoryFor(playbookId);
    },
    [showHistoryFor],
  );

  /**
   * Close the overlay and RETURN FOCUS to whatever opened it. A modal that
   * strands focus on the document body is worse than the inline panel it
   * replaced — a keyboard user has to tab from the top of the page to get
   * back to where they were.
   */
  const closeHistory = useCallback(() => {
    setHistoryPlaybookId(null);
    const opener = historyOpenerRef.current;
    historyOpenerRef.current = null;
    if (opener !== null && document.contains(opener)) {
      opener.focus();
    }
  }, []);

  // Move focus INTO the overlay when it opens, or a keyboard user is left
  // behind a modal with nothing to Escape from. The dialog container itself
  // takes it (tabIndex -1), rather than guessing which control matters —
  // from there Tab reaches everything inside in document order.
  useEffect(() => {
    if (historyPlaybookId !== null) {
      historyDialogRef.current?.focus();
    }
  }, [historyPlaybookId]);

  /**
   * One request + one refresh, with the server's own refusal message shown
   * verbatim when it has one (`readErrorDetail`) — the Gate-7 and
   * never-was-active messages are the two this screen most depends on, and
   * neither can be reconstructed client-side.
   */
  const runAction = useCallback(
    async (options: {
      key: string;
      path: string;
      method: 'POST' | 'PATCH' | 'DELETE';
      body?: unknown;
      technical: string;
      fallback: string;
      onSuccess?: () => void;
    }) => {
      setActionError(null);
      setPendingAction(options.key);
      try {
        const response = await jsonFetch(options.path, {
          method: options.method,
          ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
        });
        if (response.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ?? friendlyErrorMessage(options.technical, options.fallback),
          );
        }
        options.onSuccess?.();
      } catch (err) {
        setActionError(
          err instanceof Error ? err.message : friendlyErrorMessage(err, options.fallback),
        );
      } finally {
        setPendingAction(null);
      }
    },
    [],
  );

  const refreshAfterVersionChange = useCallback(
    (playbookId: string) => {
      // Activate/rollback/notes-save can all change the catalog's `status`
      // or `notes` (issue #464) — see this callback's call sites. One
      // invalidation now serves BOTH this table and the Review tab's dial
      // (issue #72); there is no second copy left to notify.
      invalidateCatalog();
      void loadVersions(playbookId);
    },
    [loadVersions],
  );

  const activateVersion = useCallback(
    (playbookId: string, version: string) =>
      runAction({
        key: `activate:${version}`,
        path: `/api/admin/playbooks/${encodeURIComponent(playbookId)}/versions/${encodeURIComponent(version)}/activate`,
        method: 'POST',
        technical: `POST activate ${playbookId}/${version}`,
        fallback: "We couldn't activate that version. Please try again.",
        onSuccess: () => refreshAfterVersionChange(playbookId),
      }),
    [refreshAfterVersionChange, runAction],
  );

  const downloadVersion = useCallback(
    async (playbookId: string, version: string) => {
      const path = `/api/admin/playbooks/${encodeURIComponent(playbookId)}/versions/${encodeURIComponent(version)}/download`;
      setActionError(null);
      setPendingAction(`download:${version}`);
      try {
        const response = await authorizedFetch(path);
        if (response.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (response.status === 410) {
          throw new Error('This playbook version is no longer available in storage.');
        }
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `GET playbook version download ${playbookId}/${version}`,
                "We couldn't download that playbook version. Please try again.",
              ),
          );
        }
        const data = (await response.json()) as { url?: string };
        if (!data.url) {
          throw new Error('Download URL missing from response.');
        }
        triggerBrowserDownload(data.url);
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(
                err,
                "We couldn't download that playbook version. Please try again.",
              ),
        );
      } finally {
        setPendingAction(null);
      }
    },
    [],
  );

  // Issue #595 retired the standalone `approveVersion`. Approval on its own
  // is no longer an action this screen offers — it is the first half of
  // `approveAndActivateVersion` below, which is the ONLY caller of
  // POST .../legal-approval now. There is deliberately no "approve but
  // don't activate" path left: it was the second click that bought nothing.

  /**
   * Issue #595: approve and activate as ONE operator act.
   *
   * Approval is the precheck activation requires, so making an admin click
   * twice for one decision bought nothing. What it must NOT become is a
   * self-approving upload: `record_legal_approval`'s docstring is explicit
   * that widening upload or activation to write `legal_approval` on their own
   * "would delete Gate 7 rather than satisfy it". Nothing here does that.
   * This is still an explicit human act, still strictly AFTER upload, still
   * naming the exact bytes, still recorded in the audit trail — the control
   * Gate 7 exists to preserve. Only the click count changed.
   *
   * Two existing calls, sequenced client-side, deliberately NOT a new
   * combined route: a server-side "approve and activate" would duplicate the
   * gate and give it a second place to drift from `activate_release_bundle`.
   *
   * The half-completed sequence is the interesting case. If activation fails
   * after approval succeeded, the approval record STANDS — that is correct
   * and auditable, and pretending otherwise would mean either lying about the
   * trail or inventing an un-approve route. So the failure is surfaced with
   * both facts: the server's own refusal, verbatim, plus the plain statement
   * that approval landed and the version did not go live. The trail is then
   * re-read either way, so the row shows the approval it really recorded and
   * the operator can retry the activation alone from the plain Activate the
   * refreshed row now offers.
   */
  const approveAndActivateVersion = useCallback(
    async (playbookId: string, version: string, contentHash: string) => {
      const base = `/api/admin/playbooks/${encodeURIComponent(playbookId)}/versions/${encodeURIComponent(version)}`;
      setActionError(null);
      setPendingAction(`approve-activate:${version}`);
      try {
        const approvalResponse = await jsonFetch(`${base}/legal-approval`, {
          method: 'POST',
          body: JSON.stringify({ content_hash: contentHash }),
        });
        if (approvalResponse.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (!approvalResponse.ok) {
          const detail = await readErrorDetail(approvalResponse);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `POST legal-approval ${playbookId}/${version}`,
                "We couldn't record approval for that version. Please try again.",
              ),
          );
        }

        const activateResponse = await jsonFetch(`${base}/activate`, { method: 'POST' });
        if (activateResponse.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (!activateResponse.ok) {
          const detail = await readErrorDetail(activateResponse);
          const refusal =
            detail ??
            friendlyErrorMessage(
              `POST activate ${playbookId}/${version}`,
              "We couldn't activate that version.",
            );
          throw new Error(
            `${refusal} The approval was recorded and stands, but this version is not live — ` +
              'activate it from its row once that is resolved.',
          );
        }
        refreshAfterVersionChange(playbookId);
      } catch (err) {
        setActionError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(
                err,
                "We couldn't approve and activate that version. Please try again.",
              ),
        );
        // Re-read even on failure: a half-completed sequence must show the
        // approval it really did record, not the pre-click state.
        refreshAfterVersionChange(playbookId);
      } finally {
        setPendingAction(null);
      }
    },
    [refreshAfterVersionChange],
  );

  const rollBackVersion = useCallback(
    (playbookId: string, version: string) =>
      runAction({
        key: `rollback:${version}`,
        path: `/api/admin/playbooks/${encodeURIComponent(playbookId)}/versions/${encodeURIComponent(version)}/rollback`,
        method: 'POST',
        technical: `POST rollback ${playbookId}/${version}`,
        fallback: "We couldn't roll back to that version. Please try again.",
        onSuccess: () => refreshAfterVersionChange(playbookId),
      }),
    [refreshAfterVersionChange, runAction],
  );

  const saveNotes = useCallback(
    (playbookId: string, version: string, notes: string) =>
      runAction({
        key: `notes:${version}`,
        path: `/api/admin/playbooks/${encodeURIComponent(playbookId)}/versions/${encodeURIComponent(version)}/notes`,
        method: 'PATCH',
        body: { notes },
        technical: `PATCH notes ${playbookId}/${version}`,
        fallback: "We couldn't save that note. Please try again.",
        onSuccess: () => {
          setNotesVersion(null);
          refreshAfterVersionChange(playbookId);
        },
      }),
    [refreshAfterVersionChange, runAction],
  );

  const renamePlaybook = useCallback(
    (playbookId: string, displayName: string) =>
      runAction({
        key: `rename:${playbookId}`,
        path: `/api/admin/playbooks/${encodeURIComponent(playbookId)}`,
        method: 'PATCH',
        body: { display_name: displayName },
        technical: `PATCH rename ${playbookId}`,
        fallback: "We couldn't rename that playbook. Please try again.",
        onSuccess: () => {
          setRenamingId(null);
          // Issue #464: the dial elsewhere in the app shows this same
          // display_name and has no way to know it changed on its own.
          invalidateCatalog();
        },
      }),
    [runAction],
  );

  const removePlaybook = useCallback(
    (playbookId: string) =>
      runAction({
        key: `remove:${playbookId}`,
        path: `/api/admin/playbooks/${encodeURIComponent(playbookId)}`,
        method: 'DELETE',
        technical: `DELETE playbook ${playbookId}`,
        fallback: "We couldn't remove that playbook. Please try again.",
        onSuccess: () => {
          // The removed playbook's trail and instructions are gone with it;
          // drop BOTH references (issue #611 split them) rather than leaving
          // a pane or a table of rows that no longer exist.
          setSelectedPlaybookId((current) => (current === playbookId ? null : current));
          setHistoryPlaybookId((current) => (current === playbookId ? null : current));
          setVersionsLoad((current) =>
            historyPlaybookId === playbookId ? { status: 'loading' } : current,
          );
          // Issue #464: a removed playbook must stop being a selectable
          // option on the dial, not just disappear from this table.
          invalidateCatalog();
        },
      }),
    [historyPlaybookId, runAction],
  );

  const submitUpload = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setUploadError(null);
      setUploadResult(null);

      const targetId = uploadPlaybookId.trim();
      const version = uploadVersion.trim();
      if (targetId === '') {
        setUploadError('Choose which playbook this version belongs to.');
        return;
      }
      if (version === '') {
        setUploadError('Give this version an identifier. It has to be one no earlier upload used.');
        return;
      }
      if (!uploadFile) {
        setUploadError('Choose the file that holds this version.');
        return;
      }

      setUploading(true);
      try {
        const form = new FormData();
        form.append('file', uploadFile);
        form.append('version', version);
        // No `content_hash` field: the server computes the hash over the
        // bytes it received and only ever validates a client-supplied one
        // against it. Sending our own would add a way to fail, never a way
        // to be believed.

        // authorizedFetch directly, NOT the jsonFetch wrapper above: a
        // multipart body needs the browser to set Content-Type with its own
        // generated boundary, which forcing `application/json` would break.
        const response = await authorizedFetch(
          `/api/admin/playbooks/${encodeURIComponent(targetId)}/versions`,
          { method: 'POST', body: form },
        );
        if (response.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `POST upload ${targetId}/${version}`,
                "We couldn't upload that version. Please try again.",
              ),
          );
        }

        // The upload route records no note (the version row lands with an
        // empty one), so an entered note is a second, deliberate call to the
        // notes route rather than a field on the upload itself.
        const note = uploadNotes.trim();
        if (note !== '') {
          const notesResponse = await jsonFetch(
            `/api/admin/playbooks/${encodeURIComponent(targetId)}/versions/${encodeURIComponent(version)}/notes`,
            { method: 'PATCH', body: JSON.stringify({ notes: note }) },
          );
          if (notesResponse.status === 403) {
            setIsForbidden(true);
            return;
          }
          if (!notesResponse.ok) {
            const detail = await readErrorDetail(notesResponse);
            throw new Error(
              detail ??
                friendlyErrorMessage(
                  `PATCH notes ${targetId}/${version}`,
                  'The version was uploaded, but its note could not be saved. Edit it from the version history below.',
                ),
            );
          }
        }

        setUploadResult(
          'Uploaded. It is a draft until you activate it — nothing about the live review flow has changed yet.',
        );
        setUploadVersion('');
        setUploadNotes('');
        setUploadFile(null);
        setFileDropNonce((n) => n + 1);
        // Show the trail the upload just landed in, so the admin can go
        // straight to approving and activating it (issue #611: the overlay,
        // not the instructions selection — an upload says nothing about whose
        // standing guidance they were editing).
        invalidateCatalog();
        showHistoryFor(targetId);
      } catch (err) {
        setUploadError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't upload that version. Please try again."),
        );
      } finally {
        setUploading(false);
      }
    },
    [showHistoryFor, uploadFile, uploadNotes, uploadPlaybookId, uploadVersion],
  );

  /**
   * Issue #597: read the chosen artifact and derive its version identifier,
   * the moment it is chosen — so the operator SEES the value before submit
   * rather than discovering it in the response.
   *
   * Owner decision 2026-08-22, option (a): `identity.version` wins when the
   * artifact has an opinion; otherwise the value is derived from the upload
   * date plus a short `content_hash` prefix. The rule itself lives in
   * `opfIdentity.ts` as a pure function — see that module for why the date is
   * UTC and why a same-day re-upload of identical bytes SHOULD collide.
   *
   * A file we cannot derive from is refused here with an explanation. There
   * is deliberately no fallback to a typed value: option (b) was rejected.
   *
   * This is a read for display and for the `version` form field, never a
   * gate. The server re-parses the bytes it actually received and validates
   * them (`opf_load._validate_doc`, `require_identity=True`); a client that
   * read the file wrong is refused there.
   */
  const chooseCreateFile = useCallback(async (file: File | null) => {
    setCreateFile(file);
    setCreateVersion(null);
    setCreateError(null);
    setCreateResult(null);
    if (file === null) {
      return;
    }

    let text: string;
    try {
      text = await file.text();
    } catch {
      setCreateError("We couldn't read that file. Please choose it again.");
      return;
    }

    const identity = readOpfIdentity(text);
    const derived = identity === null ? null : deriveVersionIdentifier(identity, new Date());
    if (derived === null) {
      setCreateError(
        "We couldn't read a version identifier from that file. An OPF document has to carry " +
          'an identity block — either identity.version, or identity.content_hash to derive ' +
          'one from. Nothing here is typed by hand, so there is no way to supply it another way.',
      );
      return;
    }
    setCreateVersion(derived);
  }, []);

  /**
   * Create a brand-new playbook_id + its first version (issue #485). Unlike
   * `submitUpload` above, there is no target playbook_id to send: the
   * server derives one from the uploaded OPF document and returns it, and
   * THAT returned id (never a client guess) is what gets selected below.
   */
  const submitCreate = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setCreateError(null);
      setCreateResult(null);

      if (!createFile) {
        setCreateError('Choose the OPF file this new playbook is compiled from.');
        return;
      }
      // Issue #597: `createVersion` is derived from `createFile` and cannot be
      // anything else. A null here means the chosen file carried no identity
      // to derive from — `chooseCreateFile` has already said so, and there is
      // no typed value to fall back to, so the only correct move is to refuse.
      const version = createVersion;
      if (version === null) {
        setCreateError(
          "That file carries no version identifier and none could be derived from it. " +
            'Choose an OPF document with an identity block.',
        );
        return;
      }

      setCreating(true);
      try {
        const form = new FormData();
        form.append('file', createFile);
        form.append('version', version);

        // authorizedFetch directly, NOT jsonFetch: a multipart body needs
        // the browser's own generated Content-Type boundary (same reason
        // submitUpload above bypasses jsonFetch).
        const response = await authorizedFetch('/api/admin/playbooks', {
          method: 'POST',
          body: form,
        });
        if (response.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                'POST /api/admin/playbooks',
                "We couldn't upload that playbook. Please try again.",
              ),
          );
        }
        const created = (await response.json()) as { playbook_id: string; version: string };

        // Same "note is a second, deliberate call" pattern as submitUpload:
        // the create route records no note of its own.
        const note = createNotes.trim();
        if (note !== '') {
          const notesResponse = await jsonFetch(
            `/api/admin/playbooks/${encodeURIComponent(created.playbook_id)}/versions/${encodeURIComponent(created.version)}/notes`,
            { method: 'PATCH', body: JSON.stringify({ notes: note }) },
          );
          if (notesResponse.status === 403) {
            setIsForbidden(true);
            return;
          }
          if (!notesResponse.ok) {
            const detail = await readErrorDetail(notesResponse);
            throw new Error(
              detail ??
                friendlyErrorMessage(
                  `PATCH notes ${created.playbook_id}/${created.version}`,
                  'The playbook was uploaded, but its note could not be saved. Edit it from the version history below.',
                ),
            );
          }
        }

        setCreateResult(
          `Uploaded "${created.playbook_id}". It is a draft until you approve and activate it — nothing about the live review flow has changed yet.`,
        );
        setCreateVersion(null);
        setCreateNotes('');
        setCreateFile(null);
        setCreateFileDropNonce((n) => n + 1);
        // The identity was derived server-side, not chosen here — open
        // whatever the server actually created, so the admin can go straight
        // to approving and activating it.
        invalidateCatalog();
        showHistoryFor(created.playbook_id);
      } catch (err) {
        setCreateError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't upload that playbook. Please try again."),
        );
      } finally {
        setCreating(false);
      }
    },
    [createFile, createNotes, createVersion, showHistoryFor],
  );

  if (isForbidden || catalogForbidden) {
    return null;
  }

  const selectedPlaybook =
    playbooks?.find((entry) => entry.playbook_id === selectedPlaybookId) ?? null;
  // Issue #611: the overlay's subject, which is NOT necessarily the one whose
  // standing instructions are on screen below it.
  const historyPlaybook =
    playbooks?.find((entry) => entry.playbook_id === historyPlaybookId) ?? null;

  return (
    <section data-testid="admin-playbooks-panel" className="ct-section ct-stack">
      <CtToolbar>
        <div slot="actions">
          <CtButton
            type="button"
            variant="secondary"
            data-testid="admin-playbooks-create-toggle"
            onClick={() => {
              setCreateResult(null);
              setCreateError(null);
              setCreateOpen((open) => !open);
            }}
          >
            Upload new playbook
          </CtButton>
        </div>
      </CtToolbar>

      {/* A failed load is TERMINAL: the banner carries the message and a
          working retry, and the loader below is unreachable while it shows
          (issue #511). */}
      {playbooksLoad.status === 'failed' && (
        <div className="ct-stack">
          <CtBanner variant="danger" data-testid="admin-playbooks-error">
            {playbooksLoad.message}
          </CtBanner>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid="admin-playbooks-retry"
              onClick={invalidateCatalog}
            >
              Try again
            </CtButton>
          </div>
        </div>
      )}

      {actionError && (
        <CtBanner variant="danger" data-testid="admin-playbooks-action-error">
          {actionError}
        </CtBanner>
      )}

      {playbooksLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-playbooks-loading" label="Loading playbooks…" />
      ) : playbooks === null ? null : (
        <CtCard data-testid="admin-playbooks-table-panel">
          <div
            className="ct-row ct-row--between"
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '0.75rem 1rem',
              gap: '0.75rem',
              borderBottom: '1px solid var(--ct-border-subtle, rgba(255, 255, 255, 0.08))',
            }}
          >
            <span style={{ fontSize: 'var(--ct-text-sm)', fontWeight: 600, color: 'var(--ct-text-muted)' }}>
              Catalog
            </span>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
              <input
                type="search"
                placeholder="Filter playbooks…"
                data-testid="playbooks-search-input"
                value={playbookSearchQuery}
                onChange={(e) => setPlaybookSearchQuery(e.target.value)}
                style={{
                  padding: '0.25rem 0.6rem',
                  fontSize: 'var(--ct-text-sm)',
                  borderRadius: '6px',
                  background: 'var(--ct-bg, #1e1e1e)',
                  border: '1px solid var(--ct-border, rgba(255,255,255,0.15))',
                  color: 'inherit',
                  width: '14rem',
                }}
              />
              <span className="ct-muted" style={{ fontSize: 'var(--ct-text-sm)', whiteSpace: 'nowrap' }}>
                Showing {filteredPlaybooks.length} of {playbooks.length}
              </span>
            </div>
          </div>
          <CtTable>
            <table data-testid="playbooks-table">
              <thead>
                <tr>
                  <th>Playbook</th>
                  <th>Identifier</th>
                  <th>Status</th>
                  <th>Active version&apos;s note</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {playbooks.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="ct-table__empty" data-testid="admin-playbooks-empty">
                      No playbooks yet.
                    </td>
                  </tr>
                ) : filteredPlaybooks.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="ct-table__empty" data-testid="admin-playbooks-empty-filter">
                      No playbooks match &ldquo;{playbookSearchQuery}&rdquo;.
                      <br />
                      <button
                        type="button"
                        style={{
                          marginTop: '0.5rem',
                          background: 'none',
                          border: 'none',
                          color: 'var(--ct-accent)',
                          cursor: 'pointer',
                          textDecoration: 'underline',
                        }}
                        onClick={() => setPlaybookSearchQuery('')}
                      >
                        Clear filter
                      </button>
                    </td>
                  </tr>
                ) : (
                  filteredPlaybooks.map((entry) => (
                    <tr key={entry.playbook_id} data-testid={`playbook-row-${entry.playbook_id}`}>
                      <td>
                        {renamingId === entry.playbook_id ? (
                          <div className="ct-stack">
                            <CtField
                              label="Display name"
                              hint="Presentation only — the identifier every version and review is keyed on never changes. Leave it empty to restore the shipped name."
                            >
                              <input
                                data-testid={`playbook-rename-input-${entry.playbook_id}`}
                                type="text"
                                autoComplete="off"
                                value={renameDraft}
                                onChange={(e) => setRenameDraft(e.target.value)}
                              />
                            </CtField>
                            <div className="ct-actions">
                              <CtButton
                                type="button"
                                variant="primary"
                                size="sm"
                                data-testid={`playbook-rename-save-${entry.playbook_id}`}
                                disabled={pendingAction === `rename:${entry.playbook_id}`}
                                onClick={() => void renamePlaybook(entry.playbook_id, renameDraft)}
                              >
                                Save name
                              </CtButton>
                              <CtButton
                                type="button"
                                variant="ghost"
                                size="sm"
                                data-testid={`playbook-rename-cancel-${entry.playbook_id}`}
                                onClick={() => setRenamingId(null)}
                              >
                                Cancel
                              </CtButton>
                            </div>
                          </div>
                        ) : (
                          entry.display_name
                        )}
                      </td>
                      <td className="ct-table__mono">{entry.playbook_id}</td>
                      <td data-testid={`playbook-status-${entry.playbook_id}`}>
                        <CtChip variant={catalogChipVariant(entry.status)} dot={entry.status === 'active'}>
                          {catalogStatusLabel(entry.status)}
                        </CtChip>
                      </td>
                      <td>{entry.notes === '' ? '—' : linkifyText(entry.notes)}</td>
                      <td>
                        {/* Issue #598: ONE group, two bands. The cell used to
                            be three full-size buttons in a plain
                            `.ct-actions`, which in a narrow table column wrap
                            one-per-line — so a two-row table read as a wall of
                            six stacked buttons, with the one-way door carrying
                            the same visual weight as Rename. The routine
                            actions now sit together in their own band, and the
                            destructive one sits in a separate band that
                            `.ct-row-actions` pushes to the end and separates
                            with a rule (see app.css). Grouping, not just
                            ordering: "third in a stack" is exactly what this
                            replaces. */}
                        <div
                          className="ct-row-actions ct-actions-grid-2x2"
                          role="group"
                          data-testid={`playbook-row-actions-${entry.playbook_id}`}
                          aria-label={`Actions for ${entry.display_name}`}
                        >
                          <div
                            className="ct-row-actions__main"
                            data-testid={`playbook-row-actions-main-${entry.playbook_id}`}
                          >
                            {/* Issue #611: standing instructions get their
                                OWN control. #605 left them reachable only
                                through "Version history", and #598 then made
                                that button open a modal — so the label was
                                not merely vague, it pointed elsewhere. This
                                one only chooses whose guidance the pane below
                                shows; it opens nothing. */}
                            <CtButton
                              type="button"
                              variant="secondary"
                              size="sm"
                              data-testid={`playbook-instructions-${entry.playbook_id}`}
                              onClick={() => selectPlaybook(entry.playbook_id)}
                            >
                              Standing instructions
                            </CtButton>
                            <CtButton
                              type="button"
                              variant="secondary"
                              size="sm"
                              data-testid={`playbook-versions-${entry.playbook_id}`}
                              onClick={() => openHistory(entry.playbook_id)}
                            >
                              Version history
                            </CtButton>
                            <CtButton
                              type="button"
                              variant="secondary"
                              size="sm"
                              data-testid={`playbook-rename-${entry.playbook_id}`}
                              onClick={() => {
                                setActionError(null);
                                setRenamingId(entry.playbook_id);
                                setRenameDraft(entry.display_name);
                              }}
                            >
                              Rename
                            </CtButton>
                          </div>
                          <div
                            className="ct-row-actions__danger"
                            data-testid={`playbook-row-actions-danger-${entry.playbook_id}`}
                          >
                            <CtButton
                              type="button"
                              variant="danger"
                              size="sm"
                              confirm="Click again to remove"
                              data-testid={`playbook-remove-${entry.playbook_id}`}
                              disabled={pendingAction === `remove:${entry.playbook_id}`}
                              onClick={() => void removePlaybook(entry.playbook_id)}
                            >
                              Remove
                            </CtButton>
                          </div>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </CtTable>
        </CtCard>
      )}

      {/* Issue #598: version history is an OVERLAY. It used to expand inline
          here as a second table, pushing the page down under the list it
          belonged to.

          Not a native `<dialog>` + `showModal()`: jsdom implements no
          `showModal` at all (`typeof dialog.showModal === 'undefined'` under
          this harness), so every property that matters about a new overlay —
          its role, Escape, the focus return — would have been untestable.
          A `div[role=dialog][aria-modal]` with explicit focus management is
          testable, and the behaviour a native dialog gives for free is
          written out here instead. CTDS has no dialog primitive and #598 says
          not to invent a shared one in this ticket, so this stays local to
          this screen.

          It renders in the SAME DOM position the inline panel occupied, so
          the top-to-bottom reading order #605 established (list → version
          history → standing instructions → forms) is unchanged for anything
          walking the document. */}
      {historyPlaybookId !== null && (
        <div
          className="ct-overlay"
          data-testid="admin-playbooks-versions-overlay"
          // A click on the backdrop itself (never a click that bubbled up
          // from inside the card) dismisses, the ordinary modal convention.
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              closeHistory();
            }
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.stopPropagation();
              closeHistory();
            }
          }}
        >
        <div
          ref={historyDialogRef}
          role="dialog"
          aria-modal="true"
          aria-label={`Version history — ${historyPlaybook?.display_name ?? historyPlaybookId}`}
          tabIndex={-1}
          className="ct-overlay__panel"
        >
        <CtCard data-testid="admin-playbooks-versions-panel">
          <CtToolbar
            title={`Version history — ${historyPlaybook?.display_name ?? historyPlaybookId}`}
          >
            <div slot="actions">
              <CtButton
                type="button"
                variant="primary"
                size="sm"
                data-testid="admin-playbooks-upload-toggle"
                onClick={() => {
                  setUploadResult(null);
                  setUploadError(null);
                  if (historyPlaybookId) {
                    setUploadPlaybookId(historyPlaybookId);
                  }
                  setUploadOpen((open) => !open);
                }}
              >
                Upload version
              </CtButton>
              <CtButton
                type="button"
                variant="secondary"
                size="sm"
                data-testid="admin-playbooks-versions-close"
                onClick={closeHistory}
              >
                Close
              </CtButton>
            </div>
          </CtToolbar>

          {/* Permanent, not conditional: an upload is never self-activating,
              and an admin who is not told that will read a refused activation
              as a bug. See this module's docstring, constraint 1. */}
          <CtBanner variant="muted" data-testid="admin-playbooks-activation-note">
            Activating a version checks its content against the approved hash recorded for it.
            A version whose exact bytes were never approved is refused — uploading is not the
            same as putting a version in front of a counterparty. Approving is that separate
            act, and it goes live in the same step: &ldquo;Approve &amp; activate&rdquo; records
            approval of this version&apos;s exact bytes and then activates it, so every new
            review runs against it.
          </CtBanner>

          {versionsLoad.status === 'failed' ? (
            <div className="ct-stack">
              <CtBanner variant="danger" data-testid="admin-playbooks-versions-error">
                {versionsLoad.message}
              </CtBanner>
              <div className="ct-actions" role="group">
                <CtButton
                  type="button"
                  variant="secondary"
                  size="sm"
                  data-testid="admin-playbooks-versions-retry"
                  onClick={() => historyPlaybookId && void loadVersions(historyPlaybookId)}
                >
                  Try again
                </CtButton>
              </div>
            </div>
          ) : versions === null ? (
            <CtProgress
              data-testid="admin-playbooks-versions-loading"
              label="Loading version history…"
            />
          ) : (
            <>
              {versions.length >= 2 && (
                <div
                  style={{
                    marginBottom: '1rem',
                    padding: '0.75rem',
                    borderRadius: '6px',
                    background: 'var(--ct-bg-subtle, rgba(255, 255, 255, 0.03))',
                    border: '1px solid var(--ct-border-subtle, rgba(255, 255, 255, 0.08))',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                    }}
                  >
                    <span style={{ fontSize: 'var(--ct-text-sm)', fontWeight: 600 }}>
                      Version Comparison
                    </span>
                    <CtButton
                      type="button"
                      variant="ghost"
                      size="sm"
                      data-testid="playbook-diff-toggle"
                      onClick={() => {
                        if (!diffOpen) {
                          const activeVer =
                            versions.find((v) => v.status === 'active')?.version ??
                            versions[0].version;
                          const otherVer =
                            versions.find((v) => v.version !== activeVer)?.version ??
                            versions[1]?.version ??
                            '';
                          setDiffVersionA(activeVer);
                          setDiffVersionB(otherVer);
                        }
                        setDiffOpen((o) => !o);
                      }}
                    >
                      {diffOpen ? 'Hide comparison' : 'Compare versions'}
                    </CtButton>
                  </div>

                  {diffOpen && (
                    <div
                      className="ct-stack"
                      style={{ marginTop: '0.75rem', gap: '0.75rem' }}
                      data-testid="playbook-diff-panel"
                    >
                      <div
                        className="ct-row"
                        style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}
                      >
                        <label
                          style={{
                            fontSize: 'var(--ct-text-sm)',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.35rem',
                          }}
                        >
                          <span>Version A:</span>
                          <select
                            value={diffVersionA}
                            onChange={(e) => setDiffVersionA(e.target.value)}
                            data-testid="playbook-diff-select-a"
                            style={{
                              fontSize: 'var(--ct-text-sm)',
                              padding: '0.2rem 0.4rem',
                              borderRadius: '4px',
                              background: 'var(--ct-bg)',
                            }}
                          >
                            {versions.map((v) => (
                              <option key={v.version} value={v.version}>
                                v{v.version} ({v.status})
                              </option>
                            ))}
                          </select>
                        </label>

                        <label
                          style={{
                            fontSize: 'var(--ct-text-sm)',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.35rem',
                          }}
                        >
                          <span>Version B:</span>
                          <select
                            value={diffVersionB}
                            onChange={(e) => setDiffVersionB(e.target.value)}
                            data-testid="playbook-diff-select-b"
                            style={{
                              fontSize: 'var(--ct-text-sm)',
                              padding: '0.2rem 0.4rem',
                              borderRadius: '4px',
                              background: 'var(--ct-bg)',
                            }}
                          >
                            {versions.map((v) => (
                              <option key={v.version} value={v.version}>
                                v{v.version} ({v.status})
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>

                      {(() => {
                        const verA = versions.find((v) => v.version === diffVersionA);
                        const verB = versions.find((v) => v.version === diffVersionB);
                        if (!verA || !verB) return null;
                        const sameHash =
                          verA.content_hash &&
                          verB.content_hash &&
                          verA.content_hash === verB.content_hash;
                        return (
                          <div
                            style={{
                              display: 'grid',
                              gridTemplateColumns: 'repeat(2, 1fr)',
                              gap: '0.75rem',
                              padding: '0.75rem',
                              borderRadius: '4px',
                              background: 'var(--ct-bg-card, rgba(0,0,0,0.2))',
                              border:
                                '1px solid var(--ct-border-subtle, rgba(255,255,255,0.06))',
                              fontSize: 'var(--ct-text-sm)',
                            }}
                          >
                            <div>
                              <div style={{ fontWeight: 600, marginBottom: '0.35rem' }}>
                                Version {verA.version} ({verA.status})
                              </div>
                              <div className="ct-muted">
                                Hash:{' '}
                                <span className="ct-table__mono">
                                  {verA.content_hash ? shortenHash(verA.content_hash) : '—'}
                                </span>
                              </div>
                              <div className="ct-muted">
                                Uploaded by: {verA.uploaded_by || '—'}
                              </div>
                              <div style={{ marginTop: '0.4rem' }}>
                                <strong>Note:</strong> {verA.notes || '—'}
                              </div>
                            </div>
                            <div>
                              <div style={{ fontWeight: 600, marginBottom: '0.35rem' }}>
                                Version {verB.version} ({verB.status})
                              </div>
                              <div className="ct-muted">
                                Hash:{' '}
                                <span className="ct-table__mono">
                                  {verB.content_hash ? shortenHash(verB.content_hash) : '—'}
                                </span>
                              </div>
                              <div className="ct-muted">
                                Uploaded by: {verB.uploaded_by || '—'}
                              </div>
                              <div style={{ marginTop: '0.4rem' }}>
                                <strong>Note:</strong> {verB.notes || '—'}
                              </div>
                            </div>
                            <div
                              style={{
                                gridColumn: '1 / -1',
                                paddingTop: '0.4rem',
                                borderTop:
                                  '1px solid var(--ct-border-subtle, rgba(255,255,255,0.06))',
                              }}
                            >
                              <CtChip variant={sameHash ? 'ok' : 'warn'}>
                                {sameHash ? 'Identical content bytes' : 'Content bytes differ'}
                              </CtChip>
                            </div>
                          </div>
                        );
                      })()}
                    </div>
                  )}
                </div>
              )}
              <CtTable>
              <table data-testid="playbook-versions-table">
                <thead>
                  <tr>
                    <th>Version</th>
                    <th>Status</th>
                    <th>Content hash</th>
                    <th>Uploaded by</th>
                    <th>Uploaded</th>
                    <th>Note</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {versions.length === 0 ? (
                    <tr>
                      <td
                        colSpan={7}
                        className="ct-table__empty"
                        data-testid="admin-playbooks-versions-empty"
                      >
                        No versions uploaded for this playbook yet.
                      </td>
                    </tr>
                  ) : (
                    versions.map((row) => {
                      // The three facts the whole Actions cell below is a
                      // function of. Named once here rather than recomputed
                      // inline, because issues #476/#485/#594/#595 have each
                      // added a case to that decision and an inline chain of
                      // them stopped being readable.
                      const approved = isApproved(row);
                      const isActive = row.status === 'active';
                      // A row with no content_hash at all (written before
                      // issue #478) has nothing an approval could name, so it
                      // is neither approvable nor merged-actionable — it
                      // keeps the plain Activate and lets the server state
                      // the Gate-7 refusal in its own words.
                      const canApprove = row.content_hash !== undefined && !approved;

                      return (
                      <tr key={row.version} data-testid={`playbook-version-row-${row.version}`}>
                        <td className="ct-table__mono">{row.version}</td>
                        <td data-testid={`playbook-version-status-${row.version}`}>
                          <CtChip variant={versionChipVariant(row.status)}>{row.status}</CtChip>
                        </td>
                        <td
                          className="ct-table__mono"
                          data-testid={`playbook-version-hash-${row.version}`}
                          title={row.content_hash ?? ''}
                        >
                          {row.content_hash ? shortenHash(row.content_hash) : '—'}
                        </td>
                        <td className="ct-table__mono">{row.uploaded_by || '—'}</td>
                        <td className="ct-table__mono">{formatTimestamp(row.uploaded_at)}</td>
                        <td>
                          {notesVersion === row.version ? (
                            <div className="ct-stack">
                              <CtField label={`Note for version ${row.version}`}>
                                <textarea
                                  data-testid={`playbook-version-notes-input-${row.version}`}
                                  rows={3}
                                  value={notesDraft}
                                  onChange={(e) => setNotesDraft(e.target.value)}
                                />
                              </CtField>
                              <div className="ct-actions">
                                <CtButton
                                  type="button"
                                  variant="primary"
                                  size="sm"
                                  data-testid={`playbook-version-notes-save-${row.version}`}
                                  disabled={pendingAction === `notes:${row.version}`}
                                  onClick={() =>
                                    void saveNotes(row.playbook_id, row.version, notesDraft)
                                  }
                                >
                                  Save note
                                </CtButton>
                                <CtButton
                                  type="button"
                                  variant="ghost"
                                  size="sm"
                                  data-testid={`playbook-version-notes-cancel-${row.version}`}
                                  onClick={() => setNotesVersion(null)}
                                >
                                  Cancel
                                </CtButton>
                              </div>
                            </div>
                          ) : (
                            <div className="ct-stack">
                              <span>{row.notes === '' ? '—' : linkifyText(row.notes)}</span>
                              <CtButton
                                type="button"
                                variant="ghost"
                                size="sm"
                                data-testid={`playbook-version-notes-edit-${row.version}`}
                                onClick={() => {
                                  setActionError(null);
                                  setNotesVersion(row.version);
                                  setNotesDraft(row.notes);
                                }}
                              >
                                {row.notes === '' ? 'Add a note' : 'Edit note'}
                              </CtButton>
                            </div>
                          )}
                        </td>
                        <td>
                          <div className="ct-actions" role="group">
                            <CtButton
                              type="button"
                              variant="secondary"
                              size="sm"
                              data-testid={`playbook-version-download-${row.version}`}
                              disabled={pendingAction === `download:${row.version}`}
                              onClick={() => void downloadVersion(row.playbook_id, row.version)}
                            >
                              Download
                            </CtButton>
                            {/* The APPROVAL STATE of this row, as a note —
                                never a control. Since issue #595 the only
                                approval control is the merged action below.

                                Approved (issue #485): a quiet note, the same
                                hide-don't-disable convention as Activate /
                                Roll back (issue #476). content_hash and
                                legal_approval are both immutable once set,
                                so this can never go false again for the row.

                                Active but never approved (issue #594): only
                                the deploy seed can produce that, because it
                                bypasses Gate 7 on purpose (see
                                sample_playbooks.py). Approving it now is not
                                a precondition for anything — the version is
                                already live — so no control is offered, and
                                the bypass is STATED rather than hidden:
                                silently omitting it would leave an audit
                                screen implying the live playbook had been
                                approved when it never was.

                                A row with no content_hash at all (written
                                before issue #478) has nothing an approval
                                could name, so it gets neither. */}
                            {approved ? (
                              <span
                                className="ct-muted"
                                data-testid={`playbook-version-approved-note-${row.version}`}
                              >
                                Approved
                              </span>
                            ) : isActive && row.content_hash !== undefined ? (
                              <span
                                className="ct-muted"
                                data-testid={`playbook-version-unapproved-active-note-${row.version}`}
                              >
                                Never approved — it went live without one (the deploy seed
                                bypasses the approval gate).
                              </span>
                            ) : null}

                            {/* The one ACTION that puts a version live.
                                Issue #595 collapsed Approve + Activate into
                                it: approval is the precheck activation
                                requires, so two clicks bought nothing. Still
                                one explicit, audited act naming exact bytes,
                                still strictly after upload — Gate 7 intact.

                                An already-approved version keeps a PLAIN
                                Activate that calls only the activate route
                                (the rollback-to-a-previously-approved-version
                                path must not demand re-approval), and a
                                hashless row keeps it too so the server can
                                state the Gate-7 refusal in its own words. */}
                            {isActive ? (
                              // Activating the already-active version can't
                              // mean anything — no button to click, not a
                              // disabled one (issue #476). The status chip
                              // already says "active"; this is a quiet
                              // acknowledgement in the actions column so an
                              // admin isn't left wondering where the button
                              // went.
                              <span
                                className="ct-muted"
                                data-testid={`playbook-version-active-note-${row.version}`}
                              >
                                Currently active
                              </span>
                            ) : canApprove ? (
                              <CtButton
                                type="button"
                                variant="secondary"
                                size="sm"
                                data-testid={`playbook-version-approve-activate-${row.version}`}
                                disabled={pendingAction === `approve-activate:${row.version}`}
                                onClick={() =>
                                  void approveAndActivateVersion(
                                    row.playbook_id,
                                    row.version,
                                    row.content_hash as string,
                                  )
                                }
                              >
                                Approve &amp; activate
                              </CtButton>
                            ) : (
                              <CtButton
                                type="button"
                                variant="secondary"
                                size="sm"
                                data-testid={`playbook-version-activate-${row.version}`}
                                disabled={pendingAction === `activate:${row.version}`}
                                onClick={() => void activateVersion(row.playbook_id, row.version)}
                              >
                                Activate
                              </CtButton>
                            )}
                            {/* Only a `retired` row was ever actually active
                                (activate/rollback are the sole writers of
                                that status — see playbook_versions.py), so
                                it is the sole authoritative "has something to
                                roll back to" signal; a draft or the active
                                row itself offers no Roll back at all rather
                                than a disabled button with nowhere to go
                                (issue #476). The backend's own 409 is still
                                what is rendered if it disagrees. */}
                            {row.status === 'retired' && (
                              <CtButton
                                type="button"
                                variant="secondary"
                                size="sm"
                                data-testid={`playbook-version-rollback-${row.version}`}
                                disabled={pendingAction === `rollback:${row.version}`}
                                onClick={() => void rollBackVersion(row.playbook_id, row.version)}
                              >
                                Roll back
                              </CtButton>
                            )}
                          </div>
                        </td>
                      </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </CtTable>
            </>
          )}
        </CtCard>
        </div>
        </div>
      )}

      {/* Standing instructions (issue #605's merge) — the SAME selection as
          the version history above, never a second picker. Keyed on the
          playbook id so switching selection fully remounts this pane onto
          the newly-selected playbook (see AdminInstructions.tsx's
          docstring, "Selection"). */}
      {selectedPlaybookId !== null && (
        <AdminInstructions
          key={selectedPlaybookId}
          playbookId={selectedPlaybookId}
          playbookDisplayName={selectedPlaybook?.display_name ?? selectedPlaybookId}
        />
      )}

      {/* Create/upload forms, LAST (issue #605's confirmed scope order) —
          moved here from just under the toolbar so the screen reads
          list → version history → standing instructions → forms, even
          though the buttons that open them stay in the toolbar above. */}
      {createOpen && (
        <CtCard data-testid="admin-playbooks-create-panel">
          <form className="ct-stack" noValidate onSubmit={submitCreate}>
            <CtToolbar title="Upload a new playbook" />

            {/* No playbook_id field here at all (issue #485): identity is
                derived server-side from the uploaded document's own
                agreement_type, never typed by an operator. */}
            <CtBanner variant="muted" data-testid="admin-playbooks-create-identity-note">
              The playbook_id is read from the document itself — its
              agreement_type — not typed here. If the file's agreement_type
              already matches an existing playbook, the server refuses and
              names it: upload a new version onto that one instead.
            </CtBanner>

            {createError && (
              <CtBanner variant="danger" data-testid="admin-playbooks-create-error">
                {createError}
              </CtBanner>
            )}
            {createResult && (
              <CtBanner variant="ok" data-testid="admin-playbooks-create-success">
                {createResult}
              </CtBanner>
            )}

            {/* The file comes FIRST now (issue #597): the version identifier
                below is read out of it, so asking for it first and showing
                the derived value underneath is the order the form actually
                works in.

                OPF only — a legacy v1 playbook carries no agreement_type to
                derive an identity from, so the create route refuses one
                (unlike "Upload version" below, which also accepts v1 JSON
                for an EXISTING playbook_id). */}
            {/* Issue #609 (#602's AdminPlaybooks split): these two ARE one
                thought since #597 — choose the artifact, and read back the
                version identifier extracted from it. Side by side they say
                that; stacked full-width they read as two unrelated steps.
                `ct-columns` only repositions them visually, so the DOM and
                tab order (file, then the value it produces) is untouched and
                the collapsed single-column reading is still correct. */}
            <CtColumns>
              <CtFileDrop
                key={createFileDropNonce}
                data-testid="admin-playbooks-create-file"
                label="Drop the OPF document (.opf.html or .opf.json) here or browse"
                accept=".opf.html,.opf.json"
                onFiles={(event) => void chooseCreateFile(event.detail.files[0] ?? null)}
              />

              {/* Issue #597: DERIVED, never typed. There is no input here on
                  purpose — the owner rejected prefill-but-editable, so the
                  only way to change this value is to compile a different
                  artifact. `<output>` rather than a `<p>` because it is
                  literally the result of a calculation, and because it is a
                  labelable element, which keeps ct-field's `label[for]`
                  wiring valid (a `<p>` would leave the label pointing at
                  nothing). Operator prose belongs in the Note field below,
                  which is exactly where the owner said to put it.

                  Deliberately NOT marked `narrow` (issue #609): `<output>` is
                  inline, so it was never stretched by ct-field's
                  `align-items: stretch`, and ct-field.css's narrow rule only
                  targets `:is(input, select, textarea)` anyway — the
                  attribute here would be inert decoration. */}
              <CtField
                label="Version identifier"
                hint="Read from the document itself: its identity.version when the artifact declares one, otherwise derived from today's date and the document's content hash. Not editable — put your own wording in the Note below."
              >
                <output className="ct-mono" data-testid="admin-playbooks-create-version">
                  {createVersion ?? 'Choose a file above — the identifier is read from it.'}
                </output>
              </CtField>
            </CtColumns>

            <CtField
              label="Note (optional)"
              hint="Free text stored against this first version — what it is, and why. You can edit it later."
            >
              <textarea
                data-testid="admin-playbooks-create-notes"
                rows={3}
                value={createNotes}
                onChange={(e) => setCreateNotes(e.target.value)}
              />
            </CtField>

            <div className="ct-actions">
              <CtButton
                type="submit"
                variant="primary"
                data-testid="admin-playbooks-create-submit"
                disabled={creating}
                loading={creating}
              >
                {creating ? 'Uploading…' : 'Upload new playbook'}
              </CtButton>
            </div>
          </form>
        </CtCard>
      )}

      {uploadOpen && (
        <CtCard data-testid="admin-playbooks-upload-panel">
          <form className="ct-stack" noValidate onSubmit={submitUpload}>
            <CtToolbar title="Upload a version" />

            {uploadError && (
              <CtBanner variant="danger" data-testid="admin-playbooks-upload-error">
                {uploadError}
              </CtBanner>
            )}
            {uploadResult && (
              <CtBanner variant="ok" data-testid="admin-playbooks-upload-success">
                {uploadResult}
              </CtBanner>
            )}

            {/* Issue #609 (#602's AdminPlaybooks split): the two SHORT
                fields on this form — which playbook, and which version —
                are one question, and were each spanning the whole panel.
                The file drop and the note below stay out of the grid on
                purpose: both are legitimately full width, and halving them
                would be a new defect rather than the fix #602 asked for. */}
            <CtColumns>
              <CtField label="Playbook" hint="The catalog entry this version belongs to.">
                <select
                  data-testid="admin-playbooks-upload-playbook"
                  value={uploadPlaybookId}
                  onChange={(e) => setUploadPlaybookId(e.target.value)}
                >
                  <option value="">Choose a playbook…</option>
                  {(playbooks ?? []).map((entry) => (
                    <option key={entry.playbook_id} value={entry.playbook_id}>
                      {entry.display_name}
                    </option>
                  ))}
                </select>
              </CtField>

              {/* `narrow` (ct-field.ts, #601): a version identifier is a
                  short mono token, so it keeps its own intrinsic width
                  instead of filling the column. The Playbook select next to
                  it is deliberately NOT narrow — a display name is
                  arbitrarily long, and clipping it to intrinsic width would
                  be a new defect. */}
              <CtField
                narrow
                label="Version identifier"
                hint="Uploads are append-only: a version identifier that was used before is refused, so a corrected file needs a new one."
              >
                <input
                  data-testid="admin-playbooks-upload-version"
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  className="ct-mono"
                  value={uploadVersion}
                  onChange={(e) => setUploadVersion(e.target.value)}
                />
              </CtField>
            </CtColumns>

            {/* The accept list is a browse-dialog hint only — the upload
                route hashes whatever bytes it receives and enforces no
                extension. It lists the OPF document forms plus the plain
                JSON a v1 playbook artifact ships as. */}
            <CtFileDrop
              key={fileDropNonce}
              data-testid="admin-playbooks-upload-file"
              label="Drop this version's file here or browse"
              accept=".opf.html,.opf.json,.json"
              onFiles={(event) => setUploadFile(event.detail.files[0] ?? null)}
            />

            <CtField
              label="Note (optional)"
              hint="Free text stored against this version — what changed, and why. You can edit it later."
            >
              <textarea
                data-testid="admin-playbooks-upload-notes"
                rows={3}
                value={uploadNotes}
                onChange={(e) => setUploadNotes(e.target.value)}
              />
            </CtField>

            <div className="ct-actions">
              <CtButton
                type="submit"
                variant="primary"
                data-testid="admin-playbooks-upload-submit"
                disabled={uploading}
                loading={uploading}
              >
                {uploading ? 'Uploading…' : 'Upload version'}
              </CtButton>
            </div>
          </form>
        </CtCard>
      )}
    </section>
  );
}
