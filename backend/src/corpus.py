"""
Corpus ingestion pipeline — issue #87 (Phase 3 build).

Real corpus ingestion: `POST /api/corpus` (admin) accepts an executed
agreement -> hostile-file gauntlet -> clause extraction -> clause records
(immutable clause_id, full text in the clause-text store under the corpus
CMK per #32; metadata incl. `playbook_id`, `playbook_topic_id`,
`document_type` executed-final/accepted-draft/rejected-draft, curation
fields per #60) -> embeddings via the pinned embedding model -> staging
index ingestion -> draft snapshot with a content-addressed clause-id
manifest. Rejected-draft clauses ride the hard-labeled negative channel,
never the positive context.

Read first: ARCHITECTURE.md -> "Retrieval — Amazon Bedrock Knowledge Bases
(S3 Vectors)" (Metadata model, Curation, Separate positive and negative
corpora, Corpus versioning and activation boundaries), docs/data-handling.md
-> "Derived corpus artifacts classification", issues #60, #20, #32, #45.

## Scope of this module (what "real" means here)

The hostile-file gauntlet (issue #63, backend/src/upload_validation.py) and
clause-record modeling below are real, executable code. Two dependencies are
intentionally stubbed behind narrow seams so this module is testable without
live AWS and without issue #80 (OOXML paragraph extraction) being done yet:

  - `paragraphs`: the caller supplies already-extracted, already-normalized
    paragraphs (`[{"heading": ..., "text": ...}, ...]`) — the same input
    shape scripts/diff_standard_form.py's `diff_draft_against_standard`
    consumes. Real `.docx` paragraph extraction is issue #80's job; wiring
    that extractor in is a follow-up, not a redefinition of this contract.
  - `embed_fn`: a callable `(str) -> list[float]` for the pinned embedding
    model. The default (`deterministic_embed`) is a stand-in hash-based
    vector so the pipeline is fully exercisable without a live Bedrock
    call; the real embedding client is injected the same way `AvClient` is
    injected into `upload_validation.run_upload_gauntlet`.

## Pipeline order (data-flow steps, per ARCHITECTURE.md -> Corpus versioning)

  1. `run_upload_gauntlet` (issue #63) — hostile-file checks on the raw bytes.
  2. `extract_clauses` — map each paragraph to a `playbook_topic_id` via the
     playbook's `section_anchors` (heading match against the anchor map,
     same convention as diff_standard_form.py), producing clause text +
     metadata. A paragraph that matches no topic is skipped (not every
     paragraph of an executed agreement is a reviewable clause — e.g.
     signature blocks, recitals).
  3. `build_clause_record` — assigns the immutable, content-addressed
     `clause_id`, applies the polarity rule (`document_type ==
     "rejected-draft"` forces `corpus_polarity = "negative"`; otherwise
     `"positive"`), and seeds curation fields at their governed defaults
     (`reusable_precedent=False` until a lawyer marks it reusable).
  4. `embed_clause_records` — computes an embedding per clause record via
     the injected `embed_fn`.
  5. `ingest_to_staging` — writes clause records into the **staging** index
     only (never the active store — see ARCHITECTURE.md "Active store +
     staging index"), partitioned by polarity so positive and negative
     clauses never share a top-K positive-precedent context.
  6. `build_manifest` — a content-addressed, deterministically-ordered
     manifest of the clause_ids ingested (`serialize_manifest` / hash),
     mirroring `scripts/diff_standard_form.py`'s `serialize_diff`/`diff_hash`
     determinism convention.
  7. `run_ingestion` — orchestrates 1-6 into a draft snapshot record. A
     failure at any stage marks the draft snapshot `failed` (never
     `active`/`draft`-queryable) and never touches the active store.

Environment variables consumed (mirrors backend/src/reviews.py convention):
  CORPUS_SNAPSHOTS_TABLE   DynamoDB table for draft/active snapshot records
  CLAUSE_TEXT_TABLE        DynamoDB table for the clause-text store
                           (full clause text, keyed by clause_id; corpus CMK
                           domain per #32 — this module writes the pointer
                           record only, KMS is applied at the table/bucket
                           level, out of scope for this module's tests)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, status

REPO_ROOT = Path(__file__).resolve().parents[2]

# Cross-directory import (same convention backend/src/pipeline_runner.py and
# backend/src/reviews.py already use): scripts/ isn't a package src/ can
# import via a normal dotted import, so it's added to sys.path directly.
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import playbook_registry  # noqa: E402

VALID_DOCUMENT_TYPES = {"executed-final", "accepted-draft", "rejected-draft"}
VALID_POLARITIES = {"positive", "negative"}

SNAPSHOT_STATUS_INGESTING = "ingesting"
SNAPSHOT_STATUS_DRAFT = "draft"
SNAPSHOT_STATUS_FAILED = "failed"
SNAPSHOT_STATUS_ACTIVE = "active"


class IngestionError(Exception):
    """Raised when a corpus ingestion job cannot complete.

    Mirrors upload_validation.HostileFileError's role: the caller catches
    this, marks the draft snapshot `failed`, and never lets a partially
    ingested snapshot become queryable (ARCHITECTURE.md -> "Ingestion
    interlock").
    """

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)


def _is_admin(caller_user_row: dict[str, Any]) -> bool:
    """`is_admin` is a DynamoDB `users`-row flag, never a JWT claim -- same
    convention as src/users.py::_is_admin and src/retention.py::_is_admin."""
    return bool(caller_user_row.get("is_admin", False))


def _require_admin(caller_user_row: dict[str, Any], detail: str) -> None:
    if not _is_admin(caller_user_row):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# ---------------------------------------------------------------------------
# Playbook loading (topic -> playbook_topic_id resolution)
# ---------------------------------------------------------------------------


def _load_playbook(playbook_path: Path | None = None) -> dict:
    # Late-bound: resolved via the registry's CURRENT default_playbook_id at
    # CALL time (issue #289), not a hard-coded "eiaa" path baked in at
    # import time -- a synthetic registry (see playbook_registry.py's
    # module docstring) changes what this loads with zero code changes.
    path = playbook_path or playbook_registry.resolve_playbook(
        playbook_registry.default_playbook_id()
    ).playbook_path
    with open(path) as f:
        return json.load(f)


def _normalize_heading(heading: str) -> str:
    return " ".join(heading.strip().lower().split())


_ABSENT_PREFIX = "[absent] "

# Curated keyword aliases (issue #215) for topics whose real-document
# heading can't be pinned to a single exact string:
#   - `not_in_standard` topics ('[absent] ...' section_ref) describe a
#     clause the standard form doesn't have, so an executed agreement's
#     own heading for it varies freely ('Indemnification', 'Hold
#     Harmless', 'Mutual Indemnity' all mean the same topic).
#   - the Section 10 ("Miscellaneous") sub-clauses (notices, exclusivity,
#     entire-agreement-and-amendment, order-of-precedence) share the bare
#     "10 Miscellaneous" heading and are only distinguished by content —
#     their section_ref's parenthetical annotation ('(Notices)', etc.)
#     is a display note, not literal heading text an executed agreement
#     would carry.
# Keys must be real playbook topic ids; `_build_topic_alias_index` only
# activates an entry for a topic actually present in the loaded playbook.
_KEYWORD_ALIASES: dict[str, list[str]] = {
    "indemnification": ["indemnification", "indemnity", "hold harmless"],
    "insurance": ["insurance"],
    "governing-law-and-venue": ["governing law", "choice of law", "venue", "jurisdiction"],
    "notices": ["notice", "notices"],
    "exclusivity": ["exclusiv", "non-exclusive"],
    "entire-agreement-and-amendment": ["entire agreement", "amendment"],
    "order-of-precedence": ["order of precedence", "conflict", "controls", "prevail", "additional terms"],
}


def _split_section_ref(section_ref: str) -> list[str]:
    """Split a (possibly composite) section_ref into individual segments.

    Composite refs list multiple real sections in one display label (e.g.
    '1.2 Admitting Students; 1.4 Expulsion; 1.7 Final Authority'); each
    segment is its own candidate heading.
    """
    return [seg.strip() for seg in section_ref.split(";") if seg.strip()]


def _heading_alias_from_segment(segment: str) -> str | None:
    """Derive a matchable heading alias from one section_ref segment.

    Strips the '[absent] ' marker (not_in_standard topics) and any
    trailing parenthetical annotation ('10 Miscellaneous (Notices)' ->
    '10 Miscellaneous'). A segment with no leading section text (e.g.
    'Front-page footnote on Additional Terms' — a *location* note, not a
    heading) still yields its own text as a best-effort alias; harmless
    since it won't collide with a real document heading, and keyword
    matching is the real fallback for those cases.
    """
    if segment.startswith(_ABSENT_PREFIX):
        segment = segment[len(_ABSENT_PREFIX):]
    paren_idx = segment.find("(")
    base = segment[:paren_idx].strip() if paren_idx != -1 else segment.strip()
    return base or None


def _build_topic_alias_index(playbook: dict) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Build the two-layer topic-matching index used by `extract_clauses`.

    Executed agreements don't carry `section_anchor` tags (that machinery
    is standard-form-diff-specific, per ARCHITECTURE.md -> "Section-anchor
    map"); corpus clause extraction instead matches on headings derived
    from each topic's display `section_ref`.

    Layer 1 (`heading_aliases`, returned first): normalized heading text ->
    playbook_topic_id, for exact match. Built from each topic's full
    section_ref *and* each of its ';'-separated segments (composite refs),
    with the '[absent] ' marker and any parenthetical annotation stripped.
    An alias claimed by more than one topic (e.g. the bare "10
    Miscellaneous" shared by four Section 10 sub-clause topics) is
    ambiguous on heading text alone and is dropped from this layer
    entirely — never guessed; layer 2 resolves it instead.

    Layer 2 (`keyword_aliases`, returned second): playbook_topic_id ->
    curated keyword list (`_KEYWORD_ALIASES`, restricted to topic ids
    present in this playbook). Used by `extract_clauses` only when layer 1
    found no exact match, scanning both heading and body text.
    """
    raw_aliases: dict[str, set[str]] = {}
    topic_ids_in_playbook: set[str] = set()
    for topic in playbook.get("topics", []):
        topic_id = topic.get("id")
        section_ref = topic.get("section_ref")
        if not topic_id or not section_ref:
            continue
        topic_ids_in_playbook.add(topic_id)
        aliases = {_normalize_heading(section_ref)}
        for segment in _split_section_ref(section_ref):
            alias = _heading_alias_from_segment(segment)
            if alias:
                aliases.add(_normalize_heading(alias))
        for alias in aliases:
            raw_aliases.setdefault(alias, set()).add(topic_id)

    heading_aliases = {
        alias: next(iter(topic_ids))
        for alias, topic_ids in raw_aliases.items()
        if len(topic_ids) == 1
    }
    keyword_aliases = {
        topic_id: [_normalize_heading(keyword) for keyword in keywords]
        for topic_id, keywords in _KEYWORD_ALIASES.items()
        if topic_id in topic_ids_in_playbook
    }
    return heading_aliases, keyword_aliases


def _match_by_keyword(
    normalized_heading: str,
    text: str,
    keyword_aliases: dict[str, list[str]],
) -> str | None:
    """Resolve a topic by keyword when no exact heading alias matched.

    Scans the normalized heading *and* body text together (a Section 10
    sub-clause heading alone, e.g. bare "10 Miscellaneous", carries no
    disambiguating signal — the content does). Returns the topic_id only
    when exactly one topic's keywords hit; an ambiguous multi-topic hit is
    left unresolved (never guessed), matching this module's
    IngestionError convention of failing closed rather than silently
    picking a side.
    """
    haystack = f"{normalized_heading} {_normalize_heading(text)}"
    matches = [
        topic_id
        for topic_id, keywords in keyword_aliases.items()
        if any(keyword in haystack for keyword in keywords)
    ]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Step 2: clause extraction
# ---------------------------------------------------------------------------


def extract_clauses(
    paragraphs: list[dict[str, str]],
    playbook: dict,
) -> list[dict[str, str]]:
    """Map extracted paragraphs to playbook topics.

    `paragraphs` is `[{"heading": ..., "text": ...}, ...]` — already
    extracted and normalized (issue #80's output shape; see module
    docstring). Returns one entry per paragraph whose heading (or, for
    topics whose real-document heading can't be pinned to one exact
    string — see `_build_topic_alias_index` — heading-plus-body keyword
    content) matches a playbook topic: `{"playbook_topic_id": ...,
    "heading": ..., "text": ...}`. Paragraphs matching no topic (recitals,
    signature blocks, boilerplate) are dropped — not every paragraph of an
    executed agreement is a reviewable clause.
    """
    heading_aliases, keyword_aliases = _build_topic_alias_index(playbook)
    clauses = []
    for para in paragraphs:
        heading = para.get("heading", "")
        text = para.get("text", "")
        normalized_heading = _normalize_heading(heading)
        topic_id = heading_aliases.get(normalized_heading)
        if topic_id is None:
            topic_id = _match_by_keyword(normalized_heading, text, keyword_aliases)
        if topic_id is None:
            continue
        if not text.strip():
            # A matched heading with no body text is not a usable clause.
            continue
        clauses.append({
            "playbook_topic_id": topic_id,
            "heading": heading,
            "text": text,
        })
    return clauses


# ---------------------------------------------------------------------------
# Step 3: clause record construction (immutable, content-addressed clause_id)
# ---------------------------------------------------------------------------


def compute_clause_id(
    source_document_id: str,
    playbook_topic_id: str,
    text: str,
) -> str:
    """Immutable, content-addressed clause_id.

    Content-addressed on (source_document_id, playbook_topic_id, text) so
    the same clause ingested twice (e.g. a re-run of a failed ingestion
    job) derives the identical id — required for `build_manifest`'s
    reproducibility guarantee (ARCHITECTURE.md -> "Frozen content-addressed
    manifest": "candidate pool reproducible").
    """
    raw = f"{source_document_id}:{playbook_topic_id}:{text.strip()}"
    return "clause_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_clause_record(
    *,
    source_document_id: str,
    playbook_topic_id: str,
    text: str,
    document_type: str,
    playbook_id: str | None = None,
    corpus_snapshot_version: str,
    counterparty_name: str | None = None,
    date: str | None = None,
    reusable_precedent: bool = False,
    negotiation_context: str | None = None,
    superseded_by: str | None = None,
    approved_use_scope: str | None = None,
) -> dict[str, Any]:
    """Build one clause record with the corrected metadata model
    (ARCHITECTURE.md -> "Metadata model (fits the S3 Vectors limits)").

    Polarity is derived, never caller-supplied: `document_type ==
    "rejected-draft"` forces `corpus_polarity = "negative"`; every other
    document_type is `"positive"`. This is the structural enforcement the
    issue's acceptance criteria require ("Positive/negative separation
    structurally enforced") — a caller cannot accidentally place a
    rejected-draft clause in the positive channel by passing the wrong flag,
    because there is no polarity parameter to get wrong.

    Curation fields (issue #60 curation: "not every executed clause is good
    precedent") default to the governed, conservative values: a clause is
    not reusable precedent until a lawyer marks it so.
    """
    if document_type not in VALID_DOCUMENT_TYPES:
        raise IngestionError(
            "invalid_document_type",
            f"document_type must be one of {sorted(VALID_DOCUMENT_TYPES)}, got {document_type!r}",
        )

    # Late-bound (issue #289): resolved via the registry's CURRENT default
    # at CALL time, not a hard-coded "eiaa" default baked in at import time.
    playbook_id = playbook_id or playbook_registry.default_playbook_id()

    corpus_polarity = "negative" if document_type == "rejected-draft" else "positive"

    clause_id = compute_clause_id(source_document_id, playbook_topic_id, text)

    return {
        "clause_id": clause_id,
        "source_document_id": source_document_id,
        "corpus_snapshot_version": corpus_snapshot_version,
        "corpus_polarity": corpus_polarity,
        "document_type": document_type,
        "playbook_id": playbook_id,
        "playbook_topic_id": playbook_topic_id,
        "counterparty_name": counterparty_name,
        "date": date,
        "text": text,
        # Curation fields (issue #60) — legal-curated, defaulted here.
        "reusable_precedent": reusable_precedent,
        "negotiation_context": negotiation_context,
        "superseded_by": superseded_by,
        "approved_use_scope": approved_use_scope,
    }


