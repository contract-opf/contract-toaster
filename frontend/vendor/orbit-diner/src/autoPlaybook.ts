import type { Playbook } from "./types";
/** Adapter helper. Return a selected id; never fetch, submit, or mutate preferences here. */
export function preflightPlaybookChoice(input: {
  selectedId: string;
  recommendedId?: string;
  classification: "ok" | "unavailable";
  currentFileKey: string;
  responseFileKey: string;
  userOverride: boolean;
  working: boolean;
  playbooks: readonly Playbook[];
}): string {
  if (
    input.working ||
    input.userOverride ||
    input.classification !== "ok" ||
    !input.currentFileKey ||
    input.currentFileKey !== input.responseFileKey ||
    !input.playbooks.some(
      (p) => p.playbook_id === input.recommendedId && p.status === "active",
    )
  )
    return input.selectedId;
  return input.recommendedId!;
}
