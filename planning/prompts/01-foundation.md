# Prompt 1 of 10 — Foundation, contracts, and access control

Build milestone 1 of Harnessa in this repository. Read `planning/revised-specification.md` first; it supersedes conflicting technical details in the original attachment. Implement this milestone fully, preserve unrelated work, and stop before milestone 2. You are building the actual product, not generating a simulated prototype.

## Prerequisites

No prior application is required. Inspect the working directory and applicable instructions before making changes. Reuse existing compatible code if the repository is no longer empty.

## Implement

1. Establish a modular monorepo: `apps/web`, `apps/api`, `packages/contracts`, `infra`, `tests`, and `docs`. Keep Python domain/services/adapters separated without creating unnecessary network services. API and worker will share the domain package.
2. Pin compatible supported dependencies and images. Use Python 3.12/uv, FastAPI/Pydantic v2, async SQLAlchemy/Alembic, PostgreSQL 16, Redis, Next.js/TypeScript, Tailwind, and shadcn/ui. Select frontend versions using current official documentation, record the choice, and commit lockfiles. Define extension points for LiteLLM, OpenSpec, ACP, AG-UI, Harness², and artifact storage.
3. Create the canonical task lifecycle, separate run status, event envelope, error response, identity, authorization, specification revision, and artifact contracts. Generate TypeScript API types from server-owned schemas; do not maintain duplicate hand-written schemas. Specify HTTP idempotency keys and optimistic version conflict responses.
4. Add migrations for organization/user/membership/project/repository/task foundations. Add scoped identifiers and constraints, UTC timestamps, and schema-version tracking. Reserve later entities in the architecture document rather than writing unneeded empty tables.
5. Implement self-hosted authentication using a maintained authentication/session library, a one-time administrator bootstrap, role and project authorization, secure session cookies, CSRF protection, and explicit origin policy. Design the identity interface for optional OIDC later. Do not leave an authentication bypass in production mode.
6. Create the app shell, login, project selection, and repository registration API. Repository registration accepts only explicit configured targets and performs no unsolicited GitHub connection or scan.
7. Define the Harnessa brand and UI system before building the production shell. Add a concise design-system source of truth covering brand direction, color roles, typography, spacing, component states, icon treatment, and light/dark behavior. Establish shared shadcn/ui components and Tailwind tokens for the app shell, forms, navigation, dialogs, and status/feedback patterns; do not style these as isolated one-off screens. Create initial repository-native SVG logo/wordmark variants and favicon/app icon, or document any reason they are deferred. Use image generation only for appropriate original raster artwork; preserve every selected result in the repository with its intended use and generation provenance. Do not render a logo as a bitmap when an SVG mark is the better fit.
8. Provide a developer Compose environment, `.env.example` containing references/placeholders only, health endpoints, standard lint/typecheck/test commands, and startup documentation. Treat this application as a Python/Node environment unless the actual repository indicates otherwise.

## Acceptance

- A clean checkout boots the API, web app, database, and Redis through documented commands.
- Fresh migrations and upgrade from the initial schema run successfully.
- An administrator can bootstrap, sign in, create a project, and register a repository configuration.
- An unauthorized user cannot access another project's tasks or artifacts; unauthenticated requests fail correctly.
- Generated frontend contracts pass typechecking and basic API integration checks pass.

Write `docs/architecture.md`, `docs/compatibility.md`, `docs/design-system.md`, and `docs/milestones/01.md`. Record dependency versions, brand decisions, component conventions, asset inventory/provenance, commands run, real results, and remaining setup requirements. Do not provision paid services, call live models, or claim later milestones are complete.
