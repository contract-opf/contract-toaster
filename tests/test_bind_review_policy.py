#!/usr/bin/env python3
"""Gate for bind + lineage of the review policy (work item 2 of the OPF 0.3 launch).

A review's outcome is a function of BOTH inputs: the corpus-derived playbook
(descriptive precedent) and the human-authored policy (prescriptive rules).
Recording only the playbook hash would leave half the governing input
unrecorded. This gate covers binding the second half, and the integrity check
that makes the first half trustworthy:

 1. VERIFY, DON'T TRUST: bind refuses an OPF whose identity.content_hash does
    not verify. lineage.opf_content_hash is copied from identity verbatim and is
    what every review records as "the playbook that governed this document" --
    copying it unverified would make that record a claim, not a fact.
 2. .opf.html IS BINDABLE: the engine's primary distribution artifact binds
    directly, yielding the same bundle as its embedded .opf.json.
 3. POLICY RECORDED: a bound bundle carries review_policy {path, version, hash,
    approval_status}.
 4. WRONG-PLAYBOOK POLICY REFUSED: binding one playbook's rules onto another
    would silently review a contract against the wrong positions.
 5. NO POLICY = ABSENT KEY, not an empty object -- "no policy" and "a policy
    with no rules" are different states.
 6. SCHEMA: bundles with and without review_policy both validate against
    bundle.schema-v2.json.
 7. LINEAGE: _resolve_opf_lineage surfaces policy_version + policy_hash +
    policy_approval_status, and still returns all-None for a playbook with no
    bundle_path.
 8. RECORDED: the review row a submission actually PERSISTS carries those
    three fields, as does the execution input. Check 7 only proves the
    resolver knows them; a resolver whose output is dropped at the call site
    leaves two reviews run against different policy versions byte-identical
    in the audit trail. This check reads the row back.
 9. NOT RECORDED WHEN THERE IS NOTHING TO RECORD: a playbook with no
    bundle_path leaves the policy fields ABSENT from the row (not null), same
    convention as the OPF lineage fields beside them.

Exit code: 0 = all pass, 1 = one or more failed.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(BACKEND_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# backend.src.reviews builds AWS clients at import; the table names are never
# contacted here (_resolve_opf_lineage reads the registry + bundle file only).
os.environ.setdefault("REVIEW_SUBMISSIONS_TABLE", "contract-toaster-review-submissions-test")
os.environ.setdefault("REVIEWS_TABLE", "contract-toaster-reviews-test")
os.environ.setdefault("DAILY_SPEND_TABLE", "contract-toaster-daily-spend-test")
os.environ.setdefault("PLAYBOOKS_TABLE", "contract-toaster-playbooks-test")
os.environ.setdefault(
    "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:contract-toaster-test"
)

import bind_bundle  # noqa: E402
import opf_canonicalize  # noqa: E402
import opf_load  # noqa: E402
import playbook_registry  # noqa: E402
import policy_load  # noqa: E402

FIXTURE_JSON = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.json"
FIXTURE_HTML = REPO_ROOT / "tests" / "gold-fixtures-opf" / "acme-university.opf.html"
MODEL_POLICY = REPO_ROOT / "model-policy" / "bedrock-us-east-1.json"
BUNDLE_SCHEMA = REPO_ROOT / "playbooks" / "bundle.schema-v2.json"
# Any already-valid, already-synthetic policy works here: these checks only
# need SOME loadable policy document to bind and re-key per playbook_id/
# version, never its specific content (issue #413 evicted the real eiaa
# harvest this used to point at; issue #412 deleted the "sample-agreement"
# playbook + its policy, which this used to point at instead).
POLICY_TEMPLATE = REPO_ROOT / "playbooks" / "nda-policy-v1.json"

PLAYBOOK_ID = "acme-university"  # an agreement_type alias of the fixture


def _opf() -> dict:
    return json.loads(FIXTURE_JSON.read_text(encoding="utf-8"))


def _write_policy(tmp: Path, playbook_id: str, version: int = 1) -> Path:
    """A valid policy for *playbook_id*, derived from the committed one."""
    doc = json.loads(POLICY_TEMPLATE.read_text(encoding="utf-8"))
    doc["playbook_id"] = playbook_id
    doc["version"] = version
    path = tmp / policy_load.policy_filename(playbook_id, version)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _bind(opf_doc: dict, **kw) -> dict:
    kw.setdefault("playbook_id", PLAYBOOK_ID)
    kw.setdefault("model_policy_path", MODEL_POLICY)
    return bind_bundle.bind_bundle(opf_doc, **kw)


def check_1_refuses_unverified_opf() -> list[str]:
    doc = _opf()
    # Edit after compile, do NOT reseal: identity.content_hash goes stale.
    doc["evidence"]["clauses"][0]["title"] = "Indemnification (tampered)"
    try:
        _bind(doc)
    except bind_bundle.BindBundleError as exc:
        if "content_hash" not in str(exc):
            return [f"  refused, but not for the hash reason: {exc}"]
        return []
    except Exception as exc:  # noqa: BLE001
        return [f"  raised {type(exc).__name__}, expected BindBundleError: {exc}"]
    return ["  bind ACCEPTED an OPF whose content_hash does not verify"]


def check_2_html_is_bindable() -> list[str]:
    failures: list[str] = []
    from_html = opf_load.load_opf_document(FIXTURE_HTML)
    from_json = opf_load.load_opf_document(FIXTURE_JSON)
    if from_html != from_json:
        failures.append("  .opf.html and .opf.json did not yield the same document")
    b_html = _bind(from_html)
    b_json = _bind(from_json)
    if b_html != b_json:
        failures.append("  binding the .opf.html produced a different bundle than the .opf.json")
    if b_html["lineage"]["opf_content_hash"] != from_html["identity"]["content_hash"]:
        failures.append("  lineage.opf_content_hash != the (verified) identity hash")
    return failures


def check_3_policy_recorded() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        policy_path = _write_policy(tmp, PLAYBOOK_ID, version=3)
        bundle = _bind(_opf(), review_policy_path=policy_path)
        rp = bundle.get("review_policy")
        if not rp:
            return ["  bound bundle has no review_policy block"]
        expected_hash = policy_load.policy_content_hash(policy_load.load_policy(policy_path))
        if rp.get("version") != 3:
            failures.append(f"  review_policy.version == {rp.get('version')!r}, expected 3")
        if rp.get("hash") != expected_hash:
            failures.append("  review_policy.hash != policy_content_hash of the bound policy")
        if rp.get("approval_status") != "draft":
            failures.append(f"  review_policy.approval_status == {rp.get('approval_status')!r}, expected 'draft'")
        if not rp.get("path"):
            failures.append("  review_policy.path is empty")
        # model_policy is a DIFFERENT thing and must not be conflated.
        if bundle["model_policy"]["hash"] == rp["hash"]:
            failures.append("  review_policy.hash collides with model_policy.hash")
    return failures


def check_4_refuses_wrong_playbook_policy() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # A perfectly valid policy -- for a DIFFERENT playbook.
        policy_path = _write_policy(tmp, "some-other-playbook", version=1)
        try:
            _bind(_opf(), review_policy_path=policy_path)
        except bind_bundle.BindBundleError:
            return []
        except Exception as exc:  # noqa: BLE001
            return [f"  raised {type(exc).__name__}, expected BindBundleError: {exc}"]
        return ["  bind ACCEPTED a policy belonging to a different playbook_id"]


def check_5_no_policy_absent_key() -> list[str]:
    bundle = _bind(_opf(), review_policy_path=None)
    if "review_policy" in bundle:
        return ["  binding without a policy left a review_policy key (should be absent, not empty)"]
    return []


def check_6_schema() -> list[str]:
    import jsonschema

    failures: list[str] = []
    schema = json.loads(BUNDLE_SCHEMA.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td:
        policy_path = _write_policy(Path(td), PLAYBOOK_ID)
        with_policy = _bind(_opf(), review_policy_path=policy_path)
    without_policy = _bind(_opf(), review_policy_path=None)
    for label, bundle in (("with review_policy", with_policy), ("without", without_policy)):
        try:
            jsonschema.validate(instance=bundle, schema=schema)
        except jsonschema.ValidationError as exc:
            failures.append(f"  bundle {label} failed bundle.schema-v2: {exc.validator} at {list(exc.absolute_path)}")
    # A malformed review_policy must be rejected by the schema.
    bad = copy.deepcopy(with_policy)
    bad["review_policy"]["approval_status"] = "rubber-stamped"
    try:
        jsonschema.validate(instance=bad, schema=schema)
        failures.append("  schema accepted an unknown review_policy.approval_status")
    except jsonschema.ValidationError:
        pass
    return failures


# ---------------------------------------------------------------------------
# Synthetic registry + in-memory AWS fakes for checks 7-9.
#
# No registry entry carries a `bundle_path` today, so the OPF-lineage path is
# dormant in production until an operator wires one at acceptance: these
# checks construct the wired state explicitly rather than leaning on the real
# playbooks/registry.json (which they never read or write -- the
# synthetic-registry pattern from scripts/playbook_registry.py's docstring).
#
# The DynamoDB/Step Functions fakes are the ones established by
# tests/test_active_bundle_resolver_194.py and reused by
# tests/test_review_opf_lineage.py: submit_review composes with
# reserve_spend's atomic conditional UpdateExpression, which moto 5.2.2
# cannot parse, so the full-path checks use these instead of moto.
# ---------------------------------------------------------------------------

NO_BUNDLE_PLAYBOOK_ID = "no-bundle"

POLICY_FIELDS = ("policy_version", "policy_hash", "policy_approval_status")


@contextlib.contextmanager
def _synthetic_registry(bundle: dict | None):
    """Register PLAYBOOK_ID with a `bundle_path` pointing at `bundle` (when
    given), plus a NO_BUNDLE_PLAYBOOK_ID entry carrying no `bundle_path` key
    at all. Yields the temp root."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        playbooks_dir = tmp / "playbooks"
        (playbooks_dir / "bundles").mkdir(parents=True)

        entry = {"playbook_id": PLAYBOOK_ID, "playbook_path": "playbooks/unused.json"}
        if bundle is not None:
            bundle_rel = "playbooks/bundles/acme.bundle-v2.json"
            (tmp / bundle_rel).write_text(json.dumps(bundle), encoding="utf-8")
            entry["bundle_path"] = bundle_rel
        registry = {
            "default_playbook_id": PLAYBOOK_ID,
            "playbooks": {
                PLAYBOOK_ID: entry,
                NO_BUNDLE_PLAYBOOK_ID: {
                    "playbook_id": NO_BUNDLE_PLAYBOOK_ID,
                    "playbook_path": "playbooks/unused.json",
                },
            },
        }
        registry_path = playbooks_dir / "registry.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")

        real_registry, real_root = playbook_registry.REGISTRY_PATH, playbook_registry.REPO_ROOT
        playbook_registry.REGISTRY_PATH = registry_path
        playbook_registry.REPO_ROOT = tmp
        try:
            yield tmp
        finally:
            playbook_registry.REGISTRY_PATH = real_registry
            playbook_registry.REPO_ROOT = real_root


