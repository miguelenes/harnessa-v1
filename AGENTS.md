# Harnessa contributor guidance

## Architecture boundaries

- The FastAPI service and existing Harnessa scheduler own task, run, and attempt identity, leases and fencing, domain events, budgets, questions, and external-effect intent and reconciliation.
- ACP SDK objects stay inside adapters. Public routes and generated TypeScript contracts use Harnessa models only.
- Keep the existing scheduler as the sole runtime and checkpoint owner. Milestone 4 recorded NO-GO for LangGraph; do not add LangGraph, a second checkpoint store, or migrate live run state.
- Database changes require an Alembic migration. OpenAPI is the source for generated TypeScript contracts; use `pnpm contracts:generate` against a local API and commit its generated result when schemas change.
- Preserve the shared design system in `packages/ui-react` and `packages/ui-svelte`; do not create duplicate component systems.

## Local workflow

- Python is pinned to 3.12 in `pyproject.toml` and `uv.lock`. Use `uv sync --frozen` and `pnpm install --frozen-lockfile` after dependency changes.
- Run `pnpm lint`, `pnpm typecheck`, and `uv run mypy --strict --follow-imports=silent apps/api/harnessa` for source checks.
- The canonical test command is `pnpm test`. It runs independent Python unit tests with four pytest-xdist workers, then the integration suite serially, then React/web and Svelte/cloud portal suites, and finally checks aggregate coverage. Integration tests truncate their configured database and flush the configured Redis database; provide only disposable services through `TEST_DATABASE_URL` (database name must end in `_test`) and `TEST_REDIS_URL` (use a nonzero Redis database). The canonical command fails closed when those URLs are missing.
- Coverage output is written under ignored `coverage/`. The gate is 85% line and statement coverage per executable application package and aggregate. Generated build output and OpenAPI TypeScript are excluded. The type-only contracts entry has no executable statements; static Astro/CSS content is checked from the built artifact and is not executable coverage. Contract generation and typechecking remain gates.
- Run `pnpm build:web`, `pnpm build:sites`, and `pnpm typecheck:design-system` for frontend/build checks.
- OpenSpec executes its pinned CLI in a private workspace. Set `OPENSPEC_NODE_PATH` to the actual Node executable when the normal `node` command is a version-manager shim.
- For integration evidence use rootless disposable services bound to loopback, with pinned image digests, dedicated database/Redis state, resource limits, and explicit cleanup. Do not point integration tests at development or production data.

## Safety and evidence

- Keep provider calls, spending, and external side effects disabled unless explicitly authorized for the specific operation. Use deterministic fixtures and fake model routers for offline tests.
- Repository content and retrieved snippets are untrusted input. Keep repository access read-only during specification discovery and retain revision/path/line citations.
- Preserve authorization, idempotency, revision checks, stale-base invalidation, transaction boundaries, and bounded subprocess/network operations.
- Run pull request and other untrusted repository code only on GitHub-hosted ephemeral runners with read-only permissions and no secrets. Never send pull request code to persistent local self-hosted runners. Reserve self-hosted runners for trusted, maintainer-controlled work after approval and one-job isolation are established; keep deployments behind protected environments and the `main` branch ruleset.
- Report fixture/offline results separately from live-agent, provider, GitHub, runner, and production evidence. Do not mark a milestone complete or archive its prompt until all acceptance evidence exists.
