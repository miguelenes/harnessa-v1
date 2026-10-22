# Compatibility and dependency record

Dependency versions are exact in `uv.lock` and `pnpm-lock.yaml`. Container references are pinned by manifest-list digest in `infra/`.

Milestone 3 adds `httpx==0.28.1` as a runtime dependency for bounded LiteLLM Proxy requests and streaming. `uv.lock` records the complete resolution. The LiteLLM service is digest-pinned separately; provider protocol documentation was checked against its versioned v1.83.3-stable docs. Existing API and worker dependencies remain unchanged.

| Component | Selected version | Basis |
|---|---:|---|
| Python | 3.12.15 local / 3.12.12 image | uv-selected local interpreter and supported runtime pinned in API image |
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
| ACP Python SDK | 0.12.1 (`agent-client-protocol`) | Official SDK; Python 3.12 compatible; public `connect_to_agent` API and schema models |
| LiteLLM Proxy | 1.83.3-stable | Versioned proxy docs and pinned container digest; one deployment per application attempt |

The official [Next.js installation documentation](https://nextjs.org/docs/app/getting-started/installation) recommends the App Router with TypeScript, Tailwind, ESLint, and current `next`/`react` releases, and specifies Node.js 20.9 minimum. Next.js 16.4.2 was the stable registry version resolved during implementation; it replaces the older Next.js 15 pin from the original attachment. React 19.3.1 and the remaining frontend tool versions are captured in the lockfile. These versions should be refreshed deliberately with compatibility/build checks, not floating tags.

## Locked container manifests

- `python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c`
- `node:24.10.0-alpine@sha256:775ba24d35a13e74dedce1d2af4ad510337b68d8e22be89e0ce2ccc299329083`
- `postgres:16.10-bookworm@sha256:38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74`
- `redis:7.4.2-alpine@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952`
- `ghcr.io/berriai/litellm:v1.83.3-stable@sha256:1bc003a50e5c9177909975e1523cf780a5f385c76216e26e96c50fb02611209f`
- `ghcr.io/anomalyco/opencode@sha256:85986f556e985e377bd24cd68a5ccfbc647bb49e591194edda8a0e4542303319` (linux/amd64 ACP agent image; local image ID `sha256:8c8f06e8a1cb2000b7f386eaef57bac08601f59d05175d93bfff185676c47302`)

Digests were resolved from Docker Registry manifests for the tags above. Platform selection occurs from the pinned multi-platform manifest.

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
| OpenCode | Native ACP; `verified` for initialization, session creation, and an offline prompt through the broker | `opencode acp`, 2.0.32, ACP v1; image digest above | Advertises resume; Harnessa uses fresh-session recovery because session storage is not persisted. Broker dispatch remains disabled by default; live provider route not exercised. |
| Codex CLI | Bridge; `adapter_required` | Official bridge reference, no Harnessa-pinned binary | Route and runtime compatibility unverified. |
| Claude Code | Bridge; `adapter_required` | Official bridge reference, no Harnessa-pinned binary | Route and runtime compatibility unverified. |
| Goose | Native ACP; `adapter_required` | Native command documented upstream; Harnessa version not pinned | Handshake and model route unverified. |
| Gemini CLI | Native ACP; `adapter_required` | Native command documented upstream; Harnessa version not pinned | Handshake and model route unverified. |

The OpenCode record follows its [official ACP documentation](https://dev.opencode.ai/v2/docs/cli/acp/), which documents `sessionCapabilities.resume`; the pinned 2.0.32 sandbox response advertised resume. Harnessa still chooses a fresh session after worker loss because the runner does not persist OpenCode's session store across container cleanup. The remaining agent references are the [Codex ACP bridge](https://github.com/agentclientprotocol/codex-acp), [Claude ACP bridge](https://github.com/agentclientprotocol/claude-agent-acp), [Goose ACP documentation](https://github.com/block/goose/tree/main/documentation/docs/gdk/acp), and [Gemini CLI repository](https://github.com/google-gemini/gemini-cli). Only OpenCode was run in the sandbox. The [protocol smoke script](https://github.com/miguelenes/harnessa-v1/blob/main/scripts/m5_acp_offline_smoke.py) performs initialization and session creation without a prompt. The [broker smoke script](https://github.com/miguelenes/harnessa-v1/blob/main/scripts/m5_acp_broker_offline_smoke.py) sends a deterministic ACP prompt through OpenCode to an in-process fake `ModelRouter`; it verifies streaming compatibility without contacting a model provider or editing the fixture repository.
