# Harnessa

A self-hosted repository-change control plane. Milestone 1 establishes the modular monorepo, server-owned contracts, project-scoped identity, and explicit repository registration. Agent execution and later product views are not part of this milestone.

## Start locally

Requirements: Podman with `podman compose` (or Docker Compose), network access for the first image/package build, and at least 4 GB available for the web build.

1. Create local configuration and replace the placeholders, especially the database password, `SECRET_KEY` (32+ random bytes), and one-time `BOOTSTRAP_TOKEN`:

   ```sh
   cp .env.example .env
   set -a && . ./.env && set +a
   ```

   Keep `DATABASE_URL`'s password in sync with `POSTGRES_PASSWORD`. `REPOSITORY_TARGETS` is a comma-separated allowlist of exact IDs, for example `acme/service-a,acme/service-b`. The default development API and web ports are 8010 and 3000; set `API_PORT` or `WEB_PORT` if needed. Keep `ALLOWED_ORIGINS` aligned with the web origin. For HTTPS deployments set `COOKIE_SECURE=true` and configure a TLS-terminating reverse proxy.

2. Build and start the four services:

   ```sh
   podman compose -f infra/compose.yaml up --build -d
   ```

   The API automatically applies Alembic migrations before it starts. Check `http://localhost:${API_PORT}/health/live`, `http://localhost:${API_PORT}/health/ready`, and `http://localhost:${WEB_PORT}`.

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
pnpm --filter @harnessa/web lint
pnpm --filter @harnessa/web typecheck
pnpm --filter @harnessa/web build
pnpm contracts:generate
```

`pnpm contracts:generate` reads the running API OpenAPI document at `http://localhost:8010/openapi.json` by default. Set `HARNESSA_API_URL` to use another URL. Generated files live in `packages/contracts/src/generated.ts` and must not be hand-edited.

## Structure

- `apps/web`: Next.js application shell, sign-in, project selection, and repository registration UI.
- `apps/api`: FastAPI application, domain/services/adapters boundaries, async persistence, Alembic, and separately runnable worker entry point.
- `packages/contracts`: TypeScript types generated from the API's OpenAPI schema.
- `infra`: Compose and digest-pinned API/web images.
- `tests`: server contract and API boundary checks.
- `docs`: architecture, compatibility, and milestone record.

Read [docs/architecture.md](docs/architecture.md), [docs/compatibility.md](docs/compatibility.md), and [docs/milestones/01.md](docs/milestones/01.md) for implementation boundaries and verified results.
