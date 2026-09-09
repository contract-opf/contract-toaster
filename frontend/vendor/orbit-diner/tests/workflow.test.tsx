// @vitest-environment jsdom
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { OrbitDiner } from "../src/OrbitDiner";
import { fixture } from "../preview/fixtures";
import {
  canSubmit,
  costText,
  inferMotion,
  isManual,
  stageInfo,
  statusText,
} from "../src/state";
import { preflightPlaybookChoice } from "../src/autoPlaybook";
import { createMotion, motionStyles } from "../src/motion";
import type { ReviewModel, Action } from "../src/types";
let root: Root, host: HTMLDivElement;
const media = new Map<string, any>();
beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  media.clear();
  window.matchMedia = vi.fn((q) => {
    if (!media.has(q))
      media.set(q, {
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      });
    return media.get(q);
  });
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
});
function render(m: ReviewModel, actions: Action[] = []) {
  act(() =>
    root.render(
      <OrbitDiner
        model={m}
        onFile={() => {}}
        onPreferences={() => {}}
        onAction={(a) => actions.push(a)}
      />,
    ),
  );
  return host;
}
describe("honest state projection", () => {
  it("does not infer elapsed progress or costs from missing data", () => {
    const m = fixture("RUNNING", "unknown");
    expect(stageInfo(m.stage)).toBe(null);
    expect(costText({ ...m, cost: { kind: "unavailable" } })).toBe("—");
    expect(costText({ ...m, cost: { kind: "held" } })).toBe("Held");
    expect(
      costText({
        ...m,
        cost: { kind: "settled", cents: 0, settlementComplete: false },
      }),
    ).toBe("Pending");
    expect(
      costText({
        ...m,
        cost: { kind: "settled", cents: 0, settlementComplete: true },
      }),
    ).toBe("$0.00");
  });
  it("waits for confirmed cancellation and lets completion win a cancel race", () => {
    const before = fixture("RUNNING");
    const requested = { ...before, cancelRequested: true };
    expect(inferMotion(before, requested)).not.toContain("cancelled");
    const done = { ...fixture("DONE"), cancelRequested: true };
    expect(statusText(done)).toBe("Redline ready");
    expect(inferMotion(requested, done)).toContain("done");
    expect(inferMotion(requested, done)).not.toContain("cancelled");
  });
  it("does not replay a lever ritual on resume or initial mount", () => {
    const running = { ...fixture("RUNNING"), resumed: true };
    expect(inferMotion(undefined, running)).toEqual([]);
    expect(inferMotion(fixture("SUBMITTING"), running)).toEqual([]);
  });
  it("separates manual review from burnt and requires an actual selected File to arm", () => {
    expect(isManual(fixture("ERROR_MANUAL_REVIEW_REQUIRED"))).toBe(true);
    const m = { ...fixture("DONE"), fileSelected: false };
    expect(canSubmit(m)).toBe(false);
    expect(canSubmit(fixture("LOADED"))).toBe(true);
  });
});
describe("control and accessibility contract", () => {
  it("keeps completed detail behind the receipt, cover and outcome keys", () => {
    const actions: Action[] = [];
    const h = render(fixture("DONE", "cover"), actions);
    expect(h.querySelector('[data-testid="review-preflight-card"]')).toBeNull();
    expect(h.querySelector('[data-testid="review-result"]')).toBeNull();
    expect(h.querySelector(".od-disposition")).toBeNull();
    expect(h.querySelector(".od-register-status")?.textContent).toBe("");
    expect(h.querySelector(".od-ready")?.textContent).toBe("Redline ready");
    const press = (text: string) =>
      act(() =>
        [...h.querySelectorAll<HTMLButtonElement>("button")]
          .find((x) => x.textContent?.trim() === text)!
          .click(),
      );
    act(() =>
      (
        h.querySelector(
          '[data-testid="review-cover-note-butter"]',
        ) as HTMLButtonElement
      ).click(),
    );
    expect(h.querySelector("dialog[open]")?.textContent).toContain(
      "Regenerate",
    );
    expect(actions).toEqual([]); // opening an existing draft must not bill again
    press("Close");
    press("Record outcome");
    expect(
      h.querySelector('dialog[open] textarea[maxlength="4000"]'),
    ).toBeTruthy();
    press("Accepted with edits");
    expect(actions).toEqual([
      { type: "disposition", outcome: "EDITED", note: "" },
    ]);
    press("Close");
    act(() =>
      (h.querySelector(".od-receipt-printer") as HTMLButtonElement).click(),
    );
    const receipt = h.querySelector("dialog[open]")!;
    expect(receipt.textContent).toContain("Copy ID");
    expect(receipt.textContent).toContain("Save original");
    expect(receipt.textContent).toContain("Changes requested");
    expect(h.querySelector("style")?.textContent).toContain(
      "prefers-reduced-motion: reduce",
    );
  });
  it("does not show a prior estimate as final cost or announce receipt feedback in the register", () => {
    const m = fixture("DONE");
    expect(costText(m)).toBe("—");
    expect(
      costText({
        ...m,
        cost: { kind: "settled", cents: 27, settlementComplete: false },
      }),
    ).toBe("—");
    expect(
      costText({
        ...m,
        cost: { kind: "settled", cents: 0, settlementComplete: true },
      }),
    ).toBe("$0.00");
    m.messages = [{ id: "copy", scope: "receipt", title: "Receipt copied." }];
    const h = render(m);
    expect(h.querySelector(".od-register-status")?.textContent).not.toContain(
      "copied",
    );
    act(() =>
      (h.querySelector(".od-receipt-printer") as HTMLButtonElement).click(),
    );
    expect(h.querySelector("dialog[open]")?.textContent).toContain(
      "Receipt copied.",
    );
    expect(h.querySelector(".od-status-window")).toBeNull();
  });
  it("represents known stages as ordinal steps, without claiming timed percentage completion", () => {
    render(fixture("RUNNING"));
    const bar = host.querySelector('[role="progressbar"]')!;
    expect(bar.getAttribute("aria-valuenow")).toBe("2");
    expect(bar.getAttribute("aria-valuemax")).toBe("4");
    expect(bar.getAttribute("aria-valuetext")).toContain("Step 2 of 4");
  });
  it("uses distinct guarded Start and Stop controls when forced colours is active", () => {
    media.set("(forced-colors: active)", {
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
    const actions: Action[] = [];
    render(fixture("RUNNING"), actions);
    expect(host.querySelector(".od-plain")).toBeTruthy();
    const start = host.querySelector(
      '[data-testid="review-submit-button"]',
    ) as HTMLButtonElement;
    const stop = host.querySelector(
      '[data-testid="review-cancel-button"]',
    ) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    expect(stop.disabled).toBe(false);
    act(() => stop.click());
    expect(actions).toEqual([{ type: "cancel" }]);
  });
  it("closes modal Escape locally without clearing a file or submitting", () => {
    const actions: Action[] = [];
    render(fixture("LOADED"), actions);
    act(() => (host.querySelector(".od-browse") as HTMLButtonElement).click());
    const dialog = host.querySelector("dialog[open]")!;
    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    act(() => dialog.dispatchEvent(event));
    expect(event.defaultPrevented).toBe(true);
    expect(host.querySelector("dialog[open]")).toBeNull();
    expect(actions).toEqual([]);
  });
  it("has no invented numeric progress and prevents unarmed submission", () => {
    const actions: Action[] = [];
    render(fixture("EMPTY"), actions);
    const start = host.querySelector(
      "[data-testid=review-submit-button]",
    ) as HTMLButtonElement;
    act(() => start.click());
    expect(actions).toEqual([]);
    render(fixture("RUNNING", "unknown"));
    const bar = host.querySelector("[role=progressbar]")!;
    expect(bar.hasAttribute("aria-valuenow")).toBe(false);
    expect(bar.getAttribute("aria-valuetext")).toContain(
      "Stage not yet reported",
    );
  });
  it("keeps dynamic catalog entries native and excludes disabled values", () => {
    const m = fixture("LOADED");
    m.playbooks = Array.from({ length: 24 }, (_, i) => ({
      playbook_id: `p-${i}`,
      display_name: `Long contract title ${i}`,
      status: i === 23 ? "coming_soon" : "active",
    }));
    m.preferences.playbookId = "p-0";
    render(m);
    expect(host.querySelectorAll("select option")).toHaveLength(24);
    expect(
      host.querySelector('option[value="p-23"]')?.hasAttribute("disabled"),
    ).toBe(true);
  });
  it("escapes instruction and critic text instead of interpreting HTML", () => {
    const m = fixture("DONE");
    m.preferences.instructions = "<img src=x onerror=alert(1)>";
    m.result!.criticDetails = ["<script>throw 1</script>"];
    render(m);
    expect(host.querySelector("script")).toBe(null);
    expect(host.querySelector("img")).toBe(null);
    expect(
      (
        host.querySelector(
          "[data-testid=review-guidance-input]",
        ) as HTMLTextAreaElement
      ).value,
    ).toContain("<img");
  });
  it("keeps two independently mounted instances free of duplicate SVG ids", () => {
    act(() =>
      root.render(
        <>
          <OrbitDiner
            model={fixture("LOADED")}
            onFile={() => {}}
            onPreferences={() => {}}
            onAction={() => {}}
          />
          <OrbitDiner
            model={fixture("LOADED")}
            onFile={() => {}}
            onPreferences={() => {}}
            onAction={() => {}}
          />
        </>,
      ),
    );
    const ids = [...host.querySelectorAll("[id]")].map((x) => x.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
describe("motion rails", () => {
  it("limits animation properties and cancels immediately for reduced motion", () => {
    const shell = document.createElement("div");
    shell.innerHTML =
      '<button class="od-key"></button><div data-part="slice"></div><div data-part="receipt-paper"></div>';
    host.append(shell);
    const cancel = vi.fn();
    const animate = vi.fn(() => ({ cancel, finished: Promise.resolve() }));
    for (const el of shell.children) (el as any).animate = animate;
    const control = createMotion(shell);
    control.play("done");
    expect(animate).toHaveBeenCalledTimes(2);
    for (const [frames] of animate.mock.calls as any[])
      for (const frame of frames)
        for (const property of Object.keys(frame))
          expect(["transform", "opacity", "filter", "offset"]).toContain(
            property,
          );
    const reduce = media.get("(prefers-reduced-motion: reduce)");
    reduce.matches = true;
    reduce.addEventListener.mock.calls[0][1]();
    expect(cancel).toHaveBeenCalled();
    const before = animate.mock.calls.length;
    control.play("key");
    expect(animate).toHaveBeenCalledTimes(before);
    expect(motionStyles).toContain("forced-colors: active");
    control.dispose();
  });
});

describe("automatic playbook selection", () => {
  const input = {
    selectedId: "nda-mutual",
    recommendedId: "msa",
    classification: "ok" as const,
    currentFileKey: "file-2",
    responseFileKey: "file-2",
    userOverride: false,
    working: false,
    playbooks: fixture("LOADED").playbooks,
  };
  it("accepts a current active recommendation and permits the dial to override it", () => {
    expect(preflightPlaybookChoice(input)).toBe("msa");
    expect(preflightPlaybookChoice({ ...input, userOverride: true })).toBe(
      "nda-mutual",
    );
  });
  it("ignores stale-file, unavailable and in-flight recommendations", () => {
    expect(
      preflightPlaybookChoice({ ...input, responseFileKey: "file-1" }),
    ).toBe("nda-mutual");
    expect(
      preflightPlaybookChoice({ ...input, classification: "unavailable" }),
    ).toBe("nda-mutual");
    expect(preflightPlaybookChoice({ ...input, working: true })).toBe(
      "nda-mutual",
    );
    expect(preflightPlaybookChoice({ ...input, recommendedId: "dpa" })).toBe(
      "nda-mutual",
    );
  });
});
