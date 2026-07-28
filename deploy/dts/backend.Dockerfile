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
FROM python:3.12-slim

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

WORKDIR /app/backend
EXPOSE 8080

# --no-access-log: never log request/response bodies (no document substance).
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
