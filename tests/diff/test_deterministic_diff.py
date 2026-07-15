#!/usr/bin/env python3
"""
RED test — deterministic standard-form diff generator.

Issue #64 (BLOCKING GATE): "Standard-form storage + deterministic diff".

The review pipeline must not hand the model a bare uploaded document. It must
diff the (normalized) uploaded draft against the canonical Exos standard form
for the active playbook version, anchored to the same `section_anchor` values
the anchor map (issue #3) and detector layer (issue #1) already use, and feed
the model the diff plus anchored clause text — not just the upload.

This test exercises `scripts/diff_standard_form.py`, which does not exist yet,
on known draft/standard pairs:

  1. Verbatim draft (identical to the standard form) -> the empty diff: every
     hunk is "unchanged", nothing inserted/deleted. This is the D1 zero-fire
     baseline input (docs/evaluation.md).
  2. A draft that MODIFIES an existing standard-form section (deletes the
     $150,000 cap and consequential-damages waiver from sec-8) -> a hunk
     anchored to "sec-8" tagged "deleted" (or "modified_new" for the
     replacement text), carrying a source_text_hash of the deleted standard
     text so the redline-patching path (issue #17) can rely on it.
  3. A draft that INSERTS a wholly new standalone section (a standalone
     indemnification article that doesn't correspond to any existing standard
     section) -> a hunk tagged "inserted" anchored to the reserved pseudo-anchor
     "sec-_new" (ARCHITECTURE.md -> "Reserved pseudo-anchor sec-_new").

Determinism requirement (AC): the SAME inputs must produce the SAME diff. This
test runs the generator twice on the same pair and asserts byte-identical
output (a stable JSON serialization / hash), and asserts hunk ordering does
not depend on dict iteration order.

This test FAILS today (RED) because scripts/diff_standard_form.py does not
exist -- there is no deterministic diff generator, so the model would only
ever see the bare uploaded document.

Exit codes: 0 = pass, 1 = fail
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SCRIPTS_DIR))


def _import_diff_module():
    try:
        import diff_standard_form  # type: ignore
        return diff_standard_form, ""
    except ImportError as exc:
        return None, (
            f"MISSING: scripts/diff_standard_form.py does not exist or fails to "
            f"import ({exc}).\n"
            f"  FIX: implement the deterministic standard-form diff generator "
            f"(issue #64)."
        )


def _load_fixture(name):
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


def _apply_replacement(standard, replacement_paragraphs):
    """
    Build a full draft paragraph list: start from the verbatim standard-form
    paragraphs, and for each replacement paragraph either overwrite the
    standard paragraph with the matching heading (a modification), or append
    it if no standard paragraph has that heading (a wholly new section).

    This keeps the fixtures minimal (they only declare what the counterparty
    changed) while still exercising the generator on a realistic "mostly
    verbatim, N changes" draft -- the shape a real diff will actually see.
    """
    draft = [dict(p) for p in standard]
    headings_in_standard = {p["heading"]: i for i, p in enumerate(draft)}

    for repl in replacement_paragraphs:
        heading = repl["heading"]
        if heading in headings_in_standard:
            idx = headings_in_standard[heading]
            draft[idx] = {"heading": heading, "text": repl["text"]}
        else:
            draft.append({"heading": heading, "text": repl["text"]})

    return draft


def main():
    failures = []

    mod, err = _import_diff_module()
    if mod is None:
        print("FAIL: deterministic diff generator gate cannot run.\n")
        print(f"[G0] {err}")
        sys.exit(1)

    standard = mod.load_standard_form_paragraphs()

    # --- Case 1: verbatim draft -> empty diff -------------------------------
    # The verbatim draft fixture is intentionally derived from the standard-form
    # paragraphs themselves (not hand-copied) so this case can never drift from
    # whatever the standard-form text actually is.
    verbatim_case = _load_fixture("verbatim-draft.json")
    assert verbatim_case["draft"] == "__VERBATIM__", (
        "verbatim-draft.json must use the __VERBATIM__ sentinel; the actual "
        "paragraph text is derived from load_standard_form_paragraphs() below "
        "so this fixture can never silently drift from the real standard text."
    )
    verbatim_draft = [dict(p) for p in standard]
    hunks_verbatim = mod.diff_draft_against_standard(standard, verbatim_draft)

    changed = [h for h in hunks_verbatim if h["kind"] != "unchanged"]
    if changed:
        failures.append(
            "[G1] Verbatim draft produced non-empty diff: expected every hunk "
            f"'unchanged', got kinds {sorted({h['kind'] for h in changed})} "
            f"on anchors {[h['anchor'] for h in changed]}."
        )
    if not hunks_verbatim:
        failures.append(
            "[G1] Verbatim draft produced zero hunks -- diff must still enumerate "
            "the full standard form as 'unchanged' hunks."
        )

    # --- Case 2: modification of an existing section (sec-8) ---------------
    modify_case = _load_fixture("modify-liability-cap.json")
    modify_draft = _apply_replacement(standard, modify_case["draft"])
    hunks_modify = mod.diff_draft_against_standard(standard, modify_draft)

    sec8_hunks = [h for h in hunks_modify if h["anchor"] == "sec-8"]
    sec8_deleted_or_modified = [
        h for h in sec8_hunks if h["kind"] in ("deleted", "modified_new", "modified_old")
    ]
    if not sec8_deleted_or_modified:
        failures.append(
            "[G2] Modifying the sec-8 liability cap produced no deleted/modified "
            f"hunk anchored to 'sec-8'. Got hunks: {sec8_hunks}"
        )
    else:
        for h in sec8_deleted_or_modified:
            if "source_text_hash" not in h or not h["source_text_hash"]:
                failures.append(
                    f"[G2b] sec-8 hunk (kind={h['kind']}) missing 'source_text_hash' -- "
                    "the redline-patching path (issue #17) needs a hash of the "
                    "source text it intends to replace."
                )

    # required_tokens for preserve-liability-cap / preserve-consequential-damages-waiver
    # must have been present in the STANDARD side before the delete, proving the
    # synthetic standard-form text actually carries the protected tokens.
    standard_sec8_text = " ".join(
        p["text"] for p in standard if p["anchor"] == "sec-8"
    )
    for token in ("$150,000", "aggregate liability", "consequential damages"):
        if token not in standard_sec8_text:
            failures.append(
                f"[G2c] Standard-form sec-8 text does not contain required token "
                f"'{token}' -- on_remove_or_alter rules guarding an absent token "
                f"are dead config (ARCHITECTURE.md CI rule)."
            )

    # --- Case 3: wholly new inserted section -> sec-_new --------------------
    insert_case = _load_fixture("insert-new-indemnification-section.json")
    insert_draft = _apply_replacement(standard, insert_case["draft"])
    hunks_insert = mod.diff_draft_against_standard(standard, insert_draft)

    new_section_hunks = [
        h for h in hunks_insert
        if h["kind"] in ("inserted", "modified_new") and "indemnif" in h["text"].lower()
    ]
    if not new_section_hunks:
        failures.append(
            "[G3] Inserting a wholly new standalone indemnification article "
            "produced no inserted hunk containing 'indemnif'."
        )
    else:
        wrong_anchor = [h for h in new_section_hunks if h["anchor"] != "sec-_new"]
        if wrong_anchor:
            failures.append(
                "[G3b] New standalone section not anchored to reserved pseudo-anchor "
                f"'sec-_new': got anchors {[h['anchor'] for h in wrong_anchor]}. "
                "Per ARCHITECTURE.md, a hunk that does not fall inside any existing "
                "standard-form section must be tagged sec-_new so not_in_standard "
                "on_insert rules have a non-empty scope (issue #1)."
            )

    # sec-_new must never be assigned to a deleted or unmodified hunk.
    bad_sec_new = [
        h for h in hunks_insert
        if h["anchor"] == "sec-_new" and h["kind"] in ("deleted", "unchanged")
    ]
    if bad_sec_new:
        failures.append(
            "[G3c] sec-_new assigned to a deleted/unmodified hunk -- ARCHITECTURE.md: "
            "'sec-_new is assigned only to inserted/modified-new hunks; it is never "
            f"assigned to deleted or unmodified hunks.' Offending hunks: {bad_sec_new}"
        )

    # --- Determinism: same inputs -> same diff ------------------------------
    hunks_modify_again = mod.diff_draft_against_standard(standard, modify_draft)
    ser1 = mod.serialize_diff(hunks_modify)
    ser2 = mod.serialize_diff(hunks_modify_again)
    if ser1 != ser2:
        failures.append(
            "[G4] Running the diff twice on identical inputs produced different "
            "serialized output -- the diff must be deterministic (same inputs -> "
            "same diff)."
        )

    if mod.diff_hash(hunks_modify) != mod.diff_hash(hunks_modify_again):
        failures.append(
            "[G4b] diff_hash() differs across two runs on identical inputs."
        )

    # --- Anchors resolve to real sections (except sec-_new) -----------------
    known_anchors = {p["anchor"] for p in standard} | {"sec-_new"}
    for h in hunks_verbatim + hunks_modify + hunks_insert:
        if h["anchor"] not in known_anchors:
            failures.append(
                f"[G5] Hunk anchor '{h['anchor']}' does not resolve to a real "
                "standard-form section and is not the sec-_new pseudo-anchor."
            )

    # --- Report ---------------------------------------------------------------
    if failures:
        print("FAIL: deterministic standard-form diff gate.\n")
        for f in failures:
            print(f)
            print()
        print(f"Total failures: {len(failures)}")
        sys.exit(1)
    else:
        print(
            f"PASS: deterministic standard-form diff gate. "
            f"{len(hunks_verbatim)} hunks (verbatim), "
            f"{len(hunks_modify)} hunks (sec-8 modification), "
            f"{len(hunks_insert)} hunks (new section insertion)."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
