# `hyperlink-cross-reference.SYNTHETIC.docx` — provenance

Issue #663 (`<w:hyperlink>` text never reached the extracted document).

Unlike this repo's other OOXML fixtures, this one is **not** assembled by a
test out of raw XML strings. That is the whole point of it: a hand-built
`<w:hyperlink>` fixture only proves the extractor handles the shape the test
author imagined, and #663 was exactly a case where the shape a real word
processor emits was never exercised. This file was produced by a real
word-processor toolchain, from synthetic source text, and is committed as
bytes so the regression is pinned against markup nobody in this repo wrote.

The document is **SYNTHETIC**: invented clause text, no real party names, no
real agreement.

## How it was generated

```
pandoc -o hyperlink-cross-reference.SYNTHETIC.docx fixture.md
```

with `fixture.md`:

```markdown
# Indemnity {#indemnity}

The Supplier shall indemnify the Customer against any Loss arising under [Section 7.2](#limitation-of-liability) and the [Data Protection Addendum](https://example.invalid/synthetic/dpa), subject to the cap set out in this Agreement.

# Limitation of Liability {#limitation-of-liability}

Each party's aggregate liability under this Agreement shall not exceed $150,000. This SYNTHETIC sample document contains no real party names and no real agreement text.
```

## What it carries that matters

- **Both** hyperlink forms in one paragraph: an internal cross-reference
  (`<w:hyperlink w:anchor="...">`, the defined-term / "see Section N" case)
  and an external link (`<w:hyperlink r:id="rId9">` with a real
  `TargetMode="External"` relationship in
  `word/_rels/document.xml.rels`). The extractor reads neither attribute —
  it never opens the rels part at all — so the two forms must extract
  identically.
- Hyperlink runs carrying the `Hyperlink` character style in their own
  `<w:rPr>`, with the surrounding words in separate sibling runs, i.e. the
  extracted clause text is only correct if the walk descends into the
  wrapper and keeps document order across it.
- A full, well-formed package (`styles.xml`, `settings.xml`, `theme/`,
  `docProps/*`, `numbering.xml`, `footnotes.xml`, `comments.xml`) rather
  than the three-entry minimum a test builder emits — so the block-apply
  round-trip and projection proofs run against a realistic package.
- Typographic (curly) punctuation, because that is what a word processor
  actually writes.

Consumed by `tests/test_extraction_normalization_stage_80.py` (extraction)
and `tests/test_redline_block_ops.py` (transcript proof + redline apply
round-trip on a hyperlink-bearing block).
