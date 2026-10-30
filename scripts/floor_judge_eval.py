#!/usr/bin/env python3
"""
Floor judge accuracy eval -- how often `scripts/floor_judge.py` calls a
signed Floor invariant correctly.

## Why this exists

`floor_judge.judge_floor_invariants` is the only control in the review that
turns a playbook's signed hard lines (`opf.floor.invariants`) into a
monotonic REQUEST_CHANGE, and until this script nothing had ever scored
it. Every existing Floor test drives it with a `FakeBedrockClient`, so it
proves the seam (shape validation, retry, fail-closed, perspective note)
and says nothing about whether a real model's verdicts are right. Issue
#679 is what that gap costs: measured live, the judge resolved "the
counterparty" backwards on every run, and no suite could see it.

The question came over from the engine (playbook-engine#5, Part A): the
engine writes and signs the Floor, but the judge that applies it lives here,
so this is where its accuracy can be measured.

## What it measures

`tests/fixtures/floor-judge-eval/cases.json` holds hand-written, SYNTHETIC
invariants (NDA-example style), each paired with clauses labelled
`expected_violated`. Every case carries a `kind`:

  - `violation` -- states the forbidden term plainly.
  - `paraphrase_violation` -- the same violation in words the invariant does
    not use, so a judge that matches vocabulary misses it.
  - `compliant` -- addresses the concept and satisfies it, including
    boundary values.
  - `mention_not_violation` -- names the concept without violating it (a
    for-cause termination clause against a convenience-notice rule, a forum
    clause against a governing-law rule). A judge that fires on topic
    rather than substance fails these.
  - `directional_near_miss` -- the violation's mirror image, running in our
    client's favour. These are the #679 cases.

Each (invariant, clause) pair is one call to the REAL
`judge_floor_invariants`, unmodified, with the clause as `review_context`
and the perspective note `review_spine._floor_perspective_note` renders for
the fixture's `perspective` block -- the same note a production review
sends. The report gives, per invariant and overall: TP/FP/TN/FN, precision,
recall, specificity, per-kind accuracy, and every miss by case id. An
invariant the judge could not answer (`unjudged`, or a model-seam error) is
counted on its own and never scored as right or wrong: in production it
fails the review closed rather than passing it.

Precision is reported next to recall on purpose. A judge that fires on
everything scores recall 1.0, and the negative kinds above are what expose
it.

## The wording check (`--break`)

`--break INVARIANT_ID` additionally judges that invariant's cases against
its `broken_statement` -- the same rule with one condition deleted (the
30-day minimum, the three-year term) -- and scores the answers against the
ORIGINAL labels. If the judge is reading the statement, the cases that
hinged on the deleted condition flip and the score falls; if the score does
not move, the judge was answering from the clause alone and the invariant's
wording is not what governs its verdict. The report's `wording_check` block
holds both arms and the deltas.

## What it does NOT establish

Whether the labels are legally right. They are the fixture author's reading
of each statement; an attorney confirming the judge's calls are CORRECT,
not just consistent with those labels, is Part B of the handoff and needs
the owner. One clause per call is also narrower than production, where
`review_context` is the whole document.

## Running it

Live only -- a fake client would score the fake. It spends real money (one
call per case per run, plus one per broken case), so it never runs in CI
and refuses to start without `--yes`. The model defaults to the one a
production review gives the judge, the resolved critic id
(`model_settings.resolve_openrouter_model_ids`, which honours
`OPENROUTER_CRITIC_MODEL_ID`):

    OPENROUTER_API_KEY="$(security find-generic-password -s openrouter-api-key -w)" \\
    OPENROUTER_CRITIC_MODEL_ID=anthropic/claude-haiku-4.5 \\
    python3 scripts/floor_judge_eval.py --break floor-termination-notice --out floor-eval.json --yes

The report is built only from this file's synthetic fixtures and the
judge's verdicts on them; there is no document text in it to leak.

Offline test: `python3 tests/test_floor_judge_eval.py`
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional  # noqa: UP035

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"

for _dir in (BACKEND_SRC_DIR, SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import floor_judge  # noqa: E402
import review_spine  # noqa: E402

# The backend modules are loaded by name: this script is type-checked strict,
# and mypy cannot resolve backend/src's top-level names from scripts/.
_model_client = importlib.import_module("model_client")

CASES_PATH = REPO_ROOT / "tests" / "fixtures" / "floor-judge-eval" / "cases.json"
SCHEMA = "floor-judge-eval-v1"

POSITIVE_KINDS = frozenset({"violation", "paraphrase_violation"})
NEGATIVE_KINDS = frozenset({"compliant", "mention_not_violation", "directional_near_miss"})
CASE_KINDS = POSITIVE_KINDS | NEGATIVE_KINDS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    """Load and shape-check the case file. Raises ValueError on a malformed
    one, so a bad fixture is refused before any money is spent on it."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}: schema must be {SCHEMA!r}")
    seen: set[str] = set()
    for invariant in data["invariants"]:
        for case in invariant["cases"]:
            case_id = case["case_id"]
            if case_id in seen:
                raise ValueError(f"duplicate case_id {case_id!r}")
            seen.add(case_id)
            if case["kind"] not in CASE_KINDS:
                raise ValueError(f"{case_id}: unknown kind {case['kind']!r}")
            if case["expected_violated"] is not (case["kind"] in POSITIVE_KINDS):
                raise ValueError(f"{case_id}: expected_violated contradicts kind {case['kind']!r}")
    return data


