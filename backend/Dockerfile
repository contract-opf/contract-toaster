FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies (pinned, no extras we don't need)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

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

# App Runner expects the container to listen on port 8080
EXPOSE 8080

# Run FastAPI via uvicorn.
# --no-access-log avoids logging request bodies (no document substance in logs).
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
