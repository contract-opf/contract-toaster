#!/usr/bin/env python3
"""
RED test -- heading-rename similarity fallback + absent/structural anchor
exemptions (issue #206).

Audit finding: `scripts/diff_standard_form.py` anchors a draft paragraph to a
standard-form section by heading text ONLY (case/whitespace-normalized
equality, `diff_standard_form.py:121-124,340-369` at audit time). Two
consequences:

  1. A counterparty who renames/renumbers a heading ("Limitation on
     Liability" -> "Limitation of Liability; Indemnification") shreds that
     section into a "deleted" hunk (old heading) plus an unrelated
     "sec-_new" "inserted" hunk (new heading) even when the clause body is
     essentially unchanged -- noisy, and the patch path fails closed for
     that section since there is no exact-heading anchor left to patch.
  2. Anchors registered in the map but never actually present in a real
     draft -- sec-2.2.1 "Without Cause" (deliberately absent from the Exos
     standard form), sec-preamble, sec-signature (structural, no reviewable
     clause) -- get placeholder paragraphs (`diff_standard_form.py:236-241,
     310-313`) that no real draft ever echoes, so EVERY review emits phantom
     "deleted" hunks for them, polluting what the model is told changed.

This test exercises the GREEN fix:
  (1) A similarity fallback tier (DETECTION only, never for patching):
      heading match first, then body-text similarity above a high threshold
      (`RETITLE_SIMILARITY_THRESHOLD`) surfaces a renamed section as ONE
      "possibly_retitled" hunk anchored to the STANDARD anchor, not a
      deleted+inserted pair. `source_text_hash` on that hunk is still the
      OLD standard-side text's hash -- redline patching
      (scripts/redline_patch.py) is completely unaffected by the fuzzy
      match, so this cannot weaken the fail-closed "exact match or no edit"
      guarantee.
  (2) `absent_from_form: true` anchors (build_anchor_map.py) are skipped on
      the deleted path when the draft has no matching heading at all;
      `structural: true` anchors (preamble/signature) are exempt from hunk
      emission entirely, unconditionally.

Both behaviors FAIL on the pre-fix code:
  - (1) fails because heading-only matching produces a "deleted" hunk for
    the old heading and a "sec-_new"-anchored "inserted" hunk for the new
    heading, with no "possibly_retitled" kind at all.
  - (2) fails because sec-2.2.1 / sec-preamble / sec-signature all emit
    "deleted" hunks whenever the draft omits those headings (the normal,
    expected case for every real draft).

Exit codes: 0 = pass, 1 = fail
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))


def _import_modules():
    try:
        import diff_standard_form  # type: ignore
        import redline_patch  # type: ignore
        return diff_standard_form, redline_patch, ""
    except ImportError as exc:
        return None, None, (
            f"MISSING: scripts/diff_standard_form.py or scripts/redline_patch.py "
            f"does not import ({exc})."
        )


def _draft_from_standard(standard, overrides_by_anchor=None, omit_anchors=None):
    """
    Build a full draft paragraph list ({"heading", "text"} only -- no anchor,
    matching what the normalized-upload pipeline actually hands the diff)
    from the standard-form paragraphs, optionally overriding specific
    anchors' heading/text and/or omitting specific anchors entirely (the
    shape of a REAL draft, which never echoes structural/absent placeholder
    headings verbatim).
    """
    overrides_by_anchor = overrides_by_anchor or {}
    omit_anchors = omit_anchors or set()
    draft = []
    for p in standard:
        if p["anchor"] in omit_anchors:
            continue
        if p["anchor"] in overrides_by_anchor:
            override = overrides_by_anchor[p["anchor"]]
            draft.append({
                "heading": override.get("heading", p["heading"]),
                "text": override.get("text", p["text"]),
            })
        else:
            draft.append({"heading": p["heading"], "text": p["text"]})
    return draft


def check_heading_rename_similarity_fallback(mod, standard):
    """
    (1) A renamed/renumbered heading over an otherwise-unchanged clause must
    surface as a single "possibly_retitled" hunk anchored to the STANDARD
    anchor -- never a "deleted" hunk plus a separate "sec-_new" "inserted"
    hunk.
    """
    failures = []

    sec8 = next(p for p in standard if p["anchor"] == "sec-8")
    new_heading = "Limitation of Liability; Indemnification"

    draft = _draft_from_standard(
        standard,
        overrides_by_anchor={
            "sec-8": {"heading": new_heading, "text": sec8["text"]},
        },
    )

    hunks = mod.diff_draft_against_standard(standard, draft)

    sec8_hunks = [h for h in hunks if h["anchor"] == "sec-8"]
    if len(sec8_hunks) != 1:
        failures.append(
            f"[R1] Renaming sec-8's heading must produce EXACTLY ONE hunk anchored "
            f"to 'sec-8', got {len(sec8_hunks)}: {sec8_hunks}"
        )
    else:
        hunk = sec8_hunks[0]
        if hunk["kind"] != "possibly_retitled":
            failures.append(
                f"[R2] Renamed-heading hunk anchored to 'sec-8' must have kind "
                f"'possibly_retitled', got {hunk['kind']!r}. Heading-only matching "
                f"would instead produce a 'deleted' hunk here."
            )
        if hunk.get("source_text_hash") != mod._sha256_text(sec8["text"]):
            failures.append(
                "[R3] 'possibly_retitled' hunk's source_text_hash must still be the "
                "hash of the OLD standard-side text (patching is unaffected by the "
                "fuzzy DETECTION match)."
            )
        if hunk.get("detected_new_heading") != new_heading:
            failures.append(
                f"[R4] 'possibly_retitled' hunk must record the draft's new heading "
                f"(detected_new_heading={hunk.get('detected_new_heading')!r}, "
                f"expected {new_heading!r})."
            )
        if not isinstance(hunk.get("text_similarity"), float) or hunk["text_similarity"] < mod.RETITLE_SIMILARITY_THRESHOLD:
            failures.append(
                f"[R5] 'possibly_retitled' hunk must carry a text_similarity >= "
                f"RETITLE_SIMILARITY_THRESHOLD ({mod.RETITLE_SIMILARITY_THRESHOLD}), "
                f"got {hunk.get('text_similarity')!r}."
            )

    bad_deleted = [h for h in hunks if h["anchor"] == "sec-8" and h["kind"] == "deleted"]
    if bad_deleted:
        failures.append(
            f"[R6] Renaming sec-8's heading must NOT produce a 'deleted' hunk "
            f"anchored to 'sec-8': {bad_deleted}"
        )

    bad_inserted = [
        h for h in hunks
        if h["anchor"] == "sec-_new" and h["kind"] == "inserted"
        and mod._normalize_text(h["text"]) == mod._normalize_text(sec8["text"])
    ]
    if bad_inserted:
        failures.append(
            f"[R7] Renaming sec-8's heading must NOT ALSO produce a separate "
            f"sec-_new 'inserted' hunk carrying the (unchanged) clause body: "
            f"{bad_inserted}"
        )

    # Every other anchor must still diff normally (verbatim except sec-8's
    # heading) -- the similarity fallback must not perturb unrelated anchors.
    other_changed = [
        h for h in hunks
        if h["anchor"] not in ("sec-8",) and h["kind"] not in ("unchanged",)
    ]
    if other_changed:
        failures.append(
            f"[R8] Renaming only sec-8's heading perturbed other anchors' hunk "
            f"kinds (must remain 'unchanged' or be skipped structural/absent "
            f"anchors): {[(h['anchor'], h['kind']) for h in other_changed]}"
        )

    return failures


def check_patching_unaffected_by_retitle_detection(mod, patch_mod, standard):
    """
    The fail-closed patch hash still guarantees no wrong-clause edit: a
    'possibly_retitled' hunk's source_text_hash must validate an exact-match
    patch against the CURRENT (still-OLD-anchor) document state exactly the
    same way a 'modified_new'/'deleted' hunk would -- fuzzier DETECTION
    anchoring must not weaken patching.
    """
    failures = []

    sec8 = next(p for p in standard if p["anchor"] == "sec-8")
    new_heading = "Limitation of Liability; Indemnification"
    draft = _draft_from_standard(
        standard,
        overrides_by_anchor={"sec-8": {"heading": new_heading, "text": sec8["text"]}},
    )
    hunks = mod.diff_draft_against_standard(standard, draft)

    model_issue = {
        "anchor": "sec-8",
        "proposed_replacement_text": "Replacement clause text.",
    }
    patches = patch_mod.join_patches_from_diff(hunks, [model_issue])
    if len(patches) != 1:
        failures.append(f"[P1] Expected exactly one joined patch, got {len(patches)}.")
        return failures
    patch = patches[0]

    # Current document state still has the OLD text at the sec-8 anchor (the
    # document itself has not moved since the diff was computed) -- the
    # exact-match gate must still succeed.
    current = {"sec-8": sec8["text"]}
    result = patch_mod.apply_patch(current, patch)
    if not result["applied"] or result["fail_closed"]:
        failures.append(
            f"[P2] Exact-match patch against a 'possibly_retitled' hunk's anchor "
            f"must still apply cleanly (fail-closed patching unaffected by the "
            f"similarity DETECTION tier). Got: {result}"
        )

    # And it must STILL fail closed on any real drift at that anchor -- the
    # similarity tier must not have loosened the hash check itself.
    drifted = {"sec-8": sec8["text"] + " some drift"}
    drifted_result = patch_mod.apply_patch(drifted, patch)
    if drifted_result["applied"] or not drifted_result["fail_closed"]:
        failures.append(
            f"[P3] A drifted anchor must still fail closed even for a "
            f"'possibly_retitled'-sourced patch. Got: {drifted_result}"
        )

    return failures


def check_absent_and_structural_anchors(mod, standard):
    """
    (2) absent_from_form (sec-2.2.1) and structural (sec-preamble,
    sec-signature) anchors must never emit a phantom "deleted" hunk when a
    real draft omits them -- structural anchors must never emit ANY hunk at
    all, even if a draft happens to echo the heading.
    """
    failures = []

    sec21 = next(p for p in standard if p["anchor"] == "sec-2.2.1")
    preamble = next(p for p in standard if p["anchor"] == "sec-preamble")
    signature = next(p for p in standard if p["anchor"] == "sec-signature")

    if not sec21.get("absent_from_form"):
        failures.append(
            "[A0a] load_standard_form_paragraphs() must carry "
            "absent_from_form=True for sec-2.2.1 (from the anchor map)."
        )
    if not preamble.get("structural") or not signature.get("structural"):
        failures.append(
            "[A0b] load_standard_form_paragraphs() must carry structural=True "
            "for sec-preamble and sec-signature (from the anchor map)."
        )

    # A REAL draft: verbatim everywhere except it never carries the
    # "Without Cause" / "Preamble" / "Signature Block" headings at all --
    # the normal, expected shape of every real counterparty draft.
    realistic_draft = _draft_from_standard(
        standard,
        omit_anchors={"sec-2.2.1", "sec-preamble", "sec-signature"},
    )
    hunks = mod.diff_draft_against_standard(standard, realistic_draft)

    for anchor in ("sec-2.2.1", "sec-preamble", "sec-signature"):
        offending = [h for h in hunks if h["anchor"] == anchor]
        if offending:
            failures.append(
                f"[A1] '{anchor}' must emit NO hunk at all when a real draft omits "
                f"it (absent_from_form/structural exemption), got: {offending}"
            )

    # Every other (non-exempt) standard anchor must still diff normally.
    exempt = {"sec-2.2.1", "sec-preamble", "sec-signature"}
    non_exempt_anchors = {p["anchor"] for p in standard if p["anchor"] not in exempt}
    hunk_anchors = {h["anchor"] for h in hunks}
    missing = non_exempt_anchors - hunk_anchors
    if missing:
        failures.append(
            f"[A2] Non-exempt anchors must still get a hunk on a realistic draft; "
            f"missing hunks for: {sorted(missing)}"
        )

    # Structural anchors are exempt from hunk emission ENTIRELY -- even if the
    # draft happens to carry the exact structural heading verbatim.
    verbatim_structural_draft = _draft_from_standard(standard, omit_anchors={"sec-2.2.1"})
    hunks2 = mod.diff_draft_against_standard(standard, verbatim_structural_draft)
    structural_hunks = [h for h in hunks2 if h["anchor"] in ("sec-preamble", "sec-signature")]
    if structural_hunks:
        failures.append(
            f"[A3] structural anchors must emit no hunk even when the draft "
            f"echoes the structural heading verbatim: {structural_hunks}"
        )

    # If a draft DOES introduce a heading matching an absent_from_form anchor
    # (the counterparty adds the intentionally-absent clause), it must still
    # be detectable -- the deleted-path skip must not become an emission
    # blackout for a real, present paragraph.
    introduced_draft = _draft_from_standard(
        standard,
        overrides_by_anchor={
            "sec-2.2.1": {
                "heading": "Without Cause",
                "text": "Either party may terminate this Agreement for convenience upon 30 days' notice.",
            },
        },
        omit_anchors={"sec-preamble", "sec-signature"},
    )
    hunks3 = mod.diff_draft_against_standard(standard, introduced_draft)
    introduced_hunks = [h for h in hunks3 if h["anchor"] == "sec-2.2.1"]
    if not introduced_hunks:
        failures.append(
            "[A4] A draft that DOES introduce a 'Without Cause' section (matching "
            "the absent_from_form sec-2.2.1 anchor) must still produce a hunk -- "
            "the deleted-path skip must only suppress the 'no draft match at all' "
            "case, not genuine detection of an introduced clause."
        )
    elif introduced_hunks[0]["kind"] == "deleted":
        failures.append(
            f"[A5] An introduced sec-2.2.1 section must not be diffed as 'deleted': "
            f"{introduced_hunks[0]}"
        )

    return failures


def check_deterministic(mod, standard):
    """Same inputs -> same diff, including for the new possibly_retitled kind."""
    failures = []
    sec8 = next(p for p in standard if p["anchor"] == "sec-8")
    draft = _draft_from_standard(
        standard,
        overrides_by_anchor={
            "sec-8": {"heading": "Limitation of Liability; Indemnification", "text": sec8["text"]},
        },
    )
    hunks_a = mod.diff_draft_against_standard(standard, draft)
    hunks_b = mod.diff_draft_against_standard(standard, draft)
    if mod.serialize_diff(hunks_a) != mod.serialize_diff(hunks_b):
        failures.append(
            "[D1] Running the diff twice on an identical rename draft produced "
            "different serialized output."
        )
    if mod.diff_hash(hunks_a) != mod.diff_hash(hunks_b):
        failures.append("[D2] diff_hash() differs across two runs on identical input.")
    return failures


def main():
    mod, patch_mod, err = _import_modules()
    if mod is None:
        print("FAIL: heading-rename / absent-anchor gate cannot run.\n")
        print(f"[G0] {err}")
        sys.exit(1)

    standard = mod.load_standard_form_paragraphs()

    all_failures = []
    all_failures += check_heading_rename_similarity_fallback(mod, standard)
    all_failures += check_patching_unaffected_by_retitle_detection(mod, patch_mod, standard)
    all_failures += check_absent_and_structural_anchors(mod, standard)
    all_failures += check_deterministic(mod, standard)

    if all_failures:
        print("FAIL: heading-rename / absent-anchor gate.\n")
        for f in all_failures:
            print(f)
            print()
        print(f"Total failures: {len(all_failures)}")
        sys.exit(1)
    else:
        print(
            "PASS: heading-rename similarity fallback (DETECTION only, patching "
            "unaffected) + absent_from_form/structural anchor exemptions."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
