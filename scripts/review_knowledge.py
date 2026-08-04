#!/usr/bin/env python3
"""What a review KNOWS, resolved once, in one object, with one place to refuse.

`scripts/opf_prompt.py` is pure composition: give it a document and it renders
blocks. It deliberately makes no governance call -- it will happily compose a
playbook with no negotiation intent in it, because deciding whether that is an
acceptable review is not a renderer's job. This module is where that decision
lives, and it is the ONLY place it lives.

## Why one home for refusals

The failure this repo keeps re-discovering is not "a check was missing". It is
"a check passed while the property was broken". A review that runs on a playbook
with `posture == {}` and `floor == {}` produces a lineage record that is
indistinguishable, field for field, from one governed by a real playbook: same
hash, same id, same "playbook governed this review" claim. Nothing is *wrong* in
the record. The record simply does not describe what happened.

So every refusal below is a refusal to produce that record. And every ESCAPE
from a refusal WRITES A POSITIVE RECORD (`lineage_record()`): an operator may
decide the policy carries the intent, but that decision is then a fact in the
lineage, not an absence of one. A refusal suppressed silently is a defect --
it recreates the exact shape the refusal exists to prevent.

## The unifying rule

A review must carry SOME governed, hashed, human-approved content: in
playbook mode, that is `posture.system_prompt` OR a real digest of corpus
precedent (`digest.clauses`) OR the review policy's rules -- any one of them
is enough. In policy-only mode there is no digest at all, so the policy's
rules are the ONLY candidate; when it has none, there is nothing
prescriptive at all, and no flag can make that a review -- that refusal has
no remedy parameter.

Standing instructions (issue #482/#483) are composed into the SAME Guidance
slot as the policy's `should`-rules once one of the three candidates above
has already satisfied this rule -- see `resolve_knowledge`'s
`instructions_text` param below. They are NOT a fourth candidate this rule
checks: `resolve_knowledge` never inspects `instructions_text` when deciding
whether to refuse, so a bundle with no posture, no digest and no policy
rules still refuses even when standing instructions are present.

An empty `posture` in playbook mode is NOT "nothing prescriptive" by
itself: the digest is real, hashed, corpus-derived precedent (issue #479
DECISION, 2026-08-04 -- "an empty-posture OPF artifact is a VALID artifact
and must run"; the real and public OPF playbooks ship `posture: {}` on
purpose). So an empty posture with no digest at all is caught by
`opf_prompt.compose_opf_system_blocks`'s own no-digest refusal
(`PromptCompositionError`) further down this function, never by this
module -- and an empty posture WITH a digest is remediable via
`accept_empty_posture`: the real playbook plus its digest is emphatically
not intent-free, so requiring the flag (never inferring it) keeps the
decision on the record without training operators to wave a reflexive flag
through a fail-closed that trips on day one.

## Hashing what was SENT

`content_hash()` hashes the COMPOSED BLOCKS, not an input view. This is a
deliberate upgrade over `primary_review_pass.projected_playbook_hash`, which
hashes the projection's SOURCE -- i.e. what the caller MEANT to send. Every
defect that motivated this module (an empty posture block, a
non-negotiable-invariants intro over an empty list) is invisible to a hash of
that shape: the input view is unchanged while the model's actual prompt is a
lie. Hash the rendering and the gap closes by construction.

Voicing: this module is white-labelled -- refer to the operator as "you"/"your",
never by tenant name.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_prompt  # noqa: E402

#: The pre-OPF path: `primary_review_pass.assemble_system_blocks` projects a v1
#: playbook dict straight to JSON. It is not composed here and does not resolve
#: through this seam; its retirement is a later slice. Named so the mode
#: vocabulary (and any lineage recording one) is complete rather than implying
#: that a v1 review has no mode at all.
MODE_V1_PROJECTION = "v1_projection"

#: An OPF 0.3 playbook: Posture + Binding + Digest (+ Guidance/Context).
MODE_PLAYBOOK_DIGEST = opf_prompt.MODE_PLAYBOOK_DIGEST

#: Policy rules alone, NO digest block. A legitimate review -- of a document
#: type with no compiled corpus behind it -- but only when a human says so.
MODE_POLICY_ONLY = opf_prompt.MODE_POLICY_ONLY

_RESOLVABLE_MODES = (MODE_PLAYBOOK_DIGEST, MODE_POLICY_ONLY)


class KnowledgeRefusal(ValueError):
    """Raised when the inputs cannot honestly produce a governed review.

    Distinct from `opf_prompt.PromptCompositionError`, which means "these blocks
    cannot be RENDERED" (no digest). This means "these blocks would render, and
    the review they describe would be a claim we cannot stand behind".
    """


@dataclass(frozen=True)
class ReviewKnowledge:
    """Everything one review knows, already resolved and already composed.

    Frozen: resolution is a governance decision, so the answer is a value, not a
    mutable builder something downstream can top up after the refusals have run.
    """

    mode: str
    opf_doc: Optional[dict]
    overrides: Optional[dict]
    policy: Optional[dict]
    #: Composed once, by `resolve_knowledge`, so a refusal cannot be dodged by
    #: constructing this object and composing later.
    blocks: tuple[str, ...] = ()
    posture_source: str = "playbook"
    accepted_empty_posture: bool = False
    accepted_stub_basis: bool = False

    def system_blocks(self) -> list[dict[str, Any]]:
        """Anthropic-message-API-shaped content blocks.

        The cache breakpoint sits on the LAST block: every block here is static
        knowledge (a deterministic function of the bundle and the policy), so
        the last one maximises the cached prefix. Mirrors the convention of
        `primary_review_pass.assemble_system_blocks` -- `cache_control` as a
        structural property a caller/test can assert, not prose to parse.
        """
        out: list[dict[str, Any]] = [{"type": "text", "text": text} for text in self.blocks]
        if out:
            out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    def content_hash(self) -> str:
        """`sha256:<hex>` over the COMPOSED BLOCKS -- what was SENT.

        Deliberately not a hash of an input view; see the module docstring.
        `cache_control` is excluded: it is a transport hint, not knowledge, and
        a review whose prompt text is identical knew identical things.
        """
        canonical = json.dumps(list(self.blocks), sort_keys=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def must_rules(self) -> list[dict]:
        """The policy `must` rules, for the closing self-check.

        Only rules that actually REACHED the prompt: `policy_rules_by_strength`
        drops (and records) a textless rule, and the self-check must not
        re-read the model against a rule the model was never given.
        """
        return opf_prompt.policy_rules_by_strength(self.policy, "must")

    def lineage_record(self) -> dict[str, Any]:
        """The POSITIVE, honest record of what governed this review.

        Positive: it states what the review HAD, rather than leaving an
        operator to infer it from what is missing. `playbook_evidence: "none"`
        is a claim; a lineage with no evidence field is an ambiguity.
        """
        return {
            "review_knowledge_mode": self.mode,
            "playbook_evidence": "none" if self.mode == MODE_POLICY_ONLY else "digest",
            "posture_source": self.posture_source,
            "accepted_empty_posture": self.accepted_empty_posture,
            "accepted_stub_basis": self.accepted_stub_basis,
            "knowledge_content_hash": self.content_hash(),
            "block_count": len(self.blocks),
            "policy_id": (self.policy or {}).get("playbook_id"),
            "policy_version": (self.policy or {}).get("version"),
            "must_rule_ids": [r.get("id") for r in self.must_rules()],
        }


def _posture_prose(opf_doc: dict, overrides: Optional[dict]) -> tuple[Optional[str], str]:
    """The posture prose that will actually reach the model, and its source."""
    override_posture = (overrides or {}).get("posture") or {}
    if override_posture.get("system_prompt"):
        return str(override_posture["system_prompt"]), "override"
    prose = (opf_doc.get("posture") or {}).get("system_prompt")
    return (str(prose), "playbook") if prose else (None, "playbook")


def _policy_rule_count(policy: Optional[dict]) -> int:
    rules = (policy or {}).get("rules")
    return len(rules) if isinstance(rules, list) else 0


def resolve_knowledge(
    *,
    bundle_v2: dict,
    policy: Optional[dict],
    declared_mode: str,
    accept_stub_basis: bool = False,
    accept_empty_posture: bool = False,
    instructions_text: str = "",
) -> ReviewKnowledge:
    """Resolve one review's knowledge, or refuse.

    `declared_mode` is required and has no default: a mode is DECLARED by a
    caller, never inferred from what happens to be missing. Inferring
    policy-only from an absent digest is exactly how a misconfigured playbook
    becomes a silently corpus-less review that still records a governing
    playbook.

    `instructions_text` (issue #479/#482/#483, default `""`): the playbook's
    resolved standing instructions, already resolved once by the caller
    (`backend/src/reviews.py`) and threaded read-only through
    `scripts/review_spine.py::run_review`. Composed into the SAME position
    `opf_prompt.compose_opf_system_blocks` composes the policy's `should`-rule
    Guidance block -- see that function's own docstring. Included in
    `content_hash()`/`lineage_record()` for free, since both are functions of
    `self.blocks`, which now includes it.

    Raises `KnowledgeRefusal` per the module docstring, or
    `opf_prompt.PromptCompositionError` when a declared playbook-mode document
    has no digest to render.
    """
    if declared_mode == MODE_V1_PROJECTION:
        raise KnowledgeRefusal(
            "declared_mode 'v1_projection' does not resolve through this seam: the v1 path "
            "composes via primary_review_pass.assemble_system_blocks. Its retirement is a "
            "later slice; until then, call that path directly rather than declaring it here."
        )
    if declared_mode not in _RESOLVABLE_MODES:
        raise KnowledgeRefusal(
            f"declared_mode {declared_mode!r} is not one of {list(_RESOLVABLE_MODES)!r}. The mode "
            "is a positive declaration by the caller and is never inferred from the data."
        )

    opf_doc = bundle_v2.get("opf") or {}
    overrides = bundle_v2.get("overrides")

    # --- Stub basis ------------------------------------------------------
    # The engine watermarks quick-compile output that is "structurally valid but
    # semantically blank" with this flag. It is NOT a corpus-less escape hatch:
    # a doc that genuinely has no corpus is caught by the digest check below,
    # flag or no flag, and setting stub_basis_present=False does not buy a
    # corpus-less document a pass.
    compiler = opf_doc.get("compiler") or {}
    accepted_stub_basis = False
    if compiler.get("stub_basis_present") is True:
        if not accept_stub_basis:
            raise KnowledgeRefusal(
                "opf.compiler.stub_basis_present is True: this playbook is a quick-compile "
                "watermarked by the engine as structurally valid but SEMANTICALLY BLANK. Its "
                "digest will render, and the review will record it as a governing playbook, "
                "having learned nothing from it. Pass accept_stub_basis=True to proceed on the "
                "record, or compile the playbook against its corpus."
            )
        accepted_stub_basis = True

    # --- Prescriptive intent ---------------------------------------------
    posture_source = "policy"
    accepted_empty_posture = False
    rule_count = _policy_rule_count(policy)

    if declared_mode == MODE_POLICY_ONLY:
        # The policy's rules ARE this mode's prescriptive intent, so there is
        # nothing to fall back to when there are none.
        if rule_count == 0:
            raise KnowledgeRefusal(
                "declared_mode 'policy_only' with no policy rules: this review would carry no "
                "prescriptive intent and no corpus evidence -- it would be a review in name and "
                "a bare model call in fact."
            )
    else:
        # Issue #479 DECISION (2026-08-04): an empty posture is NOT "nothing
        # prescriptive from any governed artifact" by itself -- the digest is
        # real, hashed, corpus-derived precedent, and the real/public OPF
        # playbooks ship `posture: {}` on purpose. A document with no digest
        # AT ALL (the genuinely corpus-less case) is caught below by
        # `opf_prompt.compose_opf_system_blocks`'s own `PromptCompositionError`,
        # never here -- this module no longer special-cases "posture empty AND
        # rule_count == 0" as an unconditional, no-remedy refusal (that used to
        # be this branch's `elif rule_count == 0` case; folding it into the
        # remediable case below is this ticket's fix for "no redline, ever"
        # against a shipped, published playbook whose only sin is an empty
        # posture).
        prose, source = _posture_prose(opf_doc, overrides)
        if prose:
            posture_source = source
        elif not accept_empty_posture:
            raise KnowledgeRefusal(
                f"this playbook's posture is empty; the review's prescriptive intent would come "
                f"from the review policy ({rule_count} rule(s)), any standing instructions "
                f"supplied, and/or the playbook's own digest of governed precedent. That may well "
                f"be correct -- the real, published OPF playbooks ship posture: {{}} on purpose -- "
                f"but it is an operator's call to make on the record: pass "
                f"accept_empty_posture=True and lineage will record where the review's intent "
                f"came from."
            )
        else:
            accepted_empty_posture = True
            # Positive record of where the review's intent came from: the
            # approved policy's rules when it has any, otherwise the
            # playbook's own digest (never a bare "we accepted an escape" with
            # no further detail).
            posture_source = "policy" if rule_count > 0 else "playbook"

    blocks = opf_prompt.compose_opf_system_blocks(
        opf_doc, overrides, policy=policy, mode=declared_mode, instructions_text=instructions_text
    )

    return ReviewKnowledge(
        mode=declared_mode,
        opf_doc=opf_doc,
        overrides=overrides,
        policy=policy,
        blocks=tuple(blocks),
        posture_source=posture_source,
        accepted_empty_posture=accepted_empty_posture,
        accepted_stub_basis=accepted_stub_basis,
    )
