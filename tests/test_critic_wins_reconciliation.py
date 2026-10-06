#!/usr/bin/env python3
"""
Issue #138 (ADR 0001, docs/adr/0001-critic-has-the-last-word.md, including its
2026-09-17 Amendment): the critic's result is the final review.

## What was wrong

`reconciliation.reconcile()` kept the PRIMARY's issues and forwarded the
PRIMARY's `block_patches`/`block_ops` to the redline stage. The critic could
add issues and force `REQUEST_CHANGE`, but a critic that rewrote an edit
changed nothing in the document: the redline carried the primary's words and
the critic's were recorded beside them. Under ADR 0001 the critic -- later,
with strictly more information -- has the last word.

## The table this file drives

  1. Primary says A, critic says B for the same block: the delivered redline
     carries B (and not A), and `critic_delta.overrides` carries A, B and the
     critic's reason -- both the critic's own record and the computed diff.
  2. Primary raises hard rejection H, critic omits it: H is in the final
     issues with provenance `primary-retained`, decision REQUEST_CHANGE, and
     none of the primary's edits ships with it.
  3. Detector fire D absent from both passes: appended, REQUEST_CHANGE
     (unchanged behaviour).
  4. The critic pass fails: the review's status is the critic failure reason
     and no redline is produced. Also when the critic's result leaves a
     primary issue with no disposition (a schema failure that fails closed),
     and when there is no critic result at all.

Plus the ticket's notes and the Amendment's rules:

  5. The critic's text goes through the leakage gate: a planted playbook gram
     in a CRITIC replacement is blocked, with the primary's text clean.
  6. Overlapping replacement spans are rejected, not merged: the primary's
     and the critic's transcripts are never spliced, and a critic transcript
     with two entries for one block fails the review.
  7. The new override shape survives `pipeline_runner._ANALYSIS_FIELDS`
     (issue #96's carry-through), which `get_review_detail` serves verbatim.

Fix round 1 (the reviewer's probes, each now a row):

  8. Guard 1 is per ISSUE: the primary raises one hard rule twice, the critic
     keeps one instance and drops the other, and the dropped one is retained.
  9. A KEEP or REVISE whose issue the critic's own `issues` do not carry fails
     closed as `critic_schema_invalid` -- at the merger, and through the real
     pass (which spends its informed retry on it first) -- instead of
     completing as an ACCEPT with the issue silently gone.
 10. Every text the audit record forwards to the review's owner is scanned:
     a planted gram in a disposition reason, in a critic override, in a
     dropped primary issue and in a replaced primary insert all fail the
     review as `leakage_detected`, and none reaches the analysis artifact. The
     document's own words that an edit removes are NOT scanned (they are the
     counterparty's text, not model prose), and a critic cannot use that
     exemption to hide prose.
 11. Hard-rejection ids resolve from an OPF review's Floor invariants too: a
     real OPF review whose critic drops the primary's Floor-invariant issue
     keeps it as `primary-retained`.

Fix round 2 (the reviewer's remaining finding):

 12. An explicit DROP beats the pairing heuristic. The critic DROPs the
     primary's hard rejection at sec-8 and raises the same rule at sec-12:
     sec-8 is recorded `dropped` and retained, the critic's sec-12 is `added`
     / `critic-added` -- never a `replaced` pair that loses sec-8. Variant:
     the primary raises the rule at sec-8 and sec-12, the critic DROPs both
     and raises it at "Section 12": both primary instances are retained once
     each and the critic's issue is added once. At `reconcile()` and end to
     end.

## Fixture fidelity

Every end-to-end case drives `review_spine.run_review` with the shipped
`FakeBedrockClient` over the real synthetic-generic bundle, the real output
schema and a real `.docx`, so every critic response passes the REAL
`critic_review_pass.run_critic_pass` (schema validation, the disposition
check, the block-transcript pre-check) exactly as a model's would. Block ids
are derived from the document by the production extractor, never typed in.
The hard-rejection id is the playbook's own `hard_rejections[].id`, resolved
by `reconciliation.hard_rejection_rule_ids` exactly as the spine resolves it.

The table cases that call `reconcile()` directly hand it critic results of the
shape `run_critic_pass` returns OK. The exceptions are case 4's
missing-disposition result and case 9's KEEP over an absent issue: the pass
itself rejects both shapes before they can reach `reconcile()`, so in
production only the merger's own refusal backs it up -- which is why each is
asserted at that seam, labelled as such, and case 9 also through the real
pass. Case 11 resolves its ids with the spine's own resolver
(`opf_prompt.resolve_floor_invariants`) over a shipped OPF fixture and drives
the real `run_review`, Floor judge included.

Run standalone: `.venv/bin/python tests/test_critic_wins_reconciliation.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

os.environ.setdefault("REVIEWS_TABLE", "reviews-test")
os.environ.setdefault("UPLOADS_BUCKET", "uploads-test")
os.environ.setdefault("OUTPUTS_BUCKET", "outputs-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "playbooks-test")

import leakage_scan as ls  # noqa: E402
import model_client  # noqa: E402
import opf_prompt  # noqa: E402
import pipeline_runner as pr  # noqa: E402
import reconciliation as recon  # noqa: E402
import review_spine  # noqa: E402
import synthetic_form_paragraphs as sfp_module  # noqa: E402

# Cross-test-file import of the docx-fixture harness (the established
# convention -- tests/test_critic_failure_reason_665.py does the same).
from test_review_spine import (  # noqa: E402
    _SEC8_DRAFT_TEXT,
    _SEC8_ISSUE,
    _build_draft_docx,
    _load_bundle,
    block_id_for_text,
)

REVIEW_ID = "00000000-0000-4000-a000-000000000138"

# Two replacement wordings that share no substring of note, so "the redline
# carries B and not A" cannot be satisfied by accident.
TEXT_A = "Liability of each party is capped at one hundred fifty thousand dollars."
TEXT_B = "Aggregate liability of either party is limited to $150,000 per contract year."
CRITIC_REASON = "The cap must be stated per contract year, as the playbook position requires."

# The synthetic-generic playbook's own hard-rejection rule (its `id`), and its
# confidential description -- the text the leakage gate must never let out.
HARD_RULE_ID = "preserve-liability-cap"


def _hard_rule_description() -> str:
    for rule in _load_bundle().get("hard_rejections") or []:
        if rule.get("id") == HARD_RULE_ID:
            return rule["description"]
    raise AssertionError(f"setup: the synthetic-generic playbook lost rule {HARD_RULE_ID!r}")


# ---------------------------------------------------------------------------
# Response builders (wire-shaped: what a model returns).
# ---------------------------------------------------------------------------


def _sec8_issue(key: str = "I1", **overrides: Any) -> dict[str, Any]:
    issue = dict(_SEC8_ISSUE)
    issue["issue_key"] = key
    issue["replacement_scope_note"] = (
        "The clause states the opposite position outright; a local repair "
        "would leave a sentence that reads as an unlimited-liability term."
    )
    issue.update(overrides)
    return issue


def _replace_sec8(block_id: str, text: str, key: str = "I1") -> dict[str, Any]:
    return {
        "block_id": block_id,
        "segments": [
            {"op": "delete", "text": _SEC8_DRAFT_TEXT, "issue_key": key},
            {"op": "insert", "text": text, "issue_key": key},
        ],
    }


def _response(
    issues: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    *,
    critic_delta: dict[str, Any] | None = None,
    summary: str | None = "One issue identified in Section 8 requiring attention.",
) -> str:
    return json.dumps(
        {
            "decision": "REQUEST_CHANGE" if issues else "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": issues,
            "block_patches": patches,
            "block_ops": [],
            "critic_delta": critic_delta,
            "verdict_summary": summary,
        }
    )


def _delta(dispositions: list[tuple[str, str]], overrides: list[dict] | None = None) -> dict:
    return {
        "dispositions": [
            {"issue_id": key, "disposition": disposition, "reason": "Reviewed."}
            for key, disposition in dispositions
        ],
        "overrides": overrides or [],
    }


def _draft() -> tuple[bytes, str]:
    docx_bytes = _build_draft_docx(sfp_module, {"sec-8": _SEC8_DRAFT_TEXT})
    return docx_bytes, block_id_for_text(docx_bytes, _SEC8_DRAFT_TEXT)


def _run(docx_bytes: bytes, primary: str, critic: list[str]) -> dict[str, Any]:
    bundle = _load_bundle()
    metadata = bundle["playbook"]["metadata"]
    client = model_client.FakeBedrockClient(
        {
            metadata["primary_model_id"]: [primary],
            metadata["critic_model_id"]: critic,
        }
    )
    return review_spine.run_review(docx_bytes, bundle, client, review_id=REVIEW_ID)


def _document_xml(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _overrides(result: dict[str, Any], source: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in ((result.get("critic_delta") or {}).get("overrides") or [])
        if entry.get("source") == source
    ]


# ---------------------------------------------------------------------------
# 1. A vs B: the redline carries the critic's words; the override carries both.
# ---------------------------------------------------------------------------


def _a_versus_b_run() -> dict[str, Any]:
    docx_bytes, block_id = _draft()
    primary = _response([_sec8_issue()], [_replace_sec8(block_id, TEXT_A)])
    critic = _response(
        [_sec8_issue()],
        [_replace_sec8(block_id, TEXT_B)],
        critic_delta=_delta(
            [("I1", "REVISE")],
            [
                {
                    "issue_id": "I1",
                    "field": "replacement_text",
                    "primary_value": TEXT_A,
                    "critic_value": TEXT_B,
                    "reason": CRITIC_REASON,
                }
            ],
        ),
    )
    return _run(docx_bytes, primary, [critic])


def test_the_redline_carries_the_critics_text(failures: list[str]) -> None:
    result = _a_versus_b_run()
    if result.get("status") != "OK" or result.get("decision") != "REQUEST_CHANGE":
        failures.append(
            f"[1a] setup: expected a completed REQUEST_CHANGE; got status="
            f"{result.get('status')!r} reason={result.get('reason')!r}"
        )
        return
    redline = result.get("redline_bytes")
    if not redline:
        failures.append("[1b] no redline was delivered")
        return
    xml = _document_xml(redline)
    if TEXT_B not in xml:
        failures.append("[1c] the delivered redline does not carry the CRITIC's text (B)")
    if TEXT_A in xml:
        failures.append("[1d] the delivered redline still carries the PRIMARY's text (A)")
    findings = result.get("findings") or []
    if [f.get("proposed_replacement_text") for f in findings] != [TEXT_B]:
        failures.append(
            "[1e] the finding's derived replacement text must be the critic's: "
            f"{[f.get('proposed_replacement_text') for f in findings]!r}"
        )


def test_the_override_carries_both_versions_and_the_reason(failures: list[str]) -> None:
    result = _a_versus_b_run()
    stated = _overrides(result, recon.OVERRIDE_SOURCE_CRITIC)
    if not any(
        o.get("primary_value") == TEXT_A
        and o.get("critic_value") == TEXT_B
        and o.get("reason") == CRITIC_REASON
        for o in stated
    ):
        failures.append(f"[1f] the critic's own override (A, B, reason) is missing: {stated!r}")

    computed = _overrides(result, recon.OVERRIDE_SOURCE_COMPUTED)
    replaced = [o for o in computed if o.get("change") == recon.CHANGE_REPLACED]
    if len(replaced) != 1:
        failures.append(f"[1g] expected one computed 'replaced' override; got {computed!r}")
    else:
        entry = replaced[0]
        if TEXT_A not in entry.get("primary_value", "") or TEXT_B not in entry.get(
            "critic_value", ""
        ):
            failures.append(f"[1h] the computed override must carry both versions: {entry!r}")
        if entry.get("issue_id") != "I1" or not entry.get("reason"):
            failures.append(f"[1i] the computed override must name I1 and a reason: {entry!r}")


# ---------------------------------------------------------------------------
# 2. A hard rejection the critic dropped is retained, flag-only.
# ---------------------------------------------------------------------------


def _hard_issue(key: str = "I1") -> dict[str, Any]:
    return _sec8_issue(key, playbook_topic_id=HARD_RULE_ID)


def test_a_dropped_hard_rejection_is_retained_in_reconcile(failures: list[str]) -> None:
    hard_ids = recon.hard_rejection_rule_ids(_load_bundle())
    if HARD_RULE_ID not in hard_ids:
        failures.append(f"[2a] setup: {HARD_RULE_ID!r} is not resolved as a hard rule id")
        return
    primary = json.loads(_response([_hard_issue()], [_replace_sec8("p0001", TEXT_A)]))
    other = _sec8_issue("I1", playbook_topic_id="assignment", section_ref="sec-9")
    critic = json.loads(
        _response(
            [other],
            [_replace_sec8("p0001", TEXT_B)],
            critic_delta=_delta([("I1", "DROP")]),
        )
    )
    result = recon.reconcile(
        primary_result=primary, critic_result=critic, hard_rejection_rule_ids=hard_ids
    )
    retained = [
        i for i in result["issues"] if i.get("provenance") == recon.PROVENANCE_PRIMARY_RETAINED
    ]
    if len(retained) != 1 or retained[0].get("playbook_topic_id") != HARD_RULE_ID:
        failures.append(f"[2b] H must be retained as primary-retained: {result['issues']!r}")
    elif retained[0].get("issue_key") == "I1":
        failures.append("[2c] H kept a key the critic's own issue (and transcript) uses")
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[2d] a retained hard rejection forces REQUEST_CHANGE; got {result['decision']!r}")
    if result.get("block_patches") != critic["block_patches"]:
        failures.append(
            "[2e] the forwarded transcript must be the critic's alone -- H's primary "
            f"edit is not merged into it: {result.get('block_patches')!r}"
        )
    dropped = [
        o
        for o in _overrides(result, recon.OVERRIDE_SOURCE_COMPUTED)
        if o.get("change") == recon.CHANGE_DROPPED
    ]
    if len(dropped) != 1 or not dropped[0].get("retained"):
        failures.append(f"[2f] the drop must be recorded, marked retained: {dropped!r}")
    elif TEXT_A not in dropped[0].get("primary_edits", ""):
        failures.append(f"[2g] the dropped record must keep the primary's edit: {dropped[0]!r}")

    # A NON-hard issue the critic drops is gone -- the critic decides.
    soft = json.loads(_response([_sec8_issue()], []))
    accept = json.loads(_response([], [], critic_delta=_delta([("I1", "DROP")])))
    softened = recon.reconcile(
        primary_result=soft, critic_result=accept, hard_rejection_rule_ids=hard_ids
    )
    if softened["issues"] or softened["decision"] != "ACCEPT":
        failures.append(
            f"[2h] a dropped non-hard issue must not survive the critic: {softened!r}"
        )


def test_a_dropped_hard_rejection_is_retained_end_to_end(failures: list[str]) -> None:
    docx_bytes, block_id = _draft()
    primary = _response([_hard_issue()], [_replace_sec8(block_id, TEXT_A)])
    critic = _response(
        [], [], critic_delta=_delta([("I1", "DROP")]), summary="No changes identified."
    )
    result = _run(docx_bytes, primary, [critic])
    if result.get("decision") != "REQUEST_CHANGE":
        failures.append(
            f"[2i] a critic ACCEPT must not remove a hard rejection; got decision="
            f"{result.get('decision')!r} status={result.get('status')!r} "
            f"reason={result.get('reason')!r}"
        )
    findings = result.get("findings") or []
    if [
        (f.get("playbook_topic_id"), f.get("provenance")) for f in findings
    ] != [(HARD_RULE_ID, recon.PROVENANCE_PRIMARY_RETAINED)]:
        failures.append(f"[2j] H must be the one finding, primary-retained: {findings!r}")
    redline = result.get("redline_bytes")
    if redline and TEXT_A in _document_xml(redline):
        failures.append("[2k] the primary's edit shipped on its own")


# ---------------------------------------------------------------------------
# 3. A detector fire absent from both passes is appended (unchanged).
# ---------------------------------------------------------------------------


def _fire() -> dict[str, Any]:
    return {
        "section_ref": "no-uncapped-liability",
        "section_title": "Floor invariant",
        "counterparty_change_summary": "A Floor invariant was judged violated.",
        "decision": "REQUEST_CHANGE",
        "external_rationale_for_footnote": "This agreement must satisfy the Floor invariant.",
        "proposed_replacement_text": "",
        "playbook_topic_id": "no-uncapped-liability",
        "internal_precedent_citation": None,
        "provenance": "floor:no-uncapped-liability",
    }


def test_a_detector_fire_absent_from_both_is_appended(failures: list[str]) -> None:
    accept = json.loads(_response([], []))
    result = recon.reconcile(primary_result=accept, critic_result=accept, detector_fires=[_fire()])
    if [i.get("provenance") for i in result["issues"]] != ["floor:no-uncapped-liability"]:
        failures.append(f"[3a] the fire must be the one final issue: {result['issues']!r}")
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[3b] a fire forces REQUEST_CHANGE; got {result['decision']!r}")
    if not result["issues"] or not result["issues"][0].get("issue_key"):
        failures.append("[3c] the fire must be given a response-unique issue_key")


# ---------------------------------------------------------------------------
# 4. A critic failure fails the review; the primary never ships alone.
# ---------------------------------------------------------------------------


def test_a_failed_critic_fails_the_review(failures: list[str]) -> None:
    docx_bytes, block_id = _draft()
    primary = _response([_sec8_issue()], [_replace_sec8(block_id, TEXT_A)])
    invalid = json.loads(_response([], []))
    invalid["confidence_state"] = "medium"  # the real-model enum miss (#673)
    result = _run(docx_bytes, primary, [json.dumps(invalid)] * 6)
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[4a] a failed critic must fail the review; got {result.get('status')!r}")
    if result.get("reason") != review_spine.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(f"[4b] the status must carry the critic's reason; got {result.get('reason')!r}")
    if result.get("redline_bytes") is not None or result.get("findings"):
        failures.append("[4c] no redline and no findings when the critic fails")


def test_a_missing_disposition_fails_closed_at_the_merger(failures: list[str]) -> None:
    """Not reachable through `run_critic_pass` (its own disposition check
    rejects this shape first); asserted at the merger's seam because the
    merger must not trust that the pass ran it."""
    primary = json.loads(_response([_sec8_issue()], []))
    critic = json.loads(_response([], [], critic_delta=_delta([])))
    composed = recon.run_two_pass_review(
        primary_pass_result={"status": "OK", "response": primary, "attempts": 1},
        critic_pass_result={"status": "OK", "response": critic, "attempts": 1},
    )
    if composed.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED" or "result" in composed:
        failures.append(f"[4d] an undisposed primary issue must fail closed: {composed!r}")
    if review_spine.critic_failure_reason(composed) != review_spine.REASON_CRITIC_SCHEMA_INVALID:
        failures.append(
            f"[4e] the failure must read as critic_schema_invalid; got "
            f"{review_spine.critic_failure_reason(composed)!r}"
        )
    if "I1" not in str(composed.get("last_error")):
        failures.append(f"[4f] the error must name the undisposed key: {composed!r}")

    try:
        recon.reconcile(primary_result=primary, critic_result=None)
        failures.append("[4g] reconcile() must refuse to reconcile the primary alone")
    except ValueError:
        pass
    missing = recon.run_two_pass_review(
        primary_pass_result={"status": "OK", "response": primary, "attempts": 1},
        critic_pass_result=None,
    )
    if missing.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED" or "result" in missing:
        failures.append(f"[4h] no critic result must fail the review: {missing!r}")


# ---------------------------------------------------------------------------
# 5. The critic's text is what the leakage gate scans.
# ---------------------------------------------------------------------------


def test_a_planted_gram_in_a_critic_replacement_is_blocked(failures: list[str]) -> None:
    docx_bytes, block_id = _draft()
    leaked = _hard_rule_description()
    primary = _response([_sec8_issue()], [_replace_sec8(block_id, TEXT_A)])
    critic = _response(
        [_sec8_issue()],
        [_replace_sec8(block_id, leaked)],
        critic_delta=_delta([("I1", "REVISE")]),
    )
    result = _run(docx_bytes, primary, [critic])
    if result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED" or result.get("reason") != "leakage_detected":
        failures.append(
            "[5a] a playbook gram in the CRITIC's replacement must be blocked; got "
            f"status={result.get('status')!r} reason={result.get('reason')!r}"
        )
    if result.get("redline_bytes") is not None or result.get("critic_delta"):
        failures.append("[5b] a leakage block surfaces no redline and no critic_delta")


# ---------------------------------------------------------------------------
# 6. Overlapping spans are rejected, not merged.
# ---------------------------------------------------------------------------


def test_overlapping_spans_are_rejected_not_merged(failures: list[str]) -> None:
    primary = json.loads(_response([_sec8_issue()], [_replace_sec8("p0001", TEXT_A)]))
    critic = json.loads(
        _response(
            [_sec8_issue()],
            [_replace_sec8("p0001", TEXT_B)],
            critic_delta=_delta([("I1", "REVISE")]),
        )
    )
    result = recon.reconcile(primary_result=primary, critic_result=critic)
    if result.get("block_patches") != critic["block_patches"]:
        failures.append(
            f"[6a] two transcripts on one block must never be spliced: {result.get('block_patches')!r}"
        )

    # A critic transcript with two entries for one block is refused by the
    # proof the critic's text goes through; the review fails, nothing ships.
    docx_bytes, block_id = _draft()
    overlapping = _response(
        [_sec8_issue()],
        [_replace_sec8(block_id, TEXT_B), _replace_sec8(block_id, TEXT_A)],
        critic_delta=_delta([("I1", "REVISE")]),
    )
    primary_wire = _response([_sec8_issue()], [_replace_sec8(block_id, TEXT_A)])
    run = _run(docx_bytes, primary_wire, [overlapping] * 6)
    if run.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED" or run.get("redline_bytes") is not None:
        failures.append(
            "[6b] an overlapping critic transcript must fail the review, not merge; got "
            f"status={run.get('status')!r} reason={run.get('reason')!r}"
        )
    if not str(run.get("reason") or "").startswith("critic_"):
        failures.append(f"[6c] the failure must be a critic_* reason; got {run.get('reason')!r}")


# ---------------------------------------------------------------------------
# 7. The new override shape survives the analysis allowlist (#96).
# ---------------------------------------------------------------------------


def test_the_override_shape_reaches_the_analysis_artifact(failures: list[str]) -> None:
    result = _a_versus_b_run()
    document = {field: result.get(field) for field in pr._ANALYSIS_FIELDS}
    delta = json.loads(json.dumps(document.get("critic_delta"), default=str)) or {}
    sources = {entry.get("source") for entry in delta.get("overrides") or []}
    if sources != {recon.OVERRIDE_SOURCE_CRITIC, recon.OVERRIDE_SOURCE_COMPUTED}:
        failures.append(
            f"[7a] both override sources must reach the analysis artifact; got {sources!r}"
        )
    if [d.get("disposition") for d in delta.get("dispositions") or []] != ["REVISE"]:
        failures.append(f"[7b] the dispositions must reach the artifact: {delta!r}")


# ---------------------------------------------------------------------------
# 8. Guard 1 is per issue: a second instance of one hard rule is not covered
#    by the critic keeping the first.
# ---------------------------------------------------------------------------


def test_each_instance_of_a_hard_rule_is_retained(failures: list[str]) -> None:
    hard_ids = recon.hard_rejection_rule_ids(_load_bundle())
    primary = json.loads(
        _response(
            [
                _sec8_issue("I1", playbook_topic_id=HARD_RULE_ID, section_ref="sec-8"),
                _sec8_issue("I2", playbook_topic_id=HARD_RULE_ID, section_ref="sec-12"),
            ],
            [],
        )
    )
    critic = json.loads(
        _response(
            [_sec8_issue("I1", playbook_topic_id=HARD_RULE_ID, section_ref="sec-8")],
            [],
            critic_delta=_delta([("I1", "KEEP"), ("I2", "DROP")]),
        )
    )
    result = recon.reconcile(
        primary_result=primary, critic_result=critic, hard_rejection_rule_ids=hard_ids
    )
    shape = [(i.get("section_ref"), i.get("provenance")) for i in result["issues"]]
    if shape != [
        ("sec-8", recon.PROVENANCE_MODEL),
        ("sec-12", recon.PROVENANCE_PRIMARY_RETAINED),
    ]:
        failures.append(
            "[8a] the critic kept one instance of a hard rule and dropped the other; the "
            f"dropped one must be retained: {shape!r}"
        )
    keys = [i.get("issue_key") for i in result["issues"]]
    if len(keys) != len(set(keys)):
        failures.append(f"[8b] the final issues repeat an issue_key: {keys!r}")
    if result["decision"] != "REQUEST_CHANGE":
        failures.append(f"[8c] a retained hard rejection forces REQUEST_CHANGE; got {result['decision']!r}")
    dropped = [
        (o.get("issue_id"), o.get("retained"))
        for o in _overrides(result, recon.OVERRIDE_SOURCE_COMPUTED)
        if o.get("change") == recon.CHANGE_DROPPED
    ]
    if dropped != [("I2", True)]:
        failures.append(f"[8d] the drop of I2 must be recorded, marked retained: {dropped!r}")


# ---------------------------------------------------------------------------
# 9. A KEEP / REVISE whose issue the critic's own list does not carry.
# ---------------------------------------------------------------------------


def test_a_keep_whose_issue_is_absent_fails_closed_at_the_merger(failures: list[str]) -> None:
    """The pass rejects this shape first (see the end-to-end row below);
    asserted at the merger's seam because the merger must not trust that the
    pass ran its check."""
    primary = json.loads(_response([_sec8_issue()], []))
    for disposition in ("KEEP", "REVISE"):
        critic = json.loads(_response([], [], critic_delta=_delta([("I1", disposition)])))
        composed = recon.run_two_pass_review(
            primary_pass_result={"status": "OK", "response": primary, "attempts": 1},
            critic_pass_result={"status": "OK", "response": critic, "attempts": 1},
        )
        if composed.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED" or "result" in composed:
            failures.append(
                f"[9a] a {disposition} over an absent issue must fail closed: {composed!r}"
            )
        if review_spine.critic_failure_reason(composed) != review_spine.REASON_CRITIC_SCHEMA_INVALID:
            failures.append(
                f"[9b] a {disposition} over an absent issue must read as critic_schema_invalid; "
                f"got {review_spine.critic_failure_reason(composed)!r}"
            )
        if "I1" not in str(composed.get("last_error")):
            failures.append(f"[9c] the error must name the kept key: {composed!r}")

    # Not over-eager: a KEEP whose issue the critic restated under a re-worded
    # section reference still pairs (on the topic) and is carried.
    reworded = json.loads(
        _response(
            [_sec8_issue(section_ref="Section 8")], [], critic_delta=_delta([("I1", "KEEP")])
        )
    )
    try:
        kept = recon.reconcile(primary_result=primary, critic_result=reworded)
    except recon.ReconciliationRejected as exc:
        failures.append(f"[9d] a KEEP the critic's issues DO carry was refused: {exc.last_error}")
    else:
        if [i.get("provenance") for i in kept["issues"]] != [recon.PROVENANCE_MODEL]:
            failures.append(f"[9e] the kept issue must be the final one: {kept['issues']!r}")


def test_a_keep_whose_issue_is_absent_never_ships_end_to_end(failures: list[str]) -> None:
    docx_bytes, block_id = _draft()
    primary = _response([_sec8_issue()], [_replace_sec8(block_id, TEXT_A)])
    keep_over_nothing = _response(
        [], [], critic_delta=_delta([("I1", "KEEP")]), summary="No changes identified."
    )
    result = _run(docx_bytes, primary, [keep_over_nothing] * 6)
    if (
        result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED"
        or result.get("reason") != review_spine.REASON_CRITIC_SCHEMA_INVALID
    ):
        failures.append(
            "[9f] a critic that KEEPs an issue it does not carry must fail the review as "
            f"critic_schema_invalid; got status={result.get('status')!r} "
            f"decision={result.get('decision')!r} reason={result.get('reason')!r}"
        )
    if result.get("findings") or result.get("redline_bytes") is not None:
        failures.append("[9g] no findings and no redline when the critic's result is refused")

    # The pass spends its informed retry on it: a critic that restates the
    # kept issue on the retry completes the review with that issue.
    restated = _response(
        [_sec8_issue()],
        [_replace_sec8(block_id, TEXT_A)],
        critic_delta=_delta([("I1", "KEEP")]),
    )
    recovered = _run(docx_bytes, primary, [keep_over_nothing, restated])
    if recovered.get("status") != "OK" or [
        f.get("provenance") for f in recovered.get("findings") or []
    ] != [recon.PROVENANCE_MODEL]:
        failures.append(
            "[9h] the critic's corrected retry must complete the review with the kept issue; "
            f"got status={recovered.get('status')!r} reason={recovered.get('reason')!r}"
        )


# ---------------------------------------------------------------------------
# 10. Every text the audit record forwards to the owner is scanned.
# ---------------------------------------------------------------------------


def _analysis_text(result: dict[str, Any]) -> str:
    """The analysis artifact `pipeline_runner` would persist for `result`,
    which `get_review_detail` serves to the review's owner."""
    return json.dumps({field: result.get(field) for field in pr._ANALYSIS_FIELDS}, default=str)