class FakeTable:
    def __init__(self, key_name: str):
        self.key_name = key_name
        self.items: dict[str, dict] = {}

    def get_item(self, Key):
        item = self.items.get(Key[self.key_name])
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item[self.key_name]
        if ConditionExpression == "attribute_not_exists(idempotency_key)" and key in self.items:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def scan(self):
        return {"Items": list(self.items.values())}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ExpressionAttributeNames=None,
    ):
        item = self.items.setdefault(Key[self.key_name], dict(Key))
        vals = ExpressionAttributeValues or {}
        if "reserved_usd_cents = if_not_exists" in UpdateExpression:
            item["reserved_usd_cents"] = item.get("reserved_usd_cents", 0) + vals[":amount"]
            item.setdefault("daily_cap_usd_cents", vals.get(":cap"))
            return
        if "execution_arn = :arn" in UpdateExpression:
            item["execution_arn"] = vals[":arn"]
            if ":status" in vals:
                item["execution_status"] = vals[":status"]
            return
        if "spend_reservation_id = :rid" in UpdateExpression:
            item["spend_reservation_id"] = vals[":rid"]
            return


class FakeDynamoDBResource:
    def __init__(self):
        self._tables: dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:  # noqa: N802 — boto3's own casing
        if name not in self._tables:
            key_name = {
                os.environ["REVIEW_SUBMISSIONS_TABLE"]: "idempotency_key",
                os.environ["REVIEWS_TABLE"]: "review_id",
                os.environ["DAILY_SPEND_TABLE"]: "spend_date",
                os.environ["PLAYBOOKS_TABLE"]: "playbook_id",
            }.get(name, "id")
            self._tables[name] = FakeTable(key_name)
        return self._tables[name]


