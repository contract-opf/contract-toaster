#!/usr/bin/env python3
"""Single-file OPF bundle (``.opf.html``) wrap + extract.

An OPF playbook can travel as one self-contained HTML file that embeds the
canonical OPF JSON and its digest in ``<script type="application/json">``
blocks — the convention emitted by the playbook-engine reference renderer
(``playbook_engine/document_renderer.py::render_bundle_html``). This module is
the contract-toaster side of that convention: it can WRAP a canonical OPF JSON
into the HTML envelope (used by the fixture generator) and EXTRACT the embedded
canonical JSON back out (used by the ingest loader, scripts/opf_load.py).

Embedding convention (must match the engine byte-for-byte at the block level):
  - ``<script id="opf-canonical" type="application/json">`` — the VERBATIM
    on-disk ``playbook.opf.json`` text. The bare JSON stays the canonical
    artifact; the block contains it, never replaces it. So a consumer can
    extract, ``json.loads``, and verify ``identity.content_hash`` over the
    canonical serialization and still get a match.
  - ``<script id="opf-digest" type="application/json">`` — the digest section
    (``doc["digest"]``), pretty-printed. Advisory; the canonical block is the
    source of truth.

Tag-safety: any ``</`` inside the JSON is escaped to ``<\\/`` before embedding
so no substring can prematurely close the ``</script>`` tag. ``json.loads``
(and the browser's ``JSON.parse``) restore the bytes exactly, so the hash still
verifies. Extraction reverses the escape.
"""

from __future__ import annotations

import json
import re
from typing import Any

CANONICAL_SCRIPT_ID = "opf-canonical"
DIGEST_SCRIPT_ID = "opf-digest"

# Matches <script id="opf-canonical" type="application/json"> ... </script>.
# id/type attribute order is fixed by our writer; we accept either order on
# read to be liberal in what we extract. DOTALL so the JSON body may span lines.
_CANONICAL_BLOCK_RE = re.compile(
    r'<script\b(?=[^>]*\bid="opf-canonical")(?=[^>]*\btype="application/json")[^>]*>'
    r"(?P<body>.*?)</script>",
    re.DOTALL,
)


def escape_json_for_script(text: str) -> str:
    """Escape ``</`` → ``<\\/`` so the JSON cannot close its ``<script>`` tag.

    Mirrors playbook-engine ``document_renderer._escape_json_for_script``.
    """
    return text.replace("</", "<\\/")


def _unescape_json_from_script(text: str) -> str:
    """Reverse :func:`escape_json_for_script` (``<\\/`` → ``</``)."""
    return text.replace("<\\/", "</")


class OpfHtmlExtractError(ValueError):
    """Raised when a ``.opf.html`` bundle has no single, parseable embedded
    canonical OPF JSON block. Fail closed: the message names the structural
    problem only — never any document content."""


def wrap_opf_html(
    canonical_json_text: str,
    digest: Any = None,
    *,
    title: str = "OPF playbook bundle",
) -> str:
    """Wrap verbatim canonical OPF JSON text into the single-file HTML envelope.

    *canonical_json_text* is embedded exactly (only tag-escaped). *digest*, if
    given, is embedded pretty-printed in the advisory digest block.
    """
    canonical_block = escape_json_for_script(canonical_json_text)
    digest_block = ""
    if digest is not None:
        digest_text = json.dumps(digest, indent=1, ensure_ascii=False)
        digest_block = (
            f'\n<script id="{DIGEST_SCRIPT_ID}" type="application/json">\n'
            f"{escape_json_for_script(digest_text)}\n</script>"
        )
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{title}</title>\n"
        "<!--\n"
        "  Single-file OPF bundle. The canonical playbook is the JSON embedded in\n"
        '  the script block with id "opf-canonical" below. Extract that block,\n'
        "  JSON-parse it, and verify identity.content_hash over the canonical\n"
        "  serialization (see scripts/opf_canonicalize.py).\n"
        "-->\n</head>\n<body>\n"
        f"<h1>{title}</h1>\n"
        f'<script id="{CANONICAL_SCRIPT_ID}" type="application/json">\n'
        f"{canonical_block}\n</script>"
        f"{digest_block}\n"
        "</body>\n</html>\n"
    )


def extract_opf_from_html(html: str) -> dict:
    """Extract and parse the embedded canonical OPF JSON from a ``.opf.html``.

    Fail closed (raises :class:`OpfHtmlExtractError`) on: no canonical block,
    more than one canonical block, or a block whose body is not valid JSON.
    Never includes document content in the error message.
    """
    matches = _CANONICAL_BLOCK_RE.findall(html)
    if not matches:
        raise OpfHtmlExtractError(
            'no <script id="opf-canonical" type="application/json"> block found'
        )
    if len(matches) > 1:
        raise OpfHtmlExtractError(
            f"expected exactly one opf-canonical block, found {len(matches)}"
        )
    body = _unescape_json_from_script(matches[0].strip())
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        # Report position/reason, never the offending substring.
        raise OpfHtmlExtractError(
            f"embedded opf-canonical block is not valid JSON "
            f"(at line {exc.lineno}, col {exc.colno})"
        ) from None
    if not isinstance(doc, dict):
        raise OpfHtmlExtractError("embedded opf-canonical block is not a JSON object")
    return doc
