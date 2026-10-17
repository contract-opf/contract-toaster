#!/usr/bin/env python3
"""
LLM-native evaluation harness -- issue #400.

## Why this rewrite

Before this issue, this module scored gold fixtures against the RETIRED
deterministic lexical hard-rejection detector engine and the standard-form
line-diff module -- both retired from issue-generation by the 2026-07-22
LLM-native architecture decision (issue #380, see `scripts/review_spine.py`'s
own "LLM-native review" docstring section, which names the two retired
modules explicitly). That left the project with NO instrument for review
quality on the path actually in production: `scripts/review_spine.py
::run_review()`, the composed extract -> primary -> critic -> reconcile ->
leakage-scan -> redline chain.

This module now drives THAT path, fully offline: each fixture supplies a
synthetic document and a set of CANNED model responses (the
`FakeBedrockClient` pattern, `backend/src/model_client.py` -- no live
Bedrock, no network), and the harness scores the pipeline's OUTPUT CONTRACT
(decision, issue-count bounds, quote locatability, leakage blocking)
against the fixture's declared expectations. It does not re-implement any
review RULE -- it judges what the composed pipeline actually produced,
which is the only way to test the LLM-native path without smuggling
detector logic back in through the back door.

A live judged-NL run against a real model (`docs/evaluation.md`'s "Full
stochastic gate") remains a separate, human-executed step -- this harness
proves the MECHANICAL contract holds (decision fidelity to what the canned
model said, quotes locate, leakage blocks), not that a real model's
judgment is good.

Of the two retired modules named above, only the detector engine still
exists: it remains fully alive for its OTHER production consumers
(`tests/lint-gold-fixtures.py`, `tests/lint-acceptable-variations.py`,
`scripts/replacement_text_enforcement.py`). The standard-form line-diff
was deleted outright by issue #631, together with the anchor-map builder
and the form-match router that were its last consumers. Either way this
module imports neither.

Neither retired module is spelled by its module name anywhere in this
file, deliberately: `tests/test_eval_harness.py`'s check 2 greps this
whole source file -- prose included, not just its imports -- for those two
names, so naming one here fails the gate. See `scripts/review_spine.py`'s
docstring for the detector engine's exact module path.

## Fixture shape (schema "llm-native-v1")

Each gold fixture is a JSON file with:

    {
      "case_id": "<unique id>",
      "schema": "llm-native-v1",
      "document": {
        "clauses": [
          {"heading": "<optional section heading>", "text": "<clause prose>"},
          ...
        ]
      },
      "model_responses": {
        "primary": [ <output-schema-v1 dict>, ... ],
        "critic":  [ <output-schema-v1 dict>, ... ]
      },
      "expected": {
        "status": "OK" | "ERROR",  # default "OK"
        "reason": "<review_spine.run_review's reason token>",   # checked iff present
        "decision": "ACCEPT" | "REQUEST_CHANGE" | null,   # checked iff present
        "min_issues": <int>,                              # checked iff present
        "max_issues": <int>,                               # checked iff present
        "edits_must_apply": <bool>,                         # default true
                                                            # (v2 spelling:
                                                            #  quotes_must_locate)
        "issues": [                                       # checked iff present
          {"playbook_topic_id": "<id>", "section_ref": "<ref>",
           "hard": <bool>},                               # hard: default false
          ...
        ],
        "redline_inserts": {                              # checked iff present
          "include": ["<text the redline must insert>", ...],
          "exclude": ["<text the redline must not insert>", ...]
        }
      }
    }

`document.clauses` is rendered into a minimal, dependency-free OOXML
`.docx` (the same zipfile+ElementTree convention as
`tests/test_review_spine.py`) -- one
heading paragraph (if given) and one body paragraph per clause. `model_
responses.primary` / `.critic` are JSON-serialized in order into a
`model_client.FakeBedrockClient` queue keyed by the scored playbook's own
`primary_model_id` / `critic_model_id`, so a well-formed case needs exactly
one canned response per pass (no retry).

The scorer (`score_case`) then runs `review_spine.run_review(docx_bytes,
playbook, fake_client, ...)` -- the REAL composed pipeline, unmodified --
and checks the fixture's `expected` block against the actual `ReviewResult`:

  - **Decision fidelity**: `status` / `decision` match `expected` exactly.
  - **Issue-count bounds**: `len(findings)` within `[min_issues, max_issues]`.
  - **Addressing**: every proposed edit actually reaches the document,
    unless `edits_must_apply` (or its v2-era spelling `quotes_must_locate`)
    is explicitly `false`. Under the v3 block-transcript contract that means
    the result reports NO `analysis_report.changes_not_applied` entries --
    a transcript that names the wrong block, or misquotes the source, lands
    there. Issue #628 removed the v2 half of this check along with the
    locator it called: no fixture can carry a verbatim address any more,
    because no schema defines one.
  - **Floor obligations**: a fixture whose canned primary/critic output
    marks a hard-rejection-shaped issue REQUEST_CHANGE must reconcile to a
    final `decision="REQUEST_CHANGE"` -- this is the ordinary decision-
    fidelity check above, exercised by the "reject-*" fixture category; the
    Floor block a v1 review renders is soft, in-prompt guidance
    (`primary_review_pass.render_floor_block`), so "the obligation was
    violated" is exactly "the canned model said REQUEST_CHANGE for a
    hard-rejection topic" -- there is no separate deterministic Floor gate
    for a v1 bundle (that only exists for an OPF-governed review,
    `scripts/floor_judge.py`, issue #479, out of scope for this offline
    mechanical harness).
  - **Leakage blocking**: a fixture whose canned output plants confidential
    playbook text (a `hard_rejections[].description`, never surfaced to a
    counterparty) into a human-surfaced field must reconcile to
    `status="ERROR"` -- `review_spine.run_review`'s
    own leakage gate (`scripts/leakage_scan.py`) does the actual detection;
    the harness asserts the fixture's expected terminal status was reached
    AND, when the fixture also declares `expected.reason`, that the
    SPECIFIC cause matches too (`reason="leakage_detected"` for a
    leakage-planted fixture) -- `status` alone does not distinguish the
    leakage gate from any other fail-closed path that lands on the same
    terminal status, so a fixture proving leakage blocking must pin the
    reason, not just the status.

  - **Issue precision and recall** (issue #141): `expected.issues` is the
    gold answer -- the multiset of `(playbook_topic_id, section_ref)` the
    final findings must carry, nothing more and nothing less. A finding the
    answer does not list is a false positive, a listed issue the findings
    lack is a false negative, and either fails the case. An entry marked
    `"hard": true` is a hard rejection; missing one is also counted as a
    missed hard rejection, the metric whose target is zero.
  - **Redline-patch correctness** (issue #141): every
    `expected.redline_inserts.include` string must be inserted (inside a
    `<w:ins>`) by the delivered redline, and no `.exclude` string may be.
    This is the anchoring check for ADR 0001: a case whose primary pass
    proposes the wrong words and whose critic corrects them lists the
    critic's words under `include` and the primary's under `exclude`, so a
    pipeline that ships the primary's text fails it.

`aggregate_metrics` turns the per-case counts into the gold-set metrics
`docs/evaluation.md` defines (decision accuracy, issue precision and recall,
missed hard rejections, redline-patch correctness), and `check_floors`
compares them with `REGRESSION_FLOORS` -- the documented floor, recorded in
`docs/evaluation.md` → "Critic-wins regression gate (issue #141)" beside the
pre-ADR-0001 baseline. `tests/test_eval_gate_critic_wins.py` pins the two
together.

A JSON file in a scored `fixtures_dir` that does NOT carry
`"schema": "llm-native-v1"` is not an eval_harness case -- `score_case`
returns a trivial PASS with a "skipped" reason, exactly like a detector-era
fixture that was skipped because it carried no `detector_expectation`. This
is how the still-detector-shaped fixtures in `tests/gold-fixtures/`
(scored by `tests/lint-gold-fixtures.py` and the `tests/detector/`
conformance suite, see issue #219) coexist in the same directory as this
issue's ported `llm-*.json` fixtures without either format interpreting
the other's files.

## Ported / retired fixtures

Six representative scenarios were ported from the existing detector gold-
fixture corpus into this new shape (`tests/gold-fixtures/llm-*.json`):
a clean verbatim draft, an accepted narrow-mutual-IP-indemnification
variation, a near-miss non-exclusivity restatement, a single planted
uncapped-liability rejection, a two-issue interaction, and a leakage-
planted case. The other ~34 fixtures in `tests/gold-fixtures/` are
detector-mechanics fixtures with no LLM-native analogue (span-level
exemption combined-hunk cases, match-mode/fire_on/match_surface probes,
mechanically-generated one-trigger-term-per-rule cases) -- they are left
completely alone, still scored by `tests/lint-gold-fixtures.py` and
exercised directly by `tests/detector/test_span_level_exemption_212.py` /
`tests/detector/test_match_mode_and_semantics_220.py` (issue #400 addendum:
those two files call the shared detector module directly per fixture now,
instead of through this module).

## Playbook-profile-conditional CLI (issue #288, unchanged by this issue)

`main()` still iterates every registered `playbook_id` and SKIPs a
"knowledge" profile entry (no `anchor_map_path` / `section_config_path`) --
that split was never really about detector-vs-LLM scoring, but
`tests/test_registry_profiles.py` pins the exact "SKIP (knowledge
profile)" print + exit-0 contract for this module's `main()`, so it is
preserved unchanged here. The shipped default playbook ("synthetic-nda-
sample") is a knowledge-profile entry and is therefore SKIPped by a bare
`python3 scripts/eval_harness.py` run; the ported `llm-*.json` fixtures
live under "synthetic-generic" (a "precision" profile, `test_only` entry),
so the default self-run does exercise them. Use `--playbook-id <id>` to
score one playbook_id explicitly regardless of its profile.

CLI usage:
    python3 scripts/eval_harness.py                       # every registered playbook_id
    python3 scripts/eval_harness.py --quiet                # summary line only
    python3 scripts/eval_harness.py --playbook-id synthetic-generic

Exit codes: 0 = every scored case PASSed, 1 = at least one case FAILed.
"""

