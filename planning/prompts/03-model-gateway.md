# Prompt 3 of 10 — LiteLLM routing and spend controls

Implement milestone 3 of Harnessa. Read `planning/revised-specification.md` and `docs/milestones/01.md` through `02.md`. Integrate with the existing domain contracts; preserve unrelated work. Complete this milestone and stop before milestone 4.

## Prerequisites

Durable requests, worker dispatch, authorization, and effect intent interfaces exist. Live credentials are optional for implementation; offline tests must remain usable.

## Implement

1. Run a pinned LiteLLM gateway and build an application adapter around its supported routing interfaces. Model provider accounts, deployments, model aliases, protocol capabilities, and workload pools separately. Ship two default pools: interactive/primary and probe/batch, with secret references instead of secret values in API responses.
2. Define typed route requirements for Chat Completions, Responses, Anthropic-compatible messages where needed, streaming, tools, context size, and usage reporting. Validate the compatibility of each route/agent pair. Do not assume a shared API base environment variable configures every CLI.
3. Implement bounded retries, Retry-After, deployment cooldowns, circuit breakers, explicit compatible fallback order, interactive capacity reservations, and configurable probe overflow limits. Handle both accounts unavailable. Fail clearly when no authorized compatible route exists; never silently substitute a lower-capability model.
4. Add project/task/run budget ceilings, pre-dispatch atomic reservations, maximum tokens/steps/time, release/reconciliation of reservations, and an append-only model request ledger. Capture actual deployment/account/model, request IDs, route decisions, tokens, price revision, latency, billed/estimated spend, and unknown usage. Retries and parallel probes share the correct parent ceilings.
5. Define separate behavior for pre-stream request failure, partial stream failure, and a run that already executed tools. A model transport retry must not repeat an entire agent workflow. Do not promise a fixed failover latency.
6. Provide configuration/status/usage APIs, a credential-free doctor check, and an explicitly invoked live route capability check. Restrict full credentials to the gateway; provide scoped short-lived credentials to runners later. Egress rules must enforce the gateway route rather than relying solely on prompt instructions.

## Acceptance

- Fault-injected 429/503/timeouts produce bounded retries, correct cooldown/fallback events, and accurate attribution.
- Concurrent dispatch cannot reserve more than the task/project ceiling; stale reservations are reconciled safely.
- Batch traffic cannot consume the configured interactive reserve.
- Partial streaming failure is visible and cannot duplicate tool side effects.
- API responses, database records intended for users, logs, and traces contain no provider secrets.

Use deterministic local provider fixtures for automated tests. With explicitly configured credentials and spending authorization, record one real compatibility check; otherwise label it pending and list the exact user-run setup command. Update compatibility documentation and write `docs/milestones/03.md` with measured facts and limitations.

## Archive this planning step

After every acceptance criterion above is met and `docs/milestones/03.md` records actual commands/results, limitations, and the next milestone's prerequisites, move this prompt from `planning/prompts/` to `planning/archive/completed/` without changing its contents. Update the matching milestone row in `planning/README.md` to link to the archived prompt and mark it **archived complete**. Do this only after the milestone is complete; if it is blocked or partial, keep this prompt active and record the state and evidence in the milestone report.