def _replace_text(block_id: str, removed: str, added: str, key: str = "I1") -> dict[str, Any]:
    return {
        "block_id": block_id,
        "segments": [
            {"op": "delete", "text": removed, "issue_key": key},
            {"op": "insert", "text": added, "issue_key": key},
        ],
    }


def _revise(override: dict[str, Any] | None = None, *, reason: str = "Reviewed.") -> dict[str, Any]:
    return {
        "dispositions": [{"issue_id": "I1", "disposition": "REVISE", "reason": reason}],
        "overrides": [{"issue_id": "I1", **override}] if override else [],
    }


#: The new scanned names, each declared on the external channel.
_AUDIT_RECORD_FIELDS = (
    "critic_delta.dispositions.reason",
    "critic_delta.overrides.reason",
    "critic_delta.overrides.primary_value",
    "critic_delta.overrides.critic_value",
    "critic_delta.overrides.primary_edits",
    "critic_delta.overrides.critic_edits",
)


def test_every_audit_record_text_is_scanned(failures: list[str]) -> None:
    leaked = _hard_rule_description()
    corpus = ls.ConfidentialCorpus.from_playbook(_load_bundle())
    hard_ids = recon.hard_rejection_rule_ids(_load_bundle())
    sec8 = [_sec8_issue()]
    drop = _delta([("I1", "DROP")])

    def _override(**values: str) -> dict[str, Any]:
        entry = {"field": "rationale", "primary_value": "A.", "critic_value": "B.", "reason": "C."}
        entry.update(values)
        return entry

    cases = [
        (
            "a disposition reason",
            _response(sec8, []),
            _response(sec8, [], critic_delta=_revise(reason=leaked)),
            "critic_delta.dispositions.reason",
        ),
        (
            "a critic override's reason",
            _response(sec8, []),
            _response(sec8, [], critic_delta=_revise(_override(reason=leaked))),
            "critic_delta.overrides.reason",
        ),
        (
            "a critic override's primary_value",
            _response(sec8, []),
            _response(sec8, [], critic_delta=_revise(_override(primary_value=leaked))),
            "critic_delta.overrides.primary_value",
        ),
        (
            "a critic override's critic_value (replacement-text class)",
            _response(sec8, []),
            _response(
                sec8,
                [],
                critic_delta=_revise(_override(field="replacement_text", critic_value=leaked)),
            ),
            "critic_delta.overrides.critic_value",
        ),
        (
            "a critic override dressed as a removed-text line",
            _response(sec8, []),
            _response(sec8, [], critic_delta=_revise(_override(critic_value=f"- {leaked}"))),
            "critic_delta.overrides.critic_value",
        ),
        (
            "a dropped primary issue's rationale",
            _response([_sec8_issue(external_rationale_for_footnote=leaked)], []),
            _response([], [], critic_delta=drop),
            "critic_delta.overrides.primary_issue.external_rationale_for_footnote",
        ),
        (
            "a dropped primary issue's inserted text",
            _response(sec8, [_replace_sec8("p0001", leaked)]),
            _response([], [], critic_delta=drop),
            "critic_delta.overrides.primary_edits",
        ),
        (
            "a primary insert the critic replaced",
            _response(sec8, [_replace_sec8("p0001", leaked)]),
            _response(sec8, [_replace_sec8("p0001", TEXT_B)], critic_delta=_revise()),
            "critic_delta.overrides.primary_value",
        ),
    ]
    for label, primary, critic, field_name in cases:
        result = recon.reconcile(
            primary_result=json.loads(primary),
            critic_result=json.loads(critic),
            hard_rejection_rule_ids=hard_ids,
        )
        outcome = ls.scan_model_output(result, corpus)
        if not outcome.blocked or outcome.field_name != field_name:
            failures.append(
                f"[10a] a playbook gram in {label} must block as {field_name!r}; got "
                f"blocked={outcome.blocked} field={outcome.field_name!r}"
            )

    for field_name in _AUDIT_RECORD_FIELDS:
        if ls._FIELD_CHANNELS.get(field_name) != ls.CHANNEL_EXTERNAL:
            failures.append(f"[10b] {field_name!r} must be declared on the external channel")

    # Precision: the words an edit REMOVES are the document's own (the block
    # transcript proves them against its bytes), not model prose -- a
    # counterparty draft that happens to contain a gram does not fail the
    # review through the rendered record any more than through the delete
    # segment itself.
    removed = f"{_SEC8_DRAFT_TEXT} {leaked}"
    clean = recon.reconcile(
        primary_result=json.loads(
            _response(sec8, [_replace_text("p0001", removed, TEXT_A)])
        ),
        critic_result=json.loads(
            _response(sec8, [_replace_text("p0001", removed, TEXT_B)], critic_delta=_revise())
        ),
    )
    outcome = ls.scan_model_output(clean, corpus)
    if outcome.blocked:
        failures.append(
            f"[10c] the document's own removed text must not be scanned; blocked on {outcome.field_name!r}"
        )
    elif leaked not in json.dumps(clean.get("critic_delta")):
        failures.append("[10d] setup: the removed text should be in the computed record")

    # ...and that exemption is the reconciler's rendered record only, read by
    # its prefix: any line of a computed record that is not a removed line is
    # scanned, whatever it looks like.
    outcome = ls.scan_model_output(
        {
            "critic_delta": {
                "overrides": [
                    {"source": recon.OVERRIDE_SOURCE_COMPUTED, "primary_edits": leaked}
                ]
            }
        },
        corpus,
    )
    if not outcome.blocked:
        failures.append("[10e] an unprefixed line in a computed record must be scanned")


