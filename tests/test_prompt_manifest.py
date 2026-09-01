#!/usr/bin/env python3
"""
Red gate for issue #29: Define the per-pass prompt manifest; stop sending
the contract text three times.

This test asserts the following invariants:

  1. ARCHITECTURE.md LLM section must contain a heading or bold label
     "Per-pass prompt manifest" (or equivalent) that defines *exactly*
     which blocks appear in the primary and critic user prompts.

  2. Primary manifest must name all of:
       - standard-form diff (or "diff")
       - anchored clause text (or "anchored clauses")
       - retrieved precedent clauses (or "retrieved precedent")
       - size threshold that gates full-doc inclusion
     Full-doc inclusion must be conditional on a named size threshold,
     not unconditional.

  3. Critic manifest must name:
       - standard-form diff (or "diff")
       - anchored clause text (or "anchored clauses")
       - primary output (or "primary reviewer's output" or "primary's output")
     And must describe the critic's full-document input CONDITIONALLY.
     Issue #29 wrote this check to reject the then-current unconditional
     "the counterparty document"; it asserted the manifest say the critic
     does NOT receive the raw doc at all. Issue #618 reversed that policy
     (#380 retired the standard-form diff, leaving the critic reasoning
     over the primary's JSON alone), so the assertion is now the mirror
     image: the manifest must say the critic receives the document the
     primary read AND must state what happens when a caller composes a
     critic prompt with no document block at all. What #29 was really
     protecting -- that this cell is never left unqualified -- is
     unchanged; only which qualifier is correct has flipped.

  4. ARCHITECTURE.md must state an assembled-size cap (token cap or
     "max_input_tokens" reference) that the manifest assembler enforces
     on every gold case ("assembled input" / "assembled prompt" /
     "assembled size").

  5. docs/design-notes.md must contain a "Why the critic prompt omits the
     raw document" (or equivalent) design rationale — explaining the
     efficacy argument for diff+anchored-clauses over raw-doc for the
     critic.

  6. ARCHITECTURE.md must note that the manifest is a prompt-change
     artifact (manifest changes → release-bundle gated, same as prompt
     changes).

Run with: python3 tests/test_prompt_manifest.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.

GATE_KIND (issue #196): this module is a documentation-lint gate — every
check here is a regex scan over ARCHITECTURE.md/docs/*.md prose asserting
that the manifest is *documented*, not that any prompt-assembly code
exists or behaves this way. A green run does not by itself mean the
described behavior is implemented. See tests/test_docs_gate_labeling.py,
which enforces that this marker exists.
"""

import re
import sys
from pathlib import Path