from __future__ import annotations

import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402
import review_spine  # noqa: E402

BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC_DIR))

import model_client  # noqa: E402

# Back-compat literals: resolved for the DEFAULT playbook_id, so a caller
# that passes no arguments (or imports these names directly) keeps working
# unchanged -- see playbook_registry.py's DEFAULT_PLAYBOOK_ID docstring for
# why this is late-resolved against the CURRENT registry rather than a
# hard-coded path. A specific playbook_id's fixtures/playbook are resolved
# fresh via playbook_registry -- see load_playbook()/load_gold_cases()/
# score_all() below.
_DEFAULT_ENTRY = playbook_registry.resolve_playbook(playbook_registry.DEFAULT_PLAYBOOK_ID)
PLAYBOOK_PATH = _DEFAULT_ENTRY.playbook_path
FIXTURES_PATH = _DEFAULT_ENTRY.fixtures_dir

# The schema marker that distinguishes an eval_harness-owned fixture from a
# detector-era fixture sharing the same directory -- see module docstring
# "Fixture shape" above.
SCHEMA_MARKER = "llm-native-v1"


# ---------------------------------------------------------------------------
# Minimal, dependency-free OOXML .docx builder -- same convention as
# tests/test_review_spine.py. No
# python-docx needed to write a minimal valid body.
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


