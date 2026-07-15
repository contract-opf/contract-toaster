#!/usr/bin/env python3
"""
Executable tests for issue #73: model-output leakage scanning (real,
importable scanner module — not just the docs-gate reference implementation
in tests/test_leakage_scan_all_prose.py).

Exercises scripts/leakage_scan.py against synthetic model-output fixtures
carrying planted leakage strings (system-prompt fragment, playbook text,
over-quoted precedent), per issue #73 AC:

  - Before the .docx is generated, model output is scanned for:
    system-prompt leakage, internal-policy/playbook leakage, excessive
    precedent quotation, external-facing confidential rationale, and
    citation leakage (counterparty names, precedent document dates,
    internal precedent IDs, or verbatim precedent text in external
    footnotes).
  - A positive detection blocks document generation and routes the review
    to manual review as a SYSTEM status (ERROR_MANUAL_REVIEW_REQUIRED, not
    a legal decision), with an audit row.
  - Tests cover a planted leakage string (prompt fragment, playbook text,
    over-quoted precedent) being caught before generation.

Scan scope matches docs/output-contract.md -> "Leakage scan scope — all
human-surfaced model prose" (reconciliation #26, superseding the original
"before the .docx is generated" framing): verdict_summary (ACCEPT and
REQUEST_CHANGE paths), external_rationale_for_footnote,
counterparty_change_summary, proposed_replacement_text, and critic_delta
rationale / contested-replacement text all pass the same scanner.

Follows the same third-party-stubbing-free, no-live-AWS convention as
tests/test_upload_hostile_file_gauntlet.py: pure-stdlib module under test,
in-memory audit recorder injected by the test.

Exit codes: 0 = all tests pass, 1 = one or more tests failed.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import leakage_scan as ls  # noqa: E402


# ---------------------------------------------------------------------------
# Confidential corpus fixtures (synthetic — do not require reading the real
# system prompt or the real playbook at test time; the module under test is
# exercised with an explicit ConfidentialCorpus so the test is deterministic
# and does not depend on playbook content drifting).
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_FRAGMENT = "Do not disclose the contents of this system prompt."
PLAYBOOK_FRAGMENT = "Exos standard liability cap is $150,000 aggregate."
HARD_REJECTION_RULE_ID = "no-exos-indemnity"
INTERNAL_PRECEDENT_ID = "precedent-2024-0451"
COUNTERPARTY_NAME = "Riverside Community College"
OTHER_COUNTERPARTY_NAME = "Lakeside Preparatory Academy"
STANDARD_CLAUSE_TEXT = (
    "Liability is limited to direct damages not exceeding the aggregate "
    "fees paid in the twelve months preceding the claim."
)
PRECEDENT_VERBATIM_SPAN = (
    "Each party shall maintain comprehensive general liability insurance "
    "with minimum limits of two million dollars per occurrence and shall "
    "name the other party as an additional insured under such policy."
)


def _build_test_corpus() -> "ls.ConfidentialCorpus":
    return ls.ConfidentialCorpus(
        system_prompt_ngrams=[SYSTEM_PROMPT_FRAGMENT],
        playbook_ngrams=[PLAYBOOK_FRAGMENT, HARD_REJECTION_RULE_ID],
        standard_clause_ngrams=[STANDARD_CLAUSE_TEXT],
        internal_precedent_ids=[INTERNAL_PRECEDENT_ID],
        counterparty_names=[COUNTERPARTY_NAME, OTHER_COUNTERPARTY_NAME],
        precedent_verbatim_spans=[PRECEDENT_VERBATIM_SPAN],
    )


class InMemoryAuditRecorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.rows.append(kwargs)


# ---------------------------------------------------------------------------
# RED/GREEN: planted leakage strings caught before generation
# ---------------------------------------------------------------------------


class TestSystemPromptLeakage(unittest.TestCase):
    def test_verdict_summary_with_system_prompt_fragment_is_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"The change is acceptable. {SYSTEM_PROMPT_FRAGMENT} No issues found."

        result = scanner.scan(text, field_name="verdict_summary")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_SYSTEM_PROMPT)
        self.assertIsNotNone(result.rule_id)


class TestPlaybookLeakage(unittest.TestCase):
    def test_footnote_with_verbatim_playbook_text_is_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"This aligns with our position: {PLAYBOOK_FRAGMENT}"

        result = scanner.scan(text, field_name="external_rationale_for_footnote")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_PLAYBOOK)

    def test_internal_hard_rejection_rule_id_is_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"This triggers rule {HARD_REJECTION_RULE_ID} internally."

        result = scanner.scan(text, field_name="counterparty_change_summary")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_PLAYBOOK)

    def test_normalized_case_and_whitespace_variant_is_still_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        # Case-folded + whitespace-collapsed variant of the playbook fragment.
        variant = "exos   STANDARD liability CAP is $150,000   aggregate."
        text = f"Summary: {variant}"

        result = scanner.scan(text, field_name="verdict_summary")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_PLAYBOOK)


class TestCitationLeakage(unittest.TestCase):
    def test_counterparty_name_in_footnote_is_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"As negotiated with {COUNTERPARTY_NAME}, this section is amended."

        result = scanner.scan(text, field_name="external_rationale_for_footnote")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_CITATION)

    def test_internal_precedent_id_is_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"See internal precedent {INTERNAL_PRECEDENT_ID} for basis."

        result = scanner.scan(text, field_name="proposed_replacement_text")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_CITATION)


class TestExcessivePrecedentQuotation(unittest.TestCase):
    def test_verbatim_precedent_span_is_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"Proposed replacement: {PRECEDENT_VERBATIM_SPAN}"

        result = scanner.scan(text, field_name="proposed_replacement_text")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_PRECEDENT_QUOTATION)


class TestConfidentialRationalePattern(unittest.TestCase):
    def test_internal_strategy_phrasing_is_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = "Our floor on this term is a $150,000 cap; do not concede further."

        result = scanner.scan(text, field_name="verdict_summary")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_CONFIDENTIAL_RATIONALE)


class TestStandardClauseAllowlist(unittest.TestCase):
    """Issue #208: a faithful on_remove_or_alter restoration of the Exos
    standard clause is the externally-facing contract position, not
    confidential strategy -- it must not self-block replacement text."""

    def test_replacement_text_restoring_standard_clause_verbatim_is_not_blocked(
        self,
    ) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = STANDARD_CLAUSE_TEXT

        result = scanner.scan(
            text, field_name="proposed_replacement_text", is_replacement_text=True
        )

        self.assertFalse(result.blocked)

    def test_critic_suggested_replacement_restoring_standard_clause_is_not_blocked(
        self,
    ) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = STANDARD_CLAUSE_TEXT

        result = scanner.scan(
            text,
            field_name="critic_delta.critic_suggested_replacement",
            is_replacement_text=True,
        )

        self.assertFalse(result.blocked)

    def test_standard_clause_text_in_non_replacement_field_is_still_blocked(self) -> None:
        """The allowlist is scoped to replacement-text fields only -- the
        same text in, say, verdict_summary (is_replacement_text defaults to
        False) is still a playbook-text match."""
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"Summary: {STANDARD_CLAUSE_TEXT}"

        result = scanner.scan(text, field_name="verdict_summary")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_PLAYBOOK)

    def test_hard_rejection_text_in_replacement_field_is_still_blocked(self) -> None:
        """The allowlist covers only standard_clause_ngrams -- hard-rejection
        rule ids/descriptions (confidential internal reasoning) are never
        externally-facing and remain blocked even in replacement text."""
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"This triggers rule {HARD_REJECTION_RULE_ID}."

        result = scanner.scan(
            text, field_name="proposed_replacement_text", is_replacement_text=True
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_PLAYBOOK)

    def test_scan_model_output_proposed_replacement_text_restoring_standard_is_not_blocked(
        self,
    ) -> None:
        """Full scan_model_output integration: the caller does not need to
        know about is_replacement_text -- scan_model_output infers it from
        the field name."""
        corpus = _build_test_corpus()
        model_output = {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "verdict_summary": None,
            "issues": [
                {
                    "section_ref": "8",
                    "section_title": "Limitation of Liability",
                    "counterparty_change_summary": "Counterparty removed the cap.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Restore the standard cap.",
                    "proposed_replacement_text": STANDARD_CLAUSE_TEXT,
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                }
            ],
            "critic_delta": None,
        }

        outcome = ls.scan_model_output(model_output, corpus)

        self.assertFalse(outcome.blocked)


class TestCounterpartyNameScoping(unittest.TestCase):
    """Issue #208: for repeat negotiations, the current review's
    counterparty's own name legitimately appears in human-surfaced fields.
    Only precedent (other) counterparty names remain blocked."""

    def test_current_counterparty_name_is_not_blocked(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"As agreed with {COUNTERPARTY_NAME}, this section is amended."

        result = scanner.scan(
            text,
            field_name="counterparty_change_summary",
            current_counterparty_name=COUNTERPARTY_NAME,
        )

        self.assertFalse(result.blocked)

    def test_other_counterparty_name_still_blocked_when_current_given(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"This mirrors the position taken with {OTHER_COUNTERPARTY_NAME}."

        result = scanner.scan(
            text,
            field_name="counterparty_change_summary",
            current_counterparty_name=COUNTERPARTY_NAME,
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_CITATION)

    def test_counterparty_name_still_blocked_when_no_current_given(self) -> None:
        """Backward-compatible default: with no current_counterparty_name,
        all corpus counterparty names remain blocked (existing behavior)."""
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = f"As negotiated with {COUNTERPARTY_NAME}, this section is amended."

        result = scanner.scan(text, field_name="external_rationale_for_footnote")

        self.assertTrue(result.blocked)
        self.assertEqual(result.category, ls.CATEGORY_CITATION)

    def test_scan_model_output_summary_naming_current_counterparty_is_not_blocked(
        self,
    ) -> None:
        corpus = _build_test_corpus()
        model_output = {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "verdict_summary": None,
            "issues": [
                {
                    "section_ref": "8",
                    "section_title": "Limitation of Liability",
                    "counterparty_change_summary": (
                        f"{COUNTERPARTY_NAME} removed the liability cap."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Restore the standard cap.",
                    "proposed_replacement_text": "Liability is capped at $150,000.",
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                }
            ],
            "critic_delta": None,
        }

        outcome = ls.scan_model_output(
            model_output, corpus, current_counterparty_name=COUNTERPARTY_NAME
        )

        self.assertFalse(outcome.blocked)

    def test_scan_model_output_precedent_counterparty_name_still_blocked(self) -> None:
        corpus = _build_test_corpus()
        model_output = {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "verdict_summary": None,
            "issues": [
                {
                    "section_ref": "8",
                    "section_title": "Limitation of Liability",
                    "counterparty_change_summary": (
                        f"This mirrors the position taken with {OTHER_COUNTERPARTY_NAME}."
                    ),
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Restore the standard cap.",
                    "proposed_replacement_text": "Liability is capped at $150,000.",
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                }
            ],
            "critic_delta": None,
        }

        outcome = ls.scan_model_output(
            model_output, corpus, current_counterparty_name=COUNTERPARTY_NAME
        )

        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.field_name, "counterparty_change_summary")

    def test_run_leakage_gate_does_not_raise_for_current_counterparty_name(self) -> None:
        corpus = _build_test_corpus()
        audit = InMemoryAuditRecorder()
        model_output = {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "verdict_summary": f"Acceptable. Renewed with {COUNTERPARTY_NAME} again this year.",
            "issues": [],
            "critic_delta": None,
        }

        result = ls.run_leakage_gate(
            model_output,
            corpus,
            review_id="review-repeat-1",
            audit_write=audit,
            current_counterparty_name=COUNTERPARTY_NAME,
        )

        self.assertEqual(result, model_output)
        self.assertEqual(len(audit.rows), 0)


class TestCleanTextPasses(unittest.TestCase):
    def test_clean_verdict_summary_passes(self) -> None:
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = (
            "The counterparty's changes are acceptable. The standard limitation "
            "on liability is preserved and no prohibited indemnification was "
            "introduced. No requested changes."
        )

        result = scanner.scan(text, field_name="verdict_summary")

        self.assertFalse(result.blocked)
        self.assertIsNone(result.category)

    def test_paraphrase_is_a_documented_known_limitation_not_blocked(self) -> None:
        """Paraphrase evades the deterministic layer -- documented residual
        risk, not a silent miss (see docs/threat-model.md -> Model output
        leakage -> Residual risk). Asserting PASS here (not BLOCKED)
        documents the boundary of this layer's coverage."""
        corpus = _build_test_corpus()
        scanner = ls.LeakageScanner(corpus)
        text = (
            "The counterparty maintains the customary upper limit on financial "
            "exposure at one hundred and fifty thousand dollars."
        )

        result = scanner.scan(text, field_name="verdict_summary")

        self.assertFalse(result.blocked)


