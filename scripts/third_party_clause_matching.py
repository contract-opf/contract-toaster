#!/usr/bin/env python3
"""
Third-party paper: deterministic semantic clause-to-playbook-position
matching (issue #249, Third-party-paper support Slice 3 of 5).

## Problem this solves

On third-party paper there is no heading-anchor match to your form -- that
is the whole reason the first-party path fails on a counterparty's own
template (#202) -- so a segmented counterparty clause (#248's output,
`{"clause_id": ..., "heading": ..., "text": ..., "order": ...}`) cannot be
assigned a `playbook_topic_id` the way `backend/src/corpus.py::extract_
clauses` does it (exact heading-alias match, `_build_topic_alias_index` /
`_match_by_keyword`). This module assigns each third-party clause to
zero-or-one playbook topic **by content similarity** instead, using a
deterministic, offline stand-in for a real embedding/semantic-search step
-- no live Bedrock, no live Knowledge Base, no network call of any kind.

## Design: hashed-bucket TF-IDF cosine similarity, built on the SAME
   deterministic hash primitive `corpus.deterministic_embed` already uses

`corpus.deterministic_embed(text)` hashes an ENTIRE string to one fixed
vector (SHA-256 digest bytes / 255) -- a fine stand-in for "same text in,
same embedding out" when the caller only ever compares a clause to
itself (`embed_clause_records`), but two *different* strings hash to
statistically unrelated vectors, so comparing whole-clause-text vectors
directly would not track topical/lexical overlap at all (verified: cosine
similarity between unrelated whole-text hashes clusters near 1.0, because
individual digest bytes are all non-negative and every vector points in
nearly the same octant -- there is no discriminating signal there).

This module instead applies the SAME hash primitive **per token** (feature
hashing, the "hashing trick": `deterministic_embed(token, dims=2)` maps a
normalized token to one of `_HASH_BUCKETS` deterministic buckets) to build
a bag-of-words term-count vector per clause and per playbook topic, then
reweights every bucket by inverse document frequency across the playbook's
own topic set (rare, topic-specific vocabulary counts more than boilerplate
words nearly every topic shares -- "party", "notice", "term" etc.). Cosine
similarity between two TF-IDF vectors is a standard, deterministic,
stdlib-only text-similarity technique (no live model, no network) that
tracks real lexical/topical overlap -- unlike raw whole-text hashing.

A playbook topic's "position text" -- the thing each clause is compared
against -- reuses the SAME topic vocabulary `corpus.py`'s own heading/
keyword matcher relies on: `topic["section_ref"]`, `topic["our_standard"]`
(the position statement itself), and, when present, `corpus._KEYWORD_
ALIASES[topic_id]` -- the SAME curated alias list `corpus._build_topic_
alias_index` / `corpus._match_by_keyword` use for corpus ingestion's own
heading-plus-body keyword fallback. This is "reusing the SAME section_ref/
topic matching corpus already provides", per the issue.

## Output

  `match_clauses_to_playbook()` returns:
    {
      "assignments": [
        {"clause_id": ..., "playbook_topic_id": <id> | None, "score": float},
        ...
      ],
      "topic_matches": {<playbook_topic_id>: [clause_id, ...], ...},
    }

`assignments` has one entry per input clause record, in input order. Every
playbook topic id is a key of `topic_matches`, even when its list is empty
-- Slice 4 needs to see an empty list (a topic with no matched clause = a
"missing position" candidate), not a missing dict key it would have to
infer via `default=[]` and risk silently treating a KeyError as "no
findings" instead of "topic genuinely unmatched".

A clause's best-scoring topic is used ONLY when its score is `>=
threshold`; below threshold the clause is assigned `None` rather than
force-fit to the nearest (but not actually corresponding) topic -- the
same "never guessed" fail-closed convention `corpus._match_by_keyword`
already follows for ambiguous keyword hits.

## Determinism

No randomness anywhere: token->bucket hashing is `hashlib.sha256`-based
(via `corpus.deterministic_embed`), IDF is computed from the (deterministic)
per-topic term-count vectors, and cosine similarity is closed-form
arithmetic. Same clause records + same playbook + same `embed_fn` always
produce byte-identical `assignments`/`topic_matches` -- required for this
slice's own "reproducible candidate pool" convention (mirrors
`backend/src/corpus.py`'s `build_manifest` determinism goal one layer up).

See: issue #249, issue #248 (`scripts/third_party_clause_segmentation.py`,
this module's clause-record input shape), `backend/src/corpus.py`
(`deterministic_embed`, `_build_topic_alias_index`, `_match_by_keyword`,
`_KEYWORD_ALIASES`), `backend/src/model_client.py` (`FakeBedrockClient` --
the analogous injectable-fake convention for text-generation calls; this
module has no text-generation step, so it has no direct dependency on
`FakeBedrockClient`, only on the same "no live model" discipline).
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import corpus  # noqa: E402

EmbedFn = Callable[[str], list[float]]

# Best-topic score must clear this bar to be assigned; below it the clause
# is assigned `None` (no counterpart in the playbook) rather than force-fit
# to the nearest-but-unrelated topic. Picked empirically (see tests/
# test_third_party_clause_matching.py) so that a clause whose text
# genuinely corresponds to a topic clears it with margin, while boilerplate
# / off-playbook clause text (shared-stopword noise only) stays below it.
DEFAULT_MATCH_THRESHOLD = 0.20

# Number of hashed term buckets for the bag-of-words vectors. Large enough,
# relative to a clause's or a topic's typically-small unique-token count,
# that two UNRELATED tokens rarely collide into the same bucket (birthday-
# paradox headroom), so a bucket's shared count across two texts tracks
# real shared vocabulary rather than hash collisions.
_HASH_BUCKETS = 4096

# Generic contract boilerplate that appears in nearly every topic's and
# nearly every clause's text and therefore carries no topic-discriminating
# signal on its own (IDF reweighting already downweights common terms, but
# dropping the most common ones outright keeps the term-count vectors
# smaller and the signal cleaner).
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "to", "in", "and", "or", "is", "are", "be",
        "this", "that", "which", "with", "for", "on", "by", "as", "shall",
        "not", "any", "all", "its", "it", "will", "may", "if", "other",
        "than", "such", "from", "under", "upon", "at", "has", "have", "had",
        "was", "were", "been", "but", "party", "parties", "agreement",
        "section", "either", "both", "each",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Minimal deterministic suffix-stripping (a truncating stemmer): folds
# common English inflections ("indemnification"/"indemnifications",
# "governed"/"governing") onto a shared token so exact-string tokenization
# doesn't miss lexical matches on trivial morphology. Longest suffixes are
# tried first so e.g. "-ations" strips before the shorter "-s" would.
_SUFFIXES = (
    "ations", "ation", "ities", "ity", "ances", "ance", "ences", "ence",
    "ingly", "ing", "edly", "ied", "ies", "ed", "es", "s",
)


def _stem(token: str) -> str:
    for suffix in _SUFFIXES:
        if len(token) - len(suffix) >= 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _tokenize(text: str) -> list[str]:
    """Lowercase, split into word tokens, drop stopwords/very-short tokens,
    stem. Deterministic and stdlib-only (`re`, no NLP dependency)."""
    raw_tokens = _TOKEN_RE.findall((text or "").lower())
    return [
        _stem(token)
        for token in raw_tokens
        if token not in _STOPWORDS and len(token) > 2
    ]


def _token_bucket(token: str, embed_fn: EmbedFn) -> int:
    """Hash one normalized token to a bucket index in `range(_HASH_BUCKETS)`
    via `corpus.deterministic_embed` -- the SAME SHA-256-digest-based
    primitive the rest of the pipeline uses for its "no live model" stand-
    in, just applied per-token (feature hashing) instead of per-document.

    `corpus.py`'s own `EmbedFn` type is `Callable[[str], list[float]]` (no
    `dims` parameter); `corpus.deterministic_embed` additionally accepts an
    optional `dims` kwarg, which this module uses (2 dims -- 65536 possible
    buckets, comfortably more than `_HASH_BUCKETS`) when available and falls
    back to the single-argument call otherwise, so an injected `embed_fn`
    matching either shape works."""
    try:
        vector = embed_fn(token, 2)
    except TypeError:
        vector = embed_fn(token)
    byte0 = int(round(vector[0] * 255))
    byte1 = int(round(vector[1] * 255)) if len(vector) > 1 else 0
    return (byte0 * 256 + byte1) % _HASH_BUCKETS


def _term_counts(text: str, embed_fn: EmbedFn) -> list[float]:
    """Hashed bag-of-words term-count vector for `text`."""
    counts = [0.0] * _HASH_BUCKETS
    for token in _tokenize(text):
        counts[_token_bucket(token, embed_fn)] += 1.0
    return counts


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Playbook topic "position text" -- reuses corpus.py's own topic vocabulary
# ---------------------------------------------------------------------------


def _topic_position_text(topic: dict[str, Any]) -> str:
    """The text a playbook topic is matched against: its display heading
    (`section_ref`), its position statement (`our_standard`), and -- when
    this topic id has one -- `corpus._KEYWORD_ALIASES`' curated keyword
    list, the SAME curated vocabulary `corpus._build_topic_alias_index` /
    `corpus._match_by_keyword` already rely on for corpus ingestion's own
    heading-plus-body keyword fallback (issue #215)."""
    parts = [topic.get("section_ref", ""), topic.get("our_standard", "")]
    parts.extend(corpus._KEYWORD_ALIASES.get(topic.get("id", ""), []))
    return "\n".join(part for part in parts if part)


def _idf_weights(topic_term_counts: dict[str, list[float]]) -> list[float]:
    """Inverse-document-frequency weight per hash bucket, computed over the
    playbook's own topic set (the "document collection" IDF is relative
    to). Smoothed (`+1` numerator/denominator, `+1` overall) so a bucket
    present in every topic still gets a small positive weight instead of
    zeroing out identically-shared vocabulary."""
    topic_count = len(topic_term_counts)
    document_frequency = [0] * _HASH_BUCKETS
    for counts in topic_term_counts.values():
        for bucket, count in enumerate(counts):
            if count > 0:
                document_frequency[bucket] += 1
    return [
        math.log((topic_count + 1) / (df + 1)) + 1.0 for df in document_frequency
    ]


def _weighted(counts: list[float], idf: list[float]) -> list[float]:
    return [count * weight for count, weight in zip(counts, idf)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_topic_vectors(
    playbook: dict[str, Any],
    *,
    embed_fn: EmbedFn = corpus.deterministic_embed,
) -> tuple[dict[str, list[float]], list[float]]:
    """Builds one TF-IDF vector per playbook topic id, plus the IDF weight
    vector used to embed clause text comparably (same weighting applied to
    both sides of the cosine-similarity comparison). Deterministic: same
    `playbook` + `embed_fn` always produces the same vectors."""
    topic_term_counts: dict[str, list[float]] = {}
    for topic in playbook.get("topics", []):
        topic_id = topic.get("id")
        if not topic_id:
            continue
        topic_term_counts[topic_id] = _term_counts(_topic_position_text(topic), embed_fn)

    idf = _idf_weights(topic_term_counts)
    topic_vectors = {
        topic_id: _weighted(counts, idf) for topic_id, counts in topic_term_counts.items()
    }
    return topic_vectors, idf


def embed_clause_text(
    text: str,
    idf: list[float],
    *,
    embed_fn: EmbedFn = corpus.deterministic_embed,
) -> list[float]:
    """Embeds clause text with the SAME IDF weighting `build_topic_vectors`
    computed from the playbook's topic set, so clause and topic vectors are
    directly comparable via cosine similarity."""
    return _weighted(_term_counts(text, embed_fn), idf)


def match_clauses_to_playbook(
    clause_records: list[dict[str, Any]],
    playbook: dict[str, Any],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    embed_fn: EmbedFn = corpus.deterministic_embed,
) -> dict[str, Any]:
    """Assigns each clause record (issue #248's output shape --
    `{"clause_id": ..., "heading": ..., "text": ..., "order": ...}`) to
    zero-or-one playbook topic by deterministic content similarity.

    Returns:
      {
        "assignments": [
          {"clause_id": ..., "playbook_topic_id": <id> | None, "score": float},
          ...  # one entry per input clause record, in input order
        ],
        "topic_matches": {<playbook_topic_id>: [clause_id, ...], ...},
        # every playbook topic id is a key, even with an empty list --
        # Slice 4 needs to see "genuinely no match", not a missing key.
      }

    `score` is the best cosine-similarity value found for that clause
    (rounded), regardless of whether it cleared `threshold` -- so a caller
    can inspect how close an unmatched clause came, without this module
    ever assigning it a topic it didn't clear the bar for (no force-fit;
    ties broken by ascending `playbook_topic_id` for determinism).
    """
    topic_vectors, idf = build_topic_vectors(playbook, embed_fn=embed_fn)
    topic_matches: dict[str, list[str]] = {topic_id: [] for topic_id in sorted(topic_vectors)}

    assignments: list[dict[str, Any]] = []
    for clause in clause_records:
        clause_id = clause["clause_id"]
        heading = clause.get("heading") or ""
        text = clause.get("text", "")
        clause_vector = embed_clause_text(f"{heading}\n{text}", idf, embed_fn=embed_fn)

        scores = {
            topic_id: _cosine_similarity(clause_vector, topic_vector)
            for topic_id, topic_vector in topic_vectors.items()
        }
        if scores:
            best_topic_id = max(scores, key=lambda topic_id: (scores[topic_id], topic_id))
            best_score = scores[best_topic_id]
        else:
            best_topic_id, best_score = None, 0.0

        assigned_topic_id = best_topic_id if best_score >= threshold else None
        assignments.append(
            {
                "clause_id": clause_id,
                "playbook_topic_id": assigned_topic_id,
                "score": round(best_score, 6),
            }
        )
        if assigned_topic_id is not None:
            topic_matches[assigned_topic_id].append(clause_id)

    return {"assignments": assignments, "topic_matches": topic_matches}


def main() -> None:  # pragma: no cover - manual/CLI smoke entry point
    """CLI smoke test: matches a couple of hand-built clauses against the
    committed eiaa-v1.0.0 playbook."""
    playbook = corpus._load_playbook()
    clauses = [
        {
            "clause_id": "clause_smoke_confidentiality",
            "heading": "Confidentiality",
            "text": (
                "Each party agrees to maintain the confidentiality of all "
                "Confidential Information disclosed by the other party."
            ),
            "order": 0,
        },
        {
            "clause_id": "clause_smoke_unrelated",
            "heading": None,
            "text": "The parties may hold a joint holiday party for staff.",
            "order": 1,
        },
    ]
    result = match_clauses_to_playbook(clauses, playbook)
    for assignment in result["assignments"]:
        print(
            f"{assignment['clause_id']} -> "
            f"{assignment['playbook_topic_id']} (score={assignment['score']})"
        )


if __name__ == "__main__":
    main()
    sys.exit(0)