def _heading_p(text: str) -> str:
    # Clause text is arbitrary fixture prose (routine contract language like
    # "R&D costs" or "<sole> discretion" is common) and must be XML-escaped
    # before interpolation into <w:t> -- an unescaped &, <, or > produces a
    # malformed word/document.xml that raises xml.etree.ElementTree.ParseError
    # deep inside review_spine.run_review's extraction stage instead of
    # failing just this one case.
    return f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{_xml_escape(text)}</w:t></w:r></w:p>'


def _body_p(text: str) -> str:
    return f"<w:p><w:r><w:t>{_xml_escape(text)}</w:t></w:r></w:p>"


def _build_docx_bytes(body_paragraphs_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {_DOC_NS}><w:body>{body_paragraphs_xml}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def build_document(clauses: list[dict[str, Any]]) -> tuple[bytes, list[dict[str, Any]]]:
    """Render a fixture's `document.clauses` into (docx_bytes, shown_paragraphs).

    `shown_paragraphs` is one `{"heading": "", "text": ...}` entry per
    `<w:p>` element written (a heading paragraph, if the clause has one,
    then its body paragraph) -- the RAW pre-extraction view, useful for
    asserting this function rendered a clause's text unmodified. It does
    NOT match what `extraction_normalization_stage.extract_and_normalize`
    shows the model: that stage merges a clause's heading and body into a
    single paragraph (heading text becomes the `heading` field, section
    number stripped) rather than keeping them as two separate entries. Any
    check about what the model was actually shown must therefore re-derive
    the pipeline's own paragraph list from `docx_bytes`, never assert
    against `shown_paragraphs` -- see `score_case`.
    """
    parts: list[str] = []
    shown: list[dict[str, Any]] = []
    for clause in clauses:
        heading = clause.get("heading") or ""
        text = clause["text"]
        if heading:
            parts.append(_heading_p(heading))
            shown.append({"heading": "", "text": heading})
        parts.append(_body_p(text))
        shown.append({"heading": "", "text": text})
    return _build_docx_bytes("".join(parts)), shown


# ---------------------------------------------------------------------------
# Gold-case loading
# ---------------------------------------------------------------------------

@dataclass
class GoldCase:
    """A single gold fixture. Only fixtures carrying `"schema": "llm-native-
    v1"` (see module docstring) are eval_harness cases -- any other JSON
    file in a scored fixtures_dir (e.g. a detector-era fixture) is loaded
    but skipped by score_case(), never interpreted under this schema."""

    case_id: str
    path: Path
    raw: dict[str, Any]

    @property
    def is_eval_harness_case(self) -> bool:
        return self.raw.get("schema") == SCHEMA_MARKER

    @property
    def clauses(self) -> list[dict[str, Any]]:
        return self.raw.get("document", {}).get("clauses", [])

    @property
    def model_responses(self) -> dict[str, list[dict[str, Any]]]:
        return self.raw.get("model_responses", {})

    @property
    def expected(self) -> dict[str, Any]:
        return self.raw.get("expected", {})


def load_playbook(path: Path = PLAYBOOK_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_gold_cases(fixtures_dir: Path = FIXTURES_PATH) -> list[GoldCase]:
    cases: list[GoldCase] = []
    for fixture_path in sorted(fixtures_dir.glob("*.json")):
        with open(fixture_path, encoding="utf-8") as f:
            raw = json.load(f)
        case_id = raw.get("case_id", fixture_path.stem)
        cases.append(GoldCase(case_id=case_id, path=fixture_path, raw=raw))
    return cases


def build_fake_model_client(
    playbook: dict[str, Any], model_responses: dict[str, list[dict[str, Any]]]
) -> "model_client.FakeBedrockClient":  # noqa: UP037
    """Queue a fixture's canned `model_responses.primary` / `.critic`
    dicts, JSON-serialized in order, under the SCORED playbook's own
    `primary_model_id` / `critic_model_id` -- exactly how `review_spine
    .run_review` resolves which queue to pop from for each pass."""
    metadata = playbook.get("playbook", {}).get("metadata", {})
    primary_id = metadata.get("primary_model_id") or model_client.primary_model_id()
    critic_id = metadata.get("critic_model_id") or model_client.critic_model_id()
    return model_client.FakeBedrockClient(
        {
            primary_id: [json.dumps(r) for r in model_responses.get("primary", [])],
            critic_id: [json.dumps(r) for r in model_responses.get("critic", [])],
        }
    )


# ---------------------------------------------------------------------------
# Comparator / scorer -- drives the REAL LLM-native pipeline
# (scripts/review_spine.py::run_review), offline, via FakeBedrockClient.
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    """One case's verdict plus the counts `aggregate_metrics` sums (issue
    #141). `scored` is False for a skipped (non-eval_harness) fixture, whose
    counts stay zero and which `aggregate_metrics` leaves out entirely."""

    case_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    scored: bool = False
    decision_correct: bool = False
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    hard_expected: int = 0
    hard_missed: int = 0
    redline_checks: int = 0
    redline_checks_passed: int = 0


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def redline_inserted_texts(redline_bytes: bytes | None) -> list[str]:
    """The text each body paragraph of a delivered redline INSERTS: per
    `<w:p>`, every `<w:t>` under a `<w:ins>`, joined in document order.
    Deleted text (`<w:delText>`) and unchanged runs are not included, so "the
    redline carries X" means the redline proposes X, not merely that X is
    somewhere in the document."""
    if not redline_bytes:
        return []
    with zipfile.ZipFile(io.BytesIO(redline_bytes)) as archive:
        # The redline is the pipeline's own output, written from a document
        # this harness built, not an uploaded file.
        root = ET.fromstring(archive.read("word/document.xml"))  # noqa: S314
    inserted: list[str] = []
    for paragraph in root.iter(f"{_W_NS}p"):
        text = "".join(
            node.text or ""
            for ins in paragraph.iter(f"{_W_NS}ins")
            for node in ins.iter(f"{_W_NS}t")
        )
        if text:
            inserted.append(text)
    return inserted


def _issue_identity(issue: dict[str, Any]) -> tuple[Any, Any]:
    # The identity reconciliation itself joins the two passes on
    # (scripts/reconciliation.py `_issue_key`): what the issue is about and
    # where, never the response-local `issue_key`.
    return (issue.get("playbook_topic_id"), issue.get("section_ref"))


def score_case(case: GoldCase, playbook: dict[str, Any]) -> CaseResult:
    """Score a single gold case: run it through `review_spine.run_review`
    (the real, composed LLM-native pipeline) with a FakeBedrockClient
    seeded from the fixture's canned responses, then check the fixture's
    `expected` block against the actual result -- see module docstring
    "Fixture shape" for the full contract."""
    if not case.is_eval_harness_case:
        return CaseResult(
            case_id=case.case_id,
            passed=True,
            reasons=[f"not an eval_harness case (schema != {SCHEMA_MARKER!r}); skipped"],
        )

    expected = case.expected
    docx_bytes, shown_paragraphs = build_document(case.clauses)  # noqa: RUF059
    fake_client = build_fake_model_client(playbook, case.model_responses)

    try:
        result = review_spine.run_review(
            docx_bytes, playbook, fake_client, review_id=case.case_id
        )
    except Exception as exc:  # noqa: BLE001
        # run_review is documented to fail closed to an ERROR-status
        # result (with a reason token) for an EXPECTED bad outcome, but a fixture
        # whose canned response makes the pipeline RETRY a pass (e.g. a
        # model-output validation failure) can exhaust the fixture's seeded
        # FakeBedrockClient queue and raise model_client.
        # FakeBedrockClientExhausted -- or any other unexpected exception --
        # straight out of run_review. One malformed fixture must not abort
        # scoring of every other fixture in the run; record this as a FAIL
        # for this case alone and keep going.
        #
        # Issue #141: the case still counts in the gold-set metrics, as a
        # wrong decision that found nothing -- every expected issue missed,
        # every redline check failed -- so a crash can never improve them.
        gold = expected.get("issues") or []
        redline_expectation = expected.get("redline_inserts") or {}
        redline_checks = len(redline_expectation.get("include") or []) + len(
            redline_expectation.get("exclude") or []
        )
        return CaseResult(
            case_id=case.case_id,
            passed=False,
            reasons=[f"run_review raised {type(exc).__name__}: {exc}"],
            scored=True,
            false_negatives=len(gold),
            hard_expected=sum(1 for issue in gold if issue.get("hard")),
            hard_missed=sum(1 for issue in gold if issue.get("hard")),
            redline_checks=redline_checks,
        )

    reasons: list[str] = []
    passed = True
    counts = CaseResult(case_id=case.case_id, passed=True, scored=True)

    expected_status = expected.get("status", "OK")
    counts.decision_correct = result.get("status") == expected_status and (
        "decision" not in expected or result.get("decision") == expected["decision"]
    )
    if result.get("status") != expected_status:
        passed = False
        reasons.append(
            f"status mismatch: expected {expected_status!r}, got "
            f"{result.get('status')!r} (reason={result.get('reason')!r})"
        )

    # An optional `expected.reason` check (issue #400 fix-round-1): checking
    # `status` alone means ANY cause of e.g. ERROR
    # satisfies a fixture whose whole point is proving ONE specific cause
    # (e.g. the leakage gate, reason="leakage_detected") actually fired --
    # the fixture staying green would silently stop proving that if some
    # other, unrelated fail-closed path started producing the same status.
    if "reason" in expected and result.get("reason") != expected["reason"]:
        passed = False
        reasons.append(
            f"reason mismatch: expected {expected['reason']!r}, got "
            f"{result.get('reason')!r} (status={result.get('status')!r})"
        )

    if "decision" in expected and result.get("decision") != expected["decision"]:
        passed = False
        reasons.append(
            f"decision mismatch: expected {expected['decision']!r}, got "
            f"{result.get('decision')!r}"
        )

    findings = result.get("findings") or []
    min_issues = expected.get("min_issues")
    max_issues = expected.get("max_issues")
    if min_issues is not None and len(findings) < min_issues:
        passed = False
        reasons.append(f"expected >= {min_issues} issue(s), got {len(findings)}")
    if max_issues is not None and len(findings) > max_issues:
        passed = False
        reasons.append(f"expected <= {max_issues} issue(s), got {len(findings)}")

    if expected.get("edits_must_apply", expected.get("quotes_must_locate", True)):
        # Issue #627: the ADDRESSING check.
        #
        # Under v3 an issue names no quote -- it names a `block_id` and
        # transcribes that block, and the pipeline proves the transcript
        # against the document's own bytes. The equivalent of "the quote
        # located" is therefore "the edit was actually applied", which the
        # result reports directly: `redline_generate` lists every edit it
        # could not compile under `analysis_report.changes_not_applied`. A
        # fixture whose transcript addresses the wrong block, or misquotes
        # the source, lands there -- so this catches exactly the class the
        # locate check used to.
        not_applied = ((result.get("analysis_report") or {}).get("changes_not_applied")) or []
        for entry in not_applied:
            passed = False
            reasons.append(
                "a proposed edit was not applied to the document "
                f"(reason={entry.get('reason')!r}, section_ref={entry.get('section_ref')!r})"
            )

    if "issues" in expected:
        # Issue #141: precision and recall against the case's gold answer.
        gold = expected["issues"]
        wanted = Counter(_issue_identity(issue) for issue in gold)
        found = Counter(_issue_identity(issue) for issue in findings)
        matched = wanted & found
        counts.true_positives = sum(matched.values())
        counts.false_positives = sum((found - wanted).values())
        counts.false_negatives = sum((wanted - found).values())
        for key in sorted((found - wanted).elements(), key=repr):
            passed = False
            reasons.append(f"unexpected issue (false positive): {key!r}")
        for key in sorted((wanted - found).elements(), key=repr):
            passed = False
            reasons.append(f"expected issue not in findings (false negative): {key!r}")
        hard_wanted = Counter(_issue_identity(issue) for issue in gold if issue.get("hard"))
        counts.hard_expected = sum(hard_wanted.values())
        counts.hard_missed = sum((hard_wanted - found).values())

    redline_expectation = expected.get("redline_inserts")
    if redline_expectation is not None:
        # Issue #141: the anchoring check -- whose words the redline inserts.
        inserted = redline_inserted_texts(result.get("redline_bytes"))
        for text in redline_expectation.get("include") or []:
            counts.redline_checks += 1
            if any(text in paragraph for paragraph in inserted):
                counts.redline_checks_passed += 1
            else:
                passed = False
                reasons.append(f"the redline does not insert the expected text: {text!r}")
        for text in redline_expectation.get("exclude") or []:
            counts.redline_checks += 1
            if any(text in paragraph for paragraph in inserted):
                passed = False
                reasons.append(f"the redline inserts text it must not: {text!r}")
            else:
                counts.redline_checks_passed += 1

    counts.passed = passed
    counts.reasons = reasons
    return counts


# ---------------------------------------------------------------------------
# Gold-set metrics and the regression floor (issue #141).
# ---------------------------------------------------------------------------

#: The documented floor every metric must meet on the gold set
#: (docs/evaluation.md → "Critic-wins regression gate (issue #141)", which
#: `tests/test_eval_gate_critic_wins.py` pins against this dict). The harness
#: is offline and deterministic -- the canned answers are known -- so any
#: drop below 1.0 is a regression in the pipeline, not model noise.
#: `missed_hard_rejections` is a ceiling, not a floor: zero, per the
#: document's "zero new missed hard rejections".
REGRESSION_FLOORS: dict[str, float] = {
    "decision_accuracy": 1.0,
    "issue_precision": 1.0,
    "issue_recall": 1.0,
    "missed_hard_rejections": 0,
    "redline_patch_correctness": 1.0,
}


@dataclass(frozen=True)
class GoldSetMetrics:
    cases_scored: int
    decision_accuracy: float
    issue_precision: float
    issue_recall: float
    hard_rejections_expected: int
    missed_hard_rejections: int
    redline_patch_correctness: float

    def as_dict(self) -> dict[str, float]:
        return {
            "cases_scored": self.cases_scored,
            "decision_accuracy": self.decision_accuracy,
            "issue_precision": self.issue_precision,
            "issue_recall": self.issue_recall,
            "hard_rejections_expected": self.hard_rejections_expected,
            "missed_hard_rejections": self.missed_hard_rejections,
            "redline_patch_correctness": self.redline_patch_correctness,
        }


def _ratio(numerator: int, denominator: int) -> float:
    # An empty denominator means nothing could go wrong: no flag to be false,
    # no issue to miss, no redline check to fail.
    return 1.0 if denominator == 0 else numerator / denominator


def aggregate_metrics(results: list[CaseResult]) -> GoldSetMetrics:
    """Sum the scored cases' counts into the gold-set metrics. Skipped
    (non-eval_harness) fixtures are left out."""
    scored = [r for r in results if r.scored]
    tp = sum(r.true_positives for r in scored)
    fp = sum(r.false_positives for r in scored)
    fn = sum(r.false_negatives for r in scored)
    return GoldSetMetrics(
        cases_scored=len(scored),
        decision_accuracy=_ratio(sum(1 for r in scored if r.decision_correct), len(scored)),
        issue_precision=_ratio(tp, tp + fp),
        issue_recall=_ratio(tp, tp + fn),
        hard_rejections_expected=sum(r.hard_expected for r in scored),
        missed_hard_rejections=sum(r.hard_missed for r in scored),
        redline_patch_correctness=_ratio(
            sum(r.redline_checks_passed for r in scored),
            sum(r.redline_checks for r in scored),
        ),
    )


def check_floors(
    metrics: GoldSetMetrics, floors: dict[str, float] | None = None
) -> list[str]:
    """Every way `metrics` misses the documented floor, as one line each;
    empty when the gold set meets it."""
    floors = REGRESSION_FLOORS if floors is None else floors
    values = metrics.as_dict()
    breaches: list[str] = []
    for name, floor in floors.items():
        value = values[name]
        if name == "missed_hard_rejections":
            if value > floor:
                breaches.append(f"{name} = {value}, above the ceiling of {floor}")
        elif value < floor:
            breaches.append(f"{name} = {value:.4f}, below the floor of {floor}")
    return breaches


def score_all(
    fixtures_dir: Path = FIXTURES_PATH, playbook_path: Path = PLAYBOOK_PATH
) -> list[CaseResult]:
    playbook = load_playbook(playbook_path)
    cases = load_gold_cases(fixtures_dir)
    return [score_case(case, playbook) for case in cases]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_harness_gate_for_playbook(entry, quiet: bool = False) -> bool:
    """Run the eval-harness gate (score every gold fixture in this entry's
    fixtures_dir) for a single registry entry. Returns True on PASS.
    Detector-era fixtures sharing the directory score a trivial skip-PASS
    (GoldCase.is_eval_harness_case), never a failure."""
    results = score_all(fixtures_dir=entry.fixtures_dir, playbook_path=entry.playbook_path)
    failed = [r for r in results if not r.passed]

    metrics = aggregate_metrics(results)
    breaches = check_floors(metrics)

    if not quiet:
        print(f"Evaluation harness [{entry.playbook_id}]: scored {len(results)} fixture(s).")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.case_id}")
            for reason in r.reasons:
                print(f"      {reason}")
        print(
            f"  metrics [{entry.playbook_id}]: "
            + ", ".join(
                f"{name}={value:.4f}" if isinstance(value, float) else f"{name}={value}"
                for name, value in metrics.as_dict().items()
            )
        )
        for breach in breaches:
            print(f"  [FLOOR] {breach}")

    ok = not failed and not breaches
    if not quiet:
        print("PASS" if ok else "FAIL", f"[{entry.playbook_id}]\n")
    return ok


def main(argv: list[str] | None = None) -> int:
    """Eval-harness CLI (issue #400; profile-conditional per issue #288 --
    see module docstring "Playbook-profile-conditional CLI" above).

    With no `--playbook-id`, iterates every registered playbook_id: a
    "knowledge" profile entry is explicitly SKIPped (printed, never
    silent) and counted as skipped-not-passed; a "precision" profile entry
    runs the full gate. `--playbook-id` still selects exactly one entry,
    skipping the profile check -- an explicit request always runs.
    """
    argv = argv if argv is not None else sys.argv[1:]
    quiet = "--quiet" in argv

    if "--playbook-id" in argv:
        idx = argv.index("--playbook-id")
        entry = playbook_registry.resolve_playbook(argv[idx + 1])
        ok = run_harness_gate_for_playbook(entry, quiet=quiet)
        if not quiet:
            print("\nPASS" if ok else "\nFAIL")
        return 0 if ok else 1

    playbook_ids = playbook_registry.list_playbook_ids()
    skipped: list[str] = []
    ran: list[str] = []
    all_ok = True

    for playbook_id in playbook_ids:
        entry = playbook_registry.resolve_playbook(playbook_id)
        prof = playbook_registry.profile(entry)
        if prof == "knowledge":
            print(f"SKIP (knowledge profile): eval harness gate {playbook_id}")
            skipped.append(playbook_id)
            continue
        ran.append(playbook_id)
        if not run_harness_gate_for_playbook(entry, quiet=quiet):
            all_ok = False

    if not quiet:
        print(
            f"Eval harness summary: {len(ran)} playbook(s) scored, "
            f"{len(skipped)} skipped (knowledge profile): {skipped}"
        )
        print("\nPASS" if all_ok else "\nFAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
