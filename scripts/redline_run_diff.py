#!/usr/bin/env python3
"""
Minimal word-level run diff, for rendering <w:ins>/<w:del> at edit
granularity instead of whole-section delete-and-retype.

Issue #207 (audit finding, `re-redline-core`): the real-docx loader joins
all body paragraphs under a heading into one text blob
(`scripts/diff_standard_form.py:267-303`), hunks carry full-section text,
and a patch replaces the whole section (`apply_patch` returns
`proposed_replacement_text` for the anchor, `scripts/redline_patch.py`).
When that reaches the `<w:ins>`/`<w:del>` writer (`scripts/redline_docx_writer.py`,
issue #198), the tracked change Word displays is "entire section deleted,
entire new section inserted" -- even when the actual edit is restoring one
number. Attorneys evaluating redlines rely on seeing the minimal edit;
whole-clause swaps force them to re-read and manually diff both versions.

## What this module is

A pure text-in, runs-out helper: given a section's pre-patch text and its
`proposed_replacement_text`, compute a word-level diff (stdlib `difflib`
over whitespace-preserving word tokens) and emit the MINIMAL sequence of
`{"type": "unchanged" | "del" | "ins", "text": ...}` runs describing the
edit -- unchanged text carried as plain runs, only the actually-changed
tokens as `del`/`ins`. This is a rendering-input concern only; it does not
itself write OOXML (that stays `redline_docx_writer.py`'s job) and it does
not change section-level anchoring or hash validation at all.

## Section-level anchoring + hash validation stay the safety envelope

`compute_minimal_diff_for_patch()` does not re-implement or loosen any part
of the anchor/hash safety envelope: it calls `redline_patch.apply_patch()`
unchanged and, ONLY on that function's existing `applied=True` outcome,
additionally computes word-level `runs` between the pre-patch anchor text
and `new_text`. On the existing `applied=False` (`fail_closed=True`)
outcome, this module returns `apply_patch()`'s result completely
unmodified -- no diff is computed over unvalidated text, and there is no
new fail-closed path or reason code introduced here. "Apply the closest
match" remains prohibited (docs/phase-0-issues.md item 17); this module
only decides how to RENDER an already-validated, exact-match edit.

Usage:
  from redline_run_diff import compute_word_diff_runs, compute_minimal_diff_for_patch

  runs = compute_word_diff_runs(source_text, replacement_text)
  # [{"type": "unchanged", "text": "..."}, {"type": "del", "text": "150,000"},
  #  {"type": "ins", "text": "200,000"}, {"type": "unchanged", "text": "..."}]

  result = compute_minimal_diff_for_patch(current_paragraphs_by_anchor, patch)
  # applied:     {"applied": True, "fail_closed": False, "anchor": ..., "new_text": ..., "runs": [...]}
  # fail closed: {"applied": False, "fail_closed": True, "anchor": ..., "reason": "hash_mismatch_at_patch"}
  #              (identical to redline_patch.apply_patch()'s own return -- no "runs" key)
"""

import difflib
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import redline_patch  # noqa: E402

# Tokenize into words and whitespace runs (each kept as its own token) so
# joining a slice of tokens back together reproduces the original text
# exactly -- no information (spacing, punctuation) is lost by tokenizing,
# which is what lets emitted runs be concatenated back into faithful
# unchanged/del/ins text for the OOXML writer.
_TOKEN_RE = re.compile(r"\s+|\S+")


def _tokenize(text: str) -> list:
    return _TOKEN_RE.findall(text)


def compute_word_diff_runs(source_text: str, replacement_text: str) -> list:
    """
    Compute the minimal word-level diff between `source_text` and
    `replacement_text`, returning an ordered list of runs:

      [{"type": "unchanged", "text": "..."}, ...]
      [{"type": "del", "text": "..."}, ...]        (present in source only)
      [{"type": "ins", "text": "..."}, ...]        (present in replacement only)

    Concatenating every run's "text" in order, using only the "unchanged"
    and "del" runs, reproduces `source_text` exactly; concatenating only
    "unchanged" and "ins" runs reproduces `replacement_text` exactly.

    Uses `difflib.SequenceMatcher` over whitespace-preserving word tokens
    (not characters, not whole paragraphs) -- standard technique for
    "restoring one number in a sentence" style edits, per the issue's
    suggested direction. `autojunk=False` because legal prose can have a
    single very-common token (e.g. "the") appear enough times to otherwise
    trip SequenceMatcher's heuristic junk detection and produce a worse
    (non-minimal) match.
    """
    source_tokens = _tokenize(source_text)
    replacement_tokens = _tokenize(replacement_text)
    matcher = difflib.SequenceMatcher(
        a=source_tokens, b=replacement_tokens, autojunk=False
    )

    runs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            text = "".join(source_tokens[i1:i2])
            if text:
                runs.append({"type": "unchanged", "text": text})
            continue

        # "replace", "delete", "insert" all fall through here. Emit del
        # before ins (matches OOXML/Word convention: struck-through original
        # first, then the inserted replacement) -- only the tokens that
        # actually differ, never the whole section.
        del_text = "".join(source_tokens[i1:i2])
        ins_text = "".join(replacement_tokens[j1:j2])
        if del_text:
            runs.append({"type": "del", "text": del_text})
        if ins_text:
            runs.append({"type": "ins", "text": ins_text})

    return runs


def compute_minimal_diff_for_patch(
    current_paragraphs_by_anchor: dict, patch: dict
) -> dict:
    """
    Validate `patch` via the UNCHANGED anchor/hash safety envelope
    (`redline_patch.apply_patch`), and only on an exact-match `applied=True`
    outcome, additionally compute the minimal word-level `runs` between the
    pre-patch text at `patch["anchor"]` and `patch["proposed_replacement_text"]`.

    Returns `apply_patch()`'s result dict, with one extra key on success:

      applied:     {..., "runs": [{"type": ..., "text": ...}, ...]}
      fail closed: identical to apply_patch()'s own return -- unmodified,
                   no "runs" key, no diff computed over unvalidated text.
    """
    result = redline_patch.apply_patch(current_paragraphs_by_anchor, patch)
    if not result["applied"]:
        return result

    original_text = current_paragraphs_by_anchor.get(result["anchor"], "")
    new_text = result.get("new_text") or ""
    result = dict(result)
    result["runs"] = compute_word_diff_runs(original_text, new_text)
    return result


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """
    CLI smoke test: diff a trivial single-number change and print the
    resulting runs. Useful for a quick manual sanity check; the gate test
    (tests/redline/test_minimal_run_diff.py) is the authoritative check.
    """
    source = "Each party's aggregate liability shall not exceed $150,000."
    replacement = "Each party's aggregate liability shall not exceed $200,000."
    for run in compute_word_diff_runs(source, replacement):
        print(run)


if __name__ == "__main__":
    main()
    sys.exit(0)
