# ADR 0001: The critic has the last word — validated later-in-time authority

## Status

Accepted (owner decision 2026-09-16)

## Context

The two-pass review runs a primary pass and then a critic pass, merged by deterministic code (`scripts/reconciliation.py`). Under that merge the critic could veto the verdict (any critic-added issue forces REQUEST_CHANGE, and hard rejections are monotonic) but could never touch the words: the redline is compiled only from the primary's block patches, a contested replacement left the primary's text in the document, and the critic's objection and suggested alternative were recorded in `critic_delta` and, until #96, dropped before persistence. The model policy was coupled to that split: Opus as primary, Sonnet as the cheaper flagging critic.

The result was half-baked. The pass with the most information (the document, the playbook, and the primary's full analysis) had no authority over the output it was best placed to improve, and the attorney received the primary's text with objections stapled on.

## Decision

The critic pass produces the final review. It is constructed as the senior reviewer: it receives the document, the playbook, and the primary's complete output, is prompted to read the primary's work with a sceptical eye, and emits the final issues and the final block-anchored replacement text. Its authority is **validated later-in-time**: it is later, it has strictly more information, and the model policy now requires it to be at least as capable as the primary.

Reconciliation stays deterministic and keeps three guards:

1. **Hard rejections and detector fires are monotonic.** A hard rejection raised by the primary or by a deterministic detector that the critic dropped is re-appended and forces REQUEST_CHANGE. The critic may add rejections; it may not remove one.
2. **Every override is recorded.** For each issue where the critic changed the primary's replacement text, rationale or decision, `critic_delta` records the primary's version, the critic's version and the critic's stated reason. Nothing the primary said is lost from the audit record.
3. **The critic's text passes every gate the primary's did.** Block-transcript proof against the document bytes, pen rules, the leakage scan on the external channel, and the OOXML round trip run over the critic's output. A critic failure fails the review; the primary's output never ships alone.

The Models admin screen names the roles as "Reviewer" (initial review of the contract against the playbook) and "Critic" (senior reviewer: reads the contract, the playbook and the reviewer's proposed changes, and has the last word), and warns when the critic's selected tier is below the reviewer's. The default policy places the stronger model on the critic.

## Consequences

Easier: one coherent voice in the redline; the critic's reading of the primary's analysis becomes an advantage rather than a sidecar; cost is flat (still two calls); the receipt and internal footnotes (#96, #132) show the attorney every override with both versions.

Harder: the critic must author block-anchored edits, so its schema and prompt grow and its output is gated like the primary's; a critic shown the primary's answer may anchor on it, which the sceptical-review prompt and the eval gate must watch; there is no pass after the critic, so a critic error ships unless a detector or gate catches it; the token reservation formula must assume a critic output as large as the primary's.

Rejected alternative: a third call returning the critic's objections to the primary for adoption or rejection. It keeps the accountable author in place but costs a third call and invites self-serving rejection of the critic's points.

## Date

2026-09-16

## Amendment 2026-09-17 (owner-accepted external review)

Three additions to the decision, from the standalone reviewer's design the owner accepted:

1. **Hard-rejection recall is an invariant outside both models.** A hard-rejection ledger is initialised from the agreement type's hard-rule manifest before either pass runs; the critic's authority over analysis and drafting never extends to suppressing a ledger entry. An entry is cleared only by a validated contract span plus a deterministic predicate; an unproven entry fails closed **into the document** as the manifest's fallback language (tracked insertion + footnote), the review lands DONE / REQUEST_CHANGE — never a manual-review status (#133). Ticket: #147, manifest from contract-opf/playbook-engine#228.
2. **The critic disposes of everything explicitly.** Every reviewer issue and every ledger entry gets a disposition (`KEEP` / `REVISE` / `DROP` with reason; `CLEARED` with span / `ASSERT`); a missing disposition is a schema failure. Edits are addressed by block id + original-text hash (the existing block transcript), never by character offset.
3. **Byte-identical shared prefix and fixed-size critic context.** Both passes share one tool schema, the digest, the manifest and the contract before any role text, so the digest caches across reviews and the contract within one; the critic then receives deterministically selected per-clause dossiers (≤1k tokens each, ≤24) instead of a runtime lookup tool. Tickets: #148 (prefix), #40 re-pointed to dossier selection, #149 (ZDR caching policy, owner).