# ---------------------------------------------------------------------------
# Step 4: embeddings
# ---------------------------------------------------------------------------


EmbedFn = Callable[[str], list[float]]


def deterministic_embed(text: str, dims: int = 8) -> list[float]:
    """Deterministic stand-in for the pinned embedding model.

    Not a real semantic embedding — a fixed-width vector derived from the
    text's SHA-256 digest, so `embed_clause_records` is fully exercisable
    (same text -> same vector, different text -> different vector) without
    a live Bedrock call. The real embedding client (Titan v2 or equivalent,
    invoked via the dedicated `corpusKnowledgeBaseRole`, ARCHITECTURE.md ->
    "Reconciled least-privilege invariant") is injected as `embed_fn`,
    mirroring how `AvClient` is injected into `run_upload_gauntlet`.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in digest[:dims]]


def embed_clause_records(
    clause_records: list[dict[str, Any]],
    embed_fn: EmbedFn = deterministic_embed,
) -> list[dict[str, Any]]:
    """Attach an `embedding` vector to each clause record (copies, does not
    mutate the input records)."""
    out = []
    for record in clause_records:
        embedded = dict(record)
        embedded["embedding"] = embed_fn(record["text"])
        out.append(embedded)
    return out


# ---------------------------------------------------------------------------
# Step 5: staging-index ingestion (never the active store)
# ---------------------------------------------------------------------------


class StagingIndex:
    """In-process stand-in for a Bedrock KB / S3 Vectors staging index.

    Partitions ingested clause records by `corpus_polarity` so positive and
    negative clauses are structurally separated (ARCHITECTURE.md -> "Separate
    positive and negative corpora": "Rejected drafts can poison the model if
    commingled with accepted precedent... never placed in the same top-K
    positive-precedent context"). This is an in-memory object in this module
    so the pipeline is unit-testable; a real staging index is a distinct S3
    Vectors store (ARCHITECTURE.md -> "Active store + staging index").
    """

    def __init__(self, snapshot_version: str):
        self.snapshot_version = snapshot_version
        self.status = "staging"  # never "active" — activation is a separate,
        # deliberate admin action outside this module's scope (issue #20).
        self.positive: dict[str, dict[str, Any]] = {}
        self.negative: dict[str, dict[str, Any]] = {}

    def ingest(self, embedded_records: list[dict[str, Any]]) -> None:
        for record in embedded_records:
            if record["corpus_snapshot_version"] != self.snapshot_version:
                raise IngestionError(
                    "snapshot_version_mismatch",
                    f"record snapshot_version {record['corpus_snapshot_version']!r} "
                    f"does not match staging index snapshot_version {self.snapshot_version!r}",
                )
            polarity = record["corpus_polarity"]
            if polarity not in VALID_POLARITIES:
                raise IngestionError(
                    "invalid_polarity",
                    f"corpus_polarity must be one of {sorted(VALID_POLARITIES)}, got {polarity!r}",
                )
            bucket = self.positive if polarity == "positive" else self.negative
            bucket[record["clause_id"]] = record

    def all_clause_ids(self) -> list[str]:
        return sorted(set(self.positive) | set(self.negative))


# ---------------------------------------------------------------------------
# Step 6: content-addressed manifest
# ---------------------------------------------------------------------------


def build_manifest(clause_ids: list[str], snapshot_version: str) -> dict[str, Any]:
    """A content-addressed manifest of the clause_ids a snapshot contains.

    Sorted-key, deterministic JSON serialization (same convention as
    scripts/diff_standard_form.py's serialize_diff/diff_hash) so the same
    ingested clause set always produces the same manifest hash — required
    for "candidate pool reproducible" (ARCHITECTURE.md -> "Frozen
    content-addressed manifest").
    """
    sorted_ids = sorted(set(clause_ids))
    manifest = {
        "corpus_snapshot_version": snapshot_version,
        "clause_ids": sorted_ids,
    }
    return manifest


def serialize_manifest(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def manifest_hash(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(serialize_manifest(manifest).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Step 7: orchestration -> draft snapshot
# ---------------------------------------------------------------------------


def run_ingestion(
    *,
    source_document_id: str,
    document_type: str,
    paragraphs: list[dict[str, str]],
    corpus_snapshot_version: str,
    playbook_id: str | None = None,
    playbook: dict | None = None,
    counterparty_name: str | None = None,
    date: str | None = None,
    embed_fn: EmbedFn = deterministic_embed,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Orchestrate extraction -> clause records -> embeddings -> staging
    ingestion -> manifest, and return a draft snapshot record.

    Callers run `upload_validation.run_upload_gauntlet` on the raw upload
    BEFORE calling this function — gauntlet-then-extract, never the reverse
    (issue #63 order; see module docstring). This function's input is
    already-gauntleted, already-extracted paragraphs.

    On any failure the returned snapshot record has `status = "failed"` and
    an empty manifest; the staging index for a failed job is discarded (its
    clause_ids are never added to any snapshot's manifest and it is never
    promoted), and the active store is never touched regardless of outcome
    -- ingestion always targets a fresh staging index (ARCHITECTURE.md ->
    "Active store + staging index").
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    # Late-bound (issue #289): resolved via the registry's CURRENT default
    # at CALL time, not a hard-coded "eiaa" default baked in at import time.
    playbook_id = playbook_id or playbook_registry.default_playbook_id()
    playbook = playbook if playbook is not None else _load_playbook()

    snapshot: dict[str, Any] = {
        "corpus_snapshot_version": corpus_snapshot_version,
        "source_document_id": source_document_id,
        "playbook_id": playbook_id,
        "document_type": document_type,
        "status": SNAPSHOT_STATUS_INGESTING,
        "created_at": str(int(now_epoch)),
        "manifest": None,
        "manifest_hash": None,
        "clause_count": 0,
        "positive_clause_count": 0,
        "negative_clause_count": 0,
        "failure_reason": None,
    }

    try:
        clauses = extract_clauses(paragraphs, playbook)

        clause_records = [
            build_clause_record(
                source_document_id=source_document_id,
                playbook_topic_id=clause["playbook_topic_id"],
                text=clause["text"],
                document_type=document_type,
                playbook_id=playbook_id,
                corpus_snapshot_version=corpus_snapshot_version,
                counterparty_name=counterparty_name,
                date=date,
            )
            for clause in clauses
        ]

        embedded_records = embed_clause_records(clause_records, embed_fn=embed_fn)

        staging_index = StagingIndex(corpus_snapshot_version)
        staging_index.ingest(embedded_records)

        manifest = build_manifest(staging_index.all_clause_ids(), corpus_snapshot_version)

        snapshot["status"] = SNAPSHOT_STATUS_DRAFT
        snapshot["manifest"] = manifest
        snapshot["manifest_hash"] = manifest_hash(manifest)
        snapshot["clause_count"] = len(staging_index.all_clause_ids())
        snapshot["positive_clause_count"] = len(staging_index.positive)
        snapshot["negative_clause_count"] = len(staging_index.negative)
        snapshot["_staging_index"] = staging_index  # in-process handle only;
        # never serialized to the snapshot's persisted DynamoDB record (a
        # real staging index is a distinct S3 Vectors store, not an
        # in-memory object -- see StagingIndex docstring).
        return snapshot

    except IngestionError as exc:
        snapshot["status"] = SNAPSHOT_STATUS_FAILED
        snapshot["failure_reason"] = exc.reason_code
        return snapshot


# ---------------------------------------------------------------------------
# Step 8: admin-gated request wrapper -> POST /api/corpus (issue #197)
# ---------------------------------------------------------------------------


def run_ingestion_request(
    *,
    caller_user_row: dict[str, Any],
    source_document_id: str,
    document_type: str,
    paragraphs: list[dict[str, str]],
    corpus_snapshot_version: str,
    playbook_id: str | None = None,
    playbook: dict | None = None,
    counterparty_name: str | None = None,
    date: str | None = None,
    embed_fn: EmbedFn = deterministic_embed,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Admin-gated entry point for `POST /api/corpus` (issue #197).

    Raises `HTTPException(403)` for a non-admin caller before any
    extraction/embedding work runs (`is_admin` is a DynamoDB `users`-row
    flag, never a JWT claim -- same convention as `src/users.py` and
    `src/retention.py`). Delegates to `run_ingestion` for the actual
    pipeline; see that function's docstring for the full contract.

    The returned snapshot's `status` is always `"draft"` or `"failed"` --
    never `"active"`: activation is a distinct, deliberate admin action
    outside this module's scope (issue #20). This function does not touch
    the active store or any persistence layer, so its response always
    surfaces the staging manifest only, never an activated one.
    """
    _require_admin(caller_user_row, "Admin privilege required to run corpus ingestion.")
    return run_ingestion(
        source_document_id=source_document_id,
        document_type=document_type,
        paragraphs=paragraphs,
        corpus_snapshot_version=corpus_snapshot_version,
        playbook_id=playbook_id,
        playbook=playbook,
        counterparty_name=counterparty_name,
        date=date,
        embed_fn=embed_fn,
        now_epoch=now_epoch,
    )
