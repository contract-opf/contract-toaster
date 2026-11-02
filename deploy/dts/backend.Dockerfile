# DTS backend image — built from the REPO ROOT context (unlike the AWS
# backend/Dockerfile, which builds from backend/ and is left untouched so the
# AWS CI build `docker build ... backend/` keeps working).
#
# Mirrors the repo tree under /app so:
#   - `src.main:app` imports resolve (PYTHONPATH=/app/backend),
#   - model_client.py's REPO_ROOT (parents[2] of /app/backend/src/...) == /app,
#     so /app/model-policy/openrouter.json resolves (Phase 2),
#   - the real scripts/ pipeline chain is importable (Phase 2).
#
# Build:  docker build -f deploy/dts/backend.Dockerfile -t contract-toaster-dts-backend .
FROM python:3.13-slim

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/src backend/src
COPY scripts scripts
COPY model-policy model-policy
COPY playbooks playbooks
COPY standard-forms standard-forms
COPY infra/fixtures infra/fixtures
COPY deploy/dts/bootstrap.py deploy/dts/bootstrap.py

# /app/backend -> `src.*`; /app/scripts -> the real pipeline modules (Phase 2,
# which import each other as top-level modules).
ENV PYTHONPATH=/app/backend:/app/scripts

# ---- build-time metadata (issues #424, #42) ----
# Mirrors the metadata block at the end of backend/Dockerfile (the AWS
# target). GET /version reads these at runtime via os.environ
# (backend/src/main.py); without them the endpoint always hits its fallbacks
# and the deployed footer reads "Version dev (unknown)".
#
# The defaults deliberately match those os.environ.get fallbacks, so a build
# with no --build-arg degrades exactly as before. Declared HERE, after every
# install and COPY layer, so a per-commit COMMIT_SHA does not invalidate the
# cached pip-install layer above, and is a direct input of this final block.
#
# COMMIT_SHA is the ARTIFACT REVISION: the full commit SHA
# (dts-image-publish.yml passes TARGET_SHA). It is also the standard OCI
# revision annotation, and the RUN below writes it to /etc/image-revision so
# it is a direct cache input of a real filesystem layer: a warm-cache rebuild
# with a new revision cannot reuse the old layer (issue #42). VERSION is the
# separate RELEASE/BUILD identifier (`<short-sha>-<UTC time>`). No deployment
# identifier is baked in; which deployment launched an image is a runtime fact.
#
# IMAGE_DIGEST stays `unknown` on this deploy target: the digest is the digest
# of the PUSHED manifest, so it is not knowable at build time, and baking it in
# would change the very digest being recorded. The publish workflow echoes the
# real digest after `docker push` so an operator can supply it as a runtime
# env var instead (see deploy/dts/docker-compose.coolify.yml).
ARG VERSION=dev
ARG COMMIT_SHA=unknown
ARG IMAGE_DIGEST=unknown
LABEL org.opencontainers.image.revision=${COMMIT_SHA} \
      org.opencontainers.image.version=${VERSION}
ENV VERSION=${VERSION}
ENV COMMIT_SHA=${COMMIT_SHA}
ENV IMAGE_DIGEST=${IMAGE_DIGEST}
RUN printf '%s\n' "${COMMIT_SHA}" > /etc/image-revision

WORKDIR /app/backend
EXPOSE 8080

# --no-access-log: never log request/response bodies (no document substance).
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
