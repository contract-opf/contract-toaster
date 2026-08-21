/**
 * AdminPlaybooks — the playbook lifecycle admin surface (issue #434,
 * docs/frontend-design-system.md §15.1).
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
 *   2. **Rollback only accepts a `retired` target.** `rollback_playbook_version`
 *      rejects a version that was never active ("rolling back to a version
 *      that was never active is just a (second) activation"). Since only
 *      activate/rollback ever write `retired`, that status IS the "has
 *      something to roll back to" signal — so a draft or the currently-
 *      active row offers no Roll back button at all (hidden, not disabled;
 *      issue #476) rather than a dead one with nowhere to go. The backend's
 *      409 is still the authority and is rendered verbatim if it disagrees.
 *   3. **Creating a playbook never takes an operator-typed identifier.**
 *      `POST /api/admin/playbooks` (issue #485) derives the new
 *      playbook_id from the uploaded OPF document's own `agreement_type.id`
 *      — the create form has no playbook_id field at all, only a file and a
 *      version identifier, and the derived id is read back from the
 *      response and shown to the admin (and used to select that playbook's
 *      version history) rather than guessed client-side.
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

import { useCallback, useEffect, useState } from 'react';
import { failedLoad, type LoadState } from './loadState';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';
import { linkifyText } from './linkify';
import {
  CtBanner,
  CtButton,
  CtCard,
  CtChip,
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

/** Catalog status. Exactly two values exist (issue #433 removed the third). */
export type PlaybookCatalogStatus = 'active' | 'coming_soon';

export interface PlaybookCatalogEntry {
  playbook_id: string;
  display_name: string;
  status: PlaybookCatalogStatus;
  /** The currently-active version's admin-editable note, or "". */
  notes: string;
}

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

export interface AdminPlaybooksProps {
  /**
   * Issue #464: called after any mutation that can change what
   * GET /api/playbooks returns (rename, remove, activate, rollback, notes —
   * `notes` is part of the catalog response too, see `_load_playbook_catalog`).
   * App.tsx wires this to bump a refresh signal ReviewSubmission's dial
   * listens on, so a rename/remove lands there without a reload. Optional so
   * this panel still works standalone (every existing test renders it with
   * no props).
   */
  onCatalogChange?: () => void;
}

