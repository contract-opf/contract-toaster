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
#      causes a surprise restart. Unchanged: only check that the service's
#      containers are running and healthy, and report it (exit 1 if not).
#   4. Changed: POST Coolify's services restart endpoint with `latest=true`
#      (the API twin of "Restart (pull latest)"). A refused call is retried on
#      the next run.
#   5. Accepted is not the same as up. Wait (AUTOPULL_VERIFY_SECONDS) until
#      every watched image runs in this service's containers at the new
#      revision, and each container is healthy (or has no healthcheck). Only
#      then record the new digests.
#   6. Not up in time: ROLL BACK. Re-tag the watched refs to the digests last
#      rolled out (still in the local image store) and restart WITHOUT
#      pulling, so Coolify recreates the containers from them. The bad digests
#      go in "${STATE_FILE}.bad": later runs hold at the previous revision
#      (re-tagging after each pull, so a manual Coolify restart does not pick
#      the bad image up again) until a newer image is published. Delete that
#      file to retry the same image.
#
# Configuration comes from the environment (the systemd unit reads
# /etc/coolify-autopull.env; see coolify-autopull.env.example):
#   AUTOPULL_IMAGES          space-separated image refs to watch (required)
#   COOLIFY_SERVICE_UUID     last path segment of the service's Coolify page (required)
#   COOLIFY_API_TOKEN        Coolify -> Keys & Tokens -> API tokens (required)
#   COOLIFY_URL              default http://127.0.0.1:8000 (Coolify's own port)
#   AUTOPULL_STATE_FILE      default /var/lib/coolify-autopull/deployed
#   AUTOPULL_VERIFY_SECONDS  default 420; how long a rollout may take to come
#                            up healthy before it is rolled back. 0 turns
#                            verification and rollback off.
#   AUTOPULL_VERIFY_INTERVAL default 10; seconds between checks
#   AUTOPULL_HEARTBEAT_URL   optional; an Uptime Kuma push URL (or anything
#                            that takes ?status=up|down&msg=). Every run
#                            reports up or down, so a stopped timer shows as
#                            a missed heartbeat too.
#
# Exit status: 0 when there was nothing to do, a rollout came up, or the run
# was deferred; 1 when a pull, a restart call or a rollout failed, or the
# service is being held at the previous revision.
#
# Install and operate: deploy/dts/README.md#automatic-roll-forward-from-the-box

set -euo pipefail

: "${AUTOPULL_IMAGES:?set AUTOPULL_IMAGES to the image refs to watch}"
: "${COOLIFY_SERVICE_UUID:?set COOLIFY_SERVICE_UUID}"
: "${COOLIFY_API_TOKEN:?set COOLIFY_API_TOKEN}"
COOLIFY_URL="${COOLIFY_URL:-http://127.0.0.1:8000}"
STATE_FILE="${AUTOPULL_STATE_FILE:-/var/lib/coolify-autopull/deployed}"
BAD_FILE="${STATE_FILE}.bad"
VERIFY_SECONDS="${AUTOPULL_VERIFY_SECONDS:-420}"
VERIFY_INTERVAL="${AUTOPULL_VERIFY_INTERVAL:-10}"
HEARTBEAT_URL="${AUTOPULL_HEARTBEAT_URL:-}"
REVISION_LABEL="org.opencontainers.image.revision"

last_msg="ok"
log() { last_msg="$*"; printf 'coolify-autopull: %s\n' "$*"; }

# Report every run's outcome, so the monitor also notices when runs stop.
body=""
on_exit() {
  local status=$?
  [ -n "${body}" ] && rm -f "${body}"
  if [ -n "${HEARTBEAT_URL}" ]; then
    local up=up
    [ "${status}" -eq 0 ] || up=down
    curl -fsS --max-time 10 -o /dev/null -G \
      --data-urlencode "status=${up}" --data-urlencode "msg=${last_msg}" \
      "${HEARTBEAT_URL}" || printf 'coolify-autopull: heartbeat failed\n'
  fi
}
trap on_exit EXIT

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

# Point each watched ref back at the digests last rolled out. They are in the
# local store: this box pulled them when they rolled out.
retag_previous() {
  local image digest
  while read -r image digest; do
    [ -n "${image}" ] || continue
    docker tag "$(repo_of "${image}")@${digest}" "${image}" || return 1
  done < "${STATE_FILE}"
}

