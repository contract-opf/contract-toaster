#!/usr/bin/env python3
"""
RED test -- issue #247: third-party paper, deterministic form-match router
(entry point, Slice 1 of 5 of #247 -> #248 -> #249 -> #250 -> #251).

## What this proves

`scripts/form_match_router.py` does not exist yet. Without it, EVERY upload
-- whether or not it is actually based on your standard form -- runs
straight through the form-anchored diff/detector/LLM pipeline
(`scripts/review_spine.py`). A counterparty's own template produces the
"everything-deleted / everything-inserted degenerate diff" failure
#18/#192/#202 describe, instead of being routed to a path built for
non-derivative paper.

This test builds real standard-form paragraphs via
`diff_standard_form.load_standard_form_paragraphs()` (synthetic mode -- no
real .docx needed, per issue #247's Out-of-scope) and hand-built draft
paragraph lists, diffs them with `diff_standard_form.diff_draft_against_
standard()` (both already exist and are independently tested -- see
`tests/diff/test_deterministic_diff.py`), and feeds the result to the new
router:

  1. `compute_form_match()` scores a verbatim your-form draft ~= 1.0, and a
     clean-ACCEPT derivative draft (one section lightly reworded) above
     threshold.
  2. A synthetic counterparty-own-form draft (unrelated headings and body
     text) and a wholesale-restructure draft (your content, merged into
     different sections under new headings and reworded prose) both score
     BELOW threshold.
  3. `route_upload()` returns `FIRST_PARTY_DIFF` for the two derivative
     cases and `THIRD_PARTY_POSITIONS` (NOT `MANUAL_REVIEW_REQUIRED`) for
     the two non-derivative cases -- proving the owner-approved third-party
     route is taken, not the old manual-review off-ramp.
  4. An unnormalizable/scanned-input fixture (extraction_normalization_
     stage's own `status != "normalized"` result) routes to
     `MANUAL_REVIEW_REQUIRED`.
  5. The threshold is read from the release bundle
     (`bundle.playbook.metadata.form_match_threshold`); a bundle without it
     fails closed (`resolve_form_match_threshold()` raises, and
     `route_upload()` itself returns `MANUAL_REVIEW_REQUIRED` rather than
     silently defaulting or crashing). A change to the threshold value
     changes the bundle's canonical `content_hash`
     (`scripts/canonicalize.py`) -- proving the threshold is genuinely part
     of the hashed release bundle, with zero changes needed to
     `canonicalize.py` itself.
  6. Every router-emitted user-facing string (`MSG_THIRD_PARTY_ROUTE`,
     `MSG_UNCLASSIFIABLE`, `MSG_MISSING_THRESHOLD`, and every
     `user_message` a call to `route_upload()` can produce) is free of
     'Exos'/'EXOS' and uses "your" voicing.

Fails on the pre-fix tree because `scripts/form_match_router.py` does not
exist -- there is no `form_match` metric, no route decision, and every
upload (derivative or not) runs the full form-anchored pipeline.

Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import diff_standard_form  # type: ignore  # noqa: E402


def _import_router_module():
    try:
        import form_match_router  # type: ignore
        return form_match_router, ""
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/form_match_router.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement the deterministic form-match router (issue #247) -- "
            f"compute_form_match(), resolve_form_match_threshold(), "
            f"classify_route(), route_upload()."
        )


# ---------------------------------------------------------------------------
# Fixtures (synthetic-mode standard form + hand-built draft paragraph lists;
# no real .docx needed -- issue #247 Out-of-scope: "Committing a real
# standard-form .docx (synthetic-mode inputs are fine here)").
# ---------------------------------------------------------------------------

def _load_standard():
    return diff_standard_form.load_standard_form_paragraphs(playbook_id="eiaa")


def _verbatim_draft(standard):
    return [{"heading": p["heading"], "text": p["text"]} for p in standard]


def _clean_accept_draft(standard):
    draft = _verbatim_draft(standard)
    for p in draft:
        if p["heading"] == "Limitation on Liability":
            p["text"] = p["text"] + " This cap does not apply in cases of gross negligence."
    return draft


def _counterparty_own_form_draft():
    return [
        {
            "heading": "Article I: Purpose and Scope",
            "text": (
                "This Master Services Agreement governs the provision of clinical "
                "placement slots by Provider to Client under a fee-for-service "
                "arrangement negotiated annually."
            ),
        },
        {
            "heading": "Article II: Fees and Payment",
            "text": (
                "Client shall remit payment net thirty days of invoice for each "
                "placement slot utilized, at the rate schedule attached as "
                "Exhibit A, subject to annual CPI adjustment."
            ),
        },
        {
            "heading": "Article III: Data Security",
            "text": (
                "Provider shall maintain commercially reasonable administrative, "
                "technical, and physical safeguards to protect Client data "
                "transmitted under this Agreement, consistent with industry "
                "frameworks such as SOC 2."
            ),
        },
        {
            "heading": "Article IV: Dispute Resolution",
            "text": (
                "Any dispute arising under this Agreement shall be resolved "
                "through binding arbitration administered by JAMS in the state "
                "of Delaware, with each party bearing its own costs."
            ),
        },
        {
            "heading": "Article V: General Provisions",
            "text": (
                "This Agreement constitutes the entire understanding between the "
                "parties and supersedes all prior negotiations, representations, "
                "or agreements, whether written or oral, relating to its subject "
                "matter."
            ),
        },
    ]


def _wholesale_restructure_draft():
    return [
        {
            "heading": "Part A: Program Framework",
            "text": (
                "The parties agree that the training program described in this "
                "instrument, together with the rules for admitting participants, "
                "conducting site visits, and dismissing a participant for cause, "
                "shall be governed collectively by the operational policies each "
                "party separately maintains and may update from time to time "
                "without amending this instrument."
            ),
        },
        {
            "heading": "Part B: Compensation and Coverage",
            "text": (
                "No participant receives wages, stipends, or other compensation "
                "of any kind for time spent under this arrangement, and neither "
                "party is responsible for procuring insurance, workers "
                "compensation coverage, or fringe benefits of any kind on behalf "
                "of a participant or the other party's personnel."
            ),
        },
        {
            "heading": "Part C: Duration and Exit",
            "text": (
                "This arrangement begins on the effective date and continues "
                "until either side elects to walk away, which either side may do "
                "for any reason or no reason on written notice, or immediately "
                "upon a material breach that the breaching side fails to cure "
                "within the cure window described in the policies."
            ),
        },
        {
            "heading": "Part D: Regulatory and Equal Treatment",
            "text": (
                "Each side commits to following the patchwork of federal, state, "
                "and local rules that touch this arrangement, and neither side "
                "will treat a participant differently because of a "
                "characteristic the law says cannot be used as a basis for "
                "differential treatment."
            ),
        },
        {
            "heading": "Part E: Records and Privacy",
            "text": (
                "Participant records and any protected health information "
                "encountered in the course of this arrangement will be handled "
                "the way applicable privacy law requires, and neither side will "
                "disclose the other side's confidential business information to "
                "an outside party without permission."
            ),
        },
        {
            "heading": "Part F: Money Damages Ceiling",
            "text": (
                "Should a dispute arise, the amount either side can collect from "
                "the other is capped, and neither side owes the other for "
                "indirect or downstream losses, though this ceiling does not "
                "shield either side from the consequences of its own reckless or "
                "intentional misconduct."
            ),
        },
        {
            "heading": "Part G: Boilerplate",
            "text": (
                "Notices go to the addresses on file, this arrangement is not "
                "exclusive of either side's other dealings, this writing is the "
                "whole deal between the sides, and if this writing conflicts "
                "with an attached exhibit, this writing controls."
            ),
        },
    ]


def _bundle(threshold):
    bundle = {"playbook": {"id": "eiaa", "metadata": {}}}
    if threshold is not None:
        bundle["playbook"]["metadata"]["form_match_threshold"] = threshold
    return bundle


_NORMALIZED_OK = {"status": "normalized", "paragraphs": []}
_NORMALIZED_BAD = {
    "status": "unnormalizable_input",
    "analysis_report": {"reason": "scanned_image_or_non_english_body"},
}


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def test_verbatim_and_clean_accept_score_above_threshold(failures, mod, standard):
    verbatim_draft = _verbatim_draft(standard)
    verbatim_hunks = diff_standard_form.diff_draft_against_standard(standard, verbatim_draft)
    verbatim_score = mod.compute_form_match(standard, verbatim_draft, verbatim_hunks)
    if verbatim_score < 0.95:
        failures.append(
            f"[1a] Verbatim your-form draft must score form_match ~= 1.0; got {verbatim_score!r}."
        )

    accept_draft = _clean_accept_draft(standard)
    accept_hunks = diff_standard_form.diff_draft_against_standard(standard, accept_draft)
    accept_score = mod.compute_form_match(standard, accept_draft, accept_hunks)
    threshold = 0.6
    if accept_score < threshold:
        failures.append(
            f"[1b] Clean-ACCEPT derivative draft must score above threshold "
            f"{threshold}; got {accept_score!r}."
        )


def test_non_derivative_drafts_score_below_threshold(failures, mod, standard):
    threshold = 0.6

    own_form_draft = _counterparty_own_form_draft()
    own_form_hunks = diff_standard_form.diff_draft_against_standard(standard, own_form_draft)
    own_form_score = mod.compute_form_match(standard, own_form_draft, own_form_hunks)
    if own_form_score >= threshold:
        failures.append(
            f"[2a] A synthetic counterparty own-form document must score "
            f"form_match below threshold {threshold}; got {own_form_score!r}."
        )

    restructure_draft = _wholesale_restructure_draft()
    restructure_hunks = diff_standard_form.diff_draft_against_standard(standard, restructure_draft)
    restructure_score = mod.compute_form_match(standard, restructure_draft, restructure_hunks)
    if restructure_score >= threshold:
        failures.append(
            f"[2b] A wholesale-restructure of your terms must score form_match "
            f"below threshold {threshold}; got {restructure_score!r}."
        )


def test_router_emits_first_party_for_derivative_cases(failures, mod, standard):
    bundle = _bundle(0.6)

    for label, draft_builder in (
        ("verbatim", lambda: _verbatim_draft(standard)),
        ("clean-accept", lambda: _clean_accept_draft(standard)),
    ):
        draft = draft_builder()
        hunks = diff_standard_form.diff_draft_against_standard(standard, draft)
        normalized = {"status": "normalized", "paragraphs": draft}
        result = mod.route_upload(
            normalized=normalized, standard=standard, hunks=hunks, bundle=bundle
        )
        if result["status"] != mod.STATUS_OK:
            failures.append(
                f"[3a-{label}] route_upload() status must be OK for a derivative "
                f"draft; got {result['status']!r} (reason={result.get('reason')!r})."
            )
        if result["route"] != mod.ROUTE_FIRST_PARTY_DIFF:
            failures.append(
                f"[3a-{label}] route_upload() must return FIRST_PARTY_DIFF for a "
                f"derivative draft; got {result['route']!r}."
            )


def test_router_emits_third_party_positions_not_manual_review(failures, mod, standard):
    bundle = _bundle(0.6)

    for label, draft_builder in (
        ("own-form", _counterparty_own_form_draft),
        ("restructure", _wholesale_restructure_draft),
    ):
        draft = draft_builder()
        hunks = diff_standard_form.diff_draft_against_standard(standard, draft)
        normalized = {"status": "normalized", "paragraphs": draft}
        result = mod.route_upload(
            normalized=normalized, standard=standard, hunks=hunks, bundle=bundle
        )
        if result["status"] != mod.STATUS_OK:
            failures.append(
                f"[3b-{label}] route_upload() status must still be OK (a route WAS "
                f"decided) for a non-derivative draft; got {result['status']!r}."
            )
        if result["route"] != mod.ROUTE_THIRD_PARTY_POSITIONS:
            failures.append(
                f"[3b-{label}] route_upload() must return THIRD_PARTY_POSITIONS -- "
                f"the owner-approved third-party route (issue #279), NOT the old "
                f"manual-review off-ramp -- for a non-derivative draft; got "
                f"{result['route']!r}."
            )
        if result["route"] == mod.ROUTE_THIRD_PARTY_POSITIONS and not result.get("user_message"):
            failures.append(
                f"[3b-{label}] THIRD_PARTY_POSITIONS route must carry a de-branded "
                f"user_message; got {result.get('user_message')!r}."
            )


def test_unclassifiable_input_routes_to_manual_review(failures, mod, standard):
    bundle = _bundle(0.6)
    result = mod.route_upload(
        normalized=_NORMALIZED_BAD,
        standard=standard,
        hunks=[],
        bundle=bundle,
    )
    if result["status"] != mod.STATUS_MANUAL_REVIEW_REQUIRED:
        failures.append(
            f"[4] An unnormalizable/scanned-image input must route to "
            f"MANUAL_REVIEW_REQUIRED; got status={result['status']!r}."
        )
    if result["route"] is not None:
        failures.append(
            f"[4b] MANUAL_REVIEW_REQUIRED result must carry no route decision; got "
            f"{result['route']!r}."
        )
    if not result.get("user_message"):
        failures.append("[4c] MANUAL_REVIEW_REQUIRED result must carry a de-branded user_message.")


def test_threshold_read_from_bundle_and_missing_threshold_fails_closed(failures, mod, standard):
    bundle_with_threshold = _bundle(0.6)
    threshold = mod.resolve_form_match_threshold(bundle_with_threshold)
    if threshold != 0.6:
        failures.append(
            f"[5a] resolve_form_match_threshold() must read "
            f"bundle.playbook.metadata.form_match_threshold; got {threshold!r}."
        )

    bundle_missing = _bundle(None)
    raised = False
    try:
        mod.resolve_form_match_threshold(bundle_missing)
    except mod.FormMatchThresholdMissingError:
        raised = True
    if not raised:
        failures.append(
            "[5b] resolve_form_match_threshold() must raise "
            "FormMatchThresholdMissingError (fail closed) for a bundle with no "
            "form_match_threshold field."
        )

    draft = _verbatim_draft(standard)
    hunks = diff_standard_form.diff_draft_against_standard(standard, draft)
    normalized = {"status": "normalized", "paragraphs": draft}
    result = mod.route_upload(
        normalized=normalized, standard=standard, hunks=hunks, bundle=bundle_missing
    )
    if result["status"] != mod.STATUS_MANUAL_REVIEW_REQUIRED:
        failures.append(
            f"[5c] route_upload() against a bundle missing form_match_threshold "
            f"must fail closed to MANUAL_REVIEW_REQUIRED (never a silent default, "
            f"never an unhandled exception, never FIRST_PARTY_DIFF/"
            f"THIRD_PARTY_POSITIONS); got status={result['status']!r}, "
            f"route={result.get('route')!r}."
        )
    if not result.get("user_message"):
        failures.append(
            "[5d] The missing-threshold fail-closed result must carry a "
            "de-branded user_message."
        )


def test_threshold_participates_in_release_bundle_hash(failures, mod, standard):
    import canonicalize  # type: ignore  # noqa: E402

    doc_a = {"playbook": {"id": "eiaa", "metadata": {"form_match_threshold": 0.6}}}
    doc_b = {"playbook": {"id": "eiaa", "metadata": {"form_match_threshold": 0.75}}}

    hash_a = canonicalize.content_hash(doc_a)
    hash_b = canonicalize.content_hash(doc_b)
    if hash_a == hash_b:
        failures.append(
            "[5e] Two release bundles differing ONLY in "
            "playbook.metadata.form_match_threshold must produce different "
            "canonicalize.content_hash() values -- the threshold must be part of "
            "the hashed release bundle (issue #247 Scope: 'a threshold change "
            "forces a new bundle')."
        )

    hash_a_again = canonicalize.content_hash({"playbook": {"id": "eiaa", "metadata": {"form_match_threshold": 0.6}}})
    if hash_a != hash_a_again:
        failures.append("[5f] content_hash() must be deterministic for identical bundles.")


def test_router_strings_are_debranded(failures, mod, standard):
    banned = ("exos", "EXOS", "Exos")

    static_strings = [mod.MSG_THIRD_PARTY_ROUTE, mod.MSG_UNCLASSIFIABLE, mod.MSG_MISSING_THRESHOLD]
    for s in static_strings:
        for bad in banned:
            if bad.lower() in s.lower() and bad.lower() == "exos":
                failures.append(f"[6a] Router string contains 'Exos'/'EXOS' branding: {s!r}")
                break

    if "your" not in mod.MSG_THIRD_PARTY_ROUTE.lower():
        failures.append(
            f"[6b] MSG_THIRD_PARTY_ROUTE must use 'your' voicing per issue #247 "
            f"Scope; got {mod.MSG_THIRD_PARTY_ROUTE!r}."
        )

    bundle = _bundle(0.6)
    own_form_draft = _counterparty_own_form_draft()
    own_form_hunks = diff_standard_form.diff_draft_against_standard(standard, own_form_draft)
    result = mod.route_upload(
        normalized={"status": "normalized", "paragraphs": own_form_draft},
        standard=standard,
        hunks=own_form_hunks,
        bundle=bundle,
    )
    all_messages = [
        result.get("user_message"),
        mod.route_upload(normalized=_NORMALIZED_BAD, standard=standard, hunks=[], bundle=bundle).get(
            "user_message"
        ),
        mod.route_upload(
            normalized={"status": "normalized", "paragraphs": []},
            standard=standard,
            hunks=[],
            bundle=_bundle(None),
        ).get("user_message"),
    ]
    for msg in all_messages:
        if msg and "exos" in msg.lower():
            failures.append(f"[6c] Router-emitted user_message contains 'Exos'/'EXOS' branding: {msg!r}")


TESTS = [
    test_verbatim_and_clean_accept_score_above_threshold,
    test_non_derivative_drafts_score_below_threshold,
    test_router_emits_first_party_for_derivative_cases,
    test_router_emits_third_party_positions_not_manual_review,
    test_unclassifiable_input_routes_to_manual_review,
    test_threshold_read_from_bundle_and_missing_threshold_fails_closed,
    test_threshold_participates_in_release_bundle_hash,
    test_router_strings_are_debranded,
]


def main() -> int:
    mod, missing_msg = _import_router_module()
    if mod is None:
        print("FAIL: form-match router (issue #247).\n")
        print(missing_msg)
        print("\nTotal failures: 1")
        return 1

    standard = _load_standard()

    failures: list[str] = []
    for test in TESTS:
        before = len(failures)
        try:
            test(failures, mod, standard)
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
    print("PASS: form-match router (issue #247) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
