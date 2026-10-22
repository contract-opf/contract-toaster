#!/usr/bin/env python3
"""OPF acceptance harness — the ONE command that proves the chain end to end.

Reads OPF 0.6 / digest_version 5, the only format this repo accepts (issues #128, #177).

Runs the full acceptance sequence against a bundle (bare ``.opf.json`` or
single-file ``.opf.html``) and a sample contract. Used two ways:

  - NOW, against the synthetic fixture, as the launch's own acceptance gate:

        python3 scripts/opf_acceptance.py \\
          --bundle tests/gold-fixtures-opf/acme-university-0.6.opf.html \\
          --playbook-id acme-university \\
          --policy tests/gold-fixtures-opf/acme-university-policy-v1.json

    The bundle may also be given positionally, and ``--stage NAME`` makes the
    exit code report that one stage (0 only if it PASSED) -- e.g. the digest
    prompt alone:

        python3 scripts/opf_acceptance.py \\
          tests/gold-fixtures-opf/acme-university-0.6.opf.json --stage REVIEW

  - LATER, unchanged, against the operator-provided REAL playbook. The policy
    resolves automatically from the playbook_id, so neither flag is needed:

        python3 scripts/opf_acceptance.py --bundle /path/to/playbook.opf.html \\
          --contract /path/to/sample-contract.docx --comments both

Stages (each prints PASS / FAIL / PENDING):

  1. EXTRACT   — .opf.html → embedded canonical OPF JSON            [PR A]
  2. VERIFY    — schema-valid for its opf_version + content_hash    [PR A]
  3. BIND      — content_hash re-verified; review policy recorded   [PR C]
  4. REVIEW    — digest-5 prompt (<50K tokens), no wholesale dump   [PR D/F, #129, #177]
  5. ROUNDTRIP — reject-all tracked changes == inbound document      [PR G2]
  6. ATTRIBUTE — every w:ins/w:del attributed; zero unattributed     [PR G2]
  7. COMMENTS  — external|internal|both|none; ONE file in every mode [PR G2]
  8. PRESERVE  — inbound margin comments still anchored after redline[PR G1]
  9. SELFCHECK — model self-check transcript present                 [PR F]
 10. FLOOR     — empty must-rules ⇒ no floor block                   [PR F]

A stage that is not yet wired prints PENDING and names the PR that lands it;
PENDING never counts as a pass. Exit: 0 only if every stage is PASS.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import opf_html  # noqa: E402
import opf_load  # noqa: E402

PASS, FAIL, PENDING = "PASS", "FAIL", "PENDING"

STAGE_NAMES = (
    "EXTRACT", "VERIFY", "BIND", "REVIEW", "ROUNDTRIP",
    "ATTRIBUTE", "COMMENTS", "PRESERVE", "SELFCHECK", "FLOOR",
)

#: The review prompt's acceptance budget (chars / 4, the repo-wide estimate).
REVIEW_TOKEN_BUDGET = 50_000

#: The digest's variant, ask and opening texts are summaries of at most this
#: many characters; a precedent text longer than this appearing verbatim in
#: the prompt can only have come from the per-deal record.
_DIGEST_SUMMARY_MAX_CHARS = 300


def _strings(node: object) -> set[str]:
    """Every string value anywhere under *node*."""
    if isinstance(node, str):
        return {node}
    if isinstance(node, dict):
        return set().union(*(_strings(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_strings(v) for v in node)) if node else set()
    return set()


def _our_standard_parts(doc: dict) -> list[str]:
    """Every `our_standard.parts[].text` of every digest clause -- our own
    wording, which the Digest block's "Our standard" line renders by design."""
    digest = doc.get("digest")
    clauses = digest.get("clauses") if isinstance(digest, dict) else None
    texts: list[str] = []
    for clause in clauses or []:
        standard = clause.get("our_standard") if isinstance(clause, dict) else None
        parts = standard.get("parts") if isinstance(standard, dict) else None
        for part in parts or []:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text:
                texts.append(text)
    return texts


