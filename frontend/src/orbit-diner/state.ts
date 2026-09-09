import type { ReviewModel, Stage, MotionEvent } from "./types";
export const stages: Record<
  Stage,
  { step: number; label: string; caption: string; filter: string }
> = {
  primary_pass: {
    step: 1,
    label: "First read-through",
    caption: "Reading your contract against the playbook…",
    filter: "brightness(1.08) saturate(.78)",
  },
  critic_pass: {
    step: 2,
    label: "Adversarial critic",
    caption: "A second model is arguing with the markup…",
    filter: "brightness(.94) saturate(.9)",
  },
  reconciliation: {
    step: 3,
    label: "Reconciling both passes",
    caption: "Settling what survives…",
    filter: "brightness(.85) saturate(.98)",
  },
  redline: {
    step: 4,
    label: "Writing your redline",
    caption: "Writing your redline…",
    filter: "brightness(.77) saturate(1.08)",
  },
};
/* Local divergence from the supplier kit (#717): the kit calls `Object.hasOwn`,
   which is ES2022. This app targets ES2020 (frontend/tsconfig.json), where it
   neither typechecks nor downlevels, so the equivalent prototype call is used
   here and in sounds.ts. Re-apply this when merging the next supplier patch. */
export function stageInfo(stage?: string | null) {
  return stage && Object.prototype.hasOwnProperty.call(stages, stage)
    ? stages[stage as Stage]
    : null;
}
export const isWorking = (m: ReviewModel) =>
  ["SUBMITTING", "PENDING", "RUNNING"].includes(m.status);
export const isManual = (m: ReviewModel) =>
  ["MANUAL_REVIEW_REQUIRED", "ERROR_MANUAL_REVIEW_REQUIRED"].includes(m.status);
export const canSubmit = (m: ReviewModel) =>
  m.fileSelected &&
  !!m.filename &&
  !isWorking(m) &&
  m.playbooks.some(
    (p) => p.playbook_id === m.preferences.playbookId && p.status === "active",
  );
export const canDispose = (m: ReviewModel) =>
  m.status === "DONE" || isManual(m);
/* Local divergence from the supplier kit (#735, owner decision H2): the kit
   made the five TERMINAL statuses their own case, returning an em dash unless
   a complete settlement carried a number. This deployment has no settlement
   projection at all — see `projection.ts`'s `projectCost` — so every finished
   review read "FINAL REVIEW COST —", throwing away the estimate that had been
   captured for it and claiming a final figure it does not have.

   The figure is now decided by the cost's KIND alone, and `costLabel` in
   OrbitDiner.tsx names which figure it is: a finished review keeps its
   captured estimate under "Estimated review cost", and "Final review cost"
   appears only for a settlement that is genuinely complete.

   A complete settlement of `0` prints `$0.00`. A ledger that really settled
   at nothing is a fact worth showing; what must never be inferred is a $0.00
   for a review nobody priced, and `projectCost` is what routes an absent or
   non-positive estimate to `unavailable` instead. */
export function costText(m: ReviewModel) {
  const c = m.cost;
  if (c.kind === "unavailable") return "—";
  if (c.kind === "held")
    return c.holdCents == null ? "Held" : money(c.holdCents);
  if (c.kind === "settled")
    return c.settlementComplete && c.cents != null ? money(c.cents) : "Pending";
  return c.cents == null ? "—" : money(c.cents);
}
export const money = (cents: number) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(
    cents / 100,
  );
export function statusText(m: ReviewModel) {
  if (m.cancelRequested && isWorking(m)) return "Stopping…";
  if (m.status === "EMPTY") return "Choose a document";
  if (m.status === "LOADED")
    return canSubmit(m) ? "Ready to start" : "Choose an active playbook";
  if (m.status === "SUBMITTING") return "Uploading…";
  if (m.status === "DONE")
    return m.hasOutput ? "Redline ready" : "Review complete";
  if (m.status === "ERROR") return "That one burnt.";
  if (isManual(m)) return "Needs a human";
  if (m.status === "CANCELLED") return "Cancelled";
  return stageInfo(m.stage)?.label ?? "Toasting your review…";
}
export function inferMotion(
  previous: ReviewModel | undefined,
  next: ReviewModel,
): MotionEvent[] {
  if (!previous || (next.resumed && previous.reviewId !== next.reviewId))
    return [];
  const events: MotionEvent[] = [];
  if (previous.filename !== next.filename && next.filename)
    events.push("file-loaded");
  if (previous.filename && !next.filename) events.push("file-removed");
  if (
    previous.status === "SUBMITTING" &&
    ["RUNNING", "PENDING"].includes(next.status) &&
    !next.resumed
  )
    events.push("submit-accepted");
  if (previous.status !== next.status) {
    if (next.status === "DONE") events.push("done");
    else if (next.status === "ERROR") events.push("error");
    else if (isManual(next)) events.push("manual");
    else if (next.status === "CANCELLED") events.push("cancelled");
  }
  if (next.stage !== previous.stage && stageInfo(next.stage) && isWorking(next))
    events.push("stage");
  if (previous.cover?.state !== "ready" && next.cover?.state === "ready")
    events.push("cover-ready");
  if (!previous.disposition?.recorded && next.disposition?.recorded)
    events.push("disposition-saved");
  if (next.cost.kind === "held" && previous.cost.kind !== "held")
    events.push("reservation-confirmed");
  if (
    next.cost.kind === "settled" &&
    next.cost.settlementComplete &&
    !previous.cost.settlementComplete
  )
    events.push("settled");
  if (
    next.odometer != null &&
    previous.odometer != null &&
    next.odometer !== previous.odometer
  )
    events.push("odometer");
  return events;
}