def test_a_planted_audit_record_gram_never_reaches_the_analysis_artifact(
    failures: list[str],
) -> None:
    leaked = _hard_rule_description()
    docx_bytes, block_id = _draft()
    sec8 = [_sec8_issue()]
    cases = [
        (
            "a disposition reason",
            _response(sec8, [_replace_sec8(block_id, TEXT_A)]),
            _response(sec8, [_replace_sec8(block_id, TEXT_B)], critic_delta=_revise(reason=leaked)),
        ),
        (
            "a critic override's reason",
            _response(sec8, [_replace_sec8(block_id, TEXT_A)]),
            _response(
                sec8,
                [_replace_sec8(block_id, TEXT_B)],
                critic_delta=_revise(
                    {
                        "field": "replacement_text",
                        "primary_value": TEXT_A,
                        "critic_value": TEXT_B,
                        "reason": leaked,
                    }
                ),
            ),
        ),
        (
            "a dropped primary issue's rationale",
            _response(
                [_sec8_issue(external_rationale_for_footnote=leaked)],
                [_replace_sec8(block_id, TEXT_A)],
            ),
            _response(
                [], [], critic_delta=_delta([("I1", "DROP")]), summary="No changes identified."
            ),
        ),
        (
            "a primary insert the critic replaced",
            _response(sec8, [_replace_sec8(block_id, leaked)]),
            _response(sec8, [_replace_sec8(block_id, TEXT_B)], critic_delta=_revise()),
        ),
    ]
    for label, primary, critic in cases:
        result = _run(docx_bytes, primary, [critic])
        if (
            result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED"
            or result.get("reason") != "leakage_detected"
        ):
            failures.append(
                f"[10f] a playbook gram in {label} must fail the review as leakage_detected; got "
                f"status={result.get('status')!r} reason={result.get('reason')!r}"
            )
        if leaked in _analysis_text(result):
            failures.append(f"[10g] a playbook gram in {label} reached the analysis artifact")

    # Precision, end to end: a counterparty draft that itself contains the
    # gram, removed by both passes' edits, still completes with the critic's
    # words in the redline.
    removed = f"{_SEC8_DRAFT_TEXT} {leaked}"
    own_words_docx = _build_draft_docx(sfp_module, {"sec-8": removed})
    own_block = block_id_for_text(own_words_docx, removed)
    completed = _run(
        own_words_docx,
        _response(sec8, [_replace_text(own_block, removed, TEXT_A)]),
        [_response(sec8, [_replace_text(own_block, removed, TEXT_B)], critic_delta=_revise())],
    )
    if completed.get("status") != "OK" or not completed.get("redline_bytes"):
        failures.append(
            "[10h] the document's own words in an edit must not fail the review; got "
            f"status={completed.get('status')!r} reason={completed.get('reason')!r}"
        )
    elif TEXT_B not in _document_xml(completed["redline_bytes"]):
        failures.append("[10i] the completed redline must carry the critic's text")


