# Self-hosted operator guide

## Supported deployment boundary

Use the digest-pinned PostgreSQL 16, Redis 8, LiteLLM Proxy, API, web, and worker services in `infra/compose.yaml`. The API, web, worker, database, and Redis share only the internal application network. LiteLLM alone joins provider egress. The agent execution container remains isolated by the runner policy; Compose networking is not a replacement for that sandbox. Put a maintained reverse proxy in front of the web and API endpoints, terminate TLS there, and set `COOKIE_SECURE=true`, explicit `ALLOWED_ORIGINS`, a strong `SECRET_KEY`, and a one-time `BOOTSTRAP_TOKEN` through the deployment's secret manager.

The Compose file now stores content-addressed artifacts in the named `artifacts` volume shared by API and worker. Before upgrading an older deployment that stored artifacts only in the API container writable layer, copy `/var/lib/harnessa/artifacts` from the existing API container into a backup and restore it to the new volume before recreating the API. Do not remove the previous container or its storage until checksums and database artifact references have been verified.

## Bootstrap and configure

1. Configure database credentials, `SECRET_KEY`, one-time `BOOTSTRAP_TOKEN`, TLS origin and cookie settings through the deployment secret manager. Keep provider keys in the trusted secret source and pass them only to LiteLLM; for the local zsh-backed setup, follow [model-gateway-local.md](development/model-gateway-local.md). Never commit secret values.
2. Set administrator identity and allowed repository target IDs. Create the project and repository grant through the authorized setup/API flow. A GitHub App installation additionally needs an App ID, private key, webhook secret, installation and explicit repository opt-in; absent credentials remain disabled.
3. Start the pinned services with `podman compose --project-name harnessa -f infra/compose.yaml up -d --build`. The API runs the forward Alembic upgrade before readiness. Review logs for migration and service health without printing secret-bearing environment values.
4. Run `scripts/harnessa-doctor.sh`. It checks secret presence and cookie mode from the running API container without printing secret values. A passing LiteLLM readiness check means only that the proxy process is ready; it does not prove provider credentials, route capability, an authorized spend, GitHub access, or a working runner. Use the Model routing view's bounded live-check only when you intend to spend through the configured project budget.
5. Configure primary and probe pools, route capability requirements, and project/task/run limits before enabling model-backed tasks. These controls remain authoritative even when the host itself has no spend ceiling.
6. Enable only a tested trusted runner with rootless Podman and a pinned image. Do not pass provider secrets or evaluator holdout data to the runner.

## M13 live acceptance prerequisites

Complete these steps before a provider-backed agent run or external GitHub effect:

1. If a provider key may have been exposed, revoke it in that provider's account before replacing it. Store the replacement only in the operator's secret source (for the local setup, `~/.zshrc`), start a fresh zsh login environment, and recreate the LiteLLM service so it receives the new environment. Never paste a key into chat, a command argument, a report, or Harnessa's database. The doctor checks presence only; it does not send a provider request.
2. Use one dedicated disposable Harnessa project for all authorized remaining-milestone model traffic. Configure finite project, task, and run limits for both interactive and probe pools. The two project-level USD ceilings are enforced separately; set their combined maximum to **no more than USD 18** (for example, USD 9 per pool) and lower it if the current EUR/USD rate makes USD 18 exceed the €20 aggregate authorization. Include provider, evaluator, and judge charges, and count the previously recorded USD 0.0000228 route checks. Keep task and run limits below those project limits. Check billed, estimated, reserved, and unknown usage before authorizing each live scenario. Do not start another project to reset the ceiling.
3. Create the GitHub App with only the permissions and webhook subscriptions in [the GitHub App integration guide](integrations/github-app.md). Set repository selection to **Only select repositories**, install it only on `miguelenes/harnessa-m13-acceptance-20261024`, register and refresh the installation in Harnessa, and explicitly opt in that single repository. Confirm the displayed account, repository ID, granted permissions, and required checks before allowing an issue or pull-request effect.
4. For browser acceptance, enable Chrome remote debugging at `chrome://inspect/#remote-debugging`. Verify that Browser Harness reports a live browser connection before using the logged-in session. Do not run the browser scenarios while its CDP endpoint is unavailable.
5. Run the bounded real-agent, budget-contention, webhook-replay, cancellation/reconnect, and archive scenarios against that disposable project and repository. Retain redacted request IDs, task/run IDs, GitHub effect IDs, costs, and exact revisions in the acceptance report; never retain keys or hidden grader material.

## Readiness and routine operation