class ExecutionAlreadyExists(Exception):
    pass


class FakeSfnClient:
    class exceptions:  # noqa: N801 — mirrors botocore's client.exceptions shape
        ExecutionAlreadyExists = ExecutionAlreadyExists

    def start_execution(self, stateMachineArn, name, input):  # noqa: N803 — boto3 kwargs
        return {"executionArn": f"{stateMachineArn}:{name}"}


def _submit(playbook_id: str, owner_sub: str) -> tuple[dict, dict]:
    """Run the FULL submit_review path against the fakes; return the
    (review row, execution input) that were actually PERSISTED."""
    import src.reviews as reviews  # noqa: PLC0415 — imported late, after env defaults above

    ddb = FakeDynamoDBResource()
    result = reviews.submit_review(
        owner_sub=owner_sub,
        playbook_id=playbook_id,
        file_sha256="filehash-" + owner_sub,
        upload_pointer=f"uploads/{owner_sub}/in.docx",
        active_release_bundle_hash="sha256:" + "0" * 64,
        dynamodb_resource=ddb,
        sfn_client=FakeSfnClient(),
    )
    review_row = ddb.Table(os.environ["REVIEWS_TABLE"]).items[result["review_id"]]
    submission = next(iter(ddb.Table(os.environ["REVIEW_SUBMISSIONS_TABLE"]).items.values()))
    return review_row, json.loads(submission["execution_input"])