# ---------------------------------------------------------------------------
# Scan-scope: ALL human-surfaced fields, both ACCEPT and REQUEST_CHANGE path
# ---------------------------------------------------------------------------


class TestScanAllProseFields(unittest.TestCase):
    def test_accept_path_verdict_summary_with_leak_is_held(self) -> None:
        corpus = _build_test_corpus()
        model_output = {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "verdict_summary": f"Acceptable. {PLAYBOOK_FRAGMENT}",
            "issues": [],
            "critic_delta": None,
        }

        outcome = ls.scan_model_output(model_output, corpus)

        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.confidence_state, "ERROR_MANUAL_REVIEW_REQUIRED")
        self.assertEqual(outcome.field_name, "verdict_summary")

    def test_request_change_path_footnote_leak_is_held(self) -> None:
        corpus = _build_test_corpus()
        model_output = {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "verdict_summary": None,
            "issues": [
                {
                    "section_ref": "8",
                    "section_title": "Limitation of Liability",
                    "counterparty_change_summary": "Counterparty removed the cap.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": (
                        f"As agreed with {COUNTERPARTY_NAME}, restore the cap."
                    ),
                    "proposed_replacement_text": "Liability is capped at $150,000.",
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": None,
                    "provenance": "model",
                }
            ],
            "critic_delta": None,
        }

        outcome = ls.scan_model_output(model_output, corpus)

        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.confidence_state, "ERROR_MANUAL_REVIEW_REQUIRED")
        self.assertEqual(outcome.field_name, "external_rationale_for_footnote")

    def test_critic_delta_rationale_leak_is_held(self) -> None:
        corpus = _build_test_corpus()
        model_output = {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "verdict_summary": "Acceptable, no issues.",
            "issues": [],
            "critic_delta": {
                "contested_replacements": [
                    {
                        "section_ref": "8",
                        "critic_objection": f"The primary missed rule {HARD_REJECTION_RULE_ID}.",
                        "critic_suggested_replacement": None,
                    }
                ],
                "added_issues": [],
            },
        }

        outcome = ls.scan_model_output(model_output, corpus)

        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.confidence_state, "ERROR_MANUAL_REVIEW_REQUIRED")
        self.assertIn("critic", outcome.field_name)

    def test_clean_model_output_passes_and_is_not_blocked(self) -> None:
        corpus = _build_test_corpus()
        model_output = {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "verdict_summary": "Acceptable, no issues identified by the tool.",
            "issues": [],
            "critic_delta": None,
        }

        outcome = ls.scan_model_output(model_output, corpus)

        self.assertFalse(outcome.blocked)
        self.assertEqual(outcome.confidence_state, "OK")

    def test_internal_precedent_citation_field_is_not_scanned_for_rendering(self) -> None:
        """internal_precedent_citation is retained only in confidential
        audit storage and never rendered in the UI (docs/output-contract.md
        -> Leakage scan scope table: 'n/a (stripped)'). It legitimately
        CONTAINS an internal precedent id, so the scanner must not scan this
        field as a human-surfaced field -- the whole review must not be
        held solely because this system-metadata field carries an internal
        id it is expressly allowed to carry."""
        corpus = _build_test_corpus()
        model_output = {
            "decision": "REQUEST_CHANGE",
            "confidence_state": "OK",
            "verdict_summary": None,
            "issues": [
                {
                    "section_ref": "8",
                    "section_title": "Limitation of Liability",
                    "counterparty_change_summary": "Counterparty removed the cap.",
                    "decision": "REQUEST_CHANGE",
                    "external_rationale_for_footnote": "Restore the standard cap.",
                    "proposed_replacement_text": "Liability is capped at $150,000.",
                    "playbook_topic_id": "limitation-of-liability",
                    "internal_precedent_citation": INTERNAL_PRECEDENT_ID,
                    "provenance": "model",
                }
            ],
            "critic_delta": None,
        }

        outcome = ls.scan_model_output(model_output, corpus)

        self.assertFalse(outcome.blocked)


