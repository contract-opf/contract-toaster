import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type KeyboardEvent,
} from "react";
import { MaterialArt, SmallArt } from "./MaterialArt";
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
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  selected?: boolean;
  testId?: string;
  part?: string;
  title?: string;
  ariaLabel?: string;
}) {
  return (
    <button
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
  assetBase = "artwork",
  onFile,
  onPreferences,
  onAction,
  onSound,
  plain = false,
  onPlainChange,
  keyboardShortcuts = true,
}: OrbitDinerProps) {
  const root = useRef<HTMLDivElement>(null),
    file = useRef<HTMLInputElement>(null),
    motion = useRef<MotionController>(),
    previous = useRef<ReviewModel>(),
    lastEvent = useRef<string>();
  const instructions = useRef<HTMLTextAreaElement>(null),
    dialog = useRef<HTMLDialogElement>(null),
    dialogOpener = useRef<HTMLElement | null>(null);
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
    [forcedColours, setForcedColours] = useState(false);
  const plainView = forcedColours || (plainOverride ?? plain);
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
      : m.cost.kind === "unavailable"
        ? "Estimate unavailable"
        : m.cost.kind === "held"
          ? "Reservation confirmed"
          : m.status === "SUBMITTING"
            ? "Awaiting acceptance"
            : "Review settings");
  const renderMessages = (messages: readonly Message[]) =>
    messages.map((message) => (
      <div
        key={message.id}
        role={message.tone === "error" || message.action ? "alert" : "status"}
      >
        <strong>{message.title}</strong>
        {message.detail && <p>{message.detail}</p>}
        {message.action && (
          <Key onClick={() => action({ type: message.action } as Action)}>
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
    dialogOpener.current?.focus();
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
  const visibleFilename = m.filename ?? "";
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
  const costLabel = terminal
    ? "Final review cost"
    : m.cost.kind === "held"
      ? "Review reservation"
      : m.cost.kind === "settled"
        ? m.cost.settlementComplete
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
        {m.result.failureCause && (
          <div data-testid="review-failure">
            <p>{m.result.failureCause}</p>
            {m.result.failureFix && <p>{m.result.failureFix}</p>}
            <p className="od-fine">
              {m.result.failingStage}
              {m.result.reason && ` · ${m.result.reason}`}
            </p>
          </div>
        )}
        <dl className="od-result-facts">
          {m.result.issueCount != null && (
            <>
              <dt>Changes requested</dt>
              <dd>{m.result.issueCount}</dd>
            </>
          )}
          {m.result.criticContested != null && (
            <>
              <dt>Contested by the critic</dt>
              <dd>{m.result.criticContested}</dd>
            </>
          )}
          {m.result.criticAdded != null && (
            <>
              <dt>Added by the critic</dt>
              <dd>{m.result.criticAdded}</dd>
            </>
          )}
          {m.result.confidenceBand && (
            <>
              <dt>Confidence band</dt>
              <dd>{m.result.confidenceBand}</dd>
            </>
          )}
        </dl>
        {manual && m.result.reason && (
          <p className="od-fine">
            {m.result.failingStage} · {m.result.reason}
          </p>
        )}
        {m.result.criticDetails?.map((text, i) => (
          <p key={i}>{text}</p>
        ))}
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
        <div className="od-disposition">
          <h2>Record the outcome</h2>
          <p>This becomes part of the review’s record.</p>
          {m.disposition?.recorded ? (
            <p className="od-stamp" data-part="disposition-stamp">
              Recorded: {dispositionNames[m.disposition.recorded]}
            </p>
          ) : (
            <>
              <label>
                Optional disposition note
                <textarea
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
                <Key onClick={() => action({ type: "cover-retry" })}>
                  Retry cover note
                </Key>
              )}
            </>
          ) : (
            <>
              <p className="od-cover-draft">{m.cover.draft}</p>
              <div className="od-receipt-actions">
                <Key onClick={() => action({ type: "cover-copy" })}>Copy</Key>
                <Key onClick={() => action({ type: "cover-regenerate" })}>
                  Regenerate
                </Key>
              </div>
              <p className="od-fine">
                Regenerating makes a new billed request.
                {m.cost.coverCents != null &&
                  ` Last recorded cost: ${money(m.cost.coverCents)}.`}
                {m.cover.cached && " Cached draft."}
              </p>
            </>
          )}
        </Panel>
      )}
    </>
  );
  const reviewTag = (
    <>
      {" "}
      {m.reviewId && (
        <div className="od-review-tag">
          <span>Review {m.reviewId}</span>
          <button
            type="button"
            className="od-link-key"
            onClick={() => action({ type: "copy-id" })}
          >
            Copy ID
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
      data-status={m.status.toLowerCase()}
      data-testid={`toaster-state-${m.status.toLowerCase()}`}
      data-terminal={terminal || undefined}
      onKeyDown={keyHandler}
      onClickCapture={(e) => {
        const key = (e.target as Element).closest(
          ".od-key,.od-radio-key,.od-link-key,.od-browse",
        );
        if (key) motion.current?.play("key", key);
      }}
      style={
        {
          "--od-counter-image": `url("${assetBase}/counter.webp")`,
          "--od-pad-image": `url("${assetBase}/pad.webp")`,
        } as CSSProperties
      }
    >
      <style>{motionStyles}</style>
      <div className="od-scene">
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
                  <MaterialArt
                    kind="toast"
                    base={assetBase}
                    className="od-toast-texture"
                  />
                  <span className="od-toast-name">{visibleFilename}</span>
                  {m.cover?.state === "ready" && (
                    <span className="od-butter-on-toast">
                      <SmallArt kind="butter" />
                    </span>
                  )}
                </button>
              )}
              {m.status === "ERROR" && <SmallArt kind="steam" />}
              <MaterialArt kind="toaster" base={assetBase} part="shell" />
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
                <MaterialArt kind="dial" base={assetBase} />
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
              <div
                className="od-ready"
                role="status"
                data-testid="review-status"
              >
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
                  <Key onClick={() => action({ type: "retry" })}>
                    Toast another slice
                  </Key>
                ) : m.reviewId ? (
                  <Key onClick={() => open("record")}>Review details</Key>
                ) : null}
              </div>
            </div>
            <div className="od-intake-controls" hidden={working || terminal}>
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
              <p className="od-local-error" role="status">
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
                <span className="od-stage-name">
                  {working
                    ? stage
                      ? `Step ${stage.step} of 4`
                      : "Stage not yet reported"
                    : statusText(m)}
                </span>
                <span aria-live="polite">
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
              </div>
            )}
          </section>
          <section
            className="od-register"
            aria-label="Configuration, cost and receipt"
          >
            <div className="od-register-figure">
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
              <div className="od-price" data-testid="review-cost-estimate">
                <span>{costLabel}</span>
                <output>{costText(m)}</output>
              </div>
              <div className="od-register-status" aria-live="polite">
                {registerLine}
              </div>
              <div className="od-register-controls">
                <fieldset
                  title={m.browningReadback}
                  aria-describedby={`${uid}-browning-readback`}
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
                <fieldset>
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
                <Key onClick={() => open("shortcuts")}>Shortcuts</Key>
              </div>
            </div>
            {terminal && (
              <span className="od-sr" id={`${uid}-browning-readback`}>
                {m.browningReadback}
              </span>
            )}
            {terminal && m.notesDisclosure && (
              <div className="od-readback">
                <p>{m.notesDisclosure}</p>
              </div>
            )}
            {!terminal && (
              <div className="od-readback">
                <p id={`${uid}-browning-readback`}>{m.browningReadback}</p>
                {m.notesDisclosure && <p>{m.notesDisclosure}</p>}
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
        {(m.status === "ERROR" || manual) && resultPanel}
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
                      {m.preflight.wordCount != null && (
                        <p className="od-ticket-stat">
                          {m.preflight.wordCount.toLocaleString()} words
                          {m.preflight.pageEstimate != null && (
                            <> · about {m.preflight.pageEstimate} pages</>
                          )}
                        </p>
                      )}
                      {m.preflight.summary && <p>{m.preflight.summary}</p>}
                      {m.preflight.match === "likely" && (
                        <p>Looks consistent with this playbook.</p>
                      )}
                      {recommended && (
                        <>
                          <p>Another playbook may be a better match.</p>
                          <Key
                            onClick={() =>
                              action({ type: "switch-recommended" })
                            }
                          >
                            Switch to{" "}
                            {m.preflight.recommendedPlaybookName ??
                              "recommended playbook"}
                          </Key>
                        </>
                      )}
                      {m.preflight.injectionCount != null &&
                        m.preflight.injectionCount > 0 && (
                          <p className="od-warning">
                            Potential embedded instructions:{" "}
                            {m.preflight.injectionCount} flags.{" "}
                            {m.preflight.injectionRuleIds?.join(", ")}
                          </p>
                        )}
                      {m.preflight.state === "unavailable" && (
                        <p>Some preflight checks are unavailable.</p>
                      )}
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
            <MaterialArt kind="pad" base={assetBase} />
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
              <p id={`${uid}-precedence`}>
                Your instructions govern conflicting playbook positions. Hard
                playbook requirements still apply.
              </p>
              {m.result?.appliedGuidance != null && (
                <details className="od-applied-guidance">
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
            disabled={forcedColours}
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
          aria-live="polite"
        >
          {m.status === "DONE"
            ? "Your review is ready."
            : manual
              ? "This review needs a human."
              : ""}
        </span>
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
              </div>
              {m.downloadStarted && (
                <p className="od-fine">
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
              <p className="od-fine">
                {m.playbookSelection === "automatic"
                  ? "Selected from preflight. You can override this choice."
                  : "Choose the playbook to apply; the document type may differ."}
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