def check_7_lineage() -> list[str]:
    import src.reviews as reviews  # noqa: PLC0415 — imported late, after env defaults above

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        policy_path = _write_policy(Path(td), PLAYBOOK_ID, version=2)
        bundle = _bind(_opf(), review_policy_path=policy_path)

    with _synthetic_registry(bundle):
        lineage = reviews._resolve_opf_lineage(PLAYBOOK_ID)
        if lineage.get("policy_version") != 2:
            failures.append(f"  lineage.policy_version == {lineage.get('policy_version')!r}, expected 2")
        if lineage.get("policy_hash") != bundle["review_policy"]["hash"]:
            failures.append("  lineage.policy_hash != the bundle's review_policy.hash")
        if lineage.get("policy_approval_status") != bundle["review_policy"]["approval_status"]:
            failures.append(
                f"  lineage.policy_approval_status == {lineage.get('policy_approval_status')!r}, "
                f"expected {bundle['review_policy']['approval_status']!r}"
            )
        if lineage.get("opf_content_hash") != bundle["lineage"]["opf_content_hash"]:
            failures.append("  lineage.opf_content_hash did not resolve from the bundle")

        # A playbook with no bundle_path stays all-None, incl. the new keys.
        empty = reviews._resolve_opf_lineage(NO_BUNDLE_PLAYBOOK_ID)
        for key in POLICY_FIELDS:
            if empty.get(key) is not None:
                failures.append(f"  no-bundle playbook fabricated {key}={empty.get(key)!r}")
        if set(empty) != set(reviews._EMPTY_OPF_LINEAGE):
            failures.append("  empty lineage key set drifted from _EMPTY_OPF_LINEAGE")
    return failures