# ---------------------------------------------------------------------------
# 11. Hard-rejection ids resolved from an OPF review's Floor invariants.
# ---------------------------------------------------------------------------

_OPF_FIXTURE = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"


def test_a_dropped_floor_invariant_issue_is_retained_in_an_opf_review(
    failures: list[str],
) -> None:
    opf_doc = json.loads(_OPF_FIXTURE.read_text(encoding="utf-8"))
    metadata = _load_bundle()["playbook"]["metadata"]
    bundle = {
        "opf_bundle_v2": {"opf": opf_doc, "overrides": None},
        "playbook": {"metadata": dict(metadata)},
    }
    # The spine's own resolution: `resolve_floor_invariants` over the OPF
    # document, then `hard_rejection_rule_ids(bundle, floor_invariants)`. An
    # OPF bundle carries no v1 `hard_rejections`, so the id can only come from
    # the Floor.
    floor_invariants = opf_prompt.resolve_floor_invariants(opf_doc, None)
    if not floor_invariants:
        failures.append("[11a] setup: the OPF fixture lost its Floor invariant")
        return
    invariant_id = floor_invariants[0]["id"]
    if invariant_id not in recon.hard_rejection_rule_ids(bundle, floor_invariants):
        failures.append(f"[11b] {invariant_id!r} must resolve as a hard-rejection id from the Floor")
    if recon.hard_rejection_rule_ids(bundle):
        failures.append("[11c] setup: an OPF bundle must supply no v1 hard_rejections ids")

    docx_bytes, _block_id = _draft()
    primary = _response([_sec8_issue(playbook_topic_id=invariant_id)], [])
    critic = _response(
        [], [], critic_delta=_delta([("I1", "DROP")]), summary="No changes identified."
    )
    floor_verdict = json.dumps(
        {"invariant_id": invariant_id, "violated": False, "evidence_quote": ""}
    )
    client = model_client.FakeBedrockClient(
        {
            metadata["primary_model_id"]: [primary, floor_verdict],
            metadata["critic_model_id"]: [critic],
        }
    )
    result = review_spine.run_review(docx_bytes, bundle, client, review_id=REVIEW_ID)
    if result.get("status") != "OK" or result.get("decision") != "REQUEST_CHANGE":
        failures.append(
            "[11d] a critic ACCEPT must not remove the primary's Floor-invariant issue; got "
            f"status={result.get('status')!r} decision={result.get('decision')!r} "
            f"reason={result.get('reason')!r}"
        )
    findings = [
        (f.get("playbook_topic_id"), f.get("provenance")) for f in result.get("findings") or []
    ]
    if findings != [(invariant_id, recon.PROVENANCE_PRIMARY_RETAINED)]:
        failures.append(f"[11e] the Floor issue must be the one finding, primary-retained: {findings!r}")


