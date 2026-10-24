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

## Readiness and routine operation

`GET /health/live` checks process liveness. `GET /health/ready` checks application dependencies. The Compose healthchecks gate API and worker startup on PostgreSQL, Redis, and LiteLLM process readiness. A deployment doctor result does not replace an authorized model-route probe or a GitHub installation check.

Use the workspace Events, Routing and GitHub views for durable event connectivity, ledger/budget state, and webhook/effect status. A browser reconnect replays durable events and does not start/cancel work. Inspect stale runs and dead letters using project-authorized APIs. Dead-letter replay requires an authorized identity, an operator reason, and an idempotency key; it is audited. Reconcile unknown external effects before any manual retry.

## Execution and evaluation controls

In Setup & Health, choose a project execution adapter and save separate interactive/primary and probe project/task/run USD and token ceilings. `deployment-default` uses the worker's configured runner; `acp-opencode` requires `ACP_EXECUTION_ENABLED` and the matching repository source mapping; `disabled` prevents dispatch. The readiness detail reports when the selected runner is not available.

The evaluation center first dry-runs a bounded Harness² suite. It freezes at most 20 READY probe tasks, route alias, specification/base/policy revisions and the Harness² revision, and reports a conservative full-context estimate. It counts both parallel and sequential lanes when mode is `both`, plus configured model steps/retries and the Harness² policy multiplier. The estimate must fit each task's remaining probe ceiling, the run ceiling and remaining project ceiling. Create then records one immutable group and idempotent child experiments; cancellation is checkpointed by child workers, and resume is limited to failed children with unchanged frozen inputs. Review missing samples and the delivery Wilson interval alongside known cost and latency; no external benchmark result is implied.

API request logs are structured JSON and carry validated `X-Request-ID` and `X-Trace-ID` correlation values. The API also keeps process-local HTTP request counters and latency histograms by method, route template, and status. To scrape them, configure a high-entropy `METRICS_TOKEN` in the API service's secret environment and request `/internal/metrics` with `Authorization: Bearer <token>`. Without a non-empty token the endpoint stays hidden. These metrics do not yet cover queue age, stale leases, dead letters, provider reconciliation, external effects, or artifact growth, and Harnessa does not yet export OpenTelemetry spans. Keep the endpoint private to the monitoring network. Do not expose raw prompts, completions, webhook bodies, provider credentials, or hidden grader content in logs.

## Backup

Place Git workspace roots that contain OpenSpec and harness repositories into the colon-separated `HARNESSA_GIT_DATA_ROOTS` variable. The backup script includes their complete Git metadata when configured; paths must be absolute. If these sources live in a separate managed Git service, back up that service's refs independently and record its immutable revision IDs.

```bash
scripts/harnessa-backup.sh /secure/backup/harnessa-$(date -u +%Y%m%dT%H%M%SZ)
```

The script refuses to overwrite an existing destination, stops API and worker for a consistent database/artifact snapshot, and restarts them on exit. The backup contains a PostgreSQL custom-format dump, the artifacts volume, deployment config references, source revision, and optional Git roots. It deliberately excludes `.env`, secrets and private keys. Store the bundle encrypted and off-host. Protect the backup like the data it contains.

## Upgrade and rollback

Back up first. Apply a release's forward migrations with the standard API startup and verify `GET /health/ready`, the migration head, and task/artifact provenance before enabling dispatch. Releases support additive forward migrations; the application rollback policy is to stop API and workers, restore the pre-upgrade database and artifacts into a fresh compatible deployment, then run the prior pinned image. Do not downgrade a database in place unless that release's milestone report names and validates a specific downgrade path. External PRs, webhook deliveries and provider charges are not undone by application rollback.

Do not remove named volumes or Git workspaces as part of an application upgrade. For uninstall, stop the Compose project, export backups, and separately verify which named volumes, repository clones, GitHub App links and host runner paths are owned by this installation. Delete only those owned resources after preserving required audit/evidence retention.
