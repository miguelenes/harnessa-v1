# Getting started

## Requirements

- Podman with `podman compose`, or Docker with Docker Compose
- `uv` for Python development
- Node.js and pnpm for web development
- Network access for the initial image and package downloads
- At least 4 GB of available memory for the web build

## Start the local stack

1. Copy the sample environment and replace every placeholder. Use a unique database password, a random `SECRET_KEY` with at least 32 bytes, and a one-time `BOOTSTRAP_TOKEN`.

   ```sh
   cp .env.example .env
   set -a && . ./.env && set +a
   ```

   Keep the password in `DATABASE_URL` aligned with `POSTGRES_PASSWORD`. `REPOSITORY_TARGETS` is an allowlist of exact repository IDs. For HTTPS deployments, set `COOKIE_SECURE=true` behind a TLS-terminating proxy.

2. Build and start the services:

   ```sh
   podman compose -f infra/compose.yaml up --build -d
   ```

   The API applies migrations before startup. The default model list is empty and makes no provider calls. Provider credentials belong only in the local `.env` and are passed to LiteLLM, the only service with provider egress.

3. Bootstrap the first administrator once:

   ```sh
   curl -i http://localhost:${API_PORT:-8010}/auth/bootstrap \
     -H 'Content-Type: application/json' \
     -H "X-Bootstrap-Token: $BOOTSTRAP_TOKEN" \
     -d '{"email":"admin@example.com","password":"replace-with-a-long-password","organization_name":"My Organization"}'
   ```

   Sign in at `http://localhost:${WEB_PORT:-3000}/login`, create a project, and register an allowlisted repository. Registration stores configuration only; it does not access GitHub or inspect repository contents.

4. Check service health at `/health/live` and `/health/ready` on the API, then stop the services while retaining the local database volume:

   ```sh
   podman compose -f infra/compose.yaml down
   ```

   Add `-v` only when you also want to delete the development database.

For development commands and integration-test setup, see [contributing](contributing.md). Never point integration tests at a development or production database.