def perspective_note(data: dict[str, Any]) -> str:
    """The note a production review would send the judge for this
    perspective -- rendered by the spine's own function, not restated."""
    return review_spine._floor_perspective_note({"perspective": data["perspective"]})


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """One judge call on one case. `got` is True/False for a verdict, None
    when the invariant was left unjudged or the model seam failed."""

    invariant_id: str
    case_id: str
    kind: str
    expected: bool
    got: Optional[bool]  # noqa: UP045
    run: int
    evidence_quote: str = ""
    error: str = ""


def judge_case(
    *,
    invariant_id: str,
    statement: str,
    case: dict[str, Any],
    client: Any,
    model_id: str,
    note: str,
    run: int,
) -> Observation:
    observation = Observation(
        invariant_id=invariant_id,
        case_id=case["case_id"],
        kind=case["kind"],
        expected=case["expected_violated"],
        got=None,
        run=run,
    )
    try:
        judgment = floor_judge.judge_floor_invariants(
            invariants=[{"id": invariant_id, "statement": statement, "rationale": ""}],
            review_context=case["clause"],
            model_client=client,
            model_id=model_id,
            perspective_note=note,
        )
    except _model_client.ModelInvocationError as exc:
        # A broken seam on one case must not forfeit a paid matrix; it is
        # recorded by class name only, like the live smoke runner.
        observation.error = type(exc).__name__
        return observation
    if judgment.verdicts:
        verdict = judgment.verdicts[0]
        observation.got = verdict["violated"]
        observation.evidence_quote = verdict.get("evidence_quote", "")
    else:
        observation.error = "truncated" if judgment.truncated else "unjudged"
    return observation


def judge_all(
    data: dict[str, Any],
    *,
    client: Any,
    model_id: str,
    runs: int = 1,
    statements: Optional[dict[str, str]] = None,  # noqa: UP045
    only: Optional[set[str]] = None,  # noqa: UP045
) -> list[Observation]:
    """Judge every case `runs` times. `statements` overrides an invariant's
    statement by id (the wording check); `only` restricts to those ids."""
    statements = statements or {}
    note = perspective_note(data)
    observations: list[Observation] = []
    for run in range(1, runs + 1):
        for invariant in data["invariants"]:
            invariant_id = invariant["id"]
            if only is not None and invariant_id not in only:
                continue
            statement = statements.get(invariant_id, invariant["statement"])
            for case in invariant["cases"]:
                observations.append(
                    judge_case(
                        invariant_id=invariant_id,
                        statement=statement,
                        case=case,
                        client=client,
                        model_id=model_id,
                        note=note,
                        run=run,
                    )
                )
    return observations


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> Optional[float]:  # noqa: UP045
    """None, not 0.0, when there is nothing to divide by: "no positives were
    predicted" is not "precision was zero"."""
    return round(numerator / denominator, 4) if denominator else None


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    unjudged: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    misses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def precision(self) -> Optional[float]:  # noqa: UP045
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> Optional[float]:  # noqa: UP045
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def specificity(self) -> Optional[float]:  # noqa: UP045
        return _ratio(self.tn, self.tn + self.fp)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "unjudged": self.unjudged,
            "precision": self.precision,
            "recall": self.recall,
            "specificity": self.specificity,
            "by_kind": {
                kind: {**counts, "accuracy": _ratio(counts["correct"], counts["judged"])}
                for kind, counts in sorted(self.by_kind.items())
            },
            "misses": self.misses,
        }


