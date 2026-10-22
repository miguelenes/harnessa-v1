# Harnessa

A self-hosted repository-change control plane. Milestone 1 established the modular monorepo, server-owned contracts, project-scoped identity, and explicit repository registration. Milestone 2 adds durable task orchestration, audit history, outbox events, worker leases, and recovery APIs. Milestone 3 adds a private LiteLLM gateway with explicit routing, capability checks, budget reservations, and a redacted request ledger. Milestone 5 adds the disabled-by-default rootless Podman and ACP runner boundary; agent dispatch remains gated until ACP model calls use Harnessa's budgeted ModelRouter.

## Start locally

Requirements: Podman with `podman compose` (or Docker Compose), network access for the first image/package build, and at least 4 GB available for the web build.

1. Create local configuration and replace the placeholders, especially the database password, `SECRET_KEY` (32+ random bytes), and one-time `BOOTSTRAP_TOKEN`:

   ```sh
   cp .env.example .env
   set -a && . ./.env && set +a
   ```

   Keep `DATABASE_URL`'s password in sync with `POSTGRES_PASSWORD`. `REPOSITORY_TARGETS` is a comma-separated allowlist of exact IDs, for example `acme/service-a,acme/service-b`. The default development API and web ports are 8010 and 3000; set `API_PORT` or `WEB_PORT` if needed. Keep `ALLOWED_ORIGINS` aligned with the web origin. For HTTPS deployments set `COOKIE_SECURE=true` and configure a TLS-terminating reverse proxy.

2. Build and start the six services:

   ```sh
   podman compose -f infra/compose.yaml up --build -d
   ```

The API automatically applies Alembic migrations before it starts. The LiteLLM Proxy uses `infra/litellm/config.yaml`; its default model list is empty and makes no provider calls. Provider keys, when configured, are passed only to LiteLLM, which is the only service with provider egress. The Compose worker publishes durable events, reconciles effect intents and stale model reservations, and recovers expired leases; it does not claim tasks until a real execution adapter is configured. The ACP sandbox is intended for an optional host-side worker with rootless Podman; the Compose worker has no container-engine access. Check `http://localhost:${API_PORT}/health/live`, `http://localhost:${API_PORT}/health/ready`, and `http://localhost:${WEB_PORT}`.

3. Bootstrap the first administrator once. Use the configured token and a password of at least 12 characters:

   ```sh
   curl -i http://localhost:${API_PORT}/auth/bootstrap \
     -H 'Content-Type: application/json' \
     -H "X-Bootstrap-Token: $BOOTSTRAP_TOKEN" \
     -d '{"email":"admin@example.com","password":"replace-with-a-long-password","organization_name":"My Organization"}'
   ```

   Bootstrap returns 404 when disabled or when the token is wrong, and 409 after an administrator exists. Sign in at `http://localhost:${WEB_PORT}/login`, create a project, and register a repository target already listed in `REPOSITORY_TARGETS`. Registration stores configuration only; it does not access GitHub or inspect repository contents.

4. Stop services and retain the local database volume:

   ```sh
   podman compose -f infra/compose.yaml down
   ```

   To discard the development database too, add `-v`.

## Developer commands

```sh
uv sync --all-groups
uv run ruff check apps/api tests
uv run pytest
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm test
pnpm test:integration
pnpm --filter @harnessa/web lint
pnpm --filter @harnessa/web typecheck
pnpm --filter @harnessa/web build
pnpm contracts:generate
```

Codex project-level development MCPs are documented in [the development MCP guide](docs/development/mcp-servers.md). They provide version-aware documentation and read-only GitHub research; they do not configure product-agent tools.

`pnpm contracts:generate` reads the running API OpenAPI document at `http://localhost:8010/openapi.json` by default. Set `HARNESSA_API_URL` to use another URL. Generated files live in `packages/contracts/src/generated.ts` and must not be hand-edited.

Model gateway configuration, status, usage, request history, and project budget routes are documented in [the architecture](docs/architecture.md). Configure provider keys only in your local `.env`, add an explicit model entry to `infra/litellm/config.yaml`, and register a matching account/deployment/alias/pool route through the administrator API. The credential-free doctor endpoint is `GET /ops/models/doctor`; the explicitly invoked paid compatibility check is `POST /projects/{project_id}/models/live-check` and requires both an administrator session and `authorize_paid_request: true`. No model call is made by startup, health checks, or the doctor.

The M5 ACP verification is offline: run `PYTHONPATH=apps/api uv run python scripts/m5_acp_offline_smoke.py` to check the pinned OpenCode handshake and sandbox isolation. It creates a temporary Git fixture and sends no prompt. The optional run-scoped model broker sends OpenCode requests through Harnessa's project/task/run budgets and ledger; it is disabled by default and requires a separately managed host worker with rootless Podman, `cc`, and a configured model gateway. No provider key is passed to the agent. See [the M5 record](docs/milestones/05.md) for current evidence and the live-provider authorization blocker.

The integration suite requires disposable real PostgreSQL and Redis services. Start Compose, create a database ending in `_test`, and run it against PostgreSQL plus Redis database 1:

```sh
podman compose -f infra/compose.yaml exec -T db createdb -U "$POSTGRES_USER" harnessa_test
TEST_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/harnessa_test" \
TEST_REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/1" pnpm test:integration
```

These integration tests truncate their disposable test database and flush Redis database 1 before and after each case. Never point them at a development or production database.

## Structure

- `apps/web`: Next.js application shell, sign-in, project selection, and repository registration UI.
- `apps/api`: FastAPI application, domain/services/adapters boundaries, async persistence, Alembic, and separately runnable worker entry point.
- `packages/contracts`: TypeScript types generated from the API's OpenAPI schema.
- `infra`: Compose and digest-pinned API/web images.
- `tests`: server contract, API boundary, and opt-in real-service orchestration checks.
- `docs`: architecture, compatibility, and milestone record.

Read [docs/architecture.md](docs/architecture.md), [docs/compatibility.md](docs/compatibility.md), and the [milestone reports](docs/milestones/) for implementation boundaries and verified results.