# ---------------------------------------------------------------------------
# 12. An explicit DROP beats the pairing heuristic: a primary issue the critic
#     DROPped never pairs with a critic issue on the same rule elsewhere.
# ---------------------------------------------------------------------------


_Shape = list[tuple[str, str]]
_Dropped = list[tuple[str, bool]]


def _drop_vs_pairing_cases(block_id: str) -> list[tuple[str, str, str, _Shape, _Dropped, int]]:
    """(label, primary wire, critic wire, expected final (section_ref,
    provenance) list, expected computed `dropped` (issue_id, retained) list,
    expected computed `added` count)."""
    hard = HARD_RULE_ID
    return [
        (
            "the critic DROPs sec-8 and raises the same hard rule at sec-12",
            _response(
                [_sec8_issue("I1", playbook_topic_id=hard, section_ref="sec-8")],
                [_replace_sec8(block_id, TEXT_A)],
            ),
            _response(
                [_sec8_issue("I1", playbook_topic_id=hard, section_ref="sec-12")],
                [],
                critic_delta=_delta([("I1", "DROP")]),
            ),
            [
                ("sec-12", recon.PROVENANCE_CRITIC_ADDED),
                ("sec-8", recon.PROVENANCE_PRIMARY_RETAINED),
            ],
            [("I1", True)],
            1,
        ),
        (
            "the critic DROPs both instances and raises the rule at 'Section 12'",
            _response(
                [
                    _sec8_issue("I1", playbook_topic_id=hard, section_ref="sec-8"),
                    _sec8_issue("I2", playbook_topic_id=hard, section_ref="sec-12"),
                ],
                [_replace_sec8(block_id, TEXT_A)],
            ),
            _response(
                [_sec8_issue("I1", playbook_topic_id=hard, section_ref="Section 12")],
                [],
                critic_delta=_delta([("I1", "DROP"), ("I2", "DROP")]),
            ),
            [
                ("Section 12", recon.PROVENANCE_CRITIC_ADDED),
                ("sec-8", recon.PROVENANCE_PRIMARY_RETAINED),
                ("sec-12", recon.PROVENANCE_PRIMARY_RETAINED),
            ],
            [("I1", True), ("I2", True)],
            1,
        ),
    ]


