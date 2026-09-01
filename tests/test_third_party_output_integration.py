#!/usr/bin/env python3
"""
Test -- issue #251 ("Third-party paper: fold position findings into the
review output contract + redline (de-branded)", Slice 5 of 5), MIGRATED to
block addressing by issue #629.

## What this proves

`scripts/third_party_output_integration.py` folds #250's position-level
findings (`{"playbook_topic_id", "clause_id", "decision", "rationale",
"source"}`) into a valid `playbooks/output-schema-v3.json` response AND
into SURGICAL, IN-PLACE tracked changes on the counterparty's OWN uploaded
`.docx` -- the same block-addressed mechanics first-party paper uses, not
the anchored-patch + standalone-writer path this module shipped with.

The behaviour that changes with #629, and is asserted here rather than
assumed:

  * the response is `output-schema-v3`, every issue carries a mutually
    unique `issue_key`, and the code-built `block_patches`/`block_ops` ride
    on the response object;
  * a `reject` finding on a MID-document clause with governed fixed
    replacement text becomes a whole-clause replacement WRITTEN INTO THE
    UPLOADED DOCUMENT: accept-all reads the governed text exactly where the
    struck clause stood, reject-all reproduces the upload, and every other
    clause is left alone;
  * a `flag` finding is flag-only even when its topic HAS fixed replacement
    text -- under the pre-#629 code that same finding produced a patch, so
    this is the assertion that pins the change;
  * a `reject` finding is flag-only too when its topic's
    `replacement_text.mode` is not `fixed` -- the mode gate, not the
    decision, is what keeps pre-authored boilerplate out of a clause whose
    playbook author asked for a *minimal* edit;
  * a clause whose text drifted since segmentation is never edited on a
    stale anchor, is reported with a LABELLED reason rather than as a
    deliberate flag-only (issue #585), and -- when its edit was the only
    one this run had -- fails the run CLOSED rather than reporting a clean
    `OK` with no document;
  * a clause_id that is not 1:1 with a block (two identical clauses) is
    dropped from the mapping rather than resolved to an arbitrary one, and
    fails the run closed the same way.

## What it keeps from the original slice

  1. The mapped response validates against the governed output-contract
     artifact -- binary `decision`, each `Issue` carrying
     `playbook_topic_id`, `section_ref`, `provenance` and the required
     footnote/summary fields.
  2. A `reject` finding yields `decision: REQUEST_CHANGE`; an all-`accept`
     set yields `ACCEPT` with a non-null `verdict_summary`.
  3. Anchors are SELF-DERIVED from a REAL `.docx` run through #248's real
     segmenter, never a pre-built anchor map.
  4. The leakage scan is applied to the human-surfaced fields before any
     document is produced.
  5. Every human-facing string in the response AND the generated redline is
     free of tenant-brand strings and uses "your" voicing.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import jsonschema  # type: ignore  # noqa: E402
import model_client  # type: ignore  # noqa: E402
import extraction_normalization_stage  # type: ignore  # noqa: E402
import leakage_scan  # type: ignore  # noqa: E402
import redline_generate  # type: ignore  # noqa: E402
import redline_projections  # type: ignore  # noqa: E402
import replacement_text_enforcement as rte  # type: ignore  # noqa: E402
import third_party_clause_segmentation as segmentation  # type: ignore  # noqa: E402
import third_party_position_findings as findings_mod  # type: ignore  # noqa: E402

OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
FOOTNOTES_PART = "word/footnotes.xml"
DOCUMENT_PART = "word/document.xml"

#: The id `_segment_uploaded_doc()` segments with. Every clause_id in this
#: test is content-addressed under it, so the integration module must be
#: driven with the SAME id or nothing resolves (that is the contract, not an
#: incidental detail -- `test_source_document_id_must_match_segmentation`
#: pins it).
SOURCE_DOCUMENT_ID = "counterparty-doc-251"


def _qn(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _import_integration_module():
    try:
        import third_party_output_integration  # type: ignore
        return third_party_output_integration, ""
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/third_party_output_integration.py does not exist or "
            f"fails to import ({exc}).\n"
            f"  FIX: implement the third-party findings -> output-schema-v3 response "
            f"+ block-addressed in-place redline mapping (issues #251, #629) -- "
            f"build_third_party_response()/generate_third_party_review_output()."
        )


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder (same zipfile-only convention
# as tests/test_third_party_clause_segmentation.py).
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

_DOC_NAMESPACES = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NAMESPACES}>"
        f"<w:body>{body_paragraphs_xml}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _heading_p(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


_DEFINITIONS_BODY = "Capitalized terms have the meanings given to them in this Agreement."
_CONFIDENTIALITY_BODY = (
    "The receiving party's confidentiality obligation under this clause "
    "shall be perpetual and shall survive termination of this Agreement "
    "indefinitely."
)
_ASSIGNMENT_BODY = (
    "Either party may assign this Agreement to an affiliate upon prior "
    "written notice to the other party."
)


def _build_uploaded_docx() -> bytes:
    """A synthetic counterparty-own-form upload.

    Three clauses, deliberately ordered so the one that fires a
    deterministic hard_rejection (it contains the standalone word
    'perpetual') sits in the MIDDLE of the document -- issue #629's first
    acceptance criterion is about a mid-document clause, and a first-clause
    fixture would let an off-by-one anchor pass. The clause before it and
    the clause after it are the untouched-neighbour evidence that the edit
    is surgical.

    Segmented for REAL via #248's segmenter below, so every clause_id used
    in this test is self-derived from this document's own content, never a
    hand-picked or pre-built anchor.
    """
    body = "".join(
        [
            _heading_p("Definitions"),
            _body_p(_DEFINITIONS_BODY),
            _heading_p("Confidentiality"),
            _body_p(_CONFIDENTIALITY_BODY),
            _heading_p("Assignment"),
            _body_p(_ASSIGNMENT_BODY),
        ]
    )
    return _build_docx_bytes(body)


def _build_drifted_docx() -> bytes:
    """The SAME document after the counterparty edited the confidentiality
    clause -- the real-world drift this path must never patch through: the
    findings were made against the text above, and these are the bytes the
    redline would be written into."""
    body = "".join(
        [
            _heading_p("Definitions"),
            _body_p(_DEFINITIONS_BODY),
            _heading_p("Confidentiality"),
            _body_p(
                "The receiving party's confidentiality obligation under this "
                "clause was renegotiated after this document was segmented."
            ),
            _heading_p("Assignment"),
            _body_p(_ASSIGNMENT_BODY),
        ]
    )
    return _build_docx_bytes(body)


def _build_twin_clause_docx() -> bytes:
    """Two clauses whose heading AND text are identical -- they
    content-address to ONE clause_id, so that id is not 1:1 with a block."""
    body = "".join(
        [
            _heading_p("Confidentiality"),
            _body_p(_CONFIDENTIALITY_BODY),
            _heading_p("Assignment"),
            _body_p(_ASSIGNMENT_BODY),
            _heading_p("Confidentiality"),
            _body_p(_CONFIDENTIALITY_BODY),
        ]
    )
    return _build_docx_bytes(body)


# ---------------------------------------------------------------------------
# Synthetic playbook (the shape #250 consumes) -- deliberately small, NOT
# the real production playbook, so this test controls exactly which
# replacement_text mode each topic carries.
# ---------------------------------------------------------------------------

_CONFIDENTIALITY_FIXED_TEXT = (
    "Each party's confidentiality obligation survives termination of this "
    "Agreement for a period of five years."
)
_ASSIGNMENT_FIXED_TEXT = (
    "Neither party may assign this Agreement without your prior written consent."
)
#: Governed text on a topic whose mode is `bounded_edit`, NOT `fixed`.
#: `playbooks/schema.json` puts no dependency between `mode` and
#: `fixed_text`, so a topic may legitimately carry both -- and this string is
#: what a regression that relaxed the `mode == "fixed"` gate would write
#: verbatim into the counterparty's document.
_DEFINITIONS_BOUNDED_TEXT = (
    "Capitalized terms have the meanings given to them in the section of this "
    "Agreement in which they are first defined."
)

_HARD_REJECTIONS = [
    {
        "id": "no-perpetual-confidentiality",
        "description": "Counterparty proposes a perpetual confidentiality term with no defined survival period.",
        "kind": "on_insert",
        "trigger_terms": ["perpetual"],
        "match": "word_boundary",
        "match_surface": "inserted_or_modified",
        "applies_to_topics": ["confidentiality"],
    },
    {
        "id": "definitions-must-enumerate-defined-terms",
        "description": "Counterparty leaves defined terms open-ended instead of enumerating them.",
        "kind": "on_insert",
        "trigger_terms": ["meanings"],
        "match": "word_boundary",
        "match_surface": "inserted_or_modified",
        "applies_to_topics": ["definitions"],
    },
    {
        "id": "insurance-required",
        "description": "Placeholder rule id only -- never evaluated in this test (insurance has no matched clause).",
        "kind": "on_insert",
        "trigger_terms": ["placeholder-term-never-matched"],
        "match": "word_boundary",
        "applies_to_topics": ["insurance"],
    },
]


def _reject_scenario_playbook() -> dict[str, Any]:
    """`confidentiality` and `assignment` BOTH carry governed fixed
    replacement text. That is deliberate: the confidentiality finding is a
    `reject` and the assignment finding is a `flag`, so the only thing
    separating them is the decision -- which is exactly the branch issue
    #629 changed (a `flag` used to produce a patch here). `insurance` has no
    matched clause at all and yields a missing-position finding.

    `definitions` is the mirror image of the assignment topic: a `reject`
    on a MATCHED clause, on a topic that carries governed replacement text,
    where the only thing keeping that text out of the document is the
    `replacement_text.mode` gate (`bounded_edit`, not `fixed`). It is
    listed LAST so `topics[0]` stays the confidentiality topic the leakage
    assertions below reach by index."""
    return {
        "topics": [
            {
                "id": "confidentiality",
                "section_ref": "Confidentiality",
                "our_standard": "Confidentiality survives termination for a bounded period.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": ["no-perpetual-confidentiality"],
                "replacement_text": {
                    "mode": "fixed",
                    "fixed_text": _CONFIDENTIALITY_FIXED_TEXT,
                    "max_chars": 500,
                    "must_not_introduce": [],
                },
            },
            {
                "id": "assignment",
                "section_ref": "Assignment",
                "our_standard": "Assignment requires prior written consent.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": [],
                "replacement_text": {
                    "mode": "fixed",
                    "fixed_text": _ASSIGNMENT_FIXED_TEXT,
                    "max_chars": 500,
                    "must_not_introduce": [],
                },
            },
            {
                "id": "insurance",
                "section_ref": "Insurance",
                "our_standard": "Counterparty maintains commercial general liability insurance.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": ["insurance-required"],
                "replacement_text": {"mode": "none"},
            },
            {
                "id": "definitions",
                "section_ref": "Definitions",
                "our_standard": "Defined terms are enumerated where they are first used.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": ["definitions-must-enumerate-defined-terms"],
                "replacement_text": {
                    "mode": "bounded_edit",
                    "fixed_text": _DEFINITIONS_BOUNDED_TEXT,
                    "max_chars": 500,
                    "must_not_introduce": [],
                },
            },
        ],
        "hard_rejections": _HARD_REJECTIONS,
    }


def _accept_scenario_playbook() -> dict[str, Any]:
    return {
        "topics": [
            {
                "id": "assignment",
                "section_ref": "Assignment",
                "our_standard": "Assignment requires prior written consent.",
                "must_preserve": [],
                "reject_if_proposed": [],
                "hard_rejection_refs": [],
                "replacement_text": {"mode": "none"},
            },
        ],
        "hard_rejections": [],
    }


_MODEL_ID = model_client.primary_model_id()


def _segment_uploaded_doc() -> list[dict[str, Any]]:
    result = segmentation.segment_document(
        _build_uploaded_docx(), source_document_id=SOURCE_DOCUMENT_ID
    )
    assert result["status"] == "segmented", f"segmentation failed: {result}"
    return result["clauses"]


def _clause_id_for_heading(clauses: list[dict[str, Any]], heading: str) -> str:
    for clause in clauses:
        if clause.get("heading") == heading:
            return clause["clause_id"]
    raise AssertionError(f"no segmented clause with heading {heading!r}: {clauses!r}")


def _reject_scenario_findings(clauses: list[dict[str, Any]]):
    playbook = _reject_scenario_playbook()
    conf_id = _clause_id_for_heading(clauses, "Confidentiality")
    assign_id = _clause_id_for_heading(clauses, "Assignment")
    definitions_id = _clause_id_for_heading(clauses, "Definitions")
    match_result = {
        "topic_matches": {
            "confidentiality": [conf_id],
            "assignment": [assign_id],
            "insurance": [],
            # A REAL segmented clause, so the reject below reaches the
            # `replacement_text.mode` gate rather than exiting earlier on a
            # missing clause_id. Its rule fires deterministically, so this
            # adds no model call to the seeded queue above.
            "definitions": [definitions_id],
        }
    }
    fake_client = model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                json.dumps(
                    {
                        "decision": "flag",
                        "rationale": (
                            "This clause needs attorney review against your "
                            "assignment position before it can be accepted."
                        ),
                    }
                )
            ]
        }
    )
    findings = findings_mod.evaluate_position_findings(
        clauses, match_result, playbook, fake_client, model_id=_MODEL_ID
    )
    return playbook, findings


def _accept_scenario_findings(clauses: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    playbook = _accept_scenario_playbook()
    assign_id = _clause_id_for_heading(clauses, "Assignment")
    match_result = {"topic_matches": {"assignment": [assign_id]}}
    fake_client = model_client.FakeBedrockClient(
        {
            _MODEL_ID: [
                json.dumps(
                    {
                        "decision": "accept",
                        "rationale": (
                            "This clause matches your assignment position and "
                            "can be accepted as proposed."
                        ),
                    }
                )
            ]
        }
    )
    findings = findings_mod.evaluate_position_findings(
        clauses, match_result, playbook, fake_client, model_id=_MODEL_ID
    )
    return playbook, findings


def _generate(mod, playbook, findings, clauses, docx_bytes, **kwargs):
    return mod.generate_third_party_review_output(
        findings=findings,
        clause_records=clauses,
        playbook=playbook,
        document_docx_bytes=docx_bytes,
        corpus=leakage_scan.ConfidentialCorpus.from_playbook(playbook),
        source_document_id=SOURCE_DOCUMENT_ID,
        **kwargs,
    )


def _extract_docx_text(docx_bytes: bytes) -> str:
    texts = []
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        for name in zf.namelist():
            if not name.startswith("word/") or not name.endswith(".xml"):
                continue
            root = ET.fromstring(zf.read(name))
            for el in root.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag in ("t", "delText") and el.text:
                    texts.append(el.text)
    return "\n".join(texts)


def _block_texts(docx_bytes: bytes) -> list[str]:
    """The document's logical-paragraph texts, in document order, through
    the SAME extractor the compiler addresses blocks with."""
    normalized = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    assert normalized["status"] == "normalized", normalized
    return [p.get("text", "") for p in normalized["paragraphs"]]


def _footnote_texts(docx_bytes: bytes) -> list[str]:
    """Authored footnote bodies, excluding Word's two mandatory separator
    footnotes (ids <= 0). Empty when the package carries no footnotes part."""
    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        if FOOTNOTES_PART not in zf.namelist():
            return []
        root = ET.fromstring(zf.read(FOOTNOTES_PART))
    texts = []
    for footnote in root.findall(_qn("footnote")):
        if int(footnote.get(_qn("id"), "0")) <= 0:
            continue
        texts.append("".join((t.text or "") for t in footnote.iter(_qn("t"))).strip())
    return texts


def _paragraph_containing(root, needle: str):
    """The first `<w:p>` whose visible text contains `needle`, or None."""
    for paragraph in root.iter(_qn("p")):
        if needle in "".join(t.text or "" for t in paragraph.iter(_qn("t"))):
            return paragraph
    return None


def _footnote_reference_owner_ins_ids(docx_bytes: bytes) -> list[str]:
    """The `w:id` of every `<w:ins>` that CONTAINS a footnote reference --
    i.e. the revision each footnote is anchored to."""
    with zipfile.ZipFile(io.BytesIO(bytes(docx_bytes))) as zf:
        root = ET.fromstring(zf.read(DOCUMENT_PART))
    owners = []
    for ins in root.iter(_qn("ins")):
        if any(True for _ in ins.iter(_qn("footnoteReference"))):
            owners.append(ins.get(_qn("id")))
    return owners


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def test_response_validates_against_output_schema(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    block_map_ids = mod.map_clause_ids_to_block_ids(
        extraction_normalization_stage.extract_and_normalize(_build_uploaded_docx())["paragraphs"],
        source_document_id=SOURCE_DOCUMENT_ID,
    )
    response = mod.build_third_party_response(
        findings, clauses, playbook, block_id_by_clause_id=block_map_ids
    )
    schema = json.loads(OUTPUT_SCHEMA_PATH.read_text())
    if response.get("schema_version") != "output-schema-v3":
        failures.append(
            f"[1] third-party responses must be stamped output-schema-v3, got "
            f"{response.get('schema_version')!r}"
        )
    try:
        jsonschema.validate(instance=response, schema=schema)
    except jsonschema.ValidationError as exc:
        failures.append(f"[1] response failed output-schema-v3 validation: {exc.message}")
        return
    for issue in response.get("issues", []):
        for key in (
            "issue_key",
            "playbook_topic_id",
            "section_ref",
            "provenance",
            "external_rationale_for_footnote",
            "decision",
        ):
            if not issue.get(key) and key != "decision":
                failures.append(f"[1] issue missing/empty required key {key!r}: {issue!r}")
            if key == "decision" and issue.get(key) != "REQUEST_CHANGE":
                failures.append(f"[1] issue decision != REQUEST_CHANGE: {issue!r}")

    # Draft-07 cannot express uniqueness across array items, so the schema
    # check above does NOT cover it -- and every block op joins to its issue
    # by this key.
    keys = [issue.get("issue_key") for issue in response.get("issues", [])]
    if len(set(keys)) != len(keys):
        failures.append(f"[1] issue_keys must be mutually unique across the response: {keys!r}")

    # The code-built transcript rides on the response object (issue #629
    # Scope), and every edit names an issue that exists.
    if "block_patches" not in response or "block_ops" not in response:
        failures.append(
            f"[1] the code-built block_patches/block_ops must be part of the "
            f"response object, got keys {sorted(response)!r}"
        )
        return
    for block_op in response["block_ops"]:
        if block_op.get("issue_key") not in keys:
            failures.append(
                f"[1] block op names issue_key {block_op.get('issue_key')!r}, which no "
                f"issue in the response carries: {block_op!r}"
            )


def test_reject_finding_yields_request_change(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    response = mod.build_third_party_response(findings, clauses, playbook)
    if response.get("decision") != "REQUEST_CHANGE":
        failures.append(f"[2a] expected decision REQUEST_CHANGE, got {response.get('decision')!r}")
    if len(response.get("issues", [])) != 4:
        failures.append(
            f"[2a] expected 4 issues (confidentiality/assignment/insurance/"
            f"definitions), got {response.get('issues')!r}"
        )

    by_topic = {issue["playbook_topic_id"]: issue for issue in response.get("issues", [])}
    conf_issue = by_topic.get("confidentiality")
    if conf_issue is None:
        failures.append("[2a] no issue for topic 'confidentiality'")
    else:
        if conf_issue.get("section_ref") != "Confidentiality":
            failures.append(
                f"[2a] confidentiality issue section_ref should be the counterparty "
                f"clause's own heading 'Confidentiality', got {conf_issue.get('section_ref')!r}"
            )
        if not conf_issue.get("provenance", "").startswith("detector:"):
            failures.append(
                f"[2a] confidentiality issue provenance should attribute a detector "
                f"fire, got {conf_issue.get('provenance')!r}"
            )
        if conf_issue.get("proposed_replacement_text") != _CONFIDENTIALITY_FIXED_TEXT:
            failures.append("[2a] confidentiality issue should carry the topic's fixed_text replacement")

    assign_issue = by_topic.get("assignment")
    if assign_issue is None:
        failures.append("[2a] no issue for topic 'assignment'")
    else:
        if assign_issue.get("provenance") != "model":
            failures.append(f"[2a] assignment issue provenance should be 'model', got {assign_issue.get('provenance')!r}")
        # Issue #629: a `flag` is flag-only even though this topic HAS
        # governed fixed replacement text. Under the pre-#629 rule this
        # field carried `_ASSIGNMENT_FIXED_TEXT` and the finding produced a
        # patch.
        if assign_issue.get("proposed_replacement_text") != "":
            failures.append(
                f"[2a] a `flag` finding must stay flag-only even on a fixed-mode "
                f"topic, got proposed_replacement_text="
                f"{assign_issue.get('proposed_replacement_text')!r}"
            )

    insurance_issue = by_topic.get("insurance")
    if insurance_issue is None:
        failures.append("[2a] no issue for topic 'insurance' (missing-position finding)")
    else:
        if insurance_issue.get("proposed_replacement_text"):
            failures.append("[2a] a missing-position issue (no clause) should never carry replacement text")


def test_accept_only_findings_yield_accept_with_verdict_summary(failures, mod, clauses):
    playbook, findings = _accept_scenario_findings(clauses)
    response = mod.build_third_party_response(findings, clauses, playbook)
    if response.get("decision") != "ACCEPT":
        failures.append(f"[2b] expected decision ACCEPT, got {response.get('decision')!r}")
    if response.get("issues"):
        failures.append(f"[2b] ACCEPT response should carry no issues, got {response.get('issues')!r}")
    if response.get("block_ops"):
        failures.append(f"[2b] an ACCEPT response must propose no edits, got {response.get('block_ops')!r}")
    verdict_summary = response.get("verdict_summary")
    if not verdict_summary or not isinstance(verdict_summary, str):
        failures.append(f"[2b] ACCEPT response must carry a non-null verdict_summary, got {verdict_summary!r}")


def test_reject_finding_edits_the_uploaded_document_in_place(failures, mod, clauses):
    """Issue #629 AC 1: an IN-PLACE tracked change on the uploaded document
    -- not a standalone synthetic docx -- with the finding's footnote
    attached per `issue_key`, passing projections and round-trip."""
    playbook, findings = _reject_scenario_findings(clauses)
    uploaded = _build_uploaded_docx()
    result = _generate(mod, playbook, findings, clauses, uploaded)

    if result.get("status") != "OK":
        failures.append(f"[3] expected status='OK', got {result!r}")
        return
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append(f"[3] a reject finding on a matched clause must produce a document: {result!r}")
        return

    # The clause the transcript addressed is MID-document, so an off-by-one
    # anchor would land on a neighbour rather than going unnoticed.
    source_blocks = _block_texts(uploaded)
    if source_blocks[1] != _CONFIDENTIALITY_BODY:
        failures.append(
            f"[3] fixture drift: the confidentiality clause is no longer the "
            f"middle block: {source_blocks!r}"
        )
        return

    # IN PLACE: accept-all is the uploaded document with THAT clause
    # replaced by the governed text, and every neighbour untouched.
    accepted = _block_texts(
        extraction_normalization_stage.materialize_accept_all(docx_bytes)
    )
    expected_accepted = [_DEFINITIONS_BODY, _CONFIDENTIALITY_FIXED_TEXT, _ASSIGNMENT_BODY]
    if accepted != expected_accepted:
        failures.append(
            f"[3] accept-all should read the uploaded document with the struck "
            f"clause replaced in place.\n  expected: {expected_accepted!r}\n"
            f"  actual:   {accepted!r}"
        )

    # ... and reject-all reproduces the upload exactly -- the proof that
    # this is the counterparty's OWN document carrying tracked changes,
    # rather than a freshly written one.
    rejected = _block_texts(redline_projections.materialize_reject_all(docx_bytes))
    if rejected != source_blocks:
        failures.append(
            f"[3] reject-all must reproduce the uploaded document.\n"
            f"  expected: {source_blocks!r}\n  actual:   {rejected!r}"
        )

    # The strike is a real tracked deletion of the counterparty's own words.
    if "perpetual" not in _extract_docx_text(docx_bytes):
        failures.append(
            "[3] the struck clause's own text must survive in the delivered "
            "document as a tracked deletion, not be silently dropped"
        )

    # Round-trip: the delivered bytes open.
    try:
        redline_generate.verify_docx_round_trip(docx_bytes)
    except ValueError as exc:
        failures.append(f"[3] delivered document failed round-trip verification: {exc}")

    # One footnote, carrying THIS issue's rationale, anchored inside a
    # revision this issue authored.
    conf_finding = next(
        f for f in findings if f.get("playbook_topic_id") == "confidentiality"
    )
    footnotes = _footnote_texts(docx_bytes)
    if footnotes != [conf_finding["rationale"]]:
        failures.append(
            f"[3] expected exactly the reject finding's rationale as a footnote, "
            f"got {footnotes!r}"
        )
    if not _footnote_reference_owner_ins_ids(docx_bytes):
        failures.append(
            "[3] the footnote reference must be anchored inside the tracked "
            "insertion the issue authored, not left floating in the body"
        )


def test_flag_finding_leaves_its_clause_untouched(failures, mod, clauses):
    """Issue #629 AC 2: a `flag` finding leaves the document byte-identical
    at that clause and appears flag-only in the response."""
    playbook, findings = _reject_scenario_findings(clauses)
    uploaded = _build_uploaded_docx()
    result = _generate(mod, playbook, findings, clauses, uploaded)
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append(f"[3b] expected a delivered document to inspect: {result!r}")
        return

    response = result.get("response") or {}
    assign_key = next(
        (
            issue["issue_key"]
            for issue in response.get("issues", [])
            if issue.get("playbook_topic_id") == "assignment"
        ),
        None,
    )
    if assign_key is None:
        failures.append("[3b] no issue for the flagged assignment clause")
        return
    authored = [op for op in response.get("block_ops", []) if op.get("issue_key") == assign_key]
    if authored:
        failures.append(f"[3b] a flag finding must author no document edit, got {authored!r}")

    # Byte-identical at that clause: the assignment paragraph in the
    # delivered document is the source paragraph, unchanged, with no
    # revision markup anywhere inside it.
    with zipfile.ZipFile(io.BytesIO(uploaded)) as zf:
        source_root = ET.fromstring(zf.read(DOCUMENT_PART))
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        out_root = ET.fromstring(zf.read(DOCUMENT_PART))

    source_p = _paragraph_containing(source_root, _ASSIGNMENT_BODY)
    out_p = _paragraph_containing(out_root, _ASSIGNMENT_BODY)
    if source_p is None or out_p is None:
        failures.append("[3b] the flagged clause's paragraph is missing from one of the documents")
        return
    if ET.tostring(out_p) != ET.tostring(source_p):
        failures.append(
            f"[3b] the flagged clause's paragraph must be byte-identical to the "
            f"upload's.\n  source: {ET.tostring(source_p)!r}\n  output: {ET.tostring(out_p)!r}"
        )

    # ... and it is reported flag-only rather than silently dropped.
    flag_only_refs = {
        (entry.get("_source_issue") or {}).get("issue_key")
        for entry in result.get("flag_only") or []
    }
    if assign_key not in flag_only_refs:
        failures.append(
            f"[3b] the flagged issue must appear in flag_only, got {flag_only_refs!r}"
        )


def test_reject_on_a_non_fixed_mode_topic_is_flag_only(failures, mod, clauses):
    """A `reject` on a MATCHED clause is STILL flag-only when its topic's
    `replacement_text.mode` is not `fixed` (issue #629 Scope, and the
    module docstring's "Why the edits are code-built here").

    Nothing else in this file reaches that gate: the confidentiality reject
    is fixed-mode and passes it, the assignment flag is turned away by the
    decision check before it, and the insurance finding has no `clause_id`
    at all. So this is the only assertion standing between a `bounded_edit`
    topic -- whose author asked for a "minimal edit anchored to the
    existing clause" -- and a whole-clause strike-and-replace that inserts
    pre-authored boilerplate the author never approved for verbatim use.
    """
    playbook, findings = _reject_scenario_findings(clauses)

    # What is under test is the MODE, not an absent replacement string: the
    # topic really does carry governed text a relaxed gate would write.
    definitions_topic = next(
        (t for t in playbook["topics"] if t.get("id") == "definitions"), None
    )
    replacement_cfg = (definitions_topic or {}).get("replacement_text") or {}
    if (
        replacement_cfg.get("mode") != "bounded_edit"
        or replacement_cfg.get("fixed_text") != _DEFINITIONS_BOUNDED_TEXT
    ):
        failures.append(
            f"[3f] fixture drift: the definitions topic must be a "
            f"`bounded_edit` topic carrying governed fixed_text, got "
            f"{definitions_topic!r}"
        )
        return
    definitions_finding = next(
        (f for f in findings if f.get("playbook_topic_id") == "definitions"), None
    )
    if definitions_finding is None or definitions_finding.get("decision") != "reject":
        failures.append(
            f"[3f] fixture drift: the definitions clause must produce a `reject` "
            f"finding on a matched clause, got {definitions_finding!r}"
        )
        return

    uploaded = _build_uploaded_docx()
    result = _generate(mod, playbook, findings, clauses, uploaded)
    docx_bytes = result.get("docx_bytes")
    if not docx_bytes:
        failures.append(f"[3f] expected a delivered document to inspect: {result!r}")
        return

    response = result.get("response") or {}
    definitions_issue = next(
        (
            i
            for i in response.get("issues", [])
            if i.get("playbook_topic_id") == "definitions"
        ),
        None,
    )
    if definitions_issue is None:
        failures.append("[3f] no issue for the rejected definitions clause")
        return
    if definitions_issue.get("proposed_replacement_text") != "":
        failures.append(
            f"[3f] a reject on a non-`fixed` mode topic must propose no "
            f"replacement text, got "
            f"{definitions_issue.get('proposed_replacement_text')!r}"
        )
    authored = [
        op
        for op in response.get("block_ops", [])
        if op.get("issue_key") == definitions_issue.get("issue_key")
    ]
    if authored:
        failures.append(
            f"[3f] a `bounded_edit` topic's governed text must never become a "
            f"whole-block strike-and-replace, got {authored!r}"
        )
    if _DEFINITIONS_BOUNDED_TEXT in _extract_docx_text(docx_bytes):
        failures.append(
            "[3f] the bounded_edit topic's fixed_text reached the delivered "
            "document; only a `mode == 'fixed'` topic's text may be written verbatim"
        )

    # Byte-identical: the counterparty's own paragraph survives untouched,
    # with no revision markup anywhere inside it.
    with zipfile.ZipFile(io.BytesIO(uploaded)) as zf:
        source_root = ET.fromstring(zf.read(DOCUMENT_PART))
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        out_root = ET.fromstring(zf.read(DOCUMENT_PART))
    source_p = _paragraph_containing(source_root, _DEFINITIONS_BODY)
    out_p = _paragraph_containing(out_root, _DEFINITIONS_BODY)
    if source_p is None or out_p is None:
        failures.append(
            "[3f] the rejected clause's paragraph is missing from one of the documents"
        )
        return
    if ET.tostring(out_p) != ET.tostring(source_p):
        failures.append(
            f"[3f] the clause of a non-`fixed` mode topic must be byte-identical "
            f"to the upload's.\n  source: {ET.tostring(source_p)!r}\n"
            f"  output: {ET.tostring(out_p)!r}"
        )


def test_drifted_clause_is_never_patched_and_is_labelled(failures, mod, clauses):
    """A clause whose text changed since segmentation no longer
    content-addresses to any block in the delivered document. Nothing may be
    written against that stale anchor -- the issue must NOT be reported as a
    deliberate flag-only (issue #585's mislabel), and since that suppressed
    edit was the only one this run had, the run itself must fail CLOSED.

    The status is the assertion that pins the migration: before issue #629
    the apply step refused this input and the caller got
    `MANUAL_REVIEW_REQUIRED`. #629 moved the refusal earlier -- the edit is
    dropped at PLAN time, before the compiler sees it -- so without
    re-deriving the status here the compiler sees an empty transcript and
    reports its "every issue is deliberately flag-only" clean `OK`, telling
    the caller the review succeeded."""
    playbook, findings = _reject_scenario_findings(clauses)
    drifted = _build_drifted_docx()
    result = _generate(mod, playbook, findings, clauses, drifted)

    if result.get("status") != mod.MANUAL_REVIEW_REQUIRED:
        failures.append(
            f"[3c] the only wanted edit was suppressed for an untrustworthy "
            f"anchor, so the run must fail closed, not report success; got "
            f"status={result.get('status')!r}"
        )
    if result.get("reason") != mod.REASON_CLAUSE_ANCHOR_UNRESOLVED:
        failures.append(
            f"[3c] the fail-closed result must name WHY, got "
            f"reason={result.get('reason')!r}"
        )
    if "decision" in result:
        failures.append(
            f"[3c] a SYSTEM status is never a legal decision, got "
            f"decision={result.get('decision')!r}"
        )

    response = result.get("response") or {}
    if response.get("block_ops"):
        failures.append(
            f"[3c] a drifted clause anchor must produce no edit at all, got "
            f"{response['block_ops']!r}"
        )
    if result.get("docx_bytes") is not None:
        failures.append(
            "[3c] this scenario's only edit is on the drifted clause -- no "
            "document should be delivered"
        )

    conf_issue = next(
        (i for i in response.get("issues", []) if i.get("playbook_topic_id") == "confidentiality"),
        None,
    )
    if conf_issue is None:
        failures.append("[3c] no issue for the drifted confidentiality clause")
        return
    if conf_issue.get(rte.REPLACEMENT_TEXT_OUTCOME_FIELD) != mod.REASON_CLAUSE_ANCHOR_UNRESOLVED:
        failures.append(
            f"[3c] an unaddressable clause must be labelled "
            f"{mod.REASON_CLAUSE_ANCHOR_UNRESOLVED!r}, not left to the unlabelled "
            f"fallback; got {conf_issue.get(rte.REPLACEMENT_TEXT_OUTCOME_FIELD)!r}"
        )

    reasons = {
        entry.get("reason") for entry in (result.get("analysis_report") or {}).get(
            "changes_not_applied", []
        )
    }
    if mod.REASON_CLAUSE_ANCHOR_UNRESOLVED not in reasons:
        failures.append(
            f"[3c] the attorney hand-off must name the reason the clause was not "
            f"edited, got {reasons!r}"
        )


def test_ambiguous_clause_id_is_dropped_from_the_mapping(failures, mod, clauses):
    """Two clauses with identical heading AND text content-address to ONE
    clause_id. Resolving it to an arbitrary one of them would strike the
    wrong paragraph, so it resolves to neither."""
    twin_docx = _build_twin_clause_docx()
    twin_clauses = segmentation.segment_document(
        twin_docx, source_document_id=SOURCE_DOCUMENT_ID
    )["clauses"]
    twin_ids = [c["clause_id"] for c in twin_clauses]
    if len(twin_ids) != 3 or twin_ids[0] != twin_ids[2]:
        failures.append(
            f"[3d] fixture drift: the two identical clauses no longer share a "
            f"clause_id: {twin_ids!r}"
        )
        return

    mapping = mod.map_clause_ids_to_block_ids(
        extraction_normalization_stage.extract_and_normalize(twin_docx)["paragraphs"],
        source_document_id=SOURCE_DOCUMENT_ID,
    )
    if twin_ids[0] in mapping:
        failures.append(
            f"[3d] an ambiguous clause_id resolved to block "
            f"{mapping[twin_ids[0]]!r}; it must resolve to no block at all"
        )
    if twin_ids[1] not in mapping:
        failures.append(
            "[3d] the unambiguous clause in the same document must still resolve "
            "-- one ambiguous id may not cost every other issue its redline"
        )

    # ... and the run built on that mapping fails CLOSED. Dropping an
    # ambiguous id is not the same thing as the playbook never having asked
    # for a redline, and this scenario's only wanted edit was on that
    # clause, so nothing was delivered and nobody may be told it succeeded.
    playbook, findings = _reject_scenario_findings(clauses)
    result = _generate(mod, playbook, findings, clauses, twin_docx)
    if result.get("status") != mod.MANUAL_REVIEW_REQUIRED:
        failures.append(
            f"[3d] an ambiguous anchor must fail the run closed, got "
            f"status={result.get('status')!r}"
        )
    if result.get("reason") != mod.REASON_CLAUSE_ANCHOR_UNRESOLVED:
        failures.append(
            f"[3d] the fail-closed result must name WHY, got "
            f"reason={result.get('reason')!r}"
        )
    if result.get("docx_bytes") is not None:
        failures.append("[3d] a fail-closed run must deliver no document")


def test_source_document_id_must_match_segmentation(failures, mod, clauses):
    """The clause_id -> block_id join is content-addressed under the
    segmentation-time `source_document_id`. Driving it with a different id
    resolves NOTHING -- asserted so the parameter cannot quietly default to
    something that only happens to work."""
    mapping = mod.map_clause_ids_to_block_ids(
        extraction_normalization_stage.extract_and_normalize(_build_uploaded_docx())["paragraphs"],
        source_document_id="a-different-upload",
    )
    segmented_ids = {c["clause_id"] for c in clauses}
    if segmented_ids & set(mapping):
        failures.append(
            "[3e] clause ids segmented under one source_document_id must not "
            "resolve under another"
        )


def test_leakage_scan_applied_to_human_surfaced_fields(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)
    uploaded = _build_uploaded_docx()

    # A clean scenario must not be blocked.
    clean_result = _generate(mod, playbook, findings, clauses, uploaded)
    if clean_result.get("status") == "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(f"[4] clean scenario should not be leakage-blocked, got {clean_result!r}")

    # A leaky rationale (external_rationale_for_footnote source) must be
    # caught BEFORE any redline is produced -- planting an internal-only
    # strategy phrase (scripts/leakage_scan.py's structural pattern check,
    # corpus-independent) directly in a finding's rationale, which this
    # module passes straight through to Issue.external_rationale_for_footnote.
    leaky_findings = [dict(f) for f in findings]
    for f in leaky_findings:
        if f.get("playbook_topic_id") == "confidentiality":
            f["rationale"] = (
                "This is an internal-only rationale that must never reach "
                "the counterparty."
            )
    leaky_result = _generate(mod, playbook, leaky_findings, clauses, uploaded)
    if leaky_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[4] a rationale containing an internal-only strategy phrase must "
            f"be leakage-blocked before redline generation, got {leaky_result!r}"
        )
    if leaky_result.get("reason") != "leakage_detected":
        failures.append(f"[4] leakage-blocked result should carry reason='leakage_detected', got {leaky_result.get('reason')!r}")
    if leaky_result.get("docx_bytes") is not None:
        failures.append("[4] a leakage-blocked result must never carry docx_bytes")
    if leaky_result.get("response") is not None:
        failures.append("[4] a leakage-blocked result must never carry a surfaceable response")

    # A leaky governed `fixed_text` must not be waved through end to end
    # just because no rationale leaked. This assertion says only that: the
    # whole run stops. It proves nothing about WHICH field stopped it --
    # `_proposed_replacement_text()` puts the same `fixed_text` on the
    # Issue field and on the insert op, and the Issue field is scanned
    # first, so the real block here reports `proposed_replacement_text`.
    # The block_ops channel is covered by the direct-gate check below.
    leaky_playbook = _reject_scenario_playbook()
    leaky_playbook["topics"][0]["replacement_text"]["fixed_text"] = (
        "This is an internal-only clause that must never reach the counterparty."
    )
    leaky_text_result = _generate(mod, leaky_playbook, findings, clauses, uploaded)
    if leaky_text_result.get("status") != "ERROR_MANUAL_REVIEW_REQUIRED":
        failures.append(
            f"[4] a leaky governed fixed_text must stop the run, got "
            f"{leaky_text_result!r}"
        )

    # The governed replacement language this path writes into the
    # counterparty's document is model-facing output too, and v3 routes it
    # through `block_ops[].new_text`. Prove the gate sees THAT channel and
    # not merely its `Issue.proposed_replacement_text` twin: build a clean
    # response, leave every issue field clean, plant the internal-only
    # phrase in the insert op's `new_text` ALONE, and require the gate to
    # name `leakage_scan.BLOCK_OP_NEW_TEXT_FIELD` as the blocked field. A
    # regression that stopped walking `block_ops` fails here.
    block_map_ids = mod.map_clause_ids_to_block_ids(
        extraction_normalization_stage.extract_and_normalize(uploaded)["paragraphs"],
        source_document_id=SOURCE_DOCUMENT_ID,
    )
    block_op_response = mod.build_third_party_response(
        findings, clauses, playbook, block_id_by_clause_id=block_map_ids
    )
    insert_ops = [
        op
        for op in block_op_response.get("block_ops") or []
        if op.get("op") == "insert_block_after"
    ]
    if not insert_ops:
        failures.append(
            "[4] the reject scenario must produce an insert_block_after op for the "
            f"block_ops leakage check, got {block_op_response.get('block_ops')!r}"
        )
    for op in insert_ops:
        op["new_text"] = (
            "This is an internal-only clause that must never reach the counterparty."
        )
    try:
        leakage_scan.run_leakage_gate(
            block_op_response,
            leakage_scan.ConfidentialCorpus.from_playbook(playbook),
        )
        failures.append(
            "[4] replacement language reaching the document through "
            "block_ops[].new_text must be leakage-scanned, but the gate passed it"
        )
    except leakage_scan.LeakageDetectedError as exc:
        if exc.field_name != leakage_scan.BLOCK_OP_NEW_TEXT_FIELD:
            failures.append(
                f"[4] a leaky block_ops[].new_text must be blocked on "
                f"{leakage_scan.BLOCK_OP_NEW_TEXT_FIELD!r}, got {exc.field_name!r}"
            )


def test_output_and_redline_free_of_exos_and_your_voiced(failures, mod, clauses):
    playbook, findings = _reject_scenario_findings(clauses)

    response = mod.build_third_party_response(findings, clauses, playbook)
    human_strings = [response.get("verdict_summary") or ""]
    for issue in response.get("issues", []):
        human_strings.extend(
            [
                issue.get("counterparty_change_summary") or "",
                issue.get("external_rationale_for_footnote") or "",
                issue.get("proposed_replacement_text") or "",
                issue.get("section_ref") or "",
                issue.get("section_title") or "",
            ]
        )

    for s in human_strings:
        if "exos" in s.lower():
            failures.append(f"[5] human-facing string contains tenant-brand strings: {s!r}")

    if not any("your" in s.lower() for s in human_strings if s):
        failures.append("[5] no human-facing string uses 'your' voicing anywhere")

    result = _generate(mod, playbook, findings, clauses, _build_uploaded_docx())
    docx_bytes = result.get("docx_bytes")
    if docx_bytes:
        redline_text = _extract_docx_text(docx_bytes)
        if "exos" in redline_text.lower():
            failures.append("[5] generated redline .docx contains tenant-brand strings")


TESTS = [
    test_response_validates_against_output_schema,
    test_reject_finding_yields_request_change,
    test_accept_only_findings_yield_accept_with_verdict_summary,
    test_reject_finding_edits_the_uploaded_document_in_place,
    test_flag_finding_leaves_its_clause_untouched,
    test_reject_on_a_non_fixed_mode_topic_is_flag_only,
    test_drifted_clause_is_never_patched_and_is_labelled,
    test_ambiguous_clause_id_is_dropped_from_the_mapping,
    test_source_document_id_must_match_segmentation,
    test_leakage_scan_applied_to_human_surfaced_fields,
    test_output_and_redline_free_of_exos_and_your_voiced,
]


def main() -> int:
    mod, missing_msg = _import_integration_module()
    if mod is None:
        print("FAIL: third-party output/redline integration (issues #251, #629).\n")
        print(missing_msg)
        print("\nTotal failures: 1")
        return 1

    clauses = _segment_uploaded_doc()

    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures, mod, clauses)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"[{test.__name__}] raised {type(exc).__name__}: {exc}")
        if len(failures) == before:
            print(f"PASS: {test.__name__}")
        else:
            for f in failures[before:]:
                print(f"FAIL: {f}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s) found.")
        return 1
    print("PASS: third-party output/redline integration (issues #251, #629) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