`GET /health/live` checks process liveness. `GET /health/ready` checks application dependencies. The Compose healthchecks gate API and worker startup on PostgreSQL, Redis, and LiteLLM process readiness. A deployment doctor result does not replace an authorized model-route probe or a GitHub installation check.

Use the workspace Events, Routing and GitHub views for durable event connectivity, ledger/budget state, and webhook/effect status. A browser reconnect replays durable events and does not start/cancel work. Inspect stale runs and dead letters using project-authorized APIs. Dead-letter replay requires an authorized identity, an operator reason, and an idempotency key; it is audited. Reconcile unknown external effects before any manual retry.

## Execution and evaluation controls

In Setup & Health, choose a project execution adapter and save separate interactive/primary and probe project/task/run USD and token ceilings. `deployment-default` uses the worker's configured runner; `acp-opencode` requires `ACP_EXECUTION_ENABLED` and the matching repository source mapping; `disabled` prevents dispatch. The readiness detail reports when the selected runner is not available.

The evaluation center first dry-runs a bounded Harness² suite. It freezes at most 20 READY probe tasks, route alias, specification/base/policy revisions and the Harness² revision, and reports a conservative full-context estimate. It counts both parallel and sequential lanes when mode is `both`, plus configured model steps/retries and the Harness² policy multiplier. The estimate must fit each task's remaining probe ceiling, the run ceiling and remaining project ceiling. Create then records one immutable group and idempotent child experiments; cancellation is checkpointed by child workers, and resume is limited to failed children with unchanged frozen inputs. Review missing samples and the delivery Wilson interval alongside known cost and latency; no external benchmark result is implied.

API request logs are structured JSON and carry validated `X-Request-ID` and `X-Trace-ID` correlation values. W3C trace context is persisted across task/run dispatch and GitHub webhook processing; model routing and GitHub effects add child spans. Configure a trusted OTLP/HTTP collector to export traces from API and worker as described in [distributed tracing](development/tracing.md). Spans omit prompts, completions, webhook bodies, credentials and raw exception messages. Without a collector endpoint, trace IDs remain available in logs but spans are not exported. The API keeps process-local HTTP request counters and latency histograms by method, route template, and status. Its durable operational gauges include queued-run count/age, expired leases, open dead letters, unpublished outbox count/age, unreconciled effect count/age, pending/unknown reservation count and reserved USD, artifact count/bytes/24-hour growth, GitHub webhook outbox count/age, failed cleanup steps, and connected route candidates. The database snapshot is cached for five seconds and uses no tenant identifiers as metric labels. A failed database collection is reported with `harnessa_operational_metrics_collection_success 0` and does not include exception text.

To scrape metrics, configure a high-entropy `METRICS_TOKEN` in the API service's secret environment and request `/internal/metrics` with `Authorization: Bearer <token>`. Without a non-empty token the endpoint stays hidden. Route candidate counts reflect enabled database configuration and capacity only; they do not prove provider reachability. OpenTelemetry span export is supported but remains disabled unless a trusted collector endpoint is configured. Keep the endpoint private to the monitoring network. Do not expose raw prompts, completions, webhook bodies, provider credentials, or hidden grader content in logs.

## Backup

Place Git workspace roots that contain OpenSpec and harness repositories into the colon-separated `HARNESSA_GIT_DATA_ROOTS` variable. The backup script includes their complete Git metadata when configured; paths must be absolute. If these sources live in a separate managed Git service, back up that service's refs independently and record its immutable revision IDs.

```bash
scripts/harnessa-backup.sh /secure/backup/harnessa-$(date -u +%Y%m%dT%H%M%SZ)
```

The script refuses to overwrite an existing destination, stops API and worker for a consistent database/artifact snapshot, and restarts them on exit. The backup contains a PostgreSQL custom-format dump, the artifacts volume, deployment config references, source revision, and optional Git roots. It deliberately excludes `.env`, secrets and private keys. Store the bundle encrypted and off-host. Protect the backup like the data it contains.

## Upgrade and rollback

Back up first. Apply a release's forward migrations with the standard API startup and verify `GET /health/ready`, the migration head, and task/artifact provenance before enabling dispatch. Releases support additive forward migrations; the application rollback policy is to stop API and workers, restore the pre-upgrade database and artifacts into a fresh compatible deployment, then run the prior pinned image. Do not downgrade a database in place unless that release's milestone report names and validates a specific downgrade path. External PRs, webhook deliveries and provider charges are not undone by application rollback.

Do not remove named volumes or Git workspaces as part of an application upgrade. For uninstall, stop the Compose project, export backups, and separately verify which named volumes, repository clones, GitHub App links and host runner paths are owned by this installation. Delete only those owned resources after preserving required audit/evidence retention.
