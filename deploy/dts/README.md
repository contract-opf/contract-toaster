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
the UI shows only a last-four hint (`…4f2a`), never the key. A lost key is
regenerated at [openrouter.ai/keys](https://openrouter.ai/keys), not recovered
here. Clearing it in the UI reverts the instance to `OPENROUTER_API_KEY`.

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

## What the bootstrap does

`bootstrap.py` (a one-shot compose service the backend waits on) creates the
DynamoDB tables (+ the `reviews.owner_sub-index` GSI) and the uploads/outputs
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

## Not yet included (follow-ups)

- **Phase 2** real pipeline wiring + OpenRouter pricing branch in the spend
  model.