def _check_drop_vs_pairing(
    row: str,
    label: str,
    issues: list[dict[str, Any]],
    critic_delta: dict[str, Any] | None,
    expected_shape: _Shape,
    expected_dropped: _Dropped,
    expected_added: int,
    failures: list[str],
) -> None:
    shape = [(i.get("section_ref"), i.get("provenance")) for i in issues]
    if shape != expected_shape:
        failures.append(f"[{row}] {label}: final issues {shape!r}, expected {expected_shape!r}")
    keys = [i.get("issue_key") for i in issues]
    if len(keys) != len(set(keys)):
        failures.append(f"[{row}] {label}: the final issues repeat an issue_key: {keys!r}")
    computed = _overrides({"critic_delta": critic_delta}, recon.OVERRIDE_SOURCE_COMPUTED)
    dropped = [
        (o.get("issue_id"), o.get("retained"))
        for o in computed
        if o.get("change") == recon.CHANGE_DROPPED
    ]
    if dropped != expected_dropped:
        failures.append(
            f"[{row}] {label}: each DROPped hard rejection must be recorded dropped and "
            f"retained: {dropped!r}, expected {expected_dropped!r}"
        )
    replaced = [o for o in computed if o.get("change") == recon.CHANGE_REPLACED]
    if replaced:
        failures.append(f"[{row}] {label}: a DROPped issue was recorded as replaced: {replaced!r}")
    added = [o for o in computed if o.get("change") == recon.CHANGE_ADDED]
    if len(added) != expected_added:
        failures.append(
            f"[{row}] {label}: expected {expected_added} computed 'added' entry; got {added!r}"
        )