export default function AdminPlaybooks({
  onCatalogChange,
}: AdminPlaybooksProps = {}): React.ReactElement | null {
  // Issue #511: two explicit three-state loads. Both previously shared ONE
  // `error` string alongside a `T | null` sentinel, so a failed catalog fetch
  // left a permanent "Loading playbooks…" under a danger banner, and a failed
  // version fetch was indistinguishable from one still in flight.
  const [playbooksLoad, setPlaybooksLoad] = useState<LoadState<PlaybookCatalogEntry[]>>({
    status: 'loading',
  });
  const playbooks = playbooksLoad.status === 'ready' ? playbooksLoad.data : null;
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  // Any admin route answering 403 hides the panel outright — no client-side
  // admin claim is trusted here (see this module's docstring).
  const [isForbidden, setIsForbidden] = useState(false);

  // Version history is loaded for one playbook at a time (the trail route is
  // per-playbook), so the table below is scoped to this selection.
  const [selectedPlaybookId, setSelectedPlaybookId] = useState<string | null>(null);
  const [versionsLoad, setVersionsLoad] = useState<LoadState<PlaybookVersionRow[]>>({
    status: 'loading',
  });
  // Derived views, so the render below reads exactly as it did.
  const versions = versionsLoad.status === 'ready' ? versionsLoad.data : null;

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

  // Create-playbook form (issue #485) — deliberately separate from the
  // upload form above, and with NO playbook_id field: identity comes from
  // the uploaded OPF document itself (POST /api/admin/playbooks derives it
  // server-side), never operator free-text.
  const [createOpen, setCreateOpen] = useState(false);
  const [createVersion, setCreateVersion] = useState('');
  const [createFile, setCreateFile] = useState<File | null>(null);
  const [createNotes, setCreateNotes] = useState('');
  const [createError, setCreateError] = useState<string | null>(null);
  const [createResult, setCreateResult] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createFileDropNonce, setCreateFileDropNonce] = useState(0);

  const loadPlaybooks = useCallback(async () => {
    try {
      const response = await jsonFetch('/api/playbooks');
      if (response.status === 403) {
        setIsForbidden(true);
        return;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET /api/playbooks returned HTTP ${response.status}`,
            "We couldn't load your playbooks. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as { playbooks: PlaybookCatalogEntry[] };
      setPlaybooksLoad({ status: 'ready', data: data.playbooks });
    } catch (err) {
      setPlaybooksLoad(failedLoad(err, "We couldn't load your playbooks. Please try again."));
    }
  }, []);

  const retryLoadPlaybooks = useCallback(() => {
    setPlaybooksLoad({ status: 'loading' });
    void loadPlaybooks();
  }, [loadPlaybooks]);

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

  useEffect(() => {
    void loadPlaybooks();
  }, [loadPlaybooks]);

  const selectPlaybook = useCallback(
    (playbookId: string) => {
      setActionError(null);
      setNotesVersion(null);
      setSelectedPlaybookId(playbookId);
      void loadVersions(playbookId);
    },
    [loadVersions],
  );

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
      void loadPlaybooks();
      void loadVersions(playbookId);
      // Activate/rollback/notes-save can all change the catalog's `status`
      // or `notes` (issue #464) — see this callback's call sites.
      onCatalogChange?.();
    },
    [loadPlaybooks, loadVersions, onCatalogChange],
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

  /**
   * Records legal approval of EXACTLY the hash already shown on this row
   * (issue #485) — never an operator-typed value. The server independently
   * re-checks it against the row's own `content_hash` and 409s a mismatch,
   * but this client never gives an admin a free-text field to get it wrong
   * in the first place.
   */
  const approveVersion = useCallback(
    (playbookId: string, version: string, contentHash: string) =>
      runAction({
        key: `approve:${version}`,
        path: `/api/admin/playbooks/${encodeURIComponent(playbookId)}/versions/${encodeURIComponent(version)}/legal-approval`,
        method: 'POST',
        body: { content_hash: contentHash },
        technical: `POST legal-approval ${playbookId}/${version}`,
        fallback: "We couldn't record approval for that version. Please try again.",
        onSuccess: () => refreshAfterVersionChange(playbookId),
      }),
    [refreshAfterVersionChange, runAction],
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
          void loadPlaybooks();
          // Issue #464: the dial elsewhere in the app shows this same
          // display_name and has no way to know it changed on its own.
          onCatalogChange?.();
        },
      }),
    [loadPlaybooks, onCatalogChange, runAction],
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
          // The removed playbook's trail is gone with it; drop the selection
          // rather than leaving a table of rows that no longer exist.
          setSelectedPlaybookId((current) => (current === playbookId ? null : current));
          setVersionsLoad((current) =>
            selectedPlaybookId === playbookId ? { status: 'loading' } : current,
          );
          void loadPlaybooks();
          // Issue #464: a removed playbook must stop being a selectable
          // option on the dial, not just disappear from this table.
          onCatalogChange?.();
        },
      }),
    [loadPlaybooks, onCatalogChange, runAction, selectedPlaybookId],
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
        setSelectedPlaybookId(targetId);
        void loadPlaybooks();
        void loadVersions(targetId);
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
    [loadPlaybooks, loadVersions, uploadFile, uploadNotes, uploadPlaybookId, uploadVersion],
  );

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

      const version = createVersion.trim();
      if (version === '') {
        setCreateError("Give this playbook's first version an identifier.");
        return;
      }
      if (!createFile) {
        setCreateError('Choose the OPF file this new playbook is compiled from.');
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
                "We couldn't create that playbook. Please try again.",
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
                  'The playbook was created, but its note could not be saved. Edit it from the version history below.',
                ),
            );
          }
        }

        setCreateResult(
          `Created "${created.playbook_id}". It is a draft until you approve and activate it — nothing about the live review flow has changed yet.`,
        );
        setCreateVersion('');
        setCreateNotes('');
        setCreateFile(null);
        setCreateFileDropNonce((n) => n + 1);
        // The identity was derived server-side, not chosen here — select
        // whatever the server actually created, so the admin can go
        // straight to approving/activating it.
        setSelectedPlaybookId(created.playbook_id);
        void loadPlaybooks();
        void loadVersions(created.playbook_id);
        onCatalogChange?.();
      } catch (err) {
        setCreateError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't create that playbook. Please try again."),
        );
      } finally {
        setCreating(false);
      }
    },
    [createFile, createNotes, createVersion, loadPlaybooks, loadVersions, onCatalogChange],
  );

  if (isForbidden) {
    return null;
  }

  const selectedPlaybook =
    playbooks?.find((entry) => entry.playbook_id === selectedPlaybookId) ?? null;

  return (
    <section data-testid="admin-playbooks-panel" className="ct-section ct-stack">
      <CtToolbar title="Playbooks">
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
            Create playbook
          </CtButton>
          <CtButton
            type="button"
            variant="primary"
            data-testid="admin-playbooks-upload-toggle"
            onClick={() => {
              setUploadResult(null);
              setUploadError(null);
              setUploadOpen((open) => {
                const next = !open;
                const firstPlaybookId = playbooks?.[0]?.playbook_id;
                if (next && uploadPlaybookId === '' && firstPlaybookId !== undefined) {
                  setUploadPlaybookId(selectedPlaybookId ?? firstPlaybookId);
                }
                return next;
              });
            }}
          >
            Upload version
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
              onClick={retryLoadPlaybooks}
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

      {createOpen && (
        <CtCard data-testid="admin-playbooks-create-panel">
          <form className="ct-stack" noValidate onSubmit={submitCreate}>
            <CtToolbar title="Create a playbook" />

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

            <CtField
              label="Version identifier"
              hint="The identifier for this playbook's first version."
            >
              <input
                data-testid="admin-playbooks-create-version"
                type="text"
                autoComplete="off"
                spellCheck={false}
                className="ct-mono"
                value={createVersion}
                onChange={(e) => setCreateVersion(e.target.value)}
              />
            </CtField>

            {/* OPF only — a legacy v1 playbook carries no agreement_type to
                derive an identity from, so the create route refuses one
                (unlike "Upload version" above, which also accepts v1 JSON
                for an EXISTING playbook_id). */}
            <CtFileDrop
              key={createFileDropNonce}
              data-testid="admin-playbooks-create-file"
              label="Drop the OPF document (.opf.html or .opf.json) here or browse"
              accept=".opf.html,.opf.json"
              onFiles={(event) => setCreateFile(event.detail.files[0] ?? null)}
            />

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
                {creating ? 'Creating…' : 'Create playbook'}
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

            <CtField
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

      {playbooksLoad.status === 'loading' ? (
        <CtProgress data-testid="admin-playbooks-loading" label="Loading playbooks…" />
      ) : playbooks === null ? null : (
        <CtCard data-testid="admin-playbooks-table-panel">
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
                ) : (
                  playbooks.map((entry) => (
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
                        <CtChip variant={catalogChipVariant(entry.status)}>
                          {catalogStatusLabel(entry.status)}
                        </CtChip>
                      </td>
                      <td>{entry.notes === '' ? '—' : linkifyText(entry.notes)}</td>
                      <td>
                        <div className="ct-actions" role="group">
                          <CtButton
                            type="button"
                            variant="secondary"
                            size="sm"
                            data-testid={`playbook-versions-${entry.playbook_id}`}
                            onClick={() => selectPlaybook(entry.playbook_id)}
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
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </CtTable>
        </CtCard>
      )}

      {selectedPlaybookId !== null && (
        <CtCard data-testid="admin-playbooks-versions-panel">
          <CtToolbar
            title={`Version history — ${selectedPlaybook?.display_name ?? selectedPlaybookId}`}
          />

          {/* Permanent, not conditional: an upload is never self-activating,
              and an admin who is not told that will read a refused activation
              as a bug. See this module's docstring, constraint 1. */}
          <CtBanner variant="muted" data-testid="admin-playbooks-activation-note">
            Activating a version checks its content against the approved hash recorded for it.
            A version whose exact bytes were never approved is refused — uploading is not the
            same as putting a version in front of a counterparty.
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
                  onClick={() => selectedPlaybookId && void loadVersions(selectedPlaybookId)}
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
                    versions.map((row) => (
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
                            {/* Gate 7 (issue #485): a row with a
                                content_hash that has never been approved
                                for THAT exact hash offers "Approve for
                                activation", sending back exactly the hash
                                already shown in this row's own Content hash
                                cell — never operator free-text. Once
                                approved (content_hash/legal_approval are
                                both immutable once set, so this can never
                                go false again for the same row), the button
                                is replaced with a quiet note, the same
                                hide-don't-disable convention as Activate/
                                Roll back below (issue #476). A row with no
                                content_hash at all (written before issue
                                #478) offers neither — there is nothing to
                                approve. */}
                            {row.content_hash && isApproved(row) ? (
                              <span
                                className="ct-muted"
                                data-testid={`playbook-version-approved-note-${row.version}`}
                              >
                                Approved
                              </span>
                            ) : row.content_hash ? (
                              <CtButton
                                type="button"
                                variant="secondary"
                                size="sm"
                                data-testid={`playbook-version-approve-${row.version}`}
                                disabled={pendingAction === `approve:${row.version}`}
                                onClick={() =>
                                  void approveVersion(
                                    row.playbook_id,
                                    row.version,
                                    row.content_hash as string,
                                  )
                                }
                              >
                                Approve for activation
                              </CtButton>
                            ) : null}
                            {row.status === 'active' ? (
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
                    ))
                  )}
                </tbody>
              </table>
            </CtTable>
          )}
        </CtCard>
      )}
    </section>
  );
}