def score(observations: list[Observation]) -> Metrics:
    metrics = Metrics()
    for obs in observations:
        kind = metrics.by_kind.setdefault(obs.kind, {"judged": 0, "correct": 0, "unjudged": 0})
        if obs.got is None:
            metrics.unjudged += 1
            kind["unjudged"] += 1
            continue
        kind["judged"] += 1
        if obs.got == obs.expected:
            kind["correct"] += 1
            if obs.expected:
                metrics.tp += 1
            else:
                metrics.tn += 1
            continue
        if obs.got:
            metrics.fp += 1
        else:
            metrics.fn += 1
        metrics.misses.append(
            {
                "case_id": obs.case_id,
                "kind": obs.kind,
                "expected_violated": obs.expected,
                "judged_violated": obs.got,
                "run": obs.run,
            }
        )
    return metrics


def score_by_invariant(observations: list[Observation]) -> dict[str, Metrics]:
    grouped: dict[str, list[Observation]] = {}
    for obs in observations:
        grouped.setdefault(obs.invariant_id, []).append(obs)
    return {invariant_id: score(group) for invariant_id, group in grouped.items()}


def consistency(observations: list[Observation]) -> dict[str, int]:
    """With `runs > 1`: how many cases got the same answer every run, and how
    many flipped. A judge that flips is not yet worth scoring for accuracy."""
    answers: dict[str, set[Optional[bool]]] = {}  # noqa: UP045
    for obs in observations:
        answers.setdefault(obs.case_id, set()).add(obs.got)
    flipped = sum(1 for seen in answers.values() if len(seen) > 1)
    return {"cases": len(answers), "stable": len(answers) - flipped, "flipped": flipped}


def _delta(after: Optional[float], before: Optional[float]) -> Optional[float]:  # noqa: UP045
    if after is None or before is None:
        return None
    return round(after - before, 4)


def wording_check(
    canonical: Metrics, broken: Metrics, *, invariant_id: str, statement: str
) -> dict[str, Any]:
    deltas = {
        name: _delta(getattr(broken, name), getattr(canonical, name))
        for name in ("precision", "recall", "specificity")
    }
    flipped = (canonical.fp + canonical.fn) != (broken.fp + broken.fn) or any(deltas.values())
    return {
        "invariant_id": invariant_id,
        "broken_statement": statement,
        "canonical": canonical.as_dict(),
        "broken": broken.as_dict(),
        "delta": deltas,
        "score_moved": bool(flipped),
    }


def build_report(
    data: dict[str, Any],
    observations: list[Observation],
    *,
    model_id: str,
    runs: int,
    broken: Optional[dict[str, list[Observation]]] = None,  # noqa: UP045
) -> dict[str, Any]:
    per_invariant = score_by_invariant(observations)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "model_id": model_id,
        "runs": runs,
        "overall": score(observations).as_dict(),
        "per_invariant": {k: v.as_dict() for k, v in per_invariant.items()},
        "consistency": consistency(observations),
        "observations": [
            {
                "invariant_id": o.invariant_id,
                "case_id": o.case_id,
                "kind": o.kind,
                "expected_violated": o.expected,
                "judged_violated": o.got,
                "run": o.run,
                "evidence_quote": o.evidence_quote,
                "error": o.error,
            }
            for o in observations
        ],
    }
    if broken:
        statements = {inv["id"]: inv.get("broken_statement", "") for inv in data["invariants"]}
        report["wording_check"] = [
            wording_check(
                per_invariant[invariant_id],
                score(broken_observations),
                invariant_id=invariant_id,
                statement=statements[invariant_id],
            )
            for invariant_id, broken_observations in broken.items()
        ]
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fmt(value: Optional[float]) -> str:  # noqa: UP045
    return "  n/a" if value is None else f"{value:5.2f}"


