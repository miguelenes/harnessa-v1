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
`.env`. Copy only the provider keys from a trusted shell environment into that
ignored file, restrict its permissions, and never commit or print it. Compose
passes these two variables only to LiteLLM.

Start the gateway service after `.env` contains both variables. Compose's
configured project name is `harnessa`, which is also used by the normal local
stack, so the API and worker can resolve `litellm` on the internal network:

```sh
podman compose --env-file .env -f infra/compose.yaml up -d litellm
```

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

Provider prices and routing policies can change. As of 2026-10-23, the
[DeepSeek pricing table](https://api-docs.deepseek.com/quick_start/pricing/) lists
peak rates of $0.30 per million cache-miss input tokens and $1.20 per million
output tokens for `deepseek-flash`; off-peak rates are half those amounts. Use
the peak rates as conservative Harnessa reservation metadata if registering this
route, and record a dated price revision. The OpenRouter route is pinned to its
[DeepSeek provider](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints),
but its catalog can still change; recheck both provider and OpenRouter pricing
before enabling or revising it. Harnessa's finite project budget is required for
every invocation, and reservations remain the enforcement boundary.

No provider-backed request was made as part of this setup. The repository's
`AGENTS.md` requires bounded authorization before real provider usage. Provide a
finite per-experiment/project spend cap before running the M12 live acceptance
experiment. M12 also needs the independently permissioned evaluator and holdout
dataset described in [the milestone report](../milestones/12.md).

## Stop the gateway

```sh
podman compose --env-file .env -f infra/compose.yaml stop litellm
```
