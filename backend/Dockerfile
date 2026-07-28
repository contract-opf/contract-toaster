# ---- build-time metadata ----
ARG VERSION=dev
ARG COMMIT_SHA=unknown
ARG IMAGE_DIGEST=unknown

FROM python:3.12-slim

# Bake version metadata into the image at build time.
# The API reads these at runtime via os.environ.
ARG VERSION
ARG COMMIT_SHA
ARG IMAGE_DIGEST
ENV VERSION=${VERSION}
ENV COMMIT_SHA=${COMMIT_SHA}
ENV IMAGE_DIGEST=${IMAGE_DIGEST}

WORKDIR /app

# Install Python dependencies (pinned, no extras we don't need)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

# App Runner expects the container to listen on port 8080
EXPOSE 8080

# Run FastAPI via uvicorn.
# --no-access-log avoids logging request bodies (no document substance in logs).
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
