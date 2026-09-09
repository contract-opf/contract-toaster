# DTS frontend image — builds the SPA in password-auth mode and serves it via
# nginx, which also reverse-proxies the API so the browser talks same-origin
# (no CORS). Built from the REPO ROOT context.
#
# Build:  docker build -f deploy/dts/frontend.Dockerfile -t contract-toaster-dts-frontend .
FROM node:20-slim AS build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Password-auth build: Amplify/Cognito is not configured; same-origin API base.
ENV VITE_AUTH_MODE=password
ENV VITE_API_BASE_URL=""

# ---- build-time metadata (issue #652) ----
# The SPA has to be able to say which commit IT was built from, or the
# Settings tab cannot tell "the server and the bundle agree" from "the server
# is reporting a stale stamp" — the #613 condition, which is exactly two
# plausible-looking values that disagree. Vite inlines `import.meta.env.VITE_*`
# at build time, so these must be ENV before `npm run build`; they are read by
# frontend/src/deployIdentity.ts.
#
# Same ARG names and same graceful-degrade defaults as
# deploy/dts/backend.Dockerfile, so one pair of --build-arg values stamps both
# images and an un-parameterised build degrades identically (the SPA then
# reports "cannot be checked on this build" rather than guessing).
#
# Declared HERE, after `COPY frontend/ ./` and after `npm ci`, so a per-commit
# COMMIT_SHA never invalidates the cached dependency-install layer above.
#
# IMAGE_DIGEST is deliberately absent: it is the digest of the PUSHED manifest
# and is not knowable at build time (see backend.Dockerfile), and it is a
# server-image fact that the bundle has no business restating.
ARG VERSION=dev
ARG COMMIT_SHA=unknown
ENV VITE_BUILD_VERSION=${VERSION}
ENV VITE_COMMIT_SHA=${COMMIT_SHA}

RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/dts/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html
EXPOSE 8080
