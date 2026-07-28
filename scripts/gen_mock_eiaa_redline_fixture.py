#!/usr/bin/env python3
"""
Generate the mock pipeline's pre-baked eiaa redline fixture — issue #188.

The mock review pipeline (infra/lambda/mock_review/handler.py) does not run
the real redline generator; for the eiaa playbook it serves a canned,
clearly-SYNTHETIC tracked-changes .docx staged in the outputs bucket at
`mock-fixtures/eiaa/pre-baked-redline.docx`. The redline stage
(infra/lambda/redline/handler.py) copies that object into each review's own
`outputs/<review_id>/out.docx` so `GET /api/reviews/{id}/output` has a real
file to serve; the CDK BucketDeployment in pipeline-stack.ts seeds it.

This script is the source of truth for that committed fixture. It reuses the
real, tested tracked-changes writer (scripts/redline_docx_writer.py) so the
mock output is a genuine, valid .docx with real <w:ins>/<w:del> markup and
the standard export marker -- not a hand-rolled stub -- but with obviously
synthetic, non-legal placeholder content.

Determinism: a fixed revision date is passed to the writer and the resulting
ZIP is rewritten with a fixed entry timestamp, so re-running this script
produces byte-identical output. A reviewer can regenerate and `git diff` to
confirm the committed fixture matches this generator.

Usage:
    python3 scripts/gen_mock_eiaa_redline_fixture.py
    # writes infra/fixtures/mock-outputs/eiaa/pre-baked-redline.docx
"""

import datetime
import io
import sys
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from redline_docx_writer import build_tracked_changes_docx  # noqa: E402

# Destination the BucketDeployment source directory maps to
# `mock-fixtures/eiaa/pre-baked-redline.docx` in the outputs bucket.
FIXTURE_PATH = (
    REPO_ROOT / "infra" / "fixtures" / "mock-outputs" / "eiaa" / "pre-baked-redline.docx"
)

# Fixed date so the <w:ins>/<w:del> w:date attributes are reproducible.
_FIXED_DATE = datetime.datetime(2020, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
# Fixed ZIP entry timestamp (the DOS epoch) so the archive bytes are
# reproducible regardless of when the script runs.
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

# Obviously-synthetic placeholder content -- no real clause text, clearly
# labelled as a mock so it can never be mistaken for genuine legal output.
_SYNTHETIC_ORIGINAL = {
    "sec-mock-1": (
        "SYNTHETIC MOCK REDLINE — This is placeholder output from the "
        "contract review tool's mock pipeline. It is not legal advice and "
        "contains no real contract text. Original placeholder sentence."
    )
}
_SYNTHETIC_PATCHES = [
    {
        "anchor": "sec-mock-1",
        "new_text": (
            "SYNTHETIC MOCK REDLINE — replacement placeholder sentence. "
            "Attorney approval required; do not rely on this document."
        ),
    }
]
_SYNTHETIC_FOOTNOTES = {
    "sec-mock-1": (
        "Synthetic rationale: this footnote is mock content generated for "
        "pipeline testing only."
    )
}


def _normalize_zip(docx_bytes: bytes) -> bytes:
    """Rewrite the .docx ZIP with a fixed entry timestamp so the output is
    byte-for-byte reproducible across runs (zipfile otherwise stamps each
    entry with the current time)."""
    src = zipfile.ZipFile(io.BytesIO(docx_bytes), "r")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            info = zipfile.ZipInfo(filename=name, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, src.read(name))
    return out.getvalue()


def build_fixture_bytes() -> bytes:
    docx_bytes = build_tracked_changes_docx(
        _SYNTHETIC_PATCHES,
        _SYNTHETIC_ORIGINAL,
        author="contract-toaster-mock",
        date=_FIXED_DATE,
        footnote_text_by_anchor=_SYNTHETIC_FOOTNOTES,
        include_marker=True,
    )
    return _normalize_zip(docx_bytes)


def main() -> int:
    fixture_bytes = build_fixture_bytes()
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_bytes(fixture_bytes)
    print(f"Wrote {FIXTURE_PATH.relative_to(REPO_ROOT)} ({len(fixture_bytes)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
