import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type KeyboardEvent,
  type RefObject,
} from "react";
import { MaterialArt, SmallArt } from "./MaterialArt";
import { useArtworkGate } from "./artworkGate";
import { consoleSize, useConsoleWidth } from "./consoleWidth";
import { useFittedFilename } from "./filename";
import { platePath } from "./plates";
import { createMotion, motionStyles, type MotionController } from "./motion";
import {
  canDispose,
  canSubmit,
  costText,
  inferMotion,
  isManual,
  isWorking,
  money,
  stageInfo,
  statusText,
} from "./state";
import type {
  Action,
  Disposition,
  MotionEvent,
  OrbitDinerProps,
  ReviewModel,
  Message,
} from "./types";
import "./orbit.css";
/**
 * Issue #733. The kit collapsed seven separate banners into one message
 * channel, but the CHANNELS did not merge — each `projectMessages` id is still
 * one app concern with one test contract, so each keeps the id its banner
 * carried. Keyed by message id (which `projection.ts` assigns exactly once per
 * render) rather than by scope, so no two rendered elements can collide: the
 * cover note's two failure modes are one scope and two ids, and they must stay
 * distinguishable — a retryable hiccup is not a refusal.
 *
 * A message with no entry here renders without a test id. That is deliberate:
 * these are the ids the suite already means something by, not a hook for every
 * string the console can say.
 */
const MESSAGE_TEST_IDS: Record<string, string | undefined> = {
  "submit-error": "review-submit-error",
  "poll-error": "review-poll-error",
  "catalog-error": "review-catalog-error",
  "preference-error": "review-notes-mode-save-error",
  "cancel-error": "review-cancel-error",
  "download-error": "review-download-error",
  "disposition-error": "review-disposition-error",
  "cover-error": "review-cover-note-error",
  "cover-failed": "review-cover-note-real-error",
};
/** The retry key each of those messages offers, same reasoning. */
const MESSAGE_ACTION_TEST_IDS: Record<string, string | undefined> = {
  // Not `submit-error`: `review-retry-button` is the burnt review's "Toast
  // another slice", and a failed SUBMIT offers "Try again" — two controls,
  // two meanings, and only one may answer to the id.
  "preference-error": "review-notes-mode-retry",
  // Not `cover-error`: the cover panel renders its own "Retry cover note" key
  // from `cover.retryable`, and two elements may not answer to one id.
};
/**
 * Issue #738 (N4). The ONLY controls the plain/illustrated switch can pull out
 * from under a reviewer: the operations control, which is one lever in the
 * scene and two keys in the plain layout, and the two fields whose skin the
 * switch redraws around a live caret or an open picker. Everything else keeps
 * its element across the switch, so nothing else can be replaced underneath
 * anyone. Focus on one of these defers the switch until focus leaves; the
 * layout it defers in is the fully operable one, so waiting costs nothing.
 */