def format_summary(report: dict[str, Any]) -> str:
    lines = [f"Floor judge eval -- model {report['model_id']}, {report['runs']} run(s)", ""]
    lines.append(f"{'invariant':40} {'prec':>5} {'rec':>5} {'spec':>5}  tp fp tn fn unj")
    rows = [*report["per_invariant"].items(), ("OVERALL", report["overall"])]
    for name, m in rows:
        lines.append(
            f"{name:40} {_fmt(m['precision'])} {_fmt(m['recall'])} {_fmt(m['specificity'])}"
            f"  {m['tp']:2} {m['fp']:2} {m['tn']:2} {m['fn']:2} {m['unjudged']:3}"
        )
    lines.append("")
    lines.append("accuracy by case kind:")
    for kind, counts in report["overall"]["by_kind"].items():
        lines.append(
            f"  {kind:24} {_fmt(counts['accuracy'])}  ({counts['correct']}/{counts['judged']})"
        )
    misses = report["overall"]["misses"]
    if misses:
        lines.append("")
        lines.append("misses:")
        for miss in misses:
            kind = "false positive" if miss["judged_violated"] else "false negative"
            lines.append(f"  {miss['case_id']} ({miss['kind']}, run {miss['run']}): {kind}")
    c = report["consistency"]
    if report["runs"] > 1:
        lines.append("")
        lines.append(f"consistency: {c['stable']}/{c['cases']} cases answered the same every run")
    for check in report.get("wording_check", []):
        lines.append("")
        lines.append(f"wording check -- {check['invariant_id']}")
        lines.append(f"  broken statement: {check['broken_statement']}")
        for arm in ("canonical", "broken"):
            m = check[arm]
            lines.append(
                f"  {arm:9} prec {_fmt(m['precision'])}  rec {_fmt(m['recall'])}  "
                f"spec {_fmt(m['specificity'])}  misses {len(m['misses'])}"
            )
        lines.append(f"  score moved: {'yes' if check['score_moved'] else 'NO'}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score the Floor judge against synthetic invariants."
    )
    parser.add_argument(
        "--cases", default=str(CASES_PATH), help="Case file (default: the shipped synthetic set)."
    )
    parser.add_argument(
        "--model-id",
        default=None,
        help="Model to judge with (default: the resolved critic id a production review gives the judge).",
    )
    parser.add_argument(
        "--runs", type=int, default=1, help="Repetitions of the whole set (default: 1)."
    )
    parser.add_argument(
        "--break",
        dest="break_ids",
        action="append",
        default=[],
        metavar="INVARIANT_ID",
        help="Also judge this invariant against its broken_statement and report the score change. Repeatable.",
    )
    parser.add_argument("--out", default=None, help="Write the full JSON report here.")
    parser.add_argument(
        "--yes", action="store_true", help="Required: confirms the live spend shown in the preview."
    )
    return parser


def main(
    argv: Optional[list[str]] = None,  # noqa: UP045
    *,
    build_client: Optional[Callable[[], Any]] = None,  # noqa: UP045
    resolve_api_key: Optional[Callable[[], str]] = None,  # noqa: UP045
    resolve_model_id: Optional[Callable[[], str]] = None,  # noqa: UP045
) -> int:
    """The three callables are seams for `tests/test_floor_judge_eval.py`;
    each defaults to the live path a production review uses, imported only
    when it is actually taken."""
    if build_client is None:

        def build_client() -> Any:
            return importlib.import_module("pipeline_runner")._build_openrouter_client(None)

    if resolve_api_key is None:

        def resolve_api_key() -> str:
            return str(
                importlib.import_module("model_settings").resolve_openrouter_api_key(None) or ""
            )

    if resolve_model_id is None:

        def resolve_model_id() -> str:
            ids = importlib.import_module("model_settings").resolve_openrouter_model_ids(None)
            return str(ids["critic"])

    args = build_arg_parser().parse_args(argv)
    if args.runs < 1:
        print(f"ERROR: --runs must be >= 1 (got {args.runs}).", file=sys.stderr)
        return 1
    try:
        data = load_cases(Path(args.cases))
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: unusable case file: {exc}", file=sys.stderr)
        return 1

    by_id = {inv["id"]: inv for inv in data["invariants"]}
    for invariant_id in args.break_ids:
        if not by_id.get(invariant_id, {}).get("broken_statement"):
            print(
                f"ERROR: --break {invariant_id}: no such invariant with a broken_statement.",
                file=sys.stderr,
            )
            return 1

    if not resolve_api_key():
        print(
            "ERROR: no OpenRouter API key configured. Set OPENROUTER_API_KEY, then re-run.",
            file=sys.stderr,
        )
        return 1

    model_id = args.model_id or resolve_model_id()
    case_count = sum(len(inv["cases"]) for inv in data["invariants"])
    broken_count = sum(len(by_id[i]["cases"]) for i in args.break_ids)
    calls = (case_count + broken_count) * args.runs
    print(
        f"Will make about {calls} judge calls on {model_id} "
        f"({case_count} cases + {broken_count} broken-wording cases, x{args.runs} runs; "
        "a rejected verdict retries once)."
    )
    if not args.yes:
        print("Refusing to proceed without --yes.", file=sys.stderr)
        return 1

    client = build_client()
    try:
        observations = judge_all(data, client=client, model_id=model_id, runs=args.runs)
        broken = {
            invariant_id: judge_all(
                data,
                client=client,
                model_id=model_id,
                runs=args.runs,
                statements={invariant_id: by_id[invariant_id]["broken_statement"]},
                only={invariant_id},
            )
            for invariant_id in args.break_ids
        }
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    report = build_report(data, observations, model_id=model_id, runs=args.runs, broken=broken)
    print(format_summary(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nReport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
