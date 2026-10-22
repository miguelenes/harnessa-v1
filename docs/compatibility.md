# Compatibility and dependency record

Dependency versions are exact in `uv.lock` and `pnpm-lock.yaml`. Container references are pinned by immutable manifest-list digest in `infra/`. Maintained manifests, lockfiles, active Dockerfiles, Compose services, and the existing GitHub Actions references were reviewed on 2026-10-22.

## Current supported baseline

| Component | Selected version | Compatibility evidence |
|---|---:|---|
| Python | 3.12.15 (`>=3.12,<3.13`) | Matches `pyproject.toml`, uv lock resolution, and the API image. |
| uv | 0.13.1 local / 0.13.5 API image | The image installer pin is current; the shared host binary was not modified. |
| FastAPI / Starlette | 0.159.1 / 1.9.0 | Selected by uv; Starlette 1.9.0 satisfies FastAPI's declared `<2.0` range and the test suite passes. |
| Pydantic / settings | 2.14.1 / 2.16.1 | FastAPI and ACP SDK resolve against Pydantic v2. |
| SQLAlchemy / Alembic | 2.0.55 / 1.20.1 | Latest SQLAlchemy compatible with `fastapi-users-db-sqlalchemy==7.0.0`. |
| FastAPI Users | 15.0.5 | Latest compatible cookie-auth and SQLAlchemy integration. |
| PostgreSQL | 16.13 Bookworm | Patch upgrade within major 16; no data-directory migration. |
| Redis server / redis-py | 8.10.2 Alpine / 8.1.0 | Redis 8 is pinned; Python stream-response typing was normalized at the adapter boundary. |
| Node.js | 24.x or 26.x | `package.json` engines range matches Vitest 5 and the pinned Node 26 image. |
| pnpm | 10.34.7 | Exact root `packageManager` pin. |
| Next.js / React | 16.4.2 / 19.3.1 | Stable compatible releases; see [Next.js installation guidance](https://nextjs.org/docs/app/getting-started/installation). |
| Astro / Svelte / Vite | 7.4.2 / 5.57.2 / 8.3.5 | Current workspace releases; checked with the existing marketing and portal builds. |
| Tailwind CSS | 4.3.3 | Current v4 release used by all web workspaces. |
| TypeScript | 6.0.3 apps / 5.9.3 contracts | `openapi-typescript==7.13.0` requires TypeScript 5.x; `svelte-check==4.7.7` supports TypeScript 5 and 6, not 7. |
| ESLint / Next config | 10.13.0 / 16.4.2 | ESLint 10 lint passes. Next's nested import/React/a11y plugins still declare peer ranges ending below 10; see the compatibility limitation below. |
| OpenSpec CLI | `@fission-ai/openspec==1.14.3` | Pinned in the root manifest; actual `spec-driven` schema, templates, and JSON CLI operations were discovered from the installed package. |
| API contract generation | `openapi-typescript==7.13.0` | Existing FastAPI OpenAPI → TypeScript pipeline remains authoritative. |
| Python test/coverage | pytest 9.1.1 / pytest-cov 7.1.0 / coverage 7.16.2 / pytest-xdist 3.8.0 | Exact dev-group pins. The standard command runs unit tests with four workers, integration tests serially on an explicitly supplied disposable database/Redis pair, and a project-wide 85% line/statement gate. Current measured result and remaining gap are recorded in `docs/milestones/06.md`. |
| JavaScript test/coverage | Vitest 5.0.5 / `@vitest/coverage-v8` 5.0.5 / jsdom 30.1.3 | Exact root pins; React/web and Svelte/cloud portal tests emit coverage JSON under ignored `coverage/`. |
| ACP SDK | 0.12.1 (`agent-client-protocol`) | Stable official SDK, Python 3.12 compatible, public `connect_to_agent` API and schema models. |
| LiteLLM Proxy | 1.83.14-stable.patch.3 | Patch-level refresh; image digest recorded below. |
| OpenCode ACP agent | 1.19.4 pinned digest | Pulled and validated offline on 2026-10-22; both protocol and fake-router broker smokes pass. Live M6 Architect evidence remains separate and is recorded in `docs/milestones/06.md`. |

The root Node engine range is `>=24.0.0 <25 || >=26.0.0 <27`; Node 25 is excluded because Vitest 5's supported range is Node 22.12+, 24.x, or 26+. The web and API builder images use Node 26.12.0. `@fission-ai/openspec@1.14.3` requires Node 20.19 or newer. ESLint 10.13.0 is the latest stable release and its current web lint passes, but `eslint-config-next@16.4.2` transitively depends on plugins whose published ESLint peer ranges stop at 9. These peer warnings are recorded rather than hidden; the Next config's lint rules were exercised on ESLint 10.13.0.

Two Python versions remain below the latest registry version for hard compatibility reasons: `pwdlib==0.3.0` is pinned by FastAPI Users 15.0.5, and SQLAlchemy 2.0.55 is the newest version accepted by `fastapi-users-db-sqlalchemy==7.0.0` (`<2.1`). TypeScript 7.0.2 is not selected because the generator requires TypeScript 5.x and the Svelte checker supports through 6.x. These constraints were confirmed from locked package metadata and workspace registry data; no incompatible override was applied.

## Locked container manifests

- `node:26.12.0-bookworm-slim@sha256:742a0fe7320e09e0321222b68165e6ac731d30470e7ea7f98f9bfc3a6e1034fe` (API OpenSpec CLI runtime stage)
- `node:26.12.0-alpine@sha256:6f08a523443ddedf52941874c203475b0ad503e859ad70cb26ab8be9a517dbd5` (web)
- `python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70`
- `postgres:16.13-bookworm@sha256:472efd9a66f2b2f1a5aeb18b28de74332e6ef88c2b93a1a5d812fb6db67a5f60`
- `redis:8.10.2-alpine@sha256:615b4cedaff0c1437a1548acae4222572df8c021eeb7382d64cb8b5e3a13ac7a`
- `ghcr.io/berriai/litellm:v1.83.14-stable.patch.3@sha256:f12d528d4a05add56cb09e54c5126088f2edc6bdf3a2f943bcd3a32b08769da2`
- `ghcr.io/anomalyco/opencode:1.19.4@sha256:bf8e2b5f785166a12a26cbb34ef1a2d35403368438efe036fd16c795a975c505` (linux/amd64 ACP agent)

Digests were resolved from Docker Registry manifests for the exact tags above. The images are not floating; platform selection uses the pinned manifest list where one is published. PostgreSQL stays on major 16 because PostgreSQL 18 changes the `PGDATA` path and volume mount contract; the open Dependabot major-upgrade PR is documented in the M6 report.

## Milestone 6 package-manager and OpenSpec notes

- Root `package.json` pins `@fission-ai/openspec==1.14.3`, Vitest 5.0.5, `@vitest/coverage-v8` 5.0.5, Testing Library, and jsdom. The actual CLI returned the `spec-driven` schema as the only package schema; its `proposal`, `specs`, `design`, and `tasks` template paths were read from the installed package. `openspec schema validate spec-driven --json`, `openspec new change <id>`, and `openspec validate --json --no-interactive` are real CLI operations. `/opsx:*` entries are assistant workflow names, not CLI subcommands.
- `pnpm-workspace.yaml` uses pnpm 10's `allowBuilds` map. Only `esbuild`, `unrs-resolver`, and `workerd` are allowed to run dependency lifecycle scripts. `pnpm approve-builds` approved those three packages. A subsequent `pnpm install --offline --frozen-lockfile --ignore-scripts` passed from the local cache; no package-registry request was needed. See [pnpm's build-script approval documentation](https://pnpm.io/cli/approve-builds).
- `pnpm test` runs Python unit tests with four pytest-xdist workers, then service-backed integration tests serially because they truncate the shared disposable database and flush the configured Redis database, then React/web and Svelte/cloud portal tests, a marketing static-build artifact check, and an aggregate 85% coverage gate. It fails closed when `TEST_DATABASE_URL` and `TEST_REDIS_URL` are missing. Generated `.next`, build, coverage, and dependency output is excluded; the generated OpenAPI TypeScript client is excluded, and `packages/contracts/src/index.ts` is a type-only re-export with no executable statements. Marketing is marked not applicable only while its entire source tree contains Astro templates and CSS with no executable JS/TS; the built HTML is checked for landmarks, navigation, skip link, product content, and resolved links. Adding executable source makes the empty-package check fail until that source is instrumented.
- The active CodeQL and documentation workflows pin third-party actions to full commit SHAs and target GitHub-hosted `ubuntu-24.04` as directed during M6. The Local CI job uses digest-pinned PostgreSQL and Redis GitHub service containers. Documentation builds run in strict mode on pull requests and pushes to `main`; Pages deployment runs only after a successful push to `main`. Exact-head hosted validation is recorded in `docs/milestones/06.md`.
- Ruff 0.17.1's expanded defaults surfaced legacy style and exception-boundary findings. Safe import/UTC/Decimal updates were applied, FastAPI's request-time `Depends`/`Header`/`Query` declarations are marked immutable per the [Ruff configuration guide](https://docs.astral.sh/ruff/settings/), and broad catches are kept only at explicit adapter/cleanup/health boundaries with inline explanations. `pnpm lint` includes the API, tests, and operational scripts. The final configured Ruff result is recorded in the M6 report.

## Milestone 3 LiteLLM interface notes

- Versioned [LiteLLM Proxy configuration](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/configs.md) supports model aliases and environment-backed key references such as `os.environ/OPENAI_API_KEY`. Each configured `proxy_model` must match a `model_name` in the local mounted config.
- Versioned [Proxy health documentation](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/health.md) provides `GET /health/readiness`, which the API and Compose use without sending a provider request.
- Versioned [virtual key documentation](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/virtual_keys.md) was reviewed for a later runner credential boundary. Per-run short-lived credentials are deferred until there is an ACP runner to receive them; this milestone never sends provider secrets to a runner.
- Versioned [logging configuration documentation](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/logging.md) documents `turn_off_message_logging: true`; the shipped proxy config disables message logging. It also disables proxy-level retries/fallbacks because application-side retries and route decisions are recorded per attempt.
- LiteLLM is attached to both the internal app network and `provider_egress`; API, worker, database, Redis, and web have no provider egress. The LiteLLM port is not published on the host. Provider key environment values are only passed to the LiteLLM container.

No real provider credentials or paid route checks were used during implementation. The live compatibility result is **PENDING**; see `docs/milestones/03.md` for the exact opt-in setup procedure and one-call authorization command.

## Milestone 5 ACP compatibility

The adapter pins stable `agent-client-protocol==0.12.1`, verified against the [official Python SDK 0.12.1 source](https://github.com/agentclientprotocol/python-sdk/tree/0.12.1) and [PyPI release metadata](https://pypi.org/project/agent-client-protocol/0.12.1/). The [official release list](https://github.com/agentclientprotocol/python-sdk/releases) also contains the 1.0.0 release candidate; Harnessa stays on the stable, Python 3.12-tested API instead of adopting its pre-release schema/runtime changes. Harnessa uses the public `connect_to_agent` helper, SDK schema models, and ACP protocol version 1. Its bounded stdio transport supplies the SDK's message transport interface while enforcing 1 MiB frames, 64 MiB total traffic, 30,000 messages, and a 300-second idle timeout. The protocol fixture covers malformed/oversized frames, timeout, cancellation, process exit, permission policy, redacted updates, and the fixture agent's unsupported session-resume capability.

OpenCode uses an ephemeral custom provider config with `OPENCODE_CONFIG`, its documented OpenAI-compatible provider interface, and a run-scoped bearer token. Model requests reach a trusted host-side broker through a private Unix socket and a static loopback-only TCP relay. The broker ignores the model name selected by the agent, fixes the task/run/project/model alias from trusted context, checks the current fencing token, and calls Harnessa's existing `ModelRouter`; provider credentials remain in the trusted LiteLLM boundary. The agent container stays on `--network=none`. This optional route requires a separate host worker, rootless Podman, and a host C compiler to build the static relay; it is disabled by default. See the [OpenCode provider configuration](https://opencode.ai/docs/providers/) and [custom config path documentation](https://dev.opencode.ai/docs/config/).

| Agent | Transport/status | Version and protocol | Resume / route status |
|---|---|---|---|
| OpenCode | Native ACP; 1.19.4 offline compatibility verified | `opencode acp`, 1.19.4 pinned digest, ACP v1 | Both offline smokes pass on the pinned image. Fake-router evidence confirms transport/broker compatibility only; it does not establish live provider generation. |
| Codex CLI | Bridge; `adapter_required` | Official bridge reference, no Harnessa-pinned binary | Route and runtime compatibility unverified. |
| Claude Code | Bridge; `adapter_required` | Official bridge reference, no Harnessa-pinned binary | Route and runtime compatibility unverified. |
| Goose | Native ACP; `adapter_required` | Native command documented upstream; Harnessa version not pinned | Handshake and model route unverified. |
| Gemini CLI | Native ACP; `adapter_required` | Native command documented upstream; Harnessa version not pinned | Handshake and model route unverified. |

The OpenCode record follows its [official ACP documentation](https://dev.opencode.ai/v2/docs/cli/acp/). Harnessa uses fresh-session recovery after worker loss because its runner does not persist OpenCode's session store across container cleanup. The remaining agent references are the [Codex ACP bridge](https://github.com/agentclientprotocol/codex-acp), [Claude ACP bridge](https://github.com/agentclientprotocol/claude-agent-acp), [Goose ACP documentation](https://github.com/block/goose/tree/main/documentation/docs/gdk/acp), and [Gemini CLI repository](https://github.com/google-gemini/gemini-cli). Both offline smoke scripts now pass on the pinned 1.19.4 image. The [protocol smoke script](https://github.com/miguelenes/harnessa-v1/blob/main/scripts/m5_acp_offline_smoke.py) performs initialization and session creation without a prompt. The [broker smoke script](https://github.com/miguelenes/harnessa-v1/blob/main/scripts/m5_acp_broker_offline_smoke.py) sends a deterministic ACP prompt through OpenCode to an in-process fake `ModelRouter`; it verifies streaming compatibility without contacting a model provider or editing the fixture repository. The live provider/Architect result remains tracked separately in the M6 report.
