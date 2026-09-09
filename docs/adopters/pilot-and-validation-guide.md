# Pilot and validation guide — a template for adopters

**Status: advisory.** These criteria are **recommended, not assured** by the
code in this repository. Nothing here is a warranty that a deployment meeting
them is safe, correct, or fit for your purpose. Whether to run this sequence
before trusting the tool with real work — and whether these are the right
criteria for your playbook and your risk — is your decision, and your counsel's.

This describes a parallel run: real inbound agreements go through the tool **and**
through your normal review, and the two are compared. It exists because a
contract-review tool that has not been measured against known answers on your
own paper is not a tool, it is a liability.

## Shape of the pilot

- **10–15 real inbound agreements**, across at least three counterparty types,
  including at least one where you supplied the paper.
- Every agreement is reviewed **normally as well**. The pilot never replaces the
  human review it is being measured against.
- For each: what the tool caught, what it missed that the human caught, what it
  raised that the human did not, and the reviewing attorney's disposition.
- **Agree the exit criteria before the pilot starts.** Criteria settled after
  the results are in are not criteria.

## Exit criteria

### Safety — any failure ends the pilot

- **Zero false ACCEPTs in a predefined "must escalate" class.** Define that
  class first. It should include every mandatory position in your playbook, and
  every issue your counsel classifies as capable of causing material legal,
  financial, confidentiality, compliance, or operational harm.
- A false ACCEPT in that class is an **automatic pilot failure**. Correct the
  defect and rerun the affected part of the pilot. It is not traded off against
  good results elsewhere.

### Output integrity — any failure ends the pilot

- **Zero REQUEST_CHANGE results that contain neither an actionable edit nor an
  explicit "redline could not be safely generated" hold.** A change request that
  requests nothing is not an output.
- **Zero unexplained deletion-only changes.** Every intentional deletion must be
  classified as an approved delete operation and tied to a playbook position.
  A counterparty who receives struck text with no replacement has nothing to
  accept.
- **Every stated reason traces back** to specific agreement text and the
  applicable playbook rule.

### Quality — nonfatal individually, fatal as a pattern

- Use a **count-based ceiling**, not only percentages: in a sample of 10–15,
  a percentage hides more than it shows.
- Starting proposal: **no more than one material false-positive finding per
  agreement**, and no recurring systematic false-positive pattern.
- Lower-severity misses may be nonfatal on their own. **Any repeated miss
  pattern blocks exit** until it is corrected and covered by a regression test.

### Repeatability

The same agreement, run again, should reach the same place.

- Rerun at least **three representative pilot agreements several times each**.
- Require **consistent disposition of material issues**, and prohibit empty or
  structurally unusable redlines.
- Exact wording need not match. **Legal effect and the required negotiating
  positions should remain materially consistent.**

> **Sequencing note.** Upstream work to constrain redline shape and variance is
> a post-launch refinement in this project, not a launch gate. Apply the
> repeatability criteria once those safeguards exist in the version you are
> running; before then, measure and record the variance rather than gating on it.

### Efficiency — a business criterion, not a safety one

- Measure **active attorney time** and **total turnaround time** separately.
  They move independently and conflating them hides which one improved.
- A time-saving target is a **business-success criterion and never a substitute
  for the safety criteria above**. A faster wrong answer is worse than a slow
  right one.
- Onboarding distorts early measurements. Evaluate the target primarily against
  the **latter half** of the pilot.

## Recording the decision

Whoever owns legal risk records the go/no-go against the criteria as written,
naming any criterion waived and who accepted that risk. Misses found during the
pilot are the most valuable output: they become regression cases, subject to
whatever de-identification standard you hold yourself to.

## Related

- [Production readiness review](production-readiness-review.md) — the control
  verification that should precede this pilot.
