# AWS App Runner backend image — built from the REPO ROOT context:
#
#   docker build -f backend/Dockerfile .
#
# The backend imports pipeline modules from the repo's top-level scripts/ at
# runtime (backend/src/*.py put `parents[2] / "scripts"` on sys.path), which a
# backend/-only context cannot reach. The image mirrors the repo tree under
# /app, as deploy/dts/backend.Dockerfile does, so `parents[2]` of
# /app/backend/src/<module>.py is /app and /app/scripts is the directory those
# modules look in.
FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies (pinned, no extras we don't need)
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy application source
COPY backend/src backend/src

# The scripts/ modules the backend imports at runtime, and every scripts/
# module those import in turn — that closure and nothing else (no eval,
# test-fixture or repo tooling scripts). tests/test_aws_image_runtime_scripts.py
# recomputes the closure from the import graph and fails if this list is
# missing a module or carries one outside it.
COPY scripts/bind_bundle.py \
     scripts/block_transcript.py \
     scripts/canonicalize.py \
     scripts/clause_boundaries.py \
     scripts/confusables.py \
     scripts/cover_note_pass.py \
     scripts/critic_dossiers.py \
     scripts/critic_review_pass.py \
     scripts/deal_aliases.py \
     scripts/detector_common.py \
     scripts/document_injection_scan.py \
     scripts/docx_parts.py \
     scripts/entity_normalize.py \
     scripts/extraction_normalization_stage.py \
     scripts/floor_judge.py \
     scripts/footnote_audience.py \
     scripts/hard_ledger.py \
     scripts/leakage_scan.py \
     scripts/model_output_schema.py \
     scripts/normalize_input.py \
     scripts/ooxml_util.py \
     scripts/opf_canonicalize.py \
     scripts/opf_html.py \
     scripts/opf_integrity.py \
     scripts/opf_injection_scan.py \
     scripts/opf_load.py \
     scripts/opf_prompt.py \
     scripts/playbook_registry.py \
     scripts/playbook_validation.py \
     scripts/policy_load.py \
     scripts/precedent_citations.py \
     scripts/preflight_pass.py \
     scripts/primary_review_pass.py \
     scripts/reconciliation.py \
     scripts/redline_block_apply.py \
     scripts/redline_generate.py \
     scripts/redline_projections.py \
     scripts/regex_safety.py \
     scripts/replacement_text_enforcement.py \
     scripts/review_knowledge.py \
     scripts/review_spine.py \
     scripts/seed_active_bundle.py \
     scripts/text_fold.py \
     scripts/

# Repo-root data the backend and those modules read at runtime:
#   playbooks/     — playbook_registry loads registry.json and the output
#                    schemas when it is first imported; without it the app
#                    cannot start.
#   model-policy/  — model_client's pinned model ids, capabilities, output
#                    caps and the admin-selectable catalogue.
# Deliberately NOT copied: standard-forms/ (only the test-only
# synthetic-generic playbook names it), infra/fixtures/ (the DTS bootstrap
# uploads it to object storage; a registry `mock_output_key` is an S3 key, not
# a file) and tests/ (canonicalize's golden-hash path is CLI-only).
COPY playbooks playbooks
COPY model-policy model-policy

# ---- build-time metadata (issue #42) ----
# The source revision is an explicit input of this final, cheap block, and of
# nothing above it. Declared here, after every install and copy step, so a new
# revision never re-runs the pip install, and no cached layer can carry an
# older revision forward. (They used to be declared above FROM and consumed
# straight after it, above the pip install. A deploy then served a stale
# GET /version stamp with the correct --build-arg on its command line.)
#
#   COMMIT_SHA   — the ARTIFACT REVISION: the full commit SHA the image was
#                  built from (ci-pipeline.yml passes `git rev-parse HEAD`).
#                  Also the standard OCI revision annotation, and written to
#                  /etc/image-revision by the RUN below, which makes it a
#                  direct cache input of a real filesystem layer.
#   VERSION      — the RELEASE/BUILD identifier, `<short-sha>-<UTC time>`.
#                  Kept separate: "which artifact" is not "which build of it".
#   IMAGE_DIGEST — the immutable digest of the pushed manifest. Not knowable
#                  at build time; supplied at runtime when known.
#
# No deployment identifier is baked in: which deployment event launched an
# image is a runtime fact, not an artifact fact. The API reads these at
# runtime via os.environ (GET /version, backend/src/main.py).
ARG VERSION=dev
ARG COMMIT_SHA=unknown
ARG IMAGE_DIGEST=unknown
LABEL org.opencontainers.image.revision=${COMMIT_SHA} \
      org.opencontainers.image.version=${VERSION}
ENV VERSION=${VERSION}
ENV COMMIT_SHA=${COMMIT_SHA}
ENV IMAGE_DIGEST=${IMAGE_DIGEST}
RUN printf '%s\n' "${COMMIT_SHA}" > /etc/image-revision

# /app/backend -> `src.main:app` resolves from the working directory.
WORKDIR /app/backend

# App Runner expects the container to listen on port 8080
EXPOSE 8080

# Run FastAPI via uvicorn.
# --no-access-log avoids logging request bodies (no document substance in logs).
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
