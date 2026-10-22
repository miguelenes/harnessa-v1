# Compatibility and dependency record

Dependency versions are exact in `uv.lock` and `pnpm-lock.yaml`. Container references are pinned by manifest-list digest in `infra/`.

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

The official [Next.js installation documentation](https://nextjs.org/docs/app/getting-started/installation) recommends the App Router with TypeScript, Tailwind, ESLint, and current `next`/`react` releases, and specifies Node.js 20.9 minimum. Next.js 16.4.2 was the stable registry version resolved during implementation; it replaces the older Next.js 15 pin from the original attachment. React 19.3.1 and the remaining frontend tool versions are captured in the lockfile. These versions should be refreshed deliberately with compatibility/build checks, not floating tags.

## Locked container manifests

- `python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c`
- `node:24.10.0-alpine@sha256:775ba24d35a13e74dedce1d2af4ad510337b68d8e22be89e0ce2ccc299329083`
- `postgres:16.10-bookworm@sha256:38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74`
- `redis:7.4.2-alpine@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952`

Digests were resolved from Docker Registry manifests for the tags above. Platform selection occurs from the pinned multi-platform manifest.
