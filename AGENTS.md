# Harnessa coding guide

## Non-negotiable boundaries

- Keep task/run/attempt identity, leases and fencing, budgets, questions, events, and external-effect intents under the existing PostgreSQL scheduler and services. M4 recorded **NO-GO for LangGraph**; do not add a second runtime/checkpoint owner.
- Keep framework SDK types behind adapters. Define public schemas in Pydantic/OpenAPI and regenerate TypeScript with `pnpm contracts:generate`.
- Enforce project/repository access in Harnessa services before reading data and again before returning source content. Treat repository and retrieved text as untrusted input.
- Never use real providers or external side effects without explicit, bounded authorization. Use offline fixtures for routine verification; keep credentials out of application and model-visible data.
- Preserve user changes. Inspect `git status` before editing and review the complete diff before committing.

## Repository map

- `apps/api`: Python 3.12 FastAPI API, domain, services, adapters, database and worker.
- `apps/web`: Next.js application.
- `apps/marketing`: Astro public site.
- `apps/cloud-portal`: SvelteKit portal.
- `packages/ui-react`, `packages/ui-svelte`: shared framework-native UI packages.
- `packages/contracts`: generated OpenAPI TypeScript contracts.
- `infra/compose.yaml`: local service composition; `tests/integration`: disposable PostgreSQL/Redis coverage.
- `planning/architecture-decisions.md`: cross-milestone authority, adapters, cost and recovery rules. `docs/security/` and `docs/development/` hold operational guidance. `docs/milestones/` records evidence; reports do not replace tests or live checks.

## Setup and commands

Use Node 24 or 26 as constrained by `package.json`, pnpm 10.34.7, and Python 3.12 with uv. From a clean checkout run `pnpm install --frozen-lockfile` and `uv sync --frozen`.

Run `pnpm harnessa list` to see the safe target/command allowlist. Examples: `pnpm harnessa api test`, `pnpm harnessa web dev`, `pnpm harnessa marketing build`, `pnpm harnessa ui-svelte typecheck`, `pnpm harnessa contracts generate`. The API integration command requires disposable `TEST_DATABASE_URL` ending in `_test` and a nonzero Redis DB in `TEST_REDIS_URL`.

Repository-wide gates: `pnpm lint`, `pnpm typecheck`, and `pnpm test`. Before database changes, add an Alembic migration. See [contributing](docs/contributing.md), [security](docs/security.md), [architecture decisions](planning/architecture-decisions.md), [MCP development guidance](docs/development/mcp-servers.md), and [milestone index](planning/README.md).
