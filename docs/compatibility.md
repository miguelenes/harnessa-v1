# Compatibility and dependency record

Dependency versions are exact in `uv.lock` and `pnpm-lock.yaml`. Container references are pinned by manifest-list digest in `infra/`.

Milestone 3 adds `httpx==0.28.1` as a runtime dependency for bounded LiteLLM Proxy requests and streaming. `uv.lock` records the complete resolution. The LiteLLM service is digest-pinned separately; provider protocol documentation was checked against its versioned v1.83.3-stable docs. Existing API and worker dependencies remain unchanged.

| Component | Selected version | Basis |
|---|---:|---|
| Python | 3.12.12 | Supported runtime pinned in API image |
| uv | 0.13.1 local / 0.9.5 image | Lockfile installer |
| FastAPI | 0.121.0 | Pydantic v2 compatible |
| Pydantic settings | 2.11.0 | Pydantic v2 |
| SQLAlchemy / Alembic | 2.0.44 / 1.17.0 | Async ORM and migrations |
| PostgreSQL | 16.10 Bookworm | Major pinned image digest |
| Redis | 7.4.2 Alpine | Redis Streams transport foundation |
| Node.js | 24.10.0 Alpine | Supported LTS runtime pinned by digest |
| pnpm | 10.34.7 | Root package manager pin |
| Next.js / React | 16.4.2 / 19.3.1 | Current stable versions selected from official Next.js installation guidance and registry on 2026-10-22 |
| Tailwind CSS | 4.3.3 | Current v4 release |
| TypeScript | 5.9.3 | Stable compatible toolchain |
| openapi-typescript | 7.13.0 | Generates client types from server OpenAPI |
| FastAPI Users | 14.0.1 | Maintained cookie-auth and SQLAlchemy integration |
| HTTPX | 0.28.1 | Async requests and streaming to the internal LiteLLM Proxy |
| LiteLLM Proxy | 1.83.3-stable | Versioned proxy docs and pinned container digest; one deployment per application attempt |

The official [Next.js installation documentation](https://nextjs.org/docs/app/getting-started/installation) recommends the App Router with TypeScript, Tailwind, ESLint, and current `next`/`react` releases, and specifies Node.js 20.9 minimum. Next.js 16.4.2 was the stable registry version resolved during implementation; it replaces the older Next.js 15 pin from the original attachment. React 19.3.1 and the remaining frontend tool versions are captured in the lockfile. These versions should be refreshed deliberately with compatibility/build checks, not floating tags.

## Locked container manifests

- `python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c`
- `node:24.10.0-alpine@sha256:775ba24d35a13e74dedce1d2af4ad510337b68d8e22be89e0ce2ccc299329083`
- `postgres:16.10-bookworm@sha256:38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74`
- `redis:7.4.2-alpine@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952`
- `ghcr.io/berriai/litellm:v1.83.3-stable@sha256:1bc003a50e5c9177909975e1523cf780a5f385c76216e26e96c50fb02611209f`

Digests were resolved from Docker Registry manifests for the tags above. Platform selection occurs from the pinned multi-platform manifest.

## Milestone 3 LiteLLM interface notes

- Versioned [LiteLLM Proxy configuration](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/configs.md) supports model aliases and environment-backed key references such as `os.environ/OPENAI_API_KEY`. Each configured `proxy_model` must match a `model_name` in the local mounted config.
- Versioned [Proxy health documentation](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/health.md) provides `GET /health/readiness`, which the API and Compose use without sending a provider request.
- Versioned [virtual key documentation](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/virtual_keys.md) was reviewed for a later runner credential boundary. Per-run short-lived credentials are deferred until there is an ACP runner to receive them; this milestone never sends provider secrets to a runner.
- Versioned [logging configuration documentation](https://github.com/BerriAI/litellm/blob/v1.83.3-stable/docs/my-website/docs/proxy/logging.md) documents `turn_off_message_logging: true`; the shipped proxy config disables message logging. It also disables proxy-level retries/fallbacks because application-side retries and route decisions are recorded per attempt.
- LiteLLM is attached to both the internal app network and `provider_egress`; API, worker, database, Redis, and web have no provider egress. The LiteLLM port is not published on the host. Provider key environment values are only passed to the LiteLLM container.

No real provider credentials or paid route checks were used during implementation. The live compatibility result is **PENDING**; see `docs/milestones/03.md` for the exact opt-in setup procedure and one-call authorization command.
