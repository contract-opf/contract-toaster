#!/usr/bin/env bash
# coolify-autopull: roll the DTS Coolify service forward FROM THE BOX ITSELF.
#
# WHY. .github/workflows/dts-image-publish.yml publishes the app images on
# every green main, but its `deploy` job has to reach Coolify's API from a
# GitHub-hosted runner. When the Coolify host sits behind Cloudflare with Bot
# Fight Mode on, that call is answered by a challenge page (HTTP 403 "Just a
# moment..."), which no Access service token gets past and no WAF rule can
# skip on the free plan. This script inverts the direction: the box polls
# GHCR, and when a new image appears it calls Coolify's API on localhost. No
# inbound request, nothing crosses Cloudflare.
#
# WHAT ONE RUN DOES (a systemd timer runs it every few minutes):
#   1. `docker pull` each image in AUTOPULL_IMAGES, using the box's existing
#      GHCR login, and read its registry digest and its
#      org.opencontainers.image.revision label.
#   2. If the images disagree on that revision, the publish workflow is
#      half-way through (its backend and frontend legs push separately), so
#      wait for the next run rather than restart onto a mixed pair.
#   3. Compare the digests with the ones last rolled out (the state file). No
#      state yet: record them and do NOT restart, so installing this never
#      causes a surprise restart. Unchanged: do nothing.
#   4. Changed: POST Coolify's services restart endpoint with `latest=true`
#      (the API twin of "Restart (pull latest)"), and record the new digests
#      only if Coolify accepted it, so a failure is retried on the next run.
#
# Configuration comes from the environment (the systemd unit reads
# /etc/coolify-autopull.env; see coolify-autopull.env.example):
#   AUTOPULL_IMAGES       space-separated image refs to watch (required)
#   COOLIFY_SERVICE_UUID  last path segment of the service's Coolify page (required)
#   COOLIFY_API_TOKEN     Coolify -> Keys & Tokens -> API tokens (required)
#   COOLIFY_URL           default http://127.0.0.1:8000 (Coolify's own port)
#   AUTOPULL_STATE_FILE   default /var/lib/coolify-autopull/deployed
#
# Exit status: 0 when there was nothing to do, a restart was accepted, or the
# run was deferred; 1 when a pull or the restart call failed.
#
# Install and operate: deploy/dts/README.md#automatic-roll-forward-from-the-box

set -euo pipefail

: "${AUTOPULL_IMAGES:?set AUTOPULL_IMAGES to the image refs to watch}"
: "${COOLIFY_SERVICE_UUID:?set COOLIFY_SERVICE_UUID}"
: "${COOLIFY_API_TOKEN:?set COOLIFY_API_TOKEN}"
COOLIFY_URL="${COOLIFY_URL:-http://127.0.0.1:8000}"
STATE_FILE="${AUTOPULL_STATE_FILE:-/var/lib/coolify-autopull/deployed}"
REVISION_LABEL="org.opencontainers.image.revision"

log() { printf 'coolify-autopull: %s\n' "$*"; }

mkdir -p "$(dirname "${STATE_FILE}")"

# One run at a time. flock is util-linux, so it is there on any Linux box;
# without it (e.g. a developer's Mac running the tests) just carry on.
if command -v flock >/dev/null 2>&1; then
  exec 9>"${STATE_FILE}.lock"
  flock -n 9 || { log "another run is in progress; skipping"; exit 0; }
fi

# The repository part of a ref: everything before the tag, where the tag is a
# ':' in the last path segment (a registry port is not a tag).
repo_of() {
  local ref="$1" last="${1##*/}"
  if [[ "${last}" == *:* ]]; then printf '%s' "${ref%:*}"; else printf '%s' "${ref}"; fi
}

current=""
revision=""
for image in ${AUTOPULL_IMAGES}; do
  if ! docker pull -q "${image}" >/dev/null; then
    log "could not pull ${image}; will retry on the next run"
    exit 1
  fi
  repo="$(repo_of "${image}")"
  digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image}" \
            | awk -v p="${repo}@" 'index($0, p) == 1 { print substr($0, length(p) + 1); exit }')"
  if [ -z "${digest}" ]; then
    log "no registry digest for ${image}; will retry on the next run"
    exit 1
  fi
  rev="$(docker image inspect --format "{{index .Config.Labels \"${REVISION_LABEL}\"}}" "${image}")"
  if [ -z "${revision}" ]; then
    revision="${rev}"
  elif [ "${rev}" != "${revision}" ]; then
    log "images disagree on ${REVISION_LABEL} (${revision:-none} vs ${rev:-none}); publish still in progress, waiting"
    exit 0
  fi
  current+="${image} ${digest}"$'\n'
done

if [ ! -f "${STATE_FILE}" ]; then
  printf '%s' "${current}" > "${STATE_FILE}"
  log "initialised at revision ${revision:-unknown}; not restarting (the next new image will roll out)"
  exit 0
fi

# Both sides through $(...), which strips trailing newlines from each alike.
if [ "$(cat "${STATE_FILE}")" = "$(printf '%s' "${current}")" ]; then
  exit 0
fi

log "new images at revision ${revision:-unknown}; asking Coolify to pull and restart"
body="$(mktemp)"
trap 'rm -f "${body}"' EXIT
# The token goes in through a header file, never on the command line, where
# any local user could read it from the process list.
code="$(curl -sS --max-time 60 -o "${body}" -w '%{http_code}' -X POST \
          -H @<(printf 'Authorization: Bearer %s\n' "${COOLIFY_API_TOKEN}") \
          -H 'Accept: application/json' \
          "${COOLIFY_URL%/}/api/v1/services/${COOLIFY_SERVICE_UUID}/restart?latest=true")" || true
case "${code}" in
  2??)
    printf '%s' "${current}" > "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "${STATE_FILE}"
    log "Coolify accepted the restart (HTTP ${code}); rolled forward to ${revision:-unknown}"
    ;;
  *)
    log "Coolify refused the restart (HTTP ${code:-none}): $(head -c 300 "${body}" | tr -s ' \n' ' ')"
    log "will retry on the next run"
    exit 1
    ;;
esac
