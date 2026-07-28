#!/usr/bin/env python3
"""
Model-free evaluation-harness skeleton — issue #62.

Per the issue #62 "Reconciliation with the 2026-06-11 architecture review"
scope decision, this module builds the harness SKELETON that is buildable
before the LLM pipeline stages (#80-#83) land:

  1. A gold-case loader for the fixture schema documented in
     docs/evaluation.md -> "What a gold case contains" (case_id, playbook_
     version, expected_decision, expected_issues[], must_not_flag[],
     fp_tolerance, redline_checks[], plus the detector-only
     planted_variation / accept_variation / detector_expectation fields
     used by the D1/D2 gates in tests/gold-fixtures/).

  2. A deterministic, model-free "runner" that submits a gold case to the
     detector layer ONLY (no Bedrock call — the real primary/adversarial/
     redline pipeline stages are mocked per infra/lambda/mock_review/
     handler.py under the MVP pivot, epic #123) and captures actual
     detector fires for the case's planted or accept variation.

  3. A comparator/scorer that diffs actual vs. expected per the four
     checks in docs/evaluation.md -> "What the harness verifies per case":
     decision accuracy, missed-issue check, false-positive check (against
     must_not_flag[] and fp_tolerance), each producing a per-case pass/fail
     and a reason.

This module does NOT invoke Bedrock, does NOT run retrieval, and does NOT
run redline generation — those gates are explicitly deferred in the #62
scope decision until #80-#83 / #89 land. What it DOES do is make the gold
fixture set and the detector layer machine-checkably scored today, which
is the model-free backstop the issue asks for.

Detector simulation supports both hard_rejection kinds:
  - on_insert:          trigger_terms / match / exempt_terms over the
                         fixture's planted_variation.inserted_hunk (or
                         accept_variation.inserted_hunk / altered_hunk for
                         a no-fire assertion).
  - on_remove_or_alter: required_tokens / token_policy over the fixture's
                         planted_variation.altered_hunk, asserting the
                         hunk fails to retain a required token (fires) or
                         retains all of them (does not fire).

CLI usage:
    python3 scripts/eval_harness.py            # run all gold fixtures
    python3 scripts/eval_harness.py --quiet     # summary line only

Exit codes: 0 = every case scored PASS, 1 = at least one case scored FAIL.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import detector_common  # noqa: E402
import diff_standard_form  # noqa: E402
import extraction_normalization_stage  # noqa: E402
import playbook_registry  # noqa: E402
import primary_review_pass  # noqa: E402
import redline_patch  # noqa: E402

BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC_DIR))

import model_client  # noqa: E402

# Back-compat literals (issue #45-era): resolved for the default ("eiaa")
# playbook_id, so existing callers that pass no arguments (or import these
# names directly, e.g. scripts/generate_gold_fixtures.py) keep working
# unchanged. A specific playbook_id's fixtures/playbook are resolved fresh
# via playbook_registry -- see load_playbook()/load_gold_cases()/score_all()
# below (issue #209: playbook_id is namespaced eval-suite data, not a
# hard-coded path).
_DEFAULT_ENTRY = playbook_registry.resolve_playbook(playbook_registry.DEFAULT_PLAYBOOK_ID)
PLAYBOOK_PATH = _DEFAULT_ENTRY.playbook_path
FIXTURES_PATH = _DEFAULT_ENTRY.fixtures_dir


# ---------------------------------------------------------------------------
# Gold-case loading
# ---------------------------------------------------------------------------

@dataclass
class GoldCase:
    """A single gold fixture, per docs/evaluation.md -> 'What a gold case
    contains'. Detector-only fields (planted_variation, accept_variation,
    detector_expectation) are optional -- fixtures that only assert doc-level
    hash invariants (e.g. canonicalize-golden-hash.json) are skipped by the
    runner, not scored as cases.
    """

    case_id: str
    path: Path
    raw: dict[str, Any]

    @property
    def expected_decision(self) -> str | None:
        return self.raw.get("expected_decision")

    @property
    def expected_issues(self) -> list[dict[str, Any]]:
        return self.raw.get("expected_issues", [])

    @property
    def must_not_flag(self) -> list[Any]:
        return self.raw.get("must_not_flag", [])

    @property
    def fp_tolerance(self) -> int:
        return int(self.raw.get("fp_tolerance", 0))

    @property
    def planted_variation(self) -> dict[str, Any] | None:
        return self.raw.get("planted_variation")

    @property
    def accept_variation(self) -> dict[str, Any] | None:
        return self.raw.get("accept_variation")

    @property
    def detector_expectation(self) -> dict[str, Any] | None:
        return self.raw.get("detector_expectation")

    @property
    def is_detector_case(self) -> bool:
        """True if this fixture carries the fields needed to run it through
        the detector-simulation runner (item 1 of the #62 scope)."""
        return bool(self.detector_expectation) and bool(
            self.planted_variation or self.accept_variation
        )


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


def rules_by_id(playbook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in playbook.get("hard_rejections", [])}


# ---------------------------------------------------------------------------
# Deterministic detector simulation (model-free)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return detector_common.normalize(text)


@dataclass
class DetectorFire:
    rule_id: str
    trigger: str
    reason: str


def run_on_insert_rule(rule: dict[str, Any], hunk_text: str, topic_id: str) -> list[DetectorFire]:
    """Simulate an on_insert hard_rejection rule over a single hunk of text.

    Delegates to scripts/detector_common.check_on_insert_rule_fires -- the
    single shared implementation (issue #212) also used by
    tests/lint-gold-fixtures.py and tests/lint-acceptable-variations.py, so
    every caller agrees on what "fires" means, including SPAN-level
    exempt_terms semantics (a trigger match is suppressed only when that
    match's own span falls inside an exempt-phrase span, not merely
    because an exempt phrase appears somewhere else in the hunk).
    """
    match_type = rule.get("match", "word_boundary")
    return [
        DetectorFire(
            rule_id=fire["rule_id"],
            trigger=fire["trigger_term"],
            reason=f"trigger_term {fire['trigger_term']!r} matched ({match_type}, unexempted span)",
        )
        for fire in detector_common.check_on_insert_rule_fires(rule, hunk_text, topic_id)
    ]


def run_on_remove_or_alter_rule(
    rule: dict[str, Any], altered_hunk: str, topic_id: str
) -> list[DetectorFire]:
    """Simulate an on_remove_or_alter hard_rejection rule.

    Delegates to scripts/detector_common.check_on_remove_or_alter_rule_fires
    -- the single shared implementation (issue #213) also used by
    tests/lint-gold-fixtures.py and tests/lint-acceptable-variations.py, so
    every caller agrees on what "fires" means: `protects.required_tokens`
    with token_policy 'any' fires when the altered/replacement hunk text is
    MISSING any of the required tokens (the protected language was removed
    or weakened); token_policy 'all' fires only when ALL required tokens
    are missing.
    """
    token_policy = rule.get("protects", {}).get("token_policy", "any")
    fires: list[DetectorFire] = []
    for fire in detector_common.check_on_remove_or_alter_rule_fires(rule, altered_hunk, topic_id):
        missing = fire["missing_tokens"]
        reason = (
            f"required_tokens missing (any-policy): {missing!r}"
            if token_policy == "any"
            else f"required_tokens all missing (all-policy): {missing!r}"
        )
        fires.append(DetectorFire(rule_id=fire["rule_id"], trigger=",".join(missing), reason=reason))
    return fires


def run_detectors_on_case(case: GoldCase, playbook: dict[str, Any]) -> list[DetectorFire]:
    """Run every hard_rejection rule over a single gold case's variation
    text and return the fires actually observed. This is the harness
    'runner' step (item 1 of the #62 scope): submit a case, capture actual
    output -- here, the detector-only actual output, since the LLM stages
    are mocked/unbuilt per the #62 scope decision.
    """
    rules = playbook.get("hard_rejections", [])
    variation = case.planted_variation or case.accept_variation
    if not variation:
        return []

    topic_id = variation.get("topic_id", "")
    inserted_hunk = variation.get("inserted_hunk")
    altered_hunk = variation.get("altered_hunk")

    fires: list[DetectorFire] = []
    for rule in rules:
        if rule.get("kind") == "on_insert" and inserted_hunk is not None:
            fires.extend(run_on_insert_rule(rule, inserted_hunk, topic_id))
        elif rule.get("kind") == "on_remove_or_alter" and altered_hunk is not None:
            fires.extend(run_on_remove_or_alter_rule(rule, altered_hunk, topic_id))
    return fires


# ---------------------------------------------------------------------------
# Document-level gold cases (issue #204): real extract -> normalize -> diff
# -> detector chain over an actual `.docx` fixture, per docs/evaluation.md's
# "What a gold case contains" -> `input_docx` field -- as opposed to
# score_case() above, which simulates detector fires over a fixture's
# already-isolated planted_variation/accept_variation text SNIPPET. A
# document-level case instead supplies real `.docx` bytes; this section
# extracts, normalizes, diffs against the canonical standard form, and runs
# every hard_rejection rule over the hunks the diff actually produced --
# the real pipeline path, not a tautological "regex matches the string it
# was built from" check.
# ---------------------------------------------------------------------------


def build_anchor_topic_map(playbook: dict[str, Any]) -> dict[str, str]:
    """anchor -> playbook_topic_id, from each topic's `section_anchors`.
    Needed because a diff hunk only carries an `anchor`; hard_rejection
    rules scope by `applies_to_topics`, so a hunk's topic_id must be
    resolved before a rule can be checked against it."""
    mapping: dict[str, str] = {}
    for topic in playbook.get("topics", []):
        for anchor in topic.get("section_anchors", []):
            mapping[anchor] = topic["id"]
    return mapping


def run_detectors_on_hunks(hunks: list[dict[str, Any]], playbook: dict[str, Any]) -> list[DetectorFire]:
    """Run every hard_rejection rule over a REAL diff's hunks (as produced
    by scripts/diff_standard_form.py's diff_draft_against_standard()), the
    document-level counterpart of run_detectors_on_case() above.

    A hunk's `kind` determines which surface a rule reads:
      - "inserted" / "modified_new": on_insert rules read the hunk's
        current `text` (the counterparty's added/changed surface).
      - "modified_new": on_remove_or_alter rules read the same current
        `text`, alteration_kind="modify" (the protected token may have been
        reworded away, not wholly deleted).
      - "deleted": on_remove_or_alter rules read alteration_kind="delete"
        with an EMPTY altered surface -- the hunk's own `text` field on a
        "deleted" hunk is the OLD standard-side text (what got removed, for
        DISPLAY), not the current/altered surface, so it must not be passed
        as the text being checked for retained required_tokens.
    """
    anchor_topic = build_anchor_topic_map(playbook)
    rules = playbook.get("hard_rejections", [])
    fires: list[DetectorFire] = []

    for hunk in hunks:
        topic_id = anchor_topic.get(hunk.get("anchor", ""))
        if not topic_id:
            continue
        kind = hunk.get("kind")
        text = hunk.get("text", "") or ""

        for rule in rules:
            if rule.get("kind") == "on_insert" and kind in ("inserted", "modified_new"):
                fires.extend(run_on_insert_rule(rule, text, topic_id))
            elif rule.get("kind") == "on_remove_or_alter" and kind in ("modified_new", "deleted"):
                altered_text = "" if kind == "deleted" else text
                alteration_kind = "delete" if kind == "deleted" else "modify"
                for fire in detector_common.check_on_remove_or_alter_rule_fires(
                    rule, altered_text, topic_id, alteration_kind=alteration_kind
                ):
                    missing = fire["missing_tokens"]
                    fires.append(
                        DetectorFire(
                            rule_id=fire["rule_id"],
                            trigger=",".join(missing),
                            reason=f"document-level: required_tokens missing ({kind}): {missing!r}",
                        )
                    )
    return fires


@dataclass
class DocumentLevelResult:
    case_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    hunks: list[dict[str, Any]] = field(default_factory=list)
    fired_rule_ids: list[str] = field(default_factory=list)


def score_document_level_case(
    case_raw: dict[str, Any],
    docx_bytes: bytes,
    playbook: dict[str, Any],
    standard_paragraphs: list[dict[str, Any]],
) -> DocumentLevelResult:
    """Run a document-level gold case (docs/evaluation.md's `input_docx`
    field) through the real pipeline stages -- extract, normalize, diff,
    detect -- and score the four docs/evaluation.md checks: decision
    accuracy, missed-issue, false-positive (checks 1-3; check 4, redline-
    patch correctness, is check_redline_checks() below, since it needs a
    model "issue" to join against, not just the detector fires).
    """
    case_id = case_raw.get("case_id", "<unknown>")
    normalized = extraction_normalization_stage.extract_and_normalize(docx_bytes)
    if normalized["status"] != "normalized":
        return DocumentLevelResult(
            case_id=case_id,
            passed=False,
            reasons=[f"input_docx failed to normalize: {normalized}"],
        )

    hunks = diff_standard_form.diff_draft_against_standard(
        standard_paragraphs, normalized["paragraphs"]
    )
    fires = run_detectors_on_hunks(hunks, playbook)
    fired_rule_ids = sorted({f.rule_id for f in fires})

    expected_decision = case_raw.get("expected_decision")
    expected_issues = case_raw.get("expected_issues", [])
    fp_tolerance = int(case_raw.get("fp_tolerance", 0))
    expected_hard_rule_ids = {
        issue["rule_id"] for issue in expected_issues if issue.get("is_hard_rejection") and issue.get("rule_id")
    }

    actual_decision = "REQUEST_CHANGE" if fired_rule_ids else "ACCEPT"

    reasons: list[str] = []
    passed = True

    # Check 1: decision accuracy.
    if actual_decision != expected_decision:
        passed = False
        reasons.append(
            f"decision mismatch: expected {expected_decision!r}, got "
            f"{actual_decision!r} (fired_rule_ids={fired_rule_ids})"
        )

    # Check 2: missed-issue -- every expected hard rejection must fire.
    missing = expected_hard_rule_ids - set(fired_rule_ids)
    if missing:
        passed = False
        reasons.append(f"missed expected hard rejection(s): {sorted(missing)}")

    # Check 3: false-positive -- unexpected fires beyond fp_tolerance.
    unexpected = set(fired_rule_ids) - expected_hard_rule_ids
    if len(unexpected) > fp_tolerance:
        passed = False
        reasons.append(
            f"unexpected fire(s) beyond fp_tolerance={fp_tolerance}: {sorted(unexpected)}"
        )

    return DocumentLevelResult(
        case_id=case_id, passed=passed, reasons=reasons, hunks=hunks, fired_rule_ids=fired_rule_ids
    )


def check_redline_checks(
    case_raw: dict[str, Any],
    hunks: list[dict[str, Any]],
    standard_paragraphs: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Check 4 (docs/evaluation.md): for each `redline_checks[]` entry, (a)
    the diff's own hunk at that anchor must carry the SAME
    (anchor, source_text_hash) pin, and (b) a patch referencing that anchor
    (server-side joined via redline_patch.join_patches_from_diff(), issue
    #205 -- never model-transcribed) must actually APPLY against the
    current canonical standard-form state -- i.e. the redline lands on the
    right clause, not merely a plausible-looking one.
    """
    redline_checks = case_raw.get("redline_checks", [])
    if not redline_checks:
        return True, ["no redline_checks[] entries on this case; nothing to verify"]

    hunks_by_anchor = {h["anchor"]: h for h in hunks}
    current_paragraphs_by_anchor = {p["anchor"]: p["text"] for p in standard_paragraphs}

    reasons: list[str] = []
    passed = True

    for check in redline_checks:
        anchor = check.get("anchor")
        expected_hash = check.get("source_text_hash")
        hunk = hunks_by_anchor.get(anchor)

        if hunk is None or hunk.get("source_text_hash") != expected_hash:
            passed = False
            reasons.append(
                f"redline_checks anchor {anchor!r}: expected source_text_hash "
                f"{expected_hash!r}, diff hunk has {hunk.get('source_text_hash') if hunk else 'NO HUNK'!r}"
            )
            continue

        model_issue = {"anchor": anchor, "proposed_replacement_text": "placeholder replacement text."}
        patches = redline_patch.join_patches_from_diff(hunks, [model_issue])
        result = redline_patch.apply_patch(current_paragraphs_by_anchor, patches[0])
        if not result["applied"]:
            passed = False
            reasons.append(f"redline_checks anchor {anchor!r}: patch failed to apply: {result}")

    return passed, reasons


# ---------------------------------------------------------------------------
# Smoke-tier "model" pass (issue #204): drives the real primary-review-pass
# assembly/validation code (scripts/primary_review_pass.py, issue #81)
# through the injectable deterministic FakeBedrockClient + a RECORDED
# response fixture (tests/fixtures/model_responses/*.json) -- NO live
# Bedrock, no network. This proves the harness can measure decision
# accuracy + missed-issue rate against a known answer end-to-end through
# the actual model-invocation seam, not just the model-free detector layer.
# The full stochastic gate (real Bedrock, many-sample statistics) stays a
# human smoke test per ARCHITECTURE.md -- out of scope here.
# ---------------------------------------------------------------------------


@dataclass
class SmokeModelCaseResult:
    case_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    actual_decision: str | None = None


def run_smoke_tier_model_pass(
    *,
    review_id: str,
    diff_hunks: list[dict[str, Any]],
    playbook: dict[str, Any],
    model_client_instance: "model_client.BedrockModelClient",
    model_id: str,
) -> dict[str, Any]:
    """Thin wrapper around primary_review_pass.run_primary_pass() with the
    minimal manifest inputs a smoke-tier eval run needs (no anchored
    clauses / retrieved precedent / raw doc text -- those are populated by
    the real pipeline stages upstream, out of scope for this harness slice)."""
    ledger: list[model_client.ModelInvocationRecord] = []
    return primary_review_pass.run_primary_pass(
        review_id=review_id,
        diff_hunks=diff_hunks,
        anchored_clauses=[],
        retrieved_precedent=[],
        playbook=playbook,
        model_client=model_client_instance,
        model_id=model_id,
        ledger_write=ledger.append,
        doc_text="",
    )


def score_smoke_tier_case(
    case_id: str,
    expected_decision: str,
    expected_hard_topic_ids: set[str],
    diff_hunks: list[dict[str, Any]],
    playbook: dict[str, Any],
    model_client_instance: "model_client.BedrockModelClient",
    model_id: str,
) -> SmokeModelCaseResult:
    """Run one smoke-tier case through the FakeBedrockClient-driven primary
    pass and score decision accuracy + missed-issue rate against the known
    answer (docs/evaluation.md checks 1-2, at the model-invocation layer
    this time rather than the detector layer)."""
    result = run_smoke_tier_model_pass(
        review_id=case_id,
        diff_hunks=diff_hunks,
        playbook=playbook,
        model_client_instance=model_client_instance,
        model_id=model_id,
    )

    if result.get("status") != "OK":
        return SmokeModelCaseResult(
            case_id=case_id, passed=False, reasons=[f"primary pass did not return OK: {result}"]
        )

    response = result["response"]
    actual_decision = response.get("decision")
    reasons: list[str] = []
    passed = True

    if actual_decision != expected_decision:
        passed = False
        reasons.append(f"decision mismatch: expected {expected_decision!r}, got {actual_decision!r}")

    actual_topic_ids = {issue.get("playbook_topic_id") for issue in response.get("issues", [])}
    missing_topics = expected_hard_topic_ids - actual_topic_ids
    if missing_topics:
        passed = False
        reasons.append(f"missed expected topic(s) in model output: {sorted(missing_topics)}")

    return SmokeModelCaseResult(
        case_id=case_id, passed=passed, reasons=reasons, actual_decision=actual_decision
    )


# ---------------------------------------------------------------------------
# Comparator / scorer
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    case_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    actual_fires: list[str] = field(default_factory=list)


def score_case(case: GoldCase, playbook: dict[str, Any]) -> CaseResult:
    """Score a single gold case against its detector_expectation (fire /
    no_fire) per docs/evaluation.md -> 'What the harness verifies per
    case': here restricted to the detector-only subset (missed-issue check
    and false-positive check on the deterministic layer), since decision
    accuracy and redline-patch correctness require the LLM pipeline stages
    that #62 explicitly defers.
    """
    if not case.is_detector_case:
        return CaseResult(case_id=case.case_id, passed=True, reasons=["not a detector case; skipped"])

    expectation = case.detector_expectation or {}
    expected_result = expectation.get("expected_result")
    expected_rule_id = expectation.get("rule_id")

    fires = run_detectors_on_case(case, playbook)
    fired_rule_ids = sorted({f.rule_id for f in fires})
    actual_fires = [f"{f.rule_id} ({f.trigger!r})" for f in fires]

    reasons: list[str] = []
    passed = True

    if expected_result == "fire":
        if expected_rule_id not in fired_rule_ids:
            passed = False
            reasons.append(
                f"expected rule {expected_rule_id!r} to fire; actual fires: {fired_rule_ids or 'none'}"
            )
        # False positive check: any OTHER rule firing beyond fp_tolerance.
        unexpected = [r for r in fired_rule_ids if r != expected_rule_id]
        if len(unexpected) > case.fp_tolerance:
            passed = False
            reasons.append(
                f"unexpected extra rule fires beyond fp_tolerance={case.fp_tolerance}: {unexpected}"
            )
    elif expected_result == "no_fire":
        if fired_rule_ids:
            if len(fired_rule_ids) > case.fp_tolerance:
                passed = False
                reasons.append(
                    f"expected no_fire for {expected_rule_id!r}; actual fires: {fired_rule_ids}"
                )
    else:
        passed = False
        reasons.append(f"unrecognized detector_expectation.expected_result: {expected_result!r}")

    return CaseResult(case_id=case.case_id, passed=passed, reasons=reasons, actual_fires=actual_fires)


def score_all(
    fixtures_dir: Path = FIXTURES_PATH, playbook_path: Path = PLAYBOOK_PATH
) -> list[CaseResult]:
    playbook = load_playbook(playbook_path)
    cases = load_gold_cases(fixtures_dir)
    return [score_case(case, playbook) for case in cases]


# ---------------------------------------------------------------------------
# Topic / rule coverage
# ---------------------------------------------------------------------------

def rule_ids_with_detector_coverage(fixtures_dir: Path = FIXTURES_PATH) -> set[str]:
    """Rule IDs exercised by at least one detector-case gold fixture (any
    expected_result). Used by the topic-coverage check: every hard_rejection
    rule must have at least one gold case (docs/evaluation.md -> 'Regression
    gates' #2)."""
    covered: set[str] = set()
    for case in load_gold_cases(fixtures_dir):
        if not case.is_detector_case:
            continue
        rule_id = (case.detector_expectation or {}).get("rule_id")
        if rule_id:
            covered.add(rule_id)
    return covered


def missing_rule_coverage(
    fixtures_dir: Path = FIXTURES_PATH, playbook_path: Path = PLAYBOOK_PATH
) -> list[str]:
    playbook = load_playbook(playbook_path)
    all_rule_ids = {r["id"] for r in playbook.get("hard_rejections", [])}
    covered = rule_ids_with_detector_coverage(fixtures_dir)
    return sorted(all_rule_ids - covered)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_detector_gate_for_playbook(entry, quiet: bool = False) -> bool:
    """Run the detector D-gate (score every gold fixture + per-rule
    coverage check) for a single PRECISION registry entry. Returns True on
    PASS. Caller is responsible for only invoking this on a "precision"
    profile entry -- see playbook_registry.profile()."""
    results = score_all(fixtures_dir=entry.fixtures_dir, playbook_path=entry.playbook_path)
    missing = missing_rule_coverage(fixtures_dir=entry.fixtures_dir, playbook_path=entry.playbook_path)

    failed = [r for r in results if not r.passed]

    if not quiet:
        print(f"Evaluation harness [{entry.playbook_id}]: scored {len(results)} gold fixture(s).")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.case_id}")
            for reason in r.reasons:
                print(f"      {reason}")
        if missing:
            print(f"\nMissing per-rule gold-case coverage for {len(missing)} rule(s): {missing}")
        else:
            print("\nAll hard_rejection rules have at least one gold-case detector fixture.")

    ok = not failed and not missing
    if not quiet:
        print("PASS" if ok else "FAIL", f"[{entry.playbook_id}]\n")
    return ok


def main(argv: list[str] | None = None) -> int:
    """Detector D-gate CLI (issue #62; profile-conditional per issue #288).

    With no `--playbook-id`, iterates every registered playbook_id: a
    "knowledge" profile entry (no anchor_map_path / section_config_path --
    see playbook_registry.profile()) has no detector-relevant standard-form
    structure for this gate to score fixtures against, so it is explicitly
    SKIPped (printed, never silent) and counted as skipped-not-passed, not
    silently omitted. "precision" profile entries (e.g. eiaa) still run the
    full detector gate below, so they lose no enforcement. `--playbook-id`
    still selects exactly one entry (skipping the profile check -- an
    explicit request always runs), preserving the pre-#288 single-playbook
    CLI contract.
    """
    argv = argv if argv is not None else sys.argv[1:]
    quiet = "--quiet" in argv

    if "--playbook-id" in argv:
        idx = argv.index("--playbook-id")
        entry = playbook_registry.resolve_playbook(argv[idx + 1])
        ok = run_detector_gate_for_playbook(entry, quiet=quiet)
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
            print(f"SKIP (knowledge profile): D-gate {playbook_id}")
            skipped.append(playbook_id)
            continue
        ran.append(playbook_id)
        if not run_detector_gate_for_playbook(entry, quiet=quiet):
            all_ok = False

    if not quiet:
        print(
            f"D-gate summary: {len(ran)} playbook(s) scored, "
            f"{len(skipped)} skipped (knowledge profile): {skipped}"
        )
        print("\nPASS" if all_ok else "\nFAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
