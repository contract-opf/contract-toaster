# Document-shapes corpus (issue #565)

Every `.docx` in this directory is **synthetic and fabricated**. There is no
real counterparty paper here, and there must never be: no real party names,
no vendored third-party agreement text, no network downloads.

They are generated on demand by `tests/test_document_shapes.py` (same
convention as `tests/fixtures/adversarial/`) from `tools/churn_docx.py`'s
programmatically-built base contracts and named transforms, so every byte
of payload lives in reviewable Python, not only inside a binary blob nobody
can grep.

## The files

One `<transform_name>.SYNTHETIC.docx` per named transform in
`tools/churn_docx.py::TRANSFORMS` — each reproduces ONE structural failure
class the private client corpus has discovered (a reserved namespace
prefix, tracked changes from two authors, curly punctuation, a clause split
across sibling paragraphs, heading styles stripped, a nested
insertion-then-deletion, a pending tracked change inside a field code) —
plus one `baseline-<flavor>.SYNTHETIC.docx` per
generated base-contract flavor, untransformed, so
`tools/document_spine_smoke.py` has a fuller, more realistic corpus to
report aggregate ratios over when pointed at this directory.

## What this proves, and what it does not

**Proved here, deterministically, offline:** every shape still survives the
full model-free spine (extract → normalize → locate → apply) — see
`tests/test_document_shapes.py`'s module docstring for the exact per-shape
assertions, including several transform-specific properties beyond the
uniform "normalizes, locates, and applies" check.

**Not proved here:** that these six failure classes are the only ones that
exist. This corpus only regression-tests failure classes someone has
already found (against real, private, never-committed documents) and
turned into a `churn_docx.py` transform. See
`docs/document-spine-smoke.md` for the full discussion of this limitation
and what to do when `tools/document_spine_smoke.py`, run against a real
corpus, finds a new one.
