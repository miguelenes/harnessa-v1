# Local model gateway

Harnessa's LiteLLM Proxy is the only service that receives provider credentials.
The Compose network keeps it internal; the default configuration does not publish
port 4000 on the host. `infra/litellm/config.yaml` exposes two explicit model names:

| LiteLLM model name | Provider model | Environment key |
| --- | --- | --- |
| `harnessa-deepseek-flash` | DeepSeek `deepseek-flash` | `DEEPSEEK_API_KEY` |
| `harnessa-openrouter-deepseek-flash` | OpenRouter `deepseek/deepseek-v4.1-flash-1017` | `OPENROUTER_API_KEY` |

The OpenRouter deployment pins its provider to DeepSeek and disables provider
fallbacks. LiteLLM retries and fallback deployments are also disabled. The two
model names are independent routes; Harnessa does not automatically switch from
one to the other.

## Start and inspect

The normal repository setup supplies the non-provider application settings in
`.env`. Keep provider credentials in the operator's secret source (here,
`~/.zshrc`) rather than copying them into Harnessa configuration. Compose
passes the declared provider variables only to LiteLLM; they are not sent to
the API, worker, web, or agent containers.

The provider variables in `.env.example` are intentionally blank. Start the
gateway from a login-interactive zsh session where the keys are exported by
`~/.zshrc`; do not copy the key values into `.env`. Compose's configured project
name is `harnessa`, which is also used by the normal local stack, so the API and
worker can resolve `litellm` on the internal network:

```sh
zsh -lic 'export DEEPSEEK_API_KEY OPENROUTER_API_KEY; podman compose --env-file .env -f infra/compose.yaml up -d litellm'
```

The login-interactive zsh loads `~/.zshrc`, and `export` makes the two
configured keys available for Compose interpolation in that child process.
This command does not print either value. Limit shell tracing and command
logging when invoking it from another tool. Compose forwards these variables to
LiteLLM only; the application API, worker, web, and agent containers receive no
provider key.

Inspect readiness and the configured aliases without invoking a model:

```sh
podman compose --env-file .env -f infra/compose.yaml exec -T litellm python -c '
import json
import urllib.request

for path in ("/health/readiness", "/v1/models"):
    with urllib.request.urlopen("http://127.0.0.1:4000" + path, timeout=5) as response:
        payload = json.load(response) if path == "/v1/models" else response.status
    if path == "/v1/models":
        print("models:", ", ".join(item["id"] for item in payload["data"]))
    else:
        print("readiness:", payload)
'
```

`/v1/models` confirms the configured aliases only. It does not validate provider
credentials or model availability; doing so requires a provider request.

## Register routes in Harnessa

LiteLLM configuration alone does not authorize application traffic. An
organization administrator must create a provider account and deployment using
the matching `env:DEEPSEEK_API_KEY` or `env:OPENROUTER_API_KEY` reference, then add
an alias and route it into the interactive or probe pool. Set
`ModelDeployment.proxy_model` to the exact LiteLLM name in the table above. Keep
the provider's real model identifier in `actual_model`; record the source and
date in `price_revision`.

Provider prices and routing policies can change. As of 2026-10-24, the
[DeepSeek pricing table](https://api-docs.deepseek.com/quick_start/pricing/) lists
peak rates of $0.30 per million cache-miss input tokens and $1.20 per million
output tokens for `deepseek-flash`; off-peak rates are half those amounts. The
model supports a 1M-token context and tool calls. Use
the peak rates as conservative Harnessa reservation metadata if registering this
route, and record a dated price revision. The OpenRouter route is pinned to its
[DeepSeek provider](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints),
but its catalog can still change; recheck both provider and OpenRouter pricing
before enabling or revising it. Harnessa's finite project budget is required for
every invocation, and reservations remain the enforcement boundary.

Every Harnessa invocation still requires its own finite project, task, and run
reservation. A missing reservation blocks the request even when the operator
has no separate global spend ceiling. Provider-backed calls require explicit
authorization; the model doctor and `/v1/models` checks do not make a provider
request.

## Stop the gateway

```sh
podman compose --env-file .env -f infra/compose.yaml stop litellm
```
