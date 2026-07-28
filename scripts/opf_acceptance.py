#!/usr/bin/env python3
"""OPF 0.3 acceptance harness — the ONE command that proves the chain end to end.

Runs the full acceptance sequence against a bundle (bare ``.opf.json`` or
single-file ``.opf.html``) and a sample contract. Used two ways:

  - NOW, against the synthetic fixture, as the launch's own acceptance gate:

        python3 scripts/opf_acceptance.py \\
          --bundle tests/gold-fixtures-opf/acme-university.opf.html \\
          --playbook-id acme-university \\
          --policy tests/gold-fixtures-opf/acme-university-policy-v1.json

  - LATER, unchanged, against the operator-provided REAL playbook. The policy
    resolves automatically from the playbook_id, so neither flag is needed:

        python3 scripts/opf_acceptance.py --bundle /path/to/playbook.opf.html \\
          --contract /path/to/sample-contract.docx --comments both

Stages (each prints PASS / FAIL / PENDING):

  1. EXTRACT   — .opf.html → embedded canonical OPF JSON            [PR A]
  2. VERIFY    — schema-valid for its opf_version + content_hash    [PR A]
  3. BIND      — content_hash re-verified; review policy recorded   [PR C]
  4. REVIEW    — digest-based prompt (<50K tokens), no wholesale dump[PR D/F]
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
) -> int:
    stages = [
        Stage(1, "EXTRACT"), Stage(2, "VERIFY"), Stage(3, "BIND"),
        Stage(4, "REVIEW"), Stage(5, "ROUNDTRIP"), Stage(6, "ATTRIBUTE"),
        Stage(7, "COMMENTS"), Stage(8, "PRESERVE"), Stage(9, "SELFCHECK"),
        Stage(10, "FLOOR"),
    ]
    by_name = {s.name: s for s in stages}

    print(f"OPF 0.3 acceptance — bundle: {bundle_path}")
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

    # --- Stage 4 (part): digest-based prompt composition ---------------------
    # PR D proves the prompt is digest-based and within budget. PR F wires it
    # into the passes and adds the policy/self-check; until then this stage
    # reports composition only and stays PENDING.
    if validated is not None:
        try:
            import opf_prompt

            blocks = opf_prompt.compose_opf_system_blocks(validated)
            tokens = sum(len(b) for b in blocks) // 4
            joined = "\n".join(blocks)
            leaked = [
                o.get("full_text")
                for c in (validated.get("evidence") or {}).get("clauses") or []
                for o in c.get("observed_positions") or []
                if o.get("full_text") and o["full_text"] in joined
            ]
            note = f"{len(blocks)} blocks, ~{tokens} tokens"
            if leaked:
                by_name["REVIEW"].status = FAIL
                by_name["REVIEW"].detail = f"{note}; evidence full_text LEAKED into the prompt"
            elif tokens >= 50_000:
                by_name["REVIEW"].status = FAIL
                by_name["REVIEW"].detail = f"{note}; over the 50K review budget"
            else:
                by_name["REVIEW"].detail = (
                    f"prompt composes: {note}, no wholesale evidence — "
                    f"pass wiring pending PR F (model-first spine)"
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
        "REVIEW": "PR F (wire digest prompt into the passes + policy + self-check)",
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
    if failed:
        print("ACCEPTANCE: FAILED — " + ", ".join(s.name for s in failed))
        return 1
    if pending:
        print("ACCEPTANCE: INCOMPLETE — stages still pending: " + ", ".join(s.name for s in pending))
        return 2
    print("ACCEPTANCE: PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OPF 0.3 end-to-end acceptance harness")
    ap.add_argument("--bundle", required=True, type=Path,
                    help="Path to the playbook bundle (.opf.html or .opf.json)")
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

    if not args.bundle.exists():
        print(f"ERROR: bundle not found: {args.bundle}", file=sys.stderr)
        return 1
    return run(args.bundle, args.contract, args.comments, args.playbook_id, args.policy)


if __name__ == "__main__":
    sys.exit(main())
