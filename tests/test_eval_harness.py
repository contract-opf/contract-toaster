#!/usr/bin/env python3
"""
Tests for the LLM-native evaluation harness -- issue #400.

Replaces tests/test_eval_harness_62.py and tests/test_eval_harness_quality_
204.py, both deleted by this issue. MOST checks in those two files exercised
the retired deterministic-detector scoring path (GoldCase.is_detector_case /
run_detectors_on_case / missing_rule_coverage / score_document_level_case /
run_smoke_tier_model_pass), superseded by this file and by
scripts/review_spine.py::run_review being the real composed pipeline the
harness now drives end to end. The CI-eval-budget checks those two files
also carried (unrelated to detector scoring) are ported, unchanged, to
tests/test_eval_budget.py. Not every remaining check in those two files fits
either bucket, though -- see below for the three that don't and are ported
here instead.

Three checks from the two deleted files are NOT superseded by the eval_
harness.py rewrite -- they pin corpus-quality / documentation invariants
independent of which pipeline eval_harness.py drives -- and are ported here
unchanged (issue #400 fix-round-1/2, addendum "relocate the coverage, don't
delete it"):
  - the generated-fixture provenance guard (from tests/test_eval_harness_
    62.py: every fixture with generated_by == "scripts/generate_gold_
    fixtures.py" must carry provenance: "synthetic" -- the de-
    identification sign-off gate in tests/test_github_threatmodel_deident.py
    defaults a missing provenance field to "synthetic" and exempts it from
    GC sign-off, so this check is what keeps that exemption tag honest),
  - the generator-tautology guard (a hand-authored near_miss/injection
    fixture the mechanical generator cannot produce must still exist,
    alongside the generated subset), and
  - the docs/evaluation.md v1-baseline-table relabel guard (the table must
    never silently revert to being cited as "Recorded baseline" evidence).

What this file asserts:
  1. scripts/eval_harness.py exposes the runner + comparator/scorer API.
  2. scripts/eval_harness.py imports neither the retired detector module nor
     the retired standard-form diff module (the acceptance criteria's grep
     check, pinned here too so a regression fails the unit gate, not just a
     manual grep).
  3. The ported llm-native-v1 fixtures in tests/gold-fixtures/ all score
     PASS through the real pipeline (scripts/review_spine.py::run_review),
     and every detector-era fixture sharing that directory is skipped, not
     silently mis-scored.
  4. A deliberately-broken fixture (decision mismatch, non-locating
     source_quote, an XML-metacharacter clause, a run_review exception, an
     expected.reason mismatch) is caught in each case: the harness actually
     FAILS a case that violates its expected output contract, not just
     PASSes everything by construction (the ticket's own acceptance-criteria
     demand).
  5. A non-eval_harness-schema fixture is a trivial skip-PASS.
  6. The profile-conditional main() CLI still runs green end-to-end.
  7. The three corpus-quality / documentation invariants ported from the two
     deleted files (see above).

Run with: python3 tests/test_eval_harness.py
Exit 0 = all checks pass; non-zero = one or more invariants not met.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import eval_harness  # noqa: E402

GOLD_FIXTURES_DIR = REPO_ROOT / "tests" / "gold-fixtures"
PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"
EVALUATION_DOC_PATH = REPO_ROOT / "docs" / "evaluation.md"

FAILURES: list[str] = []


def _check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


# ── Check 1: harness module has the runner + comparator/scorer API ──────────

def check_harness_api_present() -> list[str]:
    failures = []
    required = ["GoldCase", "load_gold_cases", "score_case", "score_all", "main", "build_document"]
    for name in required:
        if not hasattr(eval_harness, name):
            failures.append(f"  scripts/eval_harness.py is missing required API: {name}")
    return failures


# ── Check 2: no detector / standard-form-diff imports (acceptance criteria) ─

def check_no_detector_or_diff_imports() -> list[str]:
    failures = []
    source = (SCRIPTS_DIR / "eval_harness.py").read_text(encoding="utf-8")
    for banned in ("detector_common", "diff_standard_form"):
        if banned in source:
            failures.append(
                f"  scripts/eval_harness.py still references {banned!r} -- the "
                f"acceptance criteria requires this grep to return nothing."
            )
    return failures


# ── Check 3: ported llm-native-v1 fixtures all PASS; detector-era ones are
#    skipped, not silently mis-scored ─────────────────────────────────────

EXPECTED_PORTED_CASE_IDS = {
    "llm-accept-clean-draft",
    "llm-accept-narrow-mutual-ip-indemnification",
    "llm-near-miss-non-exclusive-restated",
    "llm-reject-uncapped-liability",
    "llm-reject-multi-issue-interaction",
    "llm-reject-leakage-planted",
}


def check_ported_fixtures_pass_and_detector_fixtures_skip() -> list[str]:
    failures = []
    results = {
        r.case_id: r
        for r in eval_harness.score_all(fixtures_dir=GOLD_FIXTURES_DIR, playbook_path=PLAYBOOK_PATH)
    }

    missing = EXPECTED_PORTED_CASE_IDS - set(results)
    if missing:
        failures.append(f"  expected ported llm-native-v1 fixtures not found: {sorted(missing)}")

    for case_id in sorted(EXPECTED_PORTED_CASE_IDS & set(results)):
        result = results[case_id]
        if not result.passed:
            failures.append(f"  {case_id}: scored FAIL: {result.reasons!r}")
        # issue #400 review-round-3 fix: `passed=True` alone is not proof the
        # fixture was actually scored under the llm-native-v1 contract -- a
        # fixture whose "schema" field drifts (typo, accidental edit) also
        # comes back `passed=True` via score_case's not-an-eval_harness-case
        # skip path, silently dropping it out of the scored set while this
        # loop stays green. Assert it was genuinely scored, not skipped.
        if any("skipped" in r for r in result.reasons):
            failures.append(
                f"  {case_id}: expected to be SCORED under the llm-native-v1 "
                f"contract but was skipped instead (reasons={result.reasons!r}) "
                f"-- check its \"schema\" field."
            )

    # Every OTHER fixture in the directory (the detector-era corpus) must be
    # a trivial skip-PASS, not scored under the llm-native-v1 contract.
    detector_case_ids = set(results) - EXPECTED_PORTED_CASE_IDS
    if not detector_case_ids:
        failures.append("  expected at least one detector-era fixture alongside the ported ones (none found).")
    for case_id in sorted(detector_case_ids):
        result = results[case_id]
        if not result.passed:
            failures.append(f"  detector-era fixture {case_id!r} unexpectedly scored FAIL: {result.reasons!r}")
        if not any("skipped" in r for r in result.reasons):
            failures.append(
                f"  detector-era fixture {case_id!r} was not skipped as a non-eval_harness "
                f"case (reasons={result.reasons!r}) -- it should never be interpreted under "
                f"the llm-native-v1 schema."
            )
    return failures


# ── Check 4: a deliberately-broken fixture is actually caught ───────────────

def _load_synthetic_generic_playbook() -> dict:
    return eval_harness.load_playbook(PLAYBOOK_PATH)


def _make_case(raw: dict) -> "eval_harness.GoldCase":
    return eval_harness.GoldCase(case_id=raw["case_id"], path=Path("<inline>"), raw=raw)


def check_deliberately_broken_fixture_fails_decision_mismatch() -> list[str]:
    """The canned primary/critic responses both say ACCEPT, but the fixture
    EXPECTS REQUEST_CHANGE -- a genuine output-contract violation the
    harness must catch, proving score_case() does not just PASS everything
    by construction."""
    failures = []
    playbook = _load_synthetic_generic_playbook()

    broken = {
        "case_id": "inline-broken-decision-mismatch",
        "schema": "llm-native-v1",
        "document": {"clauses": [{"heading": "8. Limitation on Liability", "text": "Liability is unlimited."}]},
        "model_responses": {
            "primary": [
                {
                    "schema_version": "output-schema-v1",
                    "decision": "ACCEPT",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    "verdict_summary": "No changes identified.",
                }
            ],
            "critic": [
                {
                    "schema_version": "output-schema-v1",
                    "decision": "ACCEPT",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    "verdict_summary": None,
                }
            ],
        },
        "expected": {"status": "OK", "decision": "REQUEST_CHANGE"},
    }

    result = eval_harness.score_case(_make_case(broken), playbook)
    if result.passed:
        failures.append(
            "  a fixture whose canned response (ACCEPT) contradicts its own "
            "expected decision (REQUEST_CHANGE) scored PASS -- the harness is "
            "not actually checking decision fidelity."
        )
    if not any("decision mismatch" in r for r in result.reasons):
        failures.append(f"  expected a 'decision mismatch' reason, got: {result.reasons!r}")
    return failures


def check_deliberately_broken_fixture_fails_quote_locate() -> list[str]:
    """The canned issue's source_quote does not appear anywhere in the shown
    document text -- the quote-locatability check must catch this."""
    failures = []
    playbook = _load_synthetic_generic_playbook()

    broken = {
        "case_id": "inline-broken-quote-locate",
        "schema": "llm-native-v1",
        "document": {"clauses": [{"heading": "8. Limitation on Liability", "text": "Liability is unlimited."}]},
        "model_responses": {
            "primary": [
                {
                    "schema_version": "output-schema-v1",
                    "decision": "REQUEST_CHANGE",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [
                        {
                            "section_ref": "sec-8",
                            "section_title": "Limitation on Liability",
                            "counterparty_change_summary": "Counterparty removed the liability cap.",
                            "decision": "REQUEST_CHANGE",
                            "external_rationale_for_footnote": "Section 8 must retain the standard cap.",
                            "proposed_replacement_text": "$150,000 mutual aggregate liability cap.",
                            "playbook_topic_id": "limitation-of-liability",
                            "internal_precedent_citation": None,
                            "provenance": "model",
                            "source_quote": "This exact sentence does not appear anywhere in the document.",
                        }
                    ],
                    "critic_delta": None,
                    "verdict_summary": "One issue identified.",
                }
            ],
            "critic": [
                {
                    "schema_version": "output-schema-v1",
                    "decision": "REQUEST_CHANGE",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    "verdict_summary": None,
                }
            ],
        },
        "expected": {"status": "OK", "decision": "REQUEST_CHANGE", "min_issues": 1, "max_issues": 1},
    }

    result = eval_harness.score_case(_make_case(broken), playbook)
    if result.passed:
        failures.append(
            "  a fixture whose canned source_quote does not locate in the shown "
            "document text scored PASS -- the quote-locatability check is not "
            "actually enforced."
        )
    if not any("does not locate" in r for r in result.reasons):
        failures.append(f"  expected a 'does not locate' reason, got: {result.reasons!r}")
    return failures


# ── Check 4c: clause text with XML metacharacters (&, <, >) is escaped,
#    not interpolated raw into <w:t> ─────────────────────────────────────

def check_clause_text_with_xml_metacharacters_does_not_raise() -> list[str]:
    """Routine contract prose contains '&' ("R&D costs") and can contain
    '<'/'>' -- interpolated raw into <w:t> these produce a malformed
    word/document.xml and score_case raises xml.etree.ElementTree.ParseError
    out of review_spine.run_review's extraction stage, aborting the whole
    harness run instead of failing one case. This must not raise, and the
    clause text must still be readable (round-tripped, not corrupted) by
    the pipeline it was fed to."""
    failures = []
    playbook = _load_synthetic_generic_playbook()

    clause_text = "R&D costs shall be borne by the Company <sole> discretion."
    case = {
        "case_id": "inline-ampersand-clause-text",
        "schema": "llm-native-v1",
        "document": {"clauses": [{"heading": "8. Limitation on Liability", "text": clause_text}]},
        "model_responses": {
            "primary": [
                {
                    "schema_version": "output-schema-v1",
                    "decision": "ACCEPT",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    "verdict_summary": "No changes identified.",
                }
            ],
            "critic": [
                {
                    "schema_version": "output-schema-v1",
                    "decision": "ACCEPT",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    "verdict_summary": None,
                }
            ],
        },
        "expected": {"status": "OK", "decision": "ACCEPT", "min_issues": 0, "max_issues": 0},
    }

    try:
        result = eval_harness.score_case(_make_case(case), playbook)
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"  score_case() raised {type(exc).__name__}: {exc} for clause text containing "
            f"'&'/'<'/'>' -- xml.sax.saxutils.escape must be applied before interpolating "
            f"clause text into <w:t> (scripts/eval_harness.py::_heading_p/_body_p)."
        )
        return failures

    if not result.passed:
        failures.append(f"  clause text containing '&'/'<'/'>' scored FAIL: {result.reasons!r}")

    docx_bytes, shown_paragraphs = eval_harness.build_document(case["document"]["clauses"])
    if not any(p["text"] == clause_text for p in shown_paragraphs):
        failures.append(
            f"  build_document() did not preserve the original clause text exactly; "
            f"shown_paragraphs={shown_paragraphs!r}"
        )
    return failures


# ── Check 4d: a fixture whose canned response drives run_review into a
#    RETRY that exhausts the seeded FakeBedrockClient queue is caught as a
#    FAIL for that case, not propagated as an uncaught exception ──────────

def check_run_review_exception_is_caught_as_a_fail() -> list[str]:
    """A canned primary response with an invalid `decision` value fails
    schema validation and drives primary_review_pass into its bounded
    retry; with only one seeded primary response, the retry's invoke()
    call finds an empty queue and model_client.FakeBedrockClientExhausted
    propagates out of review_spine.run_review. score_case must catch this
    (and any other exception from run_review) and record one FAIL
    CaseResult, not let it abort the whole harness run."""
    failures = []
    playbook = _load_synthetic_generic_playbook()

    broken = {
        "case_id": "inline-run-review-raises",
        "schema": "llm-native-v1",
        "document": {"clauses": [{"heading": "8. Limitation on Liability", "text": "Liability is unlimited."}]},
        "model_responses": {
            "primary": [
                {
                    "schema_version": "output-schema-v1",
                    "decision": "NOT_A_VALID_DECISION",
                    "confidence_state": "OK",
                    "confidence_band": None,
                    "issues": [],
                    "critic_delta": None,
                    "verdict_summary": "No changes identified.",
                }
            ],
            "critic": [],
        },
        "expected": {"status": "OK", "decision": "ACCEPT"},
    }

    try:
        result = eval_harness.score_case(_make_case(broken), playbook)
    except Exception as exc:  # noqa: BLE001
        failures.append(
            f"  score_case() let {type(exc).__name__} propagate out of run_review instead "
            f"of catching it and recording a FAIL CaseResult: {exc}"
        )
        return failures

    if result.passed:
        failures.append(
            "  a fixture that drives run_review to raise an exception (retry-queue "
            "exhaustion) scored PASS -- score_case() is not actually guarding the call."
        )
    if not any("run_review raised" in r for r in result.reasons):
        failures.append(f"  expected a 'run_review raised' reason, got: {result.reasons!r}")
    return failures


# ── Check 4e: expected.reason pins the SPECIFIC cause of a terminal status,
#    not just the status itself ─────────────────────────────────────────

def check_expected_reason_mismatch_is_caught() -> list[str]:
    """`status` alone cannot distinguish review_spine.run_review's leakage
    gate from any other fail-closed path that lands on the same terminal
    status (e.g. ERROR_MANUAL_REVIEW_REQUIRED). Monkeypatches
    review_spine.run_review to return that status for an UNRELATED reason
    and asserts score_case() catches a fixture whose expected.reason
    ('leakage_detected') no longer matches -- proving the reason check is
    real, not a no-op that only ever compares status."""
    failures = []
    playbook = _load_synthetic_generic_playbook()

    case_raw = {
        "case_id": "inline-reason-mismatch",
        "schema": "llm-native-v1",
        "document": {"clauses": [{"heading": "8. Limitation on Liability", "text": "Liability is unlimited."}]},
        "model_responses": {"primary": [], "critic": []},
        "expected": {"status": "ERROR_MANUAL_REVIEW_REQUIRED", "reason": "leakage_detected"},
    }

    original_run_review = eval_harness.review_spine.run_review

    def _fake_run_review(*args, **kwargs):
        return {
            "status": "ERROR_MANUAL_REVIEW_REQUIRED",
            "decision": None,
            "redline_bytes": None,
            "summary": None,
            "findings": [],
            "reason": "output_ooxml_scan_failed",
            "analysis_report": None,
        }

    eval_harness.review_spine.run_review = _fake_run_review
    try:
        result = eval_harness.score_case(_make_case(case_raw), playbook)
    finally:
        eval_harness.review_spine.run_review = original_run_review

    if result.passed:
        failures.append(
            "  a fixture expecting reason='leakage_detected' scored PASS when the "
            "pipeline actually failed closed for an unrelated reason "
            "('output_ooxml_scan_failed') -- status alone must not be sufficient."
        )
    if not any("reason mismatch" in r for r in result.reasons):
        failures.append(f"  expected a 'reason mismatch' reason, got: {result.reasons!r}")
    return failures


# ── Check 5: a non-eval_harness fixture (no schema marker) is a trivial
#    skip-PASS, never interpreted under the llm-native-v1 schema ───────────

def check_non_eval_harness_fixture_is_skipped() -> list[str]:
    failures = []
    playbook = _load_synthetic_generic_playbook()
    detector_shaped = {
        "case_id": "inline-detector-shaped",
        "detector_expectation": {"rule_id": "no-uncapped-liability", "expected_result": "fire"},
        "planted_variation": {"topic_id": "limitation-of-liability", "inserted_hunk": "uncapped"},
    }
    result = eval_harness.score_case(_make_case(detector_shaped), playbook)
    if not result.passed:
        failures.append(f"  a non-eval_harness-schema fixture scored FAIL instead of skip-PASS: {result.reasons!r}")
    if not any("skipped" in r for r in result.reasons):
        failures.append(f"  expected a 'skipped' reason for a non-eval_harness-schema fixture, got: {result.reasons!r}")
    return failures


# ── Check 6: the profile-conditional main() CLI runs green end-to-end ───────

def check_main_cli_runs_green() -> list[str]:
    failures = []
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            code = eval_harness.main([])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
    output = buf.getvalue()
    if code != 0:
        failures.append(f"  eval_harness.main() exited {code} (expected 0). Output:\n{output}")
    if "SKIP (knowledge profile)" not in output:
        failures.append(
            f"  eval_harness.main() did not SKIP the knowledge-profile default playbook "
            f"(synthetic-nda-sample). Output:\n{output}"
        )
    return failures


# ── Check 7a: the generator tautology is broken by a hand-authored fixture
#    (ported unchanged from tests/test_eval_harness_quality_204.py) ────────

def check_generator_tautology_is_broken_by_a_hand_authored_fixture() -> list[str]:
    """scripts/generate_gold_fixtures.py builds its planted hunk FROM the
    rule's own first/longest trigger term -- a tautology (the detector
    gate verifies a regex matches a string constructed to match it). At
    least one committed gold fixture must carry independent, hand-authored
    signal the mechanical generator cannot produce (no `generated_by`
    field, `case_type` in {"near_miss", "injection"}); this is a
    corpus-quality invariant independent of which module scores the
    fixtures, so it does not get superseded by the eval_harness.py
    rewrite."""
    failures = []
    hand_authored = []
    for path in sorted(GOLD_FIXTURES_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if "generated_by" in raw:
            continue  # mechanically generated by scripts/generate_gold_fixtures.py
        if raw.get("case_type") in ("near_miss", "injection") and raw.get("synthetic") is True:
            hand_authored.append(raw["case_id"])

    if not hand_authored:
        failures.append(
            "  No hand-authored near_miss/injection gold fixture found -- the mechanical "
            "generator (scripts/generate_gold_fixtures.py) builds its planted hunk FROM the "
            "rule's own first trigger term (a tautology); at least one fixture must carry "
            "independent signal the generator cannot produce."
        )

    # Sanity: the generator-produced fixtures (case_type absent, generated_by
    # present) must still exist alongside the hand-authored ones -- this
    # check proves the DISTINCTION is real, not that the generated set was
    # deleted.
    generated = [
        json.loads(p.read_text(encoding="utf-8"))["case_id"]
        for p in GOLD_FIXTURES_DIR.glob("*.json")
        if "generated_by" in json.loads(p.read_text(encoding="utf-8"))
    ]
    if not generated:
        failures.append("  Expected the mechanically-generated fixture subset to still be present.")
    return failures


# ── Check 7b: docs/evaluation.md's v1 baseline table stays labeled a
#    projection, never silently relabeled "Recorded baseline" (ported
#    unchanged from tests/test_eval_harness_quality_204.py) ───────────────

def check_recorded_baseline_relabeled_as_projection() -> list[str]:
    """Pins that docs/evaluation.md never re-labels the v1 critic-input-
    manifest baseline table as "Recorded baseline" / recorded evidence --
    the table remains a PROJECTION until a live stochastic gate run
    replaces it with measured numbers (docs/evaluation.md's own
    "Critic-input manifest gate" section)."""
    failures = []
    text = EVALUATION_DOC_PATH.read_text(encoding="utf-8")

    if "Recorded baseline (v1, established 2026-06-22)" in text:
        failures.append(
            "  docs/evaluation.md still labels the v1 baseline table as a 'Recorded "
            "baseline' -- it must be relabeled a PROJECTION until a live stochastic gate "
            "run replaces it."
        )

    if "projection" not in text.lower():
        failures.append(
            "  docs/evaluation.md does not contain the word 'projection' anywhere -- "
            "the v1 baseline relabel is missing."
        )
    return failures


# ── Check 7c: script-generated fixtures are tagged provenance=synthetic
#    (ported unchanged from tests/test_eval_harness_62.py) ────────────────

def check_generated_fixtures_are_synthetic() -> list[str]:
    """Every fixture mechanically generated by scripts/generate_gold_
    fixtures.py must carry provenance: "synthetic" -- the de-identification
    sign-off gate (tests/test_github_threatmodel_deident.py::
    check_3_fixture_signoff) defaults a MISSING provenance field to
    "synthetic" and exempts it from GC sign-off, so this check is what keeps
    a generated fixture from silently losing that tag and evading the
    sign-off requirement (docs/evaluation.md de-identification standard;
    issue #400 fix-round-2, addendum "relocate the coverage, don't
    delete it")."""
    failures = []
    for fixture_path in sorted(GOLD_FIXTURES_DIR.glob("*.json")):
        with open(fixture_path, encoding="utf-8") as f:
            fixture = json.load(f)
        if fixture.get("generated_by") == "scripts/generate_gold_fixtures.py":
            if fixture.get("provenance") != "synthetic":
                failures.append(
                    f"  {fixture_path.name} was mechanically generated but is not "
                    f"tagged provenance=synthetic (docs/evaluation.md de-identification "
                    f"standard only exempts synthetic fixtures from GC sign-off; "
                    f"#62 requires synthetic-only fixtures, no production data)."
                )
    return failures


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        ("1", "eval_harness.py exposes the runner + comparator/scorer API", check_harness_api_present),
        ("2", "eval_harness.py imports no detector / standard-form-diff module", check_no_detector_or_diff_imports),
        ("3", "ported llm-native-v1 fixtures PASS; detector-era fixtures skip", check_ported_fixtures_pass_and_detector_fixtures_skip),
        ("4a", "a decision-mismatch fixture is caught (FAILs)", check_deliberately_broken_fixture_fails_decision_mismatch),
        ("4b", "a non-locating source_quote fixture is caught (FAILs)", check_deliberately_broken_fixture_fails_quote_locate),
        ("4c", "clause text with '&'/'<'/'>' is escaped, not a ParseError", check_clause_text_with_xml_metacharacters_does_not_raise),
        ("4d", "run_review exceptions are caught as a per-case FAIL, not propagated", check_run_review_exception_is_caught_as_a_fail),
        ("4e", "expected.reason mismatch is caught (status alone is not enough)", check_expected_reason_mismatch_is_caught),
        ("5", "a non-eval_harness-schema fixture is a trivial skip-PASS", check_non_eval_harness_fixture_is_skipped),
        ("6", "the profile-conditional main() CLI runs green end-to-end", check_main_cli_runs_green),
        ("7a", "the generator tautology is broken by a hand-authored fixture", check_generator_tautology_is_broken_by_a_hand_authored_fixture),
        ("7b", "docs/evaluation.md's v1 baseline table stays a projection", check_recorded_baseline_relabeled_as_projection),
        ("7c", "script-generated fixtures are tagged provenance=synthetic", check_generated_fixtures_are_synthetic),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} … {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All eval-harness invariant checks passed.")
        return 0
    else:
        print("One or more eval-harness invariant checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