def _wholesale_evidence_in(doc: dict, prompt: str) -> int:
    """How many per-deal precedent values (OPF 0.6 `evidence.precedent[]`)
    reached *prompt*: any precedent id, and any signed, opening or refused
    text longer than a digest summary appearing verbatim -- unless the digest
    itself carries that exact text (our standard, a part of it, or a short
    text that is its own summary), which the Digest block renders by design.

    Also exempt: a precedent text contained in any digest `our_standard`
    part, of ANY clause (#177). A deal that refused, or opened with, wording
    that is a verbatim piece of our own standard for a different clause puts
    that wording in the prompt through that clause's "Our standard" line --
    our own text rendered by design, not the per-deal record leaking."""
    digest_texts = _strings(doc.get("digest"))
    standard_parts = _our_standard_parts(doc)
    leaked = 0
    for record in (doc.get("evidence") or {}).get("precedent") or []:
        if not isinstance(record, dict):
            continue
        record_id = record.get("id")
        if isinstance(record_id, str) and record_id and record_id in prompt:
            leaked += 1
        entries = [record.get("signed_text"), record.get("opening_text"), *(record.get("refused_asks") or [])]
        for entry in entries:
            text = entry.get("text") if isinstance(entry, dict) else None
            if (
                isinstance(text, str)
                and len(text) > _DIGEST_SUMMARY_MAX_CHARS
                and text not in digest_texts
                and not any(text in part for part in standard_parts)
                and text in prompt
            ):
                leaked += 1
    return leaked


class Stage:
    def __init__(self, num: int, name: str) -> None:
        self.num = num
        self.name = name
        self.status = PENDING
        self.detail = ""

    def report(self) -> str:
        return f"  {self.num:>2}. {self.name:<10} {self.status:<8} {self.detail}"


def _extract_only(path: Path) -> tuple[dict, str]:
    """Return (opf_doc, how) by parsing the envelope ONLY — no validation.

    Reporting-only sub-step so the EXTRACT stage can say what came out of the
    file. Stage VERIFY is the authoritative one: it runs the real production
    ingest entrypoint (opf_load.load_opf_document).
    """
    text = path.read_text(encoding="utf-8")
    if opf_load.is_html_bundle(path):
        return opf_html.extract_opf_from_html(text), "extracted from .opf.html"
    return json.loads(text), "read as bare .opf.json"


