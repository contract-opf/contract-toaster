"""
Test helper (issue #138, ADR 0001): the critic pass's FINAL result for a
first-reviewer response it stands behind in full.

Under ADR 0001 the critic's response is the final review -- `reconciliation
.reconcile` takes its `issues`, `decision`, `confidence_state`,
`verdict_summary` and `block_patches`/`block_ops`, and forwards nothing of the
first reviewer's but the hard rejections the critic dropped. So a critic that
agrees with the first reviewer cannot answer "KEEP" over an empty issues list
any more: the prompt (`primary_review_pass._CRITIC_TASKING_FINAL_RESULT_V3`)
tells it to "write out each edit you stand behind yourself, including one you
agree with", and that restated result is what this builds -- the first
reviewer's issues and transcript, verbatim, plus one KEEP disposition per
first-reviewer issue and no overrides (`_CRITIC_TASKING_NO_MINIMUM`'s
"keep its issues and edits, give each a KEEP disposition, and report no
overrides").

PRODUCTION REACHES THIS SHAPE: it is a schema-valid v3 critic response that
`critic_review_pass.run_critic_pass` accepts (dispositions complete, the
transcript proves against the same block map the primary's did, since it IS
the primary's), which is how every test using it drives it -- through the
real pass, never handed to `reconcile` around it.

Not a test file: imported by the tests that drive a full review.
"""

from __future__ import annotations

import copy
import json
from typing import Any

KEEP_REASON = "Same issue and the same edit; compliant with the playbook position."

# A strict-mode provider emits every property of an object it emits at all
# (`model_output_schema._force_all_properties_required_in_place`), so the
# three deprecated CriticDelta arrays ride along empty.
_DEPRECATED_EMPTY = {"added_issues": [], "contested_replacements": [], "rationale_objections": []}


def critic_keeps(
    primary_response: str | dict[str, Any],
    *,
    schema_enforced: bool = False,
    verdict_summary: str | None = None,
) -> str:
    """The critic's final result restating `primary_response` (a JSON string
    or dict) with a KEEP disposition for each of its issues.

    `schema_enforced` mirrors a capability-True provider: the deprecated
    CriticDelta arrays are present (empty) and an ACCEPT still carries a
    `critic_delta` object. `verdict_summary`, when given, replaces the first
    reviewer's (the critic writes its own; leaving it None keeps the first
    reviewer's words, which a critic that agrees may well repeat).
    """
    body = (
        json.loads(primary_response)
        if isinstance(primary_response, str)
        else copy.deepcopy(primary_response)
    )
    dispositions = [
        {"issue_id": issue["issue_key"], "disposition": "KEEP", "reason": KEEP_REASON}
        for issue in body.get("issues") or []
        if isinstance(issue, dict) and issue.get("issue_key")
    ]
    if dispositions or schema_enforced:
        delta: dict[str, Any] | None = {"dispositions": dispositions, "overrides": []}
        if schema_enforced:
            delta.update(copy.deepcopy(_DEPRECATED_EMPTY))
    else:
        delta = None
    body["critic_delta"] = delta
    if verdict_summary is not None:
        body["verdict_summary"] = verdict_summary
    return json.dumps(body)