def check_8_row_records_policy() -> list[str]:
    """The motivating scenario: edit the policy, bump the version, re-bind,
    review again -- the two review rows must NOT be identical in lineage.
    Asserts on the row read back out of the table, not on the resolver."""
    failures: list[str] = []
    rows: list[dict] = []
    inputs: list[dict] = []
    bundles: list[dict] = []

    for version in (1, 2):
        with tempfile.TemporaryDirectory() as td:
            policy_path = _write_policy(Path(td), PLAYBOOK_ID, version=version)
            bundle = _bind(_opf(), review_policy_path=policy_path)
        bundles.append(bundle)
        with _synthetic_registry(bundle):
            row, execution_input = _submit(PLAYBOOK_ID, f"owner-v{version}")
        rows.append(row)
        inputs.append(execution_input)

    for i, version in enumerate((1, 2)):
        rp = bundles[i]["review_policy"]
        for carrier, label in ((rows[i], "review row"), (inputs[i], "execution input")):
            if carrier.get("policy_version") != version:
                failures.append(
                    f"  {label} policy_version == {carrier.get('policy_version')!r}, expected {version}"
                )
            if carrier.get("policy_hash") != rp["hash"]:
                failures.append(f"  {label} policy_hash != the bound policy's hash")
            # The committed policy is still a DRAFT awaiting legal review. The
            # record has to say so: a hash names the rules, only this says
            # whether anyone approved them.
            if carrier.get("policy_approval_status") != rp["approval_status"]:
                failures.append(
                    f"  {label} policy_approval_status == {carrier.get('policy_approval_status')!r}, "
                    f"expected {rp['approval_status']!r}"
                )

    lineage_keys = ("opf_content_hash", "policy_version", "policy_hash", "policy_approval_status")
    if {k: rows[0].get(k) for k in lineage_keys} == {k: rows[1].get(k) for k in lineage_keys}:
        failures.append(
            "  two reviews run against DIFFERENT policy versions produced identical lineage on the row"
        )
    return failures


def check_9_no_bundle_no_policy_fields() -> list[str]:
    failures: list[str] = []
    with _synthetic_registry(None):
        row, execution_input = _submit(NO_BUNDLE_PLAYBOOK_ID, "owner-v1-playbook")
    for carrier, label in ((row, "review row"), (execution_input, "execution input")):
        for key in POLICY_FIELDS:
            if key in carrier:
                failures.append(f"  {label} carries {key}={carrier[key]!r} (should be absent, not null)")
    # The row is otherwise exactly what it always was.
    if row.get("status") != "PENDING" or row.get("playbook_id") != NO_BUNDLE_PLAYBOOK_ID:
        failures.append("  a v1-playbook review row lost a pre-existing field")
    return failures


def main() -> int:
    checks = [
        ("1", "bind refuses an OPF whose content_hash does not verify", check_1_refuses_unverified_opf),
        ("2", ".opf.html binds, and yields the same bundle as its .opf.json", check_2_html_is_bindable),
        ("3", "bound bundle records review_policy {path,version,hash,approval_status}", check_3_policy_recorded),
        ("4", "bind refuses a policy belonging to another playbook_id", check_4_refuses_wrong_playbook_policy),
        ("5", "no policy -> review_policy key ABSENT, not empty", check_5_no_policy_absent_key),
        ("6", "bundles validate against bundle.schema-v2 (and bad status rejected)", check_6_schema),
        ("7", "_resolve_opf_lineage surfaces policy_version + hash + approval_status", check_7_lineage),
        ("8", "the PERSISTED review row + execution input record the policy", check_8_row_records_policy),
        ("9", "no bundle_path -> policy fields ABSENT from the row, not null", check_9_no_bundle_no_policy_fields),
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
        print("All bind/review-policy lineage checks passed.")
        return 0
    print("One or more bind/review-policy lineage checks FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
