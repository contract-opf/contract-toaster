# DTS deployment (Docker Compose)

A parallel, non-AWS deployment target for contract-toaster, from the **same
codebase** as the AWS App Runner deployment. Adapters are selected by
environment variables at process start (see `backend/src/config.py`); the AWS
path is unchanged when these are unset.

| Concern | AWS target | DTS target |
|---|---|---|
| Object store | S3 | **MinIO** (`S3_ENDPOINT_URL`) |
| Key-value store | DynamoDB | **DynamoDB-Local** (`DYNAMODB_ENDPOINT_URL`) |
| Auth | Cognito | **username/password** (`AUTH_MODE=password`) |
| Pipeline | Step Functions | **in-process worker** (`PIPELINE_RUNNER=inprocess`) |
| Model | Bedrock | **OpenRouter** (`MODEL_PROVIDER=openrouter`) |

## Pipeline: real vs. mock

The review pipeline the runner executes is selected by `MODEL_PROVIDER`
(`backend/src/config.py` → `pipeline_runner.run_pipeline`):

- **`MODEL_PROVIDER=openrouter` (the default in both compose files): the REAL
  pipeline runs** — `run_real_pipeline` drives the `scripts/` review chain
  against a live `OpenRouterModelClient`. **`OPENROUTER_API_KEY` is required in
  this mode:** the client rejects an empty key, so with it unset/wrong every
  review fails to `ERROR` / `MANUAL_REVIEW_REQUIRED` (it does **not** fall back
  to the mock). Reviews are billed to your OpenRouter account per
  `model-policy/openrouter.json`.
- **Any other `MODEL_PROVIDER` value (or unset): the mock pipeline runs** —
  `run_mock_pipeline` returns the canned fixture
  (`"Mock review: canned REQUEST_CHANGE result."`) with no model call. This is
  the fully-offline path; no key needed.

## Run it

```bash
cp deploy/dts/.env.example deploy/dts/.env
# edit deploy/dts/.env: set DEMO_TOKEN_SECRET (e.g. `openssl rand -hex 32`)

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
DynamoDB tables (+ the `reviews.owner_sub-index` GSI), the uploads/outputs
buckets, seeds the mock eiaa redline fixture into MinIO, seeds the demo users,
and seeds a minimal active eiaa playbook bundle.

## Not yet included (follow-ups)

- **Retention purge scheduler** (APScheduler tick calling the existing retention
  logic) — not on the Phase 1 upload→download path; add before real data.
- **Phase 2** real pipeline wiring + OpenRouter pricing branch in the spend
  model.
