# MinIO server, built from upstream source for the Docker Compose deploy target.
#
# WHY THIS EXISTS. MinIO stopped publishing community images: both
# quay.io/minio/minio and docker.io/minio/minio answer 401 for the release this
# deployment pinned, so a fresh `docker compose up` could not start and a
# pull-latest restart aborted before it reached the backend and frontend. This
# file rebuilds that SAME release from source, so the on-disk format is
# identical and an existing `minio-data` volume needs no migration. It does not
# download dl.min.io binaries, which can be withdrawn the same way.
#
# LICENSE. MinIO is free software under the GNU Affero General Public License
# v3 (AGPL-3.0-or-later). This image redistributes it UNMODIFIED, in its own
# container. The corresponding source is the upstream tag below:
#
#   https://github.com/minio/minio/tree/RELEASE.2025-04-08T15-41-24Z
#   (commit d0cada583fce88f60cb276ddfb06f5cb16820069)
#
# and the upstream LICENSE and CREDITS ship inside the image under /licenses.
#
# Build:  docker build -t contract-toaster-dts-minio deploy/dts/minio
# Check:  docker run --rm contract-toaster-dts-minio --version
#
# The image tag is MINIO_TAG and is IMMUTABLE: .github/workflows/
# dts-image-publish.yml reads it from this file, publishes it once, and refuses
# to overwrite it. See deploy/dts/README.md#minio-image.

# The upstream release, and the commit its tag pointed at when this file was
# written. A tag can be moved; the commit cannot, so the build checks both.
ARG MINIO_TAG=RELEASE.2025-04-08T15-41-24Z
ARG MINIO_COMMIT=d0cada583fce88f60cb276ddfb06f5cb16820069

FROM golang:1.25-alpine AS build
ARG MINIO_TAG
ARG MINIO_COMMIT

RUN apk add --no-cache git

WORKDIR /src
RUN git clone --quiet --depth 1 --branch "${MINIO_TAG}" \
        https://github.com/minio/minio.git . \
 && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"

# Upstream's own release ldflags (buildscripts/gen-ldflags.go with
# MINIO_RELEASE=RELEASE, as its Makefile does), so `minio --version` reports
# the release tag and commit exactly as the official binary did. The argument
# is the tag's timestamp in RFC 3339: RELEASE.2025-04-08T15-41-24Z ->
# 2025-04-08T15:41:24Z. Module downloads are checked against go.sum and the Go
# checksum database. GOTOOLCHAIN=local builds with this image's Go, which is
# newer than go.mod's floor, rather than fetching another toolchain.
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN version="$(echo "${MINIO_TAG#RELEASE.}" \
        | sed -E 's/T([0-9]+)-([0-9]+)-([0-9]+)Z$/T\1:\2:\3Z/')" \
 && ldflags="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go "${version}")" \
 && go build -tags kqueue -trimpath -ldflags "${ldflags}" -o /out/minio . \
 && /out/minio --version | grep -qF "minio version ${MINIO_TAG} (commit-id=${MINIO_COMMIT})"

FROM alpine:3.22

LABEL org.opencontainers.image.title="MinIO (unmodified upstream build)" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="MinIO RELEASE.2025-04-08T15-41-24Z built from https://github.com/minio/minio (commit d0cada583fce88f60cb276ddfb06f5cb16820069), unmodified"

COPY --from=build /out/minio /usr/bin/minio
COPY --from=build /src/LICENSE /src/CREDITS /licenses/

# Runs as root, like the upstream image did, so a volume that image created
# stays writable.
EXPOSE 9000 9001
VOLUME ["/data"]

# ARTIFACT REVISION, the same block as deploy/dts/backend.Dockerfile: the full
# commit SHA of THIS repository that built the image (dts-image-publish.yml
# passes TARGET_SHA), as the OCI revision label and a direct cache input of a
# real layer (issue #42). VERSION is the release identifier; the publish
# workflow passes MINIO_TAG.
ARG VERSION=dev
ARG COMMIT_SHA=unknown
LABEL org.opencontainers.image.revision=${COMMIT_SHA} \
      org.opencontainers.image.version=${VERSION}
RUN printf '%s\n' "${COMMIT_SHA}" > /etc/image-revision

# The compose files supply `command: ["server", "/data", ...]`, which replaces
# CMD and keeps this entrypoint.
ENTRYPOINT ["/usr/bin/minio"]
CMD ["server", "/data"]