def test_a_dropped_issue_never_pairs_in_reconcile(failures: list[str]) -> None:
    hard_ids = recon.hard_rejection_rule_ids(_load_bundle())
    for label, primary, critic, shape, dropped, added in _drop_vs_pairing_cases("p0001"):
        result = recon.reconcile(
            primary_result=json.loads(primary),
            critic_result=json.loads(critic),
            hard_rejection_rule_ids=hard_ids,
        )
        _check_drop_vs_pairing(
            "12a", label, result["issues"], result.get("critic_delta"), shape, dropped, added, failures
        )
        if result["decision"] != "REQUEST_CHANGE":
            failures.append(f"[12a] {label}: expected REQUEST_CHANGE; got {result['decision']!r}")


def test_a_dropped_issue_never_pairs_end_to_end(failures: list[str]) -> None:
    docx_bytes, block_id = _draft()
    for label, primary, critic, shape, dropped, added in _drop_vs_pairing_cases(block_id):
        result = _run(docx_bytes, primary, [critic])
        if result.get("status") != "OK" or result.get("decision") != "REQUEST_CHANGE":
            failures.append(
                f"[12b] {label}: expected a completed REQUEST_CHANGE; got status="
                f"{result.get('status')!r} decision={result.get('decision')!r} "
                f"reason={result.get('reason')!r}"
            )
            continue
        _check_drop_vs_pairing(
            "12b",
            label,
            result.get("findings") or [],
            result.get("critic_delta"),
            shape,
            dropped,
            added,
            failures,
        )
        redline = result.get("redline_bytes")
        if redline and TEXT_A in _document_xml(redline):
            failures.append(f"[12b] {label}: the primary's edit shipped on its own")


