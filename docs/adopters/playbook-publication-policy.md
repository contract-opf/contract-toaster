# Playbook publication policy

**Destination: `contract-opf/playbooks`.** Drafted here for GC review. Under the
owner decision of 2026-09-08 (D5) this draft is **accepted on creation unless
the General Counsel objects**.

A playbook encodes one organization's negotiating positions. Published, it
invites readers to treat it as advice. This policy exists so that it cannot
reasonably be mistaken for advice, and so that anyone reading a playbook can
tell whose it is, what it was for, and how old it is.

---

## Part 1 — The notice every published playbook carries

> **Not legal advice.** This playbook is a reference artifact describing one
> organization's negotiating positions for one type of agreement. It is not
> legal advice, and it is not a substitute for it.
>
> **No attorney-client relationship.** Publishing, reading, downloading,
> adapting, or executing this playbook creates no attorney-client relationship
> with its author, its originating organization, or any contributor.
>
> **No warranty.** It is provided as-is. No warranty is given that it is
> current, complete, accurate, or suitable for any particular transaction,
> counterparty, industry, or jurisdiction. Law changes; positions that were
> sound when recorded may not be sound now.
>
> **Attorney review is mandatory.** Any output produced by executing this
> playbook is a tool recommendation only and must be reviewed and approved by a
> qualified attorney before it is relied on or sent to a counterparty.
>
> **Modification changes legal effect.** Adapting a playbook — adding,
> removing, or altering positions, thresholds, or rules — can change the legal
> effect of the output it produces. An adaptation is the adapter's
> responsibility alone.
>
> **No endorsement of adaptations.** Neither the original author nor the
> originating organization endorses, reviews, or is responsible for any
> adapted, derived, or downstream version.

## Part 2 — Required metadata

A playbook is not accepted without all of these. They are what make the notice
above meaningful rather than decorative — a disclaimer on an artifact of unknown
authorship, age, and jurisdiction protects nobody.

| Field | Why it is required |
|---|---|
| **Author / originating organization** | Whose positions these are. A playbook with no owner cannot be evaluated. |
| **Intended agreement type** | What it is for. Executing a playbook against the wrong agreement type produces confident nonsense. |
| **Intended user** | Which side of the deal, and in what role. Positions are directional. |
| **Jurisdictional scope** | The jurisdictions the positions were written for, and any they are known not to suit. |
| **Current as of** | The date the positions reflect. |
| **Last reviewed** | The date a human last checked them — distinct from, and usually later than, "current as of". |
| **Version and change history** | What changed between versions and why. A position that moved is the most important thing a reader can know. |
| **Source** | Where the positions came from: authored, derived from a corpus, adapted from another playbook (named). |
| **License** | The license the playbook is offered under. |

An adapted playbook must additionally name the playbook it was adapted from and
state what was changed.

## Part 3 — Licensing, and what it does not do

Contributions are accepted under **CC BY 4.0**.

That choice was deliberate: it is not a share-alike license, so an organization
can adapt a published playbook privately without incurring any obligation to
disclose its own adaptation. Attribution obligations attach on public
redistribution, and are a credit line, not a source-disclosure requirement.

**CC BY 4.0 governs reuse of the material. It says nothing about whether the
material is reliable, and it is not a disclaimer of legal advice.** The two do
different jobs and neither substitutes for the other.

One point of overlap matters in practice: CC BY 4.0's attribution conditions
require a redistributor to retain the notices supplied with the material —
attribution, the license notice, an indication of modification, **and any
disclaimer of warranties**. So the Part 1 notice is not merely conventional; it
travels with the work under the license terms.
See the [CC BY 4.0 legal code](https://creativecommons.org/licenses/by/4.0/legalcode.en),
§3(a)(1).

## Part 4 — Review policy for contributions

1. **Metadata completeness** — every Part 2 field present and plausible.
   Mechanical; can be automated.
2. **Schema validity** — the playbook validates against the published format.
   Mechanical.
3. **Notice present** — the Part 1 notice is carried, unmodified.
4. **No confidential or identifying content** — no counterparty names, no
   executed-agreement text, no material that looks extracted from a specific
   negotiation rather than generalized from one.
5. **Not held out as advice** — the contribution's own framing does not
   contradict Part 1.

Review checks that a contribution is **safe and honestly labelled**, not that
its positions are good ones. Nobody merging a playbook is warranting its
substance, and the review policy should never be described as if they were.

## Part 5 — What is not accepted

- A playbook derived from agreements the contributor cannot lawfully publish
  positions from.
- A playbook that identifies counterparties, directly or by folder, filename,
  or example text.
- A playbook presented as generally applicable legal guidance rather than as
  one organization's positions.
