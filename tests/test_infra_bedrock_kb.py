#!/usr/bin/env python3
"""
Structural gate for issue #60: Bedrock Knowledge Base + S3 Vectors (retrieval store).

Verifies that all acceptance criteria for issue #60 are satisfied (empty-corpus
phase — this issue proves the store exists, is private, and is queryable; real
ingestion/extraction is out of scope, see issue notes):

  A. Bedrock Knowledge Base + S3 Vectors store defined in
     infra/lib/nested/data-stack.ts (vector bucket, vector index, knowledge
     base, and a dedicated KB service role distinct from the pipeline's
     query-time role).

  B. Ingestion wiring: a Bedrock DataSource (S3) targets the corpus bucket so
     an admin corpus upload can trigger a KB ingestion job into a draft/
     staging index; draft snapshots are not review-queryable (documented).

  C. Corrected metadata model: the S3 Vectors index metadata configuration
     carries only compact IDs/filter fields (clause_id, source_document_id,
     document_type, playbook_id, playbook_topic_id, counterparty_name, date,
     corpus_snapshot_version, corpus_polarity) — no full clause text,
     rationale, or model-summary fields, and stays within the ~35-key /
     ~1KB Bedrock-KB-on-S3-Vectors metadata limits.

  D. Positive/negative separation: document_type distinguishes
     executed-final/accepted-draft/rejected-draft and corpus_polarity
     distinguishes positive/negative so rejected drafts are never commingled
     with positive precedent in the same top-K context.

  E. Curation fields: reusable_precedent, negotiation_context, superseded_by,
     approved_use_scope are part of the clause record / metadata contract.

  F. Corpus versioning / activation: ARCHITECTURE.md documents the
     application-layer active/staging split, per-execution physical-store
     pinning, the content-addressed clause-id manifest, and the ingestion
     interlock (already required by issue #20 — this issue's infra must not
     regress that documentation).

  G. Reconciled least-privilege invariant: the dedicated Bedrock KB service
     role (assumed by bedrock.amazonaws.com) holds bedrock:InvokeModel
     scoped ONLY to the embedding-model ARN (never a wildcard, never the
     primary/critic model ARNs) — distinct from pipelineReviewRole (#59),
     which remains the sole holder of bedrock:Retrieve/RetrieveAndGenerate
     and of bedrock:InvokeModel scoped to the primary/critic model ARNs.

  H. Private access only: the KB / S3 Vectors store is never public; reachable
     only via IAM roles, with a VPC interface endpoint for Bedrock wired in
     network-stack.ts (or documented equivalent).

  I. cdk synth runs cleanly with the KB + S3 Vectors resources present, and a
     trivial-query / empty-corpus path is documented (no error against an
     empty active snapshot).

  J. Idle cost sanity: no OpenSearch Serverless / OCU-minimum resource is
     defined anywhere in infra/ (S3 Vectors has no idle floor).

Exit codes: 0 = all checks pass, 1 = one or more checks failed.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from infra_synth_helper import NEUTRAL_CDK_CONTEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"
DATA_STACK_PATH = INFRA / "lib" / "nested" / "data-stack.ts"
PIPELINE_STACK_PATH = INFRA / "lib" / "nested" / "pipeline-stack.ts"
NETWORK_STACK_PATH = INFRA / "lib" / "nested" / "network-stack.ts"
APP_STACK_PATH = INFRA / "lib" / "nested" / "app-stack.ts"
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _find_ts_sources() -> list[Path]:
    sources: list[Path] = []
    for subdir in ("lib", "bin"):
        p = INFRA / subdir
        if p.is_dir():
            sources.extend(
                q for q in p.rglob("*.ts") if "node_modules" not in q.parts
            )
    return sources


def _assert(condition: bool, label: str, detail: str = "") -> list[str]:
    if condition:
        print(f"  [PASS] {label}")
        return []
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    return [label]


# ---------------------------------------------------------------------------
# Check A -- KB + S3 Vectors store defined, with a dedicated KB service role
# ---------------------------------------------------------------------------

def check_a_kb_and_vectors_defined() -> list[str]:
    print("\nCheck A: Bedrock Knowledge Base + S3 Vectors store defined in data-stack.ts ...")
    failures: list[str] = []

    failures += _assert(
        DATA_STACK_PATH.is_file(),
        "infra/lib/nested/data-stack.ts exists",
    )
    if failures:
        return failures

    text = _read(DATA_STACK_PATH)

    failures += _assert(
        bool(re.search(r"CfnVectorBucket|aws-s3vectors", text)),
        "data-stack.ts defines an S3 Vectors vector bucket (CfnVectorBucket)",
    )
    failures += _assert(
        bool(re.search(r"CfnIndex", text)),
        "data-stack.ts defines an S3 Vectors index (CfnIndex)",
    )
    failures += _assert(
        bool(re.search(r"CfnKnowledgeBase", text)),
        "data-stack.ts defines a Bedrock Knowledge Base (CfnKnowledgeBase)",
    )
    failures += _assert(
        bool(re.search(r"corpusKnowledgeBaseRole", text)),
        "data-stack.ts defines a dedicated corpusKnowledgeBaseRole",
        "AC G requires a KB service role distinct from pipelineReviewRole (#59).",
    )
    failures += _assert(
        bool(re.search(r"bedrock\.amazonaws\.com", text)),
        "corpusKnowledgeBaseRole is assumed by the bedrock.amazonaws.com service principal",
    )

    return failures


# ---------------------------------------------------------------------------
# Check B -- Ingestion wiring: Bedrock DataSource targets the corpus bucket
# ---------------------------------------------------------------------------

def check_b_ingestion_wiring() -> list[str]:
    print("\nCheck B: Ingestion wiring -- Bedrock DataSource targets the corpus bucket ...")
    failures: list[str] = []

    text = _read(DATA_STACK_PATH)

    failures += _assert(
        bool(re.search(r"CfnDataSource", text)),
        "data-stack.ts defines a Bedrock DataSource (CfnDataSource) for corpus ingestion",
    )
    failures += _assert(
        bool(re.search(r"draft", text, re.IGNORECASE)),
        "data-stack.ts / comments document draft ingestion snapshots (not review-queryable)",
    )
    failures += _assert(
        bool(re.search(r"staging", text, re.IGNORECASE)),
        "data-stack.ts / comments document a staging index distinct from the active store",
    )

    return failures


# ---------------------------------------------------------------------------
# Check C -- Corrected metadata model (compact IDs/filters only)
# ---------------------------------------------------------------------------

REQUIRED_METADATA_FIELDS = [
    "clause_id",
    "source_document_id",
    "corpus_snapshot_version",
    "corpus_polarity",
    "document_type",
    "playbook_id",
    "playbook_topic_id",
    "counterparty_name",
]

FORBIDDEN_METADATA_SUBSTANCE = [
    "clause_text",
    "full_text",
    "rationale_text",
    "model_summary",
]


def check_c_metadata_model() -> list[str]:
    print("\nCheck C: Corrected metadata model -- compact IDs/filter fields only ...")
    failures: list[str] = []

    text = _read(DATA_STACK_PATH)

    for field in REQUIRED_METADATA_FIELDS:
        failures += _assert(
            field in text,
            f"data-stack.ts metadata contract includes '{field}'",
        )

    for bad in FORBIDDEN_METADATA_SUBSTANCE:
        failures += _assert(
            bad not in text,
            f"data-stack.ts metadata contract does NOT include forbidden substance field '{bad}'",
            "Full clause text/rationale/summaries must live in S3/DynamoDB keyed by "
            "clause_id, never inline in vector metadata (AWS ~1KB/35-key limit).",
        )

    failures += _assert(
        bool(re.search(r"1\s*KB|~1KB|35\s*(metadata\s*)?key", text, re.IGNORECASE)),
        "data-stack.ts documents the ~1KB / 35-metadata-key S3-Vectors-on-Bedrock-KB limit",
    )

    return failures


# ---------------------------------------------------------------------------
# Check D -- Positive/negative separation
# ---------------------------------------------------------------------------

def check_d_positive_negative_separation() -> list[str]:
    print("\nCheck D: Positive/negative corpus separation ...")
    failures: list[str] = []

    text = _read(DATA_STACK_PATH)

    for doc_type in ["executed-final", "accepted-draft", "rejected-draft"]:
        failures += _assert(
            doc_type in text,
            f"data-stack.ts document_type contract includes '{doc_type}'",
        )

    failures += _assert(
        bool(re.search(r"corpus_polarity", text)) and bool(re.search(r"positive", text, re.IGNORECASE)) and bool(re.search(r"negative", text, re.IGNORECASE)),
        "data-stack.ts documents corpus_polarity positive/negative separation",
    )
    failures += _assert(
        bool(re.search(r"never\s+(?:be\s+)?commingl|not\s+commingl|separate\s+(?:corpora|index)", text, re.IGNORECASE)),
        "data-stack.ts states rejected/negative examples are never commingled with positive precedent",
    )

    return failures


# ---------------------------------------------------------------------------
# Check E -- Curation fields
# ---------------------------------------------------------------------------

CURATION_FIELDS = [
    "reusable_precedent",
    "negotiation_context",
    "superseded_by",
    "approved_use_scope",
]


def check_e_curation_fields() -> list[str]:
    print("\nCheck E: Legal-curated clause fields ...")
    failures: list[str] = []

    text = _read(DATA_STACK_PATH)

    for field in CURATION_FIELDS:
        failures += _assert(
            field in text,
            f"data-stack.ts curation contract includes '{field}'",
        )

    return failures


# ---------------------------------------------------------------------------
# Check F -- Corpus versioning / activation still documented (issue #20)
# ---------------------------------------------------------------------------

def check_f_versioning_still_documented() -> list[str]:
    print("\nCheck F: Corpus versioning / activation / ingestion interlock still documented ...")
    failures: list[str] = []

    if not ARCHITECTURE_PATH.is_file():
        return _assert(False, "ARCHITECTURE.md exists")

    arch = _read(ARCHITECTURE_PATH)

    failures += _assert(
        bool(re.search(r"physical\s+(?:store|KB|knowledge.?base)\s+(?:id|identifier)", arch, re.IGNORECASE)),
        "ARCHITECTURE.md documents the physical store/KB ID pin (issue #20)",
    )
    failures += _assert(
        bool(re.search(r"ingestion\s+interlock|interlock", arch, re.IGNORECASE)),
        "ARCHITECTURE.md documents the ingestion interlock",
    )
    failures += _assert(
        bool(re.search(r"content.?addressed\s+manifest|clause.?id\s+manifest", arch, re.IGNORECASE)),
        "ARCHITECTURE.md documents the content-addressed clause-id manifest per snapshot",
    )

    return failures


# ---------------------------------------------------------------------------
# Check G -- Reconciled least-privilege: KB role scoped to embedding model
# ONLY; pipelineReviewRole remains sole holder of the primary/critic-scoped
# grants and KB query actions.
# ---------------------------------------------------------------------------

EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2"


def check_g_reconciled_least_privilege() -> list[str]:
    print("\nCheck G: Reconciled least-privilege -- KB role scoped to embedding model ONLY ...")
    failures: list[str] = []

    if not DATA_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/data-stack.ts exists")
    if not PIPELINE_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/pipeline-stack.ts exists")

    data_text = _read(DATA_STACK_PATH)
    pipeline_text = _read(PIPELINE_STACK_PATH)

    # G1: corpusKnowledgeBaseRole holds bedrock:InvokeModel ...
    failures += _assert(
        bool(re.search(r"corpusKnowledgeBaseRole[\s\S]{0,800}bedrock:InvokeModel", data_text))
        or bool(re.search(r"bedrock:InvokeModel[\s\S]{0,800}corpusKnowledgeBaseRole", data_text)),
        "corpusKnowledgeBaseRole is granted bedrock:InvokeModel",
    )

    # G2: ... scoped strictly to the embedding-model ARN, never a wildcard
    # foundation-model/* grant in data-stack.ts.
    failures += _assert(
        EMBEDDING_MODEL_ID in data_text,
        f"data-stack.ts scopes the KB role's InvokeModel grant to the embedding model ({EMBEDDING_MODEL_ID})",
    )
    wildcard_fm_grants = re.findall(
        r"resources:\s*\[[^\]]*foundation-model/\*[^\]]*\]",
        data_text,
    )
    failures += _assert(
        not wildcard_fm_grants,
        "data-stack.ts does NOT grant bedrock:InvokeModel on a wildcard foundation-model/* resource",
        f"Found: {wildcard_fm_grants}" if wildcard_fm_grants else "",
    )

    # G3: corpusKnowledgeBaseRole must NOT hold a grant scoped to the primary
    # or critic review model IDs (anthropic.claude-opus / anthropic.claude-sonnet).
    kb_role_block_match = re.search(
        r"corpusKnowledgeBaseRole[\s\S]{0,2000}",
        data_text,
    )
    kb_role_block = kb_role_block_match.group(0) if kb_role_block_match else ""
    failures += _assert(
        "anthropic.claude" not in kb_role_block,
        "corpusKnowledgeBaseRole's policy block does not reference the primary/critic (Anthropic Claude) model ARNs",
    )

    # G4: pipelineReviewRole (#59) remains the ONLY role granted
    # bedrock:Retrieve / bedrock:RetrieveAndGenerate anywhere in infra/lib.
    kb_query_offenders = []
    for p in _find_ts_sources():
        if p == PIPELINE_STACK_PATH:
            continue
        t = _read(p)
        if "bedrock:Retrieve" in t or "bedrock:RetrieveAndGenerate" in t:
            kb_query_offenders.append(str(p.relative_to(REPO_ROOT)))
    failures += _assert(
        not kb_query_offenders,
        "No infra file other than pipeline-stack.ts grants bedrock:Retrieve/RetrieveAndGenerate",
        f"Offending files: {kb_query_offenders}" if kb_query_offenders else "",
    )
    failures += _assert(
        "bedrock:Retrieve" in pipeline_text or "bedrock:RetrieveAndGenerate" in pipeline_text,
        "pipeline-stack.ts (pipelineReviewRole) still grants bedrock:Retrieve/RetrieveAndGenerate",
    )

    # G5: pipeline-stack.ts's bedrock:InvokeModel grant (primary/critic) must
    # NOT be a bare foundation-model/* wildcard either -- reconciliation
    # requires ARN-scoping to the primary/critic model ARNs specifically.
    pipeline_wildcard = re.findall(
        r"resources:\s*\[[^\]]*foundation-model/\*[^\]]*\]",
        pipeline_text,
    )
    failures += _assert(
        not pipeline_wildcard,
        "pipeline-stack.ts does NOT grant bedrock:InvokeModel on a wildcard foundation-model/* resource",
        f"Found: {pipeline_wildcard}" if pipeline_wildcard else "",
    )
    failures += _assert(
        bool(re.search(r"anthropic\.claude-opus", pipeline_text))
        and bool(re.search(r"anthropic\.claude-sonnet", pipeline_text)),
        "pipeline-stack.ts scopes bedrock:InvokeModel to the primary (Opus) and critic (Sonnet) model ARNs",
    )

    # G6: the reconciled invariant statement itself must be documented
    # (ARN-scoped, not action-name-wide) somewhere in the infra sources.
    all_ts = "\n".join(_read(f) for f in _find_ts_sources())
    failures += _assert(
        bool(re.search(r"embedding.?model\s+ARN|embedding.?model.{0,40}scoped", all_ts, re.IGNORECASE)),
        "infra/ sources document the reconciled invariant: KB role's InvokeModel is scoped ONLY to the embedding-model ARN",
    )

    # G7: test_pipeline_stack.py's Check G must have been reworded away from
    # the unqualified action-name-wide invariant (else this test and #59's
    # test would permanently contradict each other under CI).
    pipeline_test_path = REPO_ROOT / "tests" / "test_pipeline_stack.py"
    pipeline_test_text = _read(pipeline_test_path)
    failures += _assert(
        "ONLY role in the infra" not in pipeline_test_text
        or "primary/critic" in pipeline_test_text
        or "model ARN" in pipeline_test_text,
        "tests/test_pipeline_stack.py Check G is restated as the ARN-scoped invariant "
        "(pipelineReviewRole sole holder scoped to primary/critic model ARNs + KB query actions; "
        "a bedrock.amazonaws.com KB service role may separately hold embedding-model-scoped InvokeModel)",
    )

    return failures


# ---------------------------------------------------------------------------
# Check H -- Private access only (no public exposure; VPC endpoint)
# ---------------------------------------------------------------------------

def check_h_private_access() -> list[str]:
    print("\nCheck H: KB / S3 Vectors store is private-only (IAM + VPC endpoint) ...")
    failures: list[str] = []

    data_text = _read(DATA_STACK_PATH)
    failures += _assert(
        "public" not in data_text.lower().replace("no public", "").replace("never public", "")
        or bool(re.search(r"private|never\s+public", data_text, re.IGNORECASE)),
        "data-stack.ts documents the KB/S3 Vectors store as private / never public",
    )

    if not NETWORK_STACK_PATH.is_file():
        return _assert(False, "infra/lib/nested/network-stack.ts exists")

    network_text = _read(NETWORK_STACK_PATH)
    failures += _assert(
        bool(re.search(r"BEDROCK", network_text)),
        "network-stack.ts wires a VPC interface endpoint for Bedrock",
        "AC: 'reachable only via the pipeline task role (IAM) and a VPC endpoint where applicable'.",
    )

    return failures


# ---------------------------------------------------------------------------
# Check I -- cdk synth runs cleanly; empty-corpus / trivial-query documented
# ---------------------------------------------------------------------------

def check_i_cdk_synth() -> list[str]:
    print("\nCheck I: cdk synth runs cleanly with the KB + S3 Vectors resources ...")
    failures: list[str] = []

    if not INFRA.is_dir():
        return _assert(False, "infra/ directory exists (prerequisite for cdk synth)")

    node_modules = INFRA / "node_modules"
    if not node_modules.is_dir():
        print("  (node_modules absent -- running npm install first ...)")
        install = subprocess.run(
            ["npm", "install"],
            cwd=INFRA,
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            return _assert(
                False,
                "npm install succeeded in infra/",
                f"stderr: {install.stderr[-500:]}",
            )

    result = subprocess.run(
        ["npx", "cdk", "synth", "--context", "env=dev", *NEUTRAL_CDK_CONTEXT, "--quiet"],
        cwd=INFRA,
        capture_output=True,
        text=True,
    )
    failures += _assert(
        result.returncode == 0,
        "cdk synth --context env=dev exits 0 (with Bedrock KB + S3 Vectors)",
        f"stdout (last 1000 chars): {result.stdout[-1000:]}\n"
        f"stderr (last 1000 chars): {result.stderr[-1000:]}",
    )

    data_text = _read(DATA_STACK_PATH)
    failures += _assert(
        bool(re.search(r"empty\s+(?:corpus|result|set)", data_text, re.IGNORECASE)),
        "data-stack.ts documents the empty-corpus / empty-result-set query path (no error)",
    )

    return failures


# ---------------------------------------------------------------------------
# Check J -- Idle cost sanity: no OpenSearch Serverless / OCU anywhere
# ---------------------------------------------------------------------------

def check_j_no_opensearch_serverless() -> list[str]:
    print("\nCheck J: Idle cost sanity -- no OpenSearch Serverless / OCU minimum in infra/ ...")
    failures: list[str] = []

    offenders = []
    for p in _find_ts_sources():
        t = _read(p)
        if re.search(r"CfnCollection|aoss\.amazonaws|opensearchserverless", t, re.IGNORECASE):
            # OpenSearchServerlessConfigurationProperty is a *type* the CDK
            # library exposes for other storage backends; only flag an actual
            # OpenSearch Serverless COLLECTION resource being provisioned.
            if re.search(r"new\s+\w*\.?CfnCollection\b", t):
                offenders.append(str(p.relative_to(REPO_ROOT)))

    failures += _assert(
        not offenders,
        "No OpenSearch Serverless collection (OCU-billed) provisioned anywhere in infra/",
        f"Offending files: {offenders}" if offenders else "",
    )

    return failures


def main() -> int:
    all_failures: list[str] = []

    all_failures += check_a_kb_and_vectors_defined()
    all_failures += check_b_ingestion_wiring()
    all_failures += check_c_metadata_model()
    all_failures += check_d_positive_negative_separation()
    all_failures += check_e_curation_fields()
    all_failures += check_f_versioning_still_documented()
    all_failures += check_g_reconciled_least_privilege()
    all_failures += check_h_private_access()
    all_failures += check_i_cdk_synth()
    all_failures += check_j_no_opensearch_serverless()

    print()
    if all_failures:
        print(f"FAIL: {len(all_failures)} check(s) failed. See issue #60.")
        return 1
    print("PASS: all issue #60 Bedrock Knowledge Base + S3 Vectors gates satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