def run(
    bundle_path: Path,
    contract_path: Path | None,
    comments: str,
    playbook_id_arg: str | None = None,
    policy_arg: str | None = None,
    only_stage: str | None = None,
) -> int:
    stages = [Stage(num, name) for num, name in enumerate(STAGE_NAMES, start=1)]
    by_name = {s.name: s for s in stages}

    print(f"OPF acceptance — bundle: {bundle_path}")
    print()

    # --- Stage 1: EXTRACT (reporting only) ----------------------------------
    doc = None
    try:
        doc, how = _extract_only(bundle_path)
        by_name["EXTRACT"].status = PASS
        by_name["EXTRACT"].detail = f"{how}; {len(doc.get('evidence', {}).get('clauses', []))} clauses"
    except Exception as exc:  # noqa: BLE001 — harness reports, never raises
        by_name["EXTRACT"].status = FAIL
        by_name["EXTRACT"].detail = f"{type(exc).__name__}: {exc}"

    # --- Stage 2: VERIFY (the real production ingest path) ------------------
    # Deliberately calls opf_load.load_opf_document rather than re-implementing
    # extract/schema/hash/injection here: a harness that reimplements the logic
    # proves only that the harness works. This runs what an upload runs.
    validated = None
    try:
        validated = opf_load.load_opf_document(bundle_path)
        by_name["VERIFY"].status = PASS
        by_name["VERIFY"].detail = (
            f"opf_version={validated.get('opf_version')}; schema-valid, "
            f"content_hash verified, injection scan clean"
        )
    except Exception as exc:  # noqa: BLE001
        by_name["VERIFY"].status = FAIL
        by_name["VERIFY"].detail = f"{type(exc).__name__}: {exc}"

    # --- Stage 3: BIND ------------------------------------------------------
    # Binds through the real bind_bundle, including its own content_hash
    # re-verification and the review-policy resolution.
    if validated is not None:
        try:
            import bind_bundle
            import policy_load

            playbook_id = playbook_id_arg or (validated.get("agreement_type") or {}).get("id") or ""
            policy_path = (
                Path(policy_arg) if policy_arg
                else policy_load.resolve_latest_policy_path(playbook_id)
            )
            bundle = bind_bundle.bind_bundle(
                validated,
                playbook_id=playbook_id,
                model_policy_path=REPO_ROOT / "model-policy" / "bedrock-us-east-1.json",
                review_policy_path=policy_path,
            )
            rp = bundle.get("review_policy")
            policy_note = (
                f"policy v{rp['version']} ({rp['approval_status']})" if rp else "NO review policy"
            )
            by_name["BIND"].status = PASS
            by_name["BIND"].detail = (
                f"playbook_id={playbook_id}; lineage hash verified; {policy_note}"
            )
        except Exception as exc:  # noqa: BLE001
            by_name["BIND"].status = FAIL
            by_name["BIND"].detail = f"{type(exc).__name__}: {exc}"

    # --- Stage 4: the digest prompt ------------------------------------------
    # The stage's own criterion: the review prompt is built from the digest
    # (digest 5, issues #129/#177), fits the 50K review budget, and carries no
    # wholesale evidence. The per-deal precedent record never reaches the
    # prompt: no precedent id is rendered, and no signed or proposed text
    # longer than the digest's 300-char summary appears verbatim (a shorter
    # one legitimately does -- it IS its own summary).
    if validated is not None:
        try:
            import opf_prompt

            blocks = opf_prompt.compose_opf_system_blocks(validated)
            tokens = sum(len(b) for b in blocks) // 4
            joined = "\n".join(blocks)
            leaked = _wholesale_evidence_in(validated, joined)
            note = f"{len(blocks)} blocks, ~{tokens} tokens"
            if leaked:
                by_name["REVIEW"].status = FAIL
                by_name["REVIEW"].detail = (
                    f"{note}; {leaked} per-deal precedent value(s) LEAKED into the prompt"
                )
            elif tokens >= REVIEW_TOKEN_BUDGET:
                by_name["REVIEW"].status = FAIL
                by_name["REVIEW"].detail = f"{note}; over the 50K review budget"
            elif opf_prompt.DIGEST_INTRO not in joined:
                by_name["REVIEW"].status = FAIL
                by_name["REVIEW"].detail = f"{note}; no Digest block composed"
            else:
                by_name["REVIEW"].status = PASS
                by_name["REVIEW"].detail = (
                    f"digest-{(validated.get('digest') or {}).get('digest_version')} prompt "
                    f"composes: {note}, under the 50K budget, no wholesale evidence"
                )
        except Exception as exc:  # noqa: BLE001
            by_name["REVIEW"].status = FAIL
            by_name["REVIEW"].detail = f"{type(exc).__name__}: {exc}"

    # --- Stage 8: PRESERVE --------------------------------------------------
    # Runs the real preservation gate (tests/redline/test_inbound_comments_preserved.py)
    # rather than re-asserting anything here: that gate drives the actual
    # in-place patcher over every comment run shape Word emits, and checks the
    # ANCHORS in the output document. Deliberately NOT a comments.xml
    # byte-identity check -- byte-identity reported True for a comment that was
    # already orphaned, which is how this stage came to overstate PR #363.
    try:
        import importlib.util

        gate_path = REPO_ROOT / "tests" / "redline" / "test_inbound_comments_preserved.py"
        spec = importlib.util.spec_from_file_location("_preserve_gate", gate_path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)

        failures = []
        for check in (
            gate.check_1_every_inbound_comment_body_survives,
            gate.check_2_comment_anchors_survive,
            gate.check_3_redline_still_applied,
            gate.check_4_no_text_duplicated_by_a_mixed_run,
            gate.check_5_no_comment_range_collapsed,
            gate.check_6_no_orphans_reported,
            gate.check_7_other_parts_untouched,
        ):
            failures.extend(check())

        shapes = sorted({shape for shape, _ in gate.SHAPES.values()})
        if failures:
            by_name["PRESERVE"].status = FAIL
            by_name["PRESERVE"].detail = (
                f"{len(failures)} failure(s); first: {failures[0].strip()}"
            )
        else:
            by_name["PRESERVE"].status = PASS
            by_name["PRESERVE"].detail = (
                f"all inbound comment anchors survive the redline in the output "
                f"document across {len(shapes)} Word run shapes ({', '.join(shapes)}); "
                f"zero orphaned"
            )
    except Exception as exc:  # noqa: BLE001
        by_name["PRESERVE"].status = FAIL
        by_name["PRESERVE"].detail = f"{type(exc).__name__}: {exc}"

    # --- Stages 5-10: wired by later PRs ------------------------------------
    pending_owner = {
        "ROUNDTRIP": "PR G2 (redline round-trip)",
        "ATTRIBUTE": "PR G2 (attribution manifest)",
        "COMMENTS": "PR G2 (--comments switch)",
        "SELFCHECK": "PR F (model self-check transcript)",
        "FLOOR": "PR F (empty must-rules ⇒ no floor block)",
    }
    for name, owner in pending_owner.items():
        stage = by_name[name]
        if stage.status == PENDING and not stage.detail:
            stage.detail = f"not yet wired — {owner}"

    print("Stages:")
    for s in stages:
        print(s.report())
    print()

    failed = [s for s in stages if s.status == FAIL]
    pending = [s for s in stages if s.status == PENDING]
    passed = [s for s in stages if s.status == PASS]

    print(f"Summary: {len(passed)} passed, {len(failed)} failed, {len(pending)} pending")
    if only_stage is not None:
        stage = by_name[only_stage]
        print(f"STAGE {only_stage}: {stage.status}")
        return 0 if stage.status == PASS else 1
    if failed:
        print("ACCEPTANCE: FAILED — " + ", ".join(s.name for s in failed))
        return 1
    if pending:
        print("ACCEPTANCE: INCOMPLETE — stages still pending: " + ", ".join(s.name for s in pending))
        return 2
    print("ACCEPTANCE: PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OPF end-to-end acceptance harness")
    ap.add_argument("bundle_pos", nargs="?", type=Path, default=None, metavar="BUNDLE",
                    help="Path to the playbook bundle (.opf.html or .opf.json); same as --bundle")
    ap.add_argument("--bundle", type=Path, default=None,
                    help="Path to the playbook bundle (.opf.html or .opf.json)")
    ap.add_argument("--stage", default=None, choices=STAGE_NAMES,
                    help="Exit 0 only if this one stage PASSED (the full report still prints)")
    ap.add_argument("--contract", type=Path, default=None,
                    help="Path to a sample contract .docx to review end to end")
    ap.add_argument("--playbook-id", default=None,
                    help="Registry key to bind as (default: the OPF's own agreement_type.id). "
                         "Must be one of the OPF's id/aliases.")
    ap.add_argument("--policy", default=None,
                    help="Path to the review policy document (default: the highest-versioned "
                         "playbooks/<playbook_id>-policy-v<N>.json).")
    ap.add_argument("--comments", default="none",
                    choices=["external", "internal", "both", "none"],
                    help="Comment mode to exercise (default: none)")
    args = ap.parse_args()

    if (args.bundle is None) == (args.bundle_pos is None):
        ap.error("give the bundle exactly once, positionally or with --bundle")
    bundle = args.bundle or args.bundle_pos
    if not bundle.exists():
        print(f"ERROR: bundle not found: {bundle}", file=sys.stderr)
        return 1
    return run(bundle, args.contract, args.comments, args.playbook_id, args.policy, args.stage)


if __name__ == "__main__":
    sys.exit(main())
