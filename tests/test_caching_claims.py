#!/usr/bin/env python3
"""
Red gate for issue #30: Correct prompt-caching claims; structure for
within-model hits; measure on eval runs.

Three invariants checked here (all fail against the repo state before the fix):

  AC1 — No cross-model cache-reuse claim in living docs.
        ARCHITECTURE.md and docs/design-notes.md must NOT claim that a single
        cache serves both model passes (Opus primary and Sonnet critic use
        separate caches; an Opus cache hit cannot serve the Sonnet critic).
        The specific false phrase is "reduces per-call cost and latency
        materially across both passes" — it implies a shared cache.

  AC2 — Cost-shape table acknowledges near-zero steady-state hit rate at v1
        volume.
        ARCHITECTURE.md cost-shape section must carry an explicit
        "steady-state hit rate" line (or equivalent phrase) noting that
        at v1 review volume (2–7/day spread across a workday), the
        inter-review cache-hit rate is near zero — the TTL is short relative
        to the time between reviews.

  AC3 — Eval-harness serialization and cache-hit metric documented.
        docs/evaluation.md must document:
          a) That cases are serialized within the cache TTL window (per model)
             so that back-to-back eval runs benefit from within-model cache
             hits.
          b) That cache-hit metrics (or a cache-hit rate) are recorded per
             eval run, not just described in prose.

Usage:
    python3 tests/test_caching_claims.py
    Exit 0 = all checks pass; non-zero = one or more checks fail.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
DESIGN_NOTES = REPO_ROOT / "docs" / "design-notes.md"
EVALUATION = REPO_ROOT / "docs" / "evaluation.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── AC1: No cross-model cache-reuse claim ────────────────────────────────────

# The old phrase "reduces per-call cost and latency materially across both
# passes" implies the Opus cache hit also benefits the Sonnet critic pass,
# which is false — caches are per-model.
#
# We check that this specific misleading phrase has been removed.
CROSS_MODEL_CACHE_PHRASE = re.compile(
    r"(?:reduces|reduce|lower)[^\n]{0,80}"
    r"(?:per.?call|cost|latency)[^\n]{0,80}"
    r"(?:across\s+both\s+passes|both\s+passes)",
    re.IGNORECASE,
)

# Additional guard: "both passes" must not appear in a positive caching
# context that implies a single shared cache benefits both Opus and Sonnet.
# "uncached pricing" / "uncached price" are reservation-formula phrases that
# are correct (the reservation is at uncached rates) — they must not trigger.
# We only flag when "both passes" follows "cache" in a positive-benefit context
# (e.g. "caching … reduces … across both passes").
BOTH_PASSES_CACHE_CONTEXT = re.compile(
    r"cach(?:e|ing)[^\n]{0,120}both\s+passes"
    r"|both\s+passes[^\n]{0,120}cach(?:e|ing)",
    re.IGNORECASE,
)
# Phrases that indicate this is a reservation/cost-ceiling context (correct),
# not an affirmative claim that a cache hit benefits both passes.
# Note: the BOTH_PASSES_CACHE_CONTEXT regex consumes "cache" as its terminal
# match, so "uncached pricing" may appear as "uncach" + remaining text outside
# the captured span.  We therefore also look at surrounding context.
_RESERVATION_EXEMPTION = re.compile(
    r"uncached\s+pric(?:e|ing)|reservation|reserve|uncached_price|uncach",
    re.IGNORECASE,
)


def check_ac1_no_cross_model_claim() -> list[str]:
    """
    ARCHITECTURE.md and docs/design-notes.md must not claim a single cache
    serves both the Opus primary and the Sonnet critic passes.
    """
    failures = []

    for path in [ARCHITECTURE, DESIGN_NOTES]:
        if not path.exists():
            continue
        text = read(path)
        rel = path.relative_to(REPO_ROOT)

        # Check for the exact misleading phrase
        for lineno, line in enumerate(text.splitlines(), 1):
            if CROSS_MODEL_CACHE_PHRASE.search(line):
                failures.append(
                    f"  AC1 FAIL: {rel}:{lineno} — phrase implies cross-model "
                    f"cache reuse (Opus cache cannot serve Sonnet critic):\n"
                    f"    > {line.strip()}"
                )

        # Check for "both passes" in a caching context without a within-model
        # clarification — a within-model claim is fine ("within-model" or
        # "per-model" must appear nearby, or "both passes" must be absent from
        # caching contexts).
        # Exempt: reservation/cost-ceiling contexts that say "uncached pricing"
        # (the reservation formula correctly runs at uncached rates).
        for m in BOTH_PASSES_CACHE_CONTEXT.finditer(text):
            snippet = m.group(0)
            # Allow if the phrase explicitly says "within-model" or "per-model"
            if re.search(r"within.model|per.model", snippet, re.IGNORECASE):
                continue
            # Allow reservation-formula context (uncached pricing / reserve)
            if _RESERVATION_EXEMPTION.search(snippet):
                continue
            lineno = text[: m.start()].count("\n") + 1
            failures.append(
                f"  AC1 FAIL: {rel}:{lineno} — 'both passes' in a caching "
                f"context without a within-model clarification (caches are "
                f"per-model; Opus cache cannot hit on a Sonnet call):\n"
                f"    > {snippet[:120].strip()}"
            )

    return failures


# ── AC2: Cost-shape acknowledges near-zero steady-state hit rate ─────────────

# The cost-shape section must carry a line that states the production
# steady-state hit rate is near zero at v1 volume — not just "it's an
# optimization" (that already exists) but an honest acknowledgement that
# at 2–7/day with a ~5-min TTL, inter-review hits won't materialize.
STEADY_STATE_HIT_RATE_PATTERN = re.compile(
    r"steady.state\s+hit\s+rate"
    r"|inter.review\s+(?:cache\s+)?hit\s+rate"
    r"|hit\s+rate\s+≈\s*0"
    r"|near.zero[^\n]{0,60}hit"
    r"|hit[^\n]{0,60}near.zero",
    re.IGNORECASE,
)


def check_ac2_steady_state_hit_rate() -> list[str]:
    """
    ARCHITECTURE.md cost-shape section must explicitly acknowledge that the
    steady-state inter-review cache-hit rate is near zero at v1 production
    volume (2–7 reviews/day spread across a workday).
    """
    failures = []

    if not ARCHITECTURE.exists():
        failures.append("  AC2 FAIL: ARCHITECTURE.md not found")
        return failures

    arch_text = read(ARCHITECTURE)

    if not STEADY_STATE_HIT_RATE_PATTERN.search(arch_text):
        failures.append(
            "  AC2 FAIL: ARCHITECTURE.md cost-shape section lacks an explicit\n"
            "  'steady-state hit rate ≈ 0 at v1 volume' statement (or equivalent).\n"
            "  The issue: at 2–7 reviews/day spread across a workday and a TTL of\n"
            "  ~5 min, inter-review cache hits will not materialize in production.\n"
            "  Required: an honest acknowledgement that caching only helps on eval\n"
            "  runs and back-to-back retries, not typical production usage."
        )

    return failures


# ── AC3: Eval harness serialization within TTL + cache-hit metrics ───────────

# evaluation.md already documents some serialization and cache-hit rate
# optimization.  This check verifies that:
#   a) Serialization is explicitly described as being within the cache TTL
#      window (per-model, not just "serialize calls").
#   b) Cache-hit metrics (hit rate, hit count, or equivalent) are described
#      as being *recorded per run* — not just mentioned as a prose benefit.

SERIALIZATION_WITHIN_TTL_PATTERN = re.compile(
    r"(?:serializ|within\s+the\s+cache|TTL\s+window|within\s+TTL|"
    r"cache\s+TTL|TTL\s+warm|warm\s+for\s+the|still\s+warm)",
    re.IGNORECASE,
)

CACHE_HIT_METRIC_RECORDED_PATTERN = re.compile(
    r"cache.hit\s+(?:metric|rate|count)[^\n]{0,80}(?:record|log|emit|track|measur)"
    r"|(?:record|log|emit|track|measur)[^\n]{0,80}cache.hit\s+(?:metric|rate|count)"
    r"|per.run[^\n]{0,80}cache.hit"
    r"|cache.hit[^\n]{0,80}per.run",
    re.IGNORECASE,
)


def check_ac3_eval_serialization_and_metrics() -> list[str]:
    """
    docs/evaluation.md must document:
      a) That eval cases are serialized within the cache TTL window (per model).
      b) That cache-hit metrics are recorded per eval run.
    """
    failures = []

    if not EVALUATION.exists():
        failures.append("  AC3 FAIL: docs/evaluation.md not found")
        return failures

    eval_text = read(EVALUATION)

    # Check (a): serialization within TTL window documented
    if not SERIALIZATION_WITHIN_TTL_PATTERN.search(eval_text):
        failures.append(
            "  AC3 FAIL: docs/evaluation.md does not document that eval cases\n"
            "  are serialized within the cache TTL window per model.\n"
            "  Required: a statement that the harness sequences calls so the\n"
            "  playbook block (~30K tokens) cached on one call is still warm for\n"
            "  the next, within the per-model TTL — maximizing within-model hits\n"
            "  on back-to-back eval runs.  (issue #30)"
        )

    # Check (b): cache-hit metrics recorded per run
    if not CACHE_HIT_METRIC_RECORDED_PATTERN.search(eval_text):
        failures.append(
            "  AC3 FAIL: docs/evaluation.md does not describe cache-hit metrics\n"
            "  being recorded per eval run.\n"
            "  Required: a statement that the harness records (or emits) a\n"
            "  per-run cache-hit rate or cache-hit count so actual savings can\n"
            "  be measured against the estimated 37% token reduction.  (issue #30)"
        )

    return failures


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    checks = [
        (
            "AC1",
            "No cross-model cache-reuse claim in ARCHITECTURE.md / design-notes.md",
            check_ac1_no_cross_model_claim,
        ),
        (
            "AC2",
            "Cost-shape table acknowledges near-zero steady-state hit rate at v1 volume",
            check_ac2_steady_state_hit_rate,
        ),
        (
            "AC3",
            "Eval harness: serialization within TTL + cache-hit metrics recorded per run",
            check_ac3_eval_serialization_and_metrics,
        ),
    ]

    overall_pass = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"{code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All caching-claims checks passed.")
        return 0
    else:
        print("One or more caching-claims checks FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