# Machine-readable marker (issue #196): distinguishes a documentation-lint
# gate (asserts docs SAY something) from a behavioral test (asserts running
# code DOES something). Enforced by tests/test_docs_gate_labeling.py.
GATE_KIND = "documentation-lint"

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DESIGN_NOTES = REPO_ROOT / "docs" / "design-notes.md"
EVALUATION = REPO_ROOT / "docs" / "evaluation.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def llm_section(arch_text: str) -> str:
    """Extract the LLM section from ARCHITECTURE.md."""
    m = re.search(
        r"^### LLM\b.*?\n(.*?)(?=^### |\Z)",
        arch_text,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else ""


# ── Check 1: Per-pass manifest heading/label present ─────────────────────────

def check_manifest_heading_present() -> list[str]:
    """
    ARCHITECTURE.md LLM section must contain a heading or bold label that
    introduces the per-pass prompt manifest.
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = llm_section(arch_text)

    manifest_heading = re.compile(
        r"(####|#####|\*\*)[^\n]*(per.pass.*manifest|prompt manifest|manifest.*per.pass)",
        re.IGNORECASE,
    )
    if not manifest_heading.search(section):
        failures.append(
            "  ARCHITECTURE.md LLM section is missing a 'Per-pass prompt manifest'\n"
            "  heading or bold label. The manifest must be a documented, named\n"
            "  artifact in the LLM section. (issue #29)"
        )
    return failures


# ── Check 2: Primary manifest completeness ───────────────────────────────────

def check_primary_manifest_completeness() -> list[str]:
    """
    The primary manifest entry must name:
      - the standard-form diff
      - anchored clause text
      - retrieved precedent
      - the issue #625 size policy: the full document is ALWAYS included,
        and an oversized one fails closed as `document_too_large` rather
        than being silently reduced
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = llm_section(arch_text)

    # Find the manifest subsection
    m = re.search(
        r"(per.pass.*manifest|prompt manifest).*?(?=####|\Z)",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    manifest_text = m.group(0) if m else section

    required = [
        (
            re.compile(r"standard.form diff|std.form diff|diff.*anchor|anchor.*diff", re.IGNORECASE),
            "standard-form diff",
        ),
        (
            re.compile(r"anchored clause|clause text|anchored.*clauses", re.IGNORECASE),
            "anchored clause text",
        ),
        (
            re.compile(r"retrieved precedent|precedent clause|top.K|retriev", re.IGNORECASE),
            "retrieved precedent clauses",
        ),
        (
            re.compile(r"document_too_large", re.IGNORECASE),
            "the loud oversize outcome (`document_too_large`) that replaced the\n"
            "  size-gated degrade (issue #625)",
        ),
    ]

    for pattern, label in required:
        if not pattern.search(manifest_text):
            failures.append(
                f"  ARCHITECTURE.md per-pass manifest (primary) is missing: {label}\n"
                f"  The primary manifest must name all blocks the model receives,\n"
                f"  and must state what happens to a document that does not fit.\n"
                f"  (issues #29/#625)"
            )

    # Issue #29 required this cell to describe full-doc inclusion as
    # CONDITIONAL on a size threshold; issue #625 (owner decision
    # 2026-08-25) deleted the threshold and the section-outline alternative
    # it gated, so the assertion is now its mirror image -- exactly as check
    # 3's critic cell flipped under #618. What #29 was really protecting --
    # that this cell is never silently ambiguous about whether the model saw
    # the document -- is unchanged; only which answer is correct has
    # flipped. Row-scoped, not a DOTALL search over the whole manifest, so
    # prose from a neighbouring row or from the historical rationale below
    # the table cannot satisfy it while the cell itself says otherwise.
    full_doc_row_primary_cell = re.compile(
        r"^\|\s*Full counterparty document text[^|]*\|([^|]*)\|",
        re.IGNORECASE | re.MULTILINE,
    )
    row = full_doc_row_primary_cell.search(manifest_text)
    if row is None:
        failures.append(
            "  ARCHITECTURE.md per-pass manifest has no 'Full counterparty\n"
            "  document text' row with a Primary pass cell. (issues #29/#625)"
        )
        return failures

    primary_cell = row.group(1)
    if not re.search(r"(?<!not )\balways included\b", primary_cell, re.IGNORECASE):
        failures.append(
            "  ARCHITECTURE.md per-pass manifest (primary), 'Full counterparty\n"
            "  document text' row, must state the document is ALWAYS included.\n"
            "  Issue #625 deleted the size-gated section-outline alternative:\n"
            "  a model must never redline text it did not receive. (issue #625)"
        )
    if not re.search(r"#625", primary_cell):
        failures.append(
            "  ARCHITECTURE.md per-pass manifest (primary), 'Full counterparty\n"
            "  document text' row, must cite the issue that made inclusion\n"
            "  unconditional (#625), so the change is traceable rather than\n"
            "  looking like drift. (issue #625)"
        )
    if re.search(r"section.outline", primary_cell, re.IGNORECASE):
        failures.append(
            "  ARCHITECTURE.md per-pass manifest (primary), 'Full counterparty\n"
            "  document text' row, still offers a section-outline alternative.\n"
            "  That mode is deleted, not deprecated. (issue #625)"
        )

    return failures


# ── Check 3: Critic manifest — diff + anchored + primary output; no raw doc ──

def check_critic_manifest() -> list[str]:
    """
    The critic manifest must name diff, anchored clauses, and the primary
    output.  It must describe the critic's raw/full counterparty document
    input conditionally -- unconditional in EITHER direction is the bug
    (issue #29 caught "always sent"; issue #618 replaced "never sent").
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = llm_section(arch_text)

    m = re.search(
        r"(per.pass.*manifest|prompt manifest).*?(?=####|\Z)",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    manifest_text = m.group(0) if m else section

    # The critic manifest is expressed as a table column ("Critic pass") in the
    # manifest section.  For the first two items (diff and anchored clauses) we
    # search the full manifest_text — extracting a sub-block based on the word
    # "critic" is unreliable because the table interleaves "Primary pass" and
    # "Critic pass" headers and row content.
    required_critic = [
        (
            re.compile(
                r"(?:standard.form diff|std.form diff|diff.*anchor|anchor.*diff)"
                r".*(?:Critic pass|critic.*always|always.*critic|critic.*included)",
                re.IGNORECASE | re.DOTALL,
            ),
            "standard-form diff in Critic pass column",
        ),
        (
            re.compile(
                r"(?:anchored clause|clause text|anchored.*clauses)"
                r".*(?:Critic pass|critic.*always|always.*critic|critic.*included)",
                re.IGNORECASE | re.DOTALL,
            ),
            "anchored clause text in Critic pass column",
        ),
    ]

    for pattern, label in required_critic:
        if not pattern.search(manifest_text):
            failures.append(
                f"  ARCHITECTURE.md per-pass manifest (critic) is missing: {label}\n"
                f"  The critic manifest must name: diff + anchored clauses +\n"
                f"  primary output. (issue #29)"
            )

    # Check 3 — strict row-scoped assertion: the primary-reviewer-output row's
    # Critic column must say "Always included" (or equivalent).  A DOTALL search
    # over the whole manifest_text is insufficient — it can match "Always" from
    # an unrelated row even when the primary-output row's Critic cell is absent
    # or says "OMITTED".  We therefore find the specific table row whose first
    # cell names the primary reviewer's output and verify that the third cell
    # (the Critic column) contains "Always included".
    # Table row format: | Block cell | Primary cell | Critic cell |
    primary_output_row_critic_always = re.compile(
        r"^\|\s*Primary reviewer[^|]*\|[^|]+\|[^|]*Always included[^|]*\|",
        re.IGNORECASE | re.MULTILINE,
    )
    if not primary_output_row_critic_always.search(manifest_text):
        failures.append(
            "  ARCHITECTURE.md per-pass manifest: the primary reviewer's output\n"
            "  row does not state 'Always included' in the Critic pass column.\n"
            "  The Critic column for that row must say the primary output is\n"
            "  always included in the critic pass. (issue #29)"
        )

    # Issue #29 originally required this row to say the critic gets NO raw
    # document; issue #618 reversed the policy, so the assertion is now its
    # mirror image.  It is scoped to the full-document ROW rather than
    # searched DOTALL over the whole manifest for the reason check 3's
    # primary-output assertion is: a loose search matches prose from a
    # neighbouring row (or from the historical rationale below the table,
    # which still recites the retired argument) and passes while the cell
    # itself says the opposite.
    full_doc_row_critic_cell = re.compile(
        r"^\|\s*Full counterparty document text[^|]*\|[^|]+\|([^|]*)\|",
        re.IGNORECASE | re.MULTILINE,
    )
    row = full_doc_row_critic_cell.search(manifest_text)
    if row is None:
        failures.append(
            "  ARCHITECTURE.md per-pass manifest has no 'Full counterparty\n"
            "  document text' row with a Critic pass cell. Whether untrusted\n"
            "  counterparty text reaches the second model is a security-boundary\n"
            "  statement and must be stated in the manifest. (issues #29/#618)"
        )
        return failures

    critic_cell = row.group(1)
    required_in_cell = [
        (
            # Negative lookbehind, so the reverted cell's "**Not included**"
            # cannot satisfy the assertion that the document IS included.
            re.compile(r"(?<!not )\bincluded\b", re.IGNORECASE),
            "that the critic IS given the document the primary read (issue #618\n"
            "  reversed the original omission once issue #380 retired the diff)",
        ),
        (
            re.compile(r"#618"),
            "the issue that reversed the original decision (#618), so the change\n"
            "  is traceable rather than looking like drift",
        ),
        (
            re.compile(r"no document block", re.IGNORECASE),
            "what happens when a caller composes a critic prompt with no\n"
            "  `doc_text`: no document block, and the no-document variant of the\n"
            "  tasking that says so (a prompt constant asserting a block is\n"
            "  present is a promise the assembler owns keeping true)",
        ),
    ]
    for pattern, label in required_in_cell:
        if not pattern.search(critic_cell):
            failures.append(
                f"  ARCHITECTURE.md per-pass manifest (critic), 'Full counterparty\n"
                f"  document text' row, does not state: {label}.\n"
                f"  (issues #29/#618)"
            )

    return failures


# ── Check 4: Assembled-size assertion referenced ─────────────────────────────

def check_assembled_size_assertion() -> list[str]:
    """
    ARCHITECTURE.md must state that the assembled prompt size is asserted
    against a cap (max_input_tokens or equivalent) on every pass/gold case.
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = llm_section(arch_text)

    assembled_size_cap = re.compile(
        r"assembled.*(?:size|token|input).*(?:cap|assert|limit|check|≤|<=|max)|"
        r"(?:cap|assert|limit|check).*assembled.*(?:size|token|input)|"
        r"max_input_tokens.*assembl|assembl.*max_input_tokens",
        re.IGNORECASE,
    )
    if not assembled_size_cap.search(section):
        failures.append(
            "  ARCHITECTURE.md LLM section does not state that assembled prompt\n"
            "  size is asserted against max_input_tokens (or equivalent cap) per\n"
            "  pass. The issue requires this assertion be wired into the manifest\n"
            "  and documented. (issue #29)"
        )

    return failures


# ── Check 5: Design-notes rationale for critic input choice ──────────────────

def check_design_notes_critic_rationale() -> list[str]:
    """
    docs/design-notes.md must contain a rationale section explaining why
    the critic receives diff + anchored clauses + primary output instead of
    the raw document.
    """
    failures = []
    notes_text = read(DESIGN_NOTES)

    critic_rationale_heading = re.compile(
        r"(##|###)[^\n]*(critic.*prompt|prompt.*critic|critic.*input|"
        r"why.*critic.*doc|critic.*raw doc|omit.*raw|raw.*doc.*critic)",
        re.IGNORECASE,
    )
    if not critic_rationale_heading.search(notes_text):
        failures.append(
            "  docs/design-notes.md is missing a rationale section explaining\n"
            "  why the critic receives diff + anchored clauses + primary output\n"
            "  rather than the raw counterparty document. Add a '## Why the\n"
            "  critic prompt omits the raw document' section (or equivalent).\n"
            "  (issue #29)"
        )

    # The section must also mention missed-issue detection or efficacy
    efficacy_mention = re.compile(
        r"missed.issue|issue detection|miss.*detect|detect.*miss|efficacy|"
        r"diff.*context|anchor.*context|reason.*over.*diff|reason.*diff",
        re.IGNORECASE,
    )
    if not efficacy_mention.search(notes_text):
        failures.append(
            "  docs/design-notes.md critic-input rationale does not discuss\n"
            "  missed-issue detection or the efficacy argument. The design note\n"
            "  must explain why diff+anchored context improves critic catch rate\n"
            "  over raw-doc input. (issue #29)"
        )

    return failures


# ── Check 6: Manifest changes are release-bundle gated ───────────────────────

def check_manifest_release_bundle_gated() -> list[str]:
    """
    ARCHITECTURE.md must state that manifest changes are prompt changes
    and therefore release-bundle gated.
    """
    failures = []
    arch_text = read(ARCHITECTURE)
    section = llm_section(arch_text)

    release_bundle_gate = re.compile(
        r"manifest.*(?:prompt change|release.bundle|bundle.gated|bundle gate)|"
        r"(?:prompt change|release.bundle|bundle.gated).*manifest",
        re.IGNORECASE,
    )
    if not release_bundle_gate.search(section):
        failures.append(
            "  ARCHITECTURE.md LLM section does not state that manifest changes\n"
            "  are prompt changes → release-bundle gated. This is required by\n"
            "  issue #29 (Guard criterion). (issue #29)"
        )

    return failures


# ── Check 7: Eval harness critic-input manifest gate (AC3) ───────────────────

def check_eval_critic_input_gate() -> list[str]:
    """
    docs/evaluation.md must contain a critic-input manifest gate section that:
      - names a missed-issue detection comparison baseline (critic with
        diff+anchored+primary output vs. critic with raw doc)
      - records an assembled-size target or token-count comparison for the
        critic pass
      - states that a manifest change altering critic inputs requires a new
        gate run before activation

    This gate exists so the critic-input decision in the per-pass manifest
    rests on eval evidence — not a deferred expectation — and so that a
    future change to critic inputs cannot bypass the quality bar. (issue #29)
    """
    failures = []
    eval_text = read(EVALUATION)

    # Heading for the critic-input gate section
    critic_gate_heading = re.compile(
        r"(##|###)[^\n]*(critic.input.*manifest.*gate|manifest.*gate.*critic|"
        r"critic.*manifest.*gate|critic.*input.*gate)",
        re.IGNORECASE,
    )
    if not critic_gate_heading.search(eval_text):
        failures.append(
            "  docs/evaluation.md is missing a critic-input manifest gate section.\n"
            "  The eval harness must document a comparison baseline for the\n"
            "  critic's input choice (diff+anchored+primary output vs raw doc)\n"
            "  so the decision rests on evidence, not a deferred expectation.\n"
            "  Add a '### Critic-input manifest gate' section (or equivalent).\n"
            "  (issue #29 AC3)"
        )

    # The section must record a concrete missed-issue detection comparison:
    # critic with the new input must achieve parity or better vs raw-doc critic
    missed_issue_comparison = re.compile(
        r"missed.issue.*(?:catch rate|detection|parity|baseline|comparison)|"
        r"(?:catch rate|detection|parity|baseline|comparison).*missed.issue|"
        r"missed hard rejection.*(?:baseline|comparison|parity|0 missed|zero missed)|"
        r"(?:baseline|comparison|parity|0 missed|zero missed).*missed hard rejection",
        re.IGNORECASE,
    )
    if not missed_issue_comparison.search(eval_text):
        failures.append(
            "  docs/evaluation.md critic-input gate does not record a missed-issue\n"
            "  detection comparison baseline. The gate must compare critic-with-\n"
            "  diff+anchored+primary-output vs critic-with-raw-doc on missed hard\n"
            "  rejections, with a recorded target (e.g. '0 missed hard rejections').\n"
            "  (issue #29 AC3)"
        )

    # The section must also record an assembled-size comparison (token counts)
    assembled_size_comparison = re.compile(
        r"assembled.*(?:token|size).*(?:P95|target|≤|<=|estimate|vs|comparison)|"
        r"(?:token|size).*assembled.*(?:P95|target|≤|<=|estimate|vs|comparison)|"
        r"assembled.*critic.*(?:token|size)|(?:token|size).*critic.*assembled",
        re.IGNORECASE,
    )
    if not assembled_size_comparison.search(eval_text):
        failures.append(
            "  docs/evaluation.md critic-input gate does not record assembled-size\n"
            "  comparison evidence. The gate must record the expected assembled\n"
            "  critic-pass token count (target or P95) to confirm the leaner input\n"
            "  is materially smaller than the raw-doc alternative. (issue #29 AC3)"
        )

    # The gate must state that a manifest change to critic inputs requires a
    # new gate run before bundle activation
    manifest_change_requires_gate = re.compile(
        r"manifest change.*(?:critic.*input|require.*gate|block.*activation|"
        r"gate run|bundle.*activation|activation.*gate)|"
        r"(?:critic.*input|require.*gate|block.*activation|"
        r"gate run|bundle.*activation|activation.*gate).*manifest change",
        re.IGNORECASE,
    )
    if not manifest_change_requires_gate.search(eval_text):
        failures.append(
            "  docs/evaluation.md critic-input gate does not state that a manifest\n"
            "  change to critic inputs requires a new gate run before bundle\n"
            "  activation. Without this, the gate is advisory rather than enforced.\n"
            "  (issue #29 AC3)"
        )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        (
            "1",
            "Per-pass manifest heading/label present in ARCHITECTURE.md LLM section",
            check_manifest_heading_present,
        ),
        (
            "2",
            "Primary manifest: diff + anchored clauses + precedents + always-included full-doc",
            check_primary_manifest_completeness,
        ),
        (
            "3",
            "Critic manifest: diff + anchored clauses + primary output; conditional raw doc",
            check_critic_manifest,
        ),
        (
            "4",
            "Assembled-size assertion against max_input_tokens documented",
            check_assembled_size_assertion,
        ),
        (
            "5",
            "design-notes.md: rationale for critic input omitting raw doc",
            check_design_notes_critic_rationale,
        ),
        (
            "6",
            "Manifest changes → release-bundle gated (stated in ARCHITECTURE.md)",
            check_manifest_release_bundle_gated,
        ),
        (
            "7",
            "evaluation.md: critic-input manifest gate with comparison evidence (AC3)",
            check_eval_critic_input_gate,
        ),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All prompt-manifest invariant checks passed.")
        return 0
    else:
        print("One or more prompt-manifest invariant checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
