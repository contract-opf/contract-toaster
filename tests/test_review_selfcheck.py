#!/usr/bin/env python3
"""
Red gate: the closing self-check re-reads the FINISHED REDLINE (PR F1).

## What this step is for

Every rule in the Binding block ends with a promise: "Every entry here is
re-read against your finished redline by the closing self-check"
(`opf_prompt.BINDING_INTRO`). Nothing kept that promise -- the model was told
the rules bind, and the only thing that ever checked whether the output
honoured them was the model that wrote it. The self-check is the re-read:
per `must` rule, one verdict recorded against the finished redline, and a
tension FLAGGED rather than resolved (a policy `must` in tension with the
facts is an attorney's determination in either direction --
`playbooks/policy.schema.json`, and `tests/test_policy_document.py`).

## Why the coverage gate is the load-bearing part

A self-check that quietly judges 6 of 7 rules is worse than none: it
produces a transcript that looks complete, ships with the attribution
manifest, and attests to a re-read that did not happen for the one rule that
mattered. So an UNJUDGED rule is fail-closed -> MANUAL_REVIEW_REQUIRED
(docs/output-contract.md: "The decision is binary; uncertainty is a system
status"), exactly as `scripts/floor_judge.py` treats an unjudged invariant.
For the same reason, ZERO `must` rules must produce an EMPTY transcript --
"nothing to check" and "checked, all clear" are different facts, and only one
of them is true.

## The replacement-text bound checks

`scripts/replacement_text_enforcement.py` and
`playbooks/pen-rules.defaults.json` (its only consumer) are deleted by PR F2.
Those defaults are toaster-GLOBAL bind config (issue #293) forbidding
"unlimited liability", "irrevocable" and "perpetual" in replacement text for
every playbook -- and of the three, "irrevocable" is covered by no `must`
rule in any new policy. After F2 this self-check is the only thing standing
between a model's replacement text and a contract, so it judges each
introduced text against each bound too -- as a MODEL judgment, sourced from
the bound artifact (never hardcoded here, and never a regex: a lexical guard
is exactly what this migration exists to replace).

Run standalone: `python3 tests/test_review_selfcheck.py`
Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"

for _dir in (SCRIPTS_DIR, BACKEND_SRC_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import review_selfcheck  # noqa: E402

_MODEL_ID = "anthropic.claude-opus-4-8"

# Distinctive, greppable substance. If any of these strings reaches a log line
# or an exception message, the check_5 assertions below fail -- these stand in
# for real policy prose and real contract language, both confidential.
_RULE_TEXT_A = "Do not accept uncapped or unlimited liability, or an indemnity covering their own negligence."
_RULE_TEXT_B = "Do not accept removal of the standard confidentiality exceptions."
_INTRODUCED_TEXT = (
    "Licensee hereby grants an irrevocable license to the deliverables, "
    "surviving termination of this Agreement."
)


def _must_rules() -> list[dict[str, Any]]:
    return [
        {"id": "floor.no-uncapped-liability", "strength": "must", "text": _RULE_TEXT_A},
        {"id": "confidentiality.preserve-standard-exceptions", "strength": "must", "text": _RULE_TEXT_B},
    ]


def _reconciled(replacement_text: str = _INTRODUCED_TEXT) -> dict[str, Any]:
    return {
        "schema_version": "output-schema-v1",
        "decision": "REQUEST_CHANGE",
        "issues": [
            {
                "section_ref": "sec-8",
                "section_title": "Limitation on Liability",
                "counterparty_change_summary": "Counterparty removed the liability cap.",
                "decision": "REQUEST_CHANGE",
                "external_rationale_for_footnote": "This section must retain the standard cap.",
                "proposed_replacement_text": replacement_text,
                "playbook_topic_id": "limitation-of-liability",
                "internal_precedent_citation": None,
                "provenance": "model",
            }
        ],
        "verdict_summary": "One issue requires attention.",
    }


def _bounds() -> list[dict[str, Any]]:
    """The shape `pen-rules.defaults.json` -> `default.must_not_introduce`
    already ships, which is where these values live today and what the bind
    config must carry after F2 deletes that file."""
    return [{"phrase": "irrevocable"}, {"phrase": "perpetual"}]


class _ScriptedSelfCheckClient:
    """A model that actually answers the question it was asked.

    Reads the `check_id` back out of the user prompt and replies for THAT
    check, rather than popping a canned response off a queue in a fixed order
    (`FakeBedrockClient`'s shape). That is deliberate: the properties under
    test here are "every rule got exactly one verdict" and "the verdict landed
    on the right rule", and a queue-ordered double proves neither -- it would
    answer whatever was asked, in order, and a transcript that dropped a rule
    or crossed two verdicts would still look right.

    `verdicts` maps check_id -> verdict (default "compliant"); `raw_overrides`
    maps check_id -> a raw response body, for the invalid-response paths.
    """

    def __init__(
        self,
        verdicts: dict[str, str] | None = None,
        raw_overrides: dict[str, str] | None = None,
    ):
        self.verdicts = verdicts or {}
        self.raw_overrides = raw_overrides or {}
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _check_id_of(user_prompt: str) -> str:
        for line in user_prompt.splitlines():
            if line.startswith("check_id: "):
                return line[len("check_id: ") :].strip()
        raise AssertionError("the self-check user prompt carries no `check_id: ` line")

    def invoke(self, *, model_id: str, system_prompt: str, user_prompt: str, max_output_tokens: int) -> str:
        check_id = self._check_id_of(user_prompt)
        self.calls.append(
            {
                "model_id": model_id,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "check_id": check_id,
            }
        )
        if check_id in self.raw_overrides:
            return self.raw_overrides[check_id]
        verdict = self.verdicts.get(check_id, review_selfcheck.VERDICT_COMPLIANT)
        return json.dumps(
            {
                "check_id": check_id,
                "verdict": verdict,
                "tension_note": "" if verdict == review_selfcheck.VERDICT_COMPLIANT else "note",
            }
        )


def _run(client, must_rules=None, replacement_bounds=None, reconciled=None):
    return review_selfcheck.run_self_check(
        reconciled_result=reconciled if reconciled is not None else _reconciled(),
        must_rules=_must_rules() if must_rules is None else must_rules,
        replacement_bounds=_bounds() if replacement_bounds is None else replacement_bounds,
        model_client=client,
        model_id=_MODEL_ID,
    )


# ---------------------------------------------------------------------------


def check_1_every_must_rule_gets_exactly_one_verdict() -> list[str]:
    failures = []
    client = _ScriptedSelfCheckClient()
    transcript = _run(client)

    rule_entries = [e for e in transcript.entries if e["kind"] == review_selfcheck.KIND_MUST_RULE]
    subjects = [e["subject_id"] for e in rule_entries]
    for rule in _must_rules():
        count = subjects.count(rule["id"])
        if count != 1:
            failures.append(
                f"  [1] must rule {rule['id']!r} has {count} verdict(s) in the transcript, "
                f"expected exactly 1. Every binding rule is re-read against the finished "
                f"redline exactly once -- zero is an unattested rule, more than one is two "
                f"answers to the same question."
            )
    for entry in rule_entries:
        if entry.get("verdict") not in review_selfcheck.VERDICTS:
            failures.append(
                f"  [1] entry for {entry['subject_id']!r} carries verdict "
                f"{entry.get('verdict')!r}, which is not one of {sorted(review_selfcheck.VERDICTS)}"
            )
    if transcript.fail_closed:
        failures.append(f"  [1] every rule was judged, yet fail_closed is True: {transcript.unjudged}")

    # The rule's TEXT must actually reach the model -- otherwise "judged" is a
    # verdict on nothing. (Asserted on the prompt, not on the transcript: the
    # transcript is the one place the text must NOT be.)
    for rule in _must_rules():
        asked = [c for c in client.calls if rule["text"] in c["user_prompt"]]
        if len(asked) != 1:
            failures.append(
                f"  [1] rule {rule['id']!r}'s text reached {len(asked)} self-check prompt(s), "
                f"expected exactly 1"
            )
    return failures


def check_2_unjudged_rule_fails_closed_to_manual_review() -> list[str]:
    failures = []
    # One rule's judge is broken (non-JSON, twice: the initial call plus the
    # one bounded retry). Its verdict is not "no violation" -- it is absent.
    client = _ScriptedSelfCheckClient(
        raw_overrides={"must:floor.no-uncapped-liability": "I'm sorry, I can't help with that."}
    )
    transcript = _run(client)

    subjects = [e["subject_id"] for e in transcript.entries]
    if "floor.no-uncapped-liability" in subjects:
        failures.append(
            "  [2] an unjudged rule appears in the transcript with a verdict: an invalid "
            "response must never be read as compliance"
        )
    if transcript.unjudged != ["must:floor.no-uncapped-liability"]:
        failures.append(
            f"  [2] expected exactly the unjudgeable rule's check_id in `unjudged`, got "
            f"{transcript.unjudged!r}"
        )
    if not transcript.fail_closed:
        failures.append("  [2] a transcript with an unjudged rule must be fail_closed")
    status = review_selfcheck.terminal_status_for(transcript)
    if status != review_selfcheck.STATUS_MANUAL_REVIEW_REQUIRED:
        failures.append(
            f"  [2] a fail-closed transcript must map to "
            f"{review_selfcheck.STATUS_MANUAL_REVIEW_REQUIRED!r}, got {status!r}. An unjudged "
            f"binding rule is a system status, never a silent pass and never a legal decision."
        )

    # Exactly one bounded retry for the failing rule, then it fails closed --
    # never an unbounded grind against a model that cannot answer.
    attempts = [c for c in client.calls if c["check_id"] == "must:floor.no-uncapped-liability"]
    if len(attempts) != 2:
        failures.append(
            f"  [2] expected 1 initial call + 1 bounded retry for the failing rule; got "
            f"{len(attempts)}"
        )

    # The other rule is still judged: one broken judge does not swallow the run.
    if "confidentiality.preserve-standard-exceptions" not in subjects:
        failures.append("  [2] the second rule lost its verdict when the first rule's judge failed")

    # A clean run must NOT be fail-closed -- otherwise [2] passes trivially.
    if review_selfcheck.terminal_status_for(_run(_ScriptedSelfCheckClient())) is not None:
        failures.append("  [2] a fully judged transcript must carry no terminal status")
    return failures


def check_3_zero_must_rules_is_an_empty_transcript_not_a_pass() -> list[str]:
    failures = []
    client = _ScriptedSelfCheckClient()
    transcript = _run(client, must_rules=[], replacement_bounds=[])

    if transcript.entries:
        failures.append(
            f"  [3] zero must rules and zero bounds produced {len(transcript.entries)} "
            f"transcript entr(ies): {transcript.entries!r}. There was nothing to re-read; a "
            f"verdict here is fabricated."
        )
    if transcript.fail_closed:
        failures.append("  [3] an empty transcript is not a coverage failure -- nothing was owed")
    if client.calls:
        failures.append(
            f"  [3] the self-check invoked the model {len(client.calls)} time(s) with nothing to "
            f"check -- spend, and a chance to invent a verdict"
        )
    if transcript.record()["checks_run"] != 0:
        failures.append(
            f"  [3] the record must state that zero checks ran, positively; got "
            f"{transcript.record()!r}"
        )
    return failures


def check_4_introduced_bound_is_flagged() -> list[str]:
    failures = []
    bound_check_id = "bound:0@sec-8"
    client = _ScriptedSelfCheckClient(
        verdicts={bound_check_id: review_selfcheck.VERDICT_TENSION_FLAGGED}
    )
    transcript = _run(client)

    # The bound was ASKED about, against the text that becomes <w:ins>.
    asked = [c for c in client.calls if c["check_id"] == bound_check_id]
    if len(asked) != 1:
        failures.append(
            f"  [4] expected exactly 1 judgment of bound 0 against the sec-8 introduction; got "
            f"{len(asked)}. After F2 this is the ONLY guard on what text reaches a contract."
        )
        return failures
    prompt = asked[0]["user_prompt"]
    if "irrevocable" not in prompt:
        failures.append("  [4] the bound's own phrase never reached the judge's prompt")
    if _INTRODUCED_TEXT not in prompt:
        failures.append(
            "  [4] the introduced replacement text never reached the judge's prompt: the judge "
            "was asked about a bound in the abstract"
        )

    flagged = transcript.flagged()
    bound_flags = [e for e in flagged if e["kind"] == review_selfcheck.KIND_REPLACEMENT_BOUND]
    if len(bound_flags) != 1:
        failures.append(
            f"  [4] expected exactly 1 flagged replacement-bound entry, got {bound_flags!r}"
        )
        return failures
    if bound_flags[0]["section_ref"] != "sec-8":
        failures.append(
            f"  [4] the flag must name the introduction it came from; got "
            f"{bound_flags[0]['section_ref']!r}"
        )

    # Both bounds x one introduction = 2 bound judgments; the un-flagged one is
    # recorded compliant, not omitted.
    bound_entries = [
        e for e in transcript.entries if e["kind"] == review_selfcheck.KIND_REPLACEMENT_BOUND
    ]
    if len(bound_entries) != 2:
        failures.append(
            f"  [4] expected one verdict per (bound, introduction) pair = 2; got "
            f"{len(bound_entries)}"
        )
    return failures


def check_5_the_bound_check_is_a_judgment_not_a_lexical_match() -> list[str]:
    """No regex, no phrase search: the model's verdict is the verdict.

    A redline that introduces the bound in SUBSTANCE while never using its
    word must still flag -- and a redline that uses the word must not flag on
    that basis alone. Any lexical pre-filter or post-filter in the module
    would break one of these two directions.
    """
    failures = []
    substance_only = (
        "The license granted under this Section may not be revoked, withdrawn, or terminated "
        "by Licensor for any reason."
    )
    client = _ScriptedSelfCheckClient(
        verdicts={"bound:0@sec-8": review_selfcheck.VERDICT_TENSION_FLAGGED}
    )
    transcript = _run(client, reconciled=_reconciled(substance_only))
    flagged = [e for e in transcript.flagged() if e["kind"] == review_selfcheck.KIND_REPLACEMENT_BOUND]
    if len(flagged) != 1:
        failures.append(
            f"  [5] the judge flagged a bound introduced in substance but not by name, and the "
            f"transcript dropped it ({flagged!r}) -- a lexical gate is suppressing the model's "
            f"judgment, which is the whole thing this migration removes"
        )

    # The other direction: the word is present, the judge says compliant, and
    # the transcript must NOT flag it anyway.
    clean_client = _ScriptedSelfCheckClient()  # every verdict compliant
    clean = _run(clean_client, reconciled=_reconciled(_INTRODUCED_TEXT))
    if [e for e in clean.flagged() if e["kind"] == review_selfcheck.KIND_REPLACEMENT_BOUND]:
        failures.append(
            "  [5] the judge found no tension, yet a bound was flagged anyway: something is "
            "matching the phrase lexically behind the judge's back"
        )
    return failures


def check_6_no_rule_or_document_substance_in_logs_or_exceptions() -> list[str]:
    """Same discipline as `scripts/floor_judge.py`: only ids ever surface.

    Rule prose is confidential playbook substance and introduced text is
    contract substance. A transcript ships with the attribution manifest; a
    log line ships wherever logs ship.
    """
    failures = []
    secrets = {
        "rule text": _RULE_TEXT_A,
        "the other rule's text": _RULE_TEXT_B,
        "introduced contract text": _INTRODUCED_TEXT,
    }

    # 1. A run that fails every judge -- the noisiest path there is.
    client = _ScriptedSelfCheckClient(
        raw_overrides={
            "must:floor.no-uncapped-liability": "nonsense",
            "must:confidentiality.preserve-standard-exceptions": "nonsense",
            "bound:0@sec-8": "nonsense",
            "bound:1@sec-8": "nonsense",
        }
    )
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        transcript = _run(client)
    logged = out.getvalue() + err.getvalue()
    for name, secret in secrets.items():
        if secret in logged:
            failures.append(f"  [6] the self-check logged {name} verbatim")
    for name, secret in secrets.items():
        if secret in json.dumps(transcript.record()):
            failures.append(
                f"  [6] the transcript record carries {name} verbatim -- it ships with the "
                f"attribution manifest"
            )
    if not transcript.unjudged:
        failures.append("  [6] the all-judges-broken run recorded no unjudged checks at all")

    # 2. The config-error path: a must rule with no id cannot be recorded
    # against anything, so it raises -- and the message says which rule by
    # position, never by quoting it.
    try:
        _run(_ScriptedSelfCheckClient(), must_rules=[{"strength": "must", "text": _RULE_TEXT_A}])
        failures.append(
            "  [6] an id-less must rule was accepted: its verdict cannot be attributed to "
            "anything, so the transcript would attest to a re-read of an unidentifiable rule"
        )
    except review_selfcheck.SelfCheckConfigError as exc:
        if _RULE_TEXT_A in str(exc):
            failures.append("  [6] the config error quotes the rule text back in its message")
    return failures


def main() -> int:
    checks = [
        ("1", "every must rule gets exactly one verdict", check_1_every_must_rule_gets_exactly_one_verdict),
        ("2", "an unjudged rule fails closed to MANUAL_REVIEW_REQUIRED", check_2_unjudged_rule_fails_closed_to_manual_review),
        ("3", "zero must rules is an empty transcript, not a pass", check_3_zero_must_rules_is_an_empty_transcript_not_a_pass),
        ("4", "an introduced replacement-text bound is flagged", check_4_introduced_bound_is_flagged),
        ("5", "the bound check is a judgment, not a lexical match", check_5_the_bound_check_is_a_judgment_not_a_lexical_match),
        ("6", "no rule/document substance in logs or exceptions", check_6_no_rule_or_document_substance_in_logs_or_exceptions),
    ]

    overall_pass = True
    for code, name, fn in checks:
        try:
            failures = fn()
        except Exception as exc:  # noqa: BLE001
            failures = [f"  [{code}] raised {type(exc).__name__}: {exc}"]
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            overall_pass = False

    print()
    if overall_pass:
        print("All review self-check assertions passed.")
        return 0
    print("One or more review self-check assertions FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
