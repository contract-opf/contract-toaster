/**
 * AdminInstructions — the "Standing instructions" pane on the merged
 * Playbooks admin screen (issue #605 folded this in from its own
 * standalone "Playbook instructions" tab; issue #484, epic #481 sub-issue C
 * originated it there, itself replacing the old "Pen rules & posture" tab).
 *
 * ## What this is
 *
 * A plain-English, per-playbook "Standing instructions" box:
 *
 *   > What the toaster follows for a given review = the active playbook
 *   > version + the current standing instructions for that playbook (+
 *   > anything typed for that one review).
 *
 * This is the ADMIN AUTHORING surface for that middle term. It is a thin
 * client over #482's store (`backend/src/playbook_instructions.py`,
 * `GET`/`POST /api/admin/playbooks/{id}/instructions`) — every append-only,
 * monotonic-version, compare-and-set rule lives there, not here. This
 * component's job is: let an admin see what is currently in effect for a
 * playbook, edit it, save it without silently clobbering a concurrent
 * edit, and browse (and restore) its history.
 *
 * Unlike the pen-rules/posture layer it replaces (ARCHITECTURE.md →
 * "Guidance-precedence model" item 4), standing instructions are LIVE: a
 * saved version is picked up by the very next review run against this
 * playbook (issue #483 composes it into both review passes). So this pane
 * carries no permanent "nothing here does anything yet" banner — the
 * status line is the whole truth, not a caveat on top of an inert control.
 *
 * ## Precedence
 *
 * Floor > per-review guidance (the Review screen's own box) > standing
 * instructions (this pane) > the playbook's own positions. The mid-clause
 * of that sentence is `guidancePrecedenceCopy.ts`'s
 * `GUIDANCE_PRECEDENCE_COPY`, shared verbatim with `ReviewSubmission.tsx`'s
 * per-review guidance field so the two surfaces can never drift on wording
 * for the same underlying (non-mechanical) guarantee.
 *
 * ## Conflicts
 *
 * A save always sends `expected_current_version` (the version this admin's
 * page believes is current — `0` before anything has ever been saved). A
 * stale page, or a losing race against a concurrent save, comes back HTTP
 * 409 with the actual current version; per the issue's Notes this never
 * silently overwrites — the admin's own unsaved draft stays exactly as
 * typed, the freshly-current version is fetched and shown alongside it,
 * and saving again (now with the refreshed `expected_current_version`)
 * re-applies the same edit against the version everyone can now see.
 *
 * ## Selection (issue #605)
 *
 * This component used to own its own playbook picker (a `<select>`) and
 * its own `GET /api/playbooks` catalog fetch, because it rendered as a
 * standalone tab with nothing else on screen to pick a playbook from.
 * `AdminPlaybooks.tsx` now owns selection instead — its "Version history"
 * action is the SAME click that drives this pane, per the merge ticket's
 * coupling requirement ("selecting a playbook in the list drives the
 * instructions pane") — so this component takes the selected playbook as
 * a prop and renders only once one is chosen. `AdminPlaybooks` also mounts
 * it keyed on that id (`key={playbookId}`): switching the selection fully
 * unmounts and remounts this component rather than asking it to track "am
 * I still showing the selected playbook?" itself, which is what the old
 * `selectedPlaybookIdRef` stale-response guard existed for — deleted here,
 * since a freshly-mounted instance can never race a dead one (React drops
 * a state update against an unmounted component rather than ever getting
 * the chance to paint it).
 *
 * ## Layout: deliberately one column (issue #610)
 *
 * #602 applied the `ct-columns` two-column primitive (#601) across the
 * admin panels, and #610 re-checked this pane as part of that wave. The
 * answer here is a deliberate NO CHANGE, and this note exists so nobody
 * has to re-derive it:
 *
 *   - There is exactly ONE form control on this pane — the standing-
 *     instructions `<textarea>`. #602's own Notes single it out ("the
 *     standing-instructions textarea is legitimately wide; do not force
 *     it into a narrow column"), and docs/frontend-design-system.md §6
 *     says a single logical control with nothing to pair against stays
 *     in `ct-stack`.
 *   - The `<select>` playbook picker that used to be the second control
 *     on this screen is gone — #605 moved selection to
 *     `AdminPlaybooks.tsx` (see "Selection" above), so the pairing
 *     opportunity #610 was written against no longer exists in this file
 *     at all.
 *
 * If a later ticket adds a second short control here, re-evaluate then.
 * The textarea itself is not a candidate either way.
 *
 * ## Retiring "Pen rules & posture"
 *
 * `AdminPenRules.tsx` and its test were deleted by issue #484; the
 * `POST /api/admin/playbooks/{id}/pen-rules/validate` route it called stays
 * — it is now an API/CLI-only tooling endpoint (see ARCHITECTURE.md's
 * "Guidance-precedence model" item 4).
 *
 * ## Privilege
 *
 * Every route here 403s a non-admin caller; a 403 from any of them hides
 * this pane on its own (returns `null`) — the same defense-in-depth
 * posture as every other admin screen, and independent of whatever the
 * rest of the merged Playbooks screen is doing (App.tsx's `/api/me` probe
 * decides whether the whole screen mounts at all; the server stays
 * authoritative for every action within it).
 */

