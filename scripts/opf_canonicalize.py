#!/usr/bin/env python3
"""Canonical serialization + content hashing for OPF playbooks (OPF 0.2 / 0.3).

VENDORED, byte-faithful in semantics, from the playbook-engine reference
implementation ``playbook_engine/canonicalize.py`` (issue #143). This is the
canonical-form definition the OPF spec owns; contract-toaster must compute
``identity.content_hash`` exactly the same way the engine does, or an
otherwise-valid uploaded playbook would fail hash verification on ingest.

This module is DELIBERATELY SEPARATE from ``scripts/canonicalize.py``. That
file hashes the *legacy v1* playbook JSON (strips ``playbook.status`` /
``playbook.release``) and is unrelated to OPF. Do not cross-import them: OPF
content hashing has different exclusion rules (see below) and the two schemes
must never be conflated.

Canonical form (normative definition)
--------------------------------------
The canonical form of any JSON-serializable value is its ``json.dumps`` output
with:

  - keys sorted recursively (``sort_keys=True``);
  - no insignificant whitespace (``separators=(",", ":")``);
  - UTF-8-safe non-ASCII emitted literally, not ``\\uXXXX``-escaped
    (``ensure_ascii=False``) — the hash is taken over the UTF-8 encoding of
    this string, so this only affects the human-readable form, not the hash.

Array element order is NOT touched — order is semantic (e.g.
``observed_positions`` order, taxonomy entry order) and reordering would
silently change meaning.

Whole-document ``content_hash`` excludes three things so it isn't
self-referential and isn't perturbed by non-content run/curation metadata:

  - the top-level ``identity`` object itself — it carries ``content_hash``
    (and the section digests), the very values being computed;
  - ``compiler.generated_at`` / ``compiler.run_id`` — wall-clock timestamp and
    run identifier; two compiles of byte-identical content a second apart (or
    with a different ``run_id``) must hash identically;
  - the top-level ``curation`` object (issue #147) — the attorney-pin overlay;
    a pin surviving a recompile is not itself a change to corpus-derived
    content, so it gets its own section digest instead.

A section digest (``evidence``/``posture``/``floor``/``curation``) is the hash
of that section's own canonical bytes in isolation.

Hash format: ``"sha256:" + hexdigest`` (``^sha256:[0-9a-f]{64}$``).

Keep in sync with playbook-engine ``playbook_engine/canonicalize.py``; the
schema sync test (tests/test_opf_schema_sync.py) guards the schemas, and
tests/test_opf_canonicalize.py pins a golden hash so drift here is caught.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

# Top-level keys excluded from the whole-document canonical form. "identity"
# is self-referential; "curation" (issue #147) is the attorney-pin overlay —
# see module docstring.
_EXCLUDED_TOP_LEVEL_KEYS = frozenset({"identity", "curation"})

# `compiler` sub-keys excluded from the whole-document canonical form: run
# metadata, not playbook content. See module docstring.
_EXCLUDED_COMPILER_KEYS = frozenset({"generated_at", "run_id"})

_SECTION_NAMES = ("evidence", "posture", "floor", "curation")


def canonicalize(value: Any) -> str:
    """Return the canonical JSON string for *value*.

    Recursively sorted object keys, no insignificant whitespace, UTF-8-safe.
    Does not reorder arrays — element order is semantic.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(canonical_str: str) -> str:
    """Return ``"sha256:" + hex digest`` over the UTF-8 bytes of *canonical_str*."""
    digest = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def canonicalize_playbook(playbook: dict[str, Any]) -> str:
    """Return the whole-document canonical form used for ``content_hash``.

    Strips the excluded top-level ``identity``/``curation`` keys and the
    excluded ``compiler`` sub-keys (``generated_at``, ``run_id``) from a deep
    copy of *playbook* before serializing — see module docstring for why.
    """
    doc = copy.deepcopy(playbook)
    for key in _EXCLUDED_TOP_LEVEL_KEYS:
        doc.pop(key, None)
    compiler = doc.get("compiler")
    if isinstance(compiler, dict):
        for key in _EXCLUDED_COMPILER_KEYS:
            compiler.pop(key, None)
    return canonicalize(doc)


def content_hash(playbook: dict[str, Any]) -> str:
    """Return the playbook's ``content_hash`` (``sha256:`` + hexdigest).

    Stable across key-order/whitespace variation, across
    ``compiler.generated_at``/``run_id`` changes, and across any prior value of
    ``identity``; changes when any actual content changes.
    """
    return sha256_hex(canonicalize_playbook(playbook))


def section_digest(section: Any) -> str:
    """Return the digest of a single OPF section, over its canonical bytes."""
    return sha256_hex(canonicalize(section))


def compute_section_digests(playbook: dict[str, Any]) -> dict[str, str]:
    """Return ``{"evidence", "posture", "floor", "curation"}`` section digests.

    ``curation`` digests ``{}`` (a stable value) when the playbook carries no
    ``curation`` key — e.g. a corpus-only compile with no pins yet.
    """
    return {name: section_digest(playbook.get(name, {})) for name in _SECTION_NAMES}


def verify_content_hash(playbook: dict[str, Any]) -> bool:
    """True iff ``playbook['identity']['content_hash']`` matches the recomputed
    whole-document hash. False if ``identity`` or ``content_hash`` is absent.

    This is the ingest-time integrity gate: an OPF 0.3 upload whose declared
    hash does not match its own canonical body must be rejected (see
    scripts/opf_load.py).
    """
    identity = playbook.get("identity")
    if not isinstance(identity, dict):
        return False
    declared = identity.get("content_hash")
    if not isinstance(declared, str):
        return False
    return declared == content_hash(playbook)