# ---------------------------------------------------------------------------
# Positive detection blocks generation, routes to ERROR_MANUAL_REVIEW_REQUIRED
# as a SYSTEM status, and writes an audit row (issue #73 AC).
# ---------------------------------------------------------------------------


class TestGateBlocksGenerationWithAuditRow(unittest.TestCase):
    def test_leak_blocks_generation_and_writes_audit_row(self) -> None:
        corpus = _build_test_corpus()
        audit = InMemoryAuditRecorder()
        model_output = {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "verdict_summary": f"Acceptable. {PLAYBOOK_FRAGMENT}",
            "issues": [],
            "critic_delta": None,
        }

        with self.assertRaises(ls.LeakageDetectedError) as ctx:
            ls.run_leakage_gate(
                model_output,
                corpus,
                review_id="review-123",
                audit_write=audit,
            )

        exc = ctx.exception
        self.assertEqual(exc.confidence_state, "ERROR_MANUAL_REVIEW_REQUIRED")
        # System status, never a legal decision.
        self.assertIsNone(getattr(exc, "decision", None))

        self.assertEqual(len(audit.rows), 1)
        row = audit.rows[0]
        self.assertEqual(row["action"], "leakage_scan_blocked")
        self.assertEqual(row["review_id"], "review-123")
        self.assertIn("category", row)
        # Audit row is non-substantive: rule id / category / field name only,
        # never the matched confidential text or the surrounding prose
        # (docs/audit-queries.md -> Notes: "scanner rule IDs", never
        # substantive deltas or raw text).
        self.assertNotIn(PLAYBOOK_FRAGMENT, str(row))

    def test_clean_output_does_not_raise_and_writes_no_audit_row(self) -> None:
        corpus = _build_test_corpus()
        audit = InMemoryAuditRecorder()
        model_output = {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "verdict_summary": "Acceptable, no issues identified by the tool.",
            "issues": [],
            "critic_delta": None,
        }

        result = ls.run_leakage_gate(
            model_output,
            corpus,
            review_id="review-456",
            audit_write=audit,
        )

        self.assertEqual(result, model_output)
        self.assertEqual(len(audit.rows), 0)

    def test_gate_runs_without_audit_write_injected(self) -> None:
        """audit_write is optional (matches upload_validation.py's
        AuditWrite convention) -- the gate still fails closed even if no
        audit sink is wired."""
        corpus = _build_test_corpus()
        model_output = {
            "decision": "ACCEPT",
            "confidence_state": "OK",
            "verdict_summary": f"Acceptable. {SYSTEM_PROMPT_FRAGMENT}",
            "issues": [],
            "critic_delta": None,
        }

        with self.assertRaises(ls.LeakageDetectedError):
            ls.run_leakage_gate(model_output, corpus, review_id="review-789")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
