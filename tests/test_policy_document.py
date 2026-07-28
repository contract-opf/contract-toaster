#!/usr/bin/env python3
"""Generic harness for the review policy document idiom (work item 9a of the
OPF 0.3 launch), plus this repo's own loader-mechanics self-test.

The policy document is the ONE home for prescriptive human input into a review.
It is legal content, so the idiom below guards the properties that matter for
ANY policy:

 1. SCHEMA: policy.schema.json is a usable schema and a given policy validates
    against it.
 2. LOADER invariants the schema can't express: duplicate rule ids, filename/
    version disagreement, and an `approved` stamp with no approver are all
    rejected.
 3. GENERIC: resolution is by playbook_id from the <playbook_id>-policy-v<N>.json
    convention and works for an arbitrary id. resolve_latest_policy_path picks
    the HIGHEST version.
 4. HARVEST COVERAGE, BOTH DIRECTIONS (`harvest_coverage_failures`, used when a
    policy was migrated out of an earlier v1-playbook artifact): nothing in the
    source's governance layer dies in the migration, and nothing is invented on
    the way across.
 5. DEBRANDED: no tenant-name literal survives.
 6. NOT FALSELY APPROVED: a harvest rewords the source's rules, so the source's
    sign-off does not carry over automatically -- a policy ships `draft` until
    a human approves it, and this checks we did not stamp an approval nobody
    gave.
 7. HASHING: policy_content_hash is deterministic and DOES cover `approval`, so
    re-stamping an approval forces a re-bind.
 8. PROVENANCE RESOLVES (for a harvested policy): approval.harvested_from's
    content_hash always resolves against the source file on disk, and, when a
    git blob/commit are also recorded, the hash actually hashes THAT blob,
    which the commit's tree actually resolves the source path to. The git
    anchor is present only when the source was already a landed commit at
    harvest time; it is never fabricated to make the check pass.

## Eviction note (issue #413), self-test target repointed (issue #412)

This file used to be BOTH the generic harness above AND the concrete spec for
the real eiaa policy document -- a real, tenant-derived governance harvest
(real names, real dollar figures, a real attorney's determinations) that has no
business shipping in the brand-free empty-shell engine. Issue #413 deleted that
document and the EIAA-specific spec that lived here (its EIAA_SPEC, COVERAGE
table, and check_4/4b/4c). The GENERIC HALF below is unchanged and is still what
`tests/test_nda_policy.py` imports as `test_policy_document` to gate its own
(synthetic) harvest -- see that file for the live COVERAGE-table instantiation
of the idiom. This file's own `main()` below only self-tests the loader
mechanics (checks 1-3) and runs the generic content checks (5-8) against an
already-synthetic, already-committed policy -- previously
`sample-agreement-policy-v1.json` (deleted by issue #412, "sample-agreement"
is not the shipped sample), now `nda-policy-v1.json` (the same harvest
`tests/test_nda_policy.py`'s own COVERAGE table gates), so it carries no
policy content of its own beyond what that file already specs.

## Why `harvest_coverage_failures` is DISPOSITION-shaped, not count-shaped

The first version of the EIAA gate this idiom grew out of pinned three raw
counts and went green over a harvest that had silently dropped three OTHER
prescriptive constructs entirely (all of `acceptable_variations`, several
`must_preserve` items, and all `de_minimis_categories`, while a surviving rule
still conditioned on a term whose definition had left with them). The check
encoded the same blind spot as the harvest it was guarding: it could only see
the constructs it had been told to look at.

So it is not count-shaped. It is DISPOSITION-shaped:

  - Every top-level key of the source, and every key inside a topic, must have a
    recorded disposition -- HARVESTED (mapped item by item) or NOT_HARVESTED
    (with a reason). A construct the map has never heard of is a FAILURE, not a
    silence. That is the property the count check lacked: a new or overlooked
    construct cannot pass by not being asked about.
  - v1 -> policy: every ITEM of every HARVESTED construct is either mapped to at
    least one policy rule that exists, or explicitly dispositioned in
    NOT_HARVESTED_ITEMS with a reason. Unlisted => FAIL. Nothing is dropped.
  - policy -> v1: every policy rule id is the image of at least one source item.
    Nothing is invented.

COVERAGE is keyed by (construct, topic, index) rather than by source text, so a
per-playbook spec file stays free of tenant-name literals. Source TEXT is
pinned instead by `provenance_resolves_failures`: the whole source is hashed,
so any edit to any item breaks provenance and demands a re-harvest. The two
checks are complementary -- coverage pins structure, provenance pins bytes.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import policy_load  # noqa: E402

PLAYBOOKS_DIR = REPO_ROOT / "playbooks"

# This file's own self-test target: an already-synthetic, already-committed
# policy (never a real tenant harvest), used purely to exercise the generic
# loader mechanics and the generic content checks below. It is NOT this
# policy's spec -- that lives in tests/test_nda_policy.py, which also
# imports this module for the same generic functions (issue #412: this used
# to point at the now-deleted sample-agreement-policy-v1.json instead).
POLICY_PATH = PLAYBOOKS_DIR / "nda-policy-v1.json"
SOURCE_PATH = PLAYBOOKS_DIR / "nda-v0.1.0.json"


# --------------------------------------------------------------------------
# GENERIC HALF: playbook-agnostic. Imported by the per-playbook harvest gate
# (tests/test_nda_policy.py) so that one implementation of "nothing died in
# the migration" guards every harvest.
# --------------------------------------------------------------------------

#: A COVERAGE/NOT_HARVESTED_ITEMS key: (construct, topic_id, index-or-id).
ItemKey = tuple[str, str, object]


@dataclass(frozen=True)
class HarvestSpec:
    """One playbook's harvest: the policy, its source, and every disposition.

    The dispositions are data, not code, precisely so that a reviewer can read
    what the harvest claims it did and the checks can hold it to that claim.
    """

    playbook_id: str
    policy_path: Path
    source_path: Path
    #: Constructs harvested item by item; every item must appear in `coverage`
    #: or `not_harvested_items`.
    harvested_top_level: tuple[str, ...] = ()
    harvested_topic_keys: tuple[str, ...] = ()
    #: Constructs carrying no prescriptive content, each -> the reason.
    not_harvested_top_level: dict[str, str] = field(default_factory=dict)
    not_harvested_topic_keys: dict[str, str] = field(default_factory=dict)
    #: source item -> the policy rule(s) carrying it.
    coverage: dict[ItemKey, tuple[str, ...]] = field(default_factory=dict)
    #: source item -> the reason it must NOT become a rule.
    not_harvested_items: dict[ItemKey, str] = field(default_factory=dict)
    expected_source_item_counts: dict[str, int] = field(default_factory=dict)
    expected_total_rules: int = 0

    def policy(self) -> dict:
        return policy_load.load_policy(self.policy_path)


# This file's own self-test spec: no COVERAGE table of its own (this file
# carries no harvest content), just enough to drive the generic content checks
# (schema / debranded / not-falsely-approved / hashing / provenance) against
# the already-committed nda policy.
SELF_TEST_SPEC = HarvestSpec(
    playbook_id="nda",
    policy_path=POLICY_PATH,
    source_path=SOURCE_PATH,
)


def _policy() -> dict:
    return policy_load.load_policy(POLICY_PATH)


def schema_failures(spec: HarvestSpec) -> list[str]:
    """policy.schema.json is a usable schema and this policy validates."""
    import jsonschema

    failures: list[str] = []
    schema = json.loads((PLAYBOOKS_DIR / "policy.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  policy.schema.json is not a valid draft-07 schema: {exc}")
    try:
        spec.policy()
    except policy_load.PolicyValidationError as exc:
        failures.append(f"  committed policy failed to load: {exc}")
    return failures


def check_1_schema() -> list[str]:
    return schema_failures(SELF_TEST_SPEC)


def _expect_raises(fn, label: str) -> list[str]:
    try:
        fn()
    except policy_load.PolicyValidationError:
        return []
    except Exception as exc:  # noqa: BLE001
        return [f"  {label}: raised {type(exc).__name__}, expected PolicyValidationError"]
    return [f"  {label}: did NOT raise PolicyValidationError"]


def check_2_loader_invariants() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        def write(doc: dict, name: str) -> Path:
            p = tmp / name
            p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return p

        # Duplicate rule ids -> attribution would be ambiguous.
        doc = _policy()
        doc["rules"].append(copy.deepcopy(doc["rules"][0]))
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "duplicate rule ids",
        )

        # version disagrees with the filename.
        doc = _policy()
        doc["version"] = 7
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "version/filename mismatch",
        )

        # approved with no approver -> an unsigned approval stamp.
        doc = _policy()
        doc["approval"]["status"] = "approved"
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "approved without approved_by/approved_at",
        )

        # unknown strength -> schema enum.
        doc = _policy()
        doc["rules"][0]["strength"] = "maybe"
        failures += _expect_raises(
            lambda: policy_load.load_policy(write(doc, "nda-policy-v1.json")),
            "invalid strength",
        )
    return failures


def check_3_generic_resolution() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # An arbitrary playbook id must work identically -- nothing here is
        # special-cased to any one playbook.
        for version in (1, 2, 10):
            doc = _policy()
            doc["playbook_id"] = "widget-msa"
            doc["version"] = version
            (tmp / f"widget-msa-policy-v{version}.json").write_text(
                json.dumps(doc, indent=2), encoding="utf-8"
            )
        versions = policy_load.list_policy_versions("widget-msa", tmp)
        if versions != [1, 2, 10]:
            failures.append(f"  list_policy_versions returned {versions}, expected [1, 2, 10]")
        latest = policy_load.resolve_latest_policy_path("widget-msa", tmp)
        if latest is None or latest.name != "widget-msa-policy-v10.json":
            failures.append(f"  resolve_latest_policy_path picked {latest}, expected v10 (highest)")
        else:
            loaded = policy_load.load_policy(latest)
            if loaded["playbook_id"] != "widget-msa" or loaded["version"] != 10:
                failures.append("  latest policy did not load as widget-msa v10")
        # A playbook with no policy is a legitimate state, not an error.
        if policy_load.resolve_latest_policy_path("no-such-playbook", tmp) is not None:
            failures.append("  resolve_latest_policy_path invented a policy for an unknown id")
    return failures


def enumerate_source_items(v1: dict, spec: HarvestSpec) -> tuple[list[ItemKey], list[str]]:
    """Every prescriptive item in the source, as COVERAGE keys.

    Also returns failures for any construct whose disposition is unrecorded.
    A construct nobody decided about is the failure mode this whole check
    exists for, so it is an error and never a silent skip.
    """
    keys: list[ItemKey] = []
    failures: list[str] = []

    unknown_top = set(v1) - set(spec.harvested_top_level) - set(spec.not_harvested_top_level)
    for name in sorted(unknown_top):
        failures.append(
            f"  source has top-level construct {name!r} with NO recorded disposition. "
            f"Decide it: add it to HARVESTED_TOP_LEVEL and map its items in COVERAGE, "
            f"or to NOT_HARVESTED_TOP_LEVEL with the reason it carries no position."
        )

    for construct in ("general_principles", "de_minimis_categories"):
        if construct not in spec.harvested_top_level:
            continue
        for i in range(len(v1.get(construct) or [])):
            keys.append((construct, "", i))
    if "hard_rejections" in spec.harvested_top_level:
        for hr in v1.get("hard_rejections") or []:
            keys.append(("hard_rejections", "", hr["id"]))

    for topic in v1.get("topics") or []:
        tid = topic["id"]
        unknown_keys = set(topic) - set(spec.harvested_topic_keys) - set(spec.not_harvested_topic_keys)
        for name in sorted(unknown_keys):
            failures.append(
                f"  source topic {tid!r} has key {name!r} with NO recorded disposition. "
                f"Decide it: add it to HARVESTED_TOPIC_KEYS and map its items in COVERAGE, "
                f"or to NOT_HARVESTED_TOPIC_KEYS with the reason it carries no position."
            )
        for construct in spec.harvested_topic_keys:
            for i in range(len(topic.get(construct) or [])):
                keys.append((construct, tid, i))

    return keys, failures


def source_item_text(v1: dict, key: ItemKey) -> str:
    """The source item behind a key, for failure messages -- so a human reading
    a failure sees WHAT went unmapped, not just a coordinate."""
    construct, tid, idx = key
    if construct == "hard_rejections":
        for hr in v1["hard_rejections"]:
            if hr["id"] == idx:
                return f"{hr['id']}: {hr.get('description', '')}"
        return str(idx)
    if construct in ("general_principles", "de_minimis_categories"):
        return str(v1[construct][idx])
    for topic in v1["topics"]:
        if topic["id"] == tid:
            item = topic[construct][idx]
            if isinstance(item, dict):  # acceptable_variations
                return f"if {item.get('if', '')!r} -> {item.get('to', '')!r}"
            return str(item)
    return "<not found>"


def harvest_coverage_failures(spec: HarvestSpec) -> list[str]:
    """Both directions: nothing dropped, nothing invented.

    Every source item has EXACTLY ONE disposition -- mapped in `coverage`, or
    dropped on purpose in `not_harvested_items` with a reason. Unlisted, listed
    twice, or listed against an item that no longer exists all fail.
    """
    failures: list[str] = []
    doc = spec.policy()
    rules = doc["rules"]
    ids = {r["id"] for r in rules}

    if spec.expected_total_rules and len(rules) != spec.expected_total_rules:
        failures.append(f"  policy has {len(rules)} rules, expected {spec.expected_total_rules}")

    for r in rules:
        if r["id"].startswith("floor.") and r["strength"] != "must":
            failures.append(f"  floor rule {r['id']!r} is {r['strength']!r}, expected 'must'")

    # Every rule the map claims exists must actually exist, source or no source.
    for key, rule_ids in sorted(spec.coverage.items(), key=lambda kv: str(kv[0])):
        if not rule_ids:
            failures.append(f"  COVERAGE maps {key} to nothing; every source item needs a rule")
        for rid in rule_ids:
            if rid not in ids:
                failures.append(f"  COVERAGE maps {key} to rule {rid!r}, which is not in the policy")

    # A dropped item must say WHY. "" is not a disposition.
    for key, reason in sorted(spec.not_harvested_items.items(), key=lambda kv: str(kv[0])):
        if not str(reason).strip():
            failures.append(
                f"  NOT_HARVESTED_ITEMS[{key}] records no reason; dropping a source item is a "
                f"decision and has to be justified in writing, not merely declared"
            )

    # policy -> v1: nothing invented on the way across.
    mapped_rule_ids = {rid for rids in spec.coverage.values() for rid in rids}
    if spec.coverage:
        invented = ids - mapped_rule_ids
        for rid in sorted(invented):
            failures.append(
                f"  policy rule {rid!r} traces to NO source item -- it was invented by the harvest, "
                f"or COVERAGE is missing its provenance"
            )

    # Cross-check against the source while it still exists (retired in a later PR).
    if not spec.coverage or not spec.source_path.exists():
        return failures

    v1 = json.loads(spec.source_path.read_text(encoding="utf-8"))
    source_keys, enum_failures = enumerate_source_items(v1, spec)
    failures += enum_failures

    # v1 -> policy: nothing dropped, across ALL prescriptive constructs.
    for key in source_keys:
        in_coverage = key in spec.coverage
        in_dropped = key in spec.not_harvested_items
        if in_coverage and in_dropped:
            failures.append(
                f"  source item {key} is BOTH mapped in COVERAGE and dropped in "
                f"NOT_HARVESTED_ITEMS; an item has exactly one disposition: "
                f"{source_item_text(v1, key)!r}"
            )
        elif not in_coverage and not in_dropped:
            failures.append(
                f"  source item {key} is NOT harvested and NOT dispositioned: "
                f"{source_item_text(v1, key)!r}"
            )

    # ...and neither table has gone stale against a source item that vanished.
    for key in sorted(set(spec.coverage) - set(source_keys), key=str):
        failures.append(f"  COVERAGE maps source item {key}, which no longer exists in the source")
    for key in sorted(set(spec.not_harvested_items) - set(source_keys), key=str):
        failures.append(
            f"  NOT_HARVESTED_ITEMS drops source item {key}, which no longer exists in the source"
        )

    # Readable summary of the harvest's shape.
    actual_counts: dict[str, int] = {}
    for construct, _tid, _idx in source_keys:
        actual_counts[construct] = actual_counts.get(construct, 0) + 1
    for construct, expected in spec.expected_source_item_counts.items():
        actual = actual_counts.get(construct, 0)
        if actual != expected:
            failures.append(
                f"  source {construct} is now {actual} item(s), pinned {expected} -- re-harvest"
            )
    return failures


def debranded_failures(spec: HarvestSpec) -> list[str]:
    """No tenant-name literal survives the harvest (white-label release rule)."""
    text = spec.policy_path.read_text(encoding="utf-8")
    failures: list[str] = []
    if "Exos" in text:
        failures.append("  policy contains the tenant-name literal the tenant name (white-label release rule)")
    if "teamexos" in text.lower():
        failures.append("  policy contains a 'teamexos' literal")
    return failures


def check_5_debranded() -> list[str]:
    return debranded_failures(SELF_TEST_SPEC)


def not_falsely_approved_failures(spec: HarvestSpec) -> list[str]:
    """We did not stamp an approval nobody gave."""
    doc = spec.policy()
    approval = doc["approval"]
    failures: list[str] = []
    if approval["status"] != "draft":
        failures.append(
            f"  policy ships as {approval['status']!r}; the harvest reworded the source's rules, so "
            f"the source's sign-off does not carry over — it must be 'draft' until a human approves"
        )
    if approval.get("approved_by") or approval.get("approved_at"):
        failures.append("  draft policy carries an approver/timestamp it was never given")
    harvested = approval.get("harvested_from") or {}
    if not harvested.get("path") or not harvested.get("content_hash"):
        failures.append("  approval.harvested_from must record the source path + content_hash")
    return failures


def check_6_not_falsely_approved() -> list[str]:
    return not_falsely_approved_failures(SELF_TEST_SPEC)


def hashing_failures(spec: HarvestSpec) -> list[str]:
    """policy_content_hash is deterministic and DOES cover `approval`."""
    failures: list[str] = []
    doc = spec.policy()
    h1 = policy_load.policy_content_hash(doc)
    if not h1.startswith("sha256:"):
        failures.append(f"  malformed policy hash: {h1!r}")
    # Deterministic across key ordering.
    reordered = json.loads(json.dumps(doc, sort_keys=True))
    if policy_load.policy_content_hash(reordered) != h1:
        failures.append("  policy hash is not order-invariant")
    # Approval IS covered: re-stamping must force a re-bind.
    stamped = copy.deepcopy(doc)
    stamped["approval"]["status"] = "approved"
    stamped["approval"]["approved_by"] = "A Lawyer, General Counsel"
    stamped["approval"]["approved_at"] = "2026-07-16T00:00:00Z"
    if policy_load.policy_content_hash(stamped) == h1:
        failures.append("  policy hash unchanged after re-stamping approval (approval must be covered)")
    # Rule text changes must move the hash.
    edited = copy.deepcopy(doc)
    edited["rules"][0]["text"] = edited["rules"][0]["text"] + " Edited."
    if policy_load.policy_content_hash(edited) == h1:
        failures.append("  policy hash unchanged after editing a rule's text")
    return failures


def check_7_hashing() -> list[str]:
    return hashing_failures(SELF_TEST_SPEC)


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True
    )
    return proc.returncode, proc.stdout.strip()


def provenance_resolves_failures(spec: HarvestSpec) -> list[str]:
    """The recorded source hash RESOLVES -- to a revision in git history when
    one is recorded, and always against the file on disk.

    Not "is non-empty" -- a hash of nothing is worse than no hash: it looks
    like provenance. So this check makes the record answer for itself.

    git_blob_sha/git_commit are recorded IN PAIR and are OPTIONAL: they anchor
    content_hash to a git revision when the harvested source was already a
    landed commit at harvest time, which is the normal case (see
    tests/test_nda_policy.py's harvest, pinned to a real, reachable commit).
    They are omitted when the source was edited in the SAME changeset that
    records the harvest: there is no prior commit whose tree contains the new
    bytes yet, and fabricating one -- a commit-tree object reachable from no
    pushed ref, dangling the moment it's written -- is worse than recording
    none. It survives on the machine that made it and nowhere else, so it
    resolves locally and fails on a fresh clone of pushed history, which is
    exactly the failure mode this check exists to catch. When git_commit is
    absent, content_hash is verified the only other way it can be: directly
    against the file on disk.

    It matters because the private repo's history makes a deleted source's
    deletion recoverable, but the public cut is FRESH-HISTORY. There, this
    record is all that says what a policy was harvested from -- so it has to
    be true here, where it can still be checked.
    """
    failures: list[str] = []
    harvested = spec.policy()["approval"].get("harvested_from") or {}
    path = harvested.get("path")
    recorded = harvested.get("content_hash") or ""
    blob_sha = harvested.get("git_blob_sha") or ""
    commit = harvested.get("git_commit") or ""

    for field, value in (("path", path), ("content_hash", recorded)):
        if not value:
            failures.append(f"  approval.harvested_from.{field} is empty; provenance must resolve")
    if failures:
        return failures

    if bool(blob_sha) != bool(commit):
        failures.append(
            "  approval.harvested_from has one of git_blob_sha/git_commit but not the other; "
            "they anchor content_hash to a revision as a pair or are omitted entirely"
        )
        return failures

    src = REPO_ROOT / path

    if blob_sha and commit:
        rc, _ = _git("rev-parse", "--git-dir")
        if rc != 0:
            failures.append("  not a git checkout: cannot verify that the harvest hash resolves")
            return failures

        # The blob still exists and hashes to what we recorded.
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "blob", blob_sha], capture_output=True
        )
        # FRESH-HISTORY TREE (the public cut, issue #406): `git archive` →
        # `git init` keeps the files and drops the past, so the recorded COMMIT
        # is absent even though the recorded BLOB usually still resolves — git
        # blobs are content-addressed, so an unchanged file hashes to the same
        # object id in any repository. Key the detection on the commit, not the
        # blob. Where the revision is genuinely unreachable, the git-anchored
        # half of this check is unverifiable BY CONSTRUCTION and failing would
        # only mean "this is the published mirror", which is not a defect. The
        # content checks below still run, so content_hash must still name the
        # bytes actually shipped; the private origin, where history exists,
        # remains where the revision anchor is enforced.
        commit_rc, _ = _git("rev-parse", "--verify", f"{commit}^{{commit}}")
        history_anchored = proc.returncode == 0 and commit_rc == 0
        if not history_anchored:
            print(
                f"  NOTE: commit {commit} is not in this checkout's history "
                f"(fresh-history tree); verifying {path!r} by content instead."
            )
            if proc.returncode == 0:
                # The blob IS present (content-addressed), so the recorded hash
                # can still be held to it even without the revision.
                actual = "sha256:" + hashlib.sha256(proc.stdout).hexdigest()
                if actual != recorded:
                    failures.append(
                        f"  content_hash {recorded} is NOT the hash of blob {blob_sha} "
                        f"(which hashes to {actual})"
                    )

        if history_anchored:
            actual = "sha256:" + hashlib.sha256(proc.stdout).hexdigest()
            if actual != recorded:
                failures.append(
                    f"  content_hash {recorded} is NOT the hash of blob {blob_sha} (which hashes to {actual})"
                )

            # ...and the recorded commit's tree really resolves `path` to that blob, so
            # the hash is anchored to a revision rather than a loose object.
            rc, found = _git("rev-parse", f"{commit}:{path}")
            if rc != 0:
                failures.append(f"  commit {commit} does not resolve {path!r}: the harvest revision is not in history")
            elif found != blob_sha:
                failures.append(
                    f"  commit {commit} resolves {path!r} to {found}, not the recorded blob {blob_sha}"
                )
        elif not src.exists():
            failures.append(
                f"  {path} is neither in this checkout's history nor on disk: "
                f"content_hash resolves to nothing"
            )
            return failures
    elif not src.exists():
        failures.append(
            f"  {path} does not exist on disk and no git_commit is recorded: "
            f"content_hash resolves to nothing"
        )
        return failures

    # If the source is still on disk, it must not have drifted from the
    # revision we harvested -- any edit to any item demands a re-harvest.
    if src.exists():
        on_disk = "sha256:" + hashlib.sha256(src.read_bytes()).hexdigest()
        if on_disk != recorded:
            failures.append(
                f"  {path} on disk hashes to {on_disk}, not the harvested {recorded} -- the source "
                f"changed since the harvest; re-harvest and re-record provenance"
            )
    return failures


def check_8_provenance_resolves() -> list[str]:
    return provenance_resolves_failures(SELF_TEST_SPEC)


def main() -> int:
    checks = [
        ("1", "policy.schema.json valid; committed policy validates", check_1_schema),
        ("2", "loader rejects dup ids / version mismatch / unsigned approval", check_2_loader_invariants),
        ("3", "resolution is generic by playbook_id, highest version wins", check_3_generic_resolution),
        ("5", "policy is debranded (no tenant-name literal)", check_5_debranded),
        ("6", "policy is draft, not falsely stamped approved", check_6_not_falsely_approved),
        ("7", "policy_content_hash deterministic and covers approval", check_7_hashing),
        ("8", "harvest provenance resolves (to git history when anchored, always on disk)", check_8_provenance_resolves),
    ]
    ok = True
    for code, name, fn in checks:
        failures = fn()
        status = "PASS" if not failures else "FAIL"
        print(f"Check {code}: {name} ... {status}")
        for line in failures:
            print(line)
        if failures:
            ok = False
    print()
    if ok:
        print("All policy-document checks passed.")
        return 0
    print("One or more policy-document checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