if [ -f "${BAD_FILE}" ] && [ "$(cat "${BAD_FILE}")" = "$(printf '%s' "${current}")" ]; then
  # The pull above moved the tags onto the bad image again; move them back.
  retag_previous || log "could not re-tag the previous images"
  log "holding at the previous revision: ${revision:-unknown} failed its rollout (delete ${BAD_FILE} to retry it)"
  exit 1
fi

body="$(mktemp)"
# POST Coolify's restart. The token goes in through a header file, never on
# the command line, where any local user could read it from the process list.
coolify_restart() {
  curl -sS --max-time 60 -o "${body}" -w '%{http_code}' -X POST \
    -H @<(printf 'Authorization: Bearer %s\n' "${COOLIFY_API_TOKEN}") \
    -H 'Accept: application/json' \
    "${COOLIFY_URL%/}/api/v1/services/${COOLIFY_SERVICE_UUID}/restart?latest=$1" || true
}

# True once every watched image runs in this service's containers at the new
# revision and none of them is unhealthy or still starting. Containers made
# from an older image no longer show the watched tag (it moved), so they are
# not counted.
rollout_is_up() {
  local image name ref rev health state seen
  for image in ${AUTOPULL_IMAGES}; do
    seen=0
    while IFS=$'\t' read -r name ref; do
      [ "${ref}" = "${image}" ] || continue
      [[ "${name}" == *"-${COOLIFY_SERVICE_UUID}" ]] || continue
      IFS='|' read -r rev health state < <(docker inspect --format \
        "{{index .Config.Labels \"${REVISION_LABEL}\"}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.Status}}" \
        "${name}")
      # A one-shot job (the bootstrap) that already exited is not a server.
      [ "${state}" = "running" ] || continue
      [ "${rev}" = "${revision}" ] || return 1
      case "${health}" in healthy|none) ;; *) return 1 ;; esac
      seen=1
    done < <(docker ps --format '{{.Names}}{{"\t"}}{{.Image}}')
    [ "${seen}" -eq 1 ] || return 1
  done
}

# Both sides through $(...), which strips trailing newlines from each alike.
if [ "$(cat "${STATE_FILE}")" = "$(printf '%s' "${current}")" ]; then
  # Nothing to roll out, but still say whether the service is up, so the
  # heartbeat doubles as a liveness check (an HTTP monitor from outside only
  # sees the edge's sign-in page). Report only; never restart from here.
  if [ "${VERIFY_SECONDS}" -ne 0 ] && ! rollout_is_up; then
    log "revision ${revision:-unknown} is deployed but a container is not running and healthy"
    exit 1
  fi
  last_msg="up to date and healthy at revision ${revision:-unknown}"
  exit 0
fi

log "new images at revision ${revision:-unknown}; asking Coolify to pull and restart"
code="$(coolify_restart true)"
case "${code}" in
  2??) log "Coolify accepted the restart (HTTP ${code})" ;;
  *)
    log "Coolify refused the restart (HTTP ${code:-none}): $(head -c 300 "${body}" | tr -s ' \n' ' ')"
    log "will retry on the next run"
    exit 1
    ;;
esac

record() {
  printf '%s' "${current}" > "${STATE_FILE}.tmp"
  mv "${STATE_FILE}.tmp" "${STATE_FILE}"
  rm -f "${BAD_FILE}"
}

if [ "${VERIFY_SECONDS}" -eq 0 ]; then
  record
  log "rolled forward to ${revision:-unknown} (not verified: AUTOPULL_VERIFY_SECONDS=0)"
  exit 0
fi

deadline=$((SECONDS + VERIFY_SECONDS))
while :; do
  if rollout_is_up; then
    record
    log "rolled forward to ${revision:-unknown}; every watched container is up and healthy"
    exit 0
  fi
  [ "${SECONDS}" -lt "${deadline}" ] || break
  sleep "${VERIFY_INTERVAL}"
done

log "revision ${revision:-unknown} did not come up healthy within ${VERIFY_SECONDS}s; rolling back"
printf '%s' "${current}" > "${BAD_FILE}"
if ! retag_previous; then
  log "ROLLBACK FAILED: the previous images are no longer on this box; ${revision:-unknown} is still running"
  exit 1
fi
code="$(coolify_restart false)"
case "${code}" in
  2??) log "rolled back to the previous images (HTTP ${code}); ${revision:-unknown} is marked bad" ;;
  *) log "ROLLBACK FAILED: Coolify refused the restart (HTTP ${code:-none}); ${revision:-unknown} is still running" ;;
esac
exit 1
