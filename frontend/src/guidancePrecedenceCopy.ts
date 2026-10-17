/**
 * guidancePrecedenceCopy.ts — the one sentence stating how free-text
 * guidance relates to a playbook's positions, shared by every surface that
 * collects such text (issue #484, epic #481).
 *
 * Before this file the sentence was owned solely by ReviewSubmission.tsx's
 * per-review `toaster_guidance` field (issue #431,
 * `GUIDANCE_PRECEDENCE_COPY`) and its wording is load-bearing there: per
 * that constant's own comment, ARCHITECTURE.md's "Guidance-precedence
 * model" enforces this precedence by INSTRUCTION to the model
 * (`scripts/primary_review_pass.py`'s intro blocks say "GOVERNS"), never
 * mechanically — so this says "govern", never "will override" or
 * "enforces", which would promise a guarantee the system does not make.
 * `scripts/primary_review_pass.py`'s `STANDING_INSTRUCTIONS_INTRO` uses the
 * identical framing for the playbook-level standing-instructions block this
 * constant now also backs (issue #482/#483, `AdminInstructions.tsx`), so
 * one wording change here keeps both UI surfaces honest about the same
 * underlying (non-)guarantee at once.
 *
 * Each caller supplies its own lead-in (who "these" or "they" refers to)
 * and its own closing sentence(s) — this constant is deliberately just the
 * shared middle clause, phrased to read naturally after either "These
 * instructions" or "They".
 */
export const GUIDANCE_PRECEDENCE_COPY =
  "govern over the playbook's positions wherever the two conflict — but never over rules " +
  'the playbook marks as hard requirements, which nothing can override.';

/**
 * The standing-instructions editor's one line on clauses the playbook does
 * not cover (issue #158): the instructions can set the direction for them,
 * and that is all it says. What the reviewer does there without them is the
 * OPF prompt's own default, `SILENT_PLAYBOOK_DEFAULT` in
 * `scripts/opf_prompt.py`, which yields to the standing instructions. This
 * line deliberately does not describe that default: the editor renders for
 * every playbook, and a v1 playbook's prompt does not carry it, so a
 * description would be false there -- and would go stale whenever the
 * default's wording changes. Per issue #484's rule it cross-references the
 * constant rather than restating it. Change the two together.
 */
export const UNCOVERED_CLAUSES_HINT_COPY =
  'They can also set the direction for clauses this playbook has little or no precedent for.';
