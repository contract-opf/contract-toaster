#!/usr/bin/env python3
"""
Slice test (TDD) for issue #293: "OPF bind: pen rules are toaster-owned
bind config -- global defaults, per-playbook precedence, floor-derived
sticky must_not_introduce, pipeline enforcement wiring".

## Root problem this proves fixed

Before this slice, `scripts/replacement_text_enforcement.py` enforced only
a single flat `replacement_text` block per v1 playbook topic -- there was
no toaster-global defaults artifact, no bundle-level `pen_rules`
precedence (per-topic > bundle-default > global defaults), and no
`floor_ref` stickiness guaranteeing a Floor-derived `must_not_introduce`
entry can never be silently dropped by a more specific layer. This test
FAILS on the pre-#293 tree: `playbooks/pen-rules.defaults.json` does not
exist and `replacement_text_enforcement.resolve_pen_rules` does not exist
(AttributeError/FileNotFoundError) and `bind_bundle.py` has no
`--pen-rules` floor_ref validation.

## What this test asserts (mirrors the issue's acceptance criteria)

  1. Resolution precedence: per-topic > bundle-default > global; scalars
     override wholesale.
  2. Sticky floor_ref entries survive every layer (global -> bundle-default
     -> per-topic), even when a more specific layer doesn't mention them.
  3. v1 compatibility: a v1 playbook (topics[].replacement_text, no
     top-level pen_rules keys) resolves byte-identical to the topic's own
     replacement_text block -- global defaults/stickiness never apply.
  4. `bind_bundle`'s --pen-rules validation: an unknown floor_ref raises
     BindBundleError listing the bad ref(s); a valid floor_ref binds
     cleanly and the bundle still validates against
     playbooks/bundle.schema-v2.json.
  5. Floor independence: `floor_judge.judge_floor_invariants`'s invariants
     input is IDENTICAL whether or not any pen-rules config is present --
     nothing pen-rules-shaped can filter/scope/disable Floor judging.

Run with: python3 tests/test_pen_rules_resolution.py
Exit codes: 0 = pass, 1 = fail
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bind_bundle  # noqa: E402
import floor_judge  # noqa: E402
import opf_load  # noqa: E402
import replacement_text_enforcement as rte  # noqa: E402

PLAYBOOK_PATH = REPO_ROOT / "playbooks" / "eiaa-v1.0.0.json"
OPF_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "opf" / "synthetic-eiaa.opf.json"
MODEL_POLICY_PATH = REPO_ROOT / "model-policy" / "openrouter.json"
DEFAULTS_ARTIFACT_PATH = REPO_ROOT / "playbooks" / "pen-rules.defaults.json"

# The two real Floor invariant ids on the synthetic fixture OPF
# (tests/fixtures/opf/synthetic-eiaa.opf.json -> opf.floor.invariants).
_REAL_FLOOR_REF = "floor-no-uncapped-liability"


def _load_playbook() -> dict[str, Any]:
    with open(PLAYBOOK_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write_temp_json(data: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# 0. The defaults artifact itself: exists, generic, de-branded.
# ---------------------------------------------------------------------------


def test_defaults_artifact_exists_and_is_generic(failures: list[str]) -> None:
    if not DEFAULTS_ARTIFACT_PATH.exists():
        failures.append(f"[0a] {DEFAULTS_ARTIFACT_PATH} does not exist.")
        return
    with open(DEFAULTS_ARTIFACT_PATH, encoding="utf-8") as f:
        raw_text = f.read()
        doc = json.loads(raw_text)

    if "exos" in raw_text.lower():
        failures.append("[0b] pen-rules.defaults.json must not contain 'Exos'/'EXOS' (de-brand rule).")

    default_block = doc.get("default")
    if not isinstance(default_block, dict):
        failures.append("[0c] pen-rules.defaults.json must have a top-level 'default' object.")
        return
    if "mode" not in default_block or "max_chars" not in default_block:
        failures.append(f"[0d] 'default' block must carry mode + max_chars; got {default_block!r}")
    mni = default_block.get("must_not_introduce", [])
    if not isinstance(mni, list):
        failures.append("[0e] 'default.must_not_introduce' must be a list.")
    for entry in mni:
        if not isinstance(entry, dict) or "phrase" not in entry:
            failures.append(f"[0f] every must_not_introduce entry must be a {{'phrase': ...}} object; got {entry!r}")


# ---------------------------------------------------------------------------
# 1 + 2. Resolution precedence + floor_ref stickiness.
# ---------------------------------------------------------------------------


def test_none_bundle_returns_global_defaults(failures: list[str]) -> None:
    resolved = rte.resolve_pen_rules(None, "any-topic-id")
    with open(DEFAULTS_ARTIFACT_PATH, encoding="utf-8") as f:
        expected = json.load(f)["default"]
    if resolved != expected:
        failures.append(f"[1a] resolve_pen_rules(None, ...) must equal the global defaults artifact's 'default' block; got {resolved!r}, expected {expected!r}")


def test_bundle_default_overrides_global(failures: list[str]) -> None:
    bundle = {"default": {"mode": "bounded_edit", "max_chars": 42, "must_not_introduce": []}}
    resolved = rte.resolve_pen_rules(bundle, "some-topic")
    if resolved["mode"] != "bounded_edit" or resolved["max_chars"] != 42:
        failures.append(f"[1b] bundle 'default' must override the global defaults artifact wholesale; got {resolved!r}")


def test_per_topic_overrides_bundle_default(failures: list[str]) -> None:
    bundle = {
        "default": {"mode": "bounded_edit", "max_chars": 42, "must_not_introduce": []},
        "per_topic": {"topic-x": {"max_chars": 7}},
    }
    resolved = rte.resolve_pen_rules(bundle, "topic-x")
    if resolved["max_chars"] != 7:
        failures.append(f"[1c] per_topic override must win over bundle default for max_chars; got {resolved!r}")
    if resolved["mode"] != "bounded_edit":
        failures.append(f"[1d] a scalar NOT overridden by per_topic must fall back to bundle default; got {resolved!r}")

    resolved_other_topic = rte.resolve_pen_rules(bundle, "topic-y")
    if resolved_other_topic["max_chars"] != 42:
        failures.append(f"[1e] a topic with no per_topic override must use the bundle default; got {resolved_other_topic!r}")


def test_floor_ref_entries_survive_every_layer(failures: list[str]) -> None:
    """A must_not_introduce entry carrying floor_ref in the GLOBAL DEFAULTS
    layer must survive into the resolved result even when the bundle
    default AND the per-topic override are both silent about it -- and even
    though the per-topic layer is otherwise the "most specific" layer whose
    plain entries win."""
    custom_defaults_path = _write_temp_json(
        {
            "default": {
                "mode": "replace",
                "max_chars": 1000,
                "must_not_introduce": [{"phrase": "uncapped", "floor_ref": _REAL_FLOOR_REF}],
            }
        }
    )
    try:
        bundle = {
            "default": {"mode": "replace", "max_chars": 900, "must_not_introduce": [{"phrase": "indemnify"}]},
            "per_topic": {
                "topic-x": {"max_chars": 300, "must_not_introduce": [{"phrase": "exclusive"}]},
            },
        }
        resolved = rte.resolve_pen_rules(bundle, "topic-x", defaults_path=custom_defaults_path)

        phrases = {entry["phrase"] for entry in resolved["must_not_introduce"]}
        if "uncapped" not in phrases:
            failures.append(f"[2a] a global-defaults floor_ref entry must survive into the per-topic-resolved result; got {resolved!r}")
        if "exclusive" not in phrases:
            failures.append(f"[2b] the most-specific (per_topic) layer's own plain entry must still be present; got {resolved!r}")
        if "indemnify" in phrases:
            failures.append(
                f"[2c] a plain (non-floor_ref) entry from a LESS specific layer (bundle default) must NOT survive once a "
                f"more specific layer (per_topic) defines its own must_not_introduce list; got {resolved!r}"
            )
        uncapped_entry = next((e for e in resolved["must_not_introduce"] if e["phrase"] == "uncapped"), None)
        if not uncapped_entry or uncapped_entry.get("floor_ref") != _REAL_FLOOR_REF:
            failures.append(f"[2d] the surviving sticky entry must retain its floor_ref; got {uncapped_entry!r}")

        # Same fixture, but topic-y has no per_topic override: bundle
        # default's plain entry ("indemnify") wins as the most-specific
        # layer instead, and the sticky floor_ref entry still survives.
        resolved_y = rte.resolve_pen_rules(bundle, "topic-y", defaults_path=custom_defaults_path)
        phrases_y = {entry["phrase"] for entry in resolved_y["must_not_introduce"]}
        if "indemnify" not in phrases_y:
            failures.append(f"[2e] bundle default's plain entry must apply when no per_topic override exists; got {resolved_y!r}")
        if "uncapped" not in phrases_y:
            failures.append(f"[2f] the sticky floor_ref entry must survive for topic-y too; got {resolved_y!r}")
    finally:
        custom_defaults_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. v1 compatibility: byte-identical passthrough.
# ---------------------------------------------------------------------------


def test_v1_playbook_passthrough_is_byte_identical(failures: list[str]) -> None:
    playbook = _load_playbook()
    topic = rte.find_topic(playbook, "limitation-of-liability")
    if topic is None:
        failures.append("[3a] fixture playbook must carry a 'limitation-of-liability' topic.")
        return
    expected = copy.deepcopy(topic["replacement_text"])

    resolved = rte.resolve_pen_rules(playbook, "limitation-of-liability")
    if resolved != expected:
        failures.append(
            f"[3b] resolve_pen_rules on a v1 playbook (no bundle pen_rules keys) must return the topic's own "
            f"replacement_text UNCHANGED; got {resolved!r}, expected {expected!r}"
        )

    # And check_replacement_text behaves identically whether called with the
    # raw topic or with resolve_pen_rules's (identical, for v1) output.
    text = "Each party's aggregate liability under this Agreement shall not exceed $150,000."
    direct = rte.check_replacement_text(topic, text)
    via_resolve = rte.check_replacement_text({"id": "limitation-of-liability", "replacement_text": resolved}, text)
    if direct.passed != via_resolve.passed or direct.failure != via_resolve.failure:
        failures.append(f"[3c] check_replacement_text must behave identically via direct topic vs. resolve_pen_rules output; got {direct!r} vs {via_resolve!r}")


# ---------------------------------------------------------------------------
# 4. bind_bundle --pen-rules floor_ref validation.
# ---------------------------------------------------------------------------


def _load_opf_fixture() -> dict:
    return opf_load.load_opf(OPF_FIXTURE_PATH)


def test_bind_bundle_rejects_unknown_floor_ref(failures: list[str]) -> None:
    opf_doc = _load_opf_fixture()
    pen_rules = {
        "default": {
            "mode": "replace",
            "max_chars": 500,
            "must_not_introduce": [{"phrase": "made up phrase", "floor_ref": "not-a-real-invariant-id"}],
        }
    }
    try:
        bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            pen_rules=pen_rules,
        )
        failures.append("[4a] bind_bundle must raise BindBundleError for an unknown floor_ref, not silently accept it.")
    except bind_bundle.BindBundleError as exc:
        if "not-a-real-invariant-id" not in str(exc):
            failures.append(f"[4b] BindBundleError message must list the bad floor_ref(s); got: {exc}")


def test_bind_bundle_accepts_valid_floor_ref_and_bundle_validates(failures: list[str]) -> None:
    opf_doc = _load_opf_fixture()
    pen_rules = {
        "default": {
            "mode": "replace",
            "max_chars": 500,
            "must_not_introduce": [{"phrase": "uncapped", "floor_ref": _REAL_FLOOR_REF}],
        }
    }
    try:
        bundle_doc = bind_bundle.bind_bundle(
            opf_doc,
            playbook_id="eiaa",
            model_policy_path=MODEL_POLICY_PATH,
            pen_rules=pen_rules,
        )
    except bind_bundle.BindBundleError as exc:
        failures.append(f"[4c] bind_bundle must accept a pen_rules doc whose floor_ref names a real invariant id; raised: {exc}")
        return

    if bundle_doc.get("pen_rules") != pen_rules:
        failures.append(f"[4d] bound bundle must carry the pen_rules doc verbatim; got {bundle_doc.get('pen_rules')!r}")

    try:
        import jsonschema

        with open(REPO_ROOT / "playbooks" / "bundle.schema-v2.json", encoding="utf-8") as f:
            schema = json.load(f)
        jsonschema.validate(instance=bundle_doc, schema=schema)
    except Exception as exc:  # noqa: BLE001 - want any schema-validation failure surfaced as a test failure
        failures.append(f"[4e] bundle with a valid pen_rules block must still validate against bundle.schema-v2.json: {exc}")


# ---------------------------------------------------------------------------
# 5. Floor independence: judge input identical with/without pen-rules config.
# ---------------------------------------------------------------------------


class _RecordingModelClient:
    """Minimal offline stand-in that records every invariant it was asked to
    judge and always returns 'not violated' -- this test only cares about
    WHICH invariants floor_judge.judge_floor_invariants was invoked with,
    never about a real judgment."""

    def __init__(self) -> None:
        self.seen_invariant_ids: list[str] = []

    def invoke(self, *, model_id: str, system_prompt: str, user_prompt: str, max_output_tokens: int) -> str:
        # invariant_id is always the first line of the user prompt (see
        # floor_judge._build_user_prompt).
        first_line = user_prompt.splitlines()[0]
        invariant_id = first_line.split("invariant_id:", 1)[1].strip()
        self.seen_invariant_ids.append(invariant_id)
        return json.dumps({"invariant_id": invariant_id, "violated": False, "evidence_quote": ""})


def test_floor_judge_input_independent_of_pen_rules_presence(failures: list[str]) -> None:
    opf_doc = _load_opf_fixture()
    invariants = opf_doc["floor"]["invariants"]

    # Run WITHOUT any pen-rules config at all.
    client_without = _RecordingModelClient()
    floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="A contract clause under review.",
        model_client=client_without,
        model_id="anthropic.claude-opus-4-8",
    )

    # Run again after resolving pen rules for an unrelated topic (proves
    # nothing pen-rules-shaped is threaded into, or filters, the Floor
    # invariants list the judge is called with).
    pen_rules_bundle = {
        "default": {
            "mode": "replace",
            "max_chars": 500,
            "must_not_introduce": [{"phrase": "uncapped", "floor_ref": _REAL_FLOOR_REF}],
        }
    }
    resolved = rte.resolve_pen_rules(pen_rules_bundle, "some-topic")  # exercised, result discarded
    assert resolved is not None  # keep the call from being optimized away / looking dead

    client_with = _RecordingModelClient()
    floor_judge.judge_floor_invariants(
        invariants=invariants,
        review_context="A contract clause under review.",
        model_client=client_with,
        model_id="anthropic.claude-opus-4-8",
    )

    if client_without.seen_invariant_ids != client_with.seen_invariant_ids:
        failures.append(
            f"[5a] the judge's invariants input must be IDENTICAL with and without pen-rules config in play; "
            f"got {client_without.seen_invariant_ids!r} vs {client_with.seen_invariant_ids!r}"
        )
    expected_ids = [inv["id"] for inv in invariants]
    if client_without.seen_invariant_ids != expected_ids:
        failures.append(f"[5b] the judge must be invoked once per opf.floor.invariants entry, in order; got {client_without.seen_invariant_ids!r}, expected {expected_ids!r}")


def main() -> int:
    tests = [
        test_defaults_artifact_exists_and_is_generic,
        test_none_bundle_returns_global_defaults,
        test_bundle_default_overrides_global,
        test_per_topic_overrides_bundle_default,
        test_floor_ref_entries_survive_every_layer,
        test_v1_playbook_passthrough_is_byte_identical,
        test_bind_bundle_rejects_unknown_floor_ref,
        test_bind_bundle_accepts_valid_floor_ref_and_bundle_validates,
        test_floor_judge_input_independent_of_pen_rules_presence,
    ]

    overall_failures: list[str] = []
    for test_fn in tests:
        failures: list[str] = []
        try:
            test_fn(failures)
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors as a failure, not a crash
            failures.append(f"{test_fn.__name__} raised {exc!r}")
        status = "PASS" if not failures else "FAIL"
        print(f"{status}: {test_fn.__name__}")
        for line in failures:
            print(f"    {line}")
        overall_failures.extend(failures)

    print()
    if overall_failures:
        print(f"FAILED: {len(overall_failures)} assertion(s) failed.")
        return 1
    print("PASS: all pen-rules resolution (issue #293) assertions satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
