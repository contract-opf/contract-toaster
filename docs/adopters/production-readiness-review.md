# Production readiness review — a template for adopters

**Status: advisory.** This is a model workflow, not a gate on this repository.
Nothing here is assured by the code in this repo, and no part of it is a
warranty. Whether this sequence is right for your deployment, your playbook,
and your risk posture is your decision.

Run this before a deployment of this tool informs real legal work. It is a
**verification sweep**, not construction: every control the documentation
claims is demonstrated working in the environment that will actually carry the
risk. A control that is documented but not demonstrated is a claim, not a
control.

## How to use it

Copy this file into your own repository or runbook, fill in the owner and date
columns, and keep the completed copy. The value is in the evidence you record,
not in the ticks.

Each row asks for **who verified it, when, and what they saw** — a command and
its output, a screenshot, a log line. "Reviewed" is not evidence.

## 1 · Data residency and storage

| Check | Evidence to record |
|---|---|
| Document bytes never leave the intended region | Bucket and table region config, plus the request path for an upload |
| Encryption at rest is on, with the key you expect | Key ARN or equivalent, read from the live resource |
| Only pointers, never document bytes, cross the pipeline boundary | A real execution's payloads |
| Backups exist and a restore has actually been performed | The restore, with a timestamp |

## 2 · Audit integrity

| Check | Evidence to record |
|---|---|
| The audit log is append-only in practice, not only by intent | A deliberate update and delete attempt, both denied |
| Every review produces an audit trail that identifies actor, time, and artifact versions | One review's rows |
| Audit retention matches what your policy claims | Configured lifetime |

## 3 · Access control

| Check | Evidence to record |
|---|---|
| Authentication fails closed when the identity provider is unreachable | Observed behaviour with the provider blocked |
| A user outside the permitted directory or domain is refused | A real attempt |
| A non-admin cannot reach any admin route | One attempt per admin surface |
| A departed user loses access within the window your policy states | Measured, not assumed |

## 4 · Output safety

| Check | Evidence to record |
|---|---|
| The leakage scan blocks a planted violation before output is returned | The planted case and the block |
| Output carries no clause text, server exception, or document excerpt it should not | Inspection of a real result |
| Every output is framed as a tool recommendation, never a legal verdict | The rendered result and the downloaded file |
| Legal hold prevents deletion of a held document | A deletion attempt against a held item |

## 5 · Cost and abuse limits

| Check | Evidence to record |
|---|---|
| The spend ceiling actually stops spending | A run against a lowered cap |
| Rate limits do not block a legitimate slow review | A long review polled to completion |
| An oversized or malformed upload is rejected cleanly | One of each |

## 6 · Operations

| Check | Evidence to record |
|---|---|
| Alarms reach a real address that a named person reads | A test alert, and who received it |
| The escalation contact answers | A test page, with response time |
| Rollback has been rehearsed, not just documented | The rehearsal |
| The runbook was followed by someone who did not write it | Their notes, including what was unclear |

## 7 · The honest summary

Write two paragraphs before you sign off:

1. **What is verified.** Only what you demonstrated.
2. **What is not, and what you are accepting.** Every deferred item, named,
   with who accepted the risk.

A readiness review whose summary says everything passed is usually a review
that did not look hard enough.

## Related

- [Pilot and validation guide](pilot-and-validation-guide.md) — the parallel-run
  sequence this readiness review precedes.
