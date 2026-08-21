#!/usr/bin/env python3
"""
Regression test: "this review had no posture and no binding" must be
PERSISTED, not stderr-only.

## Root problem this proves fixed

`scripts/opf_prompt.py::_report_omissions` prints an aggregated
`WARNING:` line to `sys.stderr` and nothing else -- the composed record is
never returned to any caller, so it never reaches a review row or the
analysis artifact. Running the real playbook through composition prints
exactly one line:

    WARNING: opf_prompt: x1 posture.system_prompt — the review carries NO
    Posture block; the model receives no negotiation intent from the
    playbook at all

The real playbook ships `posture: {}` AND `floor: {}` BY DESIGN (issue
#479 DECISION, 2026-08-04: an empty-posture OPF artifact is valid and must
run -- this is NOT turned back into a refusal here). But before this fix,
`_binding_block` returned `None` for the "nothing binds" case with NO
`_omit()` call at all (unlike `_posture_block`'s equivalent case just
above it) -- so the aggregate omission record could never say "no Binding
block either", and `review_knowledge.ReviewKnowledge.lineage_record()`
recorded `posture_source: "playbook"` with nothing to say the playbook
supplied no negotiating intent whatsoever.

## What this test proves

  1. `opf_prompt._binding_block` / `_guidance_block` now RECORD an
     omission for their own "nothing at all" case (not just the
     already-covered "content present but malformed" case), via the same
     `_omit` seam `_posture_block` already used.
  2. `opf_prompt.compose_opf_system_blocks`'s new `omissions_out` param
     exposes the collected omissions as DATA (not just a printed line),
     without changing the returned `blocks` list at all.
  3. `review_knowledge.resolve_knowledge` captures those omissions and
     `ReviewKnowledge.lineage_record()` carries them (substance-free: kinds
     and counts, never clause text) alongside `posture_source`, so an
     operator reading the record can tell `posture_source: "playbook"`
     coexisted with NO Posture, NO Binding, and NO Guidance content.
  4. `scripts/review_spine.py::run_review` surfaces that record on its
     result for an OPF review, and `backend/src/pipeline_runner.py`'s
     `_ANALYSIS_FIELDS` allowlist carries it through to the persisted
     `outputs/{review_id}/analysis.json` artifact -- the established seam
     this repo already uses for per-review provenance (issue #416).
  5. `_report_omissions`/the new plumbing never raises and never changes
     the composed prompt blocks.

Run standalone: `python3 tests/test_opf_prompt_omissions_persisted.py`
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
BACKEND_DIR = REPO_ROOT / "backend"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR, BACKEND_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import model_client as model_client_module  # noqa: E402
import opf_load  # noqa: E402
import opf_prompt  # noqa: E402
import review_knowledge  # noqa: E402
import review_spine  # noqa: E402

import src.pipeline_runner as pipeline_runner  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "gold-fixtures-opf"
# posture={} AND floor={} -- the real playbook's own documented shape (see
# tests/test_review_opf_digest_mode_479.py's EMPTY_POSTURE_FIXTURE_PATH).
REAL_SHAPE_FIXTURE = FIXTURES / "acme-university-real-shape.opf.json"
FULL_FIXTURE = FIXTURES / "acme-university.opf.json"

PRIMARY_MODEL_ID = "anthropic.claude-opus-4-8"
CRITIC_MODEL_ID = "anthropic.claude-sonnet-4-6"


def _load(path: Path) -> dict:
    return opf_load.load_opf(path)


def _bundle_v2(doc: dict) -> dict:
    return {"bundle_schema_version": 2, "playbook_id": "acme-university", "opf": copy.deepcopy(doc)}


# ---------------------------------------------------------------------------
# Part 1/2: opf_prompt.py -- omissions recorded AND exposed as data.
# ---------------------------------------------------------------------------


def test_binding_block_records_omission_when_nothing_binds(failures: list[str]) -> None:
    doc = _load(REAL_SHAPE_FIXTURE)
    if doc.get("floor") != {}:
        failures.append("[1a] fixture precondition broken: expected floor == {}")
        return
    omissions: dict[str, list] = {}
    opf_prompt.compose_opf_system_blocks(doc, omissions_out=omissions)
    binding_kinds = [k for k in omissions if k.startswith("binding")]
    if not binding_kinds:
        failures.append(
            f"[1b] no Floor invariants and no policy `must` rules, but no omission "
            f"was recorded for the empty Binding block. Got omissions={omissions!r}"
        )


def test_guidance_block_records_omission_when_no_should_rules(failures: list[str]) -> None:
    doc = _load(REAL_SHAPE_FIXTURE)
    omissions: dict[str, list] = {}
    opf_prompt.compose_opf_system_blocks(doc, omissions_out=omissions)  # policy=None
    guidance_kinds = [k for k in omissions if k.startswith("guidance")]
    if not guidance_kinds:
        failures.append(
            f"[2a] no policy at all, but no omission was recorded for the empty "
            f"Guidance block. Got omissions={omissions!r}"
        )


def test_omissions_out_does_not_change_composed_blocks(failures: list[str]) -> None:
    doc = _load(REAL_SHAPE_FIXTURE)
    blocks_without = opf_prompt.compose_opf_system_blocks(doc)
    omissions: dict[str, list] = {}
    blocks_with = opf_prompt.compose_opf_system_blocks(doc, omissions_out=omissions)
    if blocks_without != blocks_with:
        failures.append(
            f"[3a] passing omissions_out must not change the composed blocks. "
            f"without={blocks_without!r} with={blocks_with!r}"
        )


def test_omissions_out_none_is_the_pre_existing_default(failures: list[str]) -> None:
    """Every pre-existing caller (scripts/opf_acceptance.py, every test in
    this suite that predates this fix) calls with no `omissions_out` at
    all -- must still work exactly as before."""
    doc = _load(FULL_FIXTURE)
    try:
        blocks = opf_prompt.compose_opf_system_blocks(doc)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"[4a] compose_opf_system_blocks with no omissions_out raised: {exc!r}")
        return
    if not blocks:
        failures.append("[4b] full fixture must still compose at least one block")


# ---------------------------------------------------------------------------
# Part 3: review_knowledge.py -- lineage_record() carries the omissions.
# ---------------------------------------------------------------------------


def test_lineage_record_carries_prompt_omissions(failures: list[str]) -> None:
    doc = _load(REAL_SHAPE_FIXTURE)
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle_v2(doc),
        policy=None,
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
        accept_empty_posture=True,
    )
    record = knowledge.lineage_record()
    if "prompt_omissions" not in record:
        failures.append(f"[5a] lineage_record() has no 'prompt_omissions' key at all: {record!r}")
        return
    prompt_omissions = record["prompt_omissions"]
    if not isinstance(prompt_omissions, dict) or not prompt_omissions:
        failures.append(
            f"[5b] expected a non-empty prompt_omissions mapping (posture={{}} AND "
            f"floor={{}} AND no policy all omit something); got {prompt_omissions!r}"
        )
        return
    kinds = " ".join(prompt_omissions.keys())
    if "posture" not in kinds:
        failures.append(f"[5c] expected a 'posture' omission recorded; got keys {list(prompt_omissions)!r}")
    if "binding" not in kinds:
        failures.append(f"[5d] expected a 'binding' omission recorded; got keys {list(prompt_omissions)!r}")
    if "guidance" not in kinds:
        failures.append(f"[5e] expected a 'guidance' omission recorded; got keys {list(prompt_omissions)!r}")
    # Substance-free: counts only, never a value that looks like it could be
    # document/clause text (a plain int per kind, not a nested dict/list of
    # strings pulled from the document).
    for kind, count in prompt_omissions.items():
        if not isinstance(count, int):
            failures.append(
                f"[5f] prompt_omissions must map kind -> count (int); got "
                f"{kind!r} -> {count!r} ({type(count).__name__})"
            )

    # The exact "reads as if the playbook supplied intent" problem Marc
    # named: posture_source == "playbook" coexisting with a record that
    # says nothing else about it, unless prompt_omissions is read too.
    if record.get("posture_source") != "playbook":
        failures.append(f"[5g] fixture precondition broken: expected posture_source == 'playbook'")


def test_lineage_record_omits_nothing_extra_for_a_full_playbook(failures: list[str]) -> None:
    """A playbook with real posture, a real Floor invariant, still composes
    -- prompt_omissions may be empty/absent for a document with nothing to
    drop (never a false positive)."""
    doc = _load(FULL_FIXTURE)
    knowledge = review_knowledge.resolve_knowledge(
        bundle_v2=_bundle_v2(doc),
        policy=None,
        declared_mode=review_knowledge.MODE_PLAYBOOK_DIGEST,
    )
    record = knowledge.lineage_record()
    prompt_omissions = record.get("prompt_omissions") or {}
    if any(k.startswith("posture") for k in prompt_omissions):
        failures.append(f"[6a] full fixture has real posture; must not record a posture omission: {prompt_omissions!r}")
    if any(k.startswith("binding") for k in prompt_omissions):
        failures.append(f"[6b] full fixture has a real Floor invariant; must not record a binding omission: {prompt_omissions!r}")


# ---------------------------------------------------------------------------
# Part 4: the analysis-artifact seam -- run_review() + _ANALYSIS_FIELDS.
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)
_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_docx_bytes() -> bytes:
    body = _body_p("Each party shall indemnify the other without limitation as to amount.")
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _opf_run_review_bundle(doc: dict) -> dict:
    return {
        "opf_bundle_v2": {"opf": doc, "overrides": None},
        "playbook": {
            "metadata": {"primary_model_id": PRIMARY_MODEL_ID, "critic_model_id": CRITIC_MODEL_ID}
        },
    }


def _primary_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": "No changes identified.",
        }
    )


def _critic_accept_response() -> str:
    return json.dumps(
        {
            "schema_version": "output-schema-v1",
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "confidence_band": None,
            "issues": [],
            "critic_delta": None,
            "verdict_summary": None,
        }
    )


def test_run_review_surfaces_opf_knowledge_lineage(failures: list[str]) -> None:
    doc = _load(REAL_SHAPE_FIXTURE)
    bundle = _opf_run_review_bundle(doc)
    docx_bytes = _build_docx_bytes()
    fake_client = model_client_module.FakeBedrockClient(
        {
            PRIMARY_MODEL_ID: [_primary_accept_response()],
            CRITIC_MODEL_ID: [_critic_accept_response()],
        }
    )

    result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="omissions-test-1")

    if result["status"] != "OK":
        failures.append(f"[7a] setup failure: expected status=OK, got {result}")
        return
    lineage = result.get("opf_knowledge_lineage")
    if not lineage:
        failures.append(
            f"[7b] run_review's result carries no 'opf_knowledge_lineage' key for an "
            f"OPF review -- the omission record never left review_knowledge.py. "
            f"Got keys: {sorted(result.keys())!r}"
        )
        return
    prompt_omissions = lineage.get("prompt_omissions") or {}
    if not any(k.startswith("posture") for k in prompt_omissions):
        failures.append(f"[7c] expected a posture omission on the result's lineage record: {prompt_omissions!r}")
    if not any(k.startswith("binding") for k in prompt_omissions):
        failures.append(f"[7d] expected a binding omission on the result's lineage record: {prompt_omissions!r}")


def test_analysis_fields_allowlist_carries_the_lineage_key(failures: list[str]) -> None:
    if "opf_knowledge_lineage" not in pipeline_runner._ANALYSIS_FIELDS:
        failures.append(
            "[8a] backend/src/pipeline_runner.py::_ANALYSIS_FIELDS does not carry "
            "'opf_knowledge_lineage' -- run_review's omission record would never reach "
            "outputs/{review_id}/analysis.json, the established per-review-provenance seam."
        )


def test_v1_review_carries_no_opf_knowledge_lineage(failures: list[str]) -> None:
    """A v1 bundle (no opf_bundle_v2 key) never resolves OPF knowledge at
    all -- the key must be absent, never a null placeholder, matching this
    repo's own convention for every other OPF-only result field."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from test_review_spine import _build_draft_docx, _load_bundle  # noqa: E402 (local import)
    import diff_standard_form as dsf_module

    bundle = _load_bundle()
    docx_bytes = _build_draft_docx(dsf_module, {})
    primary_id = bundle["playbook"]["metadata"]["primary_model_id"]
    critic_id = bundle["playbook"]["metadata"]["critic_model_id"]
    fake_client = model_client_module.FakeBedrockClient(
        {
            primary_id: [_primary_accept_response()],
            critic_id: [_critic_accept_response()],
        }
    )
    result = review_spine.run_review(docx_bytes, bundle, fake_client, review_id="omissions-test-v1")
    if result["status"] != "OK":
        failures.append(f"[9a] setup failure: expected status=OK, got {result}")
        return
    if "opf_knowledge_lineage" in result:
        failures.append(
            f"[9b] a v1 review must never carry 'opf_knowledge_lineage'; got "
            f"{result.get('opf_knowledge_lineage')!r}"
        )


def main() -> int:
    failures: list[str] = []

    test_binding_block_records_omission_when_nothing_binds(failures)
    test_guidance_block_records_omission_when_no_should_rules(failures)
    test_omissions_out_does_not_change_composed_blocks(failures)
    test_omissions_out_none_is_the_pre_existing_default(failures)
    test_lineage_record_carries_prompt_omissions(failures)
    test_lineage_record_omits_nothing_extra_for_a_full_playbook(failures)
    test_run_review_surfaces_opf_knowledge_lineage(failures)
    test_analysis_fields_allowlist_carries_the_lineage_key(failures)
    test_v1_review_carries_no_opf_knowledge_lineage(failures)

    if failures:
        print("FAIL: OPF prompt omissions persistence gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        return 1

    print("PASS: OPF prompt omissions persistence gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