import { useCallback, useEffect, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage } from './api';
import { GUIDANCE_PRECEDENCE_COPY } from './guidancePrecedenceCopy';
import { CtBanner, CtButton, CtCard, CtField, CtProgress, CtToolbar } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/playbook_instructions.py and the two routes in
// backend/src/main.py (`get_admin_playbook_instructions` /
// `post_admin_playbook_instructions`).
// ---------------------------------------------------------------------------

export interface InstructionsVersion {
  version: number;
  text: string;
  saved_by: string | null;
  saved_at: number | null;
}

interface InstructionsGetResponse {
  current: InstructionsVersion | null;
  history: InstructionsVersion[];
}

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

function formatDate(epochSeconds: number | null): string {
  if (epochSeconds === null || epochSeconds === undefined) {
    return '—';
  }
  return new Date(epochSeconds * 1000).toLocaleDateString();
}

function formatDateTime(epochSeconds: number | null): string {
  if (epochSeconds === null || epochSeconds === undefined) {
    return '—';
  }
  return new Date(epochSeconds * 1000).toLocaleString();
}

/**
 * "v3 in effect for every new review · saved by admin · 8/2/2026" (or, for
 * an explicitly-cleared version, "v5 cleared · …" — issue #484's Notes:
 * "Saving empty text is allowed and reads back as 'cleared (v5)'."). This
 * is the ONLY state banner this pane renders — no permanent liveness
 * caveat (see this module's docstring for why that would misdescribe a
 * live feature).
 */
function statusLine(current: InstructionsVersion | null): string {
  if (current === null) {
    return 'No standing instructions — the playbook speaks for itself.';
  }
  const who = current.saved_by && current.saved_by.trim() !== '' ? current.saved_by : 'someone';
  const when = formatDate(current.saved_at);
  const headline =
    current.text.trim() === ''
      ? `v${current.version} cleared`
      : `v${current.version} in effect for every new review`;
  return `${headline} · saved by ${who} · ${when}`;
}

export interface AdminInstructionsProps {
  /**
   * The playbook this pane edits. `AdminPlaybooks` only mounts this
   * component once a playbook is selected (see this module's docstring),
   * and keys the mount on this same id — never blank, and never changes
   * without a full remount.
   */
  playbookId: string;
  /**
   * Only for this pane's own heading, e.g. "Standing instructions — EIAA".
   * Never used to pick which playbook's instructions load — `playbookId`
   * alone decides that.
   */
  playbookDisplayName: string;
}