const SWITCH_SENSITIVE_FOCUS = "textarea,select,.od-lever,.od-plain-operations";
const dispositionNames: Record<Disposition, string> = {
  ACCEPTED: "Accepted",
  EDITED: "Accepted with edits",
  REJECTED: "Rejected",
};
function Panel({
  title,
  children,
  className = "",
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`od-paper-panel ${className}`}>
      <span className="od-paper-clip" aria-hidden="true" />
      <h2>{title}</h2>
      {children}
    </section>
  );
}
function Key({
  children,
  onClick,
  disabled = false,
  selected = false,
  testId,
  part,
  title,
  ariaLabel,
  buttonRef,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  selected?: boolean;
  testId?: string;
  part?: string;
  title?: string;
  ariaLabel?: string;
  /** For a key whose own render condition unmounts it while the overlay it
      opened is up: the console needs a handle to give focus back to. */
  buttonRef?: RefObject<HTMLButtonElement>;
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      className="od-key"
      disabled={disabled}
      aria-pressed={selected || undefined}
      onClick={onClick}
      data-testid={testId}
      data-part={part}
      title={title}
      aria-label={ariaLabel}
    >
      {children}
    </button>
  );
}
export function OrbitDiner({
  model: m,
  assetBase,
  onFile,
  onPreferences,
  onAction,
  onSound,
  plain = false,
  onPlainChange,
  // Issue #720 (owner decision H6, defence in depth). The supplier's default
  // is `true`; ours is `false`, because in THIS app the host panel owns the
  // shortcut vocabulary and a forgotten prop would double-dispatch every
  // chord. A host that wants the kit's own handler asks for it by name.
  // `vendor/orbit-diner/` keeps the supplier's default — it is the source of
  // record, not ours to edit.
  keyboardShortcuts = false,
}: OrbitDinerProps) {
  const root = useRef<HTMLDivElement>(null),
    file = useRef<HTMLInputElement>(null),
    motion = useRef<MotionController>(),
    previous = useRef<ReviewModel>(),
    lastEvent = useRef<string>();
  const instructions = useRef<HTMLTextAreaElement>(null),
    dialog = useRef<HTMLDialogElement>(null),
    dialogOpener = useRef<HTMLElement | null>(null),
    // Issue #734. The result card's "Review details" key is the one overlay
    // opener that unmounts itself by opening its overlay, so `close()` cannot
    // focus the node it captured. This handle survives the round trip.
    reviewDetails = useRef<HTMLButtonElement>(null),
    restoreReviewDetails = useRef(false);
  const [modal, setModal] = useState<
      | "receipt"
      | "playbooks"
      | "shortcuts"
      | "record"
      | "cover"
      | "disposition"
      | null
    >(null),
    [search, setSearch] = useState(""),
    [fileError, setFileError] = useState(""),
    [dragOver, setDragOver] = useState(false),
    [plainOverride, setPlainOverride] = useState<boolean | null>(null),
    // Issue #739. A callback ref, not a `useRef`: the inscription exists only
    // once a document is chosen, so an effect keyed on a ref object would run
    // (and find nothing) when the CONSOLE mounts and never again.
    [nameBox, setNameBox] = useState<HTMLElement | null>(null),
    [forcedColours, setForcedColours] = useState(false);
  // The one layout observer (issue #725). `orbit.css` reads the same width
  // through `@container od-console (…)`; this reading exists for the two
  // things a container query cannot express — a declaration whose subject is
  // the container itself (the phone corner radius) and the toaster plate
  // variant (#738), which is a fetch, not a style. Nothing here reads
  // `window.innerWidth`: inside ct-app-shell the viewport is always wider
  // than the console.
  const consoleWidth = useConsoleWidth(root);
  const consoleStep = consoleSize(consoleWidth);
  // Issue #738 (N4/N5). The plates every illustrated control is positioned
  // against, for the ONE toaster variant that width selects. Until they are
  // decoded there is no scene to show, and the console renders the plain
  // layout — which is not a placeholder: a document can be chosen, configured
  // and submitted in it.
  const artwork = useArtworkGate(consoleWidth, assetBase);
  const [pointerHeld, setPointerHeld] = useState(false),
    [focusHeld, setFocusHeld] = useState(false),
    [artworkShown, setArtworkShown] = useState(false);
  // Never swap the layout mid-gesture, under a focused control, or with an
  // overlay up (N4). The switch waits for all three to clear.
  const deferSwitch = pointerHeld || focusHeld || modal !== null;
  useEffect(() => {
    if (deferSwitch) return;
    setArtworkShown(artwork.status === "ready");
  }, [deferSwitch, artwork.status]);
  // A pointer that went down inside the console may come up anywhere,
  // including outside it (a drag off the lever), so the release is watched on
  // the window and only while a gesture is actually open.
  useEffect(() => {
    if (!pointerHeld) return undefined;
    const release = () => setPointerHeld(false);
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    return () => {
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
    };
  }, [pointerHeld]);
  const holdsSwitch = (node: EventTarget | null): boolean => {
    const el = node instanceof Element ? node : null;
    if (!el || !root.current?.contains(el)) return false;
    return !!el.closest(SWITCH_SENSITIVE_FOCUS);
  };
  // Forced colours and the reviewer's own choice keep winning exactly as they
  // did (#724); `artworkShown` only adds the case where the console has no
  // illustration to offer in the first place.
  const plainView = forcedColours || !artworkShown || (plainOverride ?? plain);
  useEffect(() => setPlainOverride(null), [plain]);
  useEffect(() => {
    const media = window.matchMedia("(forced-colors: active)");
    const sync = () => setForcedColours(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  const uid = useId().replace(/:/g, ""),
    p = m.preferences,
    working = isWorking(m),
    manual = isManual(m),
    stage = m.status === "SUBMITTING" ? null : stageInfo(m.stage);
  const terminal = [
    "DONE",
    "ERROR",
    "MANUAL_REVIEW_REQUIRED",
    "ERROR_MANUAL_REVIEW_REQUIRED",
    "CANCELLED",
  ].includes(m.status);
  const canButter = m.result?.decision === "REQUEST_CHANGE" && !!m.hasOutput;
  const mainMessages = (m.messages ?? []).filter((message) => {
    const local =
      modal === "receipt" || modal === "record"
        ? ["receipt", "download", "support"]
        : modal === "cover"
          ? ["cover"]
          : modal === "disposition"
            ? ["disposition"]
            : [];
    if (local.includes(message.scope)) return false;
    return !(
      message.tone === "success" &&
      ["receipt", "support", "disposition", "cover"].includes(message.scope)
    );
  });
  const registerMessage = mainMessages.find((message) =>
    ["submit", "catalog", "preference", "cancel", "poll"].includes(
      message.scope,
    ),
  );
  const registerLine =
    registerMessage?.title ??
    (terminal
      ? m.cost.kind === "settled" && !m.cost.settlementComplete
        ? "Settlement pending"
        : ""
      : m.cost.kind === "held"
        ? "Reservation confirmed"
        : m.status === "SUBMITTING"
          ? "Awaiting acceptance"
          : "Review settings");
  // "Estimate unavailable" is no longer announced here: #735 made it the
  // register's permanent LABEL rather than a transient status, and repeating
  // it on the live line would have the glass read it twice.
  // A poll hiccup announces as a STATUS, never as an alert (#726): the poller
  // retries on its own backoff, the review's own state is untouched, and
  // interrupting a screen reader for something that heals itself on the next
  // tick is the behaviour the ticket forbids. Every other channel keeps the
  // kit's rule — an error, or anything offering a key, is worth interrupting
  // for.
  const messageRole = (message: Message): "alert" | "status" =>
    message.scope === "poll"
      ? "status"
      : message.tone === "error" || message.action
        ? "alert"
        : "status";
  const renderMessages = (messages: readonly Message[]) =>
    messages.map((message) => (
      <div
        key={message.id}
        role={messageRole(message)}
        data-testid={MESSAGE_TEST_IDS[message.id]}
      >
        <strong>{message.title}</strong>
        {message.detail && <p>{message.detail}</p>}
        {message.action && (
          <Key
            testId={MESSAGE_ACTION_TEST_IDS[message.id]}
            onClick={() => action({ type: message.action } as Action)}
          >
            {message.actionLabel ?? "Retry"}
          </Key>
        )}
      </div>
    ));
  const active = m.playbooks.filter((x) => x.status === "active"),
    selected = m.playbooks.find((x) => x.playbook_id === p.playbookId),
    selectedIndex = active.findIndex((x) => x.playbook_id === p.playbookId);
  const angle =
    active.length > 1
      ? -60 + (Math.max(0, selectedIndex) * 120) / (active.length - 1)
      : 0;
  const fire = (event: MotionEvent, target?: Element) => {
    motion.current?.play(event, target);
    if (!m.muted && !document.hidden) onSound?.(event);
  };
  const action = (a: Action) => onAction(a);
  useEffect(() => {
    if (!root.current) return;
    motion.current = createMotion(root.current);
    return () => motion.current?.dispose();
  }, []);
  useEffect(() => {
    for (const event of inferMotion(previous.current, m)) fire(event);
    previous.current = m;
  }, [m]);
  useEffect(() => {
    if (m.motionEvent && lastEvent.current !== m.motionEvent.id) {
      lastEvent.current = m.motionEvent.id;
      fire(m.motionEvent.type);
    }
  }, [m.motionEvent?.id]);
  const sizeInstructions = () => {
    const el = instructions.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.max(168, el.scrollHeight + 2) + "px";
    }
  };
  useLayoutEffect(sizeInstructions, [p.instructions]);
  useEffect(() => {
    let disposed = false;
    let width = 0;
    let frame = 0;
    const el = instructions.current;
    const observer =
      typeof ResizeObserver === "function"
        ? new ResizeObserver((entries) => {
            const next = entries[0]?.contentRect.width;
            if (next && next !== width) {
              width = next;
              cancelAnimationFrame(frame);
              frame = requestAnimationFrame(sizeInstructions);
            }
          })
        : null;
    if (el) observer?.observe(el);
    void document.fonts?.ready.then(() => {
      if (!disposed) sizeInstructions();
    });
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, []);
  useEffect(() => {
    const el = dialog.current;
    if (!el) return;
    if (modal) {
      if (!el.open) el.showModal();
      if (modal === "receipt") motion.current?.play("receipt-open");
    } else if (el.open) el.close();
  }, [modal]);
  const open = (kind: NonNullable<typeof modal>, target?: HTMLElement) => {
    dialogOpener.current = target ?? (document.activeElement as HTMLElement);
    setModal(kind);
  };
  const close = () => {
    if (dialog.current?.open) dialog.current.close();
    setModal(null);
    setSearch("");
    // Issue #734. The result card yields `resultPanel` to whichever overlay is
    // open, so the "Review details" key that opened the record is detached by
    // now and focusing it would drop focus to <body>. Hand that case to the
    // effect below, which runs once the key is back on the page.
    const opener = dialogOpener.current;
    if (opener?.isConnected) opener.focus();
    else restoreReviewDetails.current = true;
  };
  useEffect(() => {
    if (modal || !restoreReviewDetails.current) return;
    restoreReviewDetails.current = false;
    reviewDetails.current?.focus();
  }, [modal]);
  /**
   * Issue #740. Hand the page to the browser's print dialog, which the print
   * stylesheet narrows to `.od-print-receipt` alone.
   *
   * Deliberately NOT an `Action`. Every entry in that union is a hand-off to a
   * guarded `ReviewSubmission` handler — a request, a download, a clipboard
   * write, a state change the host owns. This owns none of those: it is the
   * same class of thing as `open()` and `close()`, a view affordance the
   * console performs on itself, and routing it through the host would add a
   * callback whose only body is `window.print()` and a fourth place for the
   * receipt's wiring to drift.
   *
   * Guarded because a browser (or a test harness) may not implement it, and a
   * key that throws is worse than a key that quietly does nothing.
   */
  const printReceipt = () => {
    if (typeof window.print === "function") window.print();
  };
  const selectFile = (next: File | null) => {
    if (next && !/\.docx$/i.test(next.name)) {
      setFileError("Choose a Word .docx document.");
      return;
    }
    setFileError("");
    onFile(next);
    if (file.current) file.current.value = "";
  };
  const changeBook = (value: string) => {
    if (active.some((x) => x.playbook_id === value)) {
      onPreferences({ playbookId: value });
      fire("key");
    }
  };
  const stepBook = (delta: number) => {
    if (active.length > 1)
      changeBook(
        active[
          (Math.max(0, selectedIndex) + delta + active.length) % active.length
        ].playbook_id,
      );
  };
  const dialKeys = (e: KeyboardEvent) => {
    if (
      [
        "ArrowLeft",
        "ArrowUp",
        "ArrowRight",
        "ArrowDown",
        "Home",
        "End",
      ].includes(e.key)
    ) {
      e.preventDefault();
      if (e.key === "Home" && active[0]) changeBook(active[0].playbook_id);
      else if (e.key === "End" && active.length)
        changeBook(active[active.length - 1].playbook_id);
      else stepBook(["ArrowLeft", "ArrowUp"].includes(e.key) ? -1 : 1);
    }
  };
  const dialDrag = useRef<{
      angle: number;
      sum: number;
      origin: number;
      dragged: boolean;
    } | null>(null),
    skipDialClick = useRef(false);
  const pointerAngle = (e: {
    clientX: number;
    clientY: number;
    currentTarget: HTMLElement;
  }) => {
    const box = e.currentTarget.getBoundingClientRect();
    return (
      (Math.atan2(
        e.clientY - box.top - box.height / 2,
        e.clientX - box.left - box.width / 2,
      ) *
        180) /
      Math.PI
    );
  };
  const drag = useRef<{ start: number; last: number; dragged: boolean } | null>(
      null,
    ),
    suppressClick = useRef(false);
  const leverAction = () => {
    if (working) {
      if (m.status !== "SUBMITTING" && !m.cancelRequested)
        action({ type: "cancel" });
    } else if (canSubmit(m)) action({ type: "submit" });
    else fire("refusal");
  };
  // Issue #739. The painted inscription, measured to two lines at most in the
  // bread's central face. The whole name stays on the button's accessible name
  // and title below; this is the only place it is ever shortened.
  // The revision carries both things that change the inscription's box: the
  // console width the one observer publishes, and the plain toggle, which
  // takes the slice out of layout entirely (`.od-plain .od-slice` is
  // `display: none`). A name chosen in plain view is measured against a box
  // of zero width, so the switch back to illustrated is the moment it first
  // becomes measurable. One string, not an array: an array literal would be a
  // new value every render and re-fit on every one of them.
  const visibleFilename = useFittedFilename(
    nameBox,
    m.filename ?? "",
    `${consoleWidth}/${plainView}`,
  );
  const renderedStage =
    m.status === "ERROR"
      ? "brightness(.23) saturate(.25)"
      : working
        ? (stage?.filter ?? "brightness(1)")
        : "brightness(.97)";
  const recommended =
    m.preflight?.classification === "ok" &&
    active.some((x) => x.playbook_id === m.preflight?.recommendedPlaybookId) &&
    m.preflight.recommendedPlaybookId !== p.playbookId;
  const guideLabel =
    working || terminal
      ? "Special instructions · next review"
      : "Special instructions";
  // Owner decision H2 (#735). The label names WHICH figure the glass carries,
  // and a terminal status is deliberately not one of the cases: with no
  // settlement projection in this deployment, `terminal ? "Final review cost"`
  // labelled every finished review with a figure it does not have — and
  // `costText` then had nothing to put under it but an em dash. A finished
  // review keeps the estimate captured for it, honestly labelled as an
  // estimate; "Final review cost" waits for a settlement that really completed.
  const costLabel =
    m.cost.kind === "unavailable"
      ? "Estimate unavailable"
      : m.cost.kind === "held"
        ? "Review reservation"
        : m.cost.kind === "settled"
          ? m.cost.settlementComplete && m.cost.cents != null
            ? "Final review cost"
            : "Review settlement"
          : "Estimated review cost";
  const keyHandler = (e: KeyboardEvent<HTMLDivElement>) => {
    if (modal) {
      e.stopPropagation();
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
      return;
    }
    if (!keyboardShortcuts || e.defaultPrevented) return;
    const target = e.target as HTMLElement;
    const typing = target.matches(
      'input,textarea,select,[contenteditable="true"]',
    );
    const modifier = e.ctrlKey || e.metaKey;
    if (
      modifier &&
      e.shiftKey &&
      ["p", "g", "m", "r"].includes(e.key.toLowerCase())
    ) {
      e.preventDefault();
      e.stopPropagation();
      const key = e.key.toLowerCase();
      if (key === "p") open("playbooks");
      if (key === "g") instructions.current?.focus();
      if (key === "m") action({ type: "sound-toggle" });
      if (key === "r" && recommended) action({ type: "switch-recommended" });
      return;
    }
    if (modifier && e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      if (canSubmit(m)) action({ type: "submit" });
      return;
    }
    if (modifier && ["u", "o"].includes(e.key.toLowerCase())) {
      if (!working) {
        e.preventDefault();
        e.stopPropagation();
        file.current?.click();
      }
      return;
    }
    if (modifier && ["d", "s"].includes(e.key.toLowerCase())) {
      if (m.hasOutput && !m.downloading) {
        e.preventDefault();
        e.stopPropagation();
        action({ type: "download" });
      }
      return;
    }
    if (modifier && e.key === "/") {
      e.preventDefault();
      e.stopPropagation();
      open("shortcuts");
      return;
    }
    if (modal || typing || modifier || e.altKey) return;
    if (
      [
        "?",
        "Escape",
        "g",
        "u",
        "p",
        "m",
        "r",
        "[",
        "-",
        "]",
        "=",
        "+",
        "1",
        "2",
        "3",
        "4",
      ].includes(e.key.toLowerCase()) ||
      e.key === "Escape"
    )
      e.stopPropagation();
    if (e.key === "?") {
      open("shortcuts");
      return;
    }
    if (e.key === "Escape" && !working) {
      selectFile(null);
      return;
    }
    if (e.key.toLowerCase() === "g") instructions.current?.focus();
    if (e.key.toLowerCase() === "u" && !working) file.current?.click();
    if (e.key.toLowerCase() === "p")
      root.current
        ?.querySelector<HTMLSelectElement>(
          '[data-testid="review-playbook-dial"]',
        )
        ?.focus();
    if (e.key.toLowerCase() === "m") action({ type: "sound-toggle" });
    if (e.key.toLowerCase() === "r" && recommended)
      action({ type: "switch-recommended" });
    const levels = ["light", "medium", "dark"] as const;
    const direction = ["[", "-"].includes(e.key)
      ? -1
      : ["]", "=", "+"].includes(e.key)
        ? 1
        : 0;
    if (direction)
      onPreferences({
        intensity:
          levels[
            Math.max(0, Math.min(2, levels.indexOf(p.intensity) + direction))
          ],
      });
    const notes = ["none", "external", "internal", "both"] as const;
    const n = Number(e.key) - 1;
    if (n >= 0 && n < 4 && (n < 2 || m.internalNotesAvailable))
      onPreferences({ notesMode: notes[n] });
  };
  const resultPanel = m.result ? (
    <div data-testid="review-result">
      {" "}
      <Panel
        title={
          manual
            ? "Next step"
            : m.status === "ERROR"
              ? "What happened"
              : "Review result"
        }
        className={m.status === "ERROR" ? "od-failure" : ""}
      >
        <p className="od-outcome" data-testid="review-outcome">
          {m.result.outcome}
        </p>
        {m.result.copy && <p>{m.result.copy}</p>}
        {/* The failure IS the announcement on the error path (issue #510):
            one alert, carrying the cause-and-fix prose written for this reason
            code, instead of a second polite region competing with it. */}
        {m.result.failureCause && (
          <div role="alert" data-testid="review-failure">
            <p data-testid="review-failure-headline">
              <strong>{m.result.failureCause}</strong>
            </p>
            {m.result.failureFix && <p>{m.result.failureFix}</p>}
            {m.result.normalizationNotes && (
              <p data-testid="review-failure-normalization-notes">
                {m.result.normalizationNotes}
              </p>
            )}
            <p className="od-fine">
              {m.result.failingStage && (
                <code data-testid="review-failing-stage">
                  {m.result.failingStage}
                </code>
              )}
              {m.result.reason && (
                <>
                  {m.result.failingStage ? " · " : ""}
                  <code data-testid="review-failure-reason">
                    {m.result.reason}
                  </code>
                </>
              )}
            </p>
          </div>
        )}
        {/* Issue #734 (owner decision H1). The toaster's one free key is the
            retry on ERROR, so on a failed review the review id, Copy ID and
            Save original had no call site at all — and the retry is the very
            thing that clears the review they belong to. They get a call site
            here instead, beside the cause and fix the reader is already
            looking at, on every status this card renders for: ERROR and both
            manual handoffs.
            OUTSIDE the `role="alert"` above on purpose — the alert speaks the
            diagnosis, and a control is not part of what a screen reader should
            be interrupted with.
            Rendered only when this panel is the PAGE's copy (issue #733): the
            record and receipt overlays render this same fragment, and a key
            that opens the overlay you are already reading is a dead control. */}
        {m.reviewId && modal !== "record" && modal !== "receipt" && (
          <div className="od-result-actions">
            <Key
              testId="review-details-button"
              buttonRef={reviewDetails}
              onClick={() => open("record")}
            >
              Review details
            </Key>
          </div>
        )}
        <dl className="od-result-facts">
          {m.result.issueCount != null && (
            <>
              <dt>Changes requested</dt>
              <dd>{m.result.issueCount}</dd>
            </>
          )}
          {m.result.confidenceBand && (
            <div className="od-fact-group" data-testid="review-confidence-band">
              <dt>Confidence band</dt>
              <dd>{m.result.confidenceBand}</dd>
            </div>
          )}
        </dl>
        {manual && m.result.reason && !m.result.failureCause && (
          <p className="od-fine">
            <code data-testid="review-failing-stage">
              {m.result.failingStage}
            </code>{" "}
            ·{" "}
            <code data-testid="review-failure-reason">{m.result.reason}</code>
          </p>
        )}
        {/* Issue #733. The critic's delta is ONE region — the counts and the
            objections it raised — because that is the unit the pre-download
            trust gate is about (docs/output-contract.md). Splitting the
            numbers into the facts grid and the objections below it left the
            indicator naming only half of itself. */}
        {/* Truthiness, not presence: a delta of zero contested and zero added
            is a review the critic had nothing to say about, and announcing an
            empty indicator would train the reader to ignore a real one. */}
        {(m.result.criticContested ||
          m.result.criticAdded ||
          m.result.criticDetails) && (
          <div data-testid="review-critic-delta">
            <dl className="od-result-facts">
              {m.result.criticContested != null && (
                <>
                  <dt>Contested by the critic</dt>
                  <dd>{m.result.criticContested}</dd>
                </>
              )}
              {m.result.criticAdded != null && (
                <>
                  <dt>Added by the critic</dt>
                  <dd data-testid="critic-added-issues">
                    {m.result.criticAdded}
                  </dd>
                </>
              )}
            </dl>
            {m.result.criticDetails?.map((text, i) => (
              <p key={i}>{text}</p>
            ))}
          </div>
        )}
        {!!m.result.meta?.length && (
          <p className="od-fine" data-testid="review-meta-line">
            {m.result.meta.map((part, i) => (
              <span key={part.id} data-testid={part.id}>
                {i > 0 ? " · " : ""}
                {part.text}
              </span>
            ))}
          </p>
        )}
        {m.result.metadata?.map(({ label, value }) => (
          <p className="od-fine" key={label}>
            {label}: {value}
          </p>
        ))}
      </Panel>
    </div>
  ) : null;
  const dispositionPanel = (
    <>
      {" "}
      {canDispose(m) && (
        <div className="od-disposition" data-testid="review-disposition">
          <h2>Record the outcome</h2>
          <p>This becomes part of the review’s record.</p>
          {m.disposition?.recorded ? (
            <p
              className="od-stamp"
              data-part="disposition-stamp"
              data-testid="review-disposition-recorded"
            >
              Recorded: {dispositionNames[m.disposition.recorded]}
            </p>
          ) : (
            <>
              <label>
                Optional disposition note
                <textarea
                  data-testid="review-disposition-note"
                  maxLength={4000}
                  rows={2}
                  value={p.dispositionNote}
                  onChange={(e) =>
                    onPreferences({ dispositionNote: e.target.value })
                  }
                />
              </label>
              <div className="od-disposition-keys">
                {(["ACCEPTED", "EDITED", "REJECTED"] as const).map(
                  (outcome) => (
                    <Key
                      key={outcome}
                      testId={`review-disposition-${outcome.toLowerCase()}`}
                      disabled={m.disposition?.saving}
                      onClick={() =>
                        action({
                          type: "disposition",
                          outcome,
                          note: p.dispositionNote,
                        })
                      }
                    >
                      {dispositionNames[outcome]}
                    </Key>
                  ),
                )}
              </div>
              {m.disposition?.saving && <p role="status">Recording…</p>}
            </>
          )}
        </div>
      )}
    </>
  );
  const coverPanel = (
    <>
      {" "}
      {m.cover && m.cover.state !== "idle" && (
        <Panel title="Cover note" className="od-cover-note">
            {m.cover.state === "loading" ? (
              <p role="status">Preparing the cover note…</p>
            ) : m.cover.state === "error" ? (
              <>
                <p role="alert">
                  {m.cover.error ?? "The cover note could not be prepared."}
                </p>
                {m.cover.retryable && (
                  <Key
                    testId="review-cover-note-retry"
                    onClick={() => action({ type: "cover-retry" })}
                  >
                    Retry cover note
                  </Key>
                )}
              </>
            ) : (
              // The CARD is the draft and what you can do with it. A failure
              // is not a card with bad news in it — issue #733 keeps the id on
              // the thing whose absence the failure tests assert.
              <div data-testid="review-cover-note-card">
                <p
                  className="od-cover-draft"
                  data-testid="review-cover-note-text"
                >
                  {m.cover.draft}
                </p>
                <div className="od-receipt-actions">
                  <Key
                    testId="review-cover-note-copy"
                    onClick={() => action({ type: "cover-copy" })}
                  >
                    Copy
                  </Key>
                  <Key
                    testId="review-cover-note-regenerate"
                    onClick={() => action({ type: "cover-regenerate" })}
                  >
                    Regenerate
                  </Key>
                </div>
                <p className="od-fine" data-testid="review-cover-note-cost">
                  {m.cover.cached
                    ? "Served from the cached draft — no charge."
                    : "Regenerating makes a new billed request."}
                  {m.cover.lastCostCents != null &&
                    ` Last recorded cost: ${money(m.cover.lastCostCents)}.`}
                </p>
              </div>
            )}
        </Panel>
      )}
    </>
  );
  const reviewTag = (
    <>
      {" "}
      {m.reviewId && (
        <div className="od-review-tag" data-testid="review-id-row">
          {/* Issue #492/#733: the raw review id is for support correlation,
              not for reading off a screen — the control copies it, and nothing
              paints it. */}
          <span>This review’s reference</span>
          <button
            type="button"
            className="od-link-key"
            data-testid="review-copy-id-button"
            onClick={() => action({ type: "copy-id" })}
          >
            {m.messages?.some((x) => x.id === "review-id-copied")
              ? "Copied"
              : "Copy review ID"}
          </button>
          {m.hasInput && (
            <button
              type="button"
              className="od-link-key"
              onClick={() => action({ type: "download-input" })}
            >
              Save original
            </button>
          )}
        </div>
      )}
    </>
  );
  return (
    <div
      ref={root}
      className={`od-console ${plainView ? "od-plain" : ""}`}
      data-console-size={consoleStep}
      data-status={m.status.toLowerCase()}
      // Issue #733. The console REPLACES the panel, so it is the panel: the
      // root carries `review-submission`, the id every caller outside this
      // file uses to mean "the Review tab's own region" (app.css positions it,
      // `layout-audit` check 2 names it). The status-named badge the kit puts
      // here moves one level in, onto `.od-scene` — one element, one id.
      data-testid="review-submission"
      data-terminal={terminal || undefined}
      onKeyDown={keyHandler}
      // Issue #738 (N4). These three only gate WHEN the plain/illustrated
      // switch may happen; none of them changes what any control does.
      onPointerDown={() => setPointerHeld(true)}
      onFocus={(e) => setFocusHeld(holdsSwitch(e.target))}
      onBlur={(e) => setFocusHeld(holdsSwitch(e.relatedTarget))}
      onClickCapture={(e) => {
        const key = (e.target as Element).closest(
          ".od-key,.od-radio-key,.od-link-key,.od-browse",
        );
        if (key) motion.current?.play("key", key);
      }}
      style={
        {
          "--od-counter-image": `url("${platePath("counter", assetBase)}")`,
          "--od-pad-image": `url("${platePath("pad", assetBase)}")`,
        } as CSSProperties
      }
    >
      <style>{motionStyles}</style>
      {/* Issue #738 (N5). An essential plate did not arrive. The console is
          already in the plain layout, the review, the poll and every form
          value are exactly where they were, and the only thing lost is the
          picture — so this is a STATUS, not an alert: nothing here is worth
          interrupting a reviewer mid-sentence for, the same reasoning #726
          applied to a poll hiccup that heals itself. The retry is a key the
          reviewer presses; nothing retries on a timer. */}
      {artwork.status === "failed" && (
        <div
          className="od-artwork-notice"
          role="status"
          data-testid="review-artwork-notice"
        >
          <p>Illustration unavailable. All review controls are available.</p>
          <button
            type="button"
            className="od-link-key"
            data-testid="review-artwork-retry"
            onClick={artwork.retry}
          >
            Retry illustration
          </button>
        </div>
      )}
      {/* The status badge (issue #733): `toaster-state-empty|loaded|running|
          done|error|cancelled|manual_review_required`. It is status-NAMED
          rather than appearance-named, so the old `-sober` (burnt) assertions
          land on `-error` and the old `-done` keeps its name. */}
      <div
        className="od-scene"
        data-testid={`toaster-state-${m.status.toLowerCase()}`}
      >
        <div className="od-appliances">
          <section
            className="od-toaster"
            aria-label="Document and review operation"
          >
            <div
              className={`od-toaster-figure ${dragOver ? "od-drop-active" : ""}`}
              onDragOver={(e) => {
                if (!working) {
                  e.preventDefault();
                  setDragOver(true);
                }
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                if (!working) selectFile(e.dataTransfer.files[0] ?? null);
              }}
            >
              <div
                className="od-slot-glow"
                data-part="slot-glow"
                aria-hidden="true"
              />
              {m.filename && (
                <button
                  type="button"
                  className="od-slice"
                  data-part="slice"
                  style={{ "--od-doneness": renderedStage } as CSSProperties}
                  aria-label={
                    m.hasOutput
                      ? `Save redline for ${m.filename}`
                      : `Selected document: ${m.filename}`
                  }
                  title={
                    m.hasOutput ? `Save redline: ${m.filename}` : m.filename
                  }
                  disabled={m.hasOutput ? m.downloading : working}
                  onClick={() =>
                    m.hasOutput
                      ? action({ type: "download" })
                      : file.current?.click()
                  }
                >
                  {artworkShown && (
                    <MaterialArt
                      kind="toast"
                      base={assetBase}
                      className="od-toast-texture"
                    />
                  )}
                  <span className="od-toast-name" ref={setNameBox}>
                    {visibleFilename}
                  </span>
                  {m.cover?.state === "ready" && (
                    <span className="od-butter-on-toast">
                      <SmallArt kind="butter" />
                    </span>
                  )}
                </button>
              )}
              {m.status === "ERROR" && <SmallArt kind="steam" />}
              {/* Issue #738. One variant, chosen from the console's own
                  content width, and rendered ONLY once its plate has decoded:
                  an `<image href>` in the tree is a fetch, so painting this
                  before the gate answers is how both variants get loaded. */}
              {artworkShown && (
                <MaterialArt
                  kind="toaster"
                  base={assetBase}
                  part="shell"
                  toasterVariant={artwork.variant}
                />
              )}
              {!m.filename && (
                <button
                  className="od-slot-label"
                  type="button"
                  onClick={() => file.current?.click()}
                >
                  Choose or drop a .docx
                </button>
              )}
              {plainView && m.filename && (
                <p className="od-plain-file">{m.filename}</p>
              )}
              <div className="od-playbook-display">
                <span>PLAYBOOK TYPE</span>
                <label className="od-playbook-value">
                  <span aria-hidden="true">
                    {selected?.display_name ?? "Choose a playbook"}
                  </span>
                  <select
                    value={p.playbookId}
                    onChange={(e) => changeBook(e.target.value)}
                    data-testid="review-playbook-dial"
                    aria-label="Playbook type"
                  >
                    {!selected && <option value="">Choose a playbook</option>}
                    {m.playbooks.map((x) => (
                      <option
                        key={x.playbook_id}
                        value={x.playbook_id}
                        disabled={x.status !== "active"}
                      >
                        {x.display_name}
                        {x.status !== "active" ? " · coming soon" : ""}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <button
                type="button"
                className="od-dial"
                data-part="knob"
                aria-label={`Next playbook. Current: ${selected?.display_name ?? "none"}`}
                disabled={active.length < 2}
                onClick={() => {
                  if (skipDialClick.current) {
                    skipDialClick.current = false;
                    return;
                  }
                  stepBook(1);
                }}
                onPointerDown={(e) => {
                  if (active.length < 2 || e.button !== 0) return;
                  dialDrag.current = {
                    angle: pointerAngle(e),
                    sum: 0,
                    origin: Math.max(0, selectedIndex),
                    dragged: false,
                  };
                  e.currentTarget.setPointerCapture(e.pointerId);
                }}
                onPointerMove={(e) => {
                  const d = dialDrag.current;
                  if (!d) return;
                  const angle = pointerAngle(e);
                  let delta = angle - d.angle;
                  if (delta > 180) delta -= 360;
                  if (delta < -180) delta += 360;
                  d.sum += delta;
                  d.angle = angle;
                  d.dragged ||= Math.abs(d.sum) > 5;
                  if (d.dragged) {
                    const index = Math.max(
                      0,
                      Math.min(
                        active.length - 1,
                        Math.round(
                          d.origin + (d.sum / 120) * (active.length - 1),
                        ),
                      ),
                    );
                    if (index !== selectedIndex)
                      changeBook(active[index].playbook_id);
                  }
                }}
                onPointerUp={() => {
                  skipDialClick.current = !!dialDrag.current?.dragged;
                  dialDrag.current = null;
                }}
                onPointerCancel={() => {
                  dialDrag.current = null;
                  skipDialClick.current = false;
                }}
                onKeyDown={dialKeys}
              >
                {artworkShown && <MaterialArt kind="dial" base={assetBase} />}
                <span
                  className="od-dial-index"
                  style={{ transform: `rotate(${angle}deg)` }}
                />
              </button>
              {!plainView && (
                <button
                  type="button"
                  className="od-lever"
                  aria-label={working ? "Stop review" : "Start toaster"}
                  aria-disabled={
                    working
                      ? m.status === "SUBMITTING" || m.cancelRequested
                      : !canSubmit(m)
                  }
                  data-testid={
                    working ? "review-cancel-button" : "review-submit-button"
                  }
                  onClick={() => {
                    if (suppressClick.current) {
                      suppressClick.current = false;
                      return;
                    }
                    leverAction();
                  }}
                  onPointerDown={(e) => {
                    if (!canSubmit(m) || working || e.button !== 0) return;
                    drag.current = {
                      start: e.clientY,
                      last: 0,
                      dragged: false,
                    };
                    e.currentTarget.setPointerCapture(e.pointerId);
                  }}
                  onPointerMove={(e) => {
                    if (!drag.current) return;
                    const travel = e.currentTarget.clientHeight * 0.45;
                    const y = Math.max(
                      0,
                      Math.min(travel, e.clientY - drag.current.start),
                    );
                    drag.current.last = travel ? (y / travel) * 46 : 0;
                    drag.current.dragged ||= drag.current.last > 3;
                    const handle = e.currentTarget.querySelector<HTMLElement>(
                      '[data-part="lever-handle"]',
                    );
                    if (handle) motion.current?.preview(handle, y);
                  }}
                  onPointerUp={(e) => {
                    const d = drag.current;
                    if (!d) return;
                    drag.current = null;
                    const handle = e.currentTarget.querySelector<HTMLElement>(
                      '[data-part="lever-handle"]',
                    );
                    handle?.style.removeProperty("transform");
                    if (d.dragged) {
                      suppressClick.current = true;
                      if (d.last >= (46 * 2) / 3) leverAction();
                    }
                  }}
                  onPointerCancel={(e) => {
                    drag.current = null;
                    e.currentTarget
                      .querySelector<HTMLElement>('[data-part="lever-handle"]')
                      ?.style.removeProperty("transform");
                  }}
                >
                  <span className="od-lever-handle" data-part="lever-handle">
                    <SmallArt kind="handle" />
                  </span>
                  <span className="od-lever-label">
                    {m.status === "SUBMITTING"
                      ? "Uploading"
                      : working && m.cancelRequested
                        ? "Stopping"
                        : working
                          ? "Stop"
                          : "Start"}
                  </span>
                </button>
              )}
              {plainView && (
                <div className="od-plain-operations">
                  <Key
                    testId="review-submit-button"
                    disabled={!canSubmit(m)}
                    onClick={() => action({ type: "submit" })}
                  >
                    Start toaster
                  </Key>
                  <Key
                    testId="review-cancel-button"
                    disabled={
                      !working || m.status === "SUBMITTING" || m.cancelRequested
                    }
                    onClick={() => action({ type: "cancel" })}
                  >
                    {m.cancelRequested && working ? "Stopping…" : "Stop review"}
                  </Key>
                </div>
              )}
              {/* NOT a live region (issue #510, held through #733). This lamp
                  restates the status the announcement region below already
                  speaks; leaving it `role="status"` made two polite regions
                  narrate the same terminal moment, one of them in words
                  written for a glance rather than for a screen reader. It is
                  read on demand, like any other label. */}
              <div className="od-ready" data-testid="review-status">
                <span
                  data-part="ready-lamp"
                  className="od-ready-lamp"
                  aria-hidden="true"
                />
                {statusText(m)}
              </div>
              <div className="od-toaster-keys">
                <button
                  type="button"
                  className="od-key od-browse"
                  onClick={(e) => open("playbooks", e.currentTarget)}
                >
                  Browse playbooks
                </button>
                {m.hasOutput ? (
                  <Key
                    testId="review-download-button"
                    disabled={m.downloading}
                    onClick={() => action({ type: "download" })}
                  >
                    {m.downloading ? "Preparing…" : "Save redline"}
                  </Key>
                ) : m.status === "ERROR" ? (
                  <Key
                    testId="review-retry-button"
                    onClick={() => action({ type: "retry" })}
                  >
                    Toast another slice
                  </Key>
                ) : m.reviewId && !(manual && m.result) ? (
                  // Issue #734. One "Review details" at a time. The result
                  // card owns the key on the two manual statuses, because
                  // that is where it sits beside the next step it explains;
                  // the rail keeps it for every other review that has an id
                  // and no redline (running, cancelled, done-without-output),
                  // and for the defensive case of a manual handoff that
                  // somehow projected no result to put the key in.
                  <Key onClick={() => open("record")}>Review details</Key>
                ) : null}
              </div>
            </div>
            <div
              className="od-intake-controls"
              data-testid="review-intake-slot"
              hidden={working || terminal}
            >
              <input
                ref={file}
                type="file"
                accept=".docx"
                onChange={(e) => selectFile(e.target.files?.[0] ?? null)}
                className="od-file-input"
                data-testid="review-file-input"
                aria-label="Choose Word document"
              />
              <button
                className="od-link-key"
                type="button"
                onClick={() => file.current?.click()}
              >
                {m.filename ? "Replace document" : "Choose document"}
              </button>
              {m.filename && (
                <>
                  <span>
                    {m.fileBytes == null
                      ? ""
                      : `${(m.fileBytes / 1024).toFixed(m.fileBytes < 1024 ? 1 : 0)} KB`}
                  </span>
                  <button
                    className="od-link-key"
                    type="button"
                    onClick={() => selectFile(null)}
                  >
                    Eject
                  </button>
                </>
              )}
            </div>
            {!active.length && (
              <p
                className="od-local-error"
                role="status"
                data-testid="review-no-playbooks"
              >
                No playbook is active. An admin needs to activate one.
              </p>
            )}
            {fileError && (
              <p className="od-local-error" role="alert">
                {fileError}
              </p>
            )}
            {working && (
              <div
                className="od-progress"
                role="progressbar"
                aria-valuemin={stage ? 1 : undefined}
                aria-valuemax={stage ? 4 : undefined}
                aria-valuenow={stage?.step}
                aria-label="Contract review"
                aria-valuetext={
                  m.status === "SUBMITTING"
                    ? "Uploading your document."
                    : working
                      ? stage
                        ? `Step ${stage.step} of 4. ${stage.caption}`
                        : "Toasting your review. Stage not yet reported."
                      : statusText(m)
                }
                data-testid="toaster-state-progress"
              >
                <span
                  className="od-stage-name"
                  data-testid="review-progress-step-text"
                >
                  {working
                    ? stage
                      ? `Step ${stage.step} of 4`
                      : "Stage not yet reported"
                    : statusText(m)}
                </span>
                <span aria-live="polite" data-testid="review-stage-caption">
                  {m.status === "SUBMITTING"
                    ? "Uploading your document…"
                    : working
                      ? (stage?.caption ?? "Toasting your review…")
                      : m.status === "DONE"
                        ? "Your reviewed document is ready."
                        : manual
                          ? "A person needs to review this result."
                          : "Document review, with a second model checking the markup."}
                </span>
                {/* Issue #71. The host's already-composed line — "About 4
                    minutes", or "Taking longer than usual" past the sample's
                    p90. Printed verbatim as a text node: the console holds no
                    clock, fetches no estimate and never renders a countdown.
                    Absent when there is no sample to answer from, which is
                    the whole of the no-estimate treatment. */}
                {m.timeRemaining && (
                  <span
                    className="od-time-remaining"
                    data-testid="review-time-remaining"
                  >
                    {m.timeRemaining}
                  </span>
                )}
              </div>
            )}
          </section>
          <section
            className="od-register"
            aria-label="Configuration, cost and receipt"
          >
            <div className="od-register-figure">
              {artworkShown && (
                <>
                  <MaterialArt
                    kind="register"
                    base={assetBase}
                    className="od-register-full"
                  />
                  <MaterialArt
                    kind="register"
                    base={assetBase}
                    compact
                    className="od-register-compact"
                  />
                </>
              )}
              <div className="od-price" data-testid="review-cost-estimate">
                <span>{costLabel}</span>
                <output>{costText(m)}</output>
              </div>
              {/* Issue #737 (owner decision H3). The second glass is the
                  operational message window, and with no message it must read
                  as a display that is OFF — which the stylesheet does by
                  taking the empty text layer out of the picture entirely
                  (`visibility: hidden` illustrated, `display: none` narrow and
                  plain) so `register.webp`'s own photographed unlit glass is
                  what shows. Nothing is painted in its place.

                  That is exactly why the announcer cannot BE this element. A
                  polite live region has to be in the accessibility tree BEFORE
                  its content changes; one that is hidden while empty is a new
                  region at the moment the message arrives, and a new region is
                  not announced. So the glass is decorative and `aria-hidden`,
                  and the announcer below it stays mounted and empty for the
                  whole session. The off state has no wording of its own — the
                  empty string is what it announces, which is nothing. */}
              <div className="od-register-status" aria-hidden="true">
                {registerLine}
              </div>
              <span
                className="od-sr"
                data-testid="review-register-announcement"
                role="status"
                aria-live="polite"
              >
                {registerLine}
              </span>
              <div className="od-register-controls">
                <fieldset
                  title={m.browningReadback}
                  aria-describedby={`${uid}-browning-readback`}
                  data-testid="review-browning-control"
                >
                  <legend>Markup intensity</legend>
                  <div className="od-radio-row">
                    {(["light", "medium", "dark"] as const).map((value) => (
                      <label className="od-radio-key" key={value}>
                        <input
                          type="radio"
                          name={`${uid}-intensity`}
                          data-testid={`review-browning-option-${value}`}
                          value={value}
                          checked={p.intensity === value}
                          onChange={() => {
                            onPreferences({ intensity: value });
                            fire("key");
                          }}
                        />
                        <span>{value[0].toUpperCase() + value.slice(1)}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
                <fieldset data-testid="review-notes-mode-control">
                  <legend>Footnotes</legend>
                  <div className="od-radio-row od-four">
                    {(["none", "external", "internal", "both"] as const).map(
                      (value) => (
                        <label className="od-radio-key" key={value}>
                          <input
                            type="radio"
                            name={`${uid}-notes`}
                            checked={p.notesMode === value}
                            disabled={
                              !m.internalNotesAvailable &&
                              ["internal", "both"].includes(value)
                            }
                            value={value}
                            onChange={() => {
                              onPreferences({ notesMode: value });
                              fire("key");
                            }}
                            data-testid={`review-notes-mode-option-${value}`}
                          />
                          <span>{value[0].toUpperCase() + value.slice(1)}</span>
                        </label>
                      ),
                    )}
                  </div>
                </fieldset>
              </div>
              <button
                className="od-receipt-printer"
                type="button"
                disabled={m.status !== "DONE" || !m.receiptLines}
                onClick={(e) => open("receipt", e.currentTarget)}
                aria-label="View review receipt"
              >
                <span
                  className="od-receipt-paper"
                  data-part="receipt-paper"
                  data-testid="review-receipt-paper"
                >
                  <span className="od-paper-ink">
                    View <br />
                    receipt
                  </span>
                </span>
              </button>
              <div className="od-till" data-part="till-drawer">
                {canButter && (
                  <Key
                    title="Draft a cover note for the counterparty"
                    ariaLabel="Butter it: open cover note"
                    testId="review-cover-note-butter"
                    part="butter-key"
                    onClick={() => {
                      open("cover");
                      if (!m.cover || m.cover.state === "idle")
                        action({ type: "butter" });
                    }}
                  >
                    Butter it
                  </Key>
                )}
                {canDispose(m) && (
                  <Key
                    title="Record how this review was used"
                    onClick={() => open("disposition")}
                  >
                    Record outcome
                  </Key>
                )}
                <Key
                  onClick={() => action({ type: "sound-toggle" })}
                  selected={!m.muted}
                  testId="sound-toggle"
                  title="Mechanical sounds played by this page. This does not enable browser completion alerts."
                  ariaLabel={`App sounds ${m.muted ? "muted" : "on"}. Toggle appliance sounds.`}
                >
                  <span>
                    App sounds
                    <span className="od-key-state">
                      {m.muted ? "Muted" : "On"}
                    </span>
                  </span>
                </Key>
                {m.notification !== "unsupported" && (
                  <Key
                    onClick={() => action({ type: "notification-toggle" })}
                    selected={m.notification === "granted"}
                    testId="notify-toggle"
                    title="A browser notification when the review finishes while this tab is hidden. Separate from appliance sounds; muted alerts are silent."
                    ariaLabel={`Browser completion alerts ${m.notification === "granted" ? "on" : m.notification === "denied" ? "blocked" : "off"}. Notify when this tab is hidden.`}
                  >
                    <span>
                      Browser alerts
                      <span className="od-key-state">
                        {m.notification === "granted"
                          ? "On"
                          : m.notification === "denied"
                            ? "Blocked"
                            : "Off"}
                      </span>
                    </span>
                  </Key>
                )}
                {/* Issue #720: the host panel's `?` and Cmd/Ctrl + / press
                    THIS control, so the cheat sheet has one owner and one
                    open/close state rather than a second list beside it. */}
                <Key
                  onClick={() => open("shortcuts")}
                  testId="review-shortcuts-key"
                >
                  Shortcuts
                </Key>
              </div>
            </div>
            {terminal && (
              <span className="od-sr" id={`${uid}-browning-readback`}>
                {m.browningReadback}
              </span>
            )}
            {terminal && m.notesDisclosure && (
              <div className="od-readback">
                <p data-testid="review-notes-mode-internal-disclosure">
                  {m.notesDisclosure}
                </p>
              </div>
            )}
            {!terminal && (
              <div className="od-readback">
                <p
                  id={`${uid}-browning-readback`}
                  data-testid="review-browning-note"
                >
                  {m.browningNote}
                  {m.browningSentence && (
                    <>
                      {" "}
                      <q data-testid="review-browning-sentence">
                        {m.browningSentence}
                      </q>
                    </>
                  )}
                </p>
                {m.notesDisclosure && (
                  <p data-testid="review-notes-mode-internal-disclosure">
                    {m.notesDisclosure}
                  </p>
                )}
                {working && (
                  <p>
                    Settings and instructions changed now apply to the next
                    review.
                  </p>
                )}
                <p>
                  {m.cost.kind === "unavailable"
                    ? "Estimate unavailable."
                    : m.cost.kind === "held" && m.cost.holdCents == null
                      ? "The reservation amount is not available."
                      : m.cost.kind === "estimated"
                        ? m.cost.basis === "document-model"
                          ? "Based on this document and the active model policy."
                          : "Based on active models and typical document length. Longer documents may cost more."
                        : ""}
                  {m.cost.coverCents != null &&
                    ` Cover note: ${money(m.cost.coverCents)}.`}
                </p>
              </div>
            )}
            {m.cost.adminDaily && (
              <div className="od-budget">
                <span>Daily budget · {m.cost.adminDaily.utcDate} UTC</span>
                <meter
                  min={0}
                  max={m.cost.adminDaily.capCents || 1}
                  value={Math.min(
                    m.cost.adminDaily.capCents,
                    m.cost.adminDaily.settledCents +
                      m.cost.adminDaily.reservedCents,
                  )}
                  aria-label="Daily committed spend"
                />
                <span>
                  {money(m.cost.adminDaily.settledCents)} settled ·{" "}
                  {money(m.cost.adminDaily.reservedCents)} reserved /{" "}
                  {money(m.cost.adminDaily.capCents)} cap
                </span>
              </div>
            )}
          </section>
        </div>
        {!!mainMessages.length && (
          <section className="od-status-window" aria-label="Review messages">
            {renderMessages(mainMessages)}
          </section>
        )}
        {/* Issue #733. `resultPanel` carries eight data-testids and an
            assertive `role="alert"`, so it may have exactly ONE render site at
            a time. A failed or manual-review result stays on the page — nobody
            should have to open something to read it — but the record and
            receipt overlays render the same fragment, so the page yields it to
            whichever overlay is open rather than duplicating every id and
            speaking the failure prose twice. */}
        {(m.status === "ERROR" || manual) &&
          modal !== "record" &&
          modal !== "receipt" &&
          resultPanel}
        <div className="od-counter-papers">
          <div className="od-ticket-column">
            {!terminal && (
              <div data-testid="review-preflight-card">
                <Panel title="Preflight ticket" className="od-preflight">
                  {!m.filename ? (
                    <p>Choose a document to run the advisory checks.</p>
                  ) : !m.preflight || m.preflight.state === "checking" ? (
                    <p role="status">
                      Checking the document… You can start while this runs.
                    </p>
                  ) : (
                    <>
                      {m.preflight.title && (
                        <p className="od-ticket-title">{m.preflight.title}</p>
                      )}
                      {m.preflight.wordCount != null && (
                        <p className="od-ticket-stat">
                          {m.preflight.wordCount.toLocaleString()} words
                          {m.preflight.pageEstimate != null && (
                            <> · about {m.preflight.pageEstimate} pages</>
                          )}
                        </p>
                      )}
                      {m.preflight.summary && (
                        <p data-testid="review-preflight-summary">
                          {m.preflight.summary}
                        </p>
                      )}
                      {/* Issue #733. Three verdicts, three distinct things to
                          say — and the neutral type/side line is what an
                          "unclear" verdict is ENTITLED to say: the classifier
                          had nothing to affirm or warn about, but what it read
                          is still worth stating. */}
                      {m.preflight.match === "likely" && (
                        <p data-testid="review-preflight-match-likely">
                          {m.preflight.readsLike
                            ? `${m.preflight.readsLike} `
                            : ""}
                          Looks consistent with this playbook.
                        </p>
                      )}
                      {m.preflight.match === "unlikely" && (
                        <p data-testid="review-preflight-match-unlikely">
                          {m.preflight.mismatchNote ??
                            "Another playbook may be a better match."}
                        </p>
                      )}
                      {m.preflight.match !== "likely" &&
                        m.preflight.match !== "unlikely" &&
                        m.preflight.readsLike && (
                          <p data-testid="review-preflight-type-side">
                            {m.preflight.readsLike}
                          </p>
                        )}
                      {recommended && (
                        <Key
                          testId="review-preflight-switch-playbook"
                          onClick={() => action({ type: "switch-recommended" })}
                        >
                          Switch to{" "}
                          {m.preflight.recommendedPlaybookName ??
                            "recommended playbook"}
                        </Key>
                      )}
                      {m.preflight.injectionCount != null &&
                        m.preflight.injectionCount > 0 && (
                          <p
                            className="od-warning"
                            data-testid="review-preflight-injection-flag"
                          >
                            {/* "1 flags" is wrong, and the count is the whole
                                point of the line (issue #506/#733). */}
                            Flagged {m.preflight.injectionCount} item
                            {m.preflight.injectionCount === 1 ? "" : "s"} for
                            review before you upload:{" "}
                            {m.preflight.injectionRuleIds?.join(", ")}.
                          </p>
                        )}
                      {/* Issue #55 (audit finding F6). Advisory, and the
                          copy says so: the review still runs, the go
                          button is untouched. No document text and no
                          entity name — naming the roster back at the
                          reviewer would put configuration on a card about
                          their document. */}
                      {m.preflight.partyUnrecognised && (
                        <p
                          className="od-warning"
                          data-testid="review-preflight-party-advisory"
                        >
                          None of your configured legal entities appears in
                          this document. The review will still run; check
                          the Settings roster if this is unexpected.
                        </p>
                      )}
                      {/* No apology line (issue #491, held through #733): the
                          deterministic stats above are the point, and a banner
                          announcing that the CHEAP model was unreachable is
                          noise about our plumbing on a card whose job is to
                          describe the reader's document. */}
                      <p className="od-fine">
                        Advisory · does not block review
                      </p>
                    </>
                  )}
                </Panel>
              </div>
            )}
          </div>
          <section
            className="od-pad-shell"
            aria-label="Special instructions pad"
            data-part="instructions-pad"
          >
            {artworkShown && <MaterialArt kind="pad" base={assetBase} />}
            <div className="od-pad-content">
              <label htmlFor={`${uid}-instructions`}>
                {guideLabel}
                <span>Optional</span>
              </label>
              <div
                className="od-guidance-field"
                data-testid="review-guidance-field"
              >
                <textarea
                  ref={instructions}
                  id={`${uid}-instructions`}
                  data-testid="review-guidance-input"
                  rows={6}
                  value={p.instructions}
                  onChange={(e) =>
                    onPreferences({ instructions: e.target.value })
                  }
                  placeholder="Add your instructions for the kitchen…"
                  aria-describedby={`${uid}-precedence`}
                  onFocus={() => fire("pad-focus")}
                />
              </div>
              <p id={`${uid}-precedence`}>{m.guidancePrecedence}</p>
              {m.result?.appliedGuidance != null && (
                <details
                  className="od-applied-guidance"
                  data-testid="review-applied-guidance"
                >
                  <summary onClick={() => fire("guidance-readback")}>
                    Sent with this review
                  </summary>
                  <p>
                    {m.result.appliedGuidance || "No additional instructions."}
                  </p>
                </details>
              )}
            </div>
          </section>
        </div>
        <div className="od-counter-footer">
          <button
            className="od-link-key"
            type="button"
            onClick={() => action({ type: "history" })}
          >
            History
          </button>
          {terminal && (
            <button
              className="od-link-key"
              type="button"
              onClick={() => file.current?.click()}
            >
              Choose next document
            </button>
          )}
          <button
            className="od-link-key"
            type="button"
            aria-pressed={plainView}
            // Issue #738. Disabled for the same reason forced colours disables
            // it: with no illustration available there is nothing to switch
            // to, and a key that reports a choice it cannot make is a lie.
            disabled={forcedColours || !artworkShown}
            onClick={() => {
              const next = !plainView;
              if (onPlainChange) onPlainChange(next);
              else setPlainOverride(next);
            }}
          >
            {plainView ? "Illustrated controls" : "Plain controls"}
          </button>
          {m.odometer != null && (
            <span className="od-odometer" data-part="odometer">
              {m.odometer.toLocaleString()} completed
            </span>
          )}
        </div>
        <span
          className="od-sr"
          data-testid="review-ready-announcement"
          role="status"
          aria-live="polite"
        >
          {/* Issue #733. Emptiness, not nullishness: the host composes
              `readyAnnouncement` only on the DONE handoff and leaves it as an
              empty string everywhere else, and `??` would let that empty
              string win — leaving a manual-review outcome with nothing in the
              polite region at all. */}
          {/* Issue #71 adds `timeAnnouncement` at the BOTTOM of this chain,
              never above the handoff: while a review is in flight the two
              fields above are empty and the estimate has this region to
              itself, and the moment the handoff has something to say it takes
              it back. One polite region, one voice at a time. */}
          {m.readyAnnouncement ||
            (m.status === "DONE"
              ? "Your review is ready."
              : manual
                ? "This review needs a human."
                : m.timeAnnouncement || "")}
        </span>
      </div>
      {/* Issue #740, owner decision N7. The printed sheet, and the only thing
          `orbit.css`'s `@media print` block lets through.

          It is rendered on every status and never shown on screen, which is
          what makes browser printing (Cmd/Ctrl+P) produce the same receipt
          whether the overlay is open, closed, or never opened. The words are
          `m.receiptLines` verbatim — `receiptText(receiptLines(detail))` as
          composed once in `ReviewSubmission` — so the sheet cannot say
          anything the slip on screen, the clipboard and the PNG do not
          (owner decision H7).

          `aria-hidden` because it is a second copy of content the console
          already offers to a screen reader through the receipt overlay: a
          print stylesheet is not an accessibility surface. */}
      <div
        className="od-print-receipt"
        data-part="print-receipt"
        data-testid="review-receipt-print"
        aria-hidden="true"
      >
        {m.receiptLines?.length ? (
          <pre
            className="od-print-receipt-text"
            data-testid="review-receipt-print-text"
          >
            {m.receiptLines.join("\n")}
          </pre>
        ) : (
          <p
            className="od-print-receipt-empty"
            data-testid="review-receipt-print-empty"
          >
            Contract Toaster — no receipt to print. A receipt is written when a
            review finishes; run a review, then print again.
          </p>
        )}
      </div>
      <dialog
        ref={dialog}
        className="od-dialog"
        data-part={modal === "receipt" ? "receipt-dialog" : undefined}
        aria-labelledby={`${uid}-dialog-title`}
        data-modal={modal ?? undefined}
        onCancel={(e) => {
          e.preventDefault();
          e.stopPropagation();
          close();
        }}
        onClick={(e) => {
          if (e.target === e.currentTarget) close();
        }}
        onClose={() => {
          if (modal) close();
        }}
      >
        <div className="od-dialog-paper">
          <div className="od-dialog-heading">
            <h2 id={`${uid}-dialog-title`}>
              {modal === "receipt"
                ? "Review receipt"
                : modal === "playbooks"
                  ? "Choose a playbook"
                  : modal === "record"
                    ? "Review record"
                    : modal === "disposition"
                      ? "Record outcome"
                      : modal === "cover"
                        ? "Cover note"
                        : "Keyboard shortcuts"}
            </h2>
            <button type="button" className="od-key" onClick={close} autoFocus>
              Close
            </button>
          </div>
          {modal === "receipt" && (
            <>
              <pre
                className="od-receipt-text"
                data-testid="review-receipt-text"
              >
                {m.receiptLines?.join("\n")}
              </pre>
              {renderMessages(
                (m.messages ?? []).filter((x) =>
                  ["receipt", "download", "support"].includes(x.scope),
                ),
              )}
              <div className="od-receipt-actions">
                <Key onClick={() => action({ type: "receipt-copy" })}>
                  Copy as text
                </Key>
                <Key onClick={() => action({ type: "receipt-save" })}>
                  Save image
                </Key>
                {/* Issue #740. The third rendering of the same lines, for the
                    paper file. Offered only when there are lines to print —
                    the browser's own Cmd/Ctrl+P still explains itself on a
                    review that has no receipt, but a key that prints an
                    apology is not a key worth pressing. */}
                {!!m.receiptLines?.length && (
                  <Key
                    testId="review-receipt-print-button"
                    onClick={printReceipt}
                  >
                    Print receipt
                  </Key>
                )}
              </div>
              {m.downloadStarted && (
                <p className="od-fine" data-testid="review-saved-line">
                  Download started automatically. Save redline repeats it.
                </p>
              )}
              {reviewTag}
              {m.result && (
                <details className="od-receipt-detail">
                  <summary>Review detail</summary>
                  {resultPanel}
                </details>
              )}
            </>
          )}
          {modal === "playbooks" && (
            <>
              {/* Issue #730, owner decision H5: an automatic choice is
                  silent. `m.playbookSelection` stays on the model as the
                  record of who chose, but this overlay reads the same line
                  either way — no explanation, no confirmation, no undo. */}
              <p className="od-fine">
                Choose the playbook to apply; the document type may differ.
              </p>
              <p className="od-fine">{m.browningReadback}</p>
              <label className="od-search-label">
                Find a playbook
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
              <div className="od-catalog">
                {m.playbooks
                  .filter((x) =>
                    x.display_name.toLowerCase().includes(search.toLowerCase()),
                  )
                  .map((x) => (
                    <button
                      type="button"
                      className="od-key od-catalog-entry"
                      key={x.playbook_id}
                      data-testid={`review-playbook-option-${x.playbook_id}`}
                      disabled={x.status !== "active"}
                      aria-pressed={x.playbook_id === p.playbookId}
                      onClick={() => {
                        changeBook(x.playbook_id);
                        close();
                      }}
                    >
                      <span>{x.display_name}</span>
                      {x.status !== "active" && <span>Coming soon</span>}
                    </button>
                  ))}
                {!m.playbooks.length && (
                  <p>No playbook is active. An admin needs to activate one.</p>
                )}
              </div>
            </>
          )}
          {modal === "record" && (
            <>
              {resultPanel}
              {reviewTag}
              {renderMessages(
                (m.messages ?? []).filter((x) =>
                  ["support", "download"].includes(x.scope),
                ),
              )}
            </>
          )}
          {modal === "cover" && (
            <>
              {coverPanel}
              {renderMessages(
                (m.messages ?? []).filter((x) => x.scope === "cover"),
              )}
            </>
          )}
          {modal === "disposition" && (
            <>
              {dispositionPanel}
              {renderMessages(
                (m.messages ?? []).filter((x) => x.scope === "disposition"),
              )}
            </>
          )}
          {modal === "shortcuts" && (
            <dl className="od-shortcuts">
              <dt>Cmd/Ctrl + Enter</dt>
              <dd>Start review</dd>
              <dt>Cmd/Ctrl + U or O</dt>
              <dd>Choose document</dd>
              <dt>Cmd/Ctrl + D or S</dt>
              <dd>Save redline</dd>
              <dt>P / G</dt>
              <dd>Playbook / instructions</dd>
              <dt>[ or − / ] or +</dt>
              <dd>Less / more markup</dd>
              <dt>1 / 2 / 3 / 4</dt>
              <dd>Footnote audience</dd>
              <dt>M / R</dt>
              <dd>Sound / recommended playbook</dd>
              <dt>Cmd/Ctrl + Shift + P / G</dt>
              <dd>Playbooks / instructions, including while typing</dd>
              <dt>Cmd/Ctrl + Shift + M / R</dt>
              <dd>Sound / recommended playbook, including while typing</dd>
              <dt>? / Cmd/Ctrl + /</dt>
              <dd>Keyboard shortcuts</dd>
              <dt>App sounds / Browser alerts</dt>
              <dd>
                Appliance sounds in this page / completion notification while
                this tab is hidden. Muted notifications are silent.
              </dd>
              <dt>Escape</dt>
              <dd>
                Close this card; otherwise eject when outside a text field
              </dd>
            </dl>
          )}
        </div>
      </dialog>
    </div>
  );
}
