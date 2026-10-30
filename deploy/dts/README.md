# Docker Compose deployment

A parallel, non-AWS deployment target for contract-toaster, from the **same
codebase** as the AWS App Runner deployment. Adapters are selected by
environment variables at process start (see `backend/src/config.py`); the AWS
path is unchanged when these are unset.

| Concern | AWS target | Docker Compose target |
|---|---|---|
| Object store | S3 | **MinIO** (`S3_ENDPOINT_URL`) |
| Key-value store | DynamoDB | **DynamoDB-Local** (`DYNAMODB_ENDPOINT_URL`) |
| Auth | Cognito | **username/password** (`AUTH_MODE=password`) |
| Pipeline | Step Functions | **in-process worker** (`PIPELINE_RUNNER=inprocess`) |
| Model | Bedrock | **OpenRouter** (`MODEL_PROVIDER=openrouter`) |

## Phase status

Both phases have landed — this stack runs the **real** review chain.

- **Phase 1** (#256): the mock pipeline in-process end-to-end (upload →
  PENDING→RUNNING→DONE → download), proving the deployment abstraction.
- **Phase 2** (#259): the in-process runner's body is the real `scripts/`
  chain driven by `OpenRouterModelClient`. `MODEL_PROVIDER=openrouter` in
  `docker-compose.yml` selects it, so **the OpenRouter key is now required**.

`run_mock_pipeline` still exists and is still what you get with
`MODEL_PROVIDER` unset or set to anything else — but nothing in `deploy/dts/`
selects it, and **there is no automatic fallback to it**. Without a reachable
key every review terminates in `ERROR` at `stage=build_model_client`; with a
wrong one, at `stage=run_review`.

## The OpenRouter key

Supply it from **either** source — the admin-set key wins when both exist:

| Source | Where | Rotate without restart? |
|---|---|---|
| Admin UI (preferred) | "Model & API key" tab, admin sign-in | Yes |
| `OPENROUTER_API_KEY` | `deploy/dts/.env` | No — needs a restart |

The key is instance-wide: one key, every user's reviews, one OpenRouter bill.
The admin-set key is stored in `MODEL_SETTINGS_TABLE` and is **write-only** —
no endpoint returns it, masked or otherwise. The UI shows only a
non-reversible fingerprint (`3f9a2c71`, the first eight hex of a salted
SHA-256), enough to tell two keys apart and to confirm a rotation took
effect. A lost key is regenerated at
[openrouter.ai/keys](https://openrouter.ai/keys), not recovered here.
Clearing it in the UI reverts the instance to `OPENROUTER_API_KEY`.

## Run it

```bash
cp deploy/dts/.env.example deploy/dts/.env
# edit deploy/dts/.env: set DEMO_TOKEN_SECRET (e.g. `openssl rand -hex 32`)
# optionally set OPENROUTER_API_KEY, or leave it blank and use the admin tab

docker compose -f deploy/dts/docker-compose.yml --env-file deploy/dts/.env up --build
```

- SPA: <http://localhost:8081> (sign in with **admin/admin** or **user/user**)
- API: <http://localhost:8080>
- MinIO console: <http://localhost:9001> (local / localsecret)

Durable across restarts (named volumes `ddb-data`, `minio-data`); `docker
compose down` keeps data, `down -v` wipes it.

### Downloads (presigned URLs) — no host setup required

Output downloads use S3 presigned URLs, which are host-bound (the signature
commits to the endpoint host used when the URL was generated). The backend's
other S3 calls use the compose-internal `S3_ENDPOINT_URL=http://minio:9000`,
which a browser on the host cannot resolve — so downloads are presigned
against a *separate* host-reachable endpoint instead:
`S3_PUBLIC_ENDPOINT_URL=http://localhost:9000` (set in `docker-compose.yml`;
MinIO's port 9000 is published to the host). Every other S3 call is
unaffected. No `/etc/hosts` edit is needed.

Tradeoff: this assumes the browser reaches the compose host at `localhost`
(true for local `docker compose up`). A remote/non-localhost deployment would
need `S3_PUBLIC_ENDPOINT_URL` set to that host's externally-reachable address
instead (e.g. behind an nginx path-route or a real DNS name) — not needed for
the local Phase 1 quickstart this README covers.

### MinIO on a public domain: credentials and an edge rule

In `docker-compose.coolify.yml`, MinIO sits on its own public domain, because
the browser follows presigned download URLs straight to it. Two consequences:

**Its login must be secret.** The local compose uses `local` / `localsecret`
(fine on a laptop). The Coolify compose names Coolify magic variables
instead, `${SERVICE_USER_MINIO}` and `${SERVICE_PASSWORD_64_MINIO}`. Coolify
generates them on first save, and MinIO's root login and the backend's
`AWS_*` pair both read them, so they always match. Changing the pair on an
existing deployment keeps every stored object. Links presigned before the
restart stop working, and new ones work. On any other host, set both
variables yourself. `tests/test_dts_minio_credentials.py` fails if a literal
value comes back.

**Only presigned downloads need the public domain.** The backend reaches MinIO
on the internal network, and presigns only `GetObject`. Its links carry
`Signature=` and `Expires=` (or `X-Amz-Signature=` and `X-Amz-Expires=` with
SigV4). If the domain is behind Cloudflare, a custom WAF rule (action:
**Block**) can refuse everything else. That covers uploads, deletes, listings,
admin calls and any request signed with a header:

```
(http.host eq "<minio-domain>" and not (
  http.request.method in {"GET" "HEAD"}
  and http.request.uri.query contains "Signature="
  and http.request.uri.query contains "Expires="
  and not ends_with(http.request.uri.path, "/")
))
```

This is defence in depth, not a replacement for the secret login. Someone
holding the login could still presign a GET for themselves.

## What the bootstrap does

`bootstrap.py` (a one-shot compose service the backend waits on) creates the
DynamoDB tables (+ the `reviews.owner_sub-index`, `reviews.status-index` and
`reviews.playbook_hash-index` GSIs) and the uploads/outputs
buckets, seeds any registry-declared mock-pipeline redline fixture into MinIO,
seeds the demo users, and installs + activates the playbook the image ships
with — **Synthetic NDA Sample**, the only contract type a fresh deployment
serves.

It seeds no other playbook row. Issue #515 removed a hardcoded, tenant-named
orphan row that every fresh deployment used to write: the catalog is
registry-filtered so it was never served, but it sat in `PLAYBOOKS_TABLE` of
the one path a public adopter copies and runs, and this README used to promise
it as a contract type that was not actually there.

Any other playbook — including a tenant's own — arrives through the ordinary
install/upload path in the admin UI, never through a bootstrap seed.

## Retention purge cadence

The backend runs the retention purge sweep on a timer on this target (issue
#509, `backend/src/purge_scheduler.py`). It calls the same
`retention.run_purge_sweep_now` the admin API drives — no separate purge logic
— so each review's own snapshotted window and its legal hold are honoured
exactly as they are everywhere else.

| Variable | Default | Meaning |
|---|---|---|
| `PURGE_SWEEP_INTERVAL_SECONDS` | `3600` | How often to sweep. Retention windows are measured in days, so an hour is fine enough that "purged on schedule" is true to within a rounding error; sweeping more often just re-scans the table. |
| `PURGE_SWEEP_ENABLED` | `1` | Set to `0` to stop the cadence without editing code. |

It does **not** run on the AWS target, where
`infra/lambda/purge_worker/handler.py` owns the cadence — starting both would
double-sweep the same rows.

Before this landed, nothing on this target ever invoked the sweep, so an
operator who set a retention window saw the *preview* work and reasonably
concluded data was being purged. It was not.

## The backend refuses to start with an incomplete environment

At startup the backend asserts that every environment variable the code
dereferences with `os.environ[...]` — i.e. required, no default — is set. If
any is missing it logs **every** missing name in one line and exits non-zero,
so the container restart-loops instead of serving:

```
STARTUP: refusing to start — required environment variable(s) not set: USER_PREFERENCES_TABLE. …
```

Only **names** are ever logged, never values. The required set is derived
from the source by an AST pass at boot (`backend/src/startup_checks.py`), not
from a list, so it cannot drift away from the code.

Fix it by setting the named variables in the deployment's environment. The
compose files in this directory already carry all of them; a Coolify-managed
compose is edited in Coolify's UI, and a variable dropped there is exactly
the failure this check exists to make loud (issue #654: `USER_PREFERENCES_TABLE`
was missing from the live compose, the container started anyway, and
`GET /api/me/preferences` 500'd on every request while the Review tab just
showed `Internal (unavailable)` — indistinguishable from an unbuilt feature).

## Our own legal entities (`ENTITY_ROSTER_TABLE`, issue #678)

The Settings tab holds the deployment's roster of OUR legal entity names —
the ~25 entities any of which can be the contracting party on a document we
review. At review time the roster is unioned with the governing playbook's
own `perspective.party` into one flat set, so a document naming a
subsidiary, a former name or a d/b/a is still recognised as us.

It is **optional configuration and degrades quietly**: with
`ENTITY_ROSTER_TABLE` unset the Settings panel says the deployment has no
roster store, writes are refused with a 400, and every review recognises the
playbook's own party alone — the behaviour that existed before #678. That is
deliberately not a startup refusal (the variable is read with `.get`, so the
AST pass above does not classify it as required), but a Coolify-managed
compose that omits it silently loses the feature: add
`ENTITY_ROSTER_TABLE: contract-toaster-entity-roster-dts` in Coolify's UI to
match the compose files here, then restart the backend — `bootstrap` creates
the table, and since issue #59 the backend creates it at **startup** too if
bootstrap has not (`backend/src/startup_checks.py::ensure_entity_roster_table`,
called from the ASGI lifespan: one `describe_table`, and a `create_table`
only when it is genuinely missing AND `DEPLOY_TARGET=dts`).

That startup helper replaced a `create_table` the backend used to issue on
**every roster read** (issue #59, audit finding F9) — a write-class DynamoDB
call on `GET /api/admin/entity-roster` and on every review's prompt assembly,
which under throttling was pure log noise on a read path. Nothing in
`backend/src/entity_roster.py` provisions anything any more. On the AWS
target, where the table is CDK-managed, the same helper **refuses the boot**
rather than creating one; here it just creates it, tolerating the race with
`bootstrap` if both run at once.

## Which commit is this image? (issue #42)

Both images end in a small metadata block that takes the source revision as
a direct input, after every install and `COPY` step. A warm-cache rebuild for
a new commit therefore reuses the dependency layers but cannot reuse the
revision layer. Before #42, an image could keep an older `GET /version`
stamp even though the right `--build-arg` was passed.

- **Artifact revision.** `COMMIT_SHA` is the full commit SHA. The publish
  workflow passes the `TARGET_SHA` that passed CI. It is recorded in three
  places: the `org.opencontainers.image.revision` label, the file
  `/etc/image-revision`, and (on the backend) the `ENV` that
  `GET /version` serves as `commit`. Read it from an image with
  `docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' <image>`.
- **Release/build identifier.** `VERSION` is `<short-sha>-<UTC build time>`.
  It is a separate field: the `org.opencontainers.image.version` label and
  `GET /version`'s `version`.
- **Image digest.** This is the digest of the pushed manifest, so the build
  cannot know it. The publish job prints it, and the deployment supplies it as
  `IMAGE_DIGEST` when it is wanted.
- **Deployment identity.** Which deployment started a container is never baked
  into an image. It is a runtime fact.

When building by hand, pass `COMMIT_SHA=$(git rev-parse HEAD)`.

The MinIO image below carries the same block, with `VERSION` set to the MinIO
release instead.

## MinIO image

MinIO stopped publishing community images. Both `quay.io/minio/minio` and
`docker.io/minio/minio` answer 401 for the release this target pinned, so
`minio/minio:RELEASE.2025-04-08T15-41-24Z` can no longer be pulled.
`minio/Dockerfile` rebuilds that **same release** from upstream source:

- It clones the tag `RELEASE.2025-04-08T15-41-24Z` and checks that it still
  points at commit `d0cada583fce88f60cb276ddfb06f5cb16820069`.
- It builds with upstream's own release ldflags, so `minio --version` reports
  that release and commit, exactly as the official binary did. The build fails
  if it doesn't.
- The runtime base is `alpine`, and the container runs as root, as the
  upstream image did.

The release is the same, so the on-disk format is the same. An existing
`minio-data` volume needs no migration. The build doesn't use dl.min.io
binaries, which could be withdrawn the same way.

**Licence.** MinIO is AGPL-3.0-or-later. This image redistributes it
unmodified, in its own container. The corresponding source is
<https://github.com/minio/minio/tree/RELEASE.2025-04-08T15-41-24Z>. The
upstream `LICENSE` and `CREDITS` ship in the image under `/licenses`.

**Where each compose file gets it.**

| File | MinIO image |
|---|---|
| `docker-compose.yml` | Built locally from `minio/Dockerfile` on `up --build`, tagged `contract-toaster-dts-minio:<release>`. The first build compiles MinIO (a few minutes). |
| `docker-compose.coolify.yml` | `ghcr.io/${GHCR_OWNER}/contract-toaster-dts-minio:<release>`, published by `.github/workflows/dts-image-publish.yml`. |

**The tag is the release, and it is immutable.** The publish workflow reads
`ARG MINIO_TAG` from the Dockerfile and pushes that tag only, with no
`:latest`. It publishes the tag when it is absent. It skips the tag when the
published image was built from this exact Dockerfile, which it records in the
`contract-toaster.dockerfile.sha256` label. It fails when the Dockerfile
changed under an existing tag.

To move to another MinIO release:

1. Change `MINIO_TAG` and `MINIO_COMMIT` together. Get the commit with
   `git ls-remote https://github.com/minio/minio 'refs/tags/<tag>^{}'`.
2. Update both compose files.

`tests/test_dts_minio_image.py` keeps the three files in step. Check MinIO's
release notes for format changes before upgrading a volume that holds data.

`amazon/dynamodb-local:2.5.2` is the only other third-party image here. AWS
still publishes it, both on Docker Hub and on
`public.ecr.aws/aws-dynamodb-local/aws-dynamodb-local`, so it stays as it is.

## Automatic roll-forward from the box

`.github/workflows/dts-image-publish.yml` publishes the backend and frontend
images on every green `main`. Its `deploy` job then calls Coolify's API from a
GitHub-hosted runner. If the Coolify host is behind Cloudflare with **Bot
Fight Mode** on, that call never arrives: Cloudflare answers with a challenge
page (HTTP 403, "Just a moment..."). Neither an Access service token nor a WAF
skip rule gets past it on the free plan.

`autopull/` reverses the direction. A systemd timer on the box runs
`coolify-autopull` every five minutes. Each run:

1. Pulls the watched `:latest` images with the box's existing GHCR login.
2. Checks that the images agree on `org.opencontainers.image.revision`. If
   they don't, the publish run is half-way through, so it waits for the next
   run.
3. When the digests have changed since the last rollout, POSTs Coolify's
   `/api/v1/services/<uuid>/restart?latest=true` on `127.0.0.1:8000`.
4. Waits (`AUTOPULL_VERIFY_SECONDS`, default 420) until the service's backend
   and frontend containers run the new revision and report `healthy` (the
   compose healthchecks). Only then does it record the rollout.
5. If they never get there, **rolls back**: it re-tags the watched refs to the
   digests last rolled out, which are still in the box's image store, and
   restarts with `latest=false`, so Coolify recreates the containers from them
   without pulling. The bad digests go in `deployed.bad`. Later runs hold at the
   previous revision until a newer image is published.

Nothing inbound is needed, and Cloudflare stays as it is. The first run only
records the current digests; it never restarts. A refused restart is retried
on the next run.

**Install (once, as root on the Coolify host):**

```bash
git clone --depth 1 https://github.com/<owner>/contract-toaster /tmp/ct
install -m 0755 /tmp/ct/deploy/dts/autopull/coolify-autopull.sh /usr/local/bin/coolify-autopull
install -m 0644 /tmp/ct/deploy/dts/autopull/coolify-autopull.service /tmp/ct/deploy/dts/autopull/coolify-autopull.timer /etc/systemd/system/
install -m 0600 /tmp/ct/deploy/dts/autopull/coolify-autopull.env.example /etc/coolify-autopull.env
# edit /etc/coolify-autopull.env: the two image refs, the service UUID, an API token
systemctl daemon-reload
systemctl start coolify-autopull.service   # first run: records digests, no restart
journalctl -u coolify-autopull -n 5         # expect "initialised at revision …"
systemctl enable --now coolify-autopull.timer
```

Then set the repository variable `DTS_DEPLOY_MODE=pull`. The workflow's
`deploy` job then skips instead of failing at the edge on every run.

**Operate it:**

- `journalctl -u coolify-autopull` shows each rollout ("rolled forward to
  <sha>"), each deferral, and each refusal.
- `systemctl list-timers coolify-autopull.timer` shows the next run.
- To force a rollout of what is already published, run
  `echo stale > /var/lib/coolify-autopull/deployed` and then
  `systemctl start coolify-autopull.service`. Deleting the file instead only
  re-initialises it, without a restart.
- After a rollback the journal says `holding at the previous revision`. Fix
  forward on `main`, and the next image rolls out on its own. To retry the
  same image, delete `/var/lib/coolify-autopull/deployed.bad`.
- **Roll back by hand** to any published commit: in the Coolify compose, change
  the backend and frontend `:latest` to `:<short-sha>` (every publish also
  pushes that tag), then click Restart. Put `:latest` back to resume auto-pull.
- **Alerting.** Set `AUTOPULL_HEARTBEAT_URL` to an Uptime Kuma *Push* monitor's
  URL (heartbeat interval about 15 minutes). Every run reports `up` or `down`
  with its last log line, so a failed rollout, a rollback and a stopped timer
  all raise an alert. A run with nothing to roll out still checks that the
  backend and frontend containers are running and healthy, so the same
  monitor is the service's liveness check. An HTTP monitor from outside would
  only see the edge's sign-in page.

**Upgrade the script** the same way you installed it: re-run the three
`install` lines from a fresh checkout. Leave the env file and the state files
as they are.

**Coolify API access.** The script authenticates with a bearer token. If
Coolify's API is limited to listed IPs (Settings → API), allow the address
Coolify sees for a request from the host itself: the Docker bridge gateway,
usually `172.17.0.1`, or the `coolify` network's gateway. Otherwise the restart
is refused with a 403, which the journal shows.

`tests/test_dts_autopull.py` runs the script against fake `docker` and `curl`
binaries.

## Not yet included (follow-ups)

- **Phase 2** real pipeline wiring + OpenRouter pricing branch in the spend
  model.
- **`Strict-Transport-Security` is yours to set at the TLS hop.**
  `nginx.conf` serves the SPA's security headers (CSP, `nosniff`,
  `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy` —
  issues #387 and #57) but deliberately sets NO HSTS: its single server block
  is a plaintext `listen 8080;` listener and TLS terminates upstream
  (Coolify/Traefik). Browsers ignore HSTS on non-secure responses, so it has
  to be added by whatever terminates TLS in front of the container —
  `max-age=63072000; includeSubDomains; preload` is the value the managed
  target uses. `tests/test_infra_hardening_headers_57.py` flips to REQUIRING
  HSTS in `nginx.conf` if a `listen … ssl` block is ever added there.