export default function AdminInstructions({
  playbookId,
  playbookDisplayName,
}: AdminInstructionsProps): React.ReactElement | null {
  const [current, setCurrent] = useState<InstructionsVersion | null>(null);
  const [history, setHistory] = useState<InstructionsVersion[] | null>(null);
  const [instructionsError, setInstructionsError] = useState<string | null>(null);

  const [draftText, setDraftText] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<InstructionsVersion | null>(null);

  const [expandedVersions, setExpandedVersions] = useState<Set<number>>(new Set());
  const [restoringVersion, setRestoringVersion] = useState<number | null>(null);

  // A 403 from any route below hides this pane only — see this module's
  // docstring's "Privilege" section.
  const [isForbidden, setIsForbidden] = useState(false);

  const loadInstructions = useCallback(async () => {
    setInstructionsError(null);
    setCurrent(null);
    setHistory(null);
    setConflict(null);
    // A failed save must not leave its red banner sitting above a reload
    // that just succeeded.
    setSaveError(null);
    try {
      const response = await jsonFetch(
        `/api/admin/playbooks/${encodeURIComponent(playbookId)}/instructions`,
      );
      if (response.status === 403) {
        setIsForbidden(true);
        return undefined;
      }
      if (!response.ok) {
        throw new Error(
          friendlyErrorMessage(
            `GET playbook instructions returned HTTP ${response.status}`,
            "We couldn't load the standing instructions for this playbook. Please try again.",
          ),
        );
      }
      const data = (await response.json()) as InstructionsGetResponse;
      setCurrent(data.current);
      setHistory(data.history);
      setDraftText(data.current?.text ?? '');
      return data;
    } catch (err) {
      setInstructionsError(
        err instanceof Error
          ? err.message
          : friendlyErrorMessage(
              err,
              "We couldn't load the standing instructions for this playbook. Please try again.",
            ),
      );
      return undefined;
    }
  }, [playbookId]);

  useEffect(() => {
    void loadInstructions();
  }, [loadInstructions]);

  const saveText = useCallback(
    async (text: string) => {
      setSaveError(null);
      setSaving(true);
      try {
        const response = await jsonFetch(
          `/api/admin/playbooks/${encodeURIComponent(playbookId)}/instructions`,
          {
            method: 'POST',
            body: JSON.stringify({
              text,
              expected_current_version: current?.version ?? 0,
            }),
          },
        );
        if (response.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (response.status === 409) {
          // Never silently overwrite (issue #484's Notes): re-fetch so
          // `current`/`history` reflect the version that just won, and
          // leave the admin's own unsaved draft exactly as typed so it can
          // be reviewed and re-applied deliberately.
          const refreshed = await loadInstructions();
          setDraftText(text);
          setConflict(refreshed?.current ?? null);
          return;
        }
        if (!response.ok) {
          let detail: string | undefined;
          if (response.status === 400) {
            const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
            detail = typeof body.detail === 'string' ? body.detail : undefined;
          }
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `POST playbook instructions returned HTTP ${response.status}`,
                "We couldn't save the standing instructions. Please try again.",
              ),
          );
        }
        // Consumed only to drain the response body; `current`/`history`/
        // `draftText` are all set from the fresh GET below rather than
        // reconstructed from this response, so the two can never disagree.
        await response.json();
        setConflict(null);
        // The history list is append-only and this save just added to it —
        // refresh rather than reconstruct it client-side.
        await loadInstructions();
      } catch (err) {
        setSaveError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't save the standing instructions. Please try again."),
        );
      } finally {
        setSaving(false);
      }
    },
    [current, loadInstructions, playbookId],
  );

  const handleSave = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      void saveText(draftText);
    },
    [draftText, saveText],
  );

  const toggleHistoryVersion = useCallback((version: number) => {
    setExpandedVersions((existing) => {
      const next = new Set(existing);
      if (next.has(version)) {
        next.delete(version);
      } else {
        next.add(version);
      }
      return next;
    });
  }, []);

  const restoreVersion = useCallback(
    async (row: InstructionsVersion) => {
      setRestoringVersion(row.version);
      await saveText(row.text);
      setRestoringVersion(null);
    },
    [saveText],
  );

  if (isForbidden) {
    return null;
  }

  return (
    <div data-testid="admin-instructions-panel" className="ct-stack">
      <CtToolbar title={`Standing instructions — ${playbookDisplayName}`} />

      {instructionsError && (
        <CtBanner variant="danger" data-testid="admin-instructions-error">
          {instructionsError}
        </CtBanner>
      )}

      {history === null && !instructionsError ? (
        <CtProgress
          data-testid="admin-instructions-loading"
          label="Loading standing instructions…"
        />
      ) : (
        history !== null && (
          <>
            <CtCard data-testid="admin-instructions-form-card">
              <div className="ct-stack">
                {/* The ONLY state banner (issue #484's spec) — no
                    permanent liveness caveat, this feature is live. */}
                <p data-testid="admin-instructions-status" className="ct-muted">
                  {statusLine(current)}
                </p>

                {conflict && (
                  <CtBanner variant="warn" data-testid="admin-instructions-conflict">
                    <div className="ct-stack">
                      <p>
                        Someone saved v{conflict.version} while you were editing — review their
                        version below, then re-apply your edit.
                      </p>
                      <div className="ct-row" data-testid="admin-instructions-conflict-diff">
                        <div data-testid="admin-instructions-conflict-mine">
                          <strong>Your edit (unsaved)</strong>
                          <p style={{ whiteSpace: 'pre-wrap' }}>
                            {draftText === '' ? '(empty)' : draftText}
                          </p>
                        </div>
                        <div data-testid="admin-instructions-conflict-theirs">
                          <strong>
                            v{conflict.version} · {conflict.saved_by ?? 'someone'} ·{' '}
                            {formatDateTime(conflict.saved_at)}
                          </strong>
                          <p style={{ whiteSpace: 'pre-wrap' }}>
                            {conflict.text === '' ? '(cleared)' : conflict.text}
                          </p>
                        </div>
                      </div>
                    </div>
                  </CtBanner>
                )}

                {saveError && (
                  <CtBanner variant="danger" data-testid="admin-instructions-save-error">
                    {saveError}
                  </CtBanner>
                )}

                <form className="ct-stack" noValidate onSubmit={handleSave}>
                  <CtField
                    label="Standing instructions for this contract type (optional)"
                    hint={`These apply to every review run with this playbook. They ${GUIDANCE_PRECEDENCE_COPY} The instructions box on the Review screen still wins for a single review. Leave blank to let the playbook speak for itself.`}
                  >
                    <textarea
                      data-testid="admin-instructions-text"
                      rows={8}
                      value={draftText}
                      onChange={(e) => setDraftText(e.target.value)}
                    />
                  </CtField>

                  <div className="ct-row">
                    <CtButton
                      type="submit"
                      variant="primary"
                      data-testid="admin-instructions-save"
                      disabled={saving}
                      loading={saving}
                    >
                      {saving ? 'Saving…' : 'Save — takes effect for the next review'}
                    </CtButton>
                  </div>
                </form>
              </div>
            </CtCard>

            <CtCard data-testid="admin-instructions-history-card">
              <CtToolbar title="History" />
              {history.length === 0 ? (
                <p className="ct-muted" data-testid="admin-instructions-history-empty">
                  Nothing has been saved for this playbook yet.
                </p>
              ) : (
                <div className="ct-stack">
                  {history.map((row) => {
                    const isExpanded = expandedVersions.has(row.version);
                    return (
                      <div key={row.version} data-testid={`admin-instructions-history-row-${row.version}`}>
                        <div className="ct-row">
                          <span>
                            v{row.version} · {row.saved_by ?? 'someone'} ·{' '}
                            {formatDateTime(row.saved_at)}
                            {row.text.trim() === '' ? ' · cleared' : ''}
                          </span>
                          <CtButton
                            type="button"
                            variant="ghost"
                            size="sm"
                            data-testid={`admin-instructions-history-toggle-${row.version}`}
                            onClick={() => toggleHistoryVersion(row.version)}
                          >
                            {isExpanded ? 'Hide text' : 'Show text'}
                          </CtButton>
                          <CtButton
                            type="button"
                            variant="secondary"
                            size="sm"
                            data-testid={`admin-instructions-history-restore-${row.version}`}
                            disabled={restoringVersion !== null}
                            loading={restoringVersion === row.version}
                            onClick={() => void restoreVersion(row)}
                          >
                            Restore as new version
                          </CtButton>
                        </div>
                        {isExpanded && (
                          <p
                            style={{ whiteSpace: 'pre-wrap' }}
                            data-testid={`admin-instructions-history-text-${row.version}`}
                          >
                            {row.text === '' ? '(cleared — empty text)' : row.text}
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </CtCard>
          </>
        )
      )}
    </div>
  );
}
