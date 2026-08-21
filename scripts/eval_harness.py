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

The two retired modules named above remain fully alive for their OTHER
production consumers (`tests/lint-gold-fixtures.py`,
`tests/lint-acceptable-variations.py`, `scripts/form_match_router.py`,
`scripts/build_anchor_map.py`, `scripts/replacement_text_enforcement.py`,
`scripts/third_party_position_findings.py`) -- this module no longer
imports either one; see `scripts/review_spine.py`'s docstring for their
exact module paths.

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
        "status": "OK" | "MANUAL_REVIEW_REQUIRED" | "ERROR_MANUAL_REVIEW_REQUIRED",  # default "OK"
        "reason": "<review_spine.run_review's reason token>",   # checked iff present
        "decision": "ACCEPT" | "REQUEST_CHANGE" | null,   # checked iff present
        "min_issues": <int>,                              # checked iff present
        "max_issues": <int>,                               # checked iff present
        "quotes_must_locate": <bool>                        # default true
      }
    }

`document.clauses` is rendered into a minimal, dependency-free OOXML
`.docx` (the same zipfile+ElementTree convention as
`scripts/redline_docx_writer.py` / `tests/test_review_spine.py`) -- one
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
  - **Quote locatability**: every finding's `source_quote` locates in the
    document via the SAME entry point the real pipeline patches from
    (`scripts/quote_locate.py::locate_quote` -- a verification utility, not
    a review rule) unless `quotes_must_locate` is explicitly `false`.
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
    `status="ERROR_MANUAL_REVIEW_REQUIRED"` -- `review_spine.run_review`'s
    own leakage gate (`scripts/leakage_scan.py`) does the actual detection;
    the harness asserts the fixture's expected terminal status was reached
    AND, when the fixture also declares `expected.reason`, that the
    SPECIFIC cause matches too (`reason="leakage_detected"` for a
    leakage-planted fixture) -- `status` alone does not distinguish the
    leakage gate from any other fail-closed path that lands on the same
    terminal status, so a fixture proving leakage blocking must pin the
    reason, not just the status.

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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import playbook_registry  # noqa: E402
import quote_locate  # noqa: E402
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
# tests/test_review_spine.py / scripts/redline_docx_writer.py. No
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
    number stripped) rather than keeping them as two separate entries. A
    `source_quote` must therefore be verified via `quote_locate.locate_quote`
    (which re-derives the real pipeline's paragraph list from `docx_bytes`
    itself), never against `shown_paragraphs` -- see `score_case`.
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
) -> "model_client.FakeBedrockClient":
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
    case_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)


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
    docx_bytes, shown_paragraphs = build_document(case.clauses)
    fake_client = build_fake_model_client(playbook, case.model_responses)

    try:
        result = review_spine.run_review(
            docx_bytes, playbook, fake_client, review_id=case.case_id
        )
    except Exception as exc:  # noqa: BLE001
        # run_review is documented to fail closed to a MANUAL_REVIEW_
        # REQUIRED-shaped result for an EXPECTED bad outcome, but a fixture
        # whose canned response makes the pipeline RETRY a pass (e.g. a
        # model-output validation failure) can exhaust the fixture's seeded
        # FakeBedrockClient queue and raise model_client.
        # FakeBedrockClientExhausted -- or any other unexpected exception --
        # straight out of run_review. One malformed fixture must not abort
        # scoring of every other fixture in the run; record this as a FAIL
        # for this case alone and keep going.
        return CaseResult(
            case_id=case.case_id,
            passed=False,
            reasons=[f"run_review raised {type(exc).__name__}: {exc}"],
        )

    reasons: list[str] = []
    passed = True

    expected_status = expected.get("status", "OK")
    if result.get("status") != expected_status:
        passed = False
        reasons.append(
            f"status mismatch: expected {expected_status!r}, got "
            f"{result.get('status')!r} (reason={result.get('reason')!r})"
        )

    # An optional `expected.reason` check (issue #400 fix-round-1): checking
    # `status` alone means ANY cause of e.g. ERROR_MANUAL_REVIEW_REQUIRED
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

    if expected.get("quotes_must_locate", True):
        for finding in findings:
            quote = finding.get("source_quote")
            if not quote:
                continue
            # issue #400 review-round-3 fix: locate against the PIPELINE's own
            # view (re-extracted from docx_bytes), not `shown_paragraphs` --
            # extraction_normalization_stage merges a clause's heading and
            # body into one paragraph, so the two-entries-per-clause raw view
            # `build_document` returns disagrees with what the model was
            # actually shown, and disagrees with what
            # scripts/redline_quote_apply.py's real locate call sees.
            located = quote_locate.locate_quote(docx_bytes, quote)
            if located["status"] != "found":
                passed = False
                reasons.append(
                    f"source_quote does not locate in the shown document text "
                    f"(status={located['status']!r}): {quote!r}"
                )

    return CaseResult(case_id=case.case_id, passed=passed, reasons=reasons)


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

    if not quiet:
        print(f"Evaluation harness [{entry.playbook_id}]: scored {len(results)} fixture(s).")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.case_id}")
            for reason in r.reasons:
                print(f"      {reason}")

    ok = not failed
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