TESTS = [
    test_the_redline_carries_the_critics_text,
    test_the_override_carries_both_versions_and_the_reason,
    test_a_dropped_hard_rejection_is_retained_in_reconcile,
    test_a_dropped_hard_rejection_is_retained_end_to_end,
    test_a_detector_fire_absent_from_both_is_appended,
    test_a_failed_critic_fails_the_review,
    test_a_missing_disposition_fails_closed_at_the_merger,
    test_a_planted_gram_in_a_critic_replacement_is_blocked,
    test_overlapping_spans_are_rejected_not_merged,
    test_the_override_shape_reaches_the_analysis_artifact,
    test_each_instance_of_a_hard_rule_is_retained,
    test_a_keep_whose_issue_is_absent_fails_closed_at_the_merger,
    test_a_keep_whose_issue_is_absent_never_ships_end_to_end,
    test_every_audit_record_text_is_scanned,
    test_a_planted_audit_record_gram_never_reaches_the_analysis_artifact,
    test_a_dropped_floor_invariant_issue_is_retained_in_an_opf_review,
    test_a_dropped_issue_never_pairs_in_reconcile,
    test_a_dropped_issue_never_pairs_end_to_end,
]


def main() -> int:
    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for failure in failures[before:]:
                print(f"FAIL: {failure}")
    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found (issue #138).")
        return 1
    print("PASS: the critic's result is final (issue #138, ADR 0001).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
